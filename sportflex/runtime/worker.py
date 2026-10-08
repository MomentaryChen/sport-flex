from __future__ import annotations

import base64
import queue
import threading
import time

from sportflex.core.accounts import Account
from sportflex.core.engine import (
    SnipeJob,
    WatchJob,
    announce_grab,
    build_board,
    court_public,
    grab,
    public_snapshot,
)
from sportflex.core.models import Slot
from sportflex.core.notify import Notifier
from sportflex.core.venue import Venue
from sportflex.providers import create_provider
from sportflex.providers.base import Provider
from sportflex.runtime.browser import open_page


class AccountWorker:
    """Owns one account's browser. Playwright's sync API is thread-bound, so every page action
    — HTTP requests and the snipe/watch ticks alike — runs on this worker's own thread."""

    def __init__(self, account: Account, venues: dict[str, Venue], notifier: Notifier) -> None:
        self.account = account
        self.notifier = notifier
        self.venues = {vid: venue for vid, venue in venues.items() if venue.provider == account.provider}
        self.snipes = {
            vid: SnipeJob(venue, account.id, self._record_grab, self._alert) for vid, venue in self.venues.items()
        }
        self.watch: WatchJob | None = None
        self.last_grab: dict | None = None
        self.session = {"loggedIn": False, "banner": ""}
        self.ready = threading.Event()
        self.startup_error = ""
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"sportflex-{account.id}", daemon=True)

    # ---- thread plumbing ----------------------------------------------

    def start(self, wait: float = 60) -> None:
        self._thread.start()
        self.ready.wait(wait)
        if not self.ready.is_set():
            self.startup_error = "瀏覽器啟動逾時"

    def call(self, fn, timeout: float = 120):
        """Run fn(provider) on the browser thread and wait for its result."""
        if self.startup_error:
            raise RuntimeError(f"帳號 {self.account.display} 的瀏覽器無法使用：{self.startup_error}")
        done = threading.Event()
        box: dict = {}

        def job(provider: Provider):
            try:
                box["value"] = fn(provider)
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()

        self._queue.put(job)
        if not done.wait(timeout):
            raise TimeoutError("瀏覽器操作逾時")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _run(self) -> None:
        try:
            with open_page(self.account) as page:
                provider = create_provider(self.account.provider, page)
                self.session = provider.refresh_session()
                for job in self.snipes.values():
                    job.restore()
                self.ready.set()
                while True:
                    try:
                        job = self._queue.get(timeout=1)
                    except queue.Empty:
                        self._tick(provider)
                        continue
                    job(provider)
                    self._tick(provider)
        except Exception as exc:
            self.startup_error = str(exc)
            self.ready.set()

    def _tick(self, provider: Provider) -> None:
        active = [job for job in self.snipes.values() if job.active]
        for job in active:
            try:
                job.tick(provider)
            except Exception as exc:
                job.log.note(f"搶訂出錯：{exc}")
        if active:
            return  # a snipe owns the page; watching waits
        if self.watch and self.watch.running:
            try:
                self.watch.tick(provider)
            except Exception as exc:
                self.watch.log.note(f"盯場出錯：{exc}")

    def _record_grab(self, result: dict) -> None:
        result["at"] = time.time()
        result["account"] = self.account.id
        self.last_grab = result
        announce_grab(self.notifier, self.account.display, result)

    def _alert(self, title: str, body: str) -> None:
        self.notifier.send(title, f"{body}\n帳號：{self.account.display}")

    # ---- session -------------------------------------------------------

    def session_info(self) -> dict:
        return {**self.session, "account": self.account.public()}

    def captcha(self) -> dict:
        username, password = self.account.credentials()
        image = self.call(lambda provider: provider.captcha(username, password))
        return {"image": base64.b64encode(image).decode("ascii")}

    def login(self, code: str) -> dict:
        def run(provider: Provider) -> dict:
            result = provider.login(code)
            if result.get("ok"):
                self.session = provider.refresh_session()
            return result

        result = self.call(run)
        return {**result, **self.session_info()}

    # ---- browsing and booking -----------------------------------------

    def board(self, venue: Venue, category: str, query_date: str) -> dict:
        return self.call(lambda provider: build_board(provider, venue, category, query_date), timeout=90)

    def courts(self, venue: Venue, category: str) -> dict:
        courts = self.call(lambda provider: provider.list_courts(venue, category), timeout=60)
        return {"venue": venue.id, "category": category, "courts": [court_public(court) for court in courts]}

    def grab(self, venue: Venue, category: str, court_id: str, query_date: str, slot: Slot) -> dict:
        def run(provider: Provider) -> dict:
            court = provider.find_court(venue, category, court_id)
            return grab(provider, venue, court, query_date, slot)

        return self.call(run, timeout=90)

    def press(self, label: str) -> dict:
        return self.call(lambda provider: public_snapshot(provider.press(label)), timeout=60)

    # ---- jobs (mutated on the browser thread so ticks never see half-updated state) ----

    def set_snipe(self, venue: Venue, category: str | None, targets: list | None) -> dict:
        job = self.snipes[venue.id]
        if targets is None:
            return self.call(lambda _provider: job.cancel())
        return self.call(lambda _provider: job.arm(category or venue.categories[0], targets))

    def set_watch(self, venue: Venue | None, spec: dict | None) -> dict:
        def run(_provider: Provider) -> dict:
            if venue is None or spec is None:
                if self.watch:
                    self.watch.running = False
                    self.watch.log.note("已停止盯場")
            else:
                self.watch = WatchJob(venue, spec, self._record_grab, self._alert)
            return self.watch_state()

        return self.call(run)

    def watch_state(self) -> dict:
        if not self.watch:
            return {"running": False, "spec": None, "log": []}
        return self.watch.state()
