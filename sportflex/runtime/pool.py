from __future__ import annotations

import threading

from sportflex.core.accounts import load_accounts, pick_account
from sportflex.core.notify import Notifier, default_notifier
from sportflex.core.venue import Venue, get_venue, load_venues
from sportflex.runtime.worker import AccountWorker


class WorkerPool:
    """One AccountWorker per configured account; different accounts snipe in parallel."""

    def __init__(self, notifier: Notifier | None = None) -> None:
        self.venues = load_venues()
        self.accounts = load_accounts()
        self.notifier = notifier or default_notifier()
        self.workers = {
            account_id: AccountWorker(account, self.venues, self.notifier)
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

    def stop(self) -> None:
        threads = [threading.Thread(target=worker.stop) for worker in self.workers.values()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

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
