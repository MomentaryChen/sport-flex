from sportflex.core.engine import announce_grab


class Box:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.messages.append((title, body))


RESULT = {
    "venueName": "測試館",
    "court": "A",
    "date": "2026-10-21",
    "time": "19:00 - 20:00",
    "price": "290",
    "url": "https://example.test/order",
    "order": {"no": "X1", "paymentUrl": "https://pay", "carrier": "/ABC", "warning": ""},
}


def test_manual_grab_uses_same_notifier_path_as_auto():
    box = Box()
    announce_grab(box, "測試帳號", RESULT)
    assert box.messages
    title, body = box.messages[0]
    assert "搶到" in title and "測試館" in title
    assert "測試帳號" in body and "X1" in body


def test_publish_grab_on_worker(monkeypatch):
    from sportflex.core.accounts import Account
    from sportflex.runtime.worker import AccountWorker

    monkeypatch.setenv("T_USER", "u")
    monkeypatch.setenv("T_PASS", "p")
    account = Account(id="me", provider="changjia", username_env="T_USER", password_env="T_PASS", profile="x")
    box = Box()
    worker = AccountWorker(account, {}, box)
    worker._publish_grab({**RESULT, "venue": "test"}, "手動送出訂單", "manual")
    assert worker.last_grab and worker.last_grab["court"] == "A"
    assert box.messages and "手動" not in box.messages[0][0]  # title is the grab headline, not the log label
