from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from sportflex.paths import OUTPUT_DIR

DB_PATH = OUTPUT_DIR / "sportflex.db"
RETENTION_DAYS = 30
_PURGE_EVERY_SEC = 6 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,           -- unix time, used for filtering and retention
    at TEXT NOT NULL,           -- local ISO time, for people
    account TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    job TEXT NOT NULL DEFAULT '',     -- snipe / watch / manual / session
    kind TEXT NOT NULL,               -- note, grab, alert, login, snipe_result ...
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_scope ON events (account, venue, ts);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, ts);

-- One row per snipe wave. Seconds are relative to the release time (opens_at); negative = before.
CREATE TABLE IF NOT EXISTS snipe_runs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    account TEXT NOT NULL,
    venue TEXT NOT NULL,
    target_date TEXT NOT NULL,
    opens_at TEXT NOT NULL,
    result TEXT NOT NULL,             -- booked / partial / missed
    booked INTEGER NOT NULL,
    total INTEGER NOT NULL,
    fired_sec REAL,                   -- first query after release
    released_sec REAL,                -- first time the site showed the target day
    first_booked_sec REAL,            -- first confirm page reached
    finished_sec REAL,
    reason TEXT NOT NULL DEFAULT '',
    targets TEXT NOT NULL DEFAULT '[]'  -- per target: court, time, status, seenSec, bookedSec, submitSec, attempts
);
CREATE INDEX IF NOT EXISTS snipe_runs_ts ON snipe_runs (ts);
"""


class EventStore:
    """SQLite log of what happened, kept for RETENTION_DAYS. Recording never raises: a broken
    disk must not stop a snipe. One connection guarded by a lock; writes are tiny."""

    def __init__(self, path: Path | str = DB_PATH, retention_days: int = RETENTION_DAYS) -> None:
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._purged_at = 0.0
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=3000")
        self._db.executescript(SCHEMA)
        self.purge()

    def record(
        self,
        kind: str,
        message: str = "",
        *,
        account: str = "",
        venue: str = "",
        job: str = "",
        level: str = "info",
        data: dict | None = None,
    ) -> None:
        now = time.time()
        self._write(
            "INSERT INTO events (ts, at, account, venue, job, kind, level, message, data) VALUES (?,?,?,?,?,?,?,?,?)",
            (now, _local(now), account, venue, job, kind, level, message, _json(data or {})),
        )

    def record_snipe_run(self, run: dict) -> None:
        self._write(
            "INSERT INTO snipe_runs (ts, account, venue, target_date, opens_at, result, booked, total,"
            " fired_sec, released_sec, first_booked_sec, finished_sec, reason, targets)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                run["account"],
                run["venue"],
                run["targetDate"],
                run["opensAt"],
                run["result"],
                run["booked"],
                run["total"],
                run.get("firedSec"),
                run.get("releasedSec"),
                run.get("firstBookedSec"),
                run.get("finishedSec"),
                run.get("reason", ""),
                _json(run.get("targets", [])),
            ),
        )

    def events(
        self,
        *,
        account: str = "",
        venue: str = "",
        kind: str = "",
        days: float = RETENTION_DAYS,
        limit: int = 200,
    ) -> list[dict]:
        where, args = _scope(account, venue, days)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        rows = self._read(
            f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?", (*args, limit)
        )
        return [{**row, "data": json.loads(row["data"])} for row in rows]

    def snipe_runs(self, *, account: str = "", venue: str = "", days: float = RETENTION_DAYS, limit: int = 50) -> list[dict]:
        where, args = _scope(account, venue, days)
        rows = self._read(
            f"SELECT * FROM snipe_runs WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?", (*args, limit)
        )
        return [{**row, "targets": json.loads(row["targets"])} for row in rows]

    def purge(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        self._purged_at = time.time()
        self._write("DELETE FROM events WHERE ts < ?", (cutoff,), purge=False)
        self._write("DELETE FROM snipe_runs WHERE ts < ?", (cutoff,), purge=False)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _write(self, sql: str, args: tuple, purge: bool = True) -> None:
        try:
            with self._lock:
                self._db.execute(sql, args)
        except Exception as exc:
            print(f"[紀錄] 寫入資料庫失敗：{exc}", flush=True)
            return
        if purge and time.time() - self._purged_at > _PURGE_EVERY_SEC:
            self.purge()

    def _read(self, sql: str, args: tuple) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]


class NullEventStore(EventStore):
    """Records nothing; the default for jobs built without a store (tests, one-off CLI runs)."""

    def __init__(self) -> None:
        pass

    def record(self, *args, **kwargs) -> None:
        pass

    def record_snipe_run(self, run: dict) -> None:
        pass


def _scope(account: str, venue: str, days: float) -> tuple[list[str], list]:
    where, args = ["ts >= ?"], [time.time() - days * 86400]
    if account:
        where.append("account = ?")
        args.append(account)
    if venue:
        where.append("venue = ?")
        args.append(venue)
    return where, args


def _local(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
