from __future__ import annotations

from contextlib import contextmanager

from playwright.sync_api import sync_playwright

from sportflex.core.accounts import Account


@contextmanager
def open_page(account: Account, headless: bool = True):
    """A page in the account's persistent Chrome profile, so logins survive restarts."""
    profile = account.profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            channel="chrome",
            headless=headless,
            locale="zh-TW",
            viewport={"width": 430, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30_000)
        try:
            yield page
        finally:
            context.close()
