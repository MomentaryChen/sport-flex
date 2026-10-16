import pytest

from sportflex.core import identity

REAL_APP_ENV = identity.app_env


@pytest.fixture(autouse=True)
def production_env(monkeypatch):
    """Most tests exercise ordering and Telegram, which only production allows.

    Patched on the module rather than the process env, because a real .env file wins over it.
    """
    monkeypatch.setattr(identity, "app_env", lambda: identity.PRODUCTION)
