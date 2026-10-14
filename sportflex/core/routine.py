"""A weekly pattern that arms the next release on its own.

People were setting the same Tuesday and Thursday 20:00 snipe by hand every morning.
A saved routine does that before release: if the day about to open (window day 14)
falls on a chosen weekday, the snipe is armed with the saved targets.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from sportflex.core.rules import ANY_COURT, WEEKDAYS, BookingRules, next_release, validate_targets

# Phases where a snipe is waiting and can still be replaced by the routine.
WAITING_PHASES = {"armed", "need_login"}
# Phases where the browser is already on the booking flow; leave that wave alone.
LIVE_PHASES = {"preparing", "ready", "firing"}

_WEEKDAY_MARK = re.compile(r"[、,，/\s]")


def build_routine(rules: BookingRules, category: str, weekdays: list, targets: list) -> dict:
    """Validate a weekly pattern. `weekdays` is Python's Monday=0 .. Sunday=6."""
    days: list[int] = []
    for day in weekdays:
        try:
            number = int(day)
        except (TypeError, ValueError):
            raise ValueError("請選星期，例如週二、週四") from None
        if number not in range(7):
            raise ValueError("請選星期，例如週二、週四")
        if number not in days:
            days.append(number)
    days.sort()
    if not days:
        raise ValueError("請至少選一個星期，例如週二、週四")
    cleaned = validate_targets(rules, targets)
    return {
        "category": category,
        "weekdays": days,
        "targets": [
            {"courtId": item["courtId"], "name": item["name"], "time": item["time"]} for item in cleaned
        ],
        "skipDate": "",
    }


def describe_routine(routine: dict) -> str:
    """「每週二、四 20:00 任一場」."""
    days = "、".join(WEEKDAYS[day] for day in routine.get("weekdays") or [])
    targets = routine.get("targets") or []
    any_only = bool(targets) and all(_is_any(item) for item in targets)
    if any_only:
        slots = "、".join(item.get("time") or "" for item in targets) + " 任一場"
    else:
        slots = "、".join(
            f"{item.get('name') or ANY_COURT} {item.get('time') or ''}".strip() for item in targets
        )
    return f"每週{days} {slots}".strip()


def weekday_token(token: str) -> list[int] | None:
    """`二四`, `週二`, `星期二、四` → weekday numbers. None when it is not a weekday list."""
    text = token.strip().replace("星期", "").replace("週", "").replace("周", "")
    text = _WEEKDAY_MARK.sub("", text)
    if not text or any(ch not in WEEKDAYS for ch in text):
        return None
    return [WEEKDAYS.index(ch) for ch in text]


def plan_routine(spec: dict | None, routine: dict | None, rules: BookingRules, now: datetime | None = None) -> dict:
    """What to do with the current snipe so the weekly pattern is armed before release.

    `arm` sets the saved targets on the release that is about to open.
    `drop` cancels a routine wave that no longer matches (still only waiting).
    `hold` leaves a manual wave, a live wave, a finished wave, or a skipped date alone.
    """
    if not routine:
        return {"action": "hold"}
    release = next_release(rules, now)
    target = release["targetDate"]
    phase = (spec or {}).get("phase") or "idle"
    if phase in LIVE_PHASES:
        return {"action": "hold"}
    wanted = _wanted(routine, target)
    same_wave = bool(spec) and spec.get("targetDate") == target
    if phase in WAITING_PHASES and same_wave:
        if (spec or {}).get("source") != "routine":
            return {"action": "hold"}
        if not wanted:
            return {"action": "drop", "targetDate": target}
        if _same_request(spec, routine):
            return {"action": "hold"}
        return _arm(routine, target)
    if phase in WAITING_PHASES:
        return {"action": "hold"}
    if phase == "done" and same_wave:
        return {"action": "hold"}
    if not wanted:
        return {"action": "hold"}
    return _arm(routine, target)


def apply_saved_routine(job, routine: dict | None, now: datetime | None = None) -> str:
    """Arm, replace, or drop `job` to match `routine`. Returns the action taken."""
    decision = plan_routine(job.spec, routine, job.rules, now)
    action = decision["action"]
    try:
        if action == "arm":
            job.arm(decision["category"], decision["targets"], source="routine", now=now)
        elif action == "drop":
            job.cancel("這一波不在每週時段裡，已取消自動排程")
    except ValueError as exc:
        message = f"每週時段無法排上：{exc}"
        if not job.log.lines or message not in job.log.lines[-1]:
            job.log.note(message, "error", "error")
        return "error"
    return action


def skip_wave(routine: dict | None, spec: dict | None) -> dict | None:
    """Remember that the user cancelled this routine wave, so the next tick does not arm it again.

    A manual snipe is not skipped: cancelling it lets the weekly pattern take the wave.
    Returns the updated routine, or None when nothing should be stored.
    """
    if not routine or not spec or spec.get("source") != "routine":
        return None
    target = spec.get("targetDate") or ""
    if not target:
        return None
    updated = dict(routine)
    updated["skipDate"] = target
    return updated


def _arm(routine: dict, target: str) -> dict:
    return {
        "action": "arm",
        "category": routine.get("category") or "",
        "targets": [dict(item) for item in routine.get("targets") or []],
        "targetDate": target,
    }


def _wanted(routine: dict, target: str) -> bool:
    if (routine.get("skipDate") or "") == target:
        return False
    try:
        weekday = date.fromisoformat(target).weekday()
    except ValueError:
        return False
    return weekday in set(_weekday_numbers(routine))


def _weekday_numbers(routine: dict) -> list[int]:
    numbers = []
    for day in routine.get("weekdays") or []:
        try:
            number = int(day)
        except (TypeError, ValueError):
            continue
        if number in range(7):
            numbers.append(number)
    return numbers


def _same_request(spec: dict | None, routine: dict) -> bool:
    if not spec or spec.get("category") != routine.get("category"):
        return False
    return [_target_key(item) for item in spec.get("targets") or []] == [
        _target_key(item) for item in routine.get("targets") or []
    ]


def _target_key(item: dict) -> tuple[str, str, str]:
    return (item.get("courtId") or "", item.get("name") or "", item.get("time") or "")


def _is_any(item: dict) -> bool:
    return not item.get("courtId") and (item.get("name") or ANY_COURT) == ANY_COURT
