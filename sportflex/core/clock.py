"""Local clock compared with a site's HTTP Date header."""

from __future__ import annotations

import math
from datetime import timezone
from email.utils import parsedate_to_datetime


def clock_offset_seconds(date_header: str | None, observed_at: float) -> float | None:
    """Seconds the local clock is ahead of the site.

    ``observed_at`` is the local unix time when the response headers arrived.
    Positive means this machine is ahead of the site. The header only has whole
    seconds, so the tenths also include that truncation.
    """
    if not date_header or not isinstance(date_header, str):
        return None
    try:
        site = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if site.tzinfo is None:
        site = site.replace(tzinfo=timezone.utc)
    offset = observed_at - site.timestamp()
    if not math.isfinite(offset):
        return None
    return offset


def format_clock_offset(seconds: float) -> str:
    """``+0.3 秒`` / ``-1.2 秒``. A difference under 0.05s is shown as ``+0.0 秒``."""
    text = f"{seconds:+.1f}"
    if text in {"+0.0", "-0.0"}:
        text = "+0.0"
    return f"{text} 秒"
