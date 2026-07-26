"""Ensure only one Desktop Commentator process runs."""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile

from .paths import user_config_dir

logger = logging.getLogger(__name__)

_lock: QLockFile | None = None
_pid_file: Path | None = None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # Access denied often means process exists
            err = kernel32.GetLastError()
            return err == 5
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def try_acquire() -> bool:
    """Return True if this process owns the singleton lock."""
    global _lock, _pid_file
    d = user_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "instance.lock"
    pid_path = d / "instance.pid"

    # If previous PID is dead, clear stale lock files
    if pid_path.is_file():
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and not _pid_alive(old_pid):
            logger.warning("Stale instance pid=%s — clearing lock", old_pid)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
        elif old_pid == os.getpid():
            # leftover from same pid reuse
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    lock = QLockFile(str(path))
    # Reclaim quickly if holder crashed
    lock.setStaleLockTime(1_000)
    if not lock.tryLock(500):
        extra = ""
        if pid_path.is_file():
            try:
                extra = f" (pid {pid_path.read_text(encoding='utf-8').strip()})"
            except OSError:
                pass
        logger.warning("Another instance running%s — lock %s", extra, path)
        return False

    _lock = lock
    _pid_file = pid_path
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    atexit.register(release)
    logger.info("Acquired single-instance lock pid=%s", os.getpid())
    return True


def release() -> None:
    global _lock, _pid_file
    if _lock is not None:
        try:
            _lock.unlock()
        except Exception:
            pass
        _lock = None
    if _pid_file is not None:
        try:
            if _pid_file.is_file():
                _pid_file.unlink()
        except OSError:
            pass
        _pid_file = None
    # Also remove lock file path remnants
    try:
        (user_config_dir() / "instance.lock").unlink(missing_ok=True)
    except OSError:
        pass
