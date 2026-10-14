from __future__ import annotations

import asyncio
import json
import os
import queue
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from sportflex.core.models import Slot
from sportflex.core.rules import next_release
from sportflex.core.venue import get_venue
from sportflex.providers.base import ProviderError
from sportflex.core.identity import server_label
from sportflex.paths import OUTPUT_DIR
from sportflex.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from sportflex.runtime.pool import WorkerPool, bot_from_env

INDEX = Path(__file__).resolve().parent / "static" / "index.html"
SHUTDOWN = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = WorkerPool()
    await asyncio.to_thread(pool.start)
    app.state.pool = pool
    yield
    print("關閉瀏覽器中…", flush=True)
    await asyncio.to_thread(pool.stop)


app = FastAPI(title="sport-flex", lifespan=lifespan)


def pool(request: Request) -> WorkerPool:
    return request.app.state.pool


@app.exception_handler(ValueError)
@app.exception_handler(ProviderError)
async def _bad_request(_request: Request, exc: Exception):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.exception_handler(Exception)
async def _server_error(_request: Request, exc: Exception):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ---- request bodies ------------------------------------------------------


class Target(BaseModel):
    venue: str | None = None
    account: str | None = None


class LoginBody(Target):
    code: str


class SlotBody(BaseModel):
    time: str
    bookable: bool = True
    status: str = ""
    price: str = ""
    raw: dict = Field(default_factory=dict)


class GrabBody(Target):
    category: str
    courtId: str
    date: str
    slot: SlotBody


class PressBody(Target):
    label: str


class SnipeTarget(BaseModel):
    courtId: str
    name: str = ""
    time: str


class SnipeBody(Target):
    enabled: bool
    category: str | None = None
    targets: list[SnipeTarget] = Field(default_factory=list)


class RoutineBody(Target):
    enabled: bool
    category: str | None = None
    weekdays: list[int] = Field(default_factory=list)
    targets: list[SnipeTarget] = Field(default_factory=list)


class WatchBody(Target):
    enabled: bool
    category: str | None = None
    date: str = ""
    timeFrom: str = ""
    timeTo: str = ""
    courtId: str = ""
    interval: int | None = None
    untilFull: bool = False


# ---- routes ---------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX, media_type="text/html; charset=utf-8")


@app.get("/api/meta")
def meta(request: Request):
    return {"ok": True, **pool(request).meta()}


@app.get("/api/release")
def release(venue: str | None = None):
    found = get_venue(venue)
    return {"ok": True, "venue": found.id, "rulesText": found.rules.describe(), **next_release(found.rules)}


@app.get("/api/session")
def session(request: Request, venue: str | None = None, account: str | None = None):
    _venue, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.session_info()}


@app.post("/api/captcha")
def captcha(request: Request, body: Target):
    _venue, worker = pool(request).resolve(body.venue, body.account)
    return {"ok": True, **worker.captcha()}


@app.post("/api/login")
def login(request: Request, body: LoginBody):
    _venue, worker = pool(request).resolve(body.venue, body.account)
    return worker.login(body.code)


@app.get("/api/courts")
def courts(request: Request, venue: str | None = None, account: str | None = None, category: str | None = None):
    found, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.courts(found, category or found.categories[0])}


@app.get("/api/board")
def board(
    request: Request,
    venue: str | None = None,
    account: str | None = None,
    category: str | None = None,
    date: str = "",
    fresh: bool = False,
):
    found, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.board(found, category or found.categories[0], date, fresh=fresh)}


_QUEUE_EMPTY = object()


def _poll_queue(events: queue.Queue):
    try:
        return events.get(timeout=0.4)
    except queue.Empty:
        return _QUEUE_EMPTY


@app.get("/api/board/stream")
async def board_stream(
    request: Request,
    venue: str | None = None,
    account: str | None = None,
    category: str | None = None,
    date: str = "",
    fresh: bool = False,
):
    """Server-sent events: one court as soon as its slots are known, then the rest.

    The same venue, account, sport, and date is served from a 30s cache.
    ``fresh=1`` (the reload button) scans the venue again.
    """
    found, worker = pool(request).resolve(venue, account)
    events, token = worker.stream_board(found, category or found.categories[0], date, fresh=fresh)

    async def generate():
        try:
            while not SHUTDOWN.is_set() and not await request.is_disconnected():
                item = await asyncio.to_thread(_poll_queue, events)
                if item is _QUEUE_EMPTY:
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("type") in {"done", "error"}:
                    break
        finally:
            worker.cancel_board(token)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/grab")
def grab(request: Request, body: GrabBody):
    found, worker = pool(request).resolve(body.venue, body.account)
    slot = Slot(**body.slot.model_dump())
    return worker.grab(found, body.category, body.courtId, body.date, slot)


@app.post("/api/press")
def press(request: Request, body: PressBody):
    _venue, worker = pool(request).resolve(body.venue, body.account)
    return {"ok": True, **worker.press(body.label)}


@app.get("/api/snipe")
def snipe_state(request: Request, venue: str | None = None, account: str | None = None):
    found, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.snipes[found.id].state(), "lastGrab": worker.last_grab}


@app.post("/api/snipe")
def snipe(request: Request, body: SnipeBody):
    found, worker = pool(request).resolve(body.venue, body.account)
    targets = [item.model_dump() for item in body.targets] if body.enabled else None
    return {"ok": True, **worker.set_snipe(found, body.category, targets), "lastGrab": worker.last_grab}


@app.get("/api/routine")
def routine_state(request: Request, venue: str | None = None, account: str | None = None):
    found, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.routine_state(found)}


@app.post("/api/routine")
def routine(request: Request, body: RoutineBody):
    found, worker = pool(request).resolve(body.venue, body.account)
    spec = None
    if body.enabled:
        spec = {
            "category": body.category or found.categories[0],
            "weekdays": body.weekdays,
            "targets": [item.model_dump() for item in body.targets],
        }
    return {"ok": True, **worker.set_routine(found, spec)}


@app.get("/api/snipes")
def snipes(request: Request):
    """Every armed or finished snipe across venues and accounts."""
    return {"ok": True, "snipes": pool(request).snipes()}


@app.get("/api/history")
def history(
    request: Request,
    account: str = "",
    venue: str = "",
    kind: str = "",
    days: float = 7,
    limit: int = 200,
):
    """Recorded events, newest first (kept 30 days)."""
    store = pool(request).events
    return {"ok": True, "events": store.events(account=account, venue=venue, kind=kind, days=days, limit=limit)}


@app.get("/api/snipe-runs")
def snipe_runs(request: Request, account: str = "", venue: str = "", days: float = 30, limit: int = 50):
    """One row per snipe wave: result and seconds after release for each step."""
    store = pool(request).events
    return {"ok": True, "runs": store.snipe_runs(account=account, venue=venue, days=days, limit=limit)}


@app.get("/api/snipe-stats")
def snipe_stats(request: Request, account: str = "", venue: str = "", days: float = 30):
    """Hit rate, average seconds until the site releases, and which court is easiest to book.

    `offsetSec` is the median release time (once there are enough waves). The snipe uses it
    as the delay before the first query.
    """
    store = pool(request).events
    return {"ok": True, "stats": store.snipe_stats(account=account, venue=venue, days=days)}


@app.get("/api/watch")
def watch_state(request: Request, venue: str | None = None, account: str | None = None):
    _venue, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.watch_state(), "lastGrab": worker.last_grab}


@app.post("/api/watch")
def watch(request: Request, body: WatchBody):
    found, worker = pool(request).resolve(body.venue, body.account)
    spec = None
    if body.enabled:
        spec = body.model_dump(exclude={"venue", "account", "enabled"})
        spec["category"] = spec["category"] or found.categories[0]
    return {"ok": True, **worker.set_watch(found if spec else None, spec)}


@app.get("/api/events")
async def events(request: Request, venue: str | None = None, account: str | None = None):
    """Server-sent events: snipe/watch state and the latest grab, pushed when they change."""
    found, worker = pool(request).resolve(venue, account)

    async def stream():
        last = ""
        # An open tab never disconnects on its own; stop when the server shuts down (Ctrl+C).
        while not SHUTDOWN.is_set() and not await request.is_disconnected():
            payload = {
                "snipe": worker.snipes[found.id].state(),
                "routine": worker.routine_state(found),
                "watch": worker.watch_state(),
                "session": worker.session_info(),
                "lastGrab": worker.last_grab,
            }
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if text != last:
                last = text
                yield f"data: {text}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def serve(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True) -> int:
    import uvicorn

    # Before workers start, so Telegram captchas and /status use this name.
    os.environ.setdefault("SPORT_FLEX_INSTANCE", f"{socket.gethostname()}:{port}")
    # Same machine: the lock refuses a second UI before it opens browsers or touches Telegram.
    # Another machine does not share the file; the bot probe below is that check.
    lock = InstanceLock(OUTPUT_DIR / "instance.lock")
    try:
        lock.acquire()
    except InstanceAlreadyRunning as exc:
        print(str(exc), flush=True)
        return 1
    try:
        bot = bot_from_env()
        if bot is not None:
            refusal = bot.probe_other_instance()
            if refusal:
                print(refusal, flush=True)
                return 1
        url = f"http://127.0.0.1:{port}/"
        print(
            f"搶場服務聽在 {host}:{port}（本機開啟 {url}，API 文件：{url}docs，Telegram 這台是 {server_label()}）",
            flush=True,
        )
        if open_browser:
            threading.Thread(target=_open_when_up, args=(port, url), daemon=True).start()

        class Server(uvicorn.Server):
            def handle_exit(self, sig, frame) -> None:
                SHUTDOWN.set()  # Ctrl+C: let open event streams end instead of waiting on them
                super().handle_exit(sig, frame)

        config = uvicorn.Config(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
        Server(config).run()
        return 0
    finally:
        lock.release()


def _open_when_up(port: int, url: str) -> None:
    """Workers start before uvicorn binds the port; open the page once it answers."""
    for _ in range(240):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            time.sleep(0.5)
            continue
        print(f"搶場畫面：{url}", flush=True)
        webbrowser.open(url)
        return
