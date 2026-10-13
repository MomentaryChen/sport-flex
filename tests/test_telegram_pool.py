import pytest

from sportflex.core.events import EventStore
from sportflex.core.notify import ConsoleNotifier
from sportflex.runtime.pool import WorkerPool
from sportflex.runtime.telegram_bot import TelegramBot


COURTS = [
    {"id": "A", "name": "羽球場_A", "minPrice": "290"},
    {"id": "B", "name": "羽球場_B", "minPrice": "490"},
]


def board(venue, category, query_date, *, date_override=None):
    shown = date_override if date_override is not None else query_date
    return {
        "venue": venue.id,
        "category": category,
        "window": {"start": "2026-10-13", "end": "2026-10-26", "date": shown},
        "courts": [
            {
                "id": "A",
                "name": "羽球場_A",
                "slots": [
                    {"time": "19:00 - 20:00", "bookable": True, "status": "", "price": "490", "raw": {"lid": "XDSC"}},
                    {"time": "20:00 - 21:00", "bookable": False, "status": "已被預約", "price": "", "raw": {}},
                ],
            },
            {
                "id": "B",
                "name": "羽球場_B",
                "slots": [
                    {"time": "19:00 - 20:00", "bookable": True, "status": "", "price": "490", "raw": {}},
                ],
            },
        ],
    }


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr("sportflex.core.storage.SNIPES_DIR", tmp_path)
    store = EventStore(":memory:")
    bot = TelegramBot("token", "1", api=lambda *args, **kwargs: {})
    built = WorkerPool(ConsoleNotifier(), store, bot)
    worker = built.workers["default"]
    worker.courts = lambda venue, category: {"courts": COURTS}
    worker.board = lambda venue, category, query_date: board(venue, category, query_date)
    worker.grabbed = []

    def grab(venue, category, court_id, query_date, slot):
        worker.grabbed.append((court_id, query_date, slot.time, slot.raw))
        return {
            "venueName": venue.name,
            "court": "羽球場_A",
            "date": query_date,
            "time": slot.time,
            "price": slot.price,
            "order": {"no": "99", "paymentUrl": "https://pay.example", "timeoutSec": 590},
        }

    def set_snipe(venue, category, targets):
        job = worker.snipes[venue.id]
        if targets is None:
            return job.cancel()
        return job.arm(category or venue.categories[0], targets)

    worker.grab = grab
    worker.set_snipe = set_snipe
    built.worker = worker
    return built


def test_slots_and_release(pool):
    text = pool.slots_text(None, None, "羽球", "2026-10-20", "18:00", "23:00")
    assert "19:00 - 20:00  羽球場_A $490" in text
    assert "19:00 - 20:00  羽球場_B $490" in text
    assert "20:00 - 21:00" not in text
    assert "下一波" in pool.release_text(None)
    assert "新北市新店" in pool.release_text("xindian")


def test_arm_cancel_and_status(pool):
    text = pool.arm_text(None, None, None, [{"court": "", "time": "19:00"}, {"court": "A", "time": "20:00"}])
    assert "任一場 19:00" in text and "羽球場_A 20:00" in text
    assert "取消：/cancel" in text
    status = pool.status_text()
    assert "default" in status and "羽球場_A 20:00" in status
    assert pool.snipe_text(None, None) == text
    cancelled = pool.cancel_text(None, None)
    assert cancelled.startswith("已取消搶訂")
    assert "目前沒有搶訂" in pool.snipe_text(None, None)


def test_arm_rejects_too_many_targets(pool):
    with pytest.raises(ValueError):
        pool.arm_text(
            None,
            None,
            "羽球",
            [{"court": "", "time": "18:00"}, {"court": "", "time": "19:00"}, {"court": "", "time": "20:00"}],
        )


def test_book_preview_requires_one_court_then_places(pool):
    with pytest.raises(ValueError, match="多面可訂"):
        pool.preview_book(None, None, None, "", "2026-10-20", "19:00")
    plan = pool.preview_book(None, "xindian", "羽球", "A", "2026-10-20", "19:00")
    assert "羽球場_A" in plan["text"] and "/ok" in plan["text"]
    assert plan["courtId"] == "A"
    placed = pool.place_book(plan)
    assert "https://pay.example" in placed
    assert pool.worker.grabbed == [("A", "2026-10-20", "19:00 - 20:00", {"lid": "XDSC"})]


def test_book_outside_window(pool):
    pool.worker.board = lambda venue, category, query_date: board(venue, category, query_date, date_override="2026-10-13")
    with pytest.raises(ValueError, match="不在可預約範圍"):
        pool.preview_book(None, None, None, "A", "2026-10-20", "19:00")


def test_history_reads_runs(pool):
    assert pool.history_text(None) == "最近沒有開搶紀錄"
    pool.events.record_snipe_run(
        {
            "account": "default",
            "venue": "xindian",
            "targetDate": "2026-10-20",
            "opensAt": "2026-10-06T06:00:00+08:00",
            "result": "partial",
            "booked": 1,
            "total": 2,
            "reason": "時間到",
            "targets": [],
        }
    )
    text = pool.history_text("default")
    assert "2026-10-20" in text and "部分搶到 1/2" in text
