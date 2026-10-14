"""Remind before a paid booking enters the venue's no-refund window.

The cutoff is read from the cancellation text already stored on the venue, for example
「前 2 天內不退費」. The tool never cancels a booking; the person still does that at the counter.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from sportflex.core.rules import WEEKDAYS
from sportflex.core.venue import Venue

# Morning of the last day a refund is still possible, in the venue's timezone.
# The counter is open by then, and the no-refund window starts at the next midnight.
REFUND_REMIND_HOUR = 9

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NO_REFUND = re.compile(
    r"前\s*([0-9]{1,2}|[零〇一二兩三四五六七八九十]{1,3})\s*[天日]\s*內\s*(?:不予退費|不得退費|不退費|不退款)"
)


def no_refund_days(text: str) -> int | None:
    """Days before the usage date when a refund stops, or None when the text has no such cutoff.

    「使用日前 3–7 天取消收 20% 手續費；前 2 天內不退費」is 2. A fee for cancelling earlier
    is not the cutoff. Several clauses use the tightest one.
    """
    found: list[int] = []
    normalized = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    for match in _NO_REFUND.finditer(normalized):
        days = _number(match.group(1))
        if days:
            found.append(days)
    return min(found) if found else None


def plan_refund_reminder(venue: Venue, result: dict, now: datetime | None = None) -> dict | None:
    """When to ask about cancelling, plus the sentence shown as soon as the order is placed.

    None when no order was placed, the date cannot be read, or the venue text has no cutoff.
    `passed` means the no-refund window has already started, so there is nothing left to schedule.
    """
    order = result.get("order") or {}
    if not order:
        return None
    days = no_refund_days(str(venue.info.get("refund") or ""))
    if not days:
        return None
    try:
        usage = date.fromisoformat(str(result.get("date") or ""))
    except ValueError:
        return None
    tz = venue.rules.tz
    current = _aware(now, tz)
    deadline = datetime.combine(usage - timedelta(days=days), time.min, tzinfo=tz)
    deadline_label = _when(deadline)
    if current >= deadline:
        return {
            "days": days,
            "deadline": deadline.isoformat(),
            "remindAt": "",
            "passed": True,
            "key": _key(venue, result),
            "line": f"這筆已在不退費期限內（{deadline_label}起不退費）。",
            "title": "",
            "body": "",
        }
    remind_at = datetime.combine(deadline.date() - timedelta(days=1), time(REFUND_REMIND_HOUR, 0), tzinfo=tz)
    pending = bool(str(order.get("paymentUrl") or "").strip())
    prefix = "付款後會" if pending else "會"
    if current >= remind_at:
        line = f"{prefix}提醒要不要取消（{deadline_label}起不退費）。"
    else:
        line = f"{prefix}在 {_when(remind_at)} 提醒要不要取消（{deadline_label}起不退費）。"
    return {
        "days": days,
        "deadline": deadline.isoformat(),
        "remindAt": remind_at.isoformat(),
        "passed": False,
        "key": _key(venue, result),
        "line": line,
        "title": _title(venue, result, usage),
        "body": _body(venue, result, days, deadline_label),
    }


def reminder_line(result: dict) -> str:
    return str((result.get("refundReminder") or {}).get("line") or "").strip()


def _title(venue: Venue, result: dict, usage: date) -> str:
    name = str(result.get("venueName") or venue.display_name)
    court = str(result.get("court") or "").strip()
    start = str(result.get("time") or "").split("-", 1)[0].strip()
    when = f"{usage.month}/{usage.day}（週{WEEKDAYS[usage.weekday()]}）"
    if start:
        when = f"{when} {start}"
    return " ".join(part for part in (f"要不要取消？{name}", court, when) if part)


def _body(venue: Venue, result: dict, days: int, deadline_label: str) -> str:
    refund = str(venue.info.get("refund") or "").strip()
    order_no = str((result.get("order") or {}).get("no") or "").strip()
    lines = [
        " ".join(part for part in (str(result.get("court") or ""), str(result.get("date") or ""), str(result.get("time") or "")) if part),
        f"{deadline_label}起不退費（使用日前 {days} 天內）。",
        "想取消要在這之前本人到櫃台辦理。這支程式不會代為取消。",
    ]
    if refund:
        lines.append(f"取消規則：{refund}")
    if order_no:
        lines.append(f"訂單：{order_no}")
    return "\n".join(line for line in lines if line)


def _key(venue: Venue, result: dict) -> str:
    order = result.get("order") or {}
    luid = str(order.get("luid") or "").strip()
    if luid:
        return luid
    return "|".join(
        str(part or "")
        for part in (venue.id, order.get("no"), result.get("date"), result.get("time"), result.get("court"))
    )


def _when(moment: datetime) -> str:
    label = f"{moment.month}/{moment.day}（週{WEEKDAYS[moment.weekday()]}）"
    if moment.hour == 0 and moment.minute == 0:
        return label
    return f"{label} {moment.strftime('%H:%M')}"


def _aware(now: datetime | None, tz) -> datetime:
    current = now.astimezone(tz) if now else datetime.now(tz)
    if current.tzinfo is None:
        return current.replace(tzinfo=tz)
    return current


def _number(text: str) -> int | None:
    if text.isdigit():
        value = int(text)
        return value if value > 0 else None
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in _DIGITS:
            return None
        if right and right not in _DIGITS:
            return None
        return (_DIGITS[left] if left else 1) * 10 + (_DIGITS[right] if right else 0)
    if text in _DIGITS and _DIGITS[text] > 0:
        return _DIGITS[text]
    return None
