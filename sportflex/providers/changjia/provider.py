from __future__ import annotations

import json
import re
import time

from playwright.sync_api import Page

from sportflex.core.models import Availability, Court, Slot
from sportflex.core.venue import Venue
from sportflex.providers.base import NeedLogin, Provider, ProviderError, Throttled

BASE = "https://changjia.sporetrofit.com"
# 會員中心: its 待付款 button lists unpaid orders. consumeRecord.php itself can't be linked to that
# tab (it ignores URL parameters and reopens whichever tab was used last).
UNPAID_URL = BASE + "/My/"
# createTable answers a too-fast guest with this stub instead of the court list. A short pause and
# another try returns the real list; it is not a login wall.
TABLE_ATTEMPTS = 3
TABLE_PAUSE_SEC = 3.0


class ChangjiaProvider(Provider):
    """Drives the Changjia site the way a person would: 場地清單 → 開始預約 → confirm form.

    Venue params: lid — the venue's LID (e.g. XDSC for 新店). Courts of other venues are filtered out.
    """

    name = "changjia"
    login_url = BASE + "/login/"

    def __init__(self, page: Page) -> None:
        super().__init__(page)
        self.on_list = False  # page currently shows 場地清單
        self.category = ""  # sport chip active on 場地清單
        self.category_ids: dict[str, str] = {}  # 羽球 -> Badminton, learned from 場地清單
        self.category_array = ""  # CategoryArrayStr the list page posts with every sport switch

    # ---- session -------------------------------------------------------

    def refresh_session(self) -> dict:
        page = self.page
        page.goto(BASE + "/", wait_until="domcontentloaded")
        banner = page.locator("div[onclick='reservationAction()']")
        text = banner.inner_text().strip() if banner.count() else ""
        self.on_list = False
        return {"loggedIn": "點我登入" not in text, "banner": text}

    def captcha(self, username: str, password: str) -> bytes:
        page = self.page
        page.goto(self.login_url, wait_until="domcontentloaded")
        page.locator("#inputEmailAccount").fill(username)
        page.locator("#inputPassword").fill(password)
        page.locator("#captchaContainer img").wait_for()
        self.on_list = False
        return page.locator("#captchaContainer img").screenshot(type="jpeg", quality=90)

    def login(self, code: str) -> dict:
        page = self.page
        if not page.locator("#verification_code").count():
            return {"ok": False, "message": "驗證碼已過期，請重新取得"}
        page.locator("#verification_code").fill(code.strip())
        with page.expect_response(lambda response: "ajax/login.php" in response.url, timeout=30_000) as info:
            page.locator("input.btn-login").click()
        payload = info.value.json()
        if not payload.get("success"):
            return {"ok": False, "message": payload.get("msg") or "登入失敗"}
        confirm = page.locator(".swal2-confirm")
        if confirm.count():
            confirm.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        return {"ok": True, "message": "登入成功"}

    # ---- courts & availability ----------------------------------------

    def list_courts(self, venue: Venue, category: str) -> list[Court]:
        self._ensure_list(category)
        raw = self.page.locator("#tableContainer form").evaluate_all(
            """forms => forms.map(form => {
                const value = (field) => form.querySelector(`input[name="${field}"]`)?.value || "";
                return {
                    form_id: form.id,
                    lid: value("LID"),
                    venue: value("LIDName"),
                    lsid: value("LSID"),
                    name: value("LSIDName"),
                    category_id: value("CategoryID"),
                    category: value("CategoryName"),
                    min_hours: value("minDuration"),
                    price: value("minPrice"),
                };
            }).filter(court => court.lsid)"""
        )
        lid = str(venue.params.get("lid") or "").casefold()
        for item in raw:
            if item["category_id"]:
                self.category_ids[self.category or category] = item["category_id"]
                break
        courts = []
        for item in raw:
            if lid and item["lid"].casefold() != lid:
                continue
            # A failed sport switch used to leave the previous sport's forms on the page.
            if item["category"] and self.category and item["category"] != self.category:
                continue
            courts.append(
                Court(
                    id=item["lsid"],
                    name=item["name"],
                    venue=item["venue"],
                    category=item["category"],
                    price=item["price"],
                    min_hours=item["min_hours"],
                    raw={
                        "form_id": item["form_id"],
                        "lid": item["lid"],
                        "category_id": item["category_id"],
                        "list_category": self.category,
                    },
                )
            )
        return courts

    def availability(self, court: Court, query_date: str = "") -> Availability:
        """Ask for the same availability payload the reservation calendar loads."""
        self.pace_api()
        data = self.page.evaluate(
            """async ({ lid, lsid, queryDate }) => {
                const body = new URLSearchParams({
                    serviceName: "getResLocationAvailableData",
                    LID: lid,
                    LSID: lsid,
                    QueryDate: queryDate,
                });
                const response = await fetch("/api/getRequestData.php", {
                    method: "POST",
                    headers: { "Content-Type": "application/x-www-form-urlencoded" },
                    body,
                });
                if (!response.ok) {
                    throw new Error("availability HTTP " + response.status);
                }
                return await response.json();
            }""",
            {"lid": court.raw["lid"], "lsid": court.id, "queryDate": query_date},
        )
        if str(data.get("Status")) != "1":
            message = (data.get("ResultData") or {}).get("ResultMsg") or "查詢時段失敗"
            if "頻繁" in message:
                raise Throttled(message)
            if "登入" in message:
                raise NeedLogin(message)
            raise ProviderError(message)

        result = data["ResultData"]
        rows = result["AvailableData"]["DataTable"]["DataRow"]
        if isinstance(rows, dict):
            rows = [rows]
        slots = [_slot(row) for row in rows]
        return Availability(
            query_date=result.get("QueryDate") or query_date,
            window_start=result.get("ReservingStart") or "",
            window_end=result.get("ReservingEnd") or "",
            slots=slots,
        )

    def search(self, venue: Venue, category: str, query_date: str, start: str, end: str) -> list[str]:
        """The 篩選 page: one request returns every court with an open slot in the time range."""
        category_id = self.category_ids.get(category)
        if not category_id:
            self.list_courts(venue, category)
            category_id = self.category_ids.get(category)
        if not category_id:
            raise ProviderError(f"找不到 {category} 的分類代碼")
        fields = {
            "categoryId": category_id,
            "category": category,
            "categoryArray": self.category_array,
            "day": query_date,
            "start": start,
            "end": end,
        }
        result = None
        for _attempt in range(TABLE_ATTEMPTS):
            self._pace_table()
            result = self.page.evaluate(
                """async ({ categoryId, category, categoryArray, day, start, end }) => {
                    const body = new URLSearchParams({
                        LID: "", LIDName: "", CategoryID: categoryId, CategoryName: category,
                        CategoryArrayStr: categoryArray,
                        redirectFromIndex: "true", redirectFromSearch: "false", redirectFromFilter: "true",
                        startDate: day, endDate: day, startTime: start, endTime: end,
                    });
                    const response = await fetch("/Location/LocationSubList/ajax/createTable/", {
                        method: "POST",
                        headers: { "Content-Type": "application/x-www-form-urlencoded" },
                        body,
                    });
                    if (!response.ok) {
                        throw new Error("search HTTP " + response.status);
                    }
                    const html = await response.text();
                    const doc = new DOMParser().parseFromString(html, "text/html");
                    const courts = [...doc.querySelectorAll("form")].map((form) => ({
                        lsid: form.querySelector('input[name="LSID"]')?.value || "",
                        lid: form.querySelector('input[name="LID"]')?.value || "",
                    })).filter((court) => court.lsid);
                    return { courts, html };
                }""",
                fields,
            )
            if _interpret_table(result["html"]) != "retry":
                break
        else:
            raise Throttled("場地清單暫時忙碌，請稍後再查")
        kind = _interpret_table(result["html"])
        if kind == "login":
            raise NeedLogin("查看這個運動需要先登入")
        if kind == "retry":
            raise Throttled("場地清單暫時忙碌，請稍後再查")
        if not result["courts"] and kind == "empty":
            text = re.sub(r"<[^>]+>", " ", result["html"])
            if "頻繁" in text:
                raise Throttled(text.strip()[:200])
        lid = str(venue.params.get("lid") or "").casefold()
        found: list[str] = []
        for court in result["courts"]:
            if lid and court["lid"].casefold() != lid:
                continue
            if court["lsid"] not in found:
                found.append(court["lsid"])
        return found

    # ---- booking -------------------------------------------------------

    def prepare(self, court: Court) -> None:
        self._open_reserve(court)

    def submit(self, court: Court, query_date: str, slot: Slot) -> dict:
        """開始預約 → 訂單確認 (confirm.php) → 送出訂單, then stop on 付款 (pay.php) without pressing
        確認付款: the person pays from 會員中心 → 待付款 on their phone. Never pays.
        With order.submit_order off it stops on 訂單確認 as before."""
        page = self.page
        if not self._already_on_reserve(court.id):
            self._open_reserve(court)
        form = page.locator("#confirm_Form")
        form.wait_for(state="attached")
        _set_input(form, "date", query_date)
        _set_input(form, "timeSlot", slot.time)
        _set_input(form, "price", slot.price)
        _set_input(form, "payImmediatelyAfterBooking", slot.raw.get("pay_now") or "")
        form.evaluate("node => node.submit()")
        self.on_list = False
        page.wait_for_load_state("domcontentloaded")
        agreement = page.locator('input[name="agreement"]')
        try:
            agreement.wait_for(state="visible", timeout=15_000)
        except Exception:
            agreement = None
        if not self.order.submit_order or agreement is None:
            snapshot = self._snapshot()
            snapshot["pressed"] = ""
            return snapshot
        self._place_order(agreement)  # raises if the site refused: nothing was booked
        if "pay.php" not in page.url:  # venue without online payment: the booking is already complete
            snapshot = self._snapshot()
            snapshot.update(pressed="送出訂單", order={"no": "", "paymentUrl": "", "carrier": "", "warning": ""})
            return snapshot
        # The order exists from here on: report problems in the result, never raise.
        order = {
            "no": _order_no(page),
            "luid": _hidden(page, "LUID"),  # how pending_payments() names this order
            "lid": court.raw.get("lid") or "",
            # Paying on the phone asks for the invoice again, so the carrier is shown, not pre-filled here.
            "paymentUrl": UNPAID_URL,
            "carrier": self.order.invoice_carrier,
            "timeoutSec": None,
            "warning": "",
        }
        try:
            order["timeoutSec"] = self._payment_timeout(order["lid"])
        except Exception as exc:
            order["warning"] = f"查不到付款期限：{exc}"
        snapshot = self._snapshot()
        snapshot.update(pressed="送出訂單", order=order)
        return snapshot

    def _place_order(self, agreement) -> None:
        """Tick 我已同意 and press 送出訂單 like a person; the site enables the button 2.5 s after the tick."""
        page = self.page
        if not agreement.is_checked():
            agreement.click()
        page.wait_for_function(
            "() => { const b = [...document.querySelectorAll('button')].find(x => x.innerText.trim() === '送出訂單');"
            " return b && !b.disabled; }",
            timeout=15_000,
        )
        page.locator("button", has_text="送出訂單").click()
        deadline = time.time() + 45
        while time.time() < deadline:
            if "pay.php" in page.url:
                page.wait_for_load_state("domcontentloaded")
                return
            message = _dialog_message(page)
            if message is not None:
                if "預約已完成" in message:
                    return
                raise ProviderError(f"送出訂單失敗：{message or '網站沒有說明原因'}")
            page.wait_for_timeout(250)
        raise ProviderError("送出訂單後網站沒有回應")

    def _payment_timeout(self, venue_code: str) -> int | None:
        """Seconds the site keeps an unpaid order (about 590), from the read-only lookup pay.php uses."""
        result = self._api({"serviceName": "getOnlinePaymentPath", "LID": venue_code})
        seconds = str(result.get("OnlinePaymentTimeoutSeconds") or "")
        return int(seconds) if seconds.isdigit() else None

    def _api(self, params: dict) -> dict:
        """POST the site's /api/getRequestData.php from the page (same origin, same session)."""
        data = self.page.evaluate(
            """async (params) => {
                const r = await fetch('/api/getRequestData.php', {method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: new URLSearchParams(params)});
                return await r.json();
            }""",
            params,
        )
        if str(data.get("Status")) != "1":
            raise ProviderError((data.get("ResultData") or {}).get("ResultMsg") or f"{params['serviceName']} 失敗")
        return data.get("ResultData") or {}

    def pending_payments(self, venue_code: str) -> list[str] | None:
        """Orders awaiting online payment, from the same read-only call pay.php makes."""
        if not self.page.url.startswith(BASE):
            return None  # the fetch needs the site's origin; the caller retries later
        return _pending_luids(self._api({"serviceName": "getOnlinePaymentOrderDetails", "LID": venue_code}))

    def press(self, label: str) -> dict:
        self._press_label(label)
        self.on_list = False
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(600)
        return self._snapshot()

    # ---- internals -----------------------------------------------------

    def _ensure_list(self, category: str) -> None:
        if not self.on_list:
            self.category = self._open_location_list(category)
            self.on_list = True
            return
        if self.category == category:
            return
        self._show_category(self._match_category(self._read_categories(), category)["buttonId"])
        self.category = category

    def _open_location_list(self, category: str | None) -> str:
        """Open 場地清單 the same way the homepage does, then select a sport."""
        page = self.page

        def open_list() -> None:
            page.goto(BASE + "/", wait_until="domcontentloaded")
            page.locator("#locationList").click()

        self._load_table(open_list)
        self._remember_catalog()

        categories = self._read_categories()
        if not categories:
            raise ProviderError("場地清單沒有運動種類按鈕")
        chosen = self._match_category(categories, category) if category else categories[0]
        active = page.locator("#CategoryBtnDiv span.active")
        active_label = active.inner_text().strip() if active.count() else ""
        if active_label != chosen["label"]:
            self._show_category(chosen["buttonId"])
        return chosen["label"]

    def _show_category(self, button_id: str) -> None:
        page = self.page
        self._load_table(lambda: page.locator(f"#{button_id}").click())
        self._remember_catalog()

    def _load_table(self, trigger) -> None:
        """Run one createTable action. A too-fast guest gets a fake login stub; wait and try again."""
        page = self.page
        for _attempt in range(TABLE_ATTEMPTS):
            self._pace_table()
            with page.expect_response(lambda response: "ajax/createTable/" in response.url, timeout=30_000) as info:
                trigger()
            self._dismiss_loading()
            body = info.value.text()
            kind = _interpret_table(body)
            if kind == "ok":
                if "<form" in body:
                    page.wait_for_selector("#tableContainer form", state="attached", timeout=5_000)
                return
            if kind == "login":
                self.on_list = False
                raise NeedLogin("查看這個運動需要先登入")
            if kind != "retry":
                self.on_list = False
                raise ProviderError("場地清單沒有回傳球場")
        self.on_list = False
        raise Throttled("場地清單暫時忙碌，請稍後再查")

    def _pace_table(self) -> None:
        """createTable shares the site's burst limit with the availability API."""
        pause = max(self.api_pause_sec, TABLE_PAUSE_SEC)
        wait = pause - (time.time() - self._last_api_at)
        if wait > 0:
            time.sleep(wait)
        self._last_api_at = time.time()

    def _remember_catalog(self) -> None:
        """羽球 -> Badminton for every sport chip, from the list page's hidden catalog."""
        field = self.page.locator('input[name="CategoryArrayStr"]')
        if not field.count():
            return
        raw = field.first.input_value()
        if not raw:
            return
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.category_array = raw
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("Name") or "").strip()
            item_id = str(item.get("ItemID") or "").strip()
            if name and item_id:
                self.category_ids.setdefault(name, item_id)

    def _read_categories(self) -> list[dict]:
        return self.page.locator("#CategoryBtnDiv span").evaluate_all(
            """els => els.map(el => ({ buttonId: el.id, label: el.textContent.trim() }))"""
        )

    @staticmethod
    def _match_category(categories: list[dict], query: str) -> dict:
        folded = query.strip().casefold()
        for item in categories:
            button = item["buttonId"].removesuffix("_btn").casefold()
            if folded == item["label"].casefold() or folded == button:
                return item
        names = "、".join(item["label"] for item in categories)
        raise ProviderError(f"找不到運動種類「{query}」。可選：{names}")

    def _already_on_reserve(self, lsid: str) -> bool:
        page = self.page
        if "/Reserve/" not in page.url or page.locator("#confirm_Form").count() == 0:
            return False
        return page.locator('#confirm_Form input[name="LSID"]').input_value() == lsid

    def _open_reserve(self, court: Court) -> None:
        page = self.page
        form_id = court.raw["form_id"]
        if not self.on_list or page.locator(f"#{form_id}").count() == 0:
            self.on_list = False
            self._ensure_list(court.raw.get("list_category") or court.category)
        page.locator(f"#{form_id}").evaluate("form => form.submit()")
        self.on_list = False
        page.wait_for_load_state("domcontentloaded")
        deadline = time.time() + 25
        while time.time() < deadline:
            url = page.url
            if "/login" in url:
                raise NeedLogin("預約需要登入")
            if "/LocationSub/" in url and "/Reserve/" not in url:
                button = page.locator(".reserve_button")
                button.wait_for(state="visible")
                button.click()
                page.wait_for_load_state("domcontentloaded")
                continue
            if "/Reserve/" in url and page.locator("#calendar").count():
                page.wait_for_timeout(1200)
                if "/login" in page.url:
                    continue
                page.wait_for_selector(".fc-daygrid-day")
                self._dismiss_loading()
                return
            page.wait_for_timeout(500)
        raise ProviderError("無法進入開始預約頁，請確認已登入")

    def _press_label(self, label: str) -> None:
        clicked = self.page.evaluate(
            """(label) => {
                const nodes = [...document.querySelectorAll("button, input[type=submit], input[type=button], a.btn")];
                const node = nodes.find((el) => ((el.innerText || el.value || "").replace(/\\s+/g, " ").trim()) === label);
                if (!node) return false;
                node.click();
                return true;
            }""",
            label,
        )
        if not clicked:
            raise ProviderError(f"畫面上沒有「{label}」")

    def _snapshot(self) -> dict:
        page = self.page
        self._dismiss_loading()
        buttons = page.locator("button, input[type=submit], input[type=button], a.btn").evaluate_all(
            """els => els.map(el => (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim()).filter(Boolean)"""
        )
        return {
            "url": page.url,
            "text": page.locator("body").inner_text()[:1800],
            "buttons": buttons,
            "image": page.screenshot(type="jpeg", quality=55),
        }

    def _dismiss_loading(self) -> None:
        overlay = self.page.locator(".swal2-container")
        if overlay.count():
            try:
                overlay.wait_for(state="hidden", timeout=15_000)
            except Exception:
                self.page.keyboard.press("Escape")


def _slot(row: dict) -> Slot:
    bookable = row.get("allowBooking") == "Y"
    return Slot(
        time=row.get("Time", ""),
        bookable=bookable,
        status=row.get("Status") or ("可預約" if bookable else "不可預約"),
        price=row.get("Price") or "",
        mine=row.get("IsMyReservation") == "Y",
        raw={"pay_now": row.get("PayImmediatelyAfterBooking") or ""},
    )


def _interpret_table(body: str) -> str:
    """What a createTable response is: ok, retry (guest burst stub), login, or empty."""
    if "<form" in body or "筆結果" in body:
        return "ok"
    compact = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body or ""))
    if compact == "請登入以獲得更佳體驗":
        return "retry"
    if "請登入" in (body or ""):
        return "login"
    return "empty"


def _set_input(form, name: str, value: str) -> None:
    form.locator(f'input[name="{name}"]').evaluate("(el, next) => { el.value = next }", value)


def _dialog_message(page) -> str | None:
    """Text of a SweetAlert result dialog, or None while there is none (the loading spinner doesn't count)."""
    popup = page.locator(".swal2-popup")
    if not popup.count() or not popup.first.is_visible():
        return None
    if popup.first.locator("img[src*='loading']").count():
        return None
    return popup.first.inner_text().strip().replace("確定", "").replace("OK", "").strip()


def _order_no(page) -> str:
    """CarNo from 確認付款's onclick, e.g. pay('CJC2026100845514621', '290')."""
    onclick = page.locator("button", has_text="確認付款").get_attribute("onclick") or ""
    match = re.search(r"pay\('([^']+)'", onclick)
    return match.group(1) if match else ""


def _hidden(page, name: str) -> str:
    field = page.locator(f'input[name="{name}"]')
    return (field.first.input_value() if field.count() else "").upper()


def _pending_luids(result: dict) -> list[str]:
    """OnlinePaymentOrderDetails.DataTable.DataRow is one dict, a list, or empty."""
    details = result.get("OnlinePaymentOrderDetails") or {}
    table = details.get("DataTable") if isinstance(details, dict) else None
    rows = (table or {}).get("DataRow") if isinstance(table, dict) else None
    if isinstance(rows, dict):
        rows = [rows]
    return [str(row.get("LUID", "")).upper() for row in rows or [] if isinstance(row, dict)]
