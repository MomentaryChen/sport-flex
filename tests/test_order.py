import pytest

from sportflex.core.accounts import Account
from sportflex.core.engine import announce_grab, grab
from sportflex.core.models import Court, OrderOptions, Slot
from sportflex.providers.base import ProviderError
from tests.test_engine import FakeProvider, venue  # noqa: F401  (fixture)

# ---- settings -------------------------------------------------------------


def account(**extra):
    return Account(id="me", provider="changjia", username_env="U", password_env="P", profile="x", **extra)


def test_order_options_from_env(monkeypatch):
    monkeypatch.setenv("CARRIER", "/abc1234 ")
    monkeypatch.delenv("SPORT_FLEX_SUBMIT_ORDER", raising=False)
    assert account(invoice_carrier_env="CARRIER").order_options() == OrderOptions(True, "/ABC1234")
    monkeypatch.setenv("SPORT_FLEX_SUBMIT_ORDER", "0")
    assert account().order_options() == OrderOptions(False, "")


def test_bad_carrier_rejected(monkeypatch):
    monkeypatch.setenv("CARRIER", "ABC1234")
    with pytest.raises(ValueError, match="手機條碼"):
        account(invoice_carrier_env="CARRIER").order_options()


# ---- notification ----------------------------------------------------------


class Box:
    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))


RESULT = {"venueName": "新店", "court": "羽球場_A", "date": "2026-10-21", "time": "12:00 - 13:00", "price": "290", "url": "u"}


def test_announce_points_to_unpaid_list():
    box = Box()
    order = {"no": "CJC1", "paymentUrl": "https://changjia.sporetrofit.com/My/", "carrier": "/ABC1234", "timeoutSec": 590, "warning": ""}
    announce_grab(box, "我", {**RESULT, "order": order})
    title, body = box.sent[0]
    assert "搶到" in title and "CJC1" in body and "9 分鐘內" in body and "待付款" in body
    assert "https://changjia.sporetrofit.com/My/" in body and "手機載具" in body and "/ABC1234" in body


def test_announce_without_link_points_to_member_centre():
    box = Box()
    announce_grab(box, "我", {**RESULT, "order": {"no": "CJC1", "paymentUrl": "", "carrier": "", "warning": "查不到付款期限"}})
    body = box.sent[0][1]
    assert "會員中心" in body and "查不到付款期限" in body


def test_announce_when_order_not_placed():
    box = Box()
    announce_grab(box, "我", RESULT)
    assert "訂單確認頁" in box.sent[0][1]


def test_grab_passes_order_through(venue):  # noqa: F811
    class Ordering(FakeProvider):
        def submit(self, court, query_date, slot):
            return {**super().submit(court, query_date, slot), "order": {"no": "CJC1"}}

    court = Court(id="A", name="場A", venue="v", category="羽球")
    result = grab(Ordering(), venue, court, "2026-10-21", Slot("12:00 - 13:00", True, "", "290"))
    assert result["order"] == {"no": "CJC1"}


# ---- the real page flow against local copies of confirm.php / pay.php -------

CONFIRM = """<html><body>
<input type="checkbox" name="agreement" onclick="changeBtnStatus(this)"> 我已同意
<button class="btn font-reverse" disabled onclick="reserve()">送出訂單</button>
<script>
function changeBtnStatus(cb){ const b=document.querySelector('button'); b.disabled=true;
  if (cb.checked) setTimeout(() => { if (cb.checked) b.disabled=false; }, 300); }
function reserve(){ %s }
</script></body></html>"""

PAY = """<html><body>
<form id="htmlContent_Form"><input type="hidden" name="LUID" value="36f03b6a-d3e3"></form>
<select id="invoiceType"><option value="personal">個人發票</option><option value="mobileCarrier">手機載具</option></select>
<button class="btn" onclick="pay('CJC2026100845514621', '290')">確認付款</button>
</body></html>"""


@pytest.fixture
def site():
    playwright_api = pytest.importorskip("playwright.sync_api")
    from sportflex.providers.changjia.provider import BASE, ChangjiaProvider

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome")
        except Exception as exc:
            pytest.skip(f"Chrome not available: {exc}")
        page = browser.new_page()
        state = {"confirm": CONFIRM % "location.href = 'pay.php'", "api": [], "pending": ["36F03B6A-D3E3"]}
        html = "text/html; charset=utf-8"

        def serve(route):
            url = route.request.url
            if url.endswith("/api/getRequestData.php"):
                body = route.request.post_data or ""
                state["api"].append(body)
                if "getOnlinePaymentPath" in body:
                    route.fulfill(json={"Status": "1", "ResultData": {"OnlinePaymentTimeoutSeconds": "590"}})
                else:
                    rows = [{"LUID": luid} for luid in state["pending"]]
                    route.fulfill(json={"Status": "1", "ResultData": {"OnlinePaymentOrderDetails": {"DataTable": {"DataRow": rows}}}})
            elif url.endswith("pay.php"):
                route.fulfill(body=PAY, content_type=html)
            else:
                route.fulfill(body=state["confirm"], content_type=html)

        page.context.route("**/*", serve)
        yield ChangjiaProvider(page), page, state, BASE
        browser.close()


def test_places_order_and_reads_order_details(site):
    from sportflex.providers.changjia.provider import _hidden, _order_no

    provider, page, state, base = site
    page.goto(base + "/Reserve/confirm.php")
    provider._place_order(page.locator('input[name="agreement"]'))
    assert page.url.endswith("pay.php")
    assert _order_no(page) == "CJC2026100845514621" and _hidden(page, "LUID") == "36F03B6A-D3E3"
    assert provider._payment_timeout("XDSC") == 590
    assert provider.pending_payments("XDSC") == ["36F03B6A-D3E3"]
    assert not any("checkPaymentUserInfo" in body for body in state["api"])  # 確認付款 is never pressed


def test_site_refusal_before_order_raises(site):
    provider, page, state, base = site
    state["confirm"] = CONFIRM % """document.body.insertAdjacentHTML('beforeend', '<div class="swal2-popup">此時段已被預約</div>')"""
    page.goto(base + "/Reserve/confirm.php")
    with pytest.raises(ProviderError, match="已被預約"):
        provider._place_order(page.locator('input[name="agreement"]'))


def test_pending_rows_single_or_empty():
    from sportflex.providers.changjia.provider import _pending_luids

    assert _pending_luids({"OnlinePaymentOrderDetails": {"DataTable": {"DataRow": {"LUID": "ab"}}}}) == ["AB"]
    assert _pending_luids({"OnlinePaymentOrderDetails": ""}) == []


# ---- payment reminder ---------------------------------------------------------


class Unpaid:
    def __init__(self, pending):
        self.pending = pending

    def pending_payments(self, venue_code):
        return self.pending


def watcher(monkeypatch):
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    worker = AccountWorker(account(), {}, ConsoleNotifier())
    alerts = []
    monkeypatch.setattr(worker, "_alert", lambda title, body: alerts.append((title, body)))
    order = {"no": "CJC1", "luid": "L1", "lid": "XDSC", "paymentUrl": "https://changjia.sporetrofit.com/My/",
             "carrier": "/ABC1234", "timeoutSec": 590}
    worker._track_payment({**RESULT, "order": order})
    return worker, alerts


def due(worker):
    for item in worker._payments:
        item["next"] = 0


def test_reminds_once_when_still_unpaid_then_reports_the_end(monkeypatch):
    worker, alerts = watcher(monkeypatch)
    worker._check_payments(Unpaid(["L1"]))
    assert not alerts  # not five minutes yet
    due(worker)
    worker._check_payments(Unpaid(["L1"]))
    assert len(alerts) == 1 and "還沒付款" in alerts[0][0] and "待付款" in alerts[0][1] and "/ABC1234" in alerts[0][1]
    due(worker)
    worker._check_payments(Unpaid(["L1"]))  # deadline passed but the site hasn't cleared it: wait, no message
    assert len(alerts) == 1
    due(worker)
    worker._check_payments(Unpaid([]))
    assert len(alerts) == 2 and "已結束" in alerts[1][0] and not worker._payments


def test_no_reminder_when_already_paid(monkeypatch):
    worker, alerts = watcher(monkeypatch)
    due(worker)
    worker._check_payments(Unpaid([]))
    assert not alerts and not worker._payments


def test_unknown_status_retries_then_gives_up(monkeypatch):
    worker, alerts = watcher(monkeypatch)
    for _ in range(10):
        due(worker)
        worker._check_payments(Unpaid(None))
    assert not alerts and not worker._payments
