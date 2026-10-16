"""Which running copy of sport-flex a Telegram login belongs to.

Captcha photos are stamped with this label. Only one process may poll a bot
token; a second copy is refused so the same account cannot book twice.
"""

from __future__ import annotations

import socket

from sportflex.core.accounts import load_env

PRODUCTION = "production"
DEVELOPMENT = "development"
_ENV_ALIASES = {"production": PRODUCTION, "prod": PRODUCTION, "development": DEVELOPMENT, "dev": DEVELOPMENT}


def app_env() -> str:
    """SPORT_FLEX_ENV: ``production`` or ``development`` (the default).

    Only production places orders, talks to Telegram, and takes the one-per-machine
    lock, so development copies can run side by side without booking twice.
    """
    raw = load_env().get("SPORT_FLEX_ENV", "").strip().lower()
    if not raw:
        return DEVELOPMENT
    env = _ENV_ALIASES.get(raw)
    if env is None:
        raise ValueError(f"SPORT_FLEX_ENV 必須是 production 或 development（目前是「{raw}」）")
    return env


def is_production() -> bool:
    return app_env() == PRODUCTION


def server_label() -> str:
    """SPORT_FLEX_INSTANCE if set (for example ``docker`` or ``筆電``), otherwise the hostname.

    ``serve()`` fills the env with ``hostname:port`` when nothing else is set,
    so a notice still says which machine it came from.
    """
    name = load_env().get("SPORT_FLEX_INSTANCE", "").strip()
    return name or socket.gethostname()


def login_prefix(account_id: str, instance: str | None = None) -> str:
    """``[docker · default]`` or, with no server name, ``[default]``."""
    label = server_label() if instance is None else instance
    if label:
        return f"[{label} · {account_id}]"
    return f"[{account_id}]"
