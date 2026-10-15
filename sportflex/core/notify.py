from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from sportflex.core.accounts import load_env
from sportflex.core.identity import server_label

_DISCORD_HOSTS = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
_DISCORD_LIMIT = 2000
_NTFY_MESSAGE_LIMIT = 4096
_NTFY_TITLE_LIMIT = 250
_LINK = re.compile(r"https?://[^\s<>，。、）】\]]+")
_WEBHOOK_TOKEN = re.compile(r"(api/webhooks/\d+/)[^\s/?#]+")
RETRY_DELAYS_SEC = (2, 5, 15)  # waits between attempts; each send runs on its own thread
MAX_RETRY_WAIT_SEC = 60  # cap on a server's Retry-After
_sleep = time.sleep


class Notifier(Protocol):
    def send(self, title: str, body: str) -> None: ...


class ConsoleNotifier:
    def send(self, title: str, body: str) -> None:
        print(f"[通知] {title}\n{body}", flush=True)


class TelegramNotifier:
    """Push through a Telegram bot. Sends on a background thread so the snipe loop never waits."""

    def __init__(self, token: str, chat_id: str, instance: str = "") -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.instance = instance

    def send(self, title: str, body: str) -> None:
        text = _notice(title, body, self.instance)
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str) -> None:
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        _deliver(request, "Telegram")


class DiscordNotifier:
    """Discord incoming webhook. One-way text only; captcha photos stay on Telegram."""

    def __init__(self, webhook_url: str, instance: str = "") -> None:
        self.url = _discord_webhook(webhook_url)
        self.instance = instance

    def send(self, title: str, body: str) -> None:
        text = _clip(_notice(title, body, self.instance), _DISCORD_LIMIT)
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str) -> None:
        payload = json.dumps(
            {"username": "Sport Flex", "content": text, "allowed_mentions": {"parse": []}},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "sport-flex"},
        )
        _deliver(request, "Discord")


class NtfyNotifier:
    """ntfy topic. A payment or booking link in the body becomes the notification's click action."""

    def __init__(self, topic_url: str, token: str = "", instance: str = "", priority: int = 4) -> None:
        self.url = _ntfy_topic(topic_url)
        self.token = token.strip()
        self.instance = instance
        self.priority = priority

    def send(self, title: str, body: str) -> None:
        shown = f"[{self.instance}] {title}" if self.instance else title
        threading.Thread(target=self._post, args=(shown, body), daemon=True).start()

    def _post(self, title: str, body: str) -> None:
        message = _clip(body, _NTFY_MESSAGE_LIMIT)
        payload: dict[str, object] = {
            "title": _clip(title, _NTFY_TITLE_LIMIT),
            "message": message,
            "priority": self.priority,
        }
        link = _first_link(message)
        if link:
            payload["click"] = link
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "sport-flex"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        _deliver(request, "ntfy")


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier], channels: list[str] | None = None) -> None:
        self.notifiers = notifiers
        self.channels = list(channels or [])

    def send(self, title: str, body: str) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(title, body)
            except Exception as exc:
                print(f"[通知] {type(notifier).__name__} 發送失敗：{exc}", flush=True)


def default_notifier() -> Notifier:
    """Console always. Telegram, Discord, and ntfy join when their .env keys are set.

    Discord and ntfy are sent together with Telegram, not after it fails. A blocked
    Telegram API then still delivers the grab, payment, and logout notices. Captcha
    photos are not sent here; those stay on the Telegram bot, which can take a reply.
    """
    env = load_env()
    instance = server_label()
    notifiers: list[Notifier] = [ConsoleNotifier()]
    channels: list[str] = []
    if env.get("SPORT_FLEX_TELEGRAM_BOT_TOKEN") and env.get("SPORT_FLEX_TELEGRAM_CHAT_ID"):
        channels.append("Telegram")
        notifiers.append(
            TelegramNotifier(
                env["SPORT_FLEX_TELEGRAM_BOT_TOKEN"],
                env["SPORT_FLEX_TELEGRAM_CHAT_ID"],
                instance=instance,
            )
        )
    discord = env.get("SPORT_FLEX_DISCORD_WEBHOOK_URL", "").strip()
    if discord:
        channels.append("Discord")
        notifiers.append(DiscordNotifier(discord, instance=instance))
    ntfy = env.get("SPORT_FLEX_NTFY_URL", "").strip()
    if ntfy:
        channels.append("ntfy")
        notifiers.append(
            NtfyNotifier(
                ntfy,
                token=env.get("SPORT_FLEX_NTFY_TOKEN", ""),
                instance=instance,
                priority=_ntfy_priority(env.get("SPORT_FLEX_NTFY_PRIORITY", "")),
            )
        )
    return MultiNotifier(notifiers, channels=channels)


def _notice(title: str, body: str, instance: str) -> str:
    text = f"{title}\n{body}"
    if instance:
        text = f"[{instance}] {text}"
    return text


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = "…（已截斷）"
    return text[: limit - len(suffix)] + suffix


def _first_link(text: str) -> str:
    match = _LINK.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}")


def _deliver(request: urllib.request.Request, channel: str) -> bool:
    """POST with retries. A payment notice that never arrives costs the booking, so network
    errors, 429 and 5xx are retried; any other 4xx means the request itself is wrong."""
    attempts = len(RETRY_DELAYS_SEC) + 1
    for attempt in range(1, attempts + 1):
        wait = 0.0
        retryable = True
        try:
            urllib.request.urlopen(request, timeout=10).close()
            return True
        except urllib.error.HTTPError as exc:
            error = exc
            retryable = exc.code == 429 or exc.code >= 500
            wait = _retry_after(exc)
        except Exception as exc:
            error = exc
        if not retryable or attempt >= attempts:
            print(f"[通知] {channel} 發送失敗：{_public_error(error)}", flush=True)
            return False
        delay = min(MAX_RETRY_WAIT_SEC, max(RETRY_DELAYS_SEC[attempt - 1], wait))
        print(
            f"[通知] {channel} 發送失敗（第 {attempt}/{attempts} 次）：{_public_error(error)}，{delay:g} 秒後重試",
            flush=True,
        )
        _sleep(delay)
    return False


def _retry_after(exc: urllib.error.HTTPError) -> float:
    raw = (exc.headers or {}).get("Retry-After") if exc.headers is not None else None
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        return 0.0


def _public_error(exc: Exception) -> str:
    return _WEBHOOK_TOKEN.sub(r"\1***", str(exc))


def _discord_webhook(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DISCORD_HOSTS
        or parsed.username
        or parts[:2] != ["api", "webhooks"]
        or len(parts) != 4
        or not parts[2].isdigit()
        or not parts[3]
    ):
        raise ValueError("SPORT_FLEX_DISCORD_WEBHOOK_URL 必須是 https://discord.com/api/webhooks/…")
    return url.strip()


def _ntfy_topic(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("SPORT_FLEX_NTFY_URL 必須是主題網址，例如 https://ntfy.sh/你的主題")
    return url.strip()


def _ntfy_priority(raw: str) -> int:
    text = raw.strip()
    if not text:
        return 4
    if text not in {"1", "2", "3", "4", "5"}:
        raise ValueError("SPORT_FLEX_NTFY_PRIORITY 必須是 1 到 5（預設 4）")
    return int(text)
