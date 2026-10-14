"""The no-refund cutoff is read from the venue text, then a paid booking is reminded once."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sportflex.core.engine import announce_grab, grab
from sportflex.core.events import EventStore
from sportflex.core.models import Court, Slot
from sportflex.core.refund import no_refund_days, plan_refund_reminder
from sportflex.core.rules import BookingRules
from sportflex.core.venue import Venue, get_venue
from sportflex.runtime.telegram_cmd import format_grab
from tests.test_engine import FakeProvider
from tests.test_order import RESULT, Unpaid, account, due


REFUND = "使用日前 3–7 天取消收 20% 手續費；前 2 天內不退費、不換時段；須本人至 1F 櫃台"
OFFICIAL = "新北市新店國民運動中心"
TZ = ZoneInfo("Asia/Taipei")
WHEN = datetime(2026, 10, 14, 12, tzinfo=TZ)


def centre(**extra):
    info = {"refund": REFUND}
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


def placed(**extra):
    order = extra.pop("order", {"no": "CJC1", "paymentUrl": "https://pay.example", "luid": "L1"})
    return {
        "venue": "xindian",
        "venueName": "一號館",
        "court": "羽球場_A",
        "date": "2026-10-21",
        "time": "19:00 - 20:00",
        "price": "490",
        "order": order,
        **extra,
    }


def test_xindian_rule_is_two_days():
    assert no_refund_days(get_venue("xindian").info["refund"]) == 2
    assert no_refund_days(REFUND) == 2


def test_parses_chinese_numerals_and_ignores_the_fee_clause():
    assert no_refund_days("前二日內不予退費") == 2
    assert no_refund_days("前十二天內不退款") == 12
    assert no_refund_days("前１０天內不退費") == 10
    assert no_refund_days("使用日前 3–7 天取消收 20% 手續費") is None
    assert no_refund_days("") is None
    assert no_refund_days("前 2 天內不退費；前 7 天內不退費") == 2


def test_reminder_is_the_morning_before_the_cutoff():
    plan = plan_refund_reminder(centre(), placed(), WHEN)
    assert plan["days"] == 2
    assert plan["deadline"].startswith("2026-10-19T00:00:00")
    assert plan["remindAt"].startswith("2026-10-18T09:00:00")
    assert plan["passed"] is False
    assert "10/18（週日） 09:00" in plan["line"]
    assert "付款後會在" in plan["line"]
    assert "10/19（週一）起不退費" in plan["line"]
    assert plan["title"] == "要不要取消？一號館 羽球場_A 10/21（週三） 19:00"
    assert "本人到櫃台" in plan["body"]
    assert "不會代為取消" in plan["body"]
    assert "取消規則：" in plan["body"] and "前 2 天內不退費" in plan["body"]
    assert "訂單：CJC1" in plan["body"]
    assert OFFICIAL not in plan["title"] and OFFICIAL not in plan["body"]


def test_same_morning_and_after_the_cutoff_change_the_sentence():
    morning = plan_refund_reminder(centre(), placed(), datetime(2026, 10, 18, 10, tzinfo=TZ))
    assert morning["line"] == "付款後會提醒要不要取消（10/19（週一）起不退費）。"
    late = plan_refund_reminder(centre(), placed(), datetime(2026, 10, 19, 0, 1, tzinfo=TZ))
    assert late["passed"] is True
    assert late["remindAt"] == ""
    assert late["line"] == "這筆已在不退費期限內（10/19（週一）起不退費）。"


def test_no_plan_without_an_order_or_a_cutoff():
    assert plan_refund_reminder(centre(), placed(order=None)) is None
    quiet = centre(info={"refund": "現場洽櫃台"})
    assert plan_refund_reminder(quiet, placed()) is None
    held = plan_refund_reminder(centre(), placed(order={"paymentUrl": ""}), WHEN)
    assert held["line"].startswith("會在 10/18")


def test_messages_include_the_planned_reminder():
    result = placed(refundReminder=plan_refund_reminder(centre(), placed(), WHEN))
    box = []

    class Notifier:
        def send(self, title, body):
            box.append((title, body))

    announce_grab(Notifier(), "我", result)
    assert "10/18（週日） 09:00" in box[0][1]
    text = format_grab(result)
    assert "要不要取消" in text
    assert OFFICIAL not in text


def test_grab_attaches_the_plan(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 8, tzinfo=tz)

    monkeypatch.setattr("sportflex.core.refund.datetime", Clock)

    class Ordering(FakeProvider):
        def submit(self, court, query_date, slot):
            return {**super().submit(court, query_date, slot), "order": {"no": "CJC9", "paymentUrl": "https://pay"}}

    court = Court(id="A", name="羽球場_A", venue="xindian", category="羽球")
    booked = grab(Ordering(), centre(), court, "2026-10-21", Slot("19:00 - 20:00", True, "", "490"))
    assert booked["refundReminder"]["remindAt"].startswith("2026-10-18T09:00:00")
    assert "付款後會在 10/18（週日） 09:00" in booked["refundReminder"]["line"]


def watcher(monkeypatch, tmp_path, venue=None):
    from sportflex.runtime.worker import AccountWorker

    monkeypatch.setattr("sportflex.core.storage.BOOKINGS_DIR", tmp_path)
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path / "snipes")
    venue = centre() if venue is None else venue
    box = []

    class Notifier:
        def send(self, title, body):
            box.append((title, body))

    worker = AccountWorker(account(), {venue.id: venue}, Notifier(), events=EventStore(":memory:"))
    return worker, box


def test_paying_before_the_timeout_arms_one_reminder(monkeypatch, tmp_path):
    worker, box = watcher(monkeypatch, tmp_path)
    order = {
        "no": "CJC1",
        "luid": "L1",
        "lid": "XDSC",
        "paymentUrl": "https://pay.example",
        "carrier": "",
        "timeoutSec": 590,
    }
    worker._track_payment({**RESULT, "venue": "xindian", "venueName": "一號館", "date": "2099-01-10", "order": order})
    due(worker)
    worker._check_payments(Unpaid(["L1"]))  # still unpaid: the payment nag, not the refund one
    assert len(box) == 1 and "還沒付款" in box[0][0]
    due(worker)
    worker._check_payments(Unpaid([]))
    assert len(box) == 1 and len(worker._bookings) == 1
    saved = worker._bookings[0]
    assert saved["deadline"].startswith("2099-01-08T00:00:00")
    assert saved["remindAt"].startswith("2099-01-07T09:00:00")
    assert saved["reminded"] is False
    due(worker)
    worker._check_payments(Unpaid([]))  # already gone from the payment watch
    assert len(worker._bookings) == 1

    worker._check_refunds(datetime(2099, 1, 6, 12, tzinfo=TZ))
    assert box == [(box[0][0], box[0][1])] and saved["reminded"] is False
    worker._check_refunds(datetime(2099, 1, 7, 9, tzinfo=TZ))
    assert len(box) == 2
    title, body = box[1]
    assert title.startswith("要不要取消？一號館")
    assert "2099-01-10" in body and "不會代為取消" in body and "帳號：" in body
    assert OFFICIAL not in title and OFFICIAL not in body
    kinds = [row["kind"] for row in worker.events.events(days=1)]
    assert "refund_reminder" in kinds
    alert = next(row for row in worker.events.events(days=1) if row["kind"] == "alert" and "取消" in row["message"])
    assert alert["level"] == "info"
    worker._check_refunds(datetime(2099, 1, 7, 18, tzinfo=TZ))
    assert len(box) == 2

    from sportflex.runtime.worker import AccountWorker

    again = AccountWorker(account(), {"xindian": centre()}, worker.notifier, events=EventStore(":memory:"))
    again._check_refunds(datetime(2099, 1, 7, 18, tzinfo=TZ))
    assert len(box) == 2 and again._bookings[0]["reminded"] is True
    again._check_refunds(datetime(2099, 1, 8, 0, tzinfo=TZ))
    assert again._bookings == [] and not again._bookings


def test_clearing_the_list_after_the_timeout_does_not_arm_a_reminder(monkeypatch, tmp_path):
    worker, box = watcher(monkeypatch, tmp_path)
    order = {"no": "CJC1", "luid": "L1", "lid": "XDSC", "paymentUrl": "https://pay.example", "timeoutSec": 590}
    worker._track_payment({**RESULT, "venue": "xindian", "date": "2099-01-10", "order": order})
    due(worker)
    worker._check_payments(Unpaid(["L1"]))
    worker._payments[0]["placed"] = 0
    due(worker)
    worker._check_payments(Unpaid([]))
    assert any("已結束" in title for title, _body in box)
    assert worker._bookings == []


def test_a_booking_without_online_payment_is_armed_immediately(monkeypatch, tmp_path):
    worker, _box = watcher(monkeypatch, tmp_path)
    worker._track_payment(
        {
            **RESULT,
            "venue": "xindian",
            "venueName": "一號館",
            "date": "2099-01-10",
            "order": {"no": "CJC1", "paymentUrl": ""},
        }
    )
    assert len(worker._bookings) == 1
    assert worker._payments == []


def test_a_passed_cutoff_is_not_stored(monkeypatch, tmp_path):
    worker, box = watcher(monkeypatch, tmp_path)
    worker._bookings = [
        {
            "key": "old",
            "venue": "xindian",
            "title": "要不要取消？",
            "body": "太晚了",
            "deadline": "2020-01-01T00:00:00+08:00",
            "remindAt": "2019-12-31T09:00:00+08:00",
            "reminded": False,
        }
    ]
    worker._check_refunds(WHEN)
    assert worker._bookings == [] and box == []
