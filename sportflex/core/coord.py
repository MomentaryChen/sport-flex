"""Keep family accounts from booking the same court at the same time.

When several accounts snipe one release, the later one is moved onto a different
court at the requested time, or onto a nearby time when no other court is left.
During the wave a shared claim board stops two browsers from submitting the same cell.
"""

from __future__ import annotations

import copy
import threading

from sportflex.core.rules import BookingRules

# How far a blocked account may move, in either direction.
SHIFT_SPAN_MIN = 4 * 60


def demand_cap(rules: BookingRules, court_count: int) -> int:
    """How many of an account's targets can actually land, given the court count."""
    cap = rules.max_bookings_per_day
    if court_count and rules.distinct_courts:
        return min(cap, court_count)
    return cap


def nearest_times(rules: BookingRules, origin: str, span_min: int = SHIFT_SPAN_MIN) -> list[str]:
    """Aligned start times around `origin`, later slot first, then the earlier one, and so on.

    A candidate is kept only when the whole slot fits in the venue's open_time–close_time.
    """
    origin_min = _minutes(origin)
    step = rules.align_minutes
    slot = int(round(rules.slot_hours * 60))
    first = _minutes(rules.open_time)
    last = _minutes(rules.close_time) - slot
    found = []
    for delta in range(step, span_min + 1, step):
        for sign in (1, -1):
            minute = origin_min + sign * delta
            if minute < first or minute > last or minute % step:
                continue
            found.append(_hhmm(minute))
    return found


def spread_targets(
    rules: BookingRules,
    targets: list[dict],
    peers: list[tuple[str, list[dict]]],
    courts: list[tuple[str, str]],
) -> tuple[list[dict], list[str]]:
    """Return targets that no longer sit on a cell another account already holds.

    `peers` is `(account id, targets)` for accounts already armed on this venue, sport and day.
    `courts` is `(id, name)` in venue order; empty means the court list is not known yet, so only
    an identical named court is moved to another time and "any court" targets only get a lane.
    """
    court_ids = [court_id for court_id, _name in courts]
    names = {court_id: name for court_id, name in courts}
    cap = demand_cap(rules, len(court_ids))
    specific: set[tuple[str, str]] = set()
    any_at: dict[str, int] = {}
    for _account, peer_targets in sorted(peers, key=lambda item: item[0]):
        peer_specific, peer_any = _occupy(peer_targets, cap)
        specific |= peer_specific
        for when, count in peer_any.items():
            any_at[when] = any_at.get(when, 0) + count

    placed: list[dict] = []
    notes: list[str] = []
    used_courts: set[str] = set()

    def blocked(court_id: str, when: str) -> bool:
        if (court_id, when) in specific:
            return True
        return bool(rules.distinct_courts and court_id and court_id in used_courts)

    def court_open(court_id: str, when: str) -> bool:
        """True when `court_id` at `when` is not already taken by a sibling or by us."""
        if blocked(court_id, when):
            return False
        if not court_ids:
            return True
        if court_id not in names:
            return any_at.get(when, 0) == 0
        free = [cid for cid in court_ids if not blocked(cid, when)]
        # Earlier "any court" targets take the first free courts, same order as the firing lanes.
        return court_id not in set(free[: any_at.get(when, 0)])

    def room_for_any(when: str) -> bool:
        if not court_ids:
            return True
        free = [cid for cid in court_ids if not blocked(cid, when)]
        return any_at.get(when, 0) < len(free)

    for index, raw in enumerate(targets):
        court_id = raw["courtId"]
        name = raw["name"]
        when = raw["time"]
        # Times later in this request still need a slot; don't land on them if another time is free.
        avoid_times = {item["time"] for item in targets[index + 1 :]}
        new_court, new_name, new_time = court_id, name, when
        if court_id and not court_open(court_id, when):
            alt = next((cid for cid in court_ids if court_open(cid, when)), "")
            if alt:
                new_court, new_name, new_time = alt, names.get(alt, alt), when
            else:
                shifted = _shift_named(rules, court_id, name, when, names, court_open, avoid_times)
                if shifted:
                    new_court, new_name, new_time = shifted
        elif not court_id and not room_for_any(when):
            shifted = _shift_any(rules, when, room_for_any, avoid_times)
            if shifted:
                new_time = shifted
        lane = 0 if new_court else any_at.get(new_time, 0)
        item = {
            "courtId": new_court,
            "name": new_name if new_court else "任一場",
            "time": new_time,
            "status": "waiting",
        }
        if lane:
            item["lane"] = lane
        moved = new_court != court_id or new_time != when
        if moved:
            item["fromName"] = name
            item["fromTime"] = when
            notes.append(_moved(name, when, item["name"], new_time))
        elif not new_court and not room_for_any(new_time):
            notes.append(f"無法和其他帳號錯開 {name} {when}，仍搶原目標")
        elif lane:
            notes.append(f"{when} 會從第 {lane + 1} 面空場開始，避開其他帳號")
        elif court_id and not court_open(court_id, when):
            notes.append(f"無法和其他帳號錯開 {name} {when}，仍搶原目標")
        placed.append(item)
        if new_court:
            specific.add((new_court, new_time))
            if rules.distinct_courts:
                used_courts.add(new_court)
        else:
            any_at[new_time] = any_at.get(new_time, 0) + 1
    return placed, notes


def _shift_named(rules, court_id, name, when, names, court_open, avoid_times: set[str]):
    fallback = None
    for at in nearest_times(rules, when):
        chosen = None
        if court_open(court_id, at):
            chosen = (court_id, name, at)
        else:
            for cid, label in names.items():
                if court_open(cid, at):
                    chosen = (cid, label, at)
                    break
        if chosen is None:
            continue
        if at in avoid_times:
            fallback = fallback or chosen
            continue
        return chosen
    return fallback


def _shift_any(rules, when, room_for_any, avoid_times: set[str]) -> str:
    fallback = ""
    for at in nearest_times(rules, when):
        if not room_for_any(at):
            continue
        if at in avoid_times:
            fallback = fallback or at
            continue
        return at
    return fallback


def _occupy(targets: list[dict], cap: int) -> tuple[set[tuple[str, str]], dict[str, int]]:
    """Cells a peer will actually chase. A booked target occupies the court it got."""
    specific: set[tuple[str, str]] = set()
    any_at: dict[str, int] = {}
    usable = [item for item in targets if item.get("status") != "skipped"][:cap]
    for item in usable:
        when = str(item.get("time") or "")
        if item.get("status") == "booked" and item.get("bookedCourtId"):
            specific.add((item["bookedCourtId"], when))
            continue
        court_id = str(item.get("courtId") or "")
        if court_id:
            specific.add((court_id, when))
        elif when:
            any_at[when] = any_at.get(when, 0) + 1
    return specific, any_at


def _moved(old_name: str, old_time: str, new_name: str, new_time: str) -> str:
    if old_time != new_time and old_name != new_name:
        return f"和其他帳號錯開：{old_name} {old_time} 改為 {new_name} {new_time}"
    if old_time != new_time:
        return f"和其他帳號錯開：{old_name} {old_time} 改搶 {new_time}"
    return f"和其他帳號錯開：{old_time} 改搶 {new_name}（避開 {old_name}）"


def _minutes(label: str) -> int:
    hour, minute = [int(part) for part in label.split(":")]
    return hour * 60 + minute


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


class Coordinator:
    """Plans and live claims shared by every account on this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[tuple[str, str, str, str], list[dict]] = {}
        self._claims: dict[tuple[str, str, str, str, str], str] = {}

    def assign(
        self,
        rules: BookingRules,
        venue: str,
        category: str,
        target_date: str,
        account: str,
        targets: list[dict],
        courts: list[tuple[str, str]],
    ) -> tuple[list[dict], list[str]]:
        """Spread `targets` around accounts already armed, then remember the plan."""
        with self._lock:
            self._release_account(venue, account)
            peers = self._peers(venue, category, target_date, account)
            adjusted, notes = spread_targets(rules, targets, peers, courts)
            self._plans[(venue, category, target_date, account)] = copy.deepcopy(adjusted)
            return adjusted, notes

    def has_peers(self, venue: str, category: str, target_date: str, account: str) -> bool:
        with self._lock:
            return any(
                peer != account and venue_id == venue and cat == category and day == target_date
                for venue_id, cat, day, peer in self._plans
            )

    def publish(self, venue: str, category: str, target_date: str, account: str, targets: list[dict]) -> None:
        with self._lock:
            self._plans[(venue, category, target_date, account)] = copy.deepcopy(targets)

    def unpublish(self, venue: str, category: str, target_date: str, account: str) -> None:
        with self._lock:
            self._plans.pop((venue, category, target_date, account), None)

    def release_account(self, venue: str, account: str) -> None:
        with self._lock:
            self._release_account(venue, account)

    def try_claim(self, venue: str, category: str, target_date: str, account: str, court_id: str, when: str) -> bool:
        """Reserve one cell for `account`. False when another account already holds it."""
        with self._lock:
            key = (venue, category, target_date, court_id, when)
            owner = self._claims.get(key)
            if owner is not None and owner != account:
                return False
            self._claims[key] = account
            return True

    def release_claim(
        self, venue: str, category: str, target_date: str, account: str, court_id: str, when: str
    ) -> None:
        with self._lock:
            key = (venue, category, target_date, court_id, when)
            if self._claims.get(key) == account:
                del self._claims[key]

    def blocked_courts(self, venue: str, category: str, target_date: str, when: str, account: str) -> set[str]:
        """Courts another account has already been given, or is submitting, at `when`."""
        with self._lock:
            taken = {
                court_id
                for (venue_id, cat, day, court_id, time), owner in self._claims.items()
                if venue_id == venue and cat == category and day == target_date and time == when and owner != account
            }
            for (venue_id, cat, day, peer), targets in self._plans.items():
                if venue_id != venue or cat != category or day != target_date or peer == account:
                    continue
                for item in targets:
                    if item.get("time") != when or item.get("status") == "skipped":
                        continue
                    if item.get("status") == "booked" and item.get("bookedCourtId"):
                        taken.add(item["bookedCourtId"])
                    elif item.get("courtId"):
                        taken.add(item["courtId"])
            return taken

    def _peers(self, venue: str, category: str, target_date: str, account: str) -> list[tuple[str, list[dict]]]:
        found = [
            (peer, copy.deepcopy(targets))
            for (venue_id, cat, day, peer), targets in self._plans.items()
            if venue_id == venue and cat == category and day == target_date and peer != account
        ]
        return sorted(found, key=lambda item: item[0])

    def _release_account(self, venue: str, account: str) -> None:
        stale = [key for key, owner in self._claims.items() if key[0] == venue and owner == account]
        for key in stale:
            del self._claims[key]
