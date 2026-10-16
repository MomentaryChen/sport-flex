import pytest

from sportflex.core import identity
from sportflex.core.engine import DEV_NO_ORDERS, grab
from sportflex.core.notify import default_notifier
from sportflex.runtime import pool
from tests.conftest import REAL_APP_ENV


@pytest.fixture
def env(monkeypatch):
    """SPORT_FLEX_ENV and friends as load_env would return them."""
    values: dict[str, str] = {}
    monkeypatch.setattr(identity, "app_env", REAL_APP_ENV)
    monkeypatch.setattr(identity, "load_env", lambda: dict(values))
    monkeypatch.setattr(pool, "load_env", lambda: dict(values))
    monkeypatch.setattr("sportflex.core.notify.load_env", lambda: dict(values))
    return values


def test_defaults_to_development(env):
    assert identity.app_env() == "development"
    assert identity.is_production() is False


@pytest.mark.parametrize("raw", ["production", "prod", " Production "])
def test_production_aliases(env, raw):
    env["SPORT_FLEX_ENV"] = raw
    assert identity.is_production() is True


def test_unknown_env_is_refused(env):
    env["SPORT_FLEX_ENV"] = "staging"
    with pytest.raises(ValueError, match="SPORT_FLEX_ENV"):
        identity.app_env()


def test_development_never_submits(env):
    class Provider:
        def submit(self, *args):
            raise AssertionError("development must not submit an order")

    with pytest.raises(RuntimeError, match="開發環境"):
        grab(Provider(), None, None, "2026-10-21", None)  # type: ignore[arg-type]
    assert "SPORT_FLEX_ENV=production" in DEV_NO_ORDERS


def test_development_skips_telegram(env):
    env.update({"SPORT_FLEX_TELEGRAM_BOT_TOKEN": "t", "SPORT_FLEX_TELEGRAM_CHAT_ID": "1"})
    assert pool.bot_from_env() is None
    assert "Telegram" not in default_notifier().channels
    env["SPORT_FLEX_ENV"] = "production"
    assert pool.bot_from_env() is not None
    assert "Telegram" in default_notifier().channels
