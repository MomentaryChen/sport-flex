from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class Court:
    id: str
    name: str
    venue: str
    category: str
    price: str = ""
    min_hours: str = ""
    raw: dict = field(default_factory=dict)  # provider-specific fields, e.g. Changjia form_id / LID


@dataclass
class Slot:
    time: str  # "HH:MM - HH:MM"
    bookable: bool
    status: str
    price: str
    raw: dict = field(default_factory=dict)  # provider-specific fields needed to submit


@dataclass
class OrderOptions:
    """How far submit goes. submit_order=False stops on the order page as before; True places the
    order, fills the invoice carrier and stops before the card page (the person pays from a link)."""

    submit_order: bool = False
    invoice_carrier: str = ""  # Taiwan e-invoice mobile barcode, e.g. "/ABC1234"


@dataclass
class Availability:
    query_date: str
    window_start: str
    window_end: str
    slots: list[Slot]


def slot_bounds(label: str) -> tuple[str, str, float]:
    start, end = [part.strip() for part in label.split("-", 1)]
    sh, sm = [int(part) for part in start.split(":")]
    eh, em = [int(part) for part in end.split(":")]
    hours = (eh * 60 + em - (sh * 60 + sm)) / 60
    return start, end, hours


def slot_matches(slot: Slot, start: str | None, end: str | None) -> bool:
    begin = slot.time.split("-", 1)[0].strip()
    if start and begin < start:
        return False
    if end and begin >= end:
        return False
    return True


def iter_dates(start: str, end: str):
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)
