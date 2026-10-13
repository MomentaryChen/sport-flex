from datetime import datetime

import pytest
from pydantic import ValidationError

from sportflex.core.rules import BookingRules, holds_from_board, next_release, quota_reason, validate_targets
from sportflex.core.venue import load_venues

TZ = BookingRules().tz


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def test_before_release_targets_day_13():
    release = next_release(BookingRules(), at("2026-10-08T05:59:59"))
    assert release["opensAt"].startswith("2026-10-08T06:00")
    assert release["targetDate"] == "2026-10-21"
    assert release["openEnd"] == "2026-10-20"


def test_at_release_rolls_to_tomorrow_day_14():
    release = next_release(BookingRules(), at("2026-10-08T06:00:00"))
    assert release["opensAt"].startswith("2026-10-09T06:00")
    assert release["targetDate"] == "2026-10-22"
    assert release["openEnd"] == "2026-10-21"


def test_release_crosses_month():
    release = next_release(BookingRules(), at("2026-10-31T23:00:00"))
    assert release["opensAt"].startswith("2026-11-01T06:00")
    assert release["targetDate"] == "2026-11-14"


def test_other_venue_rules():
    rules = BookingRules(window_days=7, release_time="00:00")
    release = next_release(rules, at("2026-10-08T12:00:00"))
    assert release["opensAt"].startswith("2026-10-09T00:00")
    assert release["targetDate"] == "2026-10-15"


def test_unknown_rule_field_rejected():
    with pytest.raises(ValidationError):
        BookingRules(windows_days=14)


def test_bad_release_time_rejected():
    with pytest.raises(ValidationError):
        BookingRules(release_time="25:00")


def test_targets_valid():
    targets = validate_targets(BookingRules(), [
        {"courtId": "A", "name": "A", "time": "19:00"},
        {"courtId": "B", "name": "B", "time": "9:00"},
    ])
    assert [item["time"] for item in targets] == ["19:00", "09:00"]
    assert all(item["status"] == "waiting" for item in targets)


def test_targets_over_daily_limit():
    raw = [{"courtId": c, "time": "19:00"} for c in "ABC"]
    with pytest.raises(ValueError, match="1 到 2"):
        validate_targets(BookingRules(), raw)


def test_targets_same_court():
    raw = [{"courtId": "A", "time": "19:00"}, {"courtId": "A", "time": "20:00"}]
    with pytest.raises(ValueError, match="不同場"):
        validate_targets(BookingRules(), raw)
    assert len(validate_targets(BookingRules(distinct_courts=False), raw)) == 2


def test_quota_counts_hours_already_held():
    rules = BookingRules()
    held = [{"courtId": "A", "hours": 1}, {"courtId": "B", "hours": 1}]
    assert "最多" in quota_reason(rules, held, "C", 1)
    assert quota_reason(rules, held[:1], "C", 1) is None
    assert "不同場" in quota_reason(rules, held[:1], "A", 1)
    assert quota_reason(BookingRules(distinct_courts=False), held[:1], "A", 1) is None
    board = [{"id": "A", "name": "場A", "slots": [{"time": "08:00 - 09:00", "mine": True}, {"time": "09:00 - 10:00", "mine": False}]}]
    assert holds_from_board(board) == [{"courtId": "A", "name": "場A", "time": "08:00 - 09:00", "hours": 1.0}]


def test_targets_not_on_the_hour():
    with pytest.raises(ValueError, match="對齊"):
        validate_targets(BookingRules(), [{"courtId": "A", "time": "19:30"}])
    assert validate_targets(BookingRules(align_minutes=30), [{"courtId": "A", "time": "19:30"}])


def test_empty_rows_ignored_and_half_rows_rejected():
    assert len(validate_targets(BookingRules(), [{"courtId": "A", "time": "19:00"}, {}])) == 1
    with pytest.raises(ValueError, match="開始時間"):
        validate_targets(BookingRules(), [{"courtId": "A"}])


def test_shipped_venue_configs_load():
    venues = load_venues()
    assert "xindian" in venues
    assert venues["xindian"].rules.max_bookings_per_day == 2


def test_any_court_targets():
    targets = validate_targets(BookingRules(), [{"time": "19:00"}, {"courtId": "", "time": "20:00"}])
    assert [(item["courtId"], item["name"]) for item in targets] == [("", "任一場"), ("", "任一場")]
    with pytest.raises(ValueError, match="開始時間"):
        validate_targets(BookingRules(), [{"courtId": "A", "time": ""}])
