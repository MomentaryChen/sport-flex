import threading

from sportflex.core.identity import login_prefix, server_label
from sportflex.core.notify import TelegramNotifier


def test_server_label_uses_env_then_hostname(monkeypatch):
    monkeypatch.delenv("SPORT_FLEX_INSTANCE", raising=False)
    assert server_label()  # hostname when unset
    monkeypatch.setenv("SPORT_FLEX_INSTANCE", "  筆電  ")
    assert server_label() == "筆電"
    assert login_prefix("default") == "[筆電 · default]"
    assert login_prefix("default", "") == "[default]"


def test_telegram_notice_is_tagged_with_the_server(monkeypatch):
    sent = []
    notifier = TelegramNotifier("token", "1", instance="docker")
    notifier._post = lambda text: sent.append(text)

    class Immediate:
        def __init__(self, target, args, daemon):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(threading, "Thread", Immediate)
    notifier.send("登入已失效", "定時檢查發現已被登出。")
    assert sent == ["[docker] 登入已失效\n定時檢查發現已被登出。"]
