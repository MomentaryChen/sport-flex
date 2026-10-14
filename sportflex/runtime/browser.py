from __future__ import annotations

import os
import signal
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from sportflex.core.accounts import Account


@contextmanager
def open_page(account: Account, headless: bool = True):
    """A page in the account's persistent Chrome profile, so logins survive restarts."""
    profile = account.profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    launch: dict = {
        "channel": "chrome",
        "headless": headless,
        "locale": "zh-TW",
        "viewport": {"width": 430, "height": 900},
    }
    if os.environ.get("SPORT_FLEX_CHROME_NO_SANDBOX") == "1":
        launch["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), **launch)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30_000)
        try:
            yield page
        finally:
            context.close()


def chrome_pids(profile: Path) -> list[int]:
    """Chrome processes launched with this user-data-dir, parents before their children."""
    target = str(profile.resolve())
    found: list[int] = []
    seen: set[int] = set()
    for pid, args in _process_args():
        if pid in seen or not _same_profile(args, target) or not _looks_like_chrome(args):
            continue
        for child in _with_descendants(pid):
            if child not in seen:
                seen.add(child)
                found.append(child)
    return found


def kill_chrome(profile: Path) -> int:
    """SIGKILL Chrome for this profile. A stuck call then sees the pipe close and raises."""
    killed = 0
    for pid in chrome_pids(profile):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed += 1
    return killed


def _process_args() -> list[tuple[int, list[str]]]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        args = [part.decode(errors="replace") for part in raw.split(b"\x00") if part]
        if args:
            found.append((int(entry), args))
    return found


def _same_profile(args: list[str], target: str) -> bool:
    for index, arg in enumerate(args):
        raw = ""
        if arg.startswith("--user-data-dir="):
            raw = arg.split("=", 1)[1]
        elif arg == "--user-data-dir" and index + 1 < len(args):
            raw = args[index + 1]
        if raw and str(Path(raw).resolve()) == target:
            return True
    return False


def _looks_like_chrome(args: list[str]) -> bool:
    """The executable name identifies Chrome. A parent directory can contain the same letters."""
    names = [Path(args[0]).name.lower()]
    if len(args) > 1:
        names.append(Path(args[1]).name.lower())  # a script's path, when args[0] is the shell
    return any(name == "google-chrome" or name.startswith(("chrome", "chromium")) for name in names)


def _with_descendants(pid: int) -> list[int]:
    found = [pid]
    index = 0
    while index < len(found):
        found.extend(_children(found[index]))
        index += 1
    return found


def _children(pid: int) -> list[int]:
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    return [int(part) for part in text.split() if part.isdigit()]
