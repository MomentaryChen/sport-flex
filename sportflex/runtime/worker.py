from __future__ import annotations

import base64
import copy
import queue
import threading
import time
from typing import Callable

from sportflex.core.accounts import Account
from datetime import date, datetime, timedelta

from sportflex.core.coord import Coordinator
from sportflex.core.engine import (
    SnipeJob,
    WatchJob,
    announce_grab,
    court_public,
    grab,
    iter_board,
    public_snapshot,
)
from sportflex.core.events import EventStore, NullEventStore
from sportflex.core.models import Slot, slot_bounds
from sportflex.core.notify import Notifier
from sportflex.core.refund import plan_refund_reminder
from sportflex.core.routine import apply_saved_routine, build_routine, describe_routine, skip_wave
from sportflex.core.rules import next_release, quota_reason
from sportflex.core.storage import BookingStore, RoutineStore, WatchStore
from sportflex.core.venue import Venue
from sportflex.providers import create_provider
from sportflex.providers.base import Provider, ProviderError
from sportflex.runtime.browser import open_page
from sportflex.core.identity import login_prefix
from sportflex.runtime.captcha_ocr import read_captcha
from sportflex.runtime.telegram_bot import TelegramBot

ORDERS_BLOCKED = "另一個 Sport Flex 正在使用同一個 Telegram bot，這台已暫停下單，避免重複訂到。"
SESSION_CHECK_SEC = 15 * 60  # keep-alive visit while no snipe is armed
LOGIN_HOLD_SEC = 5 * 60  # how long the page waits on the login form for a captcha reply
REPROMPT_SEC = 10 * 60  # still logged out and no reply: send a fresh captcha this often
MAX_PROMPTS = 3  # unanswered captchas before going quiet until /login
OCR_SUBMIT_LIMIT = 2  # wrong OCR submits before the image is handed to a person
CAPTCHA_RETRY_SEC = 60  # fetching the captcha failed: try again this soon
PAY_REMIND_SEC = 5 * 60  # still unpaid this long after the order: remind once
DEFAULT_PAY_TIMEOUT_SEC = 590  # the site's OnlinePaymentTimeoutSeconds, when the lookup failed
BOARD_CACHE_SEC = 30  # reuse one venue/sport/date browse; the page reload button asks for a fresh scan
RELOGIN_HINT = "會先嘗試自動辨識驗證碼；不行時圖片會傳到 Telegram，直接回覆就能重新登入。"
_BUSY_SNIPE_PHASES = frozenset({"firing", "preparing", "ready"})


def _booking_snapshot(result: dict) -> dict:
    """The fields a refund reminder needs, kept beside the unpaid-order watch."""
    order = result.get("order") or {}
    return {
        "venue": result.get("venue") or "",
        "venueName": result.get("venueName") or "",
        "court": result.get("court") or "",
        "date": result.get("date") or "",
        "time": result.get("time") or "",
        "order": {"no": order.get("no") or "", "luid": order.get("luid") or "", "paymentUrl": order.get("paymentUrl") or ""},
    }


def _board_complete(events: list[dict]) -> bool:
    """True when every court announced in the meta event was actually loaded."""
    expected = None
    got = 0
    for event in events:
        kind = event.get("type")
        if kind == "meta":
            expected = len(event.get("courts") or [])
        elif kind == "court":
            got += 1
    return expected is not None and got == expected


def _board_payload(venue_id: str, category: str, events: list[dict], query_date: str = "") -> dict:
    groups = []
    window = {"start": "", "end": "", "date": query_date}
    for event in events:
        kind = event.get("type")
        if kind == "meta":
            window = event["window"]
        elif kind == "court":
            groups.append(event["court"])
            window = event["window"]
    return {"venue": venue_id, "category": category, "window": window, "courts": groups}


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
        coord: Coordinator | None = None,
        booking_blocked: Callable[[], bool] | None = None,
    ) -> None:
        self.account = account
        self.notifier = notifier
        self.events = events or NullEventStore()
        self.bot = bot
        self.coord = coord
        self.booking_blocked = booking_blocked  # another process is polling the same bot
        self.venues = {vid: venue for vid, venue in venues.items() if venue.provider == account.provider}
        self.snipes = {
            vid: SnipeJob(
                venue,
                account.id,
                self._record_grab,
                self._alert,
                self.events,
                on_logout=self._logged_out,
                on_watch=lambda spec, venue=venue: self._adopt_watch(venue, spec),
                coord=coord,
            )
            for vid, venue in self.venues.items()
        }
        self._routines: dict[str, dict] = {}
        self._load_routines()
        self.watch: WatchJob | None = None
        self.last_grab: dict | None = None
        self.session = {"loggedIn": False, "banner": ""}
        self.ready = threading.Event()
        self.startup_error = ""
        self._relogin_needed = False
        self._hold_until = 0.0  # a captcha is waiting for its code: keep the page on the login form
        self._prompted_at = 0.0
        self._payments: list[dict] = []  # placed orders we're watching until they're paid or expire
        self._bookings: list[dict] = BookingStore(self.account.id).load()
        self._logout_reason = "登入已失效"
        self._prompts = 0  # captchas sent this round without a reply
        self._gave_up = False
        self._checked_at = time.time()
        self._board_token = None  # newest timetable scan; an older one stops between courts
        self._board_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
        self._board_lock = threading.Lock()
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
                self._load_routines()
                self._apply_routines()
                self._restore_watch()
                self.ready.set()
                while not self._stop.is_set():
                    try:
                        job = self._queue.get(timeout=self._idle_timeout())
                    except queue.Empty:
                        self._tick(provider)
                        continue
                    job(provider)
                    self._tick(provider)
        except Exception as exc:
            self.startup_error = str(exc)
            self.ready.set()

    def _idle_timeout(self) -> float:
        """Wake in time for the first query and the next poll.

        The idle loop used to sleep a full second, so the first request could land up to a
        second late. If that request was also early, the site still looked closed and the
        next try waited another poll_sec.
        """
        wait = 1.0
        for job in self.snipes.values():
            if not job.active:
                continue
            remaining = job.next_action_in(datetime.now(job.rules.tz))
            if remaining is None:
                continue
            if remaining <= 0:
                return 0.05
            if remaining < 5:
                wait = min(wait, max(0.05, remaining))
        return wait

    def _snipe_near_release(self) -> bool:
        """Within prepare_lead of opens_at — snipe must keep ticking even during captcha hold."""
        for job in self.snipes.values():
            if not job.active or not job.spec:
                continue
            opens_at = datetime.fromisoformat(job.spec["opensAt"])
            now = datetime.now(job.rules.tz)
            if now >= opens_at - timedelta(seconds=job.rules.prepare_lead_sec):
                return True
        return False

    def _orders_blocked(self) -> bool:
        return bool(self.booking_blocked and self.booking_blocked())

    def _tick(self, provider: Provider) -> None:
        # Another process is polling the same bot, so its browsers are sniping too.
        # Stay quiet until that conflict clears instead of placing a second order.
        if self._orders_blocked():
            return
        self._apply_routines()
        active = [job for job in self.snipes.values() if job.active]
        if time.time() < self._hold_until and not (active and self._snipe_near_release()):
            return  # navigating now would throw away the captcha the user is typing
        for job in active:
            try:
                job.tick(provider)
            except Exception as exc:
                job.log.note(f"搶訂出錯：{exc}")
        # An armed snipe can sit for hours. Only the window around release owns the page,
        # the same rule as a manual timetable query (page_lock_reason).
        if not self.page_lock_reason():
            if self.watch and self.watch.running:
                try:
                    self.watch.tick(provider)
                except Exception as exc:
                    self.watch.log.note(f"盯場出錯：{exc}")
            if not active:  # the snipe checks its own login; keep-alive is for an idle account
                self._keepalive(provider)
        if self.watch and not self.watch.running:
            WatchStore(self.account.id).clear()
        self._check_payments(provider)
        self._check_refunds()
        self._maybe_prompt_relogin(provider)

    # ---- unpaid orders ---------------------------------------------------

    def _track_payment(self, result: dict) -> None:
        order = result.get("order") or {}
        if not order:
            return
        if not (order.get("luid") and order.get("lid")):
            # No online payment to watch. A booking that never asked for a card is already held.
            if not str(order.get("paymentUrl") or "").strip():
                self._schedule_refund(result)
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
                "booking": _booking_snapshot(result),
            }
        )

    def _check_payments(self, provider: Provider) -> None:
        """PAY_REMIND_SEC after an order: still on the unpaid list -> remind once. While the payment
        window is open, leaving that list means it was paid, so the refund reminder can be armed.
        After the site's deadline, report that it is off the list (paid at the last moment, or
        cancelled for not paying) and do not arm a refund reminder."""
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
                    self._note_paid(item)
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
                item.update(stage="watch", next=now + 60)
                continue
            if not unpaid:
                if now <= item["placed"] + item["timeout"]:
                    self._note_paid(item)
                else:
                    self._event("payment_closed", f"訂單 {item['no']} 已不在待付款清單")
                    self._alert(
                        f"訂單 {item['no']} 已結束",
                        f"{item['label']} 已不在待付款清單：有付款就已完成；沒付款的話已因逾時被取消。",
                    )
                self._payments.remove(item)
                continue
            if now > item["placed"] + item["timeout"] + 5 * 60:  # the site hasn't cleared it yet
                self._payments.remove(item)
            else:
                item["next"] = now + 60

    def _note_paid(self, item: dict) -> None:
        self._event("payment_done", f"訂單 {item['no']} 已不在待付款清單（已付款）")
        self._schedule_refund(item.get("booking") or {})

    def _schedule_refund(self, result: dict) -> None:
        """Remember a paid booking so the morning before the no-refund cutoff can ask once."""
        venue = self.venues.get(str(result.get("venue") or ""))
        if venue is None:
            return
        plan = plan_refund_reminder(venue, result)
        if not plan or plan.get("passed") or not plan.get("remindAt"):
            return
        if any(item.get("key") == plan["key"] for item in self._bookings):
            return
        self._bookings.append(
            {
                "key": plan["key"],
                "venue": venue.id,
                "title": plan["title"],
                "body": plan["body"],
                "deadline": plan["deadline"],
                "remindAt": plan["remindAt"],
                "reminded": False,
            }
        )
        self._save_bookings()

    def _check_refunds(self, now: datetime | None = None) -> None:
        """Send the cancel-or-keep reminder once, then drop the booking after the cutoff."""
        if not self._bookings:
            return
        kept: list[dict] = []
        changed = False
        for item in self._bookings:
            try:
                deadline = datetime.fromisoformat(item["deadline"])
                remind_at = datetime.fromisoformat(item["remindAt"])
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
            if deadline.tzinfo is None or remind_at.tzinfo is None:
                changed = True
                continue
            current = (now or datetime.now(deadline.tzinfo)).astimezone(deadline.tzinfo)
            if current >= deadline:
                changed = True
                continue
            if not item.get("reminded") and current >= remind_at:
                self._alert(item.get("title") or "要不要取消？", item.get("body") or "", level="info")
                self._event(
                    "refund_reminder",
                    item.get("title") or "退費期限",
                    venue=item.get("venue") or "",
                    data={"body": item.get("body") or ""},
                )
                item["reminded"] = True
                changed = True
            kept.append(item)
        if changed:
            self._bookings = kept
            self._save_bookings()

    def _save_bookings(self) -> None:
        BookingStore(self.account.id).save(self._bookings)

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
        """Let OCR submit up to OCR_SUBMIT_LIMIT times. A person is asked only after that many
        rejections, or when both reads are too uncertain to submit. A rejected image is reloaded
        in place before the next try. After MAX_PROMPTS unanswered captchas, stop until /login."""
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
        try:
            username, password = self.account.credentials()
            image = provider.captcha(username, password)
        except Exception as exc:
            # Not sent, so it doesn't use up a try; retry in a minute (e.g. a network blip at startup).
            self._prompted_at = time.time() - REPROMPT_SEC + CAPTCHA_RETRY_SEC
            self._event("error", f"取驗證碼失敗：{exc}", level="warn")
            return
        handed = self._ocr_then_maybe_hand_off(provider, username, password, image)
        if handed is None:
            return
        image, fresh = handed
        self._prompts += 1
        self._hold_until = time.time() + LOGIN_HOLD_SEC
        self._event("relogin_prompt", f"已透過 Telegram 傳送驗證碼（{self._logout_reason}）")
        self.bot.ask_code_async(self.account.id, image, self._captcha_caption(fresh=fresh))

    def _ocr_then_maybe_hand_off(
        self, provider: Provider, username: str, password: str, image: bytes
    ) -> tuple[bytes, bool] | None:
        """None when OCR logged in. Otherwise the image to show a person, and whether a submit was rejected."""
        rejected = False
        for attempt in range(1, OCR_SUBMIT_LIMIT + 1):
            code = read_captcha(image)
            if code:
                result = provider.login(code)
                if result.get("ok"):
                    self._note_login_success(provider, f"驗證碼已自動辨識並登入（第 {attempt} 次）")
                    return None
                rejected = True
                self._event(
                    "login_failed",
                    f"自動辨識第 {attempt} 次被拒絕：{result.get('message', '')}",
                    level="warn",
                )
            if attempt == OCR_SUBMIT_LIMIT and not (rejected and code):
                return image, rejected
            try:
                image = provider.captcha(username, password)
            except Exception as exc:
                self._prompted_at = time.time() - REPROMPT_SEC + CAPTCHA_RETRY_SEC
                self._event("error", f"更新驗證碼失敗：{exc}", level="warn")
                return None
        return image, True

    def _captcha_caption(self, *, fresh: bool = False) -> str:
        ask = "自動辨識未通過，請回覆這張新的驗證碼" if fresh else "請回覆這張圖上的驗證碼"
        return (
            f"{self._login_prefix()} {self.account.display} {self._logout_reason}，{ask}"
            f"（第 {self._prompts}/{MAX_PROMPTS} 次）"
        )

    def _login_prefix(self) -> str:
        """Prefer the bot's name so auto prompts and /login use the same tag."""
        instance = getattr(self.bot, "instance", "") if self.bot else ""
        if instance:
            return login_prefix(self.account.id, instance)
        return login_prefix(self.account.id)

    def _paused_text(self) -> str:
        return (
            f"{self._login_prefix()} {self.account.display} 已傳 {MAX_PROMPTS} 次驗證碼都沒有回覆，先停止提醒。\n"
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

    def _alert(self, title: str, body: str, *, level: str | None = None) -> None:
        # The pre-release confirmation is the all-clear. A refund reminder is a question, not a problem.
        if level is None:
            level = "info" if title.endswith("開搶前確認") else "warn"
        self._event("alert", title, level=level, data={"body": body})
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

    def captcha_image(self, *, notify: bool = True) -> bytes:
        username, password = self.account.credentials()

        def run(provider: Provider) -> bytes:
            image = provider.captcha(username, password)
            if self._relogin_needed or not self.session["loggedIn"]:  # never pause a snipe that is still logged in
                self._hold_until = time.time() + LOGIN_HOLD_SEC
                # this captcha (from /login or the web page) is attempt 1 of a new round
                self._restart_prompts()
                self._prompts = 1
                self._prompted_at = time.time()
                # /login sends its own caption; notifying here as well would post two photos.
                if notify and self.bot:
                    self._event("relogin_prompt", "已透過 Telegram 傳送驗證碼（網頁顯示驗證碼）")
                    self.bot.ask_code_async(self.account.id, image, self._captcha_caption())
            return image

        return self.call(run)

    def _note_login_success(self, provider: Provider, message: str) -> None:
        self._hold_until = 0
        self._clear_relogin_state()
        self.session = provider.refresh_session()
        if not self.session["loggedIn"]:
            self.session = provider.refresh_session()
        self._event("login", message)

    def login(self, code: str) -> dict:
        def run(provider: Provider) -> dict:
            self._hold_until = 0
            self._restart_prompts()
            result = provider.login(code)
            if result.get("ok"):
                # Trust the login API first; refresh_session can briefly lag right after submit.
                self._note_login_success(provider, "登入成功")
            else:
                self._event("login_failed", f"登入失敗：{result.get('message', '')}", level="warn")
            return result

        result = self.call(run)
        return {**result, **self.session_info()}

    # ---- the booking page ------------------------------------------------

    def page_lock_reason(self) -> str:
        """While a snipe is about to fire, or is firing, other work must not navigate the page."""
        for job in self.snipes.values():
            spec = job.spec
            if not spec or not job.active:
                continue
            phase = spec.get("phase")
            near = False
            try:
                opens_at = datetime.fromisoformat(spec["opensAt"])
                now = datetime.now(job.rules.tz)
                near = now >= opens_at - timedelta(seconds=job.rules.prepare_lead_sec)
            except (KeyError, TypeError, ValueError):
                near = False
            if phase in _BUSY_SNIPE_PHASES or near:
                when = spec.get("targetDate") or ""
                return f"正在搶 {when}，開搶前後暫停查表和手動訂場，以免把預約頁帶走"
        return ""

    def _ensure_page_free(self) -> None:
        reason = self.page_lock_reason()
        if reason:
            raise ProviderError(reason)

    def _restore_watch(self) -> None:
        saved = WatchStore(self.account.id).load()
        if not saved:
            return
        venue = self.venues.get(saved.get("venue") or "")
        if venue is None:
            WatchStore(self.account.id).clear()
            return
        try:
            self.watch = self._new_watch(venue, saved)
        except ValueError as exc:
            self._event("error", f"無法恢復盯場：{exc}", level="warn")
            WatchStore(self.account.id).clear()

    def _new_watch(self, venue: Venue, spec: dict) -> WatchJob:
        return WatchJob(
            venue,
            spec,
            self._record_grab,
            self._alert,
            self.events,
            on_logout=self._logged_out,
            account_id=self.account.id,
            on_change=self._persist_watch,
        )

    def _persist_watch(self, spec: dict) -> None:
        WatchStore(self.account.id).save(spec)

    def _adopt_watch(self, venue: Venue, spec: dict) -> bool:
        """Start a watch for slots a snipe missed. A watch the user already started is left alone."""
        if self.watch and self.watch.running:
            return False
        saved = {key: value for key, value in spec.items() if value is not None}
        saved["venue"] = venue.id
        self.watch = self._new_watch(venue, saved)
        self._persist_watch(saved)
        return True

    # ---- browsing and booking -----------------------------------------

    def board(self, venue: Venue, category: str, query_date: str, *, fresh: bool = False) -> dict:
        if not fresh:
            cached = self._lookup_board(venue.id, category, query_date)
            if cached is not None:
                return _board_payload(venue.id, category, cached[1], query_date)

        def run(provider: Provider) -> dict:
            self._ensure_page_free()
            stopped = False

            def stop() -> bool:
                nonlocal stopped
                if self.page_lock_reason():
                    stopped = True
                    return True
                return False

            events = list(self._scan_board(provider, venue, category, query_date, stop=stop))
            if stopped:
                raise ProviderError(self.page_lock_reason() or "開搶前後暫停查表")
            return _board_payload(venue.id, category, events, query_date)

        return self.call(run, timeout=90)

    def stream_board(self, venue: Venue, category: str, query_date: str, *, fresh: bool = False):
        """Start a timetable scan on the browser thread. Returns ``(queue, token)``.

        The queue receives iter_board events, then ``{"type": "done"}`` or
        ``{"type": "error", "error"}``, then None. A newer scan replaces the token and
        the previous scan stops between courts.

        A finished scan is reused for ``BOARD_CACHE_SEC`` seconds. A hit replays those
        events, prefixed with ``{"type": "cached", "ageMs"}``, and does not use the
        browser. ``fresh=True`` is the page's reload button: scan again and replace the cache.
        """
        if not fresh:
            cached = self._lookup_board(venue.id, category, query_date)
            if cached is not None:
                return self._replay_board(*cached)
        if self.startup_error:
            raise RuntimeError(f"帳號 {self.account.display} 的瀏覽器無法使用：{self.startup_error}")
        if self._stop.is_set():
            raise RuntimeError("服務正在關閉")
        events: queue.Queue = queue.Queue()
        token = object()
        self._board_token = token

        def run(provider: Provider) -> None:
            locked = False

            def stop() -> bool:
                nonlocal locked
                if self._board_token is not token:
                    return True
                if self.page_lock_reason():
                    locked = True
                    return True
                return False

            try:
                for event in self._scan_board(provider, venue, category, query_date, stop=stop):
                    events.put(event)
                if locked:
                    events.put({"type": "error", "error": self.page_lock_reason() or "開搶前後暫停查表"})
                else:
                    events.put({"type": "done"})
            except Exception as exc:
                events.put({"type": "error", "error": str(exc)})
            finally:
                events.put(None)

        self._queue.put(run)
        return events, token

    def _scan_board(self, provider: Provider, venue: Venue, category: str, query_date: str, stop=None):
        """Yield one scan, then remember it when every court came back."""
        collected: list[dict] = []
        for event in iter_board(provider, venue, category, query_date, stop=stop):
            collected.append(event)
            yield event
        if _board_complete(collected):
            self._remember_board(venue.id, category, query_date, collected)

    def _remember_board(self, venue_id: str, category: str, query_date: str, events: list[dict]) -> None:
        now = time.monotonic()
        dates = {query_date}
        # The page's first load asks with a blank date; the venue answers with the real day.
        # A specific date that the venue rewrites is not that day's timetable, so keep it separate.
        if not query_date:
            for event in events:
                if event.get("type") != "court":
                    continue
                resolved = str((event.get("window") or {}).get("date") or "")
                if resolved:
                    dates.add(resolved)
        snapshot = list(events)
        with self._board_lock:
            expired = [key for key, (stored_at, _events) in self._board_cache.items() if now - stored_at >= BOARD_CACHE_SEC]
            for key in expired:
                del self._board_cache[key]
            for day in dates:
                self._board_cache[(venue_id, category, day)] = (now, snapshot)

    def _lookup_board(self, venue_id: str, category: str, query_date: str):
        key = (venue_id, category, query_date)
        now = time.monotonic()
        with self._board_lock:
            hit = self._board_cache.get(key)
            if hit is None:
                return None
            stored_at, events = hit
            if now - stored_at >= BOARD_CACHE_SEC:
                del self._board_cache[key]
                return None
            return stored_at, copy.deepcopy(events)

    def _replay_board(self, stored_at: float, events: list[dict]):
        queued: queue.Queue = queue.Queue()
        queued.put({"type": "cached", "ageMs": max(0, int((time.monotonic() - stored_at) * 1000))})
        for event in events:
            queued.put(event)
        queued.put({"type": "done"})
        queued.put(None)
        return queued, object()

    def cancel_board(self, token) -> None:
        """Stop a scan if it is still the active one (the client went away or asked again)."""
        if self._board_token is token:
            self._board_token = None

    def courts(self, venue: Venue, category: str) -> dict:
        def run(provider: Provider) -> dict:
            self._ensure_page_free()
            courts = provider.list_courts(venue, category)
            return {"venue": venue.id, "category": category, "courts": [court_public(court) for court in courts]}

        return self.call(run, timeout=60)

    def grab(self, venue: Venue, category: str, court_id: str, query_date: str, slot: Slot) -> dict:
        if self._orders_blocked():
            raise RuntimeError(ORDERS_BLOCKED)

        def run(provider: Provider) -> dict:
            self._ensure_page_free()
            _start, _end, hours = slot_bounds(slot.time)
            held = provider.cached_holds(venue.id, category, query_date)
            if held is None:
                held = provider.my_bookings(venue, category, query_date)
            self._ensure_page_free()
            reason = quota_reason(venue.rules, held, court_id, hours)
            if reason:
                raise ProviderError(reason)
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
        def run(provider: Provider) -> dict:
            self._ensure_page_free()
            return public_snapshot(provider.press(label))

        return self.call(run, timeout=60)

    # ---- jobs (mutated on the browser thread so ticks never see half-updated state) ----

    def set_snipe(self, venue: Venue, category: str | None, targets: list | None) -> dict:
        job = self.snipes[venue.id]
        category = category or venue.categories[0]
        if targets is None:
            def run(_provider: Provider) -> dict:
                self._remember_routine_skip(venue, job)
                return job.cancel()

            return self.call(run)

        def run(provider: Provider) -> dict:
            courts: list[tuple[str, str]] = []
            # Court names are only needed when another account is already waiting on this wave.
            release = next_release(venue.rules)
            waiting = self.coord is not None and self.coord.has_peers(
                venue.id, category, release["targetDate"], self.account.id
            )
            if waiting:
                try:
                    courts = [(court.id, court.name) for court in provider.list_courts(venue, category)]
                except Exception:
                    courts = []
            return job.arm(category, targets, courts)

        return self.call(run)

    def set_routine(self, venue: Venue, routine: dict | None) -> dict:
        """Save a weekly pattern and arm the upcoming release when it matches.

        `routine` is None to turn the pattern off. Saving again clears a skipped wave,
        so the next matching release is armed immediately.
        """

        def run(_provider: Provider) -> dict:
            store = RoutineStore(venue.id, self.account.id)
            job = self.snipes[venue.id]
            if routine is None:
                self._routines.pop(venue.id, None)
                store.clear()
                if job.spec and job.spec.get("source") == "routine" and job.spec.get("phase") in {"armed", "need_login"}:
                    job.cancel("已停用每週固定，這一波自動搶訂已取消")
            else:
                category = routine.get("category") or venue.categories[0]
                if category not in venue.categories:
                    raise ValueError(f"{venue.display_name} 沒有 {category}")
                saved = build_routine(venue.rules, category, routine.get("weekdays") or [], routine.get("targets") or [])
                self._routines[venue.id] = saved
                store.save(saved)
                apply_saved_routine(job, saved)
            return self.routine_state(venue)

        return self.call(run)

    def routine_state(self, venue: Venue) -> dict:
        routine = self._routines.get(venue.id)
        release = next_release(venue.rules)
        state = {
            "venue": venue.id,
            "account": self.account.id,
            "enabled": False,
            "category": "",
            "weekdays": [],
            "targets": [],
            "skipDate": "",
            "label": "",
            "due": False,
            "release": release,
        }
        if not routine:
            return state
        target = release["targetDate"]
        try:
            weekday = date.fromisoformat(target).weekday()
        except ValueError:
            weekday = -1
        state.update(
            enabled=True,
            category=routine.get("category") or "",
            weekdays=list(routine.get("weekdays") or []),
            targets=list(routine.get("targets") or []),
            skipDate=routine.get("skipDate") or "",
            label=describe_routine(routine),
            due=weekday in set(routine.get("weekdays") or []) and (routine.get("skipDate") or "") != target,
        )
        return state

    def _load_routines(self) -> None:
        loaded: dict[str, dict] = {}
        for venue_id in self.venues:
            saved = RoutineStore(venue_id, self.account.id).load()
            if saved:
                loaded[venue_id] = saved
        self._routines = loaded

    def _apply_routines(self, now: datetime | None = None) -> None:
        for venue_id, job in self.snipes.items():
            apply_saved_routine(job, self._routines.get(venue_id), now)

    def _remember_routine_skip(self, venue: Venue, job: SnipeJob) -> None:
        updated = skip_wave(self._routines.get(venue.id), job.spec)
        if not updated:
            return
        self._routines[venue.id] = updated
        RoutineStore(venue.id, self.account.id).save(updated)

    def set_watch(self, venue: Venue | None, spec: dict | None) -> dict:
        def run(_provider: Provider) -> dict:
            store = WatchStore(self.account.id)
            if venue is None or spec is None:
                if self.watch:
                    self.watch.running = False
                    self.watch.log.note("已停止盯場")
                store.clear()
            else:
                saved = {key: value for key, value in spec.items() if value is not None}
                saved["venue"] = venue.id
                self.watch = self._new_watch(venue, saved)
                store.save(saved)
            return self.watch_state()

        return self.call(run)

    def watch_state(self) -> dict:
        if not self.watch:
            return {"running": False, "spec": None, "log": []}
        return self.watch.state()
