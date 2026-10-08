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
from sportflex.core.events import EventStore, NullEventStore
from sportflex.core.models import Slot
from sportflex.core.notify import Notifier
from sportflex.core.venue import Venue
from sportflex.providers import create_provider
from sportflex.providers.base import Provider
from sportflex.runtime.browser import open_page
from sportflex.runtime.telegram_bot import TelegramBot

SESSION_CHECK_SEC = 15 * 60  # keep-alive visit while no snipe is armed
LOGIN_HOLD_SEC = 5 * 60  # how long the page waits on the login form for a captcha reply
REPROMPT_SEC = 10 * 60  # still logged out and no reply: send a fresh captcha this often
MAX_PROMPTS = 3  # unanswered captchas before going quiet until /login
CAPTCHA_RETRY_SEC = 60  # fetching the captcha failed: try again this soon
PAY_REMIND_SEC = 5 * 60  # still unpaid this long after the order: remind once
DEFAULT_PAY_TIMEOUT_SEC = 590  # the site's OnlinePaymentTimeoutSeconds, when the lookup failed
RELOGIN_HINT = "驗證碼圖片會另外傳到 Telegram，直接回覆就能重新登入。"
_BUSY_SNIPE_PHASES = frozenset({"firing", "preparing", "ready"})


class AccountWorker:
    """Owns one account's browser. Playwright's sync API is thread-bound, so every page action
    — HTTP requests and the snipe/watch ticks alike — runs on this worker's own thread."""

    def __init__(
        self,
        account: Account,
        venues: dict[str, Venue],
        notifier: Notifier,
        events: EventStore | None = None,
        bot: TelegramBot | None = None,
    ) -> None:
        self.account = account
        self.notifier = notifier
        self.events = events or NullEventStore()
        self.bot = bot
        self.venues = {vid: venue for vid, venue in venues.items() if venue.provider == account.provider}
        self.snipes = {
            vid: SnipeJob(venue, account.id, self._record_grab, self._alert, self.events, on_logout=self._logged_out)
            for vid, venue in self.venues.items()
        }
        self.watch: WatchJob | None = None
        self.last_grab: dict | None = None
        self.session = {"loggedIn": False, "banner": ""}
        self.ready = threading.Event()
        self.startup_error = ""
        self._relogin_needed = False
        self._hold_until = 0.0  # a captcha is waiting for its code: keep the page on the login form
        self._prompted_at = 0.0
        self._payments: list[dict] = []  # placed orders we're watching until they're paid or expire
        self._logout_reason = "登入已失效"
        self._prompts = 0  # captchas sent this round without a reply
        self._gave_up = False
        self._checked_at = time.time()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sportflex-{account.id}", daemon=True)

    # ---- thread plumbing ----------------------------------------------

    def start(self, wait: float = 60) -> None:
        self._thread.start()
        self.ready.wait(wait)
        if not self.ready.is_set():
            self.startup_error = "瀏覽器啟動逾時"

    def stop(self, wait: float = 15) -> None:
        """Finish the current page action, then close Chrome so the profile is released cleanly."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(wait)

    def call(self, fn, timeout: float = 120):
        """Run fn(provider) on the browser thread and wait for its result."""
        if self.startup_error:
            raise RuntimeError(f"帳號 {self.account.display} 的瀏覽器無法使用：{self.startup_error}")
        if self._stop.is_set():
            raise RuntimeError("服務正在關閉")
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
                provider.order = self.account.order_options()
                if self.venues:
                    provider.api_pause_sec = max(venue.rules.api_pause_sec for venue in self.venues.values())
                self.session = provider.refresh_session()
                if not self.session["loggedIn"]:
                    self._event("logout_detected", "啟動時發現尚未登入", level="warn")
                    self._logged_out("服務啟動時發現尚未登入")
                for job in self.snipes.values():
                    job.restore()
                self.ready.set()
                while not self._stop.is_set():
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
        if time.time() < self._hold_until:
            return  # navigating now would throw away the captcha the user is typing
        active = [job for job in self.snipes.values() if job.active]
        for job in active:
            try:
                job.tick(provider)
            except Exception as exc:
                job.log.note(f"搶訂出錯：{exc}")
        if not active:  # a snipe owns the page (and checks the login itself); watching waits
            if self.watch and self.watch.running:
                try:
                    self.watch.tick(provider)
                except Exception as exc:
                    self.watch.log.note(f"盯場出錯：{exc}")
            self._keepalive(provider)
        self._check_payments(provider)
        self._maybe_prompt_relogin(provider)

    # ---- unpaid orders ---------------------------------------------------

    def _track_payment(self, result: dict) -> None:
        order = result.get("order") or {}
        if not (order.get("luid") and order.get("lid")):
            return
        placed = time.time()
        self._payments.append(
            {
                **{key: order.get(key) or "" for key in ("luid", "lid", "no", "paymentUrl", "carrier")},
                "label": f"{result.get('court', '')} {result.get('date', '')} {result.get('time', '')}".strip(),
                "placed": placed,
                "timeout": order.get("timeoutSec") or DEFAULT_PAY_TIMEOUT_SEC,
                "stage": "remind",
                "next": placed + PAY_REMIND_SEC,
                "misses": 0,
            }
        )

    def _check_payments(self, provider: Provider) -> None:
        """PAY_REMIND_SEC after an order: still on the unpaid list -> remind once. After the site's
        deadline: report that it is off the list (paid, or cancelled for not paying)."""
        now = time.time()
        for item in list(self._payments):
            if now < item["next"]:
                continue
            try:
                pending = provider.pending_payments(item["lid"])
            except Exception as exc:
                self._event("error", f"查詢付款狀態失敗：{exc}", level="warn")
                pending = None
            if pending is None:  # can't tell right now; try again shortly, then give up
                item["misses"] += 1
                item["next"] = now + 60
                if item["misses"] >= 10:
                    self._payments.remove(item)
                continue
            unpaid = item["luid"] in pending
            if item["stage"] == "remind":
                if not unpaid:
                    self._event("payment_done", f"訂單 {item['no']} 已不在待付款清單（已付款）")
                    self._payments.remove(item)
                    continue
                left = max(0, int(item["timeout"] - (now - item["placed"])) // 60)
                carrier = f"\n發票請選「手機載具」，填 {item['carrier']}" if item["carrier"] else ""
                self._event("payment_reminder", f"訂單 {item['no']} 還沒付款，剩約 {left} 分鐘", level="warn")
                self._alert(
                    f"⏰ 還沒付款：{item['label']}",
                    f"訂單 {item['no']} 還沒付款，約 {left} 分鐘後會被取消。\n"
                    f"打開連結 → 點「待付款」→ 前往付款\n{item['paymentUrl']}{carrier}",
                )
                item.update(stage="final", next=item["placed"] + item["timeout"] + 60)
            elif unpaid:  # the site hasn't cleared it yet
                if now > item["placed"] + item["timeout"] + 5 * 60:
                    self._payments.remove(item)
                else:
                    item["next"] = now + 60
            else:
                self._event("payment_closed", f"訂單 {item['no']} 已不在待付款清單")
                self._alert(
                    f"訂單 {item['no']} 已結束",
                    f"{item['label']} 已不在待付款清單：有付款就已完成；沒付款的話已因逾時被取消。",
                )
                self._payments.remove(item)

    # ---- staying logged in -----------------------------------------------

    def _keepalive(self, provider: Provider) -> None:
        """Visit the site now and then: keeps the server session warm and notices a logout early."""
        if time.time() - self._checked_at < SESSION_CHECK_SEC:
            return
        self._checked_at = time.time()
        try:
            self.session = provider.refresh_session()
        except Exception as exc:
            self._event("error", f"檢查登入失敗：{exc}", level="warn")
            return
        if self.session["loggedIn"]:
            self._clear_relogin_state()
        elif not self._relogin_needed:
            self._event("logout_detected", "定時檢查發現登入已失效", level="warn")
            self._alert("登入已失效", "定時檢查發現已被登出。" + (RELOGIN_HINT if self.bot else "請在網頁上重新登入。"))
            self._logged_out()

    def _logged_out(self, reason: str = "登入已失效") -> None:
        """Called at startup, by the jobs and by the keep-alive check whenever the site says we're logged out."""
        self.session = {**self.session, "loggedIn": False}
        if not self._relogin_needed:
            self._logout_reason = reason
        self._relogin_needed = True

    def _snipe_owns_browser(self) -> bool:
        """Don't navigate to the login page mid-snipe; it would break prepare/fire on the booking page."""
        for job in self.snipes.values():
            if job.active and job.spec and job.spec.get("phase") in _BUSY_SNIPE_PHASES:
                return True
        return False

    def _clear_relogin_state(self) -> None:
        self._relogin_needed = False
        self._restart_prompts()

    def _sync_logged_in_state(self, provider: Provider) -> bool:
        """The in-memory flag can lag (e.g. after a successful Telegram login or a transient NeedLogin)."""
        try:
            self.session = provider.refresh_session()
        except Exception as exc:
            self._event("error", f"檢查登入失敗：{exc}", level="warn")
            return False
        if self.session["loggedIn"]:
            self._clear_relogin_state()
            return True
        return False

    def _maybe_prompt_relogin(self, provider: Provider) -> None:
        """Send a captcha over Telegram; the reply logs in (TelegramBot -> WorkerPool.login_with).
        After MAX_PROMPTS unanswered captchas, stop and say so until /login restarts it."""
        if not (self._relogin_needed and self.bot) or time.time() - self._prompted_at < REPROMPT_SEC:
            return
        if self._snipe_owns_browser():
            return
        if self._sync_logged_in_state(provider):
            return
        if self._prompts >= MAX_PROMPTS:
            if not self._gave_up:
                self._gave_up = True
                self._event("relogin_paused", f"傳了 {MAX_PROMPTS} 次驗證碼都沒有回覆，停止重試", level="warn")
                self.bot.send_text_async(self._paused_text())
            return
        self._prompted_at = time.time()
        self._prompts += 1
        try:
            username, password = self.account.credentials()
            image = provider.captcha(username, password)
        except Exception as exc:
            # Not sent, so it doesn't use up a try; retry in a minute (e.g. a network blip at startup).
            self._prompts -= 1
            self._prompted_at = time.time() - REPROMPT_SEC + CAPTCHA_RETRY_SEC
            self._event("error", f"取驗證碼失敗：{exc}", level="warn")
            return
        self._hold_until = time.time() + LOGIN_HOLD_SEC
        self._event("relogin_prompt", f"已透過 Telegram 傳送驗證碼（{self._logout_reason}）")
        self.bot.ask_code_async(
            self.account.id,
            image,
            f"[{self.account.id}] {self.account.display} {self._logout_reason}，請回覆這張圖上的驗證碼"
            f"（第 {self._prompts}/{MAX_PROMPTS} 次）",
        )

    def _paused_text(self) -> str:
        return (
            f"[{self.account.id}] {self.account.display} 已傳 {MAX_PROMPTS} 次驗證碼都沒有回覆，先停止提醒。\n"
            f"要重新登入請傳 /login {self.account.id}（或到網頁上登入）。"
        )

    def _restart_prompts(self) -> None:
        """The user is responding (/login, the web captcha, or a code reply): count afresh."""
        self._prompts = 0
        self._gave_up = False

    def _publish_grab(self, result: dict, label: str, job: str) -> None:
        result["at"] = time.time()
        result["account"] = self.account.id
        self.last_grab = result
        self._grab_event(result, label, job)
        self._track_payment(result)
        announce_grab(self.notifier, self.account.display, result)

    def _record_grab(self, result: dict) -> None:
        self._publish_grab(
            result,
            "自動送出訂單" if result.get("order") else "自動送出到確認頁",
            "auto",
        )

    def _alert(self, title: str, body: str) -> None:
        self._event("alert", title, level="warn", data={"body": body})
        self.notifier.send(title, f"{body}\n帳號：{self.account.display}")

    def _event(self, kind: str, message: str, venue: str = "", job: str = "session", **kwargs) -> None:
        self.events.record(kind, message, account=self.account.id, venue=venue, job=job, **kwargs)

    def _grab_event(self, result: dict, label: str, job: str) -> None:
        self._event(
            "grab",
            f"{label} {result['court']} {result['date']} {result['time']}",
            venue=result.get("venue", ""),
            job=job,
            data={key: result.get(key) for key in ("court", "date", "time", "price", "url", "pressed", "order")},
        )

    # ---- session -------------------------------------------------------

    def session_info(self) -> dict:
        return {**self.session, "account": self.account.public()}

    def captcha(self) -> dict:
        return {"image": base64.b64encode(self.captcha_image()).decode("ascii")}

    def captcha_image(self) -> bytes:
        username, password = self.account.credentials()

        def run(provider: Provider) -> bytes:
            image = provider.captcha(username, password)
            if self._relogin_needed or not self.session["loggedIn"]:  # never pause a snipe that is still logged in
                self._hold_until = time.time() + LOGIN_HOLD_SEC
                # this captcha (from /login or the web page) is attempt 1 of a new round
                self._restart_prompts()
                self._prompts = 1
                self._prompted_at = time.time()
                if self.bot:
                    self._event("relogin_prompt", "已透過 Telegram 傳送驗證碼（網頁顯示驗證碼）")
                    self.bot.ask_code_async(
                        self.account.id,
                        image,
                        f"[{self.account.id}] {self.account.display} {self._logout_reason}，請回覆這張圖上的驗證碼"
                        f"（第 {self._prompts}/{MAX_PROMPTS} 次）",
                    )
            return image

        return self.call(run)

    def login(self, code: str) -> dict:
        def run(provider: Provider) -> dict:
            self._hold_until = 0
            self._restart_prompts()
            result = provider.login(code)
            if result.get("ok"):
                # Trust the login API first; refresh_session can briefly lag right after submit.
                self._clear_relogin_state()
                self.session = provider.refresh_session()
                if not self.session["loggedIn"]:
                    self.session = provider.refresh_session()
                self._event("login", "登入成功")
            else:
                self._event("login_failed", f"登入失敗：{result.get('message', '')}", level="warn")
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
            result = grab(provider, venue, court, query_date, slot)
            self._publish_grab(
                result,
                "手動送出訂單" if result.get("order") else "手動送出到確認頁",
                "manual",
            )
            return result

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
                self.watch = WatchJob(
                    venue,
                    spec,
                    self._record_grab,
                    self._alert,
                    self.events,
                    on_logout=self._logged_out,
                    account_id=self.account.id,
                )
            return self.watch_state()

        return self.call(run)

    def watch_state(self) -> dict:
        if not self.watch:
            return {"running": False, "spec": None, "log": []}
        return self.watch.state()
