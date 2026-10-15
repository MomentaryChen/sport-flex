import os
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sportflex.runtime.browser import chrome_pids, kill_chrome


def _chrome_script(path: Path) -> None:
    path.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_kill_chrome_only_the_matching_profile(tmp_path):
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    chrome = tmp_path / "chrome"
    sleeper = tmp_path / "sleeper"
    _chrome_script(chrome)
    _chrome_script(sleeper)
    keep = subprocess.Popen([str(chrome), f"--user-data-dir={other.resolve()}"])
    victim = subprocess.Popen([str(chrome), f"--user-data-dir={mine.resolve()}"])
    # Same profile flag, but not a Chrome process: it must be left alone.
    decoy = subprocess.Popen([str(sleeper), f"--user-data-dir={mine.resolve()}"])
    try:
        assert _alive(victim.pid)
        killed = kill_chrome(mine)
        assert killed >= 1
        victim.wait(timeout=2)
        assert keep.poll() is None
        assert decoy.poll() is None
        assert chrome_pids(mine) == []
        assert chrome_pids(other) == [keep.pid] or keep.pid in chrome_pids(other)
    finally:
        for proc in (keep, victim, decoy):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)


def test_frozen_chrome_unblocks_when_killed(tmp_path):
    """SIGSTOP freezes Chrome the way a hung browser does: the call sits there, SIGKILL frees it."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    profile = tmp_path / "profile"
    profile.mkdir()
    with playwright_api.sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                channel="chrome",
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:
            pytest.skip(f"Chrome not available: {exc}")
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(8_000)
        pids = chrome_pids(profile)
        if not pids:
            context.close()
            pytest.fail("launched Chrome was not found by its user-data-dir")
        for pid in pids:
            os.kill(pid, signal.SIGSTOP)

        def kill_soon() -> None:
            time.sleep(0.6)
            kill_chrome(profile)

        threading.Thread(target=kill_soon, daemon=True).start()
        started = time.monotonic()
        try:
            page.evaluate("1")
        except Exception:
            pass
        elapsed = time.monotonic() - started
        closer = threading.Thread(target=_close, args=(context,), daemon=True)
        closer.start()
        closer.join(5)
        kill_chrome(profile)
        # Under the 8s Playwright timeout, so this passes only when SIGKILL unblocks the call.
        assert 0.4 < elapsed < 6, f"evaluate returned after {elapsed:.2f}s; kill should unblock a frozen Chrome"


def _close(context) -> None:
    try:
        context.close()
    except Exception:
        pass
