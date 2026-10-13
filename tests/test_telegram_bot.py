import time
from datetime import timedelta

from sportflex.core.engine import SnipeJob
from sportflex.core.identity import login_prefix
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
    bot.handle(message("/help"))
    assert "/snipe" in api.texts()[-1] and "伺服器 · 帳號" in api.texts()[-1]


def test_prompt_names_the_server():
    api = FakeApi()
    bot = TelegramBot("token", CHAT, api=api, instance="docker")
    bot.relogin = FakePool(("family",))
    bot.prompt("family", "請回覆這張圖上的驗證碼")
    assert api.photos()[-1].startswith("[docker · family] ")
    bot.handle(message("1234"))
    assert api.texts()[-1] == "✅ [docker · family] 已重新登入"


def test_reply_to_another_servers_photo_is_not_used():
    bot, api, pool = make()
    bot.instance = "筆電"
    bot.ask_code("default", b"jpeg", "[筆電 · default] x")
    bot.handle(message("1234", reply_to=999))
    assert pool.logins == []
    assert "不是 [筆電] 的驗證碼" in api.texts()[-1]
    assert "[筆電 · default]" in api.texts()[-1]
    bot.handle(message("1234", reply_to=101))
    assert pool.logins == [("default", "1234")]


def test_reply_without_a_prompt_says_nothing_is_waiting():
    bot, api, pool = make()
    bot.handle(message("1234", reply_to=5))
    assert pool.logins == [] and "沒有等待" in api.texts()[-1]


class Control:
    def __init__(self) -> None:
        self.accounts = ["default"]
        self.calls: list[tuple] = []
        self.plan = {
            "text": "即將訂 羽球場_A\n確認請傳 /ok",
            "account": "default",
            "venue": "xindian",
            "category": "羽球",
            "courtId": "A",
            "date": "2026-10-14",
            "slot": {"time": "19:00 - 20:00", "bookable": True, "status": "", "price": "490", "raw": {"x": 1}},
        }

    def account_ids(self):
        return self.accounts

    def venues_public(self):
        return [{"id": "xindian", "name": "新北市新店國民運動中心"}]

    def categories(self, venue_id):
        return ["羽球", "籃球"]

    def today(self, venue_id):
        from datetime import date

        return date(2026, 10, 13)

    def status_text(self):
        return "default：已登入"

    def release_text(self, venue_id):
        self.calls.append(("release", venue_id))
        return "下一波"

    def history_text(self, account_id):
        self.calls.append(("history", account_id))
        return "沒有紀錄"

    def courts_text(self, account_id, venue_id, category):
        self.calls.append(("courts", account_id, category))
        return "球場"

    def slots_text(self, account_id, venue_id, category, query_date, time_from, time_to):
        self.calls.append(("slots", venue_id, category, query_date, time_from, time_to))
        return "19:00 羽球場_A"

    def snipe_text(self, account_id, venue_id):
        self.calls.append(("snipe-state", account_id))
        return "目前沒有搶訂"

    def arm_text(self, account_id, venue_id, category, targets):
        self.calls.append(("arm", category, targets))
        return "已設定搶訂"

    def cancel_text(self, account_id, venue_id):
        self.calls.append(("cancel", account_id))
        return "已取消搶訂"

    def watch_text(self, account_id, venue_id):
        self.calls.append(("watch-state", account_id))
        return "目前沒有盯場"

    def start_watch_text(self, account_id, venue_id, category, spec):
        self.calls.append(("watch", category, spec["date"], spec["timeFrom"], spec["court"]))
        return "盯場中"

    def stop_watch_text(self, account_id, venue_id):
        self.calls.append(("unwatch", account_id))
        return "已停止盯場"

    def preview_book(self, account_id, venue_id, category, court, query_date, when):
        self.calls.append(("preview", category, court, query_date, when))
        return dict(self.plan)

    def place_book(self, plan):
        self.calls.append(("place", plan["courtId"], plan["date"], plan["slot"]["time"]))
        return "已送出訂單"

    def captcha_for(self, account_id):
        return b"jpeg"

    def login_with(self, account_id, code):
        return {"ok": code == "1234", "message": ""}


def control_bot():
    api = FakeApi()
    ctrl = Control()
    bot = TelegramBot("token", CHAT, api=api)
    bot.relogin = ctrl
    bot.sync_commands = True
    return bot, api, ctrl


def test_slots_snipe_and_release_commands():
    bot, api, ctrl = control_bot()
    bot.handle(message("/slots 新店 籃球 明天 晚上"))
    assert ctrl.calls[-1] == ("slots", "xindian", "籃球", "2026-10-14", "18:00", "23:00")
    assert api.texts()[-1] == "19:00 羽球場_A"
    bot.handle(message("/snipe"))
    assert ctrl.calls[-1] == ("snipe-state", None)
    assert "查詢中" not in api.texts()[-1]
    bot.handle(message("/snipe A 19:00 20"))
    assert ctrl.calls[-1] == (
        "arm",
        None,
        [{"court": "A", "time": "19:00"}, {"court": "", "time": "20:00"}],
    )
    bot.handle(message("/release"))
    assert ctrl.calls[-1] == ("release", None)
    bot.handle(message("/watch"))
    assert ctrl.calls[-1] == ("watch-state", None)
    bot.handle(message("/watch 明天 晚上 A"))
    assert ctrl.calls[-1] == ("watch", None, "2026-10-14", "18:00", "A")
    bot.handle(message("/watch off"))
    assert ctrl.calls[-1] == ("unwatch", None)
    bot.handle(message("/board@SportBot 19:00"))
    assert ctrl.calls[-1][0] == "slots" and ctrl.calls[-1][-2:] == ("19:00", "20:00")


def test_book_waits_for_ok_and_can_be_dropped():
    bot, api, ctrl = control_bot()
    bot.handle(message("/book 明天 19:00 羽球場_A"))
    assert ctrl.calls[-1][0] == "preview"
    assert "確認請傳 /ok" in api.texts()[-1]
    bot.handle(message("/no"))
    assert "放棄" in api.texts()[-1]
    bot.handle(message("/ok"))
    assert "沒有待確認" in api.texts()[-1]
    assert not any(call[0] == "place" for call in ctrl.calls)

    bot.handle(message("/grab 10/14 19 A"))
    bot.handle(message("/ok"))
    assert ctrl.calls[-1] == ("place", "A", "2026-10-14", "19:00 - 20:00")
    assert api.texts()[-1] == "已送出訂單"


def test_book_confirm_expires():
    bot, api, ctrl = control_bot()
    bot.handle(message("/book 明天 19:00 A"))
    bot._confirm["at"] -= 200
    bot.handle(message("/ok"))
    assert "超過 3 分鐘" in api.texts()[-1]
    assert not any(call[0] == "place" for call in ctrl.calls)


def test_snipe_replaces_pending_book():
    bot, api, ctrl = control_bot()
    bot.handle(message("/book 明天 19:00 A"))
    bot.handle(message("/snipe 19:00"))
    bot.handle(message("/ok"))
    assert "沒有待確認" in api.texts()[-1] or "超過 3 分鐘" in api.texts()[-1]
    assert not any(call[0] == "place" for call in ctrl.calls)


def test_unknown_command_shows_help_and_bad_slots_fail():
    bot, api, ctrl = control_bot()
    bot.handle(message("/nope"))
    assert "/slots" in api.texts()[-1]
    bot.handle(message("/slots 羽球場"))
    assert api.texts()[-1].startswith("失敗：")


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
    assert len(bot.photos) == 6  # 3 unanswered, then web captcha (1) plus 2 more auto prompts
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


def test_confident_ocr_logs_in_without_asking(monkeypatch):
    from sportflex.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "read_captcha", lambda image: "F3WU")
    worker, bot = make_worker(monkeypatch)

    class Logging:
        def __init__(self):
            self.codes = []
            self.refreshes = 0

        def captcha(self, username, password):
            return b"\xff\xd8fake"

        def login(self, code):
            self.codes.append(code)
            return {"ok": True, "message": "登入成功"}

        def refresh_session(self):
            self.refreshes += 1
            return {"loggedIn": self.refreshes > 1, "banner": ""}

    provider = Logging()
    worker._logged_out()
    worker._maybe_prompt_relogin(provider)
    assert provider.codes == ["F3WU"] and bot.photos == []
    assert worker.session["loggedIn"] and not worker._relogin_needed


def test_uncertain_ocr_sends_the_image_without_submitting(monkeypatch):
    from sportflex.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "read_captcha", lambda image: None)
    worker, bot = make_worker(monkeypatch)

    class Holding:
        def __init__(self):
            self.calls = 0
            self.codes = []

        def captcha(self, username, password):
            self.calls += 1
            return b"\xff\xd8same"

        def login(self, code):
            self.codes.append(code)
            return {"ok": False, "message": "不該送出"}

        def refresh_session(self):
            return {"loggedIn": False, "banner": ""}

    provider = Holding()
    worker._logged_out()
    worker._maybe_prompt_relogin(provider)
    assert provider.calls == 2 and provider.codes == []
    assert bot.photos and "請回覆這張圖上的驗證碼" in bot.photos[0]


def test_rejected_ocr_reloads_the_image_for_a_person(monkeypatch):
    from sportflex.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "read_captcha", lambda image: "F3WU" if image == b"first" else None)
    worker, bot = make_worker(monkeypatch)

    class Reloading:
        def __init__(self):
            self.calls = 0
            self.codes = []

        def captcha(self, username, password):
            self.calls += 1
            return b"first" if self.calls == 1 else b"second"

        def login(self, code):
            self.codes.append(code)
            return {"ok": False, "message": "驗證碼錯誤"}

        def refresh_session(self):
            return {"loggedIn": False, "banner": ""}

    provider = Reloading()
    worker._logged_out()
    worker._maybe_prompt_relogin(provider)
    assert provider.codes == ["F3WU"] and provider.calls == 2 and worker._prompts == 1
    assert bot.photos and "自動辨識未通過，請回覆這張新的驗證碼" in bot.photos[0]


def test_second_ocr_submit_logs_in(monkeypatch):
    from sportflex.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "read_captcha", lambda image: "F3WU")
    worker, bot = make_worker(monkeypatch)

    class Twice:
        def __init__(self):
            self.calls = 0
            self.codes = []
            self.refreshes = 0

        def captcha(self, username, password):
            self.calls += 1
            return f"img{self.calls}".encode()

        def login(self, code):
            self.codes.append(code)
            return {"ok": len(self.codes) == 2, "message": "登入成功" if len(self.codes) == 2 else "驗證碼錯誤"}

        def refresh_session(self):
            self.refreshes += 1
            return {"loggedIn": self.refreshes > 1, "banner": ""}

    provider = Twice()
    worker._logged_out()
    worker._maybe_prompt_relogin(provider)
    assert provider.codes == ["F3WU", "F3WU"] and provider.calls == 2 and bot.photos == []
    assert worker.session["loggedIn"] and not worker._relogin_needed


def test_two_rejected_ocr_submits_then_ask_a_person(monkeypatch):
    from sportflex.runtime import worker as worker_module

    monkeypatch.setattr(worker_module, "read_captcha", lambda image: "F3WU")
    worker, bot = make_worker(monkeypatch)

    class AlwaysWrong:
        def __init__(self):
            self.calls = 0
            self.codes = []

        def captcha(self, username, password):
            self.calls += 1
            return f"img{self.calls}".encode()

        def login(self, code):
            self.codes.append(code)
            return {"ok": False, "message": "驗證碼錯誤"}

        def refresh_session(self):
            return {"loggedIn": False, "banner": ""}

    provider = AlwaysWrong()
    worker._logged_out()
    worker._maybe_prompt_relogin(provider)
    assert provider.codes == ["F3WU", "F3WU"] and provider.calls == 3 and worker._prompts == 1
    assert bot.photos and "自動辨識未通過，請回覆這張新的驗證碼" in bot.photos[0]


def test_startup_logout_prompts_with_reason(monkeypatch):
    worker, bot = make_worker(monkeypatch)
    worker._logged_out("服務啟動時發現尚未登入")
    worker._logged_out()  # a job noticing the same logout keeps the first reason
    worker._maybe_prompt_relogin(CaptchaProvider())
    assert bot.photos == [f"{login_prefix('me')} me 服務啟動時發現尚未登入，請回覆這張圖上的驗證碼（第 1/3 次）"]


def test_relogin_caption_uses_instance_name(monkeypatch):
    monkeypatch.setenv("SPORT_FLEX_INSTANCE", "docker")
    worker, bot = make_worker(monkeypatch)
    worker._logged_out("服務啟動時發現尚未登入")
    worker._maybe_prompt_relogin(CaptchaProvider())
    assert bot.photos == ["[docker · me] me 服務啟動時發現尚未登入，請回覆這張圖上的驗證碼（第 1/3 次）"]


def test_web_captcha_can_skip_the_telegram_photo(monkeypatch):
    worker, bot = make_worker(monkeypatch)
    worker.session["loggedIn"] = False
    worker.call = lambda fn, timeout=120: fn(CaptchaProvider())
    image = worker.captcha_image(notify=False)
    assert image == b"jpeg" and bot.photos == []
    assert worker._prompts == 1  # still a new round, so the auto loop does not immediately send another


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


class SessionProvider:
    def __init__(self, logged_in: bool) -> None:
        self.logged_in = logged_in
        self.captcha_calls = 0

    def refresh_session(self):
        return {"loggedIn": self.logged_in, "banner": ""}

    def captcha(self, username, password):
        self.captcha_calls += 1
        return b"jpeg"


def test_relogin_prompt_skips_when_session_already_logged_in(monkeypatch):
    worker, bot = make_worker(monkeypatch)
    worker._logged_out()
    provider = SessionProvider(logged_in=True)
    worker._maybe_prompt_relogin(provider)
    assert not bot.photos and not worker._relogin_needed


def test_login_success_clears_relogin_even_if_refresh_lags(monkeypatch):
    worker, bot = make_worker(monkeypatch)
    worker._logged_out()

    class LaggingLogin:
        def __init__(self):
            self.refresh_calls = 0

        def login(self, code):
            return {"ok": True, "message": "登入成功"}

        def refresh_session(self):
            self.refresh_calls += 1
            return {"loggedIn": self.refresh_calls >= 2, "banner": ""}

    provider = LaggingLogin()
    worker.call = lambda fn, timeout=120: fn(provider)
    result = worker.login("1234")
    assert result["ok"] and not worker._relogin_needed


def test_relogin_prompt_waits_while_snipe_firing(monkeypatch, venue):  # noqa: F811
    worker, bot = make_worker(monkeypatch)
    worker._logged_out()
    job = SnipeJob(venue, "me", lambda result: None)
    job.arm("羽球", [{"courtId": "A", "name": "A", "time": "19:00"}])
    job.spec["phase"] = "firing"
    worker.snipes[venue.id] = job
    provider = SessionProvider(logged_in=False)
    worker._maybe_prompt_relogin(provider)
    assert not bot.photos and provider.captcha_calls == 0
