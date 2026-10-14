from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Protocol

from sportflex.core.identity import login_prefix
from sportflex.runtime.telegram_cmd import (
    clip,
    command_name,
    parse_book_tokens,
    parse_slots_args,
    parse_snipe_tokens,
    parse_watch_args,
    parse_weekly_args,
    pop_known,
    pop_venue,
)

HELP = (
    "登入失效時會先讓辨識送出兩次。兩次都不對才把圖片傳過來，直接回覆那張圖片上的英數就會重新登入。\n"
    "圖片開頭是 [伺服器 · 帳號]。本機、Docker 或多個帳號同時要登入時，回覆對應的那一張。\n"
    "伺服器名稱可用 SPORT_FLEX_INSTANCE 自訂（例如 docker、筆電）。\n"
    "\n"
    "/status  登入與搶訂狀態\n"
    "/login [帳號]  重新取驗證碼\n"
    "/release  下一波開放時間\n"
    "/slots [日期] [時段]  可預約時段\n"
    "/snipe 19:00 [20:00]  搶下一波（任一場）\n"
    "/snipe A 19:00  指定場地\n"
    "/weekly 二四 20:00  每週固定，開放前自動排第 14 天\n"
    "/weekly off  停用每週固定\n"
    "/cancel  取消搶訂\n"
    "/watch 明天 晚上  盯已開放的時段，有空就訂\n"
    "/watch off  停止盯場\n"
    "/book 明天 19:00 [場地]  訂已開放的時段（會再請你 /ok）\n"
    "/courts  球場清單\n"
    "/history  最近開搶\n"
    "/help  本說明\n"
    "\n"
    "日期可寫今天、明天、後天、10/20。時段可寫上午、下午、晚上或 19:00。\n"
    "多個帳號時，在指令裡加上帳號 id。"
)

CONFIRM_SEC = 180
ALIASES = {"/start": "/help", "/grab": "/book", "/board": "/slots", "/stop": "/cancel"}


class Relogin(Protocol):
    """What the bot needs from the worker pool."""

    def account_ids(self) -> list[str]: ...

    def status_text(self) -> str: ...

    def captcha_for(self, account_id: str) -> bytes: ...

    def login_with(self, account_id: str, code: str) -> dict: ...


class TelegramBot:
    """Two-way Telegram for one configured chat. Replies to a captcha photo log that account back
    in, and /help lists the other commands (slots, snipe, book). Long-polls getUpdates on its own
    thread; Telegram allows one poller per bot token, so run one server per bot."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        api: Callable[[str, dict, dict | None], dict] | None = None,
        instance: str = "",
    ) -> None:
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.instance = instance  # shown on captchas so several servers in one chat stay distinct
        self.relogin: Relogin | None = None
        self._api = api  # injected in tests; None = real HTTP
        self._pending: dict[int, str] = {}  # captcha photo message id -> account id
        self._last_prompt: str = ""  # account of the latest captcha, for replies that don't quote it
        self._confirm: dict | None = None  # a /book waiting for /ok
        self._confirm_gen = 0
        self.sync_commands = False  # tests run browser-backed commands inline
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
        if account_id is None:
            self._foreign_reply()
            return
        if not account_id:
            self.send_text("目前沒有等待輸入的驗證碼。要重新登入請傳 /login")
            return
        self._login(account_id, text)

    def _command(self, text: str) -> None:
        try:
            self._dispatch(text)
        except Exception as exc:
            self.send_text(f"失敗：{exc}")

    def _dispatch(self, text: str) -> None:
        name, tokens = command_name(text)
        name = ALIASES.get(name, name)
        if name == "/help":
            self.send_text(HELP)
            return
        if name == "/status":
            self.send_text(self.relogin.status_text())
            return
        if name == "/login":
            self._drop_confirm()
            self._login_command(" ".join(tokens))
            return
        if name == "/ok":
            self._confirm_yes()
            return
        if name == "/no":
            self._drop_confirm()
            self.send_text("已放棄這次訂場")
            return
        if name in {"/cancel", "/book"}:
            self._drop_confirm()
        ctrl = self.relogin
        venue_id, tokens = pop_venue(tokens, ctrl.venues_public())
        account_id, tokens = pop_known(tokens, ctrl.account_ids())
        category, tokens = pop_known(tokens, ctrl.categories(venue_id))
        today = ctrl.today(venue_id)
        if name == "/release":
            self._no_extra(tokens)
            self.send_text(ctrl.release_text(venue_id))
        elif name == "/history":
            self._no_extra(tokens)
            self.send_text(ctrl.history_text(account_id))
        elif name == "/cancel":
            self._no_extra(tokens)
            self._background(lambda: ctrl.cancel_text(account_id, venue_id))
        elif name == "/courts":
            self._no_extra(tokens)
            self._background(lambda: ctrl.courts_text(account_id, venue_id, category))
        elif name == "/slots":
            query_date, time_from, time_to = parse_slots_args(tokens, today)
            self._background(lambda: ctrl.slots_text(account_id, venue_id, category, query_date, time_from, time_to))
        elif name == "/snipe":
            if not tokens:
                self.send_text(ctrl.snipe_text(account_id, venue_id))
                return
            self._drop_confirm()
            targets = parse_snipe_tokens(tokens)
            self._background(lambda: ctrl.arm_text(account_id, venue_id, category, targets))
        elif name == "/weekly":
            if not tokens:
                self.send_text(ctrl.routine_text(account_id, venue_id))
                return
            self._drop_confirm()
            spec = parse_weekly_args(tokens)
            if spec["stop"]:
                self._background(lambda: ctrl.clear_routine_text(account_id, venue_id))
            else:
                weekdays = spec["weekdays"]
                targets = spec["targets"]
                self._background(
                    lambda: ctrl.set_routine_text(account_id, venue_id, category, {"weekdays": weekdays, "targets": targets})
                )
        elif name == "/watch":
            if not tokens:
                self.send_text(ctrl.watch_text(account_id, venue_id))
                return
            self._drop_confirm()
            spec = parse_watch_args(tokens, today)
            if spec["stop"]:
                self._background(lambda: ctrl.stop_watch_text(account_id, venue_id))
            else:
                self._background(lambda: ctrl.start_watch_text(account_id, venue_id, category, spec))
        elif name == "/book":
            query_date, when, court = parse_book_tokens(tokens, today)
            gen = self._confirm_gen

            def preview() -> str:
                plan = ctrl.preview_book(account_id, venue_id, category, court, query_date, when)
                plan["at"] = time.time()
                if not self._set_confirm(plan, gen):
                    return "這次訂場已取消"
                return plan["text"]

            self._background(preview)
        else:
            self.send_text(HELP)

    def _login_command(self, arg: str) -> None:
        accounts = self.relogin.account_ids()
        account_id = arg.strip() or (accounts[0] if len(accounts) == 1 else "")
        if account_id not in accounts:
            self.send_text("請指定帳號：/login " + " | ".join(accounts))
            return
        self.prompt(account_id, "請回覆這張圖上的驗證碼")

    def _confirm_yes(self) -> None:
        plan = self._take_confirm()
        if plan is None:
            self.send_text("沒有待確認的訂場，或已超過 3 分鐘。請重新傳 /book")
            return
        self._background(lambda: self.relogin.place_book(plan))

    def _drop_confirm(self) -> None:
        with self._lock:
            self._confirm = None
            self._confirm_gen += 1

    def _set_confirm(self, plan: dict, gen: int) -> bool:
        with self._lock:
            if gen != self._confirm_gen:
                return False
            self._confirm = plan
            return True

    def _take_confirm(self) -> dict | None:
        with self._lock:
            plan = self._confirm
            self._confirm = None
        if not plan:
            return None
        if time.time() - plan.get("at", 0) > CONFIRM_SEC:
            raise ValueError("確認已超過 3 分鐘，請重新傳 /book")
        return plan

    def _no_extra(self, tokens: list[str]) -> None:
        if tokens:
            raise ValueError("看不懂：" + " ".join(tokens))

    def _background(self, work: Callable[[], str]) -> None:
        """Browser calls can take a minute; acknowledge first so the chat is not silent."""
        self.send_text("查詢中，稍等…")

        def run() -> None:
            try:
                self.send_text(clip(work()))
            except Exception as exc:
                self.send_text(f"失敗：{exc}")

        if self.sync_commands:
            run()
            return
        threading.Thread(target=run, name="sportflex-telegram-cmd", daemon=True).start()

    def _account_for(self, message: dict) -> str | None:
        """Account this code belongs to.

        A reply to a captcha we sent uses that account. A reply to some other message
        (another server's captcha) is None, so we never type that code into this server.
        A bare code uses the latest captcha this server sent, or "" when there is none.
        """
        replied = (message.get("reply_to_message") or {}).get("message_id")
        with self._lock:
            if replied and replied in self._pending:
                return self._pending[replied]
            if replied:
                return None
            return self._last_prompt

    def _foreign_reply(self) -> None:
        with self._lock:
            waiting = list(dict.fromkeys(self._pending.values()))
        if not waiting:
            self.send_text("目前沒有等待輸入的驗證碼。要重新登入請傳 /login")
            return
        names = "、".join(login_prefix(account_id, self.instance) for account_id in waiting)
        who = f"[{self.instance}]" if self.instance else "這台"
        self.send_text(f"這則不是 {who} 的驗證碼。這台正在等：{names}\n請回覆對應的那張圖片。")

    def _login(self, account_id: str, code: str) -> None:
        result = self.relogin.login_with(account_id, code)
        if result.get("ok"):
            with self._lock:
                self._pending = {mid: acc for mid, acc in self._pending.items() if acc != account_id}
                if self._last_prompt == account_id:
                    self._last_prompt = ""
            self.send_text(f"✅ {login_prefix(account_id, self.instance)} 已重新登入")
            return
        # The captcha is single-use; send a fresh one right away.
        self.prompt(account_id, f"❌ 登入失敗：{result.get('message') or '驗證碼錯誤'}\n請回覆這張新的驗證碼")

    def prompt(self, account_id: str, caption: str) -> None:
        try:
            image = self.relogin.captcha_for(account_id)
        except Exception as exc:
            self.send_text(f"取驗證碼失敗：{exc}")
            return
        self.ask_code(account_id, image, f"{login_prefix(account_id, self.instance)} {caption}")

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
