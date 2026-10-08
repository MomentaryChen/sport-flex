import time
from datetime import timedelta

from sportflex.core.engine import SnipeJob
from sportflex.runtime.telegram_bot import TelegramBot
from tests.test_engine import FakeProvider, no_disk, opens, venue  # noqa: F401  (fixtures)

CHAT = "1391"


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.next_id = 100

    def __call__(self, method, params, files=None):
        self.calls.append((method, params, files))
        if method == "sendPhoto":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getUpdates":
            return []
        return {}

    def texts(self) -> list[str]:
        return [params["text"] for method, params, _ in self.calls if method == "sendMessage"]

    def photos(self) -> list[str]:
        return [params["caption"] for method, params, _ in self.calls if method == "sendPhoto"]


class FakePool:
    def __init__(self, accounts=("default",), good_code="1234") -> None:
        self.accounts = list(accounts)
        self.good_code = good_code
        self.logins: list[tuple[str, str]] = []

    def account_ids(self):
        return self.accounts

    def status_text(self):
        return "default：未登入"

    def captcha_for(self, account_id):
        return b"jpeg"

    def login_with(self, account_id, code):
        self.logins.append((account_id, code))
        return {"ok": code == self.good_code, "message": "" if code == self.good_code else "驗證碼錯誤"}


def make(accounts=("default",)):
    api, pool = FakeApi(), FakePool(accounts)
    bot = TelegramBot("token", CHAT, api=api)
    bot.relogin = pool
    return bot, api, pool


def message(text, chat=CHAT, reply_to=None):
    msg = {"chat": {"id": int(chat)}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": msg}


def test_code_reply_logs_in():
    bot, api, pool = make()
    bot.ask_code("default", b"jpeg", "請回覆驗證碼")
    bot.handle(message(" 1234 ", reply_to=101))
    assert pool.logins == [("default", "1234")]
    assert "已重新登入" in api.texts()[-1]


def test_bare_code_goes_to_latest_prompt():
    bot, api, pool = make(("me", "family"))
    bot.ask_code("me", b"jpeg", "x")
    bot.ask_code("family", b"jpeg", "y")
    bot.handle(message("1234", reply_to=101))  # quoted: goes to "me"
    bot.handle(message("1234"))  # bare: goes to the latest prompt
    assert pool.logins == [("me", "1234"), ("family", "1234")]


def test_wrong_code_sends_a_fresh_captcha():
    bot, api, pool = make()
    bot.ask_code("default", b"jpeg", "x")
    bot.handle(message("0000"))
    assert len(api.photos()) == 2 and "登入失敗" in api.photos()[-1]


def test_ignores_other_chats():
    bot, api, pool = make()
    bot.ask_code("default", b"jpeg", "x")
    bot.handle(message("1234", chat="999"))
    assert pool.logins == [] and api.texts() == []


def test_code_without_prompt_is_not_typed_in():
    bot, api, pool = make()
    bot.handle(message("1234"))
    assert pool.logins == [] and "沒有等待" in api.texts()[-1]


def test_after_success_a_stray_code_is_ignored():
    bot, api, pool = make()
    bot.ask_code("default", b"jpeg", "x")
    bot.handle(message("1234"))
    bot.handle(message("5678"))
    assert pool.logins == [("default", "1234")]


def test_commands():
    bot, api, pool = make(("me", "family"))
    bot.handle(message("/status"))
    assert api.texts()[-1] == "default：未登入"
    bot.handle(message("/login"))
    assert "請指定帳號" in api.texts()[-1]
    bot.handle(message("/login family"))
    assert api.photos()[-1].startswith("[family]")


def test_skip_backlog_ignores_old_messages():
    def fake(method, params, files=None):
        return [{"update_id": 41}] if method == "getUpdates" else {}

    bot = TelegramBot("token", CHAT, api=fake)
    bot._skip_backlog()
    assert bot._offset == 42


class CaptchaProvider:
    def captcha(self, username, password):
        return b"jpeg"


class RecordingBot:
    def __init__(self):
        self.photos, self.texts = [], []

    def ask_code_async(self, account_id, image, caption):
        self.photos.append(caption)

    def send_text_async(self, text):
        self.texts.append(text)


def test_relogin_stops_after_three_unanswered_prompts(monkeypatch):
    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime import worker as worker_module

    monkeypatch.setenv("T_USER", "u")
    monkeypatch.setenv("T_PASS", "p")
    account = Account(id="me", provider="changjia", username_env="T_USER", password_env="T_PASS", profile="x")
    bot = RecordingBot()
    worker = worker_module.AccountWorker(account, {}, ConsoleNotifier(), bot=bot)
    worker._logged_out()
    provider = CaptchaProvider()

    def due():  # pretend REPROMPT_SEC has passed
        worker._prompted_at = 0
        worker._maybe_prompt_relogin(provider)

    for _ in range(5):
        due()
    assert len(bot.photos) == 3 and bot.photos[-1].endswith("（第 3/3 次）")
    assert len(bot.texts) == 1 and "/login me" in bot.texts[0]  # told once, then quiet

    worker.session["loggedIn"] = False
    worker.call = lambda fn, timeout=120: fn(provider)  # /login runs captcha_image on the browser thread
    worker.captcha_image()
    for _ in range(4):
        due()
    assert len(bot.photos) == 5  # /login's captcha counted as 1 of 3 (sent by the bot itself), then 2 more
    assert len(bot.texts) == 2


def make_worker(monkeypatch):
    from sportflex.core.accounts import Account
    from sportflex.core.notify import ConsoleNotifier
    from sportflex.runtime.worker import AccountWorker

    monkeypatch.setenv("T_USER", "u")
    monkeypatch.setenv("T_PASS", "p")
    account = Account(id="me", provider="changjia", username_env="T_USER", password_env="T_PASS", profile="x")
    bot = RecordingBot()
    return AccountWorker(account, {}, ConsoleNotifier(), bot=bot), bot


def test_startup_logout_prompts_with_reason(monkeypatch):
    worker, bot = make_worker(monkeypatch)
    worker._logged_out("服務啟動時發現尚未登入")
    worker._logged_out()  # a job noticing the same logout keeps the first reason
    worker._maybe_prompt_relogin(CaptchaProvider())
    assert bot.photos == ["[me] me 服務啟動時發現尚未登入，請回覆這張圖上的驗證碼（第 1/3 次）"]


def test_failed_captcha_does_not_use_a_try(monkeypatch):
    from sportflex.runtime import worker as worker_module

    class Broken:
        def captcha(self, username, password):
            raise RuntimeError("network down")

    worker, bot = make_worker(monkeypatch)
    worker._logged_out()
    worker._maybe_prompt_relogin(Broken())
    assert worker._prompts == 0 and not bot.photos
    wait = worker._prompted_at + worker_module.REPROMPT_SEC - time.time()
    assert 0 < wait <= worker_module.CAPTCHA_RETRY_SEC  # retried in about a minute, not ten
    worker._prompted_at -= worker_module.CAPTCHA_RETRY_SEC
    worker._maybe_prompt_relogin(CaptchaProvider())
    assert bot.photos and bot.photos[0].endswith("（第 1/3 次）")


def test_snipe_reports_logout_to_worker(venue):  # noqa: F811
    logouts = []
    job = SnipeJob(venue, "me", lambda result: None, on_logout=lambda: logouts.append(1))
    job.arm("羽球", [{"courtId": "A", "name": "A", "time": "19:00"}])
    job.tick(FakeProvider(logged_in=False), opens(job) - timedelta(seconds=30))
    assert logouts
