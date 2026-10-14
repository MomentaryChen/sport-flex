from datetime import timezone
from urllib.parse import parse_qs, unquote, urlparse

from sportflex.core.calendar import booking_calendar, calendar_lines, save_ics
from sportflex.core.engine import announce_grab, grab
from sportflex.core.models import Court, Slot
from sportflex.core.rules import BookingRules
from sportflex.core.venue import Venue, get_venue
from sportflex.runtime.telegram_cmd import format_grab
from tests.test_engine import FakeProvider


REFUND = "使用日前 3–7 天取消收 20% 手續費；前 2 天內不退費、不換時段；須本人至 1F 櫃台"
OFFICIAL = "新北市新店國民運動中心"


def placed(**extra):
    return {
        "venueName": "一號館",
        "court": "羽球場_A",
        "date": "2026-10-21",
        "time": "19:00 - 20:00",
        "price": "490",
        "order": {"no": "CJC1", "paymentUrl": "https://pay.example", "carrier": "/ABC1234", "timeoutSec": 590},
        **extra,
    }


def centre(**extra):
    info = {"address": "新北市新店區北新路一段88巷12號", "refund": REFUND}
    info.update(extra.pop("info", {}))
    rules = extra.pop("rules", BookingRules())
    return Venue(
        id="xindian",
        name=OFFICIAL,
        label="一號館",
        provider="changjia",
        categories=["羽球"],
        info=info,
        rules=rules,
        **extra,
    )


def unfold(ics: str) -> str:
    return ics.replace("\r\n ", "").replace("\r\n\t", "")


def test_xindian_calendar_uses_the_venue_refund_rule():
    venue = get_venue("xindian")
    calendar = booking_calendar(venue, placed())
    assert calendar["refund"] == venue.info["refund"]
    assert "前 2 天內不退費" in calendar["refund"]
    text = unfold(calendar["ics"])
    assert "DTSTART:20261021T110000Z" in text
    assert "DTEND:20261021T120000Z" in text
    assert "SUMMARY:一號館 羽球場_A" in text
    assert f"取消規則：{venue.info['refund']}" in text
    assert "訂單：CJC1" in text
    assert OFFICIAL not in text
    assert venue.name not in unquote(calendar["googleUrl"])
    query = parse_qs(urlparse(calendar["googleUrl"]).query)
    assert query["action"] == ["TEMPLATE"]
    assert query["dates"] == ["20261021T190000/20261021T200000"]
    assert query["ctz"] == ["Asia/Taipei"]
    assert query["text"] == ["一號館 羽球場_A"]
    assert "前 2 天內不退費" in query["details"][0]
    assert "北新路" in query["location"][0]
    for line in calendar["ics"].split("\r\n"):
        if line:
            assert len(line.encode("utf-8")) <= 75
    assert calendar["filename"] == "sportflex-2026-10-21-1900.ics"


def test_ics_escapes_and_folds_long_refund_text():
    refund = ("取消,規則;含\\換行\n前 2 天內不退費。" * 8).strip()
    venue = centre(info={"refund": refund})
    calendar = booking_calendar(venue, placed())
    raw = calendar["ics"]
    assert raw.endswith("\r\n")
    for line in raw.split("\r\n"):
        if line:
            assert len(line.encode("utf-8")) <= 75
    text = unfold(raw)
    assert r"取消\,規則\;含\\換行\n前 2 天內不退費。" in text
    assert "前 2 天內不退費" in parse_qs(urlparse(calendar["googleUrl"]).query)["details"][0]


def test_overnight_slot_ends_the_next_day():
    calendar = booking_calendar(centre(), placed(time="23:00 - 01:00"))
    text = unfold(calendar["ics"])
    assert "DTSTART:20261021T150000Z" in text
    assert "DTEND:20261021T170000Z" in text
    assert parse_qs(urlparse(calendar["googleUrl"]).query)["dates"] == ["20261021T230000/20261022T010000"]


def test_utc_venue_keeps_the_same_clock():
    venue = centre(rules=BookingRules(timezone="UTC"))
    text = unfold(booking_calendar(venue, placed())["ics"])
    assert "DTSTART:20261021T190000Z" in text
    assert "DTEND:20261021T200000Z" in text


def test_no_calendar_without_an_order_or_a_readable_time():
    venue = centre()
    assert booking_calendar(venue, placed(order=None)) is None
    assert booking_calendar(venue, placed(time="not-a-time")) is None
    quiet = Venue(
        id="t", name="正式名稱", label="簡稱", provider="changjia", categories=["羽球"], info={"address": "某處"}
    )
    calendar = booking_calendar(quiet, placed(venueName="簡稱"))
    assert calendar["refund"] == ""
    assert "取消規則" not in unfold(calendar["ics"])
    assert "正式名稱" not in unfold(calendar["ics"])
    assert "簡稱" in unfold(calendar["ics"])


def test_save_ics_writes_the_file(tmp_path):
    calendar = booking_calendar(get_venue("xindian"), placed())
    path = save_ics(calendar, tmp_path)
    assert path.name == "sportflex-2026-10-21-1900.ics"
    assert path.read_bytes() == calendar["ics"].encode("utf-8")
    assert save_ics({**calendar, "filename": "../nope.ics"}, tmp_path).parent == tmp_path


def test_grab_attaches_calendar_only_after_the_order_is_placed():
    venue = centre()
    court = Court(id="A", name="羽球場_A", venue="xindian", category="羽球")
    slot = Slot("19:00 - 20:00", True, "", "490")

    class Ordering(FakeProvider):
        def submit(self, court, query_date, slot):
            return {**super().submit(court, query_date, slot), "order": {"no": "CJC9"}}

    booked = grab(Ordering(), venue, court, "2026-10-21", slot)
    assert booked["order"]["no"] == "CJC9"
    assert "前 2 天內不退費" in booked["calendar"]["ics"]
    assert booked["venueName"] == "一號館"
    assert OFFICIAL not in booked["calendar"]["ics"]

    missed = grab(FakeProvider(), venue, court, "2026-10-21", slot)
    assert missed.get("order") is None
    assert "calendar" not in missed

    class Unreadable(FakeProvider):
        def submit(self, court, query_date, slot):
            return {**super().submit(court, query_date, slot), "order": {"no": "CJC9"}}

    kept = grab(Unreadable(), venue, court, "2026-10-21", Slot("bad", True, "", "490"))
    assert kept["order"]["no"] == "CJC9"
    assert "calendar" not in kept


class Box:
    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))


def test_messages_include_the_refund_rule_and_google_link():
    calendar = booking_calendar(get_venue("xindian"), placed())
    result = placed(calendar=calendar)
    box = Box()
    announce_grab(box, "我", result)
    body = box.sent[0][1]
    assert "前 2 天內不退費" in body
    assert calendar["googleUrl"] in body
    assert "取消規則：" in body
    text = format_grab(result)
    assert "取消規則：" in text and "前 2 天內不退費" in text
    assert calendar["googleUrl"] in text
    assert calendar_lines(result)[0].startswith("取消規則：")


def test_stamp_is_utc():
    text = unfold(booking_calendar(centre(), placed())["ics"])
    stamp = text.split("DTSTAMP:", 1)[1].split("\r\n", 1)[0]
    assert stamp.endswith("Z")
    assert datetime_ok(stamp)


def datetime_ok(stamp: str) -> bool:
    from datetime import datetime

    datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return True
