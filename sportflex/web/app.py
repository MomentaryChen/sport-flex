from __future__ import annotations

import asyncio
import json
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
from sportflex.runtime.pool import WorkerPool

INDEX = Path(__file__).resolve().parent / "static" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = WorkerPool()
    await asyncio.to_thread(pool.start)
    app.state.pool = pool
    yield


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


class WatchBody(Target):
    enabled: bool
    category: str | None = None
    date: str = ""
    timeFrom: str = ""
    timeTo: str = ""
    courtId: str = ""
    interval: int | None = None


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
):
    found, worker = pool(request).resolve(venue, account)
    return {"ok": True, **worker.board(found, category or found.categories[0], date)}


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


@app.get("/api/snipes")
def snipes(request: Request):
    """Every armed or finished snipe across venues and accounts."""
    return {"ok": True, "snipes": pool(request).snipes()}


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
        while not await request.is_disconnected():
            payload = {
                "snipe": worker.snipes[found.id].state(),
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


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    print(f"啟動瀏覽器中… 完成後打開 {url}（API 文件：{url}docs）", flush=True)
    if open_browser:
        threading.Thread(target=_open_when_up, args=(port, url), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


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
