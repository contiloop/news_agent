"""Non-blocking process lock for one-shot news-agent runs."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path
from types import TracebackType
from typing import Self

__all__ = ["RunLock", "RunLockBusyError", "RunLockError", "run_lock"]


class RunLockError(RuntimeError):
    """Raised when a run lock cannot be created, acquired, or released."""


class RunLockBusyError(RunLockError):
    """Raised when another process already holds the requested run lock."""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        super().__init__(f"Run lock is already held: {lock_file}")


class RunLock:
    """Hold an exclusive advisory lock for the duration of a ``with`` block.

    The lock file is intentionally retained after release. Removing it would let
    a new process lock a different inode while an earlier process still holds the
    original one.
    """

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = _validated_lock_file(lock_file)
        self._file_descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        """Return whether this instance currently owns its lock descriptor."""

        return self._file_descriptor is not None

    def __enter__(self) -> Self:
        if self._file_descriptor is not None:
            raise RunLockError(f"Run lock is already acquired: {self.lock_file}")

        file_descriptor = _open_lock_file(self.lock_file)
        try:
            _acquire_nonblocking(file_descriptor, self.lock_file)
        except BaseException:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            raise

        self._file_descriptor = file_descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        """Release the held lease and close its descriptor, if acquired."""

        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return

        self._file_descriptor = None
        unlock_error: OSError | None = None
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            unlock_error = exc
        finally:
            try:
                os.close(file_descriptor)
            except OSError as exc:
                if unlock_error is None:
                    unlock_error = exc

        if unlock_error is not None:
            raise RunLockError(
                f"Run lock could not be released: {self.lock_file}"
            ) from unlock_error


def run_lock(lock_file: Path) -> RunLock:
    """Return a context manager for an explicit run lock file."""

    return RunLock(lock_file)


def _validated_lock_file(lock_file: Path) -> Path:
    if not isinstance(lock_file, Path):
        raise TypeError("Run lock file must be an explicit pathlib.Path")
    if not lock_file.name.strip() or lock_file.name in {".", ".."}:
        raise ValueError("Run lock file must name a specific file")
    if "\x00" in os.fspath(lock_file):
        raise ValueError("Run lock file cannot contain a null byte")
    return lock_file


def _open_lock_file(lock_file: Path) -> int:
    try:
        lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(lock_file, flags, 0o600)
    except (OSError, ValueError) as exc:
        raise RunLockError(f"Run lock file cannot be used: {lock_file}") from exc

    try:
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise RunLockError(f"Run lock path is not a regular file: {lock_file}")
    except BaseException:
        os.close(file_descriptor)
        raise

    return file_descriptor


def _acquire_nonblocking(file_descriptor: int, lock_file: Path) -> None:
    while True:
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RunLockBusyError(lock_file) from None
            raise RunLockError(f"Run lock could not be acquired: {lock_file}") from exc
