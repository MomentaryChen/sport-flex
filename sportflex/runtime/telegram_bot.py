from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Callable, Protocol

HELP = (
    "登入失效時我會傳驗證碼圖片過來，直接回覆圖片上的驗證碼就會重新登入。\n"
    "/status 看每個帳號的登入狀態\n"
    "/login [帳號] 現在就取一張驗證碼"
)


class Relogin(Protocol):
    """What the bot needs from the worker pool."""

    def account_ids(self) -> list[str]: ...

    def status_text(self) -> str: ...

    def captcha_for(self, account_id: str) -> bytes: ...

    def login_with(self, account_id: str, code: str) -> dict: ...


class TelegramBot:
    """Two-way Telegram: sends captcha photos when an account is logged out and logs in with the
    code you reply. Only messages from the configured chat are accepted. Long-polls getUpdates on
    its own thread; Telegram allows one poller per bot token, so run one server per bot."""

    def __init__(self, token: str, chat_id: str, api: Callable[[str, dict, dict | None], dict] | None = None) -> None:
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.relogin: Relogin | None = None
        self._api = api  # injected in tests; None = real HTTP
        self._pending: dict[int, str] = {}  # captcha photo message id -> account id
        self._last_prompt: str = ""  # account of the latest captcha, for replies that don't quote it
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._offset = 0
        self._thread = threading.Thread(target=self._poll, name="sportflex-telegram", daemon=True)

    # ---- lifecycle -----------------------------------------------------

    def start(self, relogin: Relogin) -> None:
        self.relogin = relogin
        self._skip_backlog()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- outgoing ------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text})

    def ask_code(self, account_id: str, image: bytes, caption: str) -> None:
        """Send a captcha photo; a reply to it (or the next bare code) logs that account in."""
        result = self._call("sendPhoto", {"chat_id": self.chat_id, "caption": caption}, {"photo": image})
        message_id = (result or {}).get("message_id")
        with self._lock:
            if message_id:
                self._pending[message_id] = account_id
            self._last_prompt = account_id

    def ask_code_async(self, account_id: str, image: bytes, caption: str) -> None:
        """For the browser thread: never wait on Telegram there."""
        threading.Thread(target=self.ask_code, args=(account_id, image, caption), daemon=True).start()

    def send_text_async(self, text: str) -> None:
        threading.Thread(target=self.send_text, args=(text,), daemon=True).start()

    # ---- incoming ------------------------------------------------------

    def _skip_backlog(self) -> None:
        """Ignore messages sent while the server was down, so an old code is never typed in."""
        updates = self._call("getUpdates", {"offset": -1, "timeout": 0}) or []
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    def _poll(self) -> None:
        while not self._stop.is_set():
            updates = self._call("getUpdates", {"offset": self._offset, "timeout": 25}, timeout=35)
            if updates is None:
                self._stop.wait(5)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception as exc:
                    self.send_text(f"處理訊息時出錯：{exc}")

    def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != self.chat_id:
            return  # not you; never act on strangers' messages
        text = (message.get("text") or "").strip()
        if not text or self.relogin is None:
            return
        if text.startswith("/"):
            self._command(text)
            return
        account_id = self._account_for(message)
        if not account_id:
            self.send_text("目前沒有等待輸入的驗證碼。要重新登入請傳 /login")
            return
        self._login(account_id, text)

    def _command(self, text: str) -> None:
        name, _, arg = text.partition(" ")
        name = name.split("@")[0].lower()
        if name == "/status":
            self.send_text(self.relogin.status_text())
        elif name == "/login":
            accounts = self.relogin.account_ids()
            account_id = arg.strip() or (accounts[0] if len(accounts) == 1 else "")
            if account_id not in accounts:
                self.send_text("請指定帳號：/login " + " | ".join(accounts))
                return
            self.prompt(account_id, "請回覆這張圖上的驗證碼")
        else:
            self.send_text(HELP)

    def _account_for(self, message: dict) -> str:
        replied = (message.get("reply_to_message") or {}).get("message_id")
        with self._lock:
            if replied in self._pending:
                return self._pending[replied]
            return self._last_prompt

    def _login(self, account_id: str, code: str) -> None:
        result = self.relogin.login_with(account_id, code)
        if result.get("ok"):
            with self._lock:
                self._pending = {mid: acc for mid, acc in self._pending.items() if acc != account_id}
                if self._last_prompt == account_id:
                    self._last_prompt = ""
            self.send_text(f"✅ {account_id} 已重新登入")
            return
        # The captcha is single-use; send a fresh one right away.
        self.prompt(account_id, f"❌ 登入失敗：{result.get('message') or '驗證碼錯誤'}\n請回覆這張新的驗證碼")

    def prompt(self, account_id: str, caption: str) -> None:
        try:
            image = self.relogin.captcha_for(account_id)
        except Exception as exc:
            self.send_text(f"取驗證碼失敗：{exc}")
            return
        self.ask_code(account_id, image, f"[{account_id}] {caption}")

    # ---- transport -----------------------------------------------------

    def _call(self, method: str, params: dict, files: dict | None = None, timeout: float = 15):
        try:
            if self._api:
                return self._api(method, params, files)
            return self._http(method, params, files, timeout)
        except Exception as exc:
            print(f"[Telegram] {method} 失敗：{exc}", flush=True)
            return None

    def _http(self, method: str, params: dict, files: dict | None = None, timeout: float = 15):
        if files:
            body, content_type = _multipart(params, files)
        else:
            body, content_type = json.dumps(params).encode("utf-8"), "application/json"
        request = urllib.request.Request(self.base + method, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} {exc.read()[:200]!r}") from exc
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram 回傳失敗")
        return data["result"]


def _multipart(params: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in params.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    for key, content in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{key}.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n".encode("utf-8")
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
