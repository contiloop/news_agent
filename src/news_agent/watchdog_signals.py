"""Bounded, read-only inputs for the news-agent watchdog.

Snapshots deliberately omit launchctl output and exception messages.  In
particular, log errors may contain request URLs or credentials; only a validated
error identifier and an HTTP status number may cross this boundary.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LaunchdSnapshot:
    loaded: bool | None
    running: bool = False
    runs: int | None = None
    last_exit_code: int | None = None
    pid: int | None = None
    error: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    timestamp: datetime
    event: str
    status: str | None = None
    error_type: str | None = None
    http_status: int | None = None
    stage: str | None = None
    failed_article_count: int = 0


@dataclass(frozen=True)
class RunLogSnapshot:
    records: tuple[RunRecord, ...] = ()
    error: str | None = None
    truncated: bool = False


_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_RUN_EVENTS = frozenset({"run_started", "run_finished", "run_failed", "run_skipped"})
_RUN_STATUSES = frozenset(
    {"completed", "no_work", "completed_with_errors", "already_running", "failed"}
)
_TOP_LEVEL_FIELD = re.compile(
    r"^(?:\t| {4})(state|runs|last exit code|pid) = (.*?)\s*$"
)
_INTEGER = re.compile(r"-?\d{1,10}\Z")
_DISABLED_ENTRY = re.compile(
    r'^\s*"([^"\r\n]+)"\s*=>\s*(true|false|enabled|disabled)\s*$'
)
_MAX_COMMAND_OUTPUT = 1_048_576
_HTTP_PATTERNS = (
    re.compile(
        r"\bHTTP(?:/\d(?:\.\d)?)?(?:\s+(?:error|status)(?:\s+code)?)?"
        r"\s*[:=]?\s*([1-5]\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:client|server) error\s+['\"]([1-5]\d{2})(?=[\s'\"])",
        re.IGNORECASE,
    ),
    re.compile(r"\bstatus[_ ]code\s*[:=]?\s*['\"]?([1-5]\d{2})\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)status\s*=\s*([1-5]\d{2})(?=\s|$)", re.IGNORECASE),
)


def probe_launchd(
    service_label: str,
    *,
    uid: int | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> LaunchdSnapshot:
    """Read one exact GUI LaunchAgent and its disabled override on macOS.

    Each subprocess has a five-second timeout and never invokes a shell.  An
    absent service is ``loaded=False``; a failed or unparseable query is unknown
    (``loaded=None``), never evidence that the service was unloaded.  Failure of
    the secondary disabled query preserves the first snapshot with an error.
    """

    if not isinstance(service_label, str) or not _SAFE_LABEL.fullmatch(service_label):
        return LaunchdSnapshot(loaded=None, error="invalid_service_label")
    if sys.platform != "darwin":
        return LaunchdSnapshot(loaded=None, error="unsupported_platform")
    selected_uid = os.getuid() if uid is None else uid
    if type(selected_uid) is not int or selected_uid < 0:
        return LaunchdSnapshot(loaded=None, error="invalid_uid")

    runner = subprocess.run if command_runner is None else command_runner
    domain = f"gui/{selected_uid}"
    service = f"{domain}/{service_label}"
    result, error = _run_launchctl(runner, ["print", service])
    if error is not None or result is None:
        return LaunchdSnapshot(loaded=None, error=error)
    if result.returncode != 0:
        missing = re.compile(
            rf'^\s*Could not find service "{re.escape(service_label)}" '
            rf"in domain for user gui: {selected_uid}\s*$",
            re.MULTILINE,
        )
        if missing.search(result.stderr) or missing.search(result.stdout):
            return LaunchdSnapshot(loaded=False)
        return LaunchdSnapshot(loaded=None, error="launchctl_failed")

    snapshot = _parse_launchd(result.stdout, service)
    if snapshot.loaded is not True:
        return snapshot
    disabled_result, error = _run_launchctl(runner, ["print-disabled", domain])
    if error is not None or disabled_result is None or disabled_result.returncode != 0:
        return replace(snapshot, error="disabled_status_unavailable")
    disabled = _parse_disabled(disabled_result.stdout, service_label)
    if disabled is None:
        return replace(snapshot, error="disabled_status_unavailable")
    return replace(snapshot, disabled=disabled)


def _run_launchctl(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    arguments: list[str],
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    try:
        result = runner(
            ["/bin/launchctl", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None, "launchctl_timeout"
    except (OSError, subprocess.SubprocessError):
        return None, "launchctl_unavailable"
    if (
        type(getattr(result, "returncode", None)) is not int
        or not isinstance(getattr(result, "stdout", None), str)
        or not isinstance(getattr(result, "stderr", None), str)
        or len(result.stdout) + len(result.stderr) > _MAX_COMMAND_OUTPUT
    ):
        return None, "launchctl_invalid_output"
    return result, None


def _parse_launchd(output: str, service: str) -> LaunchdSnapshot:
    lines = [line for line in output.splitlines() if line.strip()]
    unknown = LaunchdSnapshot(loaded=None, error="launchctl_invalid_output")
    if len(lines) < 3 or lines[0] != f"{service} = {{" or lines[-1] != "}":
        return unknown
    fields: dict[str, str] = {}
    depth = 1
    for line in lines[1:-1]:
        if not line.startswith(("\t", "    ")):
            return unknown
        if line.strip() == "}":
            depth -= 1
            if depth < 1:
                return unknown
            continue
        match = _TOP_LEVEL_FIELD.fullmatch(line) if depth == 1 else None
        if match is not None:
            key, value = match.groups()
            if key in fields:
                return unknown
            fields[key] = value
        if re.fullmatch(r"\s*[^=]+(?:=|=>) \{", line):
            depth += 1
    if depth != 1:
        return unknown
    state = fields.get("state")
    if state not in {"not running", "running", "waiting", "spawn scheduled"}:
        return unknown
    values: dict[str, int | None] = {}
    for key in ("runs", "last exit code", "pid"):
        raw = fields.get(key)
        if raw is None or (key == "last exit code" and raw == "(never exited)"):
            values[key] = None
            continue
        if not _INTEGER.fullmatch(raw):
            return unknown
        value = int(raw)
        if (key == "runs" and value < 0) or (key == "pid" and value <= 0):
            return unknown
        values[key] = value
    if state == "running" and values["pid"] is None:
        return unknown
    return LaunchdSnapshot(
        loaded=True,
        running=state == "running",
        runs=values["runs"],
        last_exit_code=values["last exit code"],
        pid=values["pid"],
    )


def _parse_disabled(output: str, service_label: str) -> bool | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] != "disabled services = {" or lines[-1] != "}":
        return None
    disabled = False
    matched = False
    for line in lines[1:-1]:
        match = _DISABLED_ENTRY.fullmatch(line)
        if match is None:
            return None
        if match[1] == service_label:
            if matched:
                return None
            matched = True
            disabled = match[2] in {"true", "disabled"}
    return disabled


def read_run_log(
    path: Path,
    *,
    now: datetime,
    max_bytes: int = 1_048_576,
) -> RunLogSnapshot:
    """Read at most ``max_bytes`` of log payload plus one alignment byte.

    Only complete, valid JSON records from the four run lifecycle events are
    retained, in file order.  A rotated/missing/empty log is an unknown signal,
    not proof of a stopped job.  Timestamps must include a timezone, are
    normalized to UTC, and cannot be more than 60 seconds ahead of ``now``.
    """

    if not isinstance(path, Path):
        return RunLogSnapshot(error="invalid_log_path")
    if type(max_bytes) is not int or max_bytes <= 0:
        return RunLogSnapshot(error="invalid_max_bytes")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return RunLogSnapshot(error="invalid_now")
    try:
        future_limit = now.astimezone(UTC) + timedelta(seconds=60)
    except (OverflowError, ValueError):
        return RunLogSnapshot(error="invalid_now")

    descriptor: int | None = None
    truncated = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return RunLogSnapshot(error="log_not_regular_file")
        if before.st_size == 0:
            return RunLogSnapshot(error="log_empty")
        truncated = before.st_size > max_bytes
        offset = max(0, before.st_size - max_bytes)
        alignment = os.pread(descriptor, 1, offset - 1) if offset else b"\n"
        payload = os.pread(descriptor, max_bytes, offset)
        after = os.fstat(descriptor)
        current = path.stat()
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            return RunLogSnapshot(error="log_rotated", truncated=truncated)
        if after.st_size < before.st_size:
            return RunLogSnapshot(error="log_rotated", truncated=truncated)
    except FileNotFoundError:
        return RunLogSnapshot(error="log_missing", truncated=truncated)
    except (OSError, ValueError, OverflowError):
        return RunLogSnapshot(error="log_unreadable", truncated=truncated)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    if not payload:
        return RunLogSnapshot(error="log_empty", truncated=truncated)
    if offset and alignment != b"\n":
        _, separator, payload = payload.partition(b"\n")
        if not separator:
            payload = b""
    records: list[RunRecord] = []
    for line in payload.splitlines():
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        record = _parse_run_record(entry, future_limit)
        if record is not None:
            records.append(record)
    return RunLogSnapshot(
        records=tuple(records),
        error=None if records else "log_no_records",
        truncated=truncated,
    )


def _parse_run_record(entry: object, future_limit: datetime) -> RunRecord | None:
    if not isinstance(entry, dict):
        return None
    event = entry.get("event")
    run_id = entry.get("run_id")
    raw_timestamp = entry.get("timestamp")
    if (
        not isinstance(event, str)
        or event not in _RUN_EVENTS
        or not isinstance(run_id, str)
        or not _SAFE_RUN_ID.fullmatch(run_id)
        or not isinstance(raw_timestamp, str)
        or len(raw_timestamp) > 80
    ):
        return None
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        timestamp = timestamp.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    if timestamp > future_limit:
        return None
    status = entry.get("status")
    if not isinstance(status, str) or status not in _RUN_STATUSES:
        status = None
    failed_count = entry.get("failed_article_count", 0)
    if type(failed_count) is not int or not 0 <= failed_count <= 1_000_000:
        failed_count = 0
    return RunRecord(
        run_id=run_id,
        timestamp=timestamp,
        event=event,
        status=status,
        error_type=_safe_identifier(entry.get("error_type")),
        http_status=_http_status(entry),
        stage=_safe_identifier(entry.get("stage")),
        failed_article_count=failed_count,
    )


def _safe_identifier(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def _http_status(entry: dict[str, object]) -> int | None:
    for field in ("http_status", "status_code"):
        value = entry.get(field)
        if type(value) is int and 100 <= value <= 599:
            return value
        if isinstance(value, str) and re.fullmatch(r"[1-5]\d{2}", value):
            return int(value)
    message = entry.get("error_message")
    if isinstance(message, str):
        for pattern in _HTTP_PATTERNS:
            match = pattern.search(message[:8192])
            if match is not None:
                return int(match[1])
    return None
