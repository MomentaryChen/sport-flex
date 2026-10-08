import time
from datetime import timedelta

import pytest

from sportflex.core.engine import SnipeJob
from sportflex.core.events import EventStore
from tests.test_engine import FakeProvider, opens, venue  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def no_disk(tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path)


@pytest.fixture
def store():
    store = EventStore(":memory:")
    yield store
    store.close()


def test_records_and_filters(store):
    store.record("alert", "登入失效", account="me", venue="t", level="warn", data={"body": "x"})
    store.record("note", "其他", account="other", venue="t")
    found = store.events(account="me")
    assert len(found) == 1 and found[0]["kind"] == "alert" and found[0]["data"] == {"body": "x"}
    assert [event["kind"] for event in store.events(kind="note")] == ["note"]


def test_purges_older_than_retention(store):
    store.record("note", "old")
    store._db.execute("UPDATE events SET ts = ?", (time.time() - 31 * 86400,))
    store.record("note", "new")
    store.purge()
    assert [event["message"] for event in store.events(days=365)] == ["new"]


def test_write_failure_never_raises(store):
    store.close()
    store.record("note", "after close")  # prints a warning, does not raise


def test_snipe_run_records_result_and_seconds(venue, store):  # noqa: F811
    job = SnipeJob(venue, "me", lambda result: None, events=store)
    job.arm("羽球", [{"courtId": "A", "name": "A", "time": "19:00"}, {"courtId": "B", "name": "B", "time": "20:00"}])
    provider = FakeProvider(released_date=job.spec["targetDate"])
    provider.open = {"A": ["19:00"]}

    job.tick(provider, opens(job) - timedelta(seconds=30))
    job.tick(provider, opens(job) + timedelta(seconds=1.5))  # A booked, B not open yet
    job._next_at = 0
    job.tick(provider, opens(job) + timedelta(minutes=10))  # past the deadline

    [run] = store.snipe_runs()
    assert run["result"] == "partial" and (run["booked"], run["total"]) == (1, 2)
    assert run["fired_sec"] == pytest.approx(1.5, abs=0.2)
    assert run["released_sec"] == pytest.approx(1.5, abs=0.2)
    assert run["first_booked_sec"] == pytest.approx(1.5, abs=0.2)
    assert run["finished_sec"] == pytest.approx(600, abs=0.5)
    first, second = run["targets"]
    assert first["status"] == "booked" and first["bookedSec"] >= first["seenSec"] and first["submitSec"] >= 0
    assert first["attempts"] == 1
    assert second["status"] != "booked" and second["bookedSec"] is None

    kinds = [event["kind"] for event in store.events()]
    for kind in ("armed", "ready", "fired", "released", "submit", "booked", "snipe_result"):
        assert kind in kinds


def test_miss_before_firing_still_records_run(venue, store):  # noqa: F811
    job = SnipeJob(venue, "me", lambda result: None, events=store)
    job.arm("羽球", [{"courtId": "Z", "name": "Z", "time": "19:00"}])
    job.tick(FakeProvider(), opens(job) - timedelta(seconds=30))  # court Z doesn't exist
    [run] = store.snipe_runs()
    assert run["result"] == "missed" and run["fired_sec"] is None and "找不到" in run["reason"]
