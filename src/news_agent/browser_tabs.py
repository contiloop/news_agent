"""CUA browser, window, DOM, and dedicated-tab operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from news_agent.extraction import ReaderError, _element_text, _same_title


DOM_PARAGRAPH_ATTRIBUTES = [
    "tagName",
    "innerText",
    "data-testid",
    "hidden",
    "offsetLeft",
    "offsetTop",
    "offsetWidth",
    "offsetHeight",
]
POLL_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class _NativeTabState:
    tabs: tuple[Mapping[str, Any], ...]
    labels: tuple[str, ...]
    selected_index: int


@dataclass(frozen=True)
class _WorkTabContext:
    original_labels: tuple[str, ...]
    original_selected_index: int
    work_index: int
    marker_fragment: str


async def _open_work_tab(
    driver: Any,
    original_snapshot: Mapping[str, Any],
    *,
    browser_bundle_id: str,
    url: str,
    pid: int,
    window_id: int,
    session: str,
    timeout_seconds: float,
    marker_fragment: str,
) -> _WorkTabContext:
    if not marker_fragment or any(
        character in marker_fragment for character in "#&"
    ):
        raise ReaderError("Dedicated browser tab marker is invalid")
    original = _native_tab_state(original_snapshot)
    marked_url = _with_work_tab_marker(url, marker_fragment)
    launch_result = await _call_tool(
        driver,
        "launch_app",
        {
            "bundle_id": browser_bundle_id,
            "urls": [marked_url],
        },
    )
    launch_state = launch_result.get("launch_state")
    if (
        launch_result.get("pid") != pid
        or not isinstance(launch_state, Mapping)
        or launch_state.get("requested") is not True
        or launch_state.get("process_running") is not True
        or launch_state.get("window_ready") is not True
    ):
        raise ReaderError(
            "Cua Driver did not open the article URL in the selected browser process"
        )

    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        snapshot = await _window_state(
            driver,
            pid,
            window_id,
            session=session,
        )
        current = _native_tab_state(snapshot)
        if len(current.tabs) == len(original.tabs) + 1:
            selected_context = _selected_work_tab_context(
                original,
                current,
                snapshot,
                url=url,
                marker_fragment=marker_fragment,
            )
            if selected_context is not None:
                return selected_context

            insertion_indexes = [
                index
                for index in range(len(current.tabs))
                if (
                    current.labels[:index] + current.labels[index + 1 :]
                    == original.labels
                )
            ]
            if len(insertion_indexes) == 1:
                work_index = insertion_indexes[0]
                if current.selected_index != work_index:
                    await _call_tool(
                        driver,
                        "click",
                        {
                            "pid": pid,
                            "window_id": window_id,
                            "element_token": _text_field(
                                current.tabs[work_index],
                                "element_token",
                            ),
                            "session": session,
                            "action": "press",
                        },
                    )
                    continue

                address = _text_field(_unique_address_field(snapshot), "value")
                if not _same_address(address, url) or not _has_work_tab_marker(
                    address,
                    marker_fragment,
                ):
                    raise ReaderError(
                        "Opened browser tab does not contain its dedicated URL "
                        "marker; no tab was closed"
                    )
                return _WorkTabContext(
                    original_labels=original.labels,
                    original_selected_index=original.selected_index,
                    work_index=work_index,
                    marker_fragment=marker_fragment,
                )
        elif len(current.tabs) != len(original.tabs):
            raise ReaderError(
                "Browser tab set changed ambiguously while opening the work tab; "
                "no tab was closed"
            )

        await asyncio.sleep(
            min(POLL_INTERVAL_SECONDS, max(0.0, deadline - monotonic()))
        )

    raise ReaderError(
        "Dedicated browser tab could not be proven after opening; no tab was closed"
    )


async def _close_opened_work_tab_if_proven(
    driver: Any,
    original_snapshot: Mapping[str, Any],
    *,
    url: str,
    pid: int,
    window_id: int,
    session: str,
    timeout_seconds: float,
    marker_fragment: str,
) -> bool:
    """Close a partially opened tab only when its selected marker proves ownership."""

    original = _native_tab_state(original_snapshot)
    snapshot = await _window_state(
        driver,
        pid,
        window_id,
        session=session,
    )
    current = _native_tab_state(snapshot)
    context = _selected_work_tab_context(
        original,
        current,
        snapshot,
        url=url,
        marker_fragment=marker_fragment,
    )
    if context is None:
        return False

    await _close_work_tab(
        driver,
        context,
        pid=pid,
        window_id=window_id,
        session=session,
        timeout_seconds=timeout_seconds,
    )
    return True


async def _close_work_tab(
    driver: Any,
    context: _WorkTabContext,
    *,
    pid: int,
    window_id: int,
    session: str,
    timeout_seconds: float,
) -> None:
    snapshot = await _window_state(
        driver,
        pid,
        window_id,
        session=session,
    )
    current = _native_tab_state(snapshot)
    _require_selected_work_tab(current, context, snapshot)

    work_tab = current.tabs[context.work_index]
    work_index = _integer_field(work_tab, "element_index")
    close_controls = [
        element
        for element in _snapshot_elements(snapshot)
        if element.get("parent_index") == work_index
        and element.get("role") == "AXButton"
        and isinstance(element.get("element_token"), str)
        and _has_positive_bounds(element.get("frame"))
    ]
    if len(close_controls) != 1:
        raise ReaderError(
            "Expected exactly one close control on the selected work tab"
        )

    await _call_tool(
        driver,
        "click",
        {
            "pid": pid,
            "window_id": window_id,
            "element_token": _text_field(close_controls[0], "element_token"),
            "session": session,
            "action": "press",
        },
    )

    deadline = monotonic() + timeout_seconds
    restore_sent = False
    while monotonic() < deadline:
        snapshot = await _window_state(
            driver,
            pid,
            window_id,
            session=session,
        )
        current = _native_tab_state(snapshot)
        if current.labels == context.original_labels:
            if current.selected_index == context.original_selected_index:
                return
            if not restore_sent:
                original_tab = current.tabs[context.original_selected_index]
                await _call_tool(
                    driver,
                    "click",
                    {
                        "pid": pid,
                        "window_id": window_id,
                        "element_token": _text_field(
                            original_tab,
                            "element_token",
                        ),
                        "session": session,
                        "action": "press",
                    },
                )
                restore_sent = True

        await asyncio.sleep(
            min(POLL_INTERVAL_SECONDS, max(0.0, deadline - monotonic()))
        )

    raise ReaderError(
        "Dedicated browser tab did not close with the original tab set restored"
    )


def _selected_work_tab_context(
    original: _NativeTabState,
    current: _NativeTabState,
    snapshot: Mapping[str, Any],
    *,
    url: str,
    marker_fragment: str,
) -> _WorkTabContext | None:
    if len(current.tabs) != len(original.tabs) + 1:
        return None

    selected_index = current.selected_index
    if (
        current.labels[:selected_index]
        + current.labels[selected_index + 1 :]
        != original.labels
    ):
        return None

    address = _text_field(_unique_address_field(snapshot), "value")
    if not _same_address(address, url) or not _has_work_tab_marker(
        address,
        marker_fragment,
    ):
        return None

    return _WorkTabContext(
        original_labels=original.labels,
        original_selected_index=original.selected_index,
        work_index=selected_index,
        marker_fragment=marker_fragment,
    )


def _native_tab_state(snapshot: Mapping[str, Any]) -> _NativeTabState:
    elements = _snapshot_elements(snapshot)
    roots = [
        element
        for element in elements
        if element.get("role") == "AXWindow"
        and element.get("depth") == 0
        and element.get("parent_index") is None
    ]
    if len(roots) != 1:
        raise ReaderError("Browser accessibility snapshot has no unique root window")
    root_index = _integer_field(roots[0], "element_index")

    tabs = sorted(
        (
            element
            for element in elements
            if element.get("role") == "AXRadioButton"
            and element.get("parent_index") == root_index
            and element.get("in_web_content") is not True
        ),
        key=lambda element: _integer_field(element, "element_index"),
    )
    if not tabs:
        raise ReaderError("Browser accessibility snapshot contains no native tabs")

    labels: list[str] = []
    for tab in tabs:
        label = tab.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ReaderError("Browser native tab is missing a text label")
        labels.append(" ".join(label.split()))

    selected = [
        index for index, tab in enumerate(tabs) if tab.get("selected") is True
    ]
    if len(selected) != 1:
        raise ReaderError("Expected exactly one selected native browser tab")
    return _NativeTabState(
        tabs=tuple(tabs),
        labels=tuple(labels),
        selected_index=selected[0],
    )


def _require_selected_work_tab(
    current: _NativeTabState,
    context: _WorkTabContext,
    snapshot: Mapping[str, Any],
) -> None:
    if (
        len(current.tabs) != len(context.original_labels) + 1
        or current.selected_index != context.work_index
        or (
            current.labels[: context.work_index]
            + current.labels[context.work_index + 1 :]
        )
        != context.original_labels
    ):
        raise ReaderError(
            "Selected browser tab cannot be proven to be the dedicated work tab; "
            "no tab was closed"
        )
    address = _unique_address_field(snapshot)
    if not _has_work_tab_marker(
        _text_field(address, "value"),
        context.marker_fragment,
    ):
        raise ReaderError(
            "Selected browser tab does not contain its dedicated ownership marker; "
            "no tab was closed"
        )


def _snapshot_elements(
    snapshot: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    elements = snapshot.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(element, Mapping) for element in elements
    ):
        raise ReaderError("Browser accessibility snapshot is invalid")
    return elements


async def _select_browser_pid(driver: Any, bundle_id: str) -> int:
    payload = await _call_tool(driver, "list_apps", {})
    apps = payload.get("apps")
    if not isinstance(apps, list):
        raise ReaderError("Cua Driver returned an invalid application list")

    matches = [
        app
        for app in apps
        if isinstance(app, Mapping)
        and app.get("bundle_id") == bundle_id
        and app.get("running") is True
        and _optional_integer_field(app, "pid") > 0
    ]
    if len(matches) != 1:
        raise ReaderError(
            f"Expected exactly one running browser for bundle id {bundle_id!r}"
        )
    return _integer_field(matches[0], "pid")


async def _select_browser_window(driver: Any, pid: int) -> int:
    payload = await _call_tool(
        driver,
        "list_windows",
        {"pid": pid, "on_screen_only": True},
    )
    windows = payload.get("windows")
    if not isinstance(windows, list):
        raise ReaderError("Cua Driver returned an invalid window list")

    matches = [
        window
        for window in windows
        if isinstance(window, Mapping)
        and window.get("is_on_screen") is True
        and window.get("on_current_space") is True
        and _has_positive_bounds(window.get("bounds"))
    ]
    if len(matches) != 1:
        raise ReaderError(
            "Expected exactly one visible browser window on the current desktop"
        )
    return _integer_field(matches[0], "window_id")


async def _navigate(
    driver: Any,
    snapshot: Mapping[str, Any],
    *,
    pid: int,
    window_id: int,
    session: str,
    url: str,
) -> None:
    address = _unique_address_field(snapshot)
    await _call_tool(
        driver,
        "set_value",
        {
            "pid": pid,
            "element_token": _text_field(address, "element_token"),
            "session": session,
            "value": url,
        },
    )

    refreshed = await _window_state(driver, pid, window_id, session=session)
    address = _unique_address_field(refreshed)
    current_value = _text_field(address, "value")
    if not _same_address(current_value, url):
        raise ReaderError("Browser address bar did not accept the article URL")

    await _call_tool(
        driver,
        "press_key",
        {
            "pid": pid,
            "window_id": window_id,
            "element_token": _text_field(address, "element_token"),
            "session": session,
            "key": "return",
            "delivery_mode": "foreground",
        },
    )


async def _window_state(
    driver: Any,
    pid: int,
    window_id: int,
    *,
    session: str,
) -> Mapping[str, Any]:
    return await _call_tool(
        driver,
        "get_window_state",
        {
            "pid": pid,
            "window_id": window_id,
            "session": session,
            "include_screenshot": False,
        },
    )


async def _query_dom_paragraphs(
    driver: Any,
    *,
    pid: int,
    window_id: int,
    selector: str = "p",
) -> list[Mapping[str, Any]]:
    payload = await _call_tool(
        driver,
        "page",
        {
            "action": "query_dom",
            "pid": pid,
            "window_id": window_id,
            "css_selector": selector,
            "attributes": DOM_PARAGRAPH_ATTRIBUTES,
        },
    )
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 1:
        raise ReaderError("Cua Driver returned invalid query_dom content")

    block = content[0]
    if (
        not isinstance(block, Mapping)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
    ):
        raise ReaderError("Cua Driver returned invalid query_dom text")

    try:
        paragraphs = json.loads(block["text"])
    except json.JSONDecodeError as exc:
        raise ReaderError(
            "query_dom did not return a DOM paragraph array; "
            "the browser DOM bridge may be unavailable"
        ) from exc

    if not isinstance(paragraphs, list) or not all(
        isinstance(paragraph, Mapping) for paragraph in paragraphs
    ):
        raise ReaderError("query_dom returned an invalid DOM paragraph array")
    return paragraphs


async def _call_tool(
    driver: Any,
    name: str,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        result = await driver.call_tool(name, json.dumps(arguments))
    except Exception as exc:
        raise ReaderError(f"Cua Driver tool failed: {name}") from exc

    raw = getattr(result, "structured_json", None) or getattr(
        result, "raw_json", None
    )
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ReaderError(f"Cua Driver returned invalid JSON for {name}") from exc

    if getattr(result, "is_error", False):
        detail = payload.get("error") if isinstance(payload, Mapping) else None
        suffix = f": {detail}" if isinstance(detail, str) and detail else ""
        raise ReaderError(f"Cua Driver tool failed: {name}{suffix}")
    if not isinstance(payload, Mapping):
        raise ReaderError(f"Cua Driver returned an invalid result for {name}")
    return payload


async def _bounded(awaitable: Any, timeout_seconds: float, action: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise ReaderError(
            f"CUA timed out after {timeout_seconds:g} seconds while {action}"
        ) from exc


def _has_exact_heading(snapshot: Mapping[str, Any], title: str) -> bool:
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return False
    return sum(
        isinstance(element, Mapping)
        and element.get("role") == "AXHeading"
        and str(element.get("value", "")) == "1"
        and _same_title(_element_text(element), title)
        for element in elements
    ) == 1


def _has_expected_page(
    snapshot: Mapping[str, Any],
    title: str,
    url: str,
) -> bool:
    if not _has_exact_heading(snapshot, title):
        return False
    try:
        address = _unique_address_field(snapshot)
        return _same_address(_text_field(address, "value"), url)
    except ReaderError:
        return False


def _has_visible_dialog(snapshot: Mapping[str, Any]) -> bool:
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return False
    return any(
        isinstance(element, Mapping)
        and element.get("role") in {"AXDialog", "AXSheet"}
        and _has_positive_bounds(element.get("frame"))
        for element in elements
    )


def _unique_address_field(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        raise ReaderError("Browser accessibility snapshot is invalid")
    matches = [
        element
        for element in elements
        if isinstance(element, Mapping)
        and element.get("role") == "AXTextField"
        and element.get("in_web_content") is not True
        and isinstance(element.get("element_token"), str)
    ]

    explicit_native = [
        element
        for element in matches
        if element.get("in_web_content") is False
    ]
    if explicit_native:
        matches = explicit_native

    if len(matches) > 1:
        depths = [
            element.get("depth")
            for element in matches
            if isinstance(element.get("depth"), int)
            and not isinstance(element.get("depth"), bool)
        ]
        if depths:
            minimum_depth = min(depths)
            matches = [
                element
                for element in matches
                if element.get("depth") == minimum_depth
            ]

    if len(matches) != 1:
        raise ReaderError("Expected exactly one native browser address field")
    return matches[0]


def _same_address(actual: str, expected: str) -> bool:
    return _normalized_address(actual) == _normalized_address(expected)


def _with_work_tab_marker(url: str, marker_fragment: str) -> str:
    parsed = urlsplit(url)
    fragment = (
        f"{parsed.fragment}&{marker_fragment}"
        if parsed.fragment
        else marker_fragment
    )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment)
    )


def _has_work_tab_marker(address: str, marker_fragment: str) -> bool:
    candidate = address.strip()
    parsed = urlsplit(
        candidate if "://" in candidate else f"https://{candidate}"
    )
    return parsed.fragment.rsplit("&", maxsplit=1)[-1] == marker_fragment


def _normalized_address(value: str) -> tuple[str, str, str]:
    candidate = value.strip()
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return host, path, parsed.query


def _has_positive_bounds(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("width", value.get("w")), (int, float))
        and isinstance(value.get("height", value.get("h")), (int, float))
        and value.get("width", value.get("w")) > 0
        and value.get("height", value.get("h")) > 0
    )


def _integer_field(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ReaderError(f"Cua Driver result is missing integer field {key!r}")
    return result


def _optional_integer_field(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    return result if isinstance(result, int) and not isinstance(result, bool) else -1


def _text_field(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ReaderError(f"Cua Driver result is missing text field {key!r}")
    return result
