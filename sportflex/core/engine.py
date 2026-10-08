from __future__ import annotations

import base64
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Callable

from sportflex.core.events import EventStore, NullEventStore
from sportflex.core.models import Court, Slot, slot_bounds, slot_matches
from sportflex.core.notify import Notifier
from sportflex.core.rules import next_release, slot_end, validate_targets
from sportflex.core.storage import SnipeStore
from sportflex.core.venue import Venue
from sportflex.providers.base import NeedLogin, Provider, Throttled

ACTIVE_PHASES = {"armed", "preparing", "ready", "firing", "need_login"}
RUN_TARGET_KEYS = ("courtId", "name", "time", "status", "bookedName", "seenSec", "bookedSec", "submitSec", "attempts")

Alert = Callable[[str, str], None]  # (title, body) pushed to the user


def _no_alert(title: str, body: str) -> None:
    pass


def _no_logout() -> None:
    pass


LOGIN_DROPPED_WHILE_FIRING = "搶訂中登入失效，請立刻在網頁上重新登入，系統會繼續搶到截止為止。"


Sink = Callable[[str, str, str, dict], None]  # (kind, message, level, data) -> event store


class JobLog:
    """The last lines shown on the page; every line also goes to the event store through `sink`."""

    def __init__(self, sink: Sink | None = None, keep: int = 30) -> None:
        self.lines: list[str] = []
        self.keep = keep
        self.sink = sink
        self._quiet_at = 0.0

    def note(self, text: str, kind: str = "note", level: str = "info", **data) -> None:
        self.lines.append(f"{time.strftime('%H:%M:%S')} {text}")
        self.lines = self.lines[-self.keep :]
        if self.sink:
            self.sink(kind, text, level, data)

    def quiet(self, text: str, every: float = 10, kind: str = "status") -> None:
        """Repeated status lines, at most once per `every` seconds."""
        if time.time() - self._quiet_at < every:
            return
        self._quiet_at = time.time()
        self.note(text, kind)


def _sink(events: EventStore, account: str, venue: str, job: str) -> Sink:
    def write(kind: str, message: str, level: str, data: dict) -> None:
        events.record(kind, message, account=account, venue=venue, job=job, level=level, data=data)

    return write


def build_board(provider: Provider, venue: Venue, category: str, query_date: str) -> dict:
    """Every court of a sport with its slots for one day."""
    courts = provider.list_courts(venue, category)
    groups = []
    window = {"start": "", "end": "", "date": query_date}
    for index, court in enumerate(courts):
        if index:
            time.sleep(0.35)
        availability = provider.availability(court, query_date)
        window = {
            "start": availability.window_start,
            "end": availability.window_end,
            "date": availability.query_date or query_date,
        }
        groups.append({**court_public(court), "slots": [asdict(slot) for slot in availability.slots]})
    return {"venue": venue.id, "category": category, "window": window, "courts": groups}


def court_public(court: Court) -> dict:
    return {"id": court.id, "name": court.name, "venue": court.venue, "minPrice": court.price}


def grab(provider: Provider, venue: Venue, court: Court, query_date: str, slot: Slot) -> dict:
    """Submit one booking and stop before payment."""
    snapshot = provider.submit(court, query_date, slot)
    return {
        "ok": True,
        "venue": venue.id,
        "venueName": venue.name,
        "court": court.name,
        "date": query_date,
        "time": slot.time,
        "price": slot.price,
        **public_snapshot(snapshot),
    }


def public_snapshot(snapshot: dict) -> dict:
    return {
        "url": snapshot["url"],
        "text": snapshot["text"],
        "buttons": snapshot["buttons"],
        "pressed": snapshot.get("pressed") or "",
        "image": base64.b64encode(snapshot["image"]).decode("ascii"),
    }


def announce_grab(notifier: Notifier, account_label: str, result: dict) -> None:
    notifier.send(
        f"搶到 {result['venueName']} {result['court']}",
        f"{result['date']} {result['time']} ${result['price']}\n"
        f"帳號：{account_label}\n已停在確認頁，請盡快自己完成付款。\n{result['url']}",
    )


class SnipeJob:
    """Book slots of a day that is not released yet, the moment the venue releases it.

    Phases: idle → armed → preparing → ready → firing → done (need_login when the session dropped).
    Runs on its account's browser thread via tick().
    """

    def __init__(
        self,
        venue: Venue,
        account_id: str,
        on_grab: Callable[[dict], None],
        on_alert: Alert = _no_alert,
        events: EventStore | None = None,
        on_logout: Callable[[], None] = _no_logout,
    ) -> None:
        self.venue = venue
        self.account_id = account_id
        self.store = SnipeStore(venue.id, account_id)
        self.on_grab = on_grab
        self.on_alert = on_alert
        self.on_logout = on_logout  # the worker starts a Telegram re-login
        self.events = events or NullEventStore()
        self.spec: dict | None = None
        self.log = JobLog(_sink(self.events, account_id, venue.id, "snipe"))
        self._prepared = False
        self._next_at = 0.0
        self._login_stage = 0  # 1 = checked after arming, 2 = checked login_check_lead_min before release
        self._alerted: set[str] = set()
        self._run: dict | None = None  # timings of the current wave, written to snipe_runs when it ends
        self._clock = (datetime.now(), 0.0)  # (the tick's `now`, monotonic at that tick) for _sec()

    @property
    def rules(self):
        return self.venue.rules

    @property
    def active(self) -> bool:
        return bool(self.spec) and self.spec["phase"] in ACTIVE_PHASES

    def arm(self, category: str, raw_targets: list) -> dict:
        if category not in self.venue.categories:
            raise ValueError(f"{self.venue.name} 沒有 {category}")
        targets = validate_targets(self.rules, raw_targets)
        release = next_release(self.rules)
        self.spec = {
            "category": category,
            "targetDate": release["targetDate"],
            "opensAt": release["opensAt"],
            "phase": "armed",
            "targets": targets,
            "deadline": (
                datetime.fromisoformat(release["opensAt"]) + timedelta(minutes=self.rules.snipe_deadline_min)
            ).isoformat(),
        }
        self._prepared = False
        self._next_at = 0
        self._run = None
        self._reset_alerts()
        self.store.save(self.spec)
        names = "、".join(f"{item['name']} {item['time']}" for item in targets)
        self.log.note(
            f"已設定 {release['targetLabel']} {names}，{release['opensLabel']} 開搶",
            "armed",
            targetDate=release["targetDate"],
            opensAt=release["opensAt"],
            targets=targets,
        )
        return self.state()

    def cancel(self) -> dict:
        self.spec = None
        self._prepared = False
        self._run = None
        self.store.clear()
        self.log.note("已取消搶訂", "cancelled")
        return self.state()

    def restore(self) -> None:
        saved = self.store.load()
        if not saved:
            return
        release = next_release(self.rules)
        if saved.get("targetDate") != release["targetDate"]:
            self.store.clear()
            return
        saved["phase"] = "armed"
        saved["opensAt"] = release["opensAt"]
        self.spec = saved
        self._reset_alerts()
        self.log.note("恢復尚未完成的搶訂設定", "restored", targetDate=saved["targetDate"])

    def state(self) -> dict:
        phase = self.spec["phase"] if self.spec else "idle"
        return {
            "venue": self.venue.id,
            "account": self.account_id,
            "running": phase in ACTIVE_PHASES,
            "phase": phase,
            "category": self.spec["category"] if self.spec else "",
            "release": next_release(self.rules),
            "targets": [
                {key: item.get(key) for key in ("courtId", "name", "time", "status", "bookedName")}
                for item in (self.spec["targets"] if self.spec else [])
            ],
            "log": self.log.lines[-12:],
        }

    def tick(self, provider: Provider, now: datetime | None = None) -> None:
        spec = self.spec
        if not spec or spec["phase"] not in ACTIVE_PHASES:
            return
        now = now or datetime.now(self.rules.tz)
        opens_at = datetime.fromisoformat(spec["opensAt"])
        if now < opens_at - timedelta(seconds=self.rules.prepare_lead_sec):
            self._check_login(provider, spec, now, opens_at)
            return
        if now < opens_at:
            if spec["phase"] == "need_login" and time.time() < self._next_at:
                return
            self._prepare(provider, spec)
            return
        if time.time() < self._next_at:
            return
        self._fire(provider, spec, now)

    def _check_login(self, provider: Provider, spec: dict, now: datetime, opens_at: datetime) -> None:
        """Check the session right after arming and again shortly before release, so a dropped
        login is noticed while there is still time to fix it. Logged out: re-check every minute."""
        stage = 2 if now >= opens_at - timedelta(minutes=self.rules.login_check_lead_min) else 1
        if spec["phase"] == "need_login":
            if time.time() < self._next_at:
                return
        elif self._login_stage >= stage:
            return
        self._login_stage = stage
        if provider.refresh_session()["loggedIn"]:
            if spec["phase"] == "need_login":
                spec["phase"] = "armed"
                self._alerted.discard("login")
                self.log.note("已重新登入，繼續等開搶", "login_ok")
            return
        spec["phase"] = "need_login"
        self._next_at = time.time() + 60
        self.log.note("登入已失效，請在畫面上輸入驗證碼登入", "login_dropped", "warn")
        self._need_login_alert(f"{self.rules.release_time} 開搶前請先在網頁上重新登入，不然搶不到。")

    def _prepare(self, provider: Provider, spec: dict) -> None:
        if self._prepared:
            return
        spec["phase"] = "preparing"
        if not provider.refresh_session()["loggedIn"]:
            spec["phase"] = "need_login"
            self._next_at = time.time() + 15
            self.log.note("開搶前需要登入，請在畫面上輸入驗證碼", "login_dropped", "warn")
            self._need_login_alert("快開搶了，請立刻在網頁上重新登入。")
            return
        courts = provider.list_courts(self.venue, spec["category"])
        if not courts:
            self._finish(f"{spec['category']} 沒有球場")
            return
        found = {court.id: court for court in courts}
        spec["courts"] = {court.id: asdict(court) for court in courts}
        for item in spec["targets"]:
            if not item["courtId"]:
                continue
            court = found.get(item["courtId"])
            if court is None:
                self._finish(f"找不到 {item['name']}")
                return
            item["name"] = court.name
        named = [item["courtId"] for item in spec["targets"] if item["courtId"]]
        first = found[named[0]] if named else courts[0]
        provider.prepare(first)
        self._prepared = True
        spec["phase"] = "ready"
        self.store.save(spec)
        self.log.note(f"已停在 {first.name} 的預約頁，等 {self.rules.release_time}", "ready", court=first.name)

    def _fire(self, provider: Provider, spec: dict, now: datetime) -> None:
        self._clock = (now, time.monotonic())
        if self._run is None:
            self._run = {"firedSec": self._sec(), "releasedSec": None, "firstBookedSec": None}
            self.log.note("開搶", "fired", sec=self._run["firedSec"])
        spec["phase"] = "firing"
        pending = [item for item in spec["targets"] if item.get("status") not in {"booked", "skipped"}]
        if not pending or now > datetime.fromisoformat(spec["deadline"]):
            self._finish("這波搶訂結束")
            return
        booked_any = False
        for item in pending:
            try:
                candidates = self._candidates(provider, spec, item)
            except Throttled as exc:
                self.log.note(f"{item['name']} 查詢太頻繁：{exc}", "throttled", "warn", sec=self._sec())
                self._throttled()
                return
            except NeedLogin as exc:
                self.log.note(f"{item['name']} 查詢失敗，登入已失效：{exc}", "login_dropped", "error", sec=self._sec())
                self._need_login_alert(LOGIN_DROPPED_WHILE_FIRING)
                continue
            except Exception as exc:
                self.log.note(f"{item['name']} 查詢失敗：{exc}", "error", "error", sec=self._sec())
                continue
            for court in candidates:
                outcome = self._attempt(provider, spec, item, court)
                if outcome == "throttled":
                    self._throttled()
                    return
                if outcome == "unreleased":
                    self.log.quiet(f"{spec['targetDate']} 還沒釋出")
                    self._next_at = time.time() + self.rules.poll_sec
                    return
                if outcome == "booked":
                    booked_any = True
                if outcome in {"booked", "skipped"}:
                    break
        if all(item.get("status") in {"booked", "skipped"} for item in spec["targets"]):
            self._finish("目標都處理完了")
            return
        self._next_at = time.time() + self.rules.poll_sec
        if not booked_any:
            self.log.quiet(f"尚無符合的空檔，繼續查 {spec['targetDate']}")

    def _candidates(self, provider: Provider, spec: dict, item: dict) -> list[Court]:
        """The named court, or for an any-court target every free court not claimed by another target."""
        if item["courtId"]:
            return [self._court(provider, spec, item["courtId"])]
        end = slot_end(item["time"], self.rules.slot_hours)
        try:
            free = provider.search(self.venue, spec["category"], spec["targetDate"], item["time"], end)
        except Throttled:
            raise
        except Exception as exc:
            # The search page can refuse (e.g. anonymous sessions); checking each court still works.
            self.log.quiet(f"時段篩選失敗（{exc}），改逐場查詢")
            free = list(spec.get("courts") or {})
        claimed = self._claimed(spec, item)
        return [self._court(provider, spec, court_id) for court_id in free if court_id not in claimed]

    def _claimed(self, spec: dict, item: dict) -> set[str]:
        if not self.rules.distinct_courts:
            return set()
        return {
            other.get("bookedCourtId") or other["courtId"]
            for other in spec["targets"]
            if other is not item and (other.get("bookedCourtId") or other["courtId"])
        }

    def _court(self, provider: Provider, spec: dict, court_id: str) -> Court:
        raw = (spec.get("courts") or {}).get(court_id)
        if raw:
            return Court(**raw)
        return provider.find_court(self.venue, spec["category"], court_id)

    def _attempt(self, provider: Provider, spec: dict, item: dict, court: Court) -> str:
        """Check one court and submit if its slot is open. Returns booked/skipped/miss/throttled/unreleased."""
        item["attempts"] = item.get("attempts", 0) + 1
        try:
            availability = provider.availability(court, spec["targetDate"])
        except Throttled as exc:
            self.log.note(f"{court.name} 查詢太頻繁：{exc}", "throttled", "warn", sec=self._sec())
            return "throttled"
        except NeedLogin as exc:
            self.log.note(f"{court.name} 查詢失敗，登入已失效：{exc}", "login_dropped", "error", sec=self._sec())
            self._need_login_alert(LOGIN_DROPPED_WHILE_FIRING)
            return "miss"
        except Exception as exc:
            self.log.note(f"{court.name} 查詢失敗：{exc}", "error", "error", sec=self._sec())
            return "miss"
        if availability.query_date != spec["targetDate"]:
            return "unreleased"
        if self._run is not None and self._run["releasedSec"] is None:
            self._run["releasedSec"] = self._sec()
            self.log.note(f"{spec['targetDate']} 已釋出", "released", sec=self._run["releasedSec"])
        match = _match_start(availability.slots, item["time"])
        if match is None:
            return "miss"
        slot, hours = match
        if hours != self.rules.slot_hours:
            if item["courtId"]:
                item["status"] = "skipped"
                self.log.note(f"{court.name} {slot.time} 不是 {self.rules.slot_hours:g} 小時，依規定不送出", "skipped")
                return "skipped"
            return "miss"
        item["seenSec"] = self._sec()
        self.log.note(
            f"看到 {court.name} {slot.time}，送出", "submit", court=court.name, time=slot.time, sec=item["seenSec"]
        )
        started = time.monotonic()
        try:
            result = grab(provider, self.venue, court, spec["targetDate"], slot)
        except NeedLogin as exc:
            self.log.note(f"{court.name} 送出失敗，登入已失效：{exc}", "login_dropped", "error", sec=self._sec())
            self._need_login_alert(LOGIN_DROPPED_WHILE_FIRING)
            return "miss"
        except Exception as exc:
            self.log.note(f"{court.name} 送出失敗：{exc}", "error", "error", sec=self._sec())
            return "miss"
        item["status"] = "booked"
        item["bookedCourtId"] = court.id
        item["bookedName"] = court.name
        item["submitSec"] = round(time.monotonic() - started, 3)
        item["bookedSec"] = self._sec()
        if self._run is not None and self._run["firstBookedSec"] is None:
            self._run["firstBookedSec"] = item["bookedSec"]
        self.store.save(spec)
        self.on_grab(result)
        self.log.note(
            f"{court.name} {slot.time} 已到確認頁（開搶後 {item['bookedSec']:.2f} 秒），刷卡請自己按",
            "booked",
            court=court.name,
            time=slot.time,
            price=slot.price,
            sec=item["bookedSec"],
            submitSec=item["submitSec"],
        )
        return "booked"

    def _finish(self, text: str) -> None:
        spec = self.spec
        if spec:
            spec["phase"] = "done"
            self._record_run(spec, text)
        else:
            self.log.note(text)
        self.store.clear()
        missed = [item for item in (spec or {}).get("targets", []) if item.get("status") != "booked"]
        if missed:
            names = "、".join(f"{item['name']} {item['time']}" for item in missed)
            self.on_alert(
                f"沒搶到 {self.venue.name} {spec['targetDate']}",
                f"{names}\n{text}。可以到網頁看執行紀錄，或改用盯場等人退訂。",
            )

    def _sec(self) -> float:
        """Seconds since release on the tick's clock (tests pass a fake `now`)."""
        if not self.spec:
            return 0.0
        tick_now, tick_mono = self._clock
        elapsed = time.monotonic() - tick_mono if tick_mono else 0.0
        opens_at = datetime.fromisoformat(self.spec["opensAt"])
        return round((tick_now - opens_at).total_seconds() + elapsed, 3)

    def _record_run(self, spec: dict, reason: str) -> None:
        """One snipe_runs row per wave: result plus seconds-after-release for each step."""
        run = self._run or {}
        targets = spec.get("targets", [])
        booked = sum(1 for item in targets if item.get("status") == "booked")
        result = "booked" if targets and booked == len(targets) else "partial" if booked else "missed"
        summary = {
            "account": self.account_id,
            "venue": self.venue.id,
            "targetDate": spec["targetDate"],
            "opensAt": spec["opensAt"],
            "result": result,
            "booked": booked,
            "total": len(targets),
            "firedSec": run.get("firedSec"),
            "releasedSec": run.get("releasedSec"),
            "firstBookedSec": run.get("firstBookedSec"),
            "finishedSec": self._sec() if self._run else None,
            "reason": reason,
            "targets": [{key: item.get(key) for key in RUN_TARGET_KEYS} for item in targets],
        }
        self._run = None
        self.events.record_snipe_run(summary)
        level = "info" if result == "booked" else "warn"
        self.log.note(f"{reason}：搶到 {booked}/{len(targets)}", "snipe_result", level, **summary)

    def _throttled(self) -> None:
        self._next_at = time.time() + self.rules.throttle_backoff_sec
        self._alert_once(
            "throttle",
            f"{self.venue.name} 回報操作太頻繁",
            f"搶 {self.spec['targetDate']} 時被網站擋下，每次暫停 {self.rules.throttle_backoff_sec:g} 秒後繼續重試。",
        )

    def _need_login_alert(self, body: str) -> None:
        self.on_logout()
        self._alert_once("login", f"{self.venue.name} 登入已失效", f"搶 {self.spec['targetDate']}：{body}")

    def _alert_once(self, kind: str, title: str, body: str) -> None:
        if kind in self._alerted:
            return
        self._alerted.add(kind)
        self.on_alert(title, body)

    def _reset_alerts(self) -> None:
        self._login_stage = 0
        self._alerted.clear()


class WatchJob:
    """Poll an already-released day and grab the first open slot that matches."""

    def __init__(
        self,
        venue: Venue,
        spec: dict,
        on_grab: Callable[[dict], None],
        on_alert: Alert = _no_alert,
        events: EventStore | None = None,
        on_logout: Callable[[], None] = _no_logout,
        account_id: str = "",
    ) -> None:
        if spec.get("category") not in venue.categories:
            raise ValueError(f"{venue.name} 沒有 {spec.get('category')}")
        self.venue = venue
        self.spec = spec
        self.on_grab = on_grab
        self.on_alert = on_alert
        self.on_logout = on_logout  # the worker starts a Telegram re-login
        self._alerted: set[str] = set()
        self.log = JobLog(_sink(events or NullEventStore(), account_id, venue.id, "watch"))
        self.running = True
        self._next_at = 0.0
        rules = venue.rules
        self.interval = max(rules.watch_min_interval_sec, int(spec.get("interval") or rules.watch_interval_sec))
        self.log.note(
            f"開始盯 {venue.name} {spec['category']} {spec.get('date')} "
            f"{spec.get('timeFrom') or ''}-{spec.get('timeTo') or ''}",
            "watch_started",
            spec=spec,
        )

    def state(self) -> dict:
        return {"running": self.running, "venue": self.venue.id, "spec": self.spec, "log": self.log.lines[-12:]}

    def tick(self, provider: Provider) -> None:
        if not self.running or time.time() < self._next_at:
            return
        self._next_at = time.time() + self.interval
        spec = self.spec
        try:
            board = build_board(provider, self.venue, spec["category"], spec["date"])
        except Throttled as exc:
            self.log.note(f"盯場查詢太頻繁：{exc}", "throttled", "warn")
            self._next_at += self.venue.rules.throttle_backoff_sec
            self._alert_once("throttle", f"{self.venue.name} 回報操作太頻繁", f"盯場 {spec['date']} 被網站擋下，會放慢後繼續。")
            return
        except NeedLogin as exc:
            self.log.note(f"盯場查詢失敗，登入已失效：{exc}", "login_dropped", "warn")
            self.on_logout()
            self._alert_once("login", f"{self.venue.name} 登入已失效", f"盯場 {spec['date']}：請在網頁上重新登入，盯場會繼續。")
            return
        except Exception as exc:
            self.log.note(f"盯場查詢失敗：{exc}", "error", "error")
            return
        self._alerted.discard("login")
        match = _first_open(board["courts"], spec)
        if not match:
            self.log.note(f"{spec['date']} 還沒有符合的空檔", "status")
            return
        court_id, slot = match
        self.log.note(f"看到空檔 {slot.time}，開始搶", "submit", court=court_id, time=slot.time)
        try:
            court = provider.find_court(self.venue, spec["category"], court_id)
            result = grab(provider, self.venue, court, board["window"]["date"] or spec["date"], slot)
        except NeedLogin as exc:
            self.log.note(f"搶位失敗，登入已失效：{exc}", "login_dropped", "error")
            self.on_logout()
            self._alert_once("login", f"{self.venue.name} 登入已失效", f"盯場看到 {slot.time} 有空，但登入失效送不出去，請重新登入。")
            return
        except Exception as exc:
            self.log.note(f"搶位失敗：{exc}", "error", "error")
            return
        self.running = False
        self.log.note(f"已送到 {result['url']}", "booked", court=result["court"], time=result["time"])
        self.on_grab(result)

    def _alert_once(self, kind: str, title: str, body: str) -> None:
        if kind in self._alerted:
            return
        self._alerted.add(kind)
        self.on_alert(title, body)


def _match_start(slots: list[Slot], wanted: str):
    for slot in slots:
        start, _end, hours = slot_bounds(slot.time)
        if start != wanted:
            continue
        if not slot.bookable:
            return None
        return slot, hours
    return None


def _first_open(courts: list[dict], spec: dict):
    court_id = spec.get("courtId") or ""
    for court in courts:
        if court_id and court["id"] != court_id:
            continue
        for raw in court["slots"]:
            slot = Slot(**raw)
            if not slot.bookable:
                continue
            if not slot_matches(slot, spec.get("timeFrom") or None, spec.get("timeTo") or None):
                continue
            return court["id"], slot
    return None
