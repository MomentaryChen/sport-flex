from __future__ import annotations

import json
import threading
import urllib.request
from typing import Protocol

from sportflex.core.accounts import load_env


class Notifier(Protocol):
    def send(self, title: str, body: str) -> None: ...


class ConsoleNotifier:
    def send(self, title: str, body: str) -> None:
        print(f"[通知] {title}\n{body}", flush=True)


class TelegramNotifier:
    """Push through a Telegram bot. Sends on a background thread so the snipe loop never waits."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, title: str, body: str) -> None:
        threading.Thread(target=self._post, args=(f"{title}\n{body}",), daemon=True).start()

    def _post(self, text: str) -> None:
        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:
            print(f"[通知] Telegram 發送失敗：{exc}", flush=True)


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, title: str, body: str) -> None:
        for notifier in self.notifiers:
            notifier.send(title, body)


def default_notifier() -> Notifier:
    """Console always; Telegram too when SPORT_FLEX_TELEGRAM_BOT_TOKEN and SPORT_FLEX_TELEGRAM_CHAT_ID are in .env."""
    env = load_env()
    notifiers: list[Notifier] = [ConsoleNotifier()]
    if env.get("SPORT_FLEX_TELEGRAM_BOT_TOKEN") and env.get("SPORT_FLEX_TELEGRAM_CHAT_ID"):
        notifiers.append(TelegramNotifier(env["SPORT_FLEX_TELEGRAM_BOT_TOKEN"], env["SPORT_FLEX_TELEGRAM_CHAT_ID"]))
    return MultiNotifier(notifiers)
