import os

import pytest

from sportflex.core.accounts import Account
from sportflex.core.notify import ConsoleNotifier
from sportflex.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from sportflex.runtime.worker import ORDERS_BLOCKED, AccountWorker


def test_second_process_is_refused_and_names_the_holder(tmp_path):
    path = tmp_path / "instance.lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning, match="已經有 Sport Flex") as raised:
            InstanceLock(path).acquire()
        assert f"pid {os.getpid()}" in str(raised.value)
        assert "重複下單" in str(raised.value)
    finally:
        first.release()
    again = InstanceLock(path)
    again.acquire()
    again.release()


def test_serve_stops_before_telegram_when_this_machine_is_busy(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPORT_FLEX_INSTANCE", "test-lock")
    monkeypatch.setattr("sportflex.web.app.OUTPUT_DIR", tmp_path)
    held = InstanceLock(tmp_path / "instance.lock")
    held.acquire()

    def boom():
        raise AssertionError("should not probe Telegram while this machine already has a lock")

    monkeypatch.setattr("sportflex.web.app.bot_from_env", boom)
    from sportflex.web.app import serve

    try:
        assert serve(port=9, open_browser=False) == 1
    finally:
        held.release()
    assert "已經有 Sport Flex" in capsys.readouterr().out


def test_serve_refuses_a_bot_that_is_already_polling(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SPORT_FLEX_INSTANCE", "test-lock")
    monkeypatch.setattr("sportflex.web.app.OUTPUT_DIR", tmp_path)

    class Busy:
        def probe_other_instance(self):
            return "另一個 Sport Flex 正在使用同一個 Telegram bot（409 Conflict）。\n這份不啟動"

    monkeypatch.setattr("sportflex.web.app.bot_from_env", lambda: Busy())

    def fail_run(self):
        raise AssertionError("server started beside another poller")

    monkeypatch.setattr("uvicorn.Server.run", fail_run)
    from sportflex.web.app import serve

    assert serve(port=9, open_browser=False) == 1
    assert "這份不啟動" in capsys.readouterr().out
    # The lock is released on the way out, so a later start is not stuck with it.
    lock = InstanceLock(tmp_path / "instance.lock")
    lock.acquire()
    lock.release()


def test_serve_starts_when_the_machine_and_bot_are_free(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORT_FLEX_INSTANCE", "test-lock")
    monkeypatch.setattr("sportflex.web.app.OUTPUT_DIR", tmp_path)

    class Free:
        def probe_other_instance(self):
            return ""

    monkeypatch.setattr("sportflex.web.app.bot_from_env", lambda: Free())
    started = {}

    def run(self):
        started["yes"] = True

    monkeypatch.setattr("uvicorn.Server.run", run)
    from sportflex.web.app import serve

    assert serve(port=9, open_browser=False) == 0
    assert started["yes"] is True


def _worker(blocked):
    account = Account(id="me", provider="changjia", username_env="T_USER", password_env="T_PASS", profile="x")
    return AccountWorker(account, {}, ConsoleNotifier(), booking_blocked=blocked)


def test_tick_and_grab_stop_while_another_instance_holds_the_bot(monkeypatch):
    monkeypatch.setenv("T_USER", "u")
    monkeypatch.setenv("T_PASS", "p")
    worker = _worker(lambda: True)

    class Job:
        active = True

        def tick(self, _provider):
            raise AssertionError("snipe ticked while orders are paused")

    worker.snipes = {"v": Job()}
    worker._tick(None)
    with pytest.raises(RuntimeError, match=ORDERS_BLOCKED):
        worker.grab(None, "", "", "", None)  # type: ignore[arg-type]
