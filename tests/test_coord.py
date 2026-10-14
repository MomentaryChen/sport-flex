from sportflex.core.coord import Coordinator, spread_targets
from sportflex.core.rules import BookingRules
from sportflex.runtime.telegram_cmd import format_snipe

RULES = BookingRules()
COURTS = [("A", "場A"), ("B", "場B")]


def target(court_id: str, when: str, name: str = "") -> dict:
    return {"courtId": court_id, "name": name or (court_id or "任一場"), "time": when, "status": "waiting"}


def test_no_peers_keeps_the_request():
    raw = [target("A", "19:00", "場A"), target("", "20:00")]
    placed, notes = spread_targets(RULES, raw, [], COURTS)
    assert [(item["courtId"], item["time"]) for item in placed] == [("A", "19:00"), ("", "20:00")]
    assert notes == []


def test_same_named_court_moves_to_another_court():
    peers = [("me", [target("A", "19:00", "場A")])]
    placed, notes = spread_targets(RULES, [target("A", "19:00", "場A")], peers, COURTS)
    assert placed[0]["courtId"] == "B" and placed[0]["time"] == "19:00"
    assert placed[0]["fromName"] == "場A" and "場B" in notes[0]


def test_any_court_at_the_same_time_takes_the_next_lane():
    peers = [("me", [target("", "19:00")])]
    placed, notes = spread_targets(RULES, [target("", "19:00")], peers, COURTS)
    assert placed[0]["courtId"] == "" and placed[0]["time"] == "19:00" and placed[0]["lane"] == 1
    assert "第 2 面" in notes[0]


def test_one_court_moves_the_later_account_to_another_time():
    peers = [("me", [target("", "19:00"), target("", "20:00")])]
    placed, notes = spread_targets(RULES, [target("", "19:00"), target("", "20:00")], peers, [("A", "場A")])
    # The peer can only book one court, so 19:00 is taken and 20:00 stays with this account.
    assert [item["time"] for item in placed] == ["18:00", "20:00"]
    assert "18:00" in notes[0]


def test_two_named_targets_swap_so_the_cells_do_not_overlap():
    peers = [("me", [target("A", "19:00", "場A"), target("B", "20:00", "場B")])]
    raw = [target("A", "19:00", "場A"), target("B", "20:00", "場B")]
    placed, notes = spread_targets(RULES, raw, peers, COURTS)
    assert [(item["courtId"], item["time"]) for item in placed] == [("B", "19:00"), ("A", "20:00")]
    assert notes


def test_named_court_shifts_time_when_it_is_the_only_court():
    peers = [("me", [target("A", "19:00", "場A")])]
    placed, notes = spread_targets(RULES, [target("A", "19:00", "場A")], peers, [("A", "場A")])
    assert placed[0]["courtId"] == "A" and placed[0]["time"] == "20:00"
    assert "20:00" in notes[0]


def test_claim_is_exclusive_until_released():
    board = Coordinator()
    assert board.try_claim("v", "羽球", "2026-10-28", "me", "A", "19:00")
    assert not board.try_claim("v", "羽球", "2026-10-28", "family", "A", "19:00")
    assert board.try_claim("v", "羽球", "2026-10-28", "family", "B", "19:00")
    board.release_claim("v", "羽球", "2026-10-28", "me", "A", "19:00")
    assert board.try_claim("v", "羽球", "2026-10-28", "family", "A", "19:00")
    assert board.blocked_courts("v", "羽球", "2026-10-28", "19:00", "me") == {"A", "B"}


def test_snipe_text_shows_the_original_target():
    text = format_snipe(
        "一號館",
        {
            "account": "family",
            "phase": "armed",
            "running": True,
            "release": {"targetLabel": "10/28（週三）", "opensLabel": "10/14 06:00"},
            "targets": [{"name": "場B", "time": "19:00", "fromName": "場A", "fromTime": "19:00", "status": "waiting"}],
        },
    )
    assert "場B 19:00（原 場A 19:00）" in text
