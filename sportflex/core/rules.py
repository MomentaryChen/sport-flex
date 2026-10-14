from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sportflex.core.models import slot_bounds

WEEKDAYS = "一二三四五六日"


class BookingRules(BaseModel):
    """Per-venue booking policy. Every number the engine used to hard-code lives here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = "Asia/Taipei"
    window_days: int = Field(14, ge=1)  # bookable days counting today as day 1
    release_time: str = "06:00"  # when the next day of the window opens
    slot_hours: float = Field(1, gt=0)  # length of one booking
    align_minutes: int = Field(60, ge=1)  # a booking must start on this boundary
    max_hours_per_day: float = 2  # per account, per venue, per day
    distinct_courts: bool = True  # several bookings on one day must be different courts
    snipe_deadline_min: int = 3  # keep trying this long after release
    prepare_lead_sec: int = 60  # park on the reserve page this early
    login_check_lead_min: int = 10  # re-check login this long before release; still in -> ready notice, dropped -> alert
    login_recheck_min: int = 30  # while armed (before the lead window), re-check login this often
    poll_sec: float = 2.5  # pause between snipe rounds (each round checks at most one court)
    api_pause_sec: float = 2.5  # minimum gap between Changjia availability/search API calls
    throttle_backoff_sec: float = 12  # pause after the site says we are too fast
    watch_interval_sec: int = 45
    watch_min_interval_sec: int = 30

    @field_validator("release_time")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        hour, minute = [int(part) for part in value.split(":")]
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError("release_time 要是 HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def max_bookings_per_day(self) -> int:
        return int(self.max_hours_per_day // self.slot_hours)

    def describe(self) -> str:
        hours = _num(self.slot_hours)
        text = (
            f"每天 {self.release_time} 開放從當天算起的第 {self.window_days} 天。"
            f"每次 {hours} 小時，同一天最多 {_num(self.max_hours_per_day)} 小時"
        )
        if self.distinct_courts:
            text += "，須不同場"
        return text + "。"


def next_release(rules: BookingRules, now: datetime | None = None) -> dict:
    """When the next day of the booking window opens, and which day that is."""
    tz = rules.tz
    current = now.astimezone(tz) if now else datetime.now(tz)
    hour, minute = [int(part) for part in rules.release_time.split(":")]
    release_today = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    opens_at = release_today if current < release_today else release_today + timedelta(days=1)
    target = opens_at.date() + timedelta(days=rules.window_days - 1)
    open_end = target - timedelta(days=1)
    return {
        "targetDate": target.isoformat(),
        "targetLabel": f"{target.month}/{target.day}（週{WEEKDAYS[target.weekday()]}）",
        "opensAt": opens_at.isoformat(),
        "opensLabel": opens_at.strftime("%m/%d %H:%M"),
        "openEnd": open_end.isoformat(),
    }


ANY_COURT = "任一場"


def validate_targets(rules: BookingRules, raw: list) -> list[dict]:
    """Normalise snipe targets and reject anything the venue rules forbid.

    A target without courtId means any court that is free at that time.
    """
    targets = []
    seen = set()
    for item in raw:
        court_id = str(item.get("courtId") or "").strip()
        name = str(item.get("name") or court_id or ANY_COURT).strip()
        when = str(item.get("time") or "").strip()
        if not court_id and not when:
            continue
        if not when:
            raise ValueError("每個目標都要選開始時間")
        try:
            hour, minute = [int(part) for part in when.split(":")]
        except ValueError:
            raise ValueError(f"時間格式不對：{when}") from None
        if (hour * 60 + minute) % rules.align_minutes:
            raise ValueError(f"開始時間要對齊每 {rules.align_minutes} 分鐘")
        if court_id and rules.distinct_courts and court_id in seen:
            raise ValueError("同一天的目標必須是不同場")
        if court_id:
            seen.add(court_id)
        targets.append({"courtId": court_id, "name": name, "time": f"{hour:02d}:{minute:02d}", "status": "waiting"})
    limit = rules.max_bookings_per_day
    if not 1 <= len(targets) <= limit:
        raise ValueError(f"請選 1 到 {limit} 個目標，各 {_num(rules.slot_hours)} 小時")
    return targets


def quota_reason(rules: BookingRules, held: list[dict], court_id: str, hours: float) -> str | None:
    """Why this account must not book `hours` more on `court_id`.

    `held` is what the account already has that day: each item has `courtId` and `hours`.
    A snipe's own bookings belong in `held` too, so two targets plus an older order can't slip past.
    """
    used = sum(float(item.get("hours") or 0) for item in held)
    if used + hours > rules.max_hours_per_day + 1e-9:
        return f"同一天最多 {_num(rules.max_hours_per_day)} 小時，目前已有 {_num(used)} 小時"
    if rules.distinct_courts and court_id and any((item.get("courtId") or "") == court_id for item in held):
        return "同一天要不同場，這面已經有預約"
    return None


def holds_from_board(courts: list[dict]) -> list[dict]:
    """Slots on a timetable that this account already booked."""
    held = []
    for court in courts:
        for raw in court.get("slots") or []:
            if not raw.get("mine"):
                continue
            _start, _end, hours = slot_bounds(raw.get("time") or "")
            held.append(
                {
                    "courtId": court.get("id") or "",
                    "name": court.get("name") or "",
                    "time": raw.get("time") or "",
                    "hours": hours,
                }
            )
    return held


def slot_end(start: str, hours: float) -> str:
    hour, minute = [int(part) for part in start.split(":")]
    total = hour * 60 + minute + int(round(hours * 60))
    return f"{total // 60:02d}:{total % 60:02d}"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
