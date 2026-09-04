"""Dispatch article reading to CUA or an isolated dedicated Chrome backend."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from cua_driver import CuaDriver

from news_agent.browser_tabs import (
    POLL_INTERVAL_SECONDS as _POLL_INTERVAL_SECONDS,
)
from news_agent.browser_tabs import (
    _bounded,
    _call_tool,
    _close_opened_work_tab_if_proven,
    _close_work_tab,
    _has_expected_page,
    _has_visible_dialog,
    _native_tab_state,
    _navigate,
    _open_work_tab,
    _query_dom_paragraphs,
    _require_selected_work_tab,
    _select_browser_pid,
    _select_browser_window,
    _window_state,
    _WorkTabContext,
)
from news_agent.extraction import (
    AxExtractionError,
    ReaderError,
    extract_article_body,
    extract_ax_article_body,
)
from news_agent.rss import DiscoveredItem

_LOGGER = logging.getLogger(__name__)


async def read_article_body(
    item: DiscoveredItem,
    *,
    browser_bundle_id: str | None = None,
    timeout_seconds: float,
    article_body_selector: str = "p",
    article_access_denied_selector: str | None = None,
    article_access_denied_phrases: tuple[str, ...] = (),
    browser_mode: Literal["cua", "dedicated_chrome"] = "cua",
    browser_launch_mode: Literal[
        "headless",
        "hidden_cdp",
        "regular_cdp",
    ] = "headless",
    browser_executable: Path | None = None,
    browser_profile_directory: Path | None = None,
) -> str:
    """Read one selected article through the configured browser backend."""

    if browser_mode == "dedicated_chrome":
        if browser_bundle_id is not None:
            raise ReaderError(
                "Dedicated Chrome mode cannot use a CUA browser bundle ID"
            )
        if browser_executable is None or browser_profile_directory is None:
            raise ReaderError(
                "Dedicated Chrome mode requires an executable and profile directory"
            )
        from news_agent.dedicated_browser import (
            read_article_body_in_dedicated_chrome,
        )

        return await read_article_body_in_dedicated_chrome(
            item,
            executable=browser_executable,
            profile_directory=browser_profile_directory,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            article_access_denied_selector=article_access_denied_selector,
            article_access_denied_phrases=article_access_denied_phrases,
            launch_mode=browser_launch_mode,
        )

    if browser_mode != "cua":
        raise ReaderError(f"Unsupported browser mode: {browser_mode}")
    if browser_bundle_id is None or not browser_bundle_id.strip():
        raise ReaderError("CUA browser mode requires a browser bundle ID")
    if browser_executable is not None or browser_profile_directory is not None:
        raise ReaderError("CUA browser mode cannot use dedicated Chrome settings")

    try:
        driver = CuaDriver.connect(None)
        if inspect.isawaitable(driver):
            driver = await driver
    except Exception as exc:
        raise ReaderError("Cannot connect to the local Cua Driver daemon") from exc

    session = f"news-agent-read-one-{uuid4().hex[:12]}"
    try:
        await _bounded(
            _call_tool(
                driver,
                "start_session",
                {"session": session, "capture_scope": "window"},
            ),
            timeout_seconds,
            "starting the CUA session",
        )
    except BaseException:
        await _best_effort_end_session(
            driver,
            session,
            timeout_seconds=timeout_seconds,
        )
        raise

    try:
        body = await _read_in_dedicated_tab(
            driver,
            item,
            browser_bundle_id=browser_bundle_id,
            timeout_seconds=timeout_seconds,
            article_body_selector=article_body_selector,
            session=session,
        )
    except BaseException:
        await _best_effort_end_session(
            driver,
            session,
            timeout_seconds=timeout_seconds,
        )
        raise

    await _bounded(
        _call_tool(driver, "end_session", {"session": session}),
        timeout_seconds,
        "ending the CUA session",
    )
    return body


async def _read_from_browser(
    driver: Any,
    item: DiscoveredItem,
    *,
    browser_bundle_id: str,
    timeout_seconds: float,
    article_body_selector: str = "p",
    session: str,
) -> str:
    """Read the selected tab directly as an internal diagnostic seam."""

    pid = await _select_browser_pid(driver, browser_bundle_id)
    window_id = await _select_browser_window(driver, pid)
    return await _read_selected_browser_tab(
        driver,
        item,
        pid=pid,
        window_id=window_id,
        timeout_seconds=timeout_seconds,
        article_body_selector=article_body_selector,
        session=session,
    )


async def _read_in_dedicated_tab(
    driver: Any,
    item: DiscoveredItem,
    *,
    browser_bundle_id: str,
    timeout_seconds: float,
    article_body_selector: str = "p",
    session: str,
) -> str:
    pid = await _bounded(
        _select_browser_pid(driver, browser_bundle_id),
        timeout_seconds,
        "selecting the browser process",
    )
    window_id = await _bounded(
        _select_browser_window(driver, pid),
        timeout_seconds,
        "selecting the browser window",
    )
    original_snapshot = await _bounded(
        _window_state(driver, pid, window_id, session=session),
        timeout_seconds,
        "reading the original browser tab state",
    )
    marker_fragment = f"news-agent-work={uuid4().hex}"
    try:
        work_tab = await _bounded(
            _open_work_tab(
                driver,
                original_snapshot,
                browser_bundle_id=browser_bundle_id,
                url=str(item.url),
                pid=pid,
                window_id=window_id,
                session=session,
                timeout_seconds=timeout_seconds,
                marker_fragment=marker_fragment,
            ),
            timeout_seconds,
            "opening the dedicated browser tab",
        )
    except BaseException as open_error:
        try:
            await _bounded(
                _close_opened_work_tab_if_proven(
                    driver,
                    original_snapshot,
                    url=str(item.url),
                    pid=pid,
                    window_id=window_id,
                    session=session,
                    timeout_seconds=timeout_seconds,
                    marker_fragment=marker_fragment,
                ),
                timeout_seconds,
                "cleaning up a partially opened dedicated browser tab",
            )
        except BaseException as cleanup_error:
            _log_tab_cleanup_failure(item.guid, cleanup_error)
            if isinstance(open_error, ReaderError) and isinstance(
                cleanup_error, ReaderError
            ):
                raise ReaderError(
                    f"{_one_line_error(open_error)}; partial dedicated tab cleanup "
                    f"failed ({_one_line_error(cleanup_error)})"
                ) from cleanup_error
            if isinstance(cleanup_error, asyncio.CancelledError):
                raise
        raise

    try:
        body = await _bounded(
            _read_selected_browser_tab(
                driver,
                item,
                pid=pid,
                window_id=window_id,
                timeout_seconds=timeout_seconds,
                article_body_selector=article_body_selector,
                session=session,
                work_tab=work_tab,
            ),
            timeout_seconds,
            "reading the article",
        )
    except BaseException as read_error:
        try:
            await _bounded(
                _close_work_tab(
                    driver,
                    work_tab,
                    pid=pid,
                    window_id=window_id,
                    session=session,
                    timeout_seconds=timeout_seconds,
                ),
                timeout_seconds,
                "closing the dedicated browser tab",
            )
        except BaseException as cleanup_error:
            _log_tab_cleanup_failure(item.guid, cleanup_error)
            if isinstance(read_error, ReaderError) and isinstance(
                cleanup_error, ReaderError
            ):
                raise ReaderError(
                    f"{_one_line_error(read_error)}; dedicated tab cleanup "
                    f"failed ({_one_line_error(cleanup_error)})"
                ) from cleanup_error
            if isinstance(cleanup_error, asyncio.CancelledError):
                raise
        raise

    try:
        await _bounded(
            _close_work_tab(
                driver,
                work_tab,
                pid=pid,
                window_id=window_id,
                session=session,
                timeout_seconds=timeout_seconds,
            ),
            timeout_seconds,
            "closing the dedicated browser tab",
        )
    except BaseException as cleanup_error:
        _log_tab_cleanup_failure(item.guid, cleanup_error)
        raise
    return body


async def _read_selected_browser_tab(
    driver: Any,
    item: DiscoveredItem,
    *,
    pid: int,
    window_id: int,
    timeout_seconds: float,
    article_body_selector: str = "p",
    session: str,
    work_tab: _WorkTabContext | None = None,
) -> str:
    deadline = monotonic() + timeout_seconds

    snapshot = await _window_state(driver, pid, window_id, session=session)
    if work_tab is not None:
        _require_selected_work_tab(
            _native_tab_state(snapshot),
            work_tab,
            snapshot,
        )
    elif not _has_expected_page(snapshot, item.title, str(item.url)):
        await _navigate(
            driver,
            snapshot,
            pid=pid,
            window_id=window_id,
            session=session,
            url=str(item.url),
        )

    while monotonic() < deadline:
        snapshot = await _window_state(driver, pid, window_id, session=session)
        if work_tab is not None:
            _require_selected_work_tab(
                _native_tab_state(snapshot),
                work_tab,
                snapshot,
            )
        if _has_visible_dialog(snapshot):
            raise ReaderError(
                "The browser window contains a dialog; resolve it and retry read-one"
            )
        if _has_expected_page(snapshot, item.title, str(item.url)):
            try:
                return extract_ax_article_body(snapshot, item.title)
            except AxExtractionError as ax_error:
                ax_reason = _one_line_error(ax_error)
                _LOGGER.warning(
                    "event=extraction_fallback guid=%s from=ax to=dom reason=%s",
                    _log_value(item.guid),
                    _log_value(ax_reason),
                )

            try:
                paragraphs = await _query_dom_paragraphs(
                    driver,
                    pid=pid,
                    window_id=window_id,
                    selector=article_body_selector,
                )
                body = extract_article_body(paragraphs)
            except ReaderError as dom_error:
                dom_reason = _one_line_error(dom_error)
                _LOGGER.error(
                    "event=extraction_failed guid=%s ax_reason=%s dom_reason=%s",
                    _log_value(item.guid),
                    _log_value(ax_reason),
                    _log_value(dom_reason),
                )
                raise ReaderError(
                    f"AX extraction failed ({ax_reason}); "
                    f"DOM fallback failed ({dom_reason})"
                ) from dom_error

            _LOGGER.warning(
                "event=extraction_fallback_succeeded guid=%s "
                "method=dom paragraphs=%d body_characters=%d",
                _log_value(item.guid),
                body.count("\n\n") + 1,
                len(body),
            )
            return body

        await asyncio.sleep(
            min(_POLL_INTERVAL_SECONDS, max(0.0, deadline - monotonic()))
        )

    raise ReaderError(
        f"Expected article title did not appear within {timeout_seconds:g} seconds"
    )


async def _best_effort_end_session(
    driver: Any,
    session: str,
    *,
    timeout_seconds: float,
) -> None:
    try:
        await _bounded(
            _call_tool(driver, "end_session", {"session": session}),
            timeout_seconds,
            "ending the CUA session",
        )
    except ReaderError:
        pass


def _one_line_error(error: BaseException) -> str:
    return " ".join(str(error).split()) or type(error).__name__


def _log_tab_cleanup_failure(guid: str, error: BaseException) -> None:
    _LOGGER.error(
        "event=work_tab_cleanup_failed guid=%s reason=%s",
        _log_value(guid),
        _log_value(_one_line_error(error)),
    )


def _log_value(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True)
