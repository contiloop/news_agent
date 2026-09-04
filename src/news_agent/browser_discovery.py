"""Collect article candidates from configured section pages in owned Chrome."""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright

from news_agent.candidates import ArticleCandidate
from news_agent.dedicated_browser import (
    _CHROME_ARGUMENTS,
    _complete_cleanup,
    _complete_hidden_chrome_cleanup,
    _complete_regular_chrome_cleanup,
    _create_hidden_cdp_page,
    _HiddenChromeProcess,
    _launch_hidden_chrome_cdp,
    _launch_regular_chrome_cdp,
    _milliseconds,
    _minimize_regular_chrome_page,
    _prepare_owned_profile,
    _reserve_debugging_port,
    _validate_executable,
    _validate_launch_mode,
    _validate_profile_path,
    _validate_timeout,
    _wait_for_hidden_chrome_cdp,
    _wait_for_regular_chrome_cdp,
)
from news_agent.extraction import ReaderError
from news_agent.run_lock import RunLock, RunLockBusyError, RunLockError

__all__ = (
    "BrowserDiscoveryError",
    "collect_browser_section_candidates",
    "normalize_browser_article_url",
)


class BrowserDiscoveryError(RuntimeError):
    """Raised when browser-section discovery cannot return safe candidates."""


class _NonSuccessSectionResponseError(BrowserDiscoveryError):
    def __init__(self, section_url: str, status: int) -> None:
        self.section_url = section_url
        self.status = status
        super().__init__(
            "Dedicated Chrome received a non-success section response: "
            f"status={status} url={section_url}"
        )


_DATE_SLUG_PATTERN = re.compile(r"(?:19|20)\d{2}-\d{2}-\d{2}")
_MARKUP_TITLE_PATTERN = re.compile(
    r"</?[a-z][a-z0-9:-]*(?:\s|/?>)|\b(?:src|width|height|alt)=[\"']",
    re.IGNORECASE,
)
_SECTION_PAGE_CLOSE_TIMEOUT_SECONDS = 1.0
_ANCHOR_SCRIPT = """
elements => elements.map(element => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  const text = (
    element.innerText ||
    element.getAttribute("aria-label") ||
    element.textContent ||
    ""
  ).replace(/\\s+/g, " ").trim();
  const container = element.closest(
    "article, li, [data-testid], [class*='story'], [class*='article'], div"
  );
  const context = container
    ? (container.innerText || "").replace(/\\s+/g, " ").trim()
    : "";
  const time = container
    ? container.querySelector("time")?.getAttribute("datetime")
      || container.querySelector("time")?.textContent
      || null
    : null;
  return {
    href: element.href,
    text,
    context: context.slice(0, 800),
    time,
    hidden: Boolean(
      element.hidden ||
      style.display === "none" ||
      style.visibility === "hidden" ||
      rect.width <= 0 ||
      rect.height <= 0
    ),
  };
})
"""


async def collect_browser_section_candidates(
    section_urls: Sequence[str],
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    allowed_hosts: Sequence[str],
    allowed_path_prefixes: Sequence[str],
    excluded_path_prefixes: Sequence[str] = (),
    total_limit: int = 60,
    launch_mode: Literal["headless", "regular_cdp", "hidden_cdp"] = "headless",
) -> tuple[ArticleCandidate, ...]:
    """Open section pages and return bounded, same-site article candidates."""

    if not section_urls:
        return ()
    executable_path = _wrap_reader_validation(
        lambda: _validate_executable(executable)
    )
    profile_path = _wrap_reader_validation(
        lambda: _validate_profile_path(profile_directory)
    )
    timeout = _wrap_reader_validation(lambda: _validate_timeout(timeout_seconds))
    selected_limit = _validate_total_limit(total_limit)
    constraints = _UrlConstraints.from_raw(
        allowed_hosts=allowed_hosts,
        allowed_path_prefixes=allowed_path_prefixes,
        excluded_path_prefixes=excluded_path_prefixes,
    )
    selected_sections = _section_urls(section_urls)
    selected_launch_mode = _wrap_reader_validation(
        lambda: _validate_launch_mode(launch_mode)
    )

    lock_file = profile_path.with_name(f"{profile_path.name}.lock")
    try:
        with RunLock(lock_file):
            _wrap_reader_validation(lambda: _prepare_owned_profile(profile_path))
            return await _run_owned_browser(
                selected_sections,
                executable=executable_path,
                profile_directory=profile_path,
                timeout_seconds=timeout,
                constraints=constraints,
                total_limit=selected_limit,
                launch_mode=selected_launch_mode,
            )
    except RunLockBusyError as exc:
        raise BrowserDiscoveryError(
            "Dedicated Chrome profile is already in use"
        ) from exc
    except RunLockError as exc:
        raise BrowserDiscoveryError(
            "Dedicated Chrome profile lock could not be used"
        ) from exc


def normalize_browser_article_url(
    url: str,
    *,
    allowed_hosts: Sequence[str],
    allowed_path_prefixes: Sequence[str],
    excluded_path_prefixes: Sequence[str] = (),
) -> str | None:
    """Return a canonical HTTPS article URL if it passes configured filters."""

    constraints = _UrlConstraints.from_raw(
        allowed_hosts=allowed_hosts,
        allowed_path_prefixes=allowed_path_prefixes,
        excluded_path_prefixes=excluded_path_prefixes,
    )
    return _normalize_url(url, constraints=constraints)


async def _run_owned_browser(
    section_urls: Sequence[str],
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    constraints: "_UrlConstraints",
    total_limit: int,
    launch_mode: Literal["headless", "regular_cdp", "hidden_cdp"],
) -> tuple[ArticleCandidate, ...]:
    if launch_mode == "hidden_cdp":
        return await _run_owned_browser_hidden_cdp(
            section_urls,
            executable=executable,
            profile_directory=profile_directory,
            timeout_seconds=timeout_seconds,
            constraints=constraints,
            total_limit=total_limit,
        )
    if launch_mode == "regular_cdp":
        return await _run_owned_browser_regular_cdp(
            section_urls,
            executable=executable,
            profile_directory=profile_directory,
            timeout_seconds=timeout_seconds,
            constraints=constraints,
            total_limit=total_limit,
        )
    return await _run_owned_browser_headless(
        section_urls,
        executable=executable,
        profile_directory=profile_directory,
        timeout_seconds=timeout_seconds,
        constraints=constraints,
        total_limit=total_limit,
    )


async def _run_owned_browser_headless(
    section_urls: Sequence[str],
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    constraints: "_UrlConstraints",
    total_limit: int,
) -> tuple[ArticleCandidate, ...]:
    playwright: Any | None = None
    context: Any | None = None
    candidates: tuple[ArticleCandidate, ...] | None = None
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
        candidates = await _collect_from_sections(
            context,
            section_urls,
            timeout_seconds=timeout_seconds,
            constraints=constraints,
            total_limit=total_limit,
        )
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
        error = BrowserDiscoveryError(
            "Dedicated Chrome discovery cleanup could not be verified"
        )
        if primary_error is None:
            raise error from cleanup_error
        raise error from primary_error
    if primary_error is not None:
        if isinstance(primary_error, BrowserDiscoveryError):
            raise primary_error
        raise BrowserDiscoveryError(
            "Dedicated Chrome could not collect section candidates"
        ) from primary_error
    if candidates is None:
        raise BrowserDiscoveryError("Dedicated Chrome returned no candidates")
    return candidates


async def _run_owned_browser_regular_cdp(
    section_urls: Sequence[str],
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    constraints: "_UrlConstraints",
    total_limit: int,
) -> tuple[ArticleCandidate, ...]:
    playwright: Any | None = None
    browser: Any | None = None
    process: subprocess.Popen[bytes] | None = None
    candidates: tuple[ArticleCandidate, ...] | None = None
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
                f"http://127.0.0.1:{debugging_port}",
                timeout=_milliseconds(timeout_seconds),
            ),
            timeout_seconds,
            "connecting to regular Chrome CDP",
        )
        if not browser.contexts:
            raise BrowserDiscoveryError("Dedicated Chrome returned no browser context")
        candidates = await _collect_from_sections(
            browser.contexts[0],
            section_urls,
            timeout_seconds=timeout_seconds,
            constraints=constraints,
            total_limit=total_limit,
            minimize_windows=True,
        )
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
        error = BrowserDiscoveryError(
            "Dedicated Chrome discovery cleanup could not be verified"
        )
        if primary_error is None:
            raise error from cleanup_error
        raise error from primary_error
    if primary_error is not None:
        if isinstance(primary_error, BrowserDiscoveryError):
            raise primary_error
        raise BrowserDiscoveryError(
            "Dedicated Chrome could not collect section candidates"
        ) from primary_error
    if candidates is None:
        raise BrowserDiscoveryError("Dedicated Chrome returned no candidates")
    return candidates


async def _run_owned_browser_hidden_cdp(
    section_urls: Sequence[str],
    *,
    executable: Path,
    profile_directory: Path,
    timeout_seconds: float,
    constraints: "_UrlConstraints",
    total_limit: int,
) -> tuple[ArticleCandidate, ...]:
    playwright: Any | None = None
    browser: Any | None = None
    process: _HiddenChromeProcess | None = None
    candidates: tuple[ArticleCandidate, ...] | None = None
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
                f"http://127.0.0.1:{debugging_port}",
                timeout=_milliseconds(timeout_seconds),
            ),
            timeout_seconds,
            "connecting to hidden Chrome CDP",
        )
        context, page = await _create_hidden_cdp_page(
            browser,
            timeout_seconds=timeout_seconds,
        )
        candidates = await _collect_from_sections(
            context,
            section_urls,
            timeout_seconds=timeout_seconds,
            constraints=constraints,
            total_limit=total_limit,
            page=page,
        )
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
        error = BrowserDiscoveryError(
            "Dedicated Chrome discovery cleanup could not be verified"
        )
        if primary_error is None:
            raise error from cleanup_error
        raise error from primary_error
    if primary_error is not None:
        if isinstance(primary_error, BrowserDiscoveryError):
            raise primary_error
        raise BrowserDiscoveryError(
            "Dedicated Chrome could not collect section candidates"
        ) from primary_error
    if candidates is None:
        raise BrowserDiscoveryError("Dedicated Chrome returned no candidates")
    return candidates


async def _collect_from_sections(
    context: Any,
    section_urls: Sequence[str],
    *,
    timeout_seconds: float,
    constraints: "_UrlConstraints",
    total_limit: int,
    minimize_windows: bool = False,
    page: Any | None = None,
) -> tuple[ArticleCandidate, ...]:
    deadline = monotonic() + timeout_seconds
    by_url: dict[str, ArticleCandidate] = {}

    try:
        for section_url in section_urls:
            if len(by_url) >= total_limit:
                break
            remaining = max(0.001, deadline - monotonic())
            close_after_section = page is None
            section_page = page
            if section_page is None:
                section_page = await _bounded(
                    context.new_page(),
                    remaining,
                    "creating a section page",
                )
            if minimize_windows:
                await _minimize_regular_chrome_page(section_page)
            try:
                response = await _bounded(
                    section_page.goto(
                        section_url,
                        wait_until="domcontentloaded",
                        timeout=_milliseconds(remaining),
                    ),
                    remaining,
                    "loading a section page",
                )
                if response is None or not isinstance(response.status, int):
                    raise BrowserDiscoveryError(
                        "Dedicated Chrome received no section response"
                    )
                if not 200 <= response.status < 300:
                    raise _NonSuccessSectionResponseError(
                        section_url,
                        response.status,
                    )
                section_title = _clean_text(
                    await _bounded(
                        section_page.title(),
                        max(0.001, deadline - monotonic()),
                        "reading a section title",
                    )
                ) or section_url
                anchors = await _bounded(
                    section_page.locator("a[href]").evaluate_all(_ANCHOR_SCRIPT),
                    max(0.001, deadline - monotonic()),
                    "collecting section links",
                )
                if not isinstance(anchors, list):
                    raise BrowserDiscoveryError(
                        "Dedicated Chrome returned invalid section links"
                    )
                for anchor in anchors:
                    if len(by_url) >= total_limit:
                        break
                    candidate = _candidate_from_anchor(
                        anchor,
                        section_url=section_url,
                        section_title=section_title,
                        constraints=constraints,
                    )
                    if candidate is None or str(candidate.url) in by_url:
                        continue
                    by_url[str(candidate.url)] = candidate
            finally:
                if close_after_section:
                    try:
                        await _bounded(
                            section_page.close(),
                            _SECTION_PAGE_CLOSE_TIMEOUT_SECONDS,
                            "closing a section page",
                        )
                    except Exception:
                        # Browser cleanup closes any slow section tabs.
                        pass
    finally:
        if page is not None:
            try:
                await _bounded(
                    page.close(),
                    _SECTION_PAGE_CLOSE_TIMEOUT_SECONDS,
                    "closing the background section page",
                )
            except Exception:
                # Browser cleanup closes a slow shared background target.
                pass

    return tuple(by_url.values())


def _candidate_from_anchor(
    anchor: object,
    *,
    section_url: str,
    section_title: str,
    constraints: "_UrlConstraints",
) -> ArticleCandidate | None:
    if not isinstance(anchor, Mapping) or anchor.get("hidden") is True:
        return None
    raw_href = anchor.get("href")
    url = _normalize_url(raw_href, constraints=constraints)
    if url is None:
        return None

    raw_text = _clean_text(anchor.get("text"))
    context = _clean_text(anchor.get("context"))
    title = _candidate_title(raw_text, context)
    if title is None:
        return None
    visible_text = context or title
    published_at = _optional_timestamp(anchor.get("time"))

    try:
        return ArticleCandidate(
            url=url,
            title=title,
            section_url=section_url,
            section_title=section_title,
            visible_text=visible_text,
            published_at=published_at,
        )
    except ValueError:
        return None


def _normalize_url(url: object, *, constraints: "_UrlConstraints") -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() != "https":
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    if host not in constraints.allowed_hosts:
        return None
    path = parsed.path or "/"
    if not any(path.startswith(prefix) for prefix in constraints.allowed_path_prefixes):
        return None
    if any(path.startswith(prefix) for prefix in constraints.excluded_path_prefixes):
        return None
    if not _looks_like_article_path(path):
        return None
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit(("https", netloc, path, "", ""))


def _looks_like_article_path(path: str) -> bool:
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return False
    leaf = segments[-1]
    if _DATE_SLUG_PATTERN.search(leaf):
        return True
    return len(segments) >= 3 and "-" in leaf and len(leaf) >= 8


def _candidate_title(text: str, context: str) -> str | None:
    for value in (text, context):
        title = _clean_text(value)
        if not title or _looks_like_markup_title(title):
            continue
        if len(title) > 220:
            title = title[:220].rsplit(" ", maxsplit=1)[0].strip()
        if _looks_like_markup_title(title):
            continue
        if len(title) >= 8:
            return title
    return None


def _looks_like_markup_title(value: str) -> bool:
    return bool(_MARKUP_TITLE_PATTERN.search(value))


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _optional_timestamp(value: object) -> datetime | None:
    raw = _clean_text(value)
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _section_urls(urls: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in urls:
        if not isinstance(value, str) or not value.strip():
            raise BrowserDiscoveryError("Section URL cannot be empty")
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() != "https":
            raise BrowserDiscoveryError("Section URLs must use HTTPS")
        normalized = urlunsplit(
            (
                "https",
                parsed.netloc.casefold(),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
    if not selected:
        raise BrowserDiscoveryError("At least one section URL is required")
    return tuple(selected)


def _validate_total_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BrowserDiscoveryError("Candidate collection limit must be positive")
    return value


async def _bounded(awaitable: Any, timeout_seconds: float, action: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise BrowserDiscoveryError(
            f"Dedicated Chrome timed out while {action}"
        ) from exc


def _wrap_reader_validation(callback: Any) -> Any:
    try:
        return callback()
    except ReaderError as exc:
        raise BrowserDiscoveryError(str(exc)) from exc


class _UrlConstraints:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        allowed_path_prefixes: tuple[str, ...],
        excluded_path_prefixes: tuple[str, ...],
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.allowed_path_prefixes = allowed_path_prefixes
        self.excluded_path_prefixes = excluded_path_prefixes

    @classmethod
    def from_raw(
        cls,
        *,
        allowed_hosts: Sequence[str],
        allowed_path_prefixes: Sequence[str],
        excluded_path_prefixes: Sequence[str],
    ) -> "_UrlConstraints":
        return cls(
            allowed_hosts=_hosts(allowed_hosts),
            allowed_path_prefixes=_path_prefixes(
                allowed_path_prefixes,
                required=True,
            ),
            excluded_path_prefixes=_path_prefixes(
                excluded_path_prefixes,
                required=False,
            ),
        )


def _hosts(values: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise BrowserDiscoveryError("Allowed article hosts cannot be empty")
        host = value.strip().rstrip(".").casefold()
        if "/" in host or "\\" in host:
            raise BrowserDiscoveryError("Allowed article hosts must be host names")
        if host not in seen:
            seen.add(host)
            selected.append(host)
    if not selected:
        raise BrowserDiscoveryError("At least one allowed article host is required")
    return tuple(selected)


def _path_prefixes(values: Sequence[str], *, required: bool) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise BrowserDiscoveryError("Article path prefixes cannot be empty")
        prefix = value.strip()
        if not prefix.startswith("/") or "\\" in prefix:
            raise BrowserDiscoveryError("Article path prefixes must start with /")
        if prefix not in seen:
            seen.add(prefix)
            selected.append(prefix)
    if required and not selected:
        raise BrowserDiscoveryError(
            "At least one allowed article path prefix is required"
        )
    return tuple(selected)
