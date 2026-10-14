import threading
import time
from contextlib import contextmanager

import pytest

from sportflex.core.accounts import Account
from sportflex.runtime import worker as worker_module
from sportflex.runtime.worker import AccountWorker


class Box:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.messages.append((title, body))

    def titles(self) -> list[str]:
        return [title for title, _body in self.messages]


class FakePage:
    """Like Playwright's sync page: a dead Chrome shows up in is_closed() only after a call.
    `hang` blocks that call until `stop_hang` (the watchdog killed it) or `release`."""

    def __init__(self) -> None:
        self.closed = False
        self.dead = False
        self.hang = False
        self._release = threading.Event()

    def is_closed(self) -> bool:
        return self.closed

    def evaluate(self, script: str):
        self._touch()
        return 1

    def wait_for_timeout(self, ms: float) -> None:
        self._touch()

    def _touch(self) -> None:
        if self.hang and not self._release.is_set():
            self._release.wait(5)
        if self.dead or self.closed:
            self.closed = True
            raise RuntimeError("Target page, context or browser has been closed")

    def stop_hang(self) -> None:
        self.closed = True
        self._release.set()

    def release(self) -> None:
        self.hang = False
        self._release.set()


class FakeProvider:
    def __init__(self, page: FakePage, number: int) -> None:
        self.page = page
        self.number = number

    def refresh_session(self) -> dict:
        return {"loggedIn": True, "banner": ""}


class Browsers:
    """Hands out pages; `fail` makes the next opens raise, like Chrome refusing to start."""

    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.fail = 0
        self.hang = False
        self.opened = threading.Event()

    @contextmanager
    def open_page(self, account, headless: bool = True):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Chrome 無法啟動")
        page = FakePage()
        page.hang = self.hang
        self.pages.append(page)
        self.opened.set()
        yield page

    def release_all(self) -> None:
        self.hang = False
        for page in self.pages:
            page.release()


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("T_USER", "u")
    monkeypatch.setenv("T_PASS", "p")
    monkeypatch.setattr(worker_module, "WatchStore", lambda account_id: _NoStore())
    browsers = Browsers()
    monkeypatch.setattr(worker_module, "open_page", browsers.open_page)
    monkeypatch.setattr(
        worker_module, "create_provider", lambda name, page: FakeProvider(page, len(browsers.pages))
    )
    monkeypatch.setattr(worker_module, "BROWSER_RETRY_SEC", (0.05, 0.05, 0.05))
    monkeypatch.setattr(worker_module, "BROWSER_PROBE_SEC", 0)
    account = Account(id="me", provider="changjia", username_env="T_USER", password_env="T_PASS", profile="x")
    box = Box()
    worker = AccountWorker(account, {}, box)
    yield worker, browsers, box
    browsers.release_all()
    worker.stop(wait=2)


class _NoStore:
    def load(self):
        return None

    def save(self, spec):
        pass

    def clear(self):
        pass


def _wait(predicate, timeout: float = 5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_closed_page_reopens_browser_and_keeps_serving(setup):
    worker, browsers, box = setup
    worker.start(wait=5)
    assert worker.call(lambda provider: provider.number) == 1

    browsers.pages[0].closed = True
    assert _wait(lambda: len(browsers.pages) == 2)
    assert worker.call(lambda provider: provider.number) == 2
    assert worker.healthy
    assert "⚠️ 瀏覽器中斷" in box.titles()
    assert "⚠️ 瀏覽器沒有回應" not in box.titles()
    assert "瀏覽器已重開" in box.titles()


def test_idle_worker_notices_a_killed_chrome_without_a_request(setup):
    worker, browsers, box = setup
    worker.start(wait=5)
    browsers.pages[0].dead = True

    assert _wait(lambda: len(browsers.pages) == 2)
    assert "瀏覽器已重開" in box.titles()
    assert "⚠️ 瀏覽器沒有回應" not in box.titles()
    assert worker.call(lambda provider: provider.number) == 2


def test_gives_up_after_retries_and_reports_unhealthy(setup):
    worker, browsers, box = setup
    worker.start(wait=5)
    browsers.fail = 99
    browsers.pages[0].closed = True

    assert _wait(lambda: not worker.healthy)
    assert "重開 3 次都失敗" in worker.startup_error
    assert box.titles().count("⚠️ 瀏覽器中斷") == 3
    assert "❌ 瀏覽器無法重開" in box.titles()
    with pytest.raises(RuntimeError, match="無法使用"):
        worker.call(lambda provider: provider.number)


def test_first_start_failure_does_not_retry(setup):
    worker, browsers, box = setup
    browsers.fail = 1
    worker.start(wait=5)

    assert worker.startup_error == "Chrome 無法啟動"
    assert not worker.healthy
    assert browsers.pages == []
    assert box.messages == []


def test_dead_provider_fails_waiting_callers():
    provider = worker_module._DeadProvider("瀏覽器重開 5 次都失敗")
    with pytest.raises(RuntimeError, match="5 次"):
        provider.refresh_session()


def _fast_watchdog(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "BROWSER_STALL_SEC", 0.2)
    monkeypatch.setattr(worker_module, "BROWSER_HANG_GRACE", 0.15)
    monkeypatch.setattr(worker_module, "BROWSER_KILL_WAIT_SEC", 2)
    monkeypatch.setattr(worker_module, "BROWSER_WATCH_SEC", 0.05)


def test_idle_hang_kills_chrome_and_reopens(setup, monkeypatch):
    _fast_watchdog(monkeypatch)
    worker, browsers, box = setup

    def kill(profile):
        browsers.hang = False
        browsers.pages[-1].stop_hang()
        return 1

    monkeypatch.setattr(worker_module, "kill_chrome", kill)
    browsers.hang = True
    worker.start(wait=5)

    assert _wait(lambda: len(browsers.pages) == 2)
    assert "⚠️ 瀏覽器沒有回應" in box.titles()
    assert "瀏覽器已重開" in box.titles()
    assert worker.call(lambda provider: provider.number) == 2
    assert worker.healthy


def test_timed_out_call_does_not_keep_the_worker_or_run_the_queued_one(setup, monkeypatch):
    _fast_watchdog(monkeypatch)
    worker, browsers, box = setup
    worker.start(wait=5)
    ran: list[str] = []
    errors: list[BaseException] = []

    def kill(profile):
        browsers.pages[-1].stop_hang()
        return 1

    monkeypatch.setattr(worker_module, "kill_chrome", kill)

    def first(provider):
        provider.page.hang = True
        provider.page.evaluate("1")
        ran.append("first")

    def second(provider):
        ran.append("second")
        return provider.number

    def run(fn, timeout: float) -> None:
        try:
            worker.call(fn, timeout=timeout)
        except Exception as exc:
            errors.append(exc)

    stuck = threading.Thread(target=run, args=(first, 0.25))
    stuck.start()
    assert _wait(lambda: browsers.pages[0].hang)
    queued = threading.Thread(target=run, args=(second, 0.25))
    queued.start()
    stuck.join(5)
    queued.join(5)

    assert [type(exc) for exc in errors] == [TimeoutError, TimeoutError]
    assert _wait(lambda: len(browsers.pages) == 2)
    assert ran == []
    assert "⚠️ 瀏覽器沒有回應" in box.titles()
    assert worker.call(lambda provider: provider.number) == 2


def test_kill_that_does_not_free_the_thread_marks_the_account_unhealthy(setup, monkeypatch):
    monkeypatch.setattr(worker_module, "BROWSER_STALL_SEC", 0.12)
    monkeypatch.setattr(worker_module, "BROWSER_KILL_WAIT_SEC", 0.12)
    monkeypatch.setattr(worker_module, "BROWSER_WATCH_SEC", 0.04)
    worker, browsers, box = setup
    monkeypatch.setattr(worker_module, "kill_chrome", lambda profile: 0)
    browsers.hang = True
    worker.start(wait=5)

    assert _wait(lambda: not worker.healthy, timeout=3)
    assert worker.startup_error.startswith("瀏覽器沒有回應")
    assert "⚠️ 瀏覽器沒有回應" in box.titles()
