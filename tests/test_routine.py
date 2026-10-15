from datetime import date, datetime, timedelta

import pytest

from sportflex.core.engine import SnipeJob
from sportflex.core.routine import (
    apply_saved_routine,
    build_routine,
    describe_routine,
    fill_candidates,
    open_dates,
    open_days,
    plan_routine,
    skip_wave,
    weekday_token,
)
from sportflex.core.rules import BookingRules, next_release
from sportflex.core.storage import RoutineStore
from sportflex.core.venue import Venue
from sportflex.runtime.telegram_cmd import format_routine, parse_weekly_args

TZ = BookingRules().tz


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def moment_for(weekday: int, *, hour: int = 5) -> datetime:
    """A clock time whose next release opens a date on `weekday` (Monday=0)."""
    moment = datetime(2026, 10, 1, hour, 0, tzinfo=TZ)
    for offset in range(21):
        candidate = moment + timedelta(days=offset)
        release = next_release(BookingRules(), candidate)
        if date.fromisoformat(release["targetDate"]).weekday() == weekday:
            return candidate
    raise AssertionError(weekday)


@pytest.fixture
def venue():
    return Venue(id="t", name="測試中心", provider="fake", categories=["羽球"], rules=BookingRules())


def routine():
    return build_routine(BookingRules(), "羽球", [1, 3], [{"time": "20:00"}])


def test_describe_tuesday_thursday_any_court():
    saved = routine()
    assert saved["weekdays"] == [1, 3]
    assert saved["targets"] == [{"courtId": "", "name": "任一場", "time": "20:00"}]
    assert describe_routine(saved) == "每週二、四 20:00 任一場"


def test_describe_named_court():
    saved = build_routine(
        BookingRules(), "羽球", [1], [{"courtId": "A", "name": "A場", "time": "19:00"}, {"time": "20:00"}]
    )
    assert describe_routine(saved) == "每週二 A場 19:00、任一場 20:00"


def test_rejects_empty_weekdays_and_bad_targets():
    with pytest.raises(ValueError, match="星期"):
        build_routine(BookingRules(), "羽球", [], [{"time": "20:00"}])
    with pytest.raises(ValueError, match="星期"):
        build_routine(BookingRules(), "羽球", [7], [{"time": "20:00"}])
    with pytest.raises(ValueError, match="1 到 2"):
        build_routine(BookingRules(), "羽球", [1], [{"time": "18:00"}, {"time": "19:00"}, {"time": "20:00"}])


def test_arms_only_when_day_14_matches(venue):
    saved = routine()
    tuesday = moment_for(1)
    release = next_release(venue.rules, tuesday)
    assert date.fromisoformat(release["targetDate"]).weekday() == 1
    assert tuesday.hour < 6
    decision = plan_routine(None, saved, venue.rules, tuesday)
    assert decision["action"] == "arm"
    assert decision["targetDate"] == release["targetDate"]
    assert decision["targets"][0]["time"] == "20:00"

    wednesday_target = moment_for(2)
    assert plan_routine(None, saved, venue.rules, wednesday_target)["action"] == "hold"


def test_arms_the_evening_before_release(venue):
    """After today's 06:00, the next day's release is already known and gets armed."""
    evening = moment_for(1, hour=20)
    assert evening.hour == 20
    assert plan_routine(None, routine(), venue.rules, evening)["action"] == "arm"


def test_manual_and_live_waves_are_left_alone(venue):
    saved = routine()
    now = moment_for(1)
    release = next_release(venue.rules, now)
    manual = {
        "phase": "armed",
        "source": "manual",
        "category": "羽球",
        "targetDate": release["targetDate"],
        "targets": [{"courtId": "", "name": "任一場", "time": "19:00"}],
    }
    assert plan_routine(manual, saved, venue.rules, now)["action"] == "hold"
    live = {**manual, "source": "routine", "phase": "firing"}
    assert plan_routine(live, saved, venue.rules, now)["action"] == "hold"


def test_rearms_when_the_saved_time_changes(venue):
    now = moment_for(1)
    release = next_release(venue.rules, now)
    spec = {
        "phase": "armed",
        "source": "routine",
        "category": "羽球",
        "targetDate": release["targetDate"],
        "targets": [{"courtId": "", "name": "任一場", "time": "19:00"}],
    }
    decision = plan_routine(spec, routine(), venue.rules, now)
    assert decision["action"] == "arm"
    assert decision["targets"][0]["time"] == "20:00"


def test_drops_a_waiting_routine_wave_that_no_longer_matches(venue):
    now = moment_for(2)
    release = next_release(venue.rules, now)
    spec = {
        "phase": "armed",
        "source": "routine",
        "category": "羽球",
        "targetDate": release["targetDate"],
        "targets": [{"courtId": "", "name": "任一場", "time": "20:00"}],
    }
    assert plan_routine(spec, routine(), venue.rules, now)["action"] == "drop"


def test_skip_blocks_this_wave_only(venue):
    now = moment_for(1)
    saved = routine()
    release = next_release(venue.rules, now)
    saved["skipDate"] = release["targetDate"]
    assert plan_routine(None, saved, venue.rules, now)["action"] == "hold"
    later = now + timedelta(days=1)
    nxt = next_release(venue.rules, later)
    assert nxt["targetDate"] != release["targetDate"]
    if date.fromisoformat(nxt["targetDate"]).weekday() in (1, 3):
        assert plan_routine(None, saved, venue.rules, later)["action"] == "arm"


def test_finished_wave_is_not_repeated_and_the_next_match_is(venue):
    now = moment_for(1, hour=5)
    release = next_release(venue.rules, now)
    done = {
        "phase": "done",
        "source": "routine",
        "category": "羽球",
        "targetDate": release["targetDate"],
        "targets": [{"courtId": "", "name": "任一場", "time": "20:00"}],
    }
    assert plan_routine(done, routine(), venue.rules, now)["action"] == "hold"
    after = at(release["opensAt"]) + timedelta(minutes=4)
    rolled = next_release(venue.rules, after)
    assert rolled["targetDate"] != release["targetDate"]
    action = plan_routine(done, routine(), venue.rules, after)["action"]
    wanted = date.fromisoformat(rolled["targetDate"]).weekday() in (1, 3)
    assert action == ("arm" if wanted else "hold")


def test_apply_arms_and_cancel_skip_prevents_rearm(venue, tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path)
    monkeypatch.setattr("sportflex.core.storage.ROUTINES_DIR", tmp_path / "routines")
    now = moment_for(1)
    saved = routine()
    job = SnipeJob(venue, "me", lambda result: None)
    assert apply_saved_routine(job, saved, now) == "arm"
    assert job.spec["source"] == "routine"
    assert job.spec["phase"] == "armed"
    assert "每週時段已排上" in job.log.lines[-1]
    assert apply_saved_routine(job, saved, now) == "hold"

    updated = skip_wave(saved, job.spec)
    assert updated["skipDate"] == job.spec["targetDate"]
    job.cancel()
    assert apply_saved_routine(job, updated, now) == "hold"
    assert job.spec is None
    assert skip_wave(saved, {"source": "manual", "targetDate": updated["skipDate"]}) is None


def test_worker_loads_routine_and_a_cancel_sticks(venue, tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path / "snipes")
    monkeypatch.setattr("sportflex.core.storage.ROUTINES_DIR", tmp_path / "routines")
    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    now = moment_for(1)
    RoutineStore(venue.id, "me").save(routine())
    account = Account(id="me", provider="fake", username_env="U", password_env="P", profile="x")
    worker = AccountWorker(account, {venue.id: venue}, ConsoleNotifier())
    worker._apply_routines(now)
    job = worker.snipes[venue.id]
    assert job.active and job.spec["source"] == "routine"
    assert job.spec["targets"][0]["time"] == "20:00"
    worker._remember_routine_skip(venue, job)
    target = job.spec["targetDate"]
    job.cancel()
    worker._apply_routines(now)
    assert job.spec is None

    again = AccountWorker(account, {venue.id: venue}, ConsoleNotifier())
    again._apply_routines(now)
    assert again.snipes[venue.id].spec is None
    assert again._routines[venue.id]["skipDate"] == target


def test_routine_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.ROUTINES_DIR", tmp_path)
    store = RoutineStore("xindian", "default")
    store.save(routine())
    assert RoutineStore("xindian", "default").load()["weekdays"] == [1, 3]
    store.clear()
    assert RoutineStore("xindian", "default").load() is None


def test_weekly_command_tokens():
    assert weekday_token("二四") == [1, 3]
    assert weekday_token("週二") == [1]
    assert weekday_token("星期二、四") == [1, 3]
    assert parse_weekly_args(["二四", "20:00"]) == {
        "stop": False,
        "weekdays": [1, 3],
        "targets": [{"court": "", "time": "20:00"}],
        "fillDates": [],
        "fillAll": False,
    }
    assert parse_weekly_args(["二四", "20:00", "補"])["fillAll"] is True
    picked = parse_weekly_args(["二四", "20:00", "補", "10/20", "10/22"], date(2026, 10, 16))
    assert picked["fillDates"] == ["2026-10-20", "2026-10-22"]
    assert picked["fillAll"] is False
    assert parse_weekly_args(["週二", "週四", "A", "19:00"])["targets"] == [{"court": "A", "time": "19:00"}]
    assert parse_weekly_args(["off"]) == {"stop": True}
    with pytest.raises(ValueError, match="星期"):
        parse_weekly_args(["20:00"])


def test_format_routine_mentions_the_next_wave():
    text = format_routine("一號館", {"enabled": False})
    assert "/weekly 二四 20:00" in text
    armed = format_routine(
        "一號館",
        {
            "enabled": True,
            "label": "每週二、四 20:00 任一場",
            "due": True,
            "skipDate": "",
            "release": {"targetDate": "2026-10-27", "targetLabel": "10/27（週二）", "opensLabel": "10/14 06:00"},
        },
    )
    assert "每週二、四 20:00 任一場" in armed
    assert "10/27（週二）" in armed
    assert "10/14 06:00 前" in armed


def test_candidates_and_picked_open_dates():
    rules = BookingRules()
    now = at("2026-10-01T08:00:00")  # Thursday; open through 10/14, 10/15 opens next
    assert fill_candidates(routine(), rules, now) == ["2026-10-01", "2026-10-06", "2026-10-08", "2026-10-13"]
    assert open_dates(routine(), rules, now) == []  # nothing picked
    saved = build_routine(rules, "羽球", [1, 3], [{"time": "20:00"}], fill_dates=["2026-10-08", "2026-10-06", "2026-10-06"])
    assert saved["fillDates"] == ["2026-10-06", "2026-10-08"]
    assert open_dates(saved, rules, now) == ["2026-10-06", "2026-10-08"]
    assert open_dates(saved, rules, at("2026-10-07T08:00:00")) == ["2026-10-08"]  # past days drop out
    saved["skipDate"] = "2026-10-08"
    assert "2026-10-08" not in fill_candidates(saved, rules, now)
    with pytest.raises(ValueError, match="不是選定的星期"):
        build_routine(rules, "羽球", [1, 3], [{"time": "20:00"}], fill_dates=["2026-10-07"])


def test_candidates_before_release_stop_a_day_earlier():
    rules = BookingRules()
    mondays = build_routine(rules, "羽球", [0], [{"time": "20:00"}])
    # Tue 10/13: 10/26 (Mon) opens at 06:00, so it is offered only after release.
    assert fill_candidates(mondays, rules, at("2026-10-13T05:00:00")) == ["2026-10-19"]
    assert fill_candidates(mondays, rules, at("2026-10-13T07:00:00")) == ["2026-10-19", "2026-10-26"]
    assert open_days(rules, at("2026-10-13T07:00:00"))[0] == "2026-10-13"
    assert open_days(rules, at("2026-10-13T07:00:00"))[-1] == "2026-10-26"


def test_worker_fill_leaves_the_watch_day_alone(venue, tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path / "snipes")
    monkeypatch.setattr("sportflex.core.storage.ROUTINES_DIR", tmp_path / "routines")
    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    now = at("2026-10-01T08:00:00")
    picked = ["2026-10-01", "2026-10-06", "2026-10-13"]
    saved = build_routine(venue.rules, "羽球", [1, 3], [{"time": "20:00"}], fill_dates=picked)
    RoutineStore(venue.id, "me").save(saved)
    account = Account(id="me", provider="fake", username_env="U", password_env="P", profile="x")
    worker = AccountWorker(account, {venue.id: venue}, ConsoleNotifier())
    worker._apply_routines(now)
    assert sorted(worker.fills[venue.id].days) == picked

    worker.watch = type("W", (), {"running": True, "venue": venue, "spec": {"date": "2026-10-06"}})()
    worker._apply_routines(now)
    assert sorted(worker.fills[venue.id].days) == ["2026-10-01", "2026-10-13"]


def test_format_routine_lists_open_days_to_pick():
    state = {
        "enabled": True,
        "label": "每週二、四 20:00 任一場",
        "due": False,
        "release": {"targetDate": "2026-10-29", "targetLabel": "10/29（週四）"},
        "fillCandidates": ["2026-10-20", "2026-10-22"],
        "fillDates": ["2026-10-22"],
    }
    text = format_routine("一號館", state)
    assert "已開放可盯：10/20（週二）、10/22（週四） ✓" in text
    assert "補 10/20 10/22" in text
