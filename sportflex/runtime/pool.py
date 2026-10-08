from __future__ import annotations

import threading

from sportflex.core.accounts import load_accounts, load_env, pick_account
from sportflex.core.events import EventStore
from sportflex.core.notify import Notifier, default_notifier
from sportflex.core.venue import Venue, get_venue, load_venues
from sportflex.runtime.telegram_bot import TelegramBot
from sportflex.runtime.worker import AccountWorker


def bot_from_env() -> TelegramBot | None:
    """Telegram re-login is on whenever the bot is configured; SPORT_FLEX_TELEGRAM_RELOGIN=0 turns it off."""
    env = load_env()
    token, chat_id = env.get("SPORT_FLEX_TELEGRAM_BOT_TOKEN"), env.get("SPORT_FLEX_TELEGRAM_CHAT_ID")
    if not token or not chat_id or env.get("SPORT_FLEX_TELEGRAM_RELOGIN") == "0":
        return None
    return TelegramBot(token, chat_id)


class WorkerPool:
    """One AccountWorker per configured account; different accounts snipe in parallel."""

    def __init__(
        self,
        notifier: Notifier | None = None,
        events: EventStore | None = None,
        bot: TelegramBot | None = None,
    ) -> None:
        self.venues = load_venues()
        self.accounts = load_accounts()
        self.notifier = notifier or default_notifier()
        self.events = events or EventStore()
        self.bot = bot or bot_from_env()
        self.workers = {
            account_id: AccountWorker(account, self.venues, self.notifier, self.events, self.bot)
            for account_id, account in self.accounts.items()
        }

    def start(self) -> None:
        threads = [threading.Thread(target=worker.start) for worker in self.workers.values()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for worker in self.workers.values():
            if worker.startup_error:
                print(f"帳號 {worker.account.display} 啟動失敗：{worker.startup_error}", flush=True)
        if self.bot:
            self.bot.start(self)
            print("Telegram 重新登入已啟用：登入失效時會傳驗證碼過去", flush=True)

    def stop(self) -> None:
        if self.bot:
            self.bot.stop()
        threads = [threading.Thread(target=worker.stop) for worker in self.workers.values()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.events.close()

    def resolve(self, venue_id: str | None, account_id: str | None) -> tuple[Venue, AccountWorker]:
        venue = get_venue(venue_id)
        account = pick_account(venue.provider, account_id)
        return venue, self.workers[account.id]

    def meta(self) -> dict:
        return {
            "venues": [venue.public() for venue in self.venues.values()],
            "accounts": [
                {**worker.account.public(), "ready": not worker.startup_error, "error": worker.startup_error}
                for worker in self.workers.values()
            ],
        }

    def snipes(self) -> list[dict]:
        return [
            job.state()
            for worker in self.workers.values()
            for job in worker.snipes.values()
            if job.spec
        ]

    # ---- Telegram re-login (TelegramBot's Relogin protocol) -------------

    def account_ids(self) -> list[str]:
        return list(self.workers)

    def status_text(self) -> str:
        lines = []
        for worker in self.workers.values():
            state = "瀏覽器無法使用" if worker.startup_error else "已登入" if worker.session.get("loggedIn") else "未登入"
            armed = [job.state()["release"]["targetLabel"] for job in worker.snipes.values() if job.active]
            lines.append(f"{worker.account.id}（{worker.account.display}）：{state}" + (f"，搶訂 {'、'.join(armed)}" if armed else ""))
        return "\n".join(lines) or "沒有帳號"

    def captcha_for(self, account_id: str) -> bytes:
        return self.workers[account_id].captcha_image()

    def login_with(self, account_id: str, code: str) -> dict:
        return self.workers[account_id].login(code)
