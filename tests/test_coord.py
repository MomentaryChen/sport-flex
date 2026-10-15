from sportflex.core.coord import Coordinator, nearest_times, spread_targets
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


def test_nearest_times_follow_venue_hours():
    short = BookingRules(open_time="09:00", close_time="21:00")
    # 21:00 would end at 22:00, after this venue closes; 08:00 is before it opens.
    assert nearest_times(short, "19:00") == ["20:00", "18:00", "17:00", "16:00", "15:00"]
    assert nearest_times(short, "09:00")[0] == "10:00"
    assert "08:00" not in nearest_times(short, "09:00")
    # A two-hour slot at the default 22:00 close cannot start at 21:00.
    assert nearest_times(BookingRules(slot_hours=2), "19:00")[:2] == ["20:00", "18:00"]
    half = BookingRules(open_time="07:30", close_time="12:00", align_minutes=30)
    assert nearest_times(half, "08:00")[:2] == ["08:30", "07:30"]
    assert nearest_times(half, "11:00")[0] == "10:30"


def test_only_court_stays_inside_venue_hours():
    rules = BookingRules(open_time="09:00", close_time="21:00")
    placed, notes = spread_targets(
        rules, [target("A", "20:00", "場A")], [("me", [target("A", "20:00", "場A")])], [("A", "場A")]
    )
    assert placed[0]["courtId"] == "A" and placed[0]["time"] == "19:00"
    assert "19:00" in notes[0]
    early, _notes = spread_targets(
        rules, [target("A", "09:00", "場A")], [("me", [target("A", "09:00", "場A")])], [("A", "場A")]
    )
    assert early[0]["time"] == "10:00"


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
