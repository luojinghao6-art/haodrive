"""Traffic accounting buffer.

Every upload/download byte funnels through here. SQLite is only touched when
the buffer grows past a threshold or the periodic flusher fires, so a 2GB
transfer does not mean 250,000 tiny transactions.
"""
from __future__ import annotations

import threading
import time

from . import db

_lock = threading.Lock()
_up = 0
_down = 0
_up_files = 0
_down_files = 0
FLUSH_BYTES = 16 * 1024 * 1024


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def add(up: int = 0, down: int = 0, up_files: int = 0, down_files: int = 0) -> None:
    """Record transferred bytes. `up`/`down` may be called with one direction."""
    global _up, _down, _up_files, _down_files
    with _lock:
        _up += max(0, int(up))
        _down += max(0, int(down))
        _up_files += max(0, int(up_files))
        _down_files += max(0, int(down_files))
        pending = _up + _down
    if pending >= FLUSH_BYTES:
        flush()


def flush() -> None:
    global _up, _down, _up_files, _down_files
    with _lock:
        if not (_up or _down or _up_files or _down_files):
            return
        up, down = _up, _down
        up_files, down_files = _up_files, _down_files
        _up = _down = _up_files = _down_files = 0
    try:
        db.traffic_add(_today(), up, down, up_files, down_files)
    except Exception:
        # Put them back so the next flush retries; losing traffic on a
        # transient SQLITE_BUSY is worse than a slightly late chart.
        with _lock:
            _up += up
            _down += down
            _up_files += up_files
            _down_files += down_files


def snapshot() -> dict[str, int]:
    with _lock:
        return {"up": _up, "down": _down, "up_files": _up_files,
                "down_files": _down_files}
