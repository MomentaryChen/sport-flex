"""Which running copy of sport-flex a Telegram login belongs to.

A local ``python -m sportflex ui`` and the Docker server share one bot chat, and
each can ask for a captcha at the same time. The label is how you tell them apart.
"""

from __future__ import annotations

import socket

from sportflex.core.accounts import load_env


def server_label() -> str:
    """SPORT_FLEX_INSTANCE if set (for example ``docker`` or ``筆電``), otherwise the hostname.

    ``serve()`` also fills the env with ``hostname:port`` when nothing else is set,
    so two local UIs on different ports stay distinct.
    """
    name = load_env().get("SPORT_FLEX_INSTANCE", "").strip()
    return name or socket.gethostname()


def login_prefix(account_id: str, instance: str | None = None) -> str:
    """``[docker · default]`` or, with no server name, ``[default]``."""
    label = server_label() if instance is None else instance
    if label:
        return f"[{label} · {account_id}]"
    return f"[{account_id}]"
