"""The order sheet offers a calendar file and a Google Calendar link."""

from __future__ import annotations

import json
import os
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import pytest

from datetime import datetime
from zoneinfo import ZoneInfo

from sportflex.core.calendar import booking_calendar
from sportflex.core.refund import plan_refund_reminder
from sportflex.core.rules import next_release
from sportflex.core.venue import get_venue
from sportflex.web.app import INDEX

STATIC = INDEX.parent


def _serve():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(STATIC), **kwargs)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _payload():
    venue = get_venue("xindian")
    result = {
        "venueName": venue.display_name,
        "court": "羽球場_A",
        "date": "2026-10-21",
        "time": "19:00 - 20:00",
        "price": "490",
        "image": "",
        "order": {
            "no": "CJC1",
            "paymentUrl": "https://pay.example/member",
            "carrier": "/ABC1234",
            "timeoutSec": 590,
            "warning": "",
        },
    }
    result["calendar"] = booking_calendar(venue, result)
    result["refundReminder"] = plan_refund_reminder(
        venue, result, datetime(2026, 10, 14, 12, tzinfo=ZoneInfo("Asia/Taipei"))
    )
    return venue, result


def test_target_times_follow_venue_hours():
    """A venue that closes at 21:00 must not offer a 21:00 start or a 06:00 start."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    venue = get_venue("xindian")
    public = venue.public()
    public["rules"] = {**public["rules"], "open_time": "09:00", "close_time": "21:00"}
    server = _serve()
    port = server.server_address[1]
    held = []
    try:
        with playwright_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                pytest.skip(f"Chrome not available: {exc}")
            page = browser.new_page(viewport={"width": 1100, "height": 900}, color_scheme="light")
            page.route("**/*", lambda route: _route(route, venue, held, public))
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
            page.wait_for_selector("#targets .t-time")
            options = page.locator("#targets .t-time option").all_text_contents()
            assert options[0].startswith("09:00")
            assert options[-1].startswith("20:00")
            assert all(not item.startswith(("06:00", "08:00", "21:00")) for item in options)
            assert page.locator("#targets .t-time").input_value() == "19:00"
            page.locator("#addTarget").click()
            assert page.locator("#targets .t-time").nth(1).input_value() == "20:00"
            for route in held:
                route.fulfill(status=204, body=b"")
            held.clear()
            browser.close()
    finally:
        for route in held:
            try:
                route.fulfill(status=204, body=b"")
            except Exception:
                pass
        server.shutdown()


def test_order_sheet_offers_calendar_file_and_google_link(tmp_path):
    playwright_api = pytest.importorskip("playwright.sync_api")
    venue, result = _payload()
    calendar = result["calendar"]
    server = _serve()
    port = server.server_address[1]
    held = []
    try:
        with playwright_api.sync_playwright() as playwright:
            try:
                demo = os.environ.get("SPORTFLEX_DEMO") == "1"
                launch = {"channel": "chrome", "headless": not demo}
                if demo:
                    launch["slow_mo"] = 300
                    launch["args"] = ["--window-position=80,40", "--window-size=1000,860"]
                browser = playwright.chromium.launch(**launch)
            except Exception as exc:
                pytest.skip(f"Chrome not available: {exc}")
            page = browser.new_page(viewport={"width": 760, "height": 900}, color_scheme="light")
            page.route("**/*", lambda route: _route(route, venue, held))
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
            page.wait_for_function("() => typeof showResult === 'function' && document.getElementById('venueInfo').textContent.includes('一號館')")
            page.evaluate("(result) => showResult(result)", result)

            sheet = page.locator("#modal .sheet")
            sheet.get_by_text("取消規則：", exact=False).wait_for()
            refund_text = sheet.locator("#resultRefund").inner_text()
            assert "前 2 天內不退費" in refund_text
            assert "10/18（週日） 09:00" in refund_text
            assert "要不要取消" in refund_text
            assert "CJC1" in sheet.locator("#resultText").inner_text()
            google = sheet.get_by_role("link", name="加入 Google 行事曆")
            assert google.get_attribute("href") == calendar["googleUrl"]
            assert google.get_attribute("target") == "_blank"
            pay = sheet.get_by_role("link", name="開啟會員中心（待付款）")
            assert pay.get_attribute("href") == "https://pay.example/member"

            page.set_viewport_size({"width": 390, "height": 844})
            _buttons_fit(sheet)
            page.set_viewport_size({"width": 760, "height": 900})
            page.evaluate("() => { document.getElementById('resultImage').style.display = 'none'; }")
            artifacts = Path("/opt/cursor/artifacts")
            if artifacts.is_dir():
                sheet.screenshot(path=str(artifacts / "order-calendar.png"))
                sheet.locator("#resultRefund").screenshot(path=str(artifacts / "refund-reminder.png"))

            with page.expect_download() as download_info:
                sheet.get_by_role("button", name="下載 .ics").click()
            download = download_info.value
            saved = Path(download.path())
            assert download.suggested_filename == calendar["filename"]
            assert saved.read_bytes() == calendar["ics"].encode("utf-8")
            assert "前 2 天內不退費" in saved.read_bytes().decode("utf-8")

            page.evaluate(
                "(result) => showResult(result)",
                {"court": "羽球場_A", "date": "2026-10-21", "time": "19:00 - 20:00", "url": "https://example/confirm", "image": "", "buttons": ["送出訂單"]},
            )
            assert page.locator("#resultRefund").is_hidden()
            assert page.get_by_role("button", name="下載 .ics").count() == 0
            assert page.get_by_role("link", name="加入 Google 行事曆").count() == 0
            assert page.get_by_role("button", name="送出訂單").count() == 1

            if os.environ.get("SPORTFLEX_DEMO") == "1":
                page.evaluate("(result) => showResult(result)", result)
                page.evaluate("() => { document.getElementById('resultImage').style.display = 'none'; }")
                page.wait_for_timeout(3500)
                with page.expect_download():
                    sheet.get_by_role("button", name="下載 .ics").click()
                page.wait_for_timeout(1200)

            for route in held:
                route.fulfill(status=204, body=b"")
            held.clear()
            browser.close()
    finally:
        for route in held:
            try:
                route.fulfill(status=204, body=b"")
            except Exception:
                pass
        server.shutdown()


def test_watch_until_full_option_is_sent_with_the_watch(tmp_path):
    playwright_api = pytest.importorskip("playwright.sync_api")
    venue, _result = _payload()
    server = _serve()
    port = server.server_address[1]
    held = []
    watches = []
    try:
        with playwright_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                pytest.skip(f"Chrome not available: {exc}")
            page = browser.new_page(viewport={"width": 1100, "height": 900}, color_scheme="light")
            page.route("**/*", lambda route: _watch_route(route, venue, held, watches))
            page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
            page.wait_for_function("() => document.getElementById('venueInfo').textContent.includes('一號館')")
            page.wait_for_selector("#dates .date")
            box = page.locator("#watchBox")
            box.scroll_into_view_if_needed()
            assert "送出一筆訂單，然後停止" in page.locator("#watchHint").inner_text()
            assert "2 小時" in page.locator("#watchUntilFullText").inner_text()
            artifacts = Path("/opt/cursor/artifacts")
            if artifacts.is_dir():
                box.screenshot(path=str(artifacts / "watch-until-full-off.png"))

            page.locator("#watchUntilFull").check()
            assert "訂滿 2 小時才停" in page.locator("#watchHint").inner_text()
            if artifacts.is_dir():
                box.screenshot(path=str(artifacts / "watch-until-full-on.png"))
            messages = []
            page.once("dialog", lambda dialog: (messages.append(dialog.message), dialog.accept()))
            with page.expect_response("**/api/watch"):
                page.locator("#watchStart").click()
            assert watches[-1]["untilFull"] is True
            assert watches[-1]["date"]
            assert "訂滿 2 小時才停" in messages[-1]

            page.locator("#watchUntilFull").uncheck()
            assert "送出一筆訂單，然後停止" in page.locator("#watchHint").inner_text()
            page.once("dialog", lambda dialog: (messages.append(dialog.message), dialog.accept()))
            with page.expect_response("**/api/watch"):
                page.locator("#watchStart").click()
            assert watches[-1]["untilFull"] is False
            assert "送出一筆訂單，然後停止" in messages[-1]

            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("() => document.getElementById('toasts').replaceChildren()")
            box.scroll_into_view_if_needed()
            page.locator("#watchUntilFull").check()
            assert "訂滿 2 小時才停" in page.locator("#watchHint").inner_text()
            if artifacts.is_dir():
                box.screenshot(path=str(artifacts / "watch-until-full-mobile.png"))
            browser.close()
    finally:
        for route in held:
            try:
                route.fulfill(status=204, body=b"")
            except Exception:
                pass
        server.shutdown()


def _watch_route(route, venue, held, watches) -> None:
    path = urlparse(route.request.url).path
    if path == "/api/watch" and route.request.method == "POST":
        body = route.request.post_data_json or {}
        watches.append(body)
        route.fulfill(json={"ok": True, "running": False, "spec": None, "log": []})
        return
    _route(route, venue, held)


def _buttons_fit(sheet) -> None:
    box = sheet.bounding_box()
    for button in sheet.locator("#resultButtons .btn").all():
        bounds = button.bounding_box()
        assert bounds["x"] >= box["x"] - 1
        assert bounds["x"] + bounds["width"] <= box["x"] + box["width"] + 1


def _route(route, venue, held, public=None) -> None:
    url = route.request.url
    if "fonts.g" in url:
        route.abort()
        return
    path = urlparse(url).path
    if path == "/api/events":
        held.append(route)
        return
    if path == "/api/meta":
        route.fulfill(json={
            "ok": True,
            "instance": "test",
            "venues": [public or venue.public()],
            "accounts": [{
                "id": "default", "provider": venue.provider, "label": "我",
                "username": "me", "ready": True, "error": "",
            }],
        })
        return
    if path == "/api/release":
        route.fulfill(json={"ok": True, "venue": venue.id, "rulesText": venue.rules.describe(), **next_release(venue.rules)})
        return
    if path == "/api/session":
        route.fulfill(json={"ok": True, "loggedIn": True, "account": {"id": "default", "username": "me", "label": "我"}})
        return
    if path == "/api/courts":
        route.fulfill(json={"ok": True, "courts": [{"id": "A", "name": "羽球場_A", "minPrice": "290"}]})
        return
    if path == "/api/snipes":
        route.fulfill(json={"ok": True, "snipes": []})
        return
    if path == "/api/board/stream":
        window = {"start": "2026-10-14", "end": "2026-10-16", "date": "2026-10-21"}
        court = {
            "id": "A",
            "name": "羽球場_A",
            "slots": [{"time": "19:00 - 20:00", "bookable": True, "status": "", "price": "490", "mine": False}],
        }
        body = "".join(
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            for event in (
                {"type": "meta", "window": window, "courts": [{"id": "A", "name": "羽球場_A"}]},
                {"type": "court", "window": window, "court": court},
                {"type": "done"},
            )
        )
        route.fulfill(content_type="text/event-stream", body=body)
        return
    if path.startswith("/api/"):
        route.fulfill(json={"ok": True})
        return
    route.fallback()
