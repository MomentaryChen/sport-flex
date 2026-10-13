from pathlib import Path

from sportflex.runtime.captcha_ocr import accept_read, read_captcha

FIXTURES = Path(__file__).parent / "fixtures"


def test_accept_read_keeps_a_safe_uppercase_code():
    assert accept_read("f3wu", 0.99) == "F3WU"


def test_accept_read_submits_confused_characters_and_rejects_a_bad_read():
    assert accept_read("4D1e", 0.99) == "4D1E"
    assert accept_read("25zx", 0.99) == "25ZX"
    assert accept_read("F3WU", 0.5) is None
    assert accept_read("F3W", 0.99) is None
    assert accept_read("F3WUextra", 0.99) is None


def test_read_captcha_ignores_non_images():
    assert read_captcha(b"jpeg") is None
    assert read_captcha(b"") is None


def test_both_fixtures_are_submitted():
    clear = (FIXTURES / "changjia_captcha_clear.jpg").read_bytes()
    ambiguous = (FIXTURES / "changjia_captcha_ambiguous.jpg").read_bytes()
    assert read_captcha(clear) == "F3WU"
    assert read_captcha(ambiguous) == "4D1E"
