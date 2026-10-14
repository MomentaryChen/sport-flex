from sportflex.core.clock import clock_offset_seconds, format_clock_offset
from sportflex.providers.changjia.provider import BASE, ChangjiaProvider


def test_clock_offset_is_local_minus_site():
    header = "Thu, 01 Jan 1970 00:00:00 GMT"
    assert clock_offset_seconds(header, 0.3) == 0.3
    assert clock_offset_seconds(header, -1.24) == -1.24
    assert clock_offset_seconds("", 0.3) is None
    assert clock_offset_seconds("not a date", 0.3) is None
    assert clock_offset_seconds(None, 0.3) is None


def test_format_clock_offset_keeps_the_sign_and_one_decimal():
    assert format_clock_offset(0.26) == "+0.3 秒"
    assert format_clock_offset(-1.24) == "-1.2 秒"
    assert format_clock_offset(0.0) == "+0.0 秒"
    assert format_clock_offset(-0.04) == "+0.0 秒"


class _Response:
    def __init__(self, url, date, resource_type="document"):
        self.url = url
        self.headers = {"date": date} if date is not None else {}
        self.request = type("Req", (), {"resource_type": resource_type})()


class _Banner:
    def count(self):
        return 1

    def inner_text(self):
        return "會員 你好"


class _Page:
    def __init__(self, responses):
        self.responses = responses
        self.listeners = []
        self.removed = []

    def on(self, event, handler):
        assert event == "response"
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        self.removed.append(handler)
        self.listeners.remove(handler)

    def goto(self, url, wait_until=""):
        assert url == BASE + "/"
        for response in self.responses:
            for handler in list(self.listeners):
                handler(response)

    def locator(self, selector):
        assert "reservationAction" in selector
        return _Banner()


def test_refresh_session_reads_the_document_date(monkeypatch):
    page = _Page(
        [
            _Response(BASE + "/assets/app.css", "Thu, 01 Jan 1970 00:00:00 GMT", "stylesheet"),
            _Response("https://example.test/", "Thu, 01 Jan 1970 00:00:05 GMT"),
            _Response(BASE + "/", "Thu, 01 Jan 1970 00:00:00 GMT"),
        ]
    )
    monkeypatch.setattr("sportflex.providers.changjia.provider.time.time", lambda: 0.34)
    session = ChangjiaProvider(page).refresh_session()
    assert session["loggedIn"] is True
    assert session["clockOffset"] == 0.34
    assert page.removed  # the listener does not stay attached


def test_refresh_session_without_a_date_header_has_no_offset():
    page = _Page([_Response(BASE + "/", None)])
    session = ChangjiaProvider(page).refresh_session()
    assert session["loggedIn"] is True and session["clockOffset"] is None


def test_refresh_session_still_reports_login_if_the_clock_listener_cannot_attach():
    class Deaf(_Page):
        def on(self, event, handler):
            raise RuntimeError("no events")

    page = Deaf([])
    session = ChangjiaProvider(page).refresh_session()
    assert session == {"loggedIn": True, "banner": "會員 你好", "clockOffset": None}
