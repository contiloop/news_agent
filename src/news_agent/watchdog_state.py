"""Private, atomic JSON persistence for watchdog notification state.

Callers validate the watchdog schema and hold their own run lock across reads,
decisions, and writes. This module only validates safe paths and JSON values.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from pathlib import Path

__all__ = ["WatchdogStateError", "load_watchdog_state", "save_watchdog_state"]

_MAX_STATE_BYTES = 262_144


class WatchdogStateError(RuntimeError):
    """Raised when watchdog state cannot be safely read or replaced."""


def load_watchdog_state(path: Path) -> dict[str, object]:
    """Read one bounded JSON object, returning an empty object if absent."""

    path = _validated_path(path)
    descriptor = -1
    try:
        _check_parent(path.parent)
        existing = _regular_file_info(path)
        if existing is None:
            return {}
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        # Do not block if a regular file is replaced with a FIFO before open.
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return {}
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (existing.st_dev, existing.st_ino):
            raise WatchdogStateError("Watchdog state path changed while reading")
        if opened.st_size > _MAX_STATE_BYTES:
            raise WatchdogStateError("Watchdog state exceeds the size limit")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read(_MAX_STATE_BYTES + 1)
        if len(content) > _MAX_STATE_BYTES:
            raise WatchdogStateError("Watchdog state exceeds the size limit")
        state = json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
        if not isinstance(state, dict):
            raise WatchdogStateError("Watchdog state must be a JSON object")
        return state
    except (OSError, UnicodeError, ValueError, RecursionError):
        # Parser and filesystem errors must not expose state or secret values.
        raise WatchdogStateError("Watchdog state cannot be safely read") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def save_watchdog_state(path: Path, state: dict[str, object]) -> None:
    """Atomically replace state with a private, flushed JSON object."""

    path = _validated_path(path)
    try:
        if not isinstance(state, dict):
            raise WatchdogStateError("Watchdog state must be a JSON object")
        _check_json_value(state, set())
        content = (
            json.dumps(state, ensure_ascii=False, allow_nan=False, sort_keys=True)
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WatchdogStateError("Watchdog state is not valid JSON") from None
    if len(content) > _MAX_STATE_BYTES:
        raise WatchdogStateError("Watchdog state exceeds the size limit")

    descriptor = -1
    temporary_path: Path | None = None
    try:
        _check_parent(path.parent)
        _regular_file_info(path)
        _create_parent(path.parent)
        _check_parent(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Reject a symlink or special file introduced during the write, too.
        _check_parent(path.parent)
        _regular_file_info(path)
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, ValueError):
        raise WatchdogStateError("Watchdog state cannot be safely written") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                # Preserve the original failure; any remaining temp is private.
                pass


def _validated_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise WatchdogStateError("Watchdog state requires an explicit pathlib.Path")
    if (
        not path.name.strip()
        or path.name in {".", ".."}
        or ".." in path.parts
        or "\x00" in os.fspath(path)
    ):
        raise WatchdogStateError("Watchdog state path must name a safe file")
    return path


def _check_parent(parent: Path) -> None:
    try:
        info = parent.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise WatchdogStateError("Watchdog state parent must be a real directory")


def _create_parent(parent: Path) -> None:
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _check_parent(directory)


def _regular_file_info(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise WatchdogStateError("Watchdog state path must be a regular file")
    return info


def _reject_constant(value: str) -> object:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _check_json_value(value: object, ancestors: set[int]) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if not isinstance(value, (dict, list)) or id(value) in ancestors:
        raise WatchdogStateError("Watchdog state is not valid JSON")
    ancestors.add(id(value))
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise WatchdogStateError("Watchdog state object keys must be strings")
                _check_json_value(item, ancestors)
        else:
            for item in value:
                _check_json_value(item, ancestors)
    finally:
        ancestors.remove(id(value))
