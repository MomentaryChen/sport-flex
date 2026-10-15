"""One long-running Sport Flex process per machine.

Two copies of the UI each open the accounts and snipe on their own. The
multi-account spread only coordinates browsers inside one process, so a second
copy of the same account places a second order. An exclusive lock file stops
that on one machine. Another machine does not see this file; the Telegram
409 check in the bot is what refuses that case.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sportflex.core.identity import server_label
from sportflex.paths import OUTPUT_DIR

LOCK_NAME = "instance.lock"


class InstanceAlreadyRunning(RuntimeError):
    """Another process on this machine already holds the Sport Flex lock."""


class InstanceLock:
    """Advisory exclusive lock held for the lifetime of ``python -m sportflex ui``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (OUTPUT_DIR / LOCK_NAME)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        if not _try_lock(fh):
            holder = _read_holder(fh)
            fh.close()
            raise InstanceAlreadyRunning(_busy_message(holder))
        self._fh = fh
        payload = json.dumps({"pid": os.getpid(), "instance": server_label()}, ensure_ascii=False)
        # Overwrite in place. Truncating to empty first would drop a byte-range lock on Windows.
        fh.seek(0)
        fh.write(payload)
        fh.truncate()
        fh.flush()

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()


def _busy_message(holder: dict) -> str:
    parts: list[str] = []
    label = str(holder.get("instance") or "").strip()
    pid = holder.get("pid")
    if label:
        parts.append(label)
    if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()):
        parts.append(f"pid {pid}")
    detail = f"（{'，'.join(parts)}）" if parts else ""
    return (
        f"這台機器已經有 Sport Flex 在跑{detail}。\n"
        "同一個程式不要開兩份，否則同一個帳號會重複下單；多帳號錯開只在同一個程式裡有效。\n"
        "請先停掉另一個再啟動。"
    )


def _read_holder(fh) -> dict:
    try:
        fh.seek(0)
        data = json.loads(fh.read() or "{}")
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _try_lock(fh) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(" ")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
