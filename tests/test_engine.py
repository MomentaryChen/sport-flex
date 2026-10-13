from datetime import datetime, timedelta

import pytest

from sportflex.core import engine
from sportflex.core.engine import SnipeJob, WatchJob
from sportflex.core.models import Availability, Court, Slot
from sportflex.core.rules import BookingRules, next_release
from sportflex.core.venue import Venue
from sportflex.providers.base import NeedLogin, Provider, Throttled


class FakeProvider(Provider):
    """In-memory platform: courts A and B, slots controlled per test."""

    name = "fake"
    login_url = ""

    def __init__(self, released_date: str = "", logged_in: bool = True) -> None:
        super().__init__(page=None)
        self.released_date = released_date
        self.logged_in = logged_in
        self.open: dict[str, list[str]] = {}  # court id -> open start times
        self.hours = 1
        self.submitted: list[tuple[str, str, str]] = []
        self.prepared: list[str] = []
        self.throttle = False

    def refresh_session(self):
        return {"loggedIn": self.logged_in, "banner": ""}

    def list_courts(self, venue, category):
        return [Court(id=c, name=f"場{c}", venue=venue.name, category=category) for c in "AB"]

    def availability(self, court, query_date=""):
        if self.throttle:
            raise Throttled("請勿操作頻繁")
        if query_date != self.released_date:
            return Availability(query_date="2000-01-01", window_start="", window_end="", slots=[])
        slots = []
        for hour in range(6, 22):
            start = f"{hour:02d}:00"
            end = f"{hour + self.hours:02d}:00"
            slots.append(Slot(time=f"{start} - {end}", bookable=start in self.open.get(court.id, []), status="", price="290"))
        return Availability(query_date=query_date, window_start="", window_end="", slots=slots)

    def prepare(self, court):
        self.prepared.append(court.id)

    def submit(self, court, query_date, slot):
        self.submitted.append((court.id, query_date, slot.time))
        return {"url": "https://example/confirm", "text": "", "buttons": [], "image": b"", "pressed": "確認預約"}


@pytest.fixture
def venue():
    return Venue(
        id="t",
        name="測試中心",
        provider="fake",
        categories=["羽球"],
        rules=BookingRules(poll_sec=0, login_recheck_min=9999),
    )


@pytest.fixture(autouse=True)
def no_disk(tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path)


def armed(venue, grabs, targets=None):
    job = SnipeJob(venue, "me", grabs.append)
    job.arm("羽球", targets or [{"courtId": "A", "name": "A", "time": "19:00"}])
    job._next_at = 0
    return job


def opens(job) -> datetime:
    return datetime.fromisoformat(job.spec["opensAt"])


def test_waits_until_lead_time(venue):
    grabs = []
    job = armed(venue, grabs)
    provider = FakeProvider()
    job.tick(provider, opens(job) - timedelta(minutes=5))
    assert job.spec["phase"] == "armed" and not provider.prepared


def test_prepares_then_fires_and_stops_at_confirm(venue):
    grabs = []
    job = armed(venue, grabs)
    target = job.spec["targetDate"]
    provider = FakeProvider(released_date=target)
    provider.open = {"A": ["19:00"]}

    job.tick(provider, opens(job) - timedelta(seconds=30))
    assert job.spec["phase"] == "ready" and provider.prepared == ["A"]

    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert provider.submitted == [("A", target, "19:00 - 20:00")]
    assert job.spec["phase"] == "done"
    assert grabs and grabs[0]["court"] == "場A" and grabs[0]["pressed"] == "確認預約"


def test_need_login_before_release(venue):
    job = armed(venue, [])
    job.tick(FakeProvider(logged_in=False), opens(job) - timedelta(seconds=30))
    assert job.spec["phase"] == "need_login"


def test_keeps_polling_until_released(venue):
    job = armed(venue, [])
    provider = FakeProvider(released_date="not-yet")
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert job.spec["phase"] == "firing" and not provider.submitted


def test_wrong_length_slot_skipped(venue):
    job = armed(venue, [])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}
    provider.hours = 2
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert not provider.submitted
    assert job.spec["targets"][0]["status"] == "skipped"
    assert job.spec["phase"] == "done"


def test_throttle_backs_off(venue):
    job = armed(venue, [])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.throttle = True
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert job._next_at > 0 and job.spec["phase"] == "firing"


def test_gives_up_after_deadline(venue):
    job = armed(venue, [])
    job._prepared = True
    late = opens(job) + timedelta(minutes=venue.rules.snipe_deadline_min, seconds=1)
    job.tick(FakeProvider(), late)
    assert job.spec["phase"] == "done"


def test_restore_only_same_release(venue):
    job = armed(venue, [])
    again = SnipeJob(venue, "me", lambda result: None)
    again.restore()
    assert again.spec and again.spec["phase"] == "armed"
    assert again.spec["targetDate"] == next_release(venue.rules)["targetDate"]


def test_arm_rejects_unknown_category(venue):
    with pytest.raises(ValueError):
        SnipeJob(venue, "me", lambda r: None).arm("籃球", [{"courtId": "A", "time": "19:00"}])


def test_watch_grabs_first_matching_slot(venue, monkeypatch):
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    grabs = []
    provider = FakeProvider(released_date="2026-10-10")
    provider.open = {"B": ["08:00", "20:00"]}
    job = WatchJob(venue, {"category": "羽球", "date": "2026-10-10", "timeFrom": "18:00"}, grabs.append)
    job.tick(provider)
    assert provider.submitted == [("B", "2026-10-10", "20:00 - 21:00")]
    assert not job.running and grabs


def test_any_court_takes_first_free(venue):
    grabs = []
    job = armed(venue, grabs, [{"courtId": "", "time": "19:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"B": ["19:00"]}
    job.tick(provider, opens(job) - timedelta(seconds=30))
    assert job.spec["phase"] == "ready" and provider.prepared == ["A"]
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert provider.submitted == [("B", job.spec["targetDate"], "19:00 - 20:00")]
    assert job.state()["targets"][0]["bookedName"] == "場B"


def test_any_court_skips_court_claimed_by_other_target(venue):
    job = armed(venue, [], [{"courtId": "A", "time": "19:00"}, {"courtId": "", "time": "20:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["20:00"], "B": ["20:00"]}  # A@19 is gone, A@20 must not be used
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert provider.submitted == [("B", job.spec["targetDate"], "20:00 - 21:00")]


def test_two_any_targets_get_different_courts(venue):
    job = armed(venue, [], [{"courtId": "", "time": "19:00"}, {"courtId": "", "time": "20:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00", "20:00"], "B": ["20:00"]}
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert [item[0] for item in provider.submitted] == ["A", "B"]
    assert job.spec["phase"] == "done"


def test_any_court_keeps_polling_when_nothing_free(venue):
    job = armed(venue, [], [{"courtId": "", "time": "19:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert not provider.submitted and job.spec["phase"] == "firing"


def test_any_court_falls_back_when_search_fails(venue):
    class NoSearch(FakeProvider):
        def search(self, *args):
            raise RuntimeError("請登入")

    job = armed(venue, [], [{"courtId": "", "time": "19:00"}])
    provider = NoSearch(released_date=job.spec["targetDate"])
    provider.open = {"B": ["19:00"]}
    job.tick(provider, opens(job) - timedelta(seconds=30))
    job.tick(provider, opens(job) + timedelta(seconds=1))
    job._next_at = 0
    job.tick(provider, opens(job) + timedelta(seconds=2))
    assert provider.submitted == [("B", job.spec["targetDate"], "19:00 - 20:00")]


def alerting(venue, targets=None):
    alerts = []
    job = SnipeJob(venue, "me", lambda result: None, lambda title, body: alerts.append((title, body)))
    job.arm("羽球", targets or [{"courtId": "A", "name": "A", "time": "19:00"}])
    job._next_at = 0
    return job, alerts


def test_login_checked_after_arming_and_before_release(venue):
    job, alerts = alerting(venue)
    provider = FakeProvider(logged_in=False)
    job.tick(provider, opens(job) - timedelta(hours=8))
    assert job.spec["phase"] == "need_login" and len(alerts) == 1 and "登入已失效" in alerts[0][0]

    job._next_at = 0
    job.tick(provider, opens(job) - timedelta(hours=7))
    assert len(alerts) == 1  # still logged out: no repeat

    provider.logged_in = True
    job._next_at = 0
    job.tick(provider, opens(job) - timedelta(hours=6))
    assert job.spec["phase"] == "armed"

    provider.logged_in = False  # drops again near release: alert again
    job.tick(provider, opens(job) - timedelta(minutes=venue.rules.login_check_lead_min - 1))
    assert job.spec["phase"] == "need_login" and len(alerts) == 2


def test_logged_in_checks_are_silent_and_not_repeated(venue):
    job, alerts = alerting(venue)
    calls = []
    provider = FakeProvider()
    provider.refresh_session = lambda: calls.append(1) or {"loggedIn": True, "banner": ""}
    for hours in (8, 7, 6):
        job.tick(provider, opens(job) - timedelta(hours=hours))
    job.tick(provider, opens(job) - timedelta(minutes=5))
    assert len(calls) == 2 and not alerts and job.spec["phase"] == "armed"


def test_throttle_alerts_once(venue):
    job, alerts = alerting(venue)
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.throttle = True
    job._prepared = True
    for _ in range(3):
        job._next_at = 0
        job.tick(provider, opens(job) + timedelta(seconds=1))
    assert [title for title, _ in alerts] == ["測試中心 回報操作太頻繁"]


def test_login_drop_while_firing_alerts(venue):
    class Dropped(FakeProvider):
        def submit(self, court, query_date, slot):
            raise NeedLogin("請登入")

    job, alerts = alerting(venue)
    provider = Dropped(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert alerts and "登入已失效" in alerts[0][0]


def test_miss_at_deadline_alerts(venue):
    job, alerts = alerting(venue)
    job._prepared = True
    job.tick(FakeProvider(), opens(job) + timedelta(minutes=venue.rules.snipe_deadline_min, seconds=1))
    assert alerts[-1][0].startswith("沒搶到") and "A 19:00" in alerts[-1][1]


def test_full_success_sends_no_miss_alert(venue):
    job, alerts = alerting(venue)
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}
    job._prepared = True
    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert job.spec["phase"] == "done" and not alerts


def test_armed_period_rechecks_login(monkeypatch):
    recheck_venue = Venue(
        id="t",
        name="測試中心",
        provider="fake",
        categories=["羽球"],
        rules=BookingRules(poll_sec=0, login_recheck_min=30),
    )
    job, alerts = alerting(recheck_venue)
    calls = []
    provider = FakeProvider()
    provider.refresh_session = lambda: calls.append(1) or {"loggedIn": True, "banner": ""}
    mono = [5000.0]
    monkeypatch.setattr("sportflex.core.engine.time.monotonic", lambda: mono[0])
    base = opens(job) - timedelta(hours=8)
    job.tick(provider, base)
    assert len(calls) == 1
    mono[0] += 20 * 60
    job.tick(provider, base + timedelta(minutes=20))
    assert len(calls) == 1
    mono[0] += 20 * 60
    job.tick(provider, base + timedelta(minutes=40))
    assert len(calls) == 2 and not alerts


def test_partial_success_alert(venue):
    job, alerts = alerting(venue, [{"courtId": "A", "name": "A", "time": "19:00"}, {"courtId": "B", "name": "B", "time": "20:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}
    job._prepared = True
    job._next_at = 0
    job.tick(provider, opens(job) + timedelta(seconds=1))
    job._next_at = 0
    job.tick(provider, opens(job) + timedelta(minutes=venue.rules.snipe_deadline_min, seconds=1))
    assert alerts and alerts[-1][0].startswith("部分搶到")
    assert "已搶到" in alerts[-1][1] and "未搶到" in alerts[-1][1]


def _fast_venue():
    return Venue(
        id="t",
        name="測試中心",
        provider="fake",
        categories=["羽球"],
        rules=BookingRules(api_pause_sec=0),
    )


def test_iter_board_yields_each_court_before_the_rest_are_collected():
    venue = _fast_venue()
    provider = FakeProvider(released_date="2026-10-10")
    provider.open = {"A": ["09:00"], "B": ["21:00"]}
    events = list(engine.iter_board(provider, venue, "羽球", "2026-10-10"))
    assert [event["type"] for event in events] == ["meta", "court", "court"]
    assert [court["id"] for court in events[0]["courts"]] == ["A", "B"]
    assert events[1]["court"]["id"] == "A"
    assert any(slot["bookable"] and slot["time"].startswith("09:00") for slot in events[1]["court"]["slots"])
    assert events[1]["window"]["date"] == "2026-10-10"
    board = engine.build_board(provider, venue, "羽球", "2026-10-10")
    assert [court["id"] for court in board["courts"]] == ["A", "B"]
    assert board["window"]["date"] == "2026-10-10"


def test_iter_board_stops_before_the_next_court(venue):
    provider = FakeProvider(released_date="2026-10-10")
    calls = []
    original = provider.availability

    def availability(court, query_date=""):
        calls.append(court.id)
        return original(court, query_date)

    provider.availability = availability
    events = list(engine.iter_board(provider, venue, "羽球", "2026-10-10", stop=lambda: len(calls) >= 1))
    assert calls == ["A"]
    assert [event["type"] for event in events] == ["meta", "court"]
    assert events[-1]["court"]["id"] == "A"


def test_pause_returns_as_soon_as_the_scan_is_cancelled():
    assert engine._pause(2.5, lambda: True) is False


def test_stream_board_emits_each_court_and_a_cancelled_scan_does_not_query():
    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    worker = AccountWorker(
        Account(id="me", provider="fake", username_env="U", password_env="P", profile="x"),
        {},
        ConsoleNotifier(),
    )
    venue = _fast_venue()
    provider = FakeProvider(released_date="2026-10-10")
    events, _token = worker.stream_board(venue, "羽球", "2026-10-10")
    worker._queue.get_nowait()(provider)
    items = []
    while True:
        item = events.get_nowait()
        if item is None:
            break
        items.append(item)
    assert [item["type"] for item in items] == ["meta", "court", "court", "done"]

    listed = []
    provider.list_courts = lambda venue, category: listed.append(category) or []
    events, token = worker.stream_board(venue, "羽球", "2026-10-10", fresh=True)
    worker.cancel_board(token)
    worker._queue.get_nowait()(provider)
    cancelled = []
    while True:
        item = events.get_nowait()
        if item is None:
            break
        cancelled.append(item)
    assert cancelled == [{"type": "done"}] and listed == []


def _board_worker():
    import queue

    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    worker = AccountWorker(
        Account(id="me", provider="fake", username_env="U", password_env="P", profile="x"),
        {},
        ConsoleNotifier(),
    )
    provider = FakeProvider(released_date="2026-10-10")
    calls = {"list": 0, "slots": 0}
    original_list = provider.list_courts
    original_slots = provider.availability

    def list_courts(venue, category):
        calls["list"] += 1
        return original_list(venue, category)

    def availability(court, query_date=""):
        calls["slots"] += 1
        return original_slots(court, query_date)

    provider.list_courts = list_courts
    provider.availability = availability

    def play(events):
        try:
            job = worker._queue.get_nowait()
        except queue.Empty:
            job = None
        if job:
            job(provider)
        items = []
        while True:
            item = events.get_nowait()
            if item is None:
                break
            items.append(item)
        return items

    return worker, _fast_venue(), provider, calls, play


def test_same_board_query_is_reused_for_30_seconds():
    from sportflex.runtime.worker import BOARD_CACHE_SEC

    worker, venue, provider, calls, play = _board_worker()
    first = play(worker.stream_board(venue, "羽球", "2026-10-10")[0])
    assert [item["type"] for item in first] == ["meta", "court", "court", "done"]
    assert calls == {"list": 1, "slots": 2}

    active = worker._board_token
    cached_events, cached_token = worker.stream_board(venue, "羽球", "2026-10-10")
    assert worker._queue.empty()
    assert worker._board_token is active
    assert cached_token is not active
    second = play(cached_events)
    assert [item["type"] for item in second] == ["cached", "meta", "court", "court", "done"]
    assert second[0]["ageMs"] < 1000
    assert calls == {"list": 1, "slots": 2}

    provider.open = {"A": ["09:00"]}
    refreshed = play(worker.stream_board(venue, "羽球", "2026-10-10", fresh=True)[0])
    assert [item["type"] for item in refreshed] == ["meta", "court", "court", "done"]
    assert calls == {"list": 2, "slots": 4}
    assert any(slot["bookable"] for slot in refreshed[1]["court"]["slots"])

    replay = play(worker.stream_board(venue, "羽球", "2026-10-10")[0])
    assert replay[0]["type"] == "cached"
    assert calls == {"list": 2, "slots": 4}
    assert any(slot["bookable"] for slot in replay[2]["court"]["slots"])

    stored_at, snapshot = worker._board_cache[(venue.id, "羽球", "2026-10-10")]
    worker._board_cache[(venue.id, "羽球", "2026-10-10")] = (stored_at - BOARD_CACHE_SEC, snapshot)
    play(worker.stream_board(venue, "羽球", "2026-10-10")[0])
    assert calls == {"list": 3, "slots": 6}


def test_board_cache_also_serves_the_date_the_venue_resolved_and_board_reads_it():
    worker, venue, provider, calls, play = _board_worker()
    original = provider.availability

    def availability(court, query_date=""):
        return original(court, query_date or provider.released_date)

    provider.availability = availability
    items = play(worker.stream_board(venue, "羽球", "")[0])
    assert calls["slots"] == 2
    assert items[1]["window"]["date"] == "2026-10-10"

    again = play(worker.stream_board(venue, "羽球", "2026-10-10")[0])
    assert again[0]["type"] == "cached"
    assert calls["slots"] == 2

    payload = worker.board(venue, "羽球", "2026-10-10")
    assert worker._queue.empty()
    assert [court["id"] for court in payload["courts"]] == ["A", "B"]
    assert payload["window"]["date"] == "2026-10-10"
    assert calls["slots"] == 2

    other = play(worker.stream_board(venue, "羽球", "2026-10-11")[0])
    assert other[0]["type"] == "meta"
    assert calls["slots"] == 4


def test_failed_or_cancelled_board_scan_is_not_cached():
    worker, venue, provider, calls, play = _board_worker()
    provider.throttle = True
    failed = play(worker.stream_board(venue, "羽球", "2026-10-10")[0])
    assert [item["type"] for item in failed] == ["meta", "error"]
    assert worker._board_cache == {}
    assert calls == {"list": 1, "slots": 1}

    provider.throttle = False
    events, token = worker.stream_board(venue, "羽球", "2026-10-10")
    worker.cancel_board(token)
    cancelled = play(events)
    assert cancelled == [{"type": "done"}]
    assert worker._board_cache == {}
    assert calls == {"list": 1, "slots": 1}

    play(worker.stream_board(venue, "羽球", "2026-10-10")[0])
    assert calls == {"list": 2, "slots": 3}
