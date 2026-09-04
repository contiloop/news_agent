"""Read one article in a news-agent-owned Chrome profile."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from news_agent.browser_tabs import _same_address
from news_agent.extraction import ReaderError, _same_title, extract_article_body
from news_agent.rss import DiscoveredItem
from news_agent.run_lock import RunLock, RunLockBusyError, RunLockError

__all__ = ["read_article_body_in_dedicated_chrome"]


_PROFILE_MARKER_NAME = ".news-agent-owned-profile"
_PROFILE_MARKER_CONTENT = "news-agent dedicated Chrome profile\n"
_POLL_INTERVAL_SECONDS = 0.25
_DIALOG_SELECTOR = 'dialog[open]:visible, [role="dialog"]:visible'
_DIALOG_BUTTON_SELECTOR = 'button:visible, [role="button"]:visible'
_CHROME_ARGUMENTS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--use-mock-keychain",
    "--password-store=basic",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--metrics-recording-only",
    "--disk-cache-size=52428800",
    "--media-cache-size=10485760",
)
_REGULAR_CHROME_ARGUMENTS = (
    "--no-startup-window",
    "--no-first-run",
    "--no-default-browser-check",
    "--disk-cache-size=52428800",
    "--media-cache-size=10485760",
)
_CDP_HOST = "127.0.0.1"
_CDP_POLL_INTERVAL_SECONDS = 0.1
_HIDDEN_CHROME_QUIET_POLLS = 10
_REGULAR_CHROME_MINIMIZE_TIMEOUT_SECONDS = 1.0
_OPEN_EXECUTABLE = Path("/usr/bin/open")
_PS_EXECUTABLE = Path("/bin/ps")
_PARAGRAPH_SCRIPT = """
elements => elements.map(element => {
  const style = window.getComputedStyle(element);
    return {
    tagName: element.tagName,
    innerText: element.innerText,
    "data-testid": element.getAttribute("data-testid"),
    hidden: Boolean(
      element.hidden ||
      style.display === "none" ||
      style.visibility === "hidden"
    ),
    offsetLeft: element.offsetLeft,
    offsetTop: element.offsetTop,
    offsetWidth: element.offsetWidth,
    offsetHeight: element.offsetHeight,
  };
})
"""
_VISIBLE_TEXT_SCRIPT = """
elements => elements.flatMap(element => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  if (
    element.hidden ||
    style.display === "none" ||
    style.visibility === "hidden" ||
    rect.width <= 0 ||
    rect.height <= 0
  ) {
    return [];
  }
  return [element.innerText];
})
"""


class _NonSuccessArticleResponseError(ReaderError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(
            "Dedicated Chrome received a non-success article response: "
            f"status={status}"
        )


@dataclass(frozen=True)
class _ChromeProcessIdentity:
    """PID plus a stable process-start token from one ps snapshot."""

    pid: int
    started_at: str


@dataclass
class _HiddenChromeProcess:
    """Identity needed to prove and clean up one hidden Chrome instance."""

    launcher: subprocess.Popen[bytes]
    executable: Path
    profile_directory: Path
    debugging_port: int
    chrome_identity: _ChromeProcessIdentity | None = None

    @property
    def chrome_pid(self) -> int | None:
        identity = self.chrome_identity
        return None if identity is None else identity.pid


async def read_article_body_in_dedicated_chrome(
    item: DiscoveredItem,
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    article_body_selector: str = "p",
    article_access_denied_selector: str | None = None,
    article_access_denied_phrases: Sequence[str] = (),
    launch_mode: Literal["headless", "regular_cdp", "hidden_cdp"] = "headless",
) -> str:
    """Return article prose without attaching to the user's normal Chrome."""

    executable_path = _validate_executable(executable)
    profile_path = _validate_profile_path(profile_directory)
    timeout = _validate_timeout(timeout_seconds)
    selector = _validate_selector(article_body_selector)
    access_selector, access_phrases = _validate_access_denied_detection(
        article_access_denied_selector,
        article_access_denied_phrases,
    )
    selected_launch_mode = _validate_launch_mode(launch_mode)
    article_url = str(item.url)
    if urlsplit(article_url).scheme.casefold() != "https":
        raise ReaderError("Dedicated Chrome only opens HTTPS article URLs")

    lock_file = profile_path.with_name(f"{profile_path.name}.lock")
    try:
        with RunLock(lock_file):
            _prepare_owned_profile(profile_path)
            return await _run_owned_browser(
                item,
                executable=executable_path,
                profile_directory=profile_path,
                timeout_seconds=timeout,
                article_body_selector=selector,
                article_access_denied_selector=access_selector,
                article_access_denied_phrases=access_phrases,
                launch_mode=selected_launch_mode,
            )
    except RunLockBusyError as exc:
        raise ReaderError("Dedicated Chrome profile is already in use") from exc
    except RunLockError as exc:
        raise ReaderError("Dedicated Chrome profile lock could not be used") from exc


async def _run_owned_browser(
    item: DiscoveredItem,
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    article_body_selector: str,
    article_access_denied_selector: str | None,
    article_access_denied_phrases: tuple[str, ...],
    launch_mode: Literal["headless", "regular_cdp", "hidden_cdp"],
) -> str:
    if launch_mode == "hidden_cdp":
        return await _run_owned_browser_hidden_cdp(
            item,
            executable=executable,
            profile_directory=profile_directory,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            article_access_denied_selector=article_access_denied_selector,
            article_access_denied_phrases=article_access_denied_phrases,
        )
    if launch_mode == "regular_cdp":
        return await _run_owned_browser_regular_cdp(
            item,
            executable=executable,
            profile_directory=profile_directory,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            article_access_denied_selector=article_access_denied_selector,
            article_access_denied_phrases=article_access_denied_phrases,
        )
    return await _run_owned_browser_headless(
        item,
        executable=executable,
        profile_directory=profile_directory,
        timeout_seconds=timeout_seconds,
        article_body_selector=article_body_selector,
        article_access_denied_selector=article_access_denied_selector,
        article_access_denied_phrases=article_access_denied_phrases,
    )


async def _run_owned_browser_headless(
    item: DiscoveredItem,
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    article_body_selector: str,
    article_access_denied_selector: str | None,
    article_access_denied_phrases: tuple[str, ...],
) -> str:
    playwright: Any | None = None
    context: Any | None = None
    body: str | None = None
    primary_error: BaseException | None = None

    try:
        manager = async_playwright()
        playwright = await _bounded(
            manager.start(),
            timeout_seconds,
            "starting browser automation",
        )
        context = await _bounded(
            playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_directory),
                executable_path=str(executable),
                headless=True,
                args=list(_CHROME_ARGUMENTS),
                accept_downloads=False,
                service_workers="block",
                timeout=_milliseconds(timeout_seconds),
            ),
            timeout_seconds,
            "launching the owned browser",
        )
        body = await _read_page(
            context,
            item,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            article_access_denied_selector=article_access_denied_selector,
            article_access_denied_phrases=article_access_denied_phrases,
        )
    # Cleanup must also run for cancellation, KeyboardInterrupt, and SystemExit.
    except BaseException as exc:  # noqa: BLE001
        primary_error = exc

    cleanup_error, cleanup_cancellation = await _complete_cleanup(
        context,
        playwright,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(primary_error, asyncio.CancelledError):
        raise primary_error
    if cleanup_cancellation is not None:
        raise cleanup_cancellation
    if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
        raise primary_error
    if cleanup_error is not None:
        if primary_error is None:
            raise cleanup_error
        raise cleanup_error from primary_error
    if primary_error is not None:
        if isinstance(primary_error, ReaderError):
            raise primary_error
        sanitized = ReaderError("Dedicated Chrome could not read the article")
        raise sanitized from primary_error
    if body is None:
        raise ReaderError("Dedicated Chrome returned no article body")
    return body


async def _run_owned_browser_regular_cdp(
    item: DiscoveredItem,
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    article_body_selector: str,
    article_access_denied_selector: str | None,
    article_access_denied_phrases: tuple[str, ...],
) -> str:
    playwright: Any | None = None
    browser: Any | None = None
    process: subprocess.Popen[bytes] | None = None
    body: str | None = None
    primary_error: BaseException | None = None

    try:
        debugging_port = _reserve_debugging_port()
        process = _launch_regular_chrome_cdp(
            executable=executable,
            profile_directory=profile_directory,
            debugging_port=debugging_port,
        )
        manager = async_playwright()
        playwright = await _bounded(
            manager.start(),
            timeout_seconds,
            "starting browser automation",
        )
        await _bounded(
            _wait_for_regular_chrome_cdp(
                debugging_port,
                process,
                timeout_seconds=timeout_seconds,
            ),
            timeout_seconds,
            "waiting for regular Chrome CDP",
        )
        browser = await _bounded(
            playwright.chromium.connect_over_cdp(
                f"http://{_CDP_HOST}:{debugging_port}",
                timeout=_milliseconds(timeout_seconds),
            ),
            timeout_seconds,
            "connecting to regular Chrome CDP",
        )
        if not browser.contexts:
            raise ReaderError("Dedicated Chrome returned no browser context")
        body = await _read_page(
            browser.contexts[0],
            item,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            article_access_denied_selector=article_access_denied_selector,
            article_access_denied_phrases=article_access_denied_phrases,
            minimize_window=True,
        )
    # Cleanup must also run for cancellation, KeyboardInterrupt, and SystemExit.
    except BaseException as exc:  # noqa: BLE001
        primary_error = exc

    cleanup_error, cleanup_cancellation = await _complete_regular_chrome_cleanup(
        browser,
        playwright,
        process,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(primary_error, asyncio.CancelledError):
        raise primary_error
    if cleanup_cancellation is not None:
        raise cleanup_cancellation
    if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
        raise primary_error
    if cleanup_error is not None:
        if primary_error is None:
            raise cleanup_error
        raise cleanup_error from primary_error
    if primary_error is not None:
        if isinstance(primary_error, ReaderError):
            raise primary_error
        sanitized = ReaderError("Dedicated Chrome could not read the article")
        raise sanitized from primary_error
    if body is None:
        raise ReaderError("Dedicated Chrome returned no article body")
    return body


async def _run_owned_browser_hidden_cdp(
    item: DiscoveredItem,
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    article_body_selector: str,
    article_access_denied_selector: str | None,
    article_access_denied_phrases: tuple[str, ...],
) -> str:
    playwright: Any | None = None
    browser: Any | None = None
    process: _HiddenChromeProcess | None = None
    body: str | None = None
    primary_error: BaseException | None = None

    try:
        manager = async_playwright()
        playwright = await _bounded(
            manager.start(),
            timeout_seconds,
            "starting browser automation",
        )
        debugging_port = _reserve_debugging_port()
        process = _launch_hidden_chrome_cdp(
            executable=executable,
            profile_directory=profile_directory,
            debugging_port=debugging_port,
        )
        await _bounded(
            _wait_for_hidden_chrome_cdp(
                debugging_port,
                process,
                timeout_seconds=timeout_seconds,
            ),
            timeout_seconds,
            "waiting for hidden Chrome CDP",
        )
        browser = await _bounded(
            playwright.chromium.connect_over_cdp(
                f"http://{_CDP_HOST}:{debugging_port}",
                timeout=_milliseconds(timeout_seconds),
            ),
            timeout_seconds,
            "connecting to hidden Chrome CDP",
        )
        context, page = await _create_hidden_cdp_page(
            browser,
            timeout_seconds=timeout_seconds,
        )
        body = await _read_page(
            context,
            item,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            article_access_denied_selector=article_access_denied_selector,
            article_access_denied_phrases=article_access_denied_phrases,
            page=page,
        )
    # Cleanup must also run for cancellation, KeyboardInterrupt, and SystemExit.
    except BaseException as exc:  # noqa: BLE001
        primary_error = exc

    cleanup_error, cleanup_cancellation = await _complete_hidden_chrome_cleanup(
        browser,
        playwright,
        process,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(primary_error, asyncio.CancelledError):
        raise primary_error
    if cleanup_cancellation is not None:
        raise cleanup_cancellation
    if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
        raise primary_error
    if cleanup_error is not None:
        if primary_error is None:
            raise cleanup_error
        raise cleanup_error from primary_error
    if primary_error is not None:
        if isinstance(primary_error, ReaderError):
            raise primary_error
        sanitized = ReaderError("Dedicated Chrome could not read the article")
        raise sanitized from primary_error
    if body is None:
        raise ReaderError("Dedicated Chrome returned no article body")
    return body


async def _read_page(
    context: Any,
    item: DiscoveredItem,
    *,
    timeout_seconds: float,
    article_body_selector: str,
    article_access_denied_selector: str | None,
    article_access_denied_phrases: tuple[str, ...],
    minimize_window: bool = False,
    page: Any | None = None,
) -> str:
    if page is None:
        page = await _bounded(
            context.new_page(),
            timeout_seconds,
            "creating the owned article page",
        )
    if minimize_window:
        await _minimize_regular_chrome_page(page)
    dialog_seen = asyncio.Event()

    async def reject_dialog(dialog: Any) -> None:
        dialog_seen.set()
        try:
            await dialog.dismiss()
        except Exception:  # noqa: BLE001
            return

    page.on("dialog", reject_dialog)
    response = await _bounded(
        page.goto(
            str(item.url),
            wait_until="domcontentloaded",
            timeout=_milliseconds(timeout_seconds),
        ),
        timeout_seconds,
        "loading the article",
    )
    if response is None or not isinstance(response.status, int):
        raise ReaderError("Dedicated Chrome received no article response")
    if not 200 <= response.status < 300:
        raise _NonSuccessArticleResponseError(response.status)
    _require_expected_https_address(page.url, str(item.url))

    deadline = monotonic() + timeout_seconds
    last_extraction_error: ReaderError | None = None
    while monotonic() < deadline:
        _require_expected_https_address(page.url, str(item.url))
        if dialog_seen.is_set():
            raise ReaderError("Dedicated Chrome article opened a JavaScript dialog")
        remaining = max(0.001, deadline - monotonic())
        await _resolve_allowed_consent_dialog(page, remaining)

        headings = await _bounded(
            page.locator("h1:visible").all_inner_texts(),
            remaining,
            "checking the article title",
        )
        normalized_headings = [" ".join(text.split()) for text in headings]
        matching_headings = [
            heading
            for heading in normalized_headings
            if _same_title(heading, item.title)
        ]
        if len(matching_headings) == 1:
            paragraphs = await _bounded(
                page.locator(article_body_selector).evaluate_all(_PARAGRAPH_SCRIPT),
                remaining,
                "extracting article paragraphs",
            )
            if not isinstance(paragraphs, list) or not all(
                isinstance(paragraph, Mapping) for paragraph in paragraphs
            ):
                raise ReaderError(
                    "Dedicated Chrome returned invalid article paragraphs"
                )
            _require_expected_https_address(page.url, str(item.url))
            try:
                return extract_article_body(paragraphs)
            except ReaderError as exc:
                last_extraction_error = exc
                try:
                    access_denied = await _detect_access_denied(
                        page,
                        selector=article_access_denied_selector,
                        phrases=article_access_denied_phrases,
                        timeout_seconds=remaining,
                    )
                except Exception:  # noqa: BLE001
                    # Detection cannot weaken the normal body-extraction proof.
                    access_denied = False
                if access_denied:
                    raise ReaderError(
                        "Dedicated Chrome detected an access-restricted article",
                        reason="access_restricted",
                        retryable=False,
                    ) from exc

        await asyncio.sleep(
            min(_POLL_INTERVAL_SECONDS, max(0.0, deadline - monotonic()))
        )

    if last_extraction_error is not None:
        raise ReaderError(
            "Dedicated Chrome could not prove one complete article body"
        ) from last_extraction_error
    raise ReaderError("Dedicated Chrome could not verify the article title")


async def _detect_access_denied(
    page: Any,
    *,
    selector: str | None,
    phrases: tuple[str, ...],
    timeout_seconds: float,
) -> bool:
    if selector is None or not phrases:
        return False
    needle_phrases = tuple(phrase.casefold() for phrase in phrases)
    scope = page.locator(selector)
    texts = await _bounded(
        scope.evaluate_all(_VISIBLE_TEXT_SCRIPT),
        timeout_seconds,
        "checking for article access restrictions",
    )
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        return False

    if _contains_access_denied_phrase(texts, needle_phrases):
        return True

    frames = scope.locator("iframe:visible")
    frame_count = await _bounded(
        frames.count(),
        timeout_seconds,
        "checking article access restriction frames",
    )
    for index in range(frame_count):
        frame_text = await _bounded(
            frames.nth(index).content_frame.locator("body").inner_text(),
            timeout_seconds,
            "reading an article access restriction frame",
        )
        if isinstance(frame_text, str) and _contains_access_denied_phrase(
            (frame_text,),
            needle_phrases,
        ):
            return True
    return False


def _contains_access_denied_phrase(
    texts: Sequence[str],
    phrases: tuple[str, ...],
) -> bool:
    return any(
        phrase in " ".join(text.split()).casefold()
        for text in texts
        for phrase in phrases
    )


async def _resolve_allowed_consent_dialog(
    page: Any,
    timeout_seconds: float,
) -> None:
    """Dismiss only one legal notice whose sole choice is Continue."""

    try:
        dialogs = page.locator(_DIALOG_SELECTOR)
        count = await _bounded(
            dialogs.count(),
            timeout_seconds,
            "checking for article dialogs",
        )
        if count == 0:
            return
        if count != 1:
            raise ReaderError("Dedicated Chrome article contains a visible dialog")

        dialog = dialogs.first
        text = " ".join(
            (
                await _bounded(
                    dialog.inner_text(),
                    timeout_seconds,
                    "reading an article dialog",
                )
            ).split()
        ).casefold()
        buttons = dialog.locator(_DIALOG_BUTTON_SELECTOR)
        button_labels = [
            " ".join(label.split()).casefold()
            for label in await _bounded(
                buttons.all_inner_texts(),
                timeout_seconds,
                "reading article dialog choices",
            )
        ]
        if await _is_empty_feedback_dialog(
            dialog,
            text=text,
            button_labels=button_labels,
            timeout_seconds=timeout_seconds,
        ):
            return
        is_continue_only_legal_notice = (
            button_labels == ["continue"]
            and "by continuing" in text
            and "terms" in text
            and "privacy" in text
        )
        if not is_continue_only_legal_notice:
            raise ReaderError("Dedicated Chrome article contains a visible dialog")

        await _bounded(
            buttons.first.click(),
            timeout_seconds,
            "continuing past an article legal notice",
        )
        await _bounded(
            dialogs.first.wait_for(state="hidden"),
            timeout_seconds,
            "waiting for an article legal notice to close",
        )
    except ReaderError:
        raise
    except Exception as exc:
        raise ReaderError("Dedicated Chrome could not inspect article dialogs") from exc


async def _is_empty_feedback_dialog(
    dialog: Any,
    *,
    text: str,
    button_labels: list[str],
    timeout_seconds: float,
) -> bool:
    if text or button_labels:
        return False
    aria_modal = await _bounded(
        dialog.get_attribute("aria-modal"),
        timeout_seconds,
        "checking article dialog modality",
    )
    class_name = await _bounded(
        dialog.get_attribute("class"),
        timeout_seconds,
        "checking article dialog class",
    )
    return (
        aria_modal == "false"
        and isinstance(class_name, str)
        and "qsifeedbackbutton" in class_name.casefold()
    )


async def _cleanup_browser(
    context: Any | None,
    playwright: Any | None,
    *,
    timeout_seconds: float,
) -> ReaderError | asyncio.CancelledError | None:
    failed = False
    cancellation: asyncio.CancelledError | None = None
    if context is not None:
        try:
            await _bounded(
                context.close(),
                timeout_seconds,
                "closing the owned browser context",
            )
        # Record cancellation but continue so the Playwright process is stopped.
        except asyncio.CancelledError as exc:
            cancellation = exc
        except BaseException:  # noqa: BLE001
            failed = True
    if playwright is not None:
        try:
            await _bounded(
                playwright.stop(),
                timeout_seconds,
                "stopping browser automation",
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:  # noqa: BLE001
            failed = True
    if cancellation is not None:
        return cancellation
    if failed:
        return ReaderError("Dedicated Chrome cleanup could not be verified")
    return None


async def _cleanup_regular_chrome(
    browser: Any | None,
    playwright: Any | None,
    process: subprocess.Popen[bytes] | None,
    *,
    timeout_seconds: float,
) -> ReaderError | asyncio.CancelledError | None:
    failed = False
    cancellation: asyncio.CancelledError | None = None
    if browser is not None:
        try:
            await _bounded(
                browser.close(),
                timeout_seconds,
                "closing the regular Chrome CDP session",
            )
        except asyncio.CancelledError as exc:
            cancellation = exc
        except BaseException:  # noqa: BLE001
            failed = True
    if playwright is not None:
        try:
            await _bounded(
                playwright.stop(),
                timeout_seconds,
                "stopping browser automation",
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:  # noqa: BLE001
            failed = True
    if process is not None:
        try:
            await _terminate_regular_chrome_cdp(
                process,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:  # noqa: BLE001
            failed = True
    if cancellation is not None:
        return cancellation
    if failed:
        return ReaderError("Dedicated Chrome cleanup could not be verified")
    return None


async def _cleanup_hidden_chrome(
    browser: Any | None,
    playwright: Any | None,
    process: _HiddenChromeProcess | None,
    *,
    timeout_seconds: float,
) -> ReaderError | asyncio.CancelledError | None:
    failed = False
    cancellation: asyncio.CancelledError | None = None
    if browser is not None:
        try:
            await _bounded(
                browser.close(),
                timeout_seconds,
                "closing the hidden Chrome CDP session",
            )
        except asyncio.CancelledError as exc:
            cancellation = exc
        except BaseException:  # noqa: BLE001
            failed = True
    if playwright is not None:
        try:
            await _bounded(
                playwright.stop(),
                timeout_seconds,
                "stopping browser automation",
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:  # noqa: BLE001
            failed = True
    if process is not None:
        try:
            await _terminate_hidden_chrome_cdp(
                process,
                timeout_seconds=timeout_seconds,
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:  # noqa: BLE001
            failed = True
    if cancellation is not None:
        return cancellation
    if failed:
        return ReaderError("Dedicated Chrome cleanup could not be verified")
    return None


async def _complete_cleanup(
    context: Any | None,
    playwright: Any | None,
    *,
    timeout_seconds: float,
) -> tuple[ReaderError | asyncio.CancelledError | None, asyncio.CancelledError | None]:
    """Shield owned-browser cleanup and retain cancellation for later propagation."""

    cleanup_task = asyncio.create_task(
        _cleanup_browser(
            context,
            playwright,
            timeout_seconds=timeout_seconds,
        )
    )
    outer_cancellation: asyncio.CancelledError | None = None
    try:
        cleanup_error = await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as exc:
        if cleanup_task.cancelled():
            cleanup_error = exc
        else:
            outer_cancellation = exc
            try:
                cleanup_error = await cleanup_task
            except asyncio.CancelledError as cleanup_exc:
                cleanup_error = cleanup_exc
            except BaseException:  # noqa: BLE001
                cleanup_error = ReaderError(
                    "Dedicated Chrome cleanup could not be verified"
                )
    return cleanup_error, outer_cancellation


async def _complete_regular_chrome_cleanup(
    browser: Any | None,
    playwright: Any | None,
    process: subprocess.Popen[bytes] | None,
    *,
    timeout_seconds: float,
) -> tuple[ReaderError | asyncio.CancelledError | None, asyncio.CancelledError | None]:
    """Shield regular-Chrome cleanup and retain cancellation for later propagation."""

    cleanup_task = asyncio.create_task(
        _cleanup_regular_chrome(
            browser,
            playwright,
            process,
            timeout_seconds=timeout_seconds,
        )
    )
    outer_cancellation: asyncio.CancelledError | None = None
    try:
        cleanup_error = await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as exc:
        if cleanup_task.cancelled():
            cleanup_error = exc
        else:
            outer_cancellation = exc
            try:
                cleanup_error = await cleanup_task
            except asyncio.CancelledError as cleanup_exc:
                cleanup_error = cleanup_exc
            except BaseException:  # noqa: BLE001
                cleanup_error = ReaderError(
                    "Dedicated Chrome cleanup could not be verified"
                )
    return cleanup_error, outer_cancellation


async def _complete_hidden_chrome_cleanup(
    browser: Any | None,
    playwright: Any | None,
    process: _HiddenChromeProcess | None,
    *,
    timeout_seconds: float,
) -> tuple[ReaderError | asyncio.CancelledError | None, asyncio.CancelledError | None]:
    """Shield hidden-Chrome cleanup and retain cancellation for propagation."""

    cleanup_task = asyncio.create_task(
        _cleanup_hidden_chrome(
            browser,
            playwright,
            process,
            timeout_seconds=timeout_seconds,
        )
    )
    outer_cancellation: asyncio.CancelledError | None = None
    try:
        cleanup_error = await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as exc:
        if cleanup_task.cancelled():
            cleanup_error = exc
        else:
            outer_cancellation = exc
            try:
                cleanup_error = await cleanup_task
            except asyncio.CancelledError as cleanup_exc:
                cleanup_error = cleanup_exc
            except BaseException:  # noqa: BLE001
                cleanup_error = ReaderError(
                    "Dedicated Chrome cleanup could not be verified"
                )
    return cleanup_error, outer_cancellation


async def _create_hidden_cdp_page(
    browser: Any,
    *,
    timeout_seconds: float,
) -> tuple[Any, Any]:
    """Create one background target and return its surfaced context and page."""

    known_pages = tuple(
        page
        for context in browser.contexts
        for page in context.pages
    )
    session: Any | None = None
    selected: tuple[Any, Any] | None = None
    primary_error: BaseException | None = None

    try:
        session = await _bounded(
            browser.new_browser_cdp_session(),
            timeout_seconds,
            "opening the background target session",
        )
        target = await _bounded(
            session.send(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "background": True,
                    "focus": False,
                },
            ),
            timeout_seconds,
            "creating the background Chrome target",
        )
        target_id = target.get("targetId") if isinstance(target, Mapping) else None
        if not isinstance(target_id, str) or not target_id:
            raise ReaderError(
                "Dedicated Chrome returned no background target identity"
            )

        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            new_pages = [
                (context, page)
                for context in browser.contexts
                for page in context.pages
                if all(page is not known_page for known_page in known_pages)
            ]
            if len(new_pages) > 1:
                raise ReaderError(
                    "Dedicated Chrome surfaced multiple background target pages"
                )
            if len(new_pages) == 1:
                selected = new_pages[0]
                break
            await asyncio.sleep(
                min(
                    _CDP_POLL_INTERVAL_SECONDS,
                    max(0.0, deadline - monotonic()),
                )
            )
        if selected is None:
            raise ReaderError(
                "Dedicated Chrome background target was not surfaced"
            )
    except BaseException as exc:  # noqa: BLE001
        primary_error = exc

    detach_error: BaseException | None = None
    if session is not None:
        try:
            await _bounded(
                session.detach(),
                timeout_seconds,
                "closing the background target session",
            )
        except BaseException as exc:  # noqa: BLE001
            detach_error = exc

    if isinstance(primary_error, asyncio.CancelledError):
        raise primary_error
    if isinstance(detach_error, asyncio.CancelledError):
        raise detach_error
    if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
        raise primary_error
    if primary_error is not None:
        if isinstance(primary_error, ReaderError):
            raise primary_error
        raise ReaderError("Dedicated Chrome could not create a background page") from (
            primary_error
        )
    if detach_error is not None:
        if isinstance(detach_error, (KeyboardInterrupt, SystemExit)):
            raise detach_error
        raise ReaderError("Dedicated Chrome could not close its target session") from (
            detach_error
        )
    if selected is None:
        raise ReaderError("Dedicated Chrome background target was not surfaced")
    return selected


async def _minimize_regular_chrome_page(page: Any) -> None:
    """Best-effort minimize for non-headless Chrome windows driven over CDP."""

    session: Any | None = None
    try:
        session = await asyncio.wait_for(
            page.context.new_cdp_session(page),
            timeout=_REGULAR_CHROME_MINIMIZE_TIMEOUT_SECONDS,
        )
        window_info = await asyncio.wait_for(
            session.send("Browser.getWindowForTarget"),
            timeout=_REGULAR_CHROME_MINIMIZE_TIMEOUT_SECONDS,
        )
        window_id = (
            window_info.get("windowId")
            if isinstance(window_info, Mapping)
            else None
        )
        if not isinstance(window_id, int) or isinstance(window_id, bool):
            return
        await asyncio.wait_for(
            session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"windowState": "minimized"},
                },
            ),
            timeout=_REGULAR_CHROME_MINIMIZE_TIMEOUT_SECONDS,
        )
    except Exception:
        return
    finally:
        if session is not None:
            try:
                await asyncio.wait_for(
                    session.detach(),
                    timeout=_REGULAR_CHROME_MINIMIZE_TIMEOUT_SECONDS,
                )
            except Exception:
                return


async def _bounded(awaitable: Any, timeout_seconds: float, action: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise ReaderError(f"Dedicated Chrome timed out while {action}") from exc


def _validate_executable(executable: Path) -> Path:
    if not isinstance(executable, Path) or not executable.is_absolute():
        raise ReaderError("Dedicated Chrome executable must be an absolute path")
    if executable.is_symlink():
        raise ReaderError("Dedicated Chrome executable cannot be a symbolic link")
    try:
        metadata = executable.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReaderError("Dedicated Chrome executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(executable, os.X_OK):
        raise ReaderError("Dedicated Chrome executable is not an executable file")
    return executable.resolve()


def _validate_profile_path(profile_directory: Path) -> Path:
    if not isinstance(profile_directory, Path) or not profile_directory.is_absolute():
        raise ReaderError("Dedicated Chrome profile must be an absolute path")
    if profile_directory.is_symlink():
        raise ReaderError("Dedicated Chrome profile cannot be a symbolic link")
    try:
        profile = profile_directory.resolve(strict=False)
        home = Path.home().resolve()
        personal_chrome = (
            home / "Library" / "Application Support" / "Google" / "Chrome"
        ).resolve()
    except (OSError, RuntimeError) as exc:
        raise ReaderError("Dedicated Chrome profile path is invalid") from exc
    if (
        profile == Path(profile.anchor)
        or profile == home
        or profile == personal_chrome
        or personal_chrome in profile.parents
    ):
        raise ReaderError("Dedicated Chrome profile path is not safely isolated")
    if profile.exists():
        try:
            metadata = profile.stat(follow_symlinks=False)
        except OSError as exc:
            raise ReaderError("Dedicated Chrome profile path is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ReaderError("Dedicated Chrome profile path is not a directory")
    return profile


def _prepare_owned_profile(profile_directory: Path) -> None:
    try:
        profile_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile_directory.chmod(0o700)
        marker = profile_directory / _PROFILE_MARKER_NAME
        entries = tuple(profile_directory.iterdir())
        if marker not in entries:
            if entries:
                raise ReaderError("Dedicated Chrome profile is not owned by news-agent")
            descriptor = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(descriptor, _PROFILE_MARKER_CONTENT.encode("utf-8"))
            finally:
                os.close(descriptor)
        marker_metadata = marker.stat(follow_symlinks=False)
        if not stat.S_ISREG(marker_metadata.st_mode):
            raise ReaderError("Dedicated Chrome profile ownership marker is invalid")
        if marker.read_text(encoding="utf-8") != _PROFILE_MARKER_CONTENT:
            raise ReaderError("Dedicated Chrome profile ownership marker is invalid")
    except ReaderError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ReaderError("Dedicated Chrome profile could not be prepared") from exc


def _validate_timeout(value: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 < float(value) < float("inf")
    ):
        raise ReaderError("Dedicated Chrome timeout must be positive and finite")
    return float(value)


def _validate_selector(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReaderError("Dedicated Chrome article selector cannot be empty")
    return value.strip()


def _validate_access_denied_detection(
    selector: str | None,
    phrases: Sequence[str],
) -> tuple[str | None, tuple[str, ...]]:
    if isinstance(phrases, (str, bytes)) or not isinstance(phrases, Sequence):
        raise ReaderError("Dedicated Chrome access denied phrases are invalid")
    normalized_phrases = tuple(
        phrase.strip() if isinstance(phrase, str) else ""
        for phrase in phrases
    )
    if any(not phrase for phrase in normalized_phrases):
        raise ReaderError("Dedicated Chrome access denied phrases cannot be empty")
    folded_phrases = tuple(phrase.casefold() for phrase in normalized_phrases)
    if len(folded_phrases) != len(set(folded_phrases)):
        raise ReaderError("Dedicated Chrome access denied phrases must be unique")

    if selector is None:
        if normalized_phrases:
            raise ReaderError(
                "Dedicated Chrome access denied selector and phrases are "
                "required together"
            )
        return None, ()
    if not isinstance(selector, str) or not selector.strip():
        raise ReaderError("Dedicated Chrome access denied selector cannot be empty")
    if not normalized_phrases:
        raise ReaderError(
            "Dedicated Chrome access denied selector and phrases are required together"
        )
    return selector.strip(), normalized_phrases


def _validate_launch_mode(
    value: str,
) -> Literal["headless", "regular_cdp", "hidden_cdp"]:
    if value in {"headless", "regular_cdp", "hidden_cdp"}:
        return value
    raise ReaderError("Dedicated Chrome launch mode is unsupported")


def _reserve_debugging_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((_CDP_HOST, 0))
            return int(sock.getsockname()[1])
    except OSError as exc:
        raise ReaderError("Dedicated Chrome could not reserve a CDP port") from exc


def _launch_regular_chrome_cdp(
    *,
    executable: Path,
    profile_directory: Path,
    debugging_port: int,
) -> subprocess.Popen[bytes]:
    command = [
        str(executable),
        f"--user-data-dir={profile_directory}",
        f"--remote-debugging-address={_CDP_HOST}",
        f"--remote-debugging-port={debugging_port}",
        *_REGULAR_CHROME_ARGUMENTS,
        "about:blank",
    ]
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise ReaderError("Dedicated Chrome could not start regular Chrome") from exc


def _launch_hidden_chrome_cdp(
    *,
    executable: Path,
    profile_directory: Path,
    debugging_port: int,
) -> _HiddenChromeProcess:
    if sys.platform != "darwin":
        raise ReaderError("Dedicated Chrome hidden mode requires macOS")
    application = _application_bundle_for_executable(executable)
    command = [
        str(_OPEN_EXECUTABLE),
        "-W",
        "-j",
        "-g",
        "-n",
        "-a",
        str(application),
        "--args",
        f"--user-data-dir={profile_directory}",
        f"--remote-debugging-address={_CDP_HOST}",
        f"--remote-debugging-port={debugging_port}",
        *_REGULAR_CHROME_ARGUMENTS,
    ]
    try:
        launcher = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise ReaderError("Dedicated Chrome could not start hidden Chrome") from exc
    return _HiddenChromeProcess(
        launcher=launcher,
        executable=executable,
        profile_directory=profile_directory,
        debugging_port=debugging_port,
    )


def _application_bundle_for_executable(executable: Path) -> Path:
    for candidate in (executable, *executable.parents):
        if candidate.suffix.casefold() == ".app":
            return candidate
    raise ReaderError(
        "Dedicated Chrome hidden mode requires an executable in an app bundle"
    )


async def _wait_for_regular_chrome_cdp(
    debugging_port: int,
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if process.poll() is not None:
            raise ReaderError(
                "Dedicated Chrome regular Chrome exited before CDP was ready"
            )
        remaining = max(0.001, deadline - monotonic())
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(_CDP_HOST, debugging_port),
                timeout=min(_CDP_POLL_INTERVAL_SECONDS, remaining),
            )
            try:
                writer.write(
                    (
                        "GET /json/version HTTP/1.1\r\n"
                        f"Host: {_CDP_HOST}:{debugging_port}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("ascii")
                )
                await writer.drain()
                status_line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=min(_CDP_POLL_INTERVAL_SECONDS, remaining),
                )
            finally:
                writer.close()
                await writer.wait_closed()
            if b" 200 " in status_line:
                return
        except (OSError, TimeoutError):
            await asyncio.sleep(min(_CDP_POLL_INTERVAL_SECONDS, remaining))
    raise ReaderError("Dedicated Chrome regular Chrome CDP endpoint was not ready")


async def _wait_for_hidden_chrome_cdp(
    debugging_port: int,
    process: _HiddenChromeProcess,
    *,
    timeout_seconds: float,
) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        matches = _matching_hidden_chrome_processes(process)
        if len(matches) > 1:
            raise ReaderError(
                "Dedicated Chrome could not prove one hidden Chrome process"
            )
        if matches:
            if not _pin_hidden_chrome_identity(process, matches[0]):
                raise ReaderError(
                    "Dedicated Chrome hidden Chrome process identity changed"
                )
        if process.launcher.poll() is not None and not matches:
            raise ReaderError(
                "Dedicated Chrome hidden Chrome exited before CDP was ready"
            )

        remaining = max(0.001, deadline - monotonic())
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(_CDP_HOST, debugging_port),
                timeout=min(_CDP_POLL_INTERVAL_SECONDS, remaining),
            )
            try:
                writer.write(
                    (
                        "GET /json/version HTTP/1.1\r\n"
                        f"Host: {_CDP_HOST}:{debugging_port}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("ascii")
                )
                await writer.drain()
                status_line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=min(_CDP_POLL_INTERVAL_SECONDS, remaining),
                )
            finally:
                writer.close()
                await writer.wait_closed()
            if b" 200 " in status_line:
                ready_matches = _matching_hidden_chrome_processes(process)
                if len(ready_matches) > 1:
                    raise ReaderError(
                        "Dedicated Chrome could not prove one hidden Chrome process"
                    )
                if ready_matches:
                    if not _pin_hidden_chrome_identity(
                        process,
                        ready_matches[0],
                    ):
                        raise ReaderError(
                            "Dedicated Chrome hidden Chrome process identity changed"
                        )
                    return
        except (OSError, TimeoutError):
            await asyncio.sleep(min(_CDP_POLL_INTERVAL_SECONDS, remaining))
    raise ReaderError("Dedicated Chrome hidden Chrome CDP endpoint was not ready")


def _matching_hidden_chrome_processes(
    process: _HiddenChromeProcess,
) -> tuple[_ChromeProcessIdentity, ...]:
    try:
        result = subprocess.run(
            [
                str(_PS_EXECUTABLE),
                "-ww",
                "-axo",
                "pid=,lstart=,command=",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReaderError(
            "Dedicated Chrome could not inspect the hidden Chrome process"
        ) from exc
    if result.returncode != 0:
        raise ReaderError(
            "Dedicated Chrome could not inspect the hidden Chrome process"
        )

    matches: list[_ChromeProcessIdentity] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=6)
        if len(fields) != 7:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        command = fields[6]
        if _is_hidden_chrome_command(process, command):
            matches.append(
                _ChromeProcessIdentity(
                    pid=pid,
                    started_at=" ".join(fields[1:6]),
                )
            )
    return tuple(sorted(set(matches), key=lambda identity: identity.pid))


def _matching_hidden_chrome_pids(process: _HiddenChromeProcess) -> tuple[int, ...]:
    return tuple(
        identity.pid
        for identity in _matching_hidden_chrome_processes(process)
    )


def _pin_hidden_chrome_identity(
    process: _HiddenChromeProcess,
    identity: _ChromeProcessIdentity,
) -> bool:
    pinned = process.chrome_identity
    if pinned is None:
        process.chrome_identity = identity
        return True
    return pinned == identity


def _is_hidden_chrome_command(
    process: _HiddenChromeProcess,
    command: str,
) -> bool:
    executable_prefix = f"{process.executable} "
    return (
        command.startswith(executable_prefix)
        and _command_has_exact_flag(
            command,
            f"--user-data-dir={process.profile_directory}",
        )
        and _command_has_exact_flag(
            command,
            f"--remote-debugging-port={process.debugging_port}",
        )
    )


def _command_has_exact_flag(command: str, flag: str) -> bool:
    start = 0
    while True:
        index = command.find(flag, start)
        if index < 0:
            return False
        before_is_boundary = index == 0 or command[index - 1] == " "
        suffix = command[index + len(flag) :]
        after_is_boundary = not suffix or suffix.startswith(" --")
        if before_is_boundary and after_is_boundary:
            return True
        start = index + 1


async def _terminate_regular_chrome_cdp(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(process.wait),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(
            asyncio.to_thread(process.wait),
            timeout=timeout_seconds,
        )


async def _terminate_hidden_chrome_cdp(
    process: _HiddenChromeProcess,
    *,
    timeout_seconds: float,
) -> None:
    launcher_error: BaseException | None = None
    process_error: BaseException | None = None

    try:
        await _terminate_regular_chrome_cdp(
            process.launcher,
            timeout_seconds=timeout_seconds,
        )
    except BaseException as exc:  # noqa: BLE001
        launcher_error = exc

    try:
        await _stabilize_hidden_chrome_cleanup(
            process,
            timeout_seconds=timeout_seconds,
        )
    except BaseException as exc:  # noqa: BLE001
        process_error = exc

    if isinstance(launcher_error, asyncio.CancelledError):
        raise launcher_error
    if isinstance(process_error, asyncio.CancelledError):
        raise process_error
    if isinstance(launcher_error, (KeyboardInterrupt, SystemExit)):
        raise launcher_error
    if isinstance(process_error, (KeyboardInterrupt, SystemExit)):
        raise process_error
    if launcher_error is not None:
        if isinstance(launcher_error, ReaderError):
            raise launcher_error
        raise ReaderError("Dedicated Chrome hidden launcher cleanup failed") from (
            launcher_error
        )
    if process_error is not None:
        if isinstance(process_error, ReaderError):
            raise process_error
        raise ReaderError("Dedicated Chrome hidden process cleanup failed") from (
            process_error
        )


async def _stabilize_hidden_chrome_cleanup(
    process: _HiddenChromeProcess,
    *,
    timeout_seconds: float,
) -> None:
    """Reach a bounded quiet window without ever selecting a replacement."""

    quiet_window = _HIDDEN_CHROME_QUIET_POLLS * _CDP_POLL_INTERVAL_SECONDS
    deadline = monotonic() + timeout_seconds + quiet_window
    quiet_since: float | None = None
    quiet_scan_count = 0
    saw_unpinned_ambiguity = False

    while monotonic() < deadline:
        matches = _matching_hidden_chrome_processes(process)
        now = monotonic()
        if not matches:
            if quiet_since is None:
                quiet_since = now
            quiet_scan_count += 1
            if (
                quiet_scan_count >= _HIDDEN_CHROME_QUIET_POLLS
                and now - quiet_since >= quiet_window
            ):
                return
        else:
            quiet_since = None
            quiet_scan_count = 0
            pinned = process.chrome_identity
            if len(matches) > 1:
                if pinned is None:
                    saw_unpinned_ambiguity = True
            else:
                candidate = matches[0]
                if pinned is None and not saw_unpinned_ambiguity:
                    _pin_hidden_chrome_identity(process, candidate)
                    pinned = process.chrome_identity
                if candidate == pinned:
                    await _signal_and_wait_for_hidden_chrome(
                        process,
                        candidate,
                        timeout_seconds=max(
                            0.001,
                            deadline - monotonic() - quiet_window,
                        ),
                    )

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_CDP_POLL_INTERVAL_SECONDS, remaining))

    raise ReaderError("Dedicated Chrome hidden Chrome cleanup did not become quiet")


async def _signal_and_wait_for_hidden_chrome(
    process: _HiddenChromeProcess,
    identity: _ChromeProcessIdentity,
    *,
    timeout_seconds: float,
) -> None:
    # Re-prove the exact command and pinned start identity before every signal.
    if identity != process.chrome_identity:
        return
    if identity not in _matching_hidden_chrome_processes(process):
        return
    try:
        os.kill(identity.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise ReaderError("Dedicated Chrome could not stop hidden Chrome") from exc
    minimum_phase_budget = 2 * _CDP_POLL_INTERVAL_SECONDS
    total_budget = max(timeout_seconds, 2 * minimum_phase_budget)
    term_budget = max(minimum_phase_budget, total_budget / 2)
    kill_budget = max(minimum_phase_budget, total_budget - term_budget)
    if await _wait_for_hidden_chrome_exit(
        process,
        identity,
        timeout_seconds=term_budget,
    ):
        return

    if identity not in _matching_hidden_chrome_processes(process):
        return
    try:
        os.kill(identity.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise ReaderError("Dedicated Chrome could not kill hidden Chrome") from exc
    if not await _wait_for_hidden_chrome_exit(
        process,
        identity,
        timeout_seconds=kill_budget,
    ):
        raise ReaderError("Dedicated Chrome hidden Chrome did not exit")


async def _wait_for_hidden_chrome_exit(
    process: _HiddenChromeProcess,
    identity: _ChromeProcessIdentity,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if identity not in _matching_hidden_chrome_processes(process):
            return True
        await asyncio.sleep(
            min(
                _CDP_POLL_INTERVAL_SECONDS,
                max(0.0, deadline - monotonic()),
            )
        )
    return identity not in _matching_hidden_chrome_processes(process)


def _require_expected_https_address(actual: str, expected: str) -> None:
    actual_scheme = urlsplit(actual.strip()).scheme.casefold()
    if actual_scheme != "https" or not _same_address(actual, expected):
        raise ReaderError("Dedicated Chrome opened an unexpected article URL")


def _milliseconds(seconds: float) -> float:
    return seconds * 1000
