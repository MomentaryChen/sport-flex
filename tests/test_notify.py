import json
import urllib.error

import pytest

from sportflex.core.notify import (
    DiscordNotifier,
    MultiNotifier,
    NtfyNotifier,
    TelegramNotifier,
    default_notifier,
)


class Immediate:
    def __init__(self, target, args, daemon):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr("sportflex.core.notify.threading.Thread", Immediate)
    slept: list[float] = []
    monkeypatch.setattr("sportflex.core.notify._sleep", slept.append)
    return slept


def _failing(monkeypatch, errors):
    """urlopen that raises each error in turn, then succeeds. Returns the call count box."""
    calls = {"n": 0}

    def urlopen(request, timeout=10):
        calls["n"] += 1
        if errors:
            raise errors.pop(0)

        class Response:
            def close(self):
                return None

        return Response()

    monkeypatch.setattr("sportflex.core.notify.urllib.request.urlopen", urlopen)
    return calls


def _http(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://x", code, "err", headers, None)


def test_network_error_is_retried_until_it_goes_through(inline, monkeypatch, capsys):
    calls = _failing(monkeypatch, [urllib.error.URLError("down"), _http(502)])
    TelegramNotifier("tok", "1").send("✅ 搶到", "請付款")
    assert calls["n"] == 3
    assert inline == [2, 5]
    assert "第 1/4 次" in capsys.readouterr().out


def test_rate_limit_waits_for_retry_after(inline, monkeypatch):
    calls = _failing(monkeypatch, [_http(429, "30")])
    TelegramNotifier("tok", "1").send("✅ 搶到", "請付款")
    assert calls["n"] == 2
    assert inline == [30]


def test_client_error_is_not_retried(inline, monkeypatch, capsys):
    calls = _failing(monkeypatch, [_http(400)])
    TelegramNotifier("tok", "1").send("✅ 搶到", "請付款")
    assert calls["n"] == 1
    assert inline == []
    assert "Telegram 發送失敗" in capsys.readouterr().out


def test_gives_up_after_the_last_retry(inline, monkeypatch, capsys):
    calls = _failing(monkeypatch, [urllib.error.URLError("down") for _ in range(10)])
    TelegramNotifier("tok", "1").send("✅ 搶到", "請付款")
    assert calls["n"] == 4
    assert inline == [2, 5, 15]
    assert capsys.readouterr().out.rstrip().endswith("Telegram 發送失敗：<urlopen error down>")


def _capture(monkeypatch):
    captured = {}

    def urlopen(request, timeout=10):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode())
        captured["auth"] = request.get_header("Authorization")

        class Response:
            def close(self):
                return None

        return Response()

    monkeypatch.setattr("sportflex.core.notify.urllib.request.urlopen", urlopen)
    return captured


def test_discord_posts_tagged_text_without_mentions(inline, monkeypatch):
    captured = _capture(monkeypatch)
    DiscordNotifier("https://discord.com/api/webhooks/123/abc", instance="docker").send("搶到", "請付款")
    assert captured["url"] == "https://discord.com/api/webhooks/123/abc"
    assert captured["timeout"] == 10
    assert captured["body"] == {
        "username": "Sport Flex",
        "content": "[docker] 搶到\n請付款",
        "allowed_mentions": {"parse": []},
    }


def test_discord_truncates_to_the_webhook_limit(inline, monkeypatch):
    captured = _capture(monkeypatch)
    DiscordNotifier("https://ptb.discord.com/api/webhooks/9/tok").send("標題", "內" * 3000)
    content = captured["body"]["content"]
    assert len(content) == 2000
    assert content.endswith("…（已截斷）")


def test_discord_rejects_a_url_that_is_not_a_webhook():
    with pytest.raises(ValueError, match="DISCORD"):
        DiscordNotifier("https://example.com/api/webhooks/1/abc")
    with pytest.raises(ValueError, match="DISCORD"):
        DiscordNotifier("http://discord.com/api/webhooks/1/abc")


def test_discord_failure_hides_the_webhook_token(inline, monkeypatch, capsys):
    def urlopen(request, timeout=10):
        raise urllib.error.URLError(request.full_url)

    monkeypatch.setattr("sportflex.core.notify.urllib.request.urlopen", urlopen)
    DiscordNotifier("https://discord.com/api/webhooks/123/abc.SECRET_1").send("標題", "內容")
    out = capsys.readouterr().out
    assert "Discord 發送失敗" in out
    assert "SECRET" not in out
    assert "api/webhooks/123/***" in out


def test_ntfy_posts_title_priority_token_and_click(inline, monkeypatch):
    captured = _capture(monkeypatch)
    NtfyNotifier("https://ntfy.sh/sportflex/", token="tk_test", instance="筆電", priority=5).send(
        "⏰ 還沒付款",
        "打開連結 → 前往付款\nhttps://pay.example/a，然後完成",
    )
    assert captured["url"] == "https://ntfy.sh/sportflex/"
    assert captured["auth"] == "Bearer tk_test"
    assert captured["body"] == {
        "title": "[筆電] ⏰ 還沒付款",
        "message": "打開連結 → 前往付款\nhttps://pay.example/a，然後完成",
        "priority": 5,
        "click": "https://pay.example/a",
    }


def test_ntfy_omits_auth_and_click_when_unset(inline, monkeypatch):
    captured = _capture(monkeypatch)
    NtfyNotifier("http://127.0.0.1:2586/court").send("登入已失效", "請在網頁上重新登入。")
    assert captured["auth"] is None
    assert "click" not in captured["body"]
    assert captured["body"]["priority"] == 4
    assert captured["body"]["title"] == "登入已失效"


def test_ntfy_rejects_a_server_url_without_a_topic():
    with pytest.raises(ValueError, match="NTFY_URL"):
        NtfyNotifier("https://ntfy.sh")


def test_a_failing_channel_does_not_block_the_next(capsys):
    class Boom:
        def send(self, title, body):
            raise RuntimeError("telegram down")

    seen = []

    class Ok:
        def send(self, title, body):
            seen.append((title, body))

    MultiNotifier([Boom(), Ok()]).send("標題", "內文")
    assert seen == [("標題", "內文")]
    assert "Boom 發送失敗" in capsys.readouterr().out


def test_default_notifier_wires_configured_backups(monkeypatch):
    monkeypatch.setattr(
        "sportflex.core.notify.load_env",
        lambda: {
            "SPORT_FLEX_TELEGRAM_BOT_TOKEN": "token",
            "SPORT_FLEX_TELEGRAM_CHAT_ID": "1",
            "SPORT_FLEX_DISCORD_WEBHOOK_URL": " https://canary.discord.com/api/webhooks/7/hook ",
            "SPORT_FLEX_NTFY_URL": "https://ntfy.example/court",
            "SPORT_FLEX_NTFY_TOKEN": "tk",
            "SPORT_FLEX_NTFY_PRIORITY": "5",
        },
    )
    monkeypatch.setattr("sportflex.core.notify.server_label", lambda: "docker")
    notifier = default_notifier()
    assert [type(item).__name__ for item in notifier.notifiers] == [
        "ConsoleNotifier",
        "TelegramNotifier",
        "DiscordNotifier",
        "NtfyNotifier",
    ]
    assert notifier.channels == ["Telegram", "Discord", "ntfy"]
    ntfy = notifier.notifiers[-1]
    assert ntfy.priority == 5
    assert ntfy.token == "tk"
    assert ntfy.instance == "docker"


def test_default_notifier_skips_blank_backups(monkeypatch):
    monkeypatch.setattr(
        "sportflex.core.notify.load_env",
        lambda: {"SPORT_FLEX_DISCORD_WEBHOOK_URL": "  ", "SPORT_FLEX_NTFY_URL": ""},
    )
    monkeypatch.setattr("sportflex.core.notify.server_label", lambda: "docker")
    notifier = default_notifier()
    assert [type(item).__name__ for item in notifier.notifiers] == ["ConsoleNotifier"]
    assert notifier.channels == []


def test_default_notifier_rejects_bad_backup_settings(monkeypatch):
    monkeypatch.setattr("sportflex.core.notify.server_label", lambda: "docker")
    monkeypatch.setattr(
        "sportflex.core.notify.load_env",
        lambda: {"SPORT_FLEX_NTFY_URL": "https://ntfy.sh/court", "SPORT_FLEX_NTFY_PRIORITY": "high"},
    )
    with pytest.raises(ValueError, match="NTFY_PRIORITY"):
        default_notifier()
