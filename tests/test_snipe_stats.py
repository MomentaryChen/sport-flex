import time
from datetime import datetime, timedelta

import pytest

from sportflex.core.events import EventStore
from sportflex.core.snipe_stats import offset_from_samples, stats_lines, summarize_runs
from sportflex.core.engine import SnipeJob
from tests.test_engine import FakeProvider, opens, venue  # noqa: F401


@pytest.fixture(autouse=True)
def no_disk(tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path)


@pytest.fixture
def store():
    found = EventStore(":memory:")
    yield found
    found.close()


def wave(**over):
    run = {
        "account": "me",
        "venue": "t",
        "targetDate": "2026-10-20",
        "opensAt": "2026-10-06T06:00:00+08:00",
        "result": "missed",
        "booked": 0,
        "total": 1,
        "fired_sec": 1.0,
        "released_sec": None,
        "reason": "",
        "targets": [],
    }
    run.update(over)
    return run


def target(court_id, status="booked", **over):
    item = {
        "courtId": court_id,
        "name": f"場{court_id}",
        "time": "19:00",
        "status": status,
        "bookedName": f"場{court_id}" if status == "booked" else "",
        "seenSec": 1.0 if status == "booked" else None,
        "bookedSec": 1.4 if status == "booked" else None,
        "submitSec": 0.4 if status == "booked" else None,
        "attempts": 1,
    }
    item.update(over)
    return item


def test_summarize_hit_rate_release_and_best_court():
    runs = [
        wave(
            result="partial",
            booked=1,
            total=2,
            released_sec=1.0,
            targets=[target("A", seenSec=1.0), target("B", status="miss")],
        ),
        wave(
            result="booked",
            booked=2,
            total=2,
            released_sec=2.0,
            targets=[target("A", seenSec=1.2, bookedSec=1.5), target("B", seenSec=3.0, bookedSec=3.4)],
        ),
        wave(
            result="partial",
            booked=1,
            total=3,
            released_sec=3.0,
            targets=[
                target("A", seenSec=0.8, bookedSec=1.3),
                target("B", status="miss", seenSec=None),
                target("", status="booked", name="任一場", bookedName="場C", seenSec=1.1),
                target("D", status="skipped", seenSec=None, bookedSec=None, bookedName="", submitSec=None),
            ],
        ),
        wave(result="missed", fired_sec=None, targets=[]),
    ]
    stats = summarize_runs(runs)
    assert stats["waves"] == {
        "total": 4,
        "fired": 3,
        "full": 1,
        "partial": 2,
        "missed": 1,
        "landed": 3,
        "hitRate": 1.0,
    }
    assert stats["targets"] == {"tried": 6, "booked": 4, "skipped": 1, "hitRate": 0.6667}
    assert stats["release"]["avgSec"] == 2.0
    assert stats["release"]["medianSec"] == 2.0
    assert stats["release"]["minSec"] == 1.0 and stats["release"]["maxSec"] == 3.0
    assert [court["courtId"] for court in stats["courts"]] == ["A", "B"]
    best, other = stats["courts"]
    assert best["tried"] == 3 and best["booked"] == 3 and best["hitRate"] == 1
    assert best["avgSeenSec"] == 1.0
    assert other["booked"] == 1 and other["tried"] == 3 and other["avgSeenSec"] == 3.0
    assert stats["anyCourts"] == [{"name": "場C", "booked": 1}]


def test_offset_needs_three_samples_and_clamps():
    assert offset_from_samples([1.2, 1.8]) is None
    assert offset_from_samples([1.2, 1.8, 2.4]) == 1.8
    assert offset_from_samples([-0.2, -0.4, -0.6]) == -0.4
    assert offset_from_samples([9, 10, 11]) == 5.0
    assert offset_from_samples([-4, -3, -2]) == -1.0


def test_stats_fall_back_to_the_venue_when_the_account_is_thin(store):
    store.record_snipe_run(wave(releasedSec=9))
    for _ in range(3):
        store.record_snipe_run(wave(account="other", releasedSec=2, result="booked", booked=1, targets=[target("A")]))
    stats = store.snipe_stats(account="me", venue="t")
    assert stats["release"]["count"] == 1
    assert stats["offsetSec"] == 2.0 and stats["offsetScope"] == "venue" and stats["offsetSamples"] == 4
    own = store.snipe_stats(account="other", venue="t")
    assert own["offsetSec"] == 2.0 and own["offsetScope"] == "filter"
    assert "最好搶 場A 3/3" in stats_lines(own)[0]
    assert "平均 2.00 秒釋出" in stats_lines(own)[0]
    assert store.release_offset_sec(venue="t", account="me") == 2.0
    assert store.snipe_stats(account="nobody", venue="empty")["offsetSec"] is None


def test_first_query_waits_for_the_learned_offset(venue, store):  # noqa: F811
    for sec in (1.2, 1.8, 2.4):
        store.record_snipe_run(wave(releasedSec=sec, result="booked", booked=1, targets=[target("A")]))
    job = SnipeJob(venue, "me", lambda result: None, events=store)
    job.arm("羽球", [{"courtId": "A", "name": "場A", "time": "19:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}

    job.tick(provider, opens(job) - timedelta(seconds=30))
    assert job.spec["phase"] == "ready"
    assert any("開搶後 1.80 秒" in line for line in job.log.lines)

    job.tick(provider, opens(job) + timedelta(seconds=1))
    assert provider.submitted == [] and job.spec["phase"] == "ready"

    job.tick(provider, opens(job) + timedelta(seconds=1.8))
    assert provider.submitted == [("A", job.spec["targetDate"], "19:00 - 20:00")]


def test_thin_history_still_fires_at_the_advertised_open(venue, store):  # noqa: F811
    store.record_snipe_run(wave(releasedSec=4))
    store.record_snipe_run(wave(releasedSec=5))
    job = SnipeJob(venue, "me", lambda result: None, events=store)
    job.arm("羽球", [{"courtId": "A", "name": "場A", "time": "19:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}
    job.tick(provider, opens(job) + timedelta(seconds=0.2))
    assert provider.submitted


def test_idle_timeout_wakes_for_the_first_query(venue):  # noqa: F811
    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    worker = AccountWorker(
        Account(id="me", provider="fake", username_env="U", password_env="P", profile="x"),
        {venue.id: venue},
        ConsoleNotifier(),
    )
    assert worker._idle_timeout() == 1
    job = worker.snipes[venue.id]
    job.arm("羽球", [{"courtId": "A", "name": "場A", "time": "19:00"}])
    # More than a second out, the loop still wakes every second. Inside the last second it
    # sleeps only until the query, instead of possibly waking a second late.
    job.spec["opensAt"] = (datetime.now(job.rules.tz) + timedelta(seconds=2)).isoformat()
    assert worker._idle_timeout() == 1
    job.spec["opensAt"] = (datetime.now(job.rules.tz) + timedelta(seconds=0.4)).isoformat()
    assert 0.2 < worker._idle_timeout() < 0.5

    job.spec["phase"] = "firing"
    job._next_at = time.time() + 0.3
    assert 0.2 < worker._idle_timeout() < 0.45

    job.spec["phase"] = "ready"
    job.spec["opensAt"] = (datetime.now(job.rules.tz) - timedelta(seconds=1)).isoformat()
    assert worker._idle_timeout() == 0.05
