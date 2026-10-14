"""Turn a placed order into a calendar event, with the venue's cancellation rule."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from sportflex.core.models import slot_bounds
from sportflex.core.venue import Venue

_UID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def booking_calendar(venue: Venue, result: dict) -> dict | None:
    """Google Calendar link and .ics text for a placed order.

    Returns None when no order was placed, or when the slot time cannot be read.
    A calendar problem must not hide an order that already exists.
    """
    if not result.get("order"):
        return None
    try:
        start, end = _span(str(result.get("date") or ""), str(result.get("time") or ""), venue.rules.tz)
    except (ValueError, IndexError):
        return None
    title = _title(result)
    refund = str(venue.info.get("refund") or "").strip()
    address = str(venue.info.get("address") or "").strip()
    description = _description(result, refund)
    start_clock = start.strftime("%H%M")
    day = start.strftime("%Y-%m-%d")
    return {
        "googleUrl": _google_url(title, start, end, description, address, venue.rules.timezone),
        "ics": _ics(venue, result, title, description, address, start, end),
        "filename": f"sportflex-{day}-{start_clock}.ics",
        "refund": refund,
    }


def calendar_lines(result: dict) -> list[str]:
    """Cancellation reminder and Google Calendar link, for messages about a placed order."""
    calendar = result.get("calendar") or {}
    lines = []
    refund = str(calendar.get("refund") or "").strip()
    if refund:
        lines.append(f"取消規則：{refund}")
    url = str(calendar.get("googleUrl") or "").strip()
    if url:
        lines.append(f"加入 Google 行事曆：{url}")
    return lines


def save_ics(calendar: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(str(calendar.get("filename") or "booking.ics")).name
    if not name.endswith(".ics") or name.startswith("."):
        name = "booking.ics"
    path = directory / name
    path.write_bytes(str(calendar.get("ics") or "").encode("utf-8"))
    return path


def _title(result: dict) -> str:
    # venueName is the short label. The official centre name stays out of the event.
    return " ".join(part for part in (str(result.get("venueName") or ""), str(result.get("court") or "")) if part).strip() or "預約"


def _description(result: dict, refund: str) -> str:
    order = result.get("order") or {}
    lines = []
    if order.get("no"):
        lines.append(f"訂單：{order['no']}")
    if result.get("price"):
        lines.append(f"費用：${result['price']}")
    if refund:
        lines.append(f"取消規則：{refund}")
    return "\n".join(lines)


def _span(day: str, label: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start_text, end_text, _hours = slot_bounds(label)
    start = datetime.fromisoformat(f"{day}T{start_text}").replace(tzinfo=tz)
    end = datetime.fromisoformat(f"{day}T{end_text}").replace(tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _google_url(title: str, start: datetime, end: datetime, description: str, address: str, tz_name: str) -> str:
    # Wall-clock times plus ctz, so the form shows the venue's clock, not UTC.
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start.strftime('%Y%m%dT%H%M%S')}/{end.strftime('%Y%m%dT%H%M%S')}",
        "ctz": tz_name,
    }
    if description:
        params["details"] = description
    if address:
        params["location"] = address
    return "https://calendar.google.com/calendar/render?" + urlencode(params, quote_via=quote)


def _ics(venue: Venue, result: dict, title: str, description: str, address: str, start: datetime, end: datetime) -> str:
    order_no = str((result.get("order") or {}).get("no") or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fallback = f"{venue.id}-{start.strftime('%Y%m%dT%H%M%S')}"
    uid = f"{_UID_SAFE.sub('', order_no) or fallback}@sportflex"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sport Flex//Booking//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_escape(title)}",
    ]
    if address:
        lines.append(f"LOCATION:{_escape(address)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "".join(_fold(line) + "\r\n" for line in lines)


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line at 75 octets, as RFC 5545 requires."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts: list[str] = []
    limit = 75
    while encoded:
        take = min(limit, len(encoded))
        if take < len(encoded):
            while take > 0 and (encoded[take] & 0xC0) == 0x80:
                take -= 1
            if take <= 0:
                take = min(limit, len(encoded))
        parts.append(encoded[:take].decode("utf-8"))
        encoded = encoded[take:]
        limit = 74  # a continuation line starts with one space
    return "\r\n ".join(parts)
