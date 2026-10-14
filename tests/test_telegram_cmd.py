from datetime import date

import pytest

from sportflex.runtime.telegram_cmd import (
    filter_courts,
    format_grab,
    format_slots,
    match_court,
    parse_book_tokens,
    parse_slots_args,
    parse_snipe_tokens,
    format_watch,
    parse_watch_args,
    parse_window,
    resolve_date,
)

TODAY = date(2026, 10, 13)
COURTS = [
    {"id": "A", "name": "羽球場_A"},
    {"id": "B", "name": "羽球場_B"},
]


def test_dates_and_periods():
    assert resolve_date("明天", TODAY) == "2026-10-14"
    assert resolve_date("10/20", TODAY) == "2026-10-20"
    assert resolve_date("1/5", TODAY) == "2027-01-05"
    assert resolve_date("2026-10-20", TODAY) == "2026-10-20"
    assert parse_window("晚上") == ("18:00", "23:00")
    assert parse_window("19") == ("19:00", "20:00")
    assert parse_window("19:00-21:00") == ("19:00", "21:00")
    with pytest.raises(ValueError):
        resolve_date("2/31", TODAY)
    with pytest.raises(ValueError):
        parse_window("21:00-19:00")


def test_slots_args():
    assert parse_slots_args(["明天", "晚上"], TODAY) == ("2026-10-14", "18:00", "23:00")
    assert parse_slots_args(["10/20", "19:00"], TODAY) == ("2026-10-20", "19:00", "20:00")
    assert parse_slots_args([], TODAY) == ("", "", "")
    with pytest.raises(ValueError):
        parse_slots_args(["羽球"], TODAY)


def test_snipe_and_book_tokens():
    assert parse_snipe_tokens(["19:00", "20"]) == [
        {"court": "", "time": "19:00"},
        {"court": "", "time": "20:00"},
    ]
    assert parse_snipe_tokens(["A", "19:00", "B", "20:00"]) == [
        {"court": "A", "time": "19:00"},
        {"court": "B", "time": "20:00"},
    ]
    with pytest.raises(ValueError):
        parse_snipe_tokens(["A"])
    assert parse_book_tokens(["明天", "19:00", "羽球場_A"], TODAY) == ("2026-10-14", "19:00", "羽球場_A")
    with pytest.raises(ValueError):
        parse_book_tokens(["19:00"], TODAY)


def test_watch_args():
    assert parse_watch_args(["off"], TODAY) == {"stop": True}
    assert parse_watch_args(["明天", "晚上"], TODAY) == {
        "stop": False,
        "date": "2026-10-14",
        "timeFrom": "18:00",
        "timeTo": "23:00",
        "court": "",
    }
    assert parse_watch_args(["10/20", "19:00", "羽球場_A"], TODAY)["court"] == "羽球場_A"
    with pytest.raises(ValueError, match="日期"):
        parse_watch_args(["晚上"], TODAY)


def test_court_matching():
    assert match_court(COURTS, "A")["id"] == "A"
    assert match_court(COURTS, "羽球場_B")["id"] == "B"
    assert len(filter_courts(COURTS, "羽球")) == 2
    with pytest.raises(ValueError, match="多面"):
        match_court(COURTS, "羽球")
    with pytest.raises(ValueError, match="找不到"):
        match_court(COURTS, "壁球")


def test_format_slots_keeps_bookable_in_range():
    board = {
        "category": "羽球",
        "window": {"start": "2026-10-13", "end": "2026-10-26", "date": "2026-10-20"},
        "courts": [
            {
                "name": "羽球場_A",
                "slots": [
                    {"time": "18:00 - 19:00", "bookable": True, "status": "", "price": "490"},
                    {"time": "19:00 - 20:00", "bookable": False, "status": "已被預約", "price": ""},
                ],
            }
        ],
    }
    text = format_slots("新店", board, "18:00", "23:00")
    assert "18:00 - 19:00  羽球場_A $490" in text
    assert "19:00 - 20:00" not in text


def test_format_grab_includes_payment_link():
    text = format_grab(
        {
            "venueName": "新店",
            "court": "羽球場_A",
            "date": "2026-10-20",
            "time": "19:00 - 20:00",
            "price": "490",
            "order": {"no": "99", "paymentUrl": "https://pay.example", "timeoutSec": 600, "carrier": "/ABC1234"},
        }
    )
    assert "訂單已成立：99" in text
    assert "https://pay.example" in text
    assert "10 分鐘" in text
    assert "/ABC1234" in text


def test_format_watch_lists_exact_targets():
    text = format_watch(
        "測試中心",
        {
            "running": True,
            "spec": {
                "category": "羽球",
                "date": "2026-10-20",
                "timeFrom": "19:00",
                "timeTo": "22:00",
                "targets": [{"name": "A", "time": "19:00"}, {"time": "21:00"}],
            },
            "log": [],
        },
    )
    assert "A 19:00、任一場 21:00" in text
    assert "19:00–22:00" not in text
