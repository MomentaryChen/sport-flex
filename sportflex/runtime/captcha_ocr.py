"""Read the Changjia login captcha, and abstain unless the guess is safe to submit.

The image is four letters or digits with crossing lines. A wrong submit burns that
image, so a guess is returned only when every character is outside the pairs this
model mixes up (4/A, 1/I, 0/O, 2/Z, 5/S, 8/B, K/X). Anything else is left for a person,
on the same image.
"""

from __future__ import annotations

import threading

# Uppercase, because the glyphs on this captcha are capitals.
_SAFE_CHARS = frozenset("3679CDEFGHJLMNPRTUVWXY")
_CODE_LENGTH = 4
_MIN_CONFIDENCE = 0.95

_lock = threading.Lock()
_engine = None


def accept_read(text: str, confidence: float) -> str | None:
    """The submit/abstain rule, separate from the model so tests can call it directly."""
    code = "".join(text.split()).upper()
    if len(code) != _CODE_LENGTH or any(char not in _SAFE_CHARS for char in code):
        return None
    if confidence < _MIN_CONFIDENCE:
        return None
    return code


def read_captcha(image: bytes) -> str | None:
    """A code to submit, or None when the image should be shown to a person instead."""
    if len(image) < 32 or not (image.startswith(b"\x89PNG") or image.startswith(b"\xff\xd8")):
        return None
    try:
        text, confidence = _classify(image)
    except Exception:
        return None
    return accept_read(text, confidence)


def _classify(image: bytes) -> tuple[str, float]:
    with _lock:
        engine = _load_engine()
        result = engine.classification(image, probability=True)
    if isinstance(result, str):
        return result, 1.0
    return str(result.get("text") or ""), float(result.get("confidence") or 0.0)


def _load_engine():
    global _engine
    if _engine is None:
        import ddddocr

        _engine = ddddocr.DdddOcr(show_ad=False)
    return _engine
