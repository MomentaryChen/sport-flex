from __future__ import annotations

import threading
from datetime import datetime

from sportflex.core.accounts import load_accounts, load_env, pick_account
from sportflex.core.coord import Coordinator
from sportflex.core.events import EventStore
from sportflex.core.models import Slot, slot_bounds
from sportflex.core.notify import Notifier, default_notifier
from sportflex.core.rules import quota_reason
from sportflex.core.venue import Venue, get_venue, load_venues
from sportflex.core.identity import server_label
from sportflex.runtime.telegram_bot import TelegramBot
from sportflex.core.routine import describe_routine
from sportflex.runtime.telegram_cmd import (
    filter_courts,
    format_courts,
    format_grab,
    format_release,
    format_routine,
    format_runs,
    format_slots,
    format_snipe,
    format_watch,
    match_court,
)
from sportflex.runtime.worker import AccountWorker


def bot_from_env() -> TelegramBot | None:
    """Telegram re-login is on whenever the bot is configured; SPORT_FLEX_TELEGRAM_RELOGIN=0 turns it off."""
    env = load_env()
    token, chat_id = env.get("SPORT_FLEX_TELEGRAM_BOT_TOKEN"), env.get("SPORT_FLEX_TELEGRAM_CHAT_ID")
    if not token or not chat_id or env.get("SPORT_FLEX_TELEGRAM_RELOGIN") == "0":
        return None
    return TelegramBot(token, chat_id, instance=server_label())


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
        self.coord = Coordinator()
        self.workers = {
            account_id: AccountWorker(account, self.venues, self.notifier, self.events, self.bot, coord=self.coord)
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
            print(
                f"Telegram 已啟用，這台是 {server_label()}：登入失效會傳驗證碼（圖片會標這台名稱）",
                flush=True,
            )
        backups = [name for name in getattr(self.notifier, "channels", ()) if name != "Telegram"]
        if backups:
            print(
                f"備援通知已啟用：{'、'.join(backups)}。搶到、付款提醒和登出會一併送出；驗證碼仍只走 Telegram。",
                flush=True,
            )

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
            "instance": server_label(),
            "venues": [venue.public() for venue in self.venues.values()],
            "accounts": [
                {**worker.account.public(), "ready": not worker.startup_error, "error": worker.startup_error}
                for worker in self.workers.values()
            ],
        }

    def health(self) -> dict:
        accounts = [
            {"id": worker.account.id, "healthy": worker.healthy, "error": worker.startup_error}
            for worker in self.workers.values()
        ]
        return {"healthy": all(item["healthy"] for item in accounts), "accounts": accounts}

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
        lines = [f"這台：{server_label()}"]
        for worker in self.workers.values():
            state = "瀏覽器無法使用" if worker.startup_error else "已登入" if worker.session.get("loggedIn") else "未登入"
            armed = [_armed_label(job.state()) for job in worker.snipes.values() if job.active]
            extra = f"，搶訂 {'、'.join(armed)}" if armed else ""
            weekly = [describe_routine(item) for item in worker._routines.values() if item]
            if weekly:
                extra += "，" + "、".join(weekly)
            if worker.watch and worker.watch.running:
                spec = worker.watch.spec
                extra += f"，盯場 {spec.get('date', '')} {spec.get('timeFrom', '')}".rstrip()
            lines.append(f"{worker.account.id}（{worker.account.display}）：{state}{extra}")
        return "\n".join(lines) or "沒有帳號"

    def venues_public(self) -> list[dict]:
        return [{"id": venue.id, "name": venue.display_name} for venue in self.venues.values()]

    def categories(self, venue_id: str | None) -> list[str]:
        return list(self._venue(venue_id).categories)

    def today(self, venue_id: str | None):
        return datetime.now(self._venue(venue_id).rules.tz).date()

    def release_text(self, venue_id: str | None = None) -> str:
        return format_release(self._venue(venue_id))

    def courts_text(self, account_id: str | None, venue_id: str | None, category: str | None) -> str:
        venue, worker, category = self._target(account_id, venue_id, category)
        data = worker.courts(venue, category)
        return format_courts(venue.display_name, category, data["courts"])

    def slots_text(
        self,
        account_id: str | None,
        venue_id: str | None,
        category: str | None,
        query_date: str,
        time_from: str,
        time_to: str,
    ) -> str:
        venue, worker, category = self._target(account_id, venue_id, category)
        board = worker.board(venue, category, query_date)
        return format_slots(venue.display_name, board, time_from, time_to)

    def routine_text(self, account_id: str | None, venue_id: str | None) -> str:
        venue, worker, _category = self._target(account_id, venue_id, None)
        return format_routine(venue.display_name, worker.routine_state(venue))

    def set_routine_text(
        self, account_id: str | None, venue_id: str | None, category: str | None, spec: dict
    ) -> str:
        venue, worker, category = self._target(account_id, venue_id, category)
        resolved = self._resolve_targets(worker, venue, category, spec["targets"])
        state = worker.set_routine(
            venue, {"category": category, "weekdays": spec["weekdays"], "targets": resolved}
        )
        return format_routine(venue.display_name, state)

    def clear_routine_text(self, account_id: str | None, venue_id: str | None) -> str:
        venue, worker, _category = self._target(account_id, venue_id, None)
        return format_routine(venue.display_name, worker.set_routine(venue, None))

    def snipe_text(self, account_id: str | None, venue_id: str | None) -> str:
        venue, worker, _category = self._target(account_id, venue_id, None)
        return format_snipe(venue.display_name, worker.snipes[venue.id].state())

    def arm_text(self, account_id: str | None, venue_id: str | None, category: str | None, targets: list[dict]) -> str:
        venue, worker, category = self._target(account_id, venue_id, category)
        resolved = self._resolve_targets(worker, venue, category, targets)
        state = worker.set_snipe(venue, category, resolved)
        return format_snipe(venue.display_name, state)

    def watch_text(self, account_id: str | None, venue_id: str | None) -> str:
        venue, worker, _category = self._target(account_id, venue_id, None)
        return format_watch(self._watch_venue_name(worker, venue), worker.watch_state())

    def start_watch_text(self, account_id: str | None, venue_id: str | None, category: str | None, spec: dict) -> str:
        venue, worker, category = self._target(account_id, venue_id, category)
        court_id = ""
        court_name = ""
        if spec.get("court"):
            court = match_court(worker.courts(venue, category)["courts"], spec["court"])
            court_id, court_name = court["id"], court["name"]
        saved = {
            "category": category,
            "date": spec["date"],
            "timeFrom": spec.get("timeFrom") or "",
            "timeTo": spec.get("timeTo") or "",
            "courtId": court_id,
            "courtName": court_name,
        }
        state = worker.set_watch(venue, saved)
        return format_watch(venue.display_name, state)

    def stop_watch_text(self, account_id: str | None, venue_id: str | None) -> str:
        venue, worker, _category = self._target(account_id, venue_id, None)
        if not worker.watch or not worker.watch.running:
            return format_watch(venue.display_name, worker.watch_state())
        name = self._watch_venue_name(worker, venue)
        worker.set_watch(None, None)
        return f"已停止盯場 {name}"

    def _watch_venue_name(self, worker: AccountWorker, fallback: Venue) -> str:
        spec = worker.watch.spec if worker.watch else {}
        venue = self.venues.get(spec.get("venue") or "")
        return venue.display_name if venue else fallback.display_name

    def cancel_text(self, account_id: str | None, venue_id: str | None) -> str:
        venue, worker, _category = self._target(account_id, venue_id, None)
        job = worker.snipes[venue.id]
        if not job.spec:
            return format_snipe(venue.display_name, job.state())
        names = "、".join(f"{item.get('name') or '任一場'} {item.get('time')}" for item in job.spec.get("targets") or [])
        worker.set_snipe(venue, None, None)
        return f"已取消搶訂 {venue.display_name}" + (f"：{names}" if names else "")

    def history_text(self, account_id: str | None = None) -> str:
        if account_id and account_id not in self.workers:
            raise ValueError("找不到帳號 " + account_id)
        runs = self.events.snipe_runs(account=account_id or "", days=14, limit=8)
        stats = self.events.snipe_stats(account=account_id or "", days=14)
        return format_runs(runs, stats)

    def preview_book(
        self,
        account_id: str | None,
        venue_id: str | None,
        category: str | None,
        court_query: str,
        query_date: str,
        when: str,
    ) -> dict:
        venue, worker, category = self._target(account_id, venue_id, category)
        board = worker.board(venue, category, query_date)
        window = board.get("window") or {}
        got = window.get("date") or ""
        if got and got != query_date:
            raise ValueError(f"{query_date} 不在可預約範圍（{window.get('start') or '?'}～{window.get('end') or '?'}）")
        courts = filter_courts(board.get("courts") or [], court_query)
        if court_query and not courts:
            raise ValueError(f"找不到球場「{court_query}」")
        opened = [
            (court, raw)
            for court in courts
            for raw in court.get("slots") or []
            if raw.get("bookable") and str(raw.get("time") or "").split("-", 1)[0].strip() == when
        ]
        if not opened:
            where = f"{query_date} {when}" + (f" {court_query}" if court_query else "")
            raise ValueError(f"{where} 目前不能預約")
        if len(opened) > 1:
            names = "、".join(court["name"] for court, _raw in opened)
            sample = opened[0][0]["name"]
            short = query_date[5:].replace("-", "/")
            raise ValueError(f"{when} 有多面可訂：{names}\n請指定場地，例如 /book {short} {when} {sample}")
        court, raw = opened[0]
        _start, _end, hours = slot_bounds(raw.get("time") or "")
        held = [
            {"courtId": item["id"], "hours": slot_bounds(slot.get("time") or "")[2]}
            for item in board.get("courts") or []
            for slot in item.get("slots") or []
            if slot.get("mine")
        ]
        reason = quota_reason(venue.rules, held, court["id"], hours)
        if reason:
            raise ValueError(reason)
        price = f"  ${raw['price']}" if raw.get("price") else ""
        text = (
            f"即將用「{worker.account.display}」訂 {venue.display_name}\n"
            f"{court['name']}  {query_date}  {raw['time']}{price}\n"
            "會送出訂單（訂單成立），付款要自己完成。\n"
            "確認請傳 /ok ，放棄請傳 /no"
        )
        return {
            "text": text,
            "account": worker.account.id,
            "venue": venue.id,
            "category": category,
            "courtId": court["id"],
            "date": got or query_date,
            "slot": {
                "time": raw.get("time") or "",
                "bookable": bool(raw.get("bookable")),
                "status": raw.get("status") or "",
                "price": raw.get("price") or "",
                "raw": raw.get("raw") or {},
            },
        }

    def place_book(self, plan: dict) -> str:
        venue = self._venue(plan["venue"])
        worker = self.workers[plan["account"]]
        slot = plan["slot"]
        result = worker.grab(
            venue,
            plan["category"],
            plan["courtId"],
            plan["date"],
            Slot(
                time=slot["time"],
                bookable=bool(slot.get("bookable")),
                status=slot.get("status") or "",
                price=slot.get("price") or "",
                raw=slot.get("raw") or {},
            ),
        )
        return format_grab(result)

    def _venue(self, venue_id: str | None) -> Venue:
        if not venue_id:
            return next(iter(self.venues.values()))
        venue = self.venues.get(venue_id)
        if venue is None:
            raise ValueError("找不到場館 " + venue_id)
        return venue

    def _target(self, account_id: str | None, venue_id: str | None, category: str | None):
        venue = self._venue(venue_id)
        if category and category not in venue.categories:
            raise ValueError(f"{venue.display_name} 沒有「{category}」。可選：" + "、".join(venue.categories))
        return venue, self._worker(account_id, venue), category or venue.categories[0]

    def _worker(self, account_id: str | None, venue: Venue) -> AccountWorker:
        if account_id:
            worker = self.workers.get(account_id)
            if worker is None:
                raise ValueError("找不到帳號 " + account_id)
            if venue.id not in worker.snipes:
                raise ValueError(f"帳號 {account_id} 不能訂 {venue.display_name}")
            return worker
        matching = [worker for worker in self.workers.values() if venue.id in worker.snipes]
        if len(matching) == 1:
            return matching[0]
        if not matching:
            raise ValueError(f"沒有帳號可以訂 {venue.display_name}")
        raise ValueError("請在指令加上帳號：" + " | ".join(worker.account.id for worker in matching))

    def _resolve_targets(self, worker: AccountWorker, venue: Venue, category: str, targets: list[dict]) -> list[dict]:
        listed = worker.courts(venue, category)["courts"] if any(item.get("court") for item in targets) else []
        resolved = []
        for item in targets:
            query = item.get("court") or ""
            if not query:
                resolved.append({"courtId": "", "name": "任一場", "time": item["time"]})
                continue
            court = match_court(listed, query)
            resolved.append({"courtId": court["id"], "name": court["name"], "time": item["time"]})
        return resolved


    def captcha_for(self, account_id: str) -> bytes:
        return self.workers[account_id].captcha_image(notify=False)

    def login_with(self, account_id: str, code: str) -> dict:
        return self.workers[account_id].login(code)


def _armed_label(state: dict) -> str:
    targets = "、".join(f"{item.get('name') or '任一場'} {item.get('time')}" for item in state.get("targets") or [])
    label = (state.get("release") or {}).get("targetLabel") or ""
    return f"{label} {targets}".strip()
