"""Hit rate, release timing, and per-court results from snipe_runs rows."""

from __future__ import annotations

# The site rarely opens on the advertised second. Three waves is enough to trust a median;
# fewer than that and the first query still goes out at opens_at.
OFFSET_MIN_SAMPLES = 3
# A bad day must not push the first query minutes late, or a full second early.
OFFSET_MIN = -1.0
OFFSET_MAX = 5.0


def empty_stats() -> dict:
    return {
        "days": 0,
        "waves": {"total": 0, "fired": 0, "full": 0, "partial": 0, "missed": 0, "landed": 0, "hitRate": None},
        "targets": {"tried": 0, "booked": 0, "skipped": 0, "hitRate": None},
        "release": {"count": 0, "avgSec": None, "medianSec": None, "minSec": None, "maxSec": None},
        "offsetSec": None,
        "offsetScope": "",
        "offsetSamples": 0,
        "offsetMinSamples": OFFSET_MIN_SAMPLES,
        "courts": [],
        "anyCourts": [],
    }


def summarize_runs(runs: list[dict]) -> dict:
    """Aggregate stored waves. `offsetSec` is filled in by the store, which may widen the sample."""
    stats = empty_stats()
    waves = stats["waves"]
    targets = stats["targets"]
    released: list[float] = []
    courts: dict[str, dict] = {}

    for run in runs:
        waves["total"] += 1
        result = run.get("result") or ""
        if result == "booked":
            waves["full"] += 1
        elif result == "partial":
            waves["partial"] += 1
        else:
            waves["missed"] += 1
        if run.get("fired_sec") is not None:
            waves["fired"] += 1
            if result in {"booked", "partial"}:
                waves["landed"] += 1
        if run.get("released_sec") is not None:
            released.append(float(run["released_sec"]))
        _targets(run.get("targets") or [], targets, courts, stats["anyCourts"])

    waves["hitRate"] = _rate(waves["landed"], waves["fired"])
    targets["hitRate"] = _rate(targets["booked"], targets["tried"])
    if released:
        stats["release"] = {
            "count": len(released),
            "avgSec": _avg(released),
            "medianSec": _median(released),
            "minSec": round(min(released), 2),
            "maxSec": round(max(released), 2),
        }
    for bucket in courts.values():
        bucket["hitRate"] = _rate(bucket["booked"], bucket["tried"])
        bucket["avgSeenSec"] = _avg(bucket.pop("_seen"))
        bucket["avgBookedSec"] = _avg(bucket.pop("_booked"))
    stats["courts"] = sorted(
        courts.values(),
        key=lambda item: (
            -(item["hitRate"] if item["hitRate"] is not None else -1),
            item["avgSeenSec"] if item["avgSeenSec"] is not None else 1e9,
            -item["tried"],
            item["name"],
        ),
    )
    stats["anyCourts"].sort(key=lambda item: (-item["booked"], item["name"]))
    return stats


def offset_from_samples(samples: list[float]) -> float | None:
    """Median seconds after release to send the first query, or None when the sample is thin.

    Querying before the site opens is treated as "not released", and the next look waits a
    whole poll. The median lands the first query on a typical open; the clamp ignores a
    single very late or very early day.
    """
    if len(samples) < OFFSET_MIN_SAMPLES:
        return None
    return round(min(OFFSET_MAX, max(OFFSET_MIN, _median(samples))), 2)


def stats_lines(stats: dict) -> list[str]:
    """One short line for the CLI and Telegram. Empty when there is nothing to report."""
    if not stats or not stats["waves"]["total"]:
        return []
    bits = []
    targets = stats["targets"]
    if targets["tried"]:
        bits.append(f"目標命中 {targets['booked']}/{targets['tried']}（{_pct(targets['hitRate'])}）")
    release = stats["release"]
    if release["avgSec"] is not None:
        bits.append(f"平均 {release['avgSec']:.2f} 秒釋出")
    ranked = [court for court in stats["courts"] if court["tried"]]
    if ranked:
        best = ranked[0]
        bits.append(f"最好搶 {best['name']} {best['booked']}/{best['tried']}")
    offset = stats.get("offsetSec")
    if offset is not None and offset != 0:
        bits.append(f"第一次查詢偏移 {offset:+.2f} 秒")
    elif not bits:
        bits.append(f"{stats['waves']['total']} 波")
    return ["統計 " + "，".join(bits)]


def _targets(items: list[dict], totals: dict, courts: dict[str, dict], any_courts: list[dict]) -> None:
    any_hits: dict[str, int] = {}
    for item in items:
        status = item.get("status") or ""
        if status == "skipped":
            totals["skipped"] += 1
            continue
        court_id = str(item.get("courtId") or "").strip()
        if not court_id:
            # 「任一場」沒搶到時不知道是哪一面；搶到的記在 anyCourts，不灌進指定場的命中率。
            if status == "booked":
                name = str(item.get("bookedName") or "").strip()
                if name:
                    any_hits[name] = any_hits.get(name, 0) + 1
            continue
        totals["tried"] += 1
        booked = status == "booked"
        if booked:
            totals["booked"] += 1
        name = str(item.get("bookedName") or item.get("name") or court_id).strip()
        bucket = courts.get(court_id)
        if bucket is None:
            bucket = {"courtId": court_id, "name": name, "tried": 0, "booked": 0, "hitRate": None, "_seen": [], "_booked": []}
            courts[court_id] = bucket
        elif name and not bucket["name"]:
            bucket["name"] = name
        bucket["tried"] += 1
        if booked:
            bucket["booked"] += 1
        seen = item.get("seenSec")
        if seen is not None:
            bucket["_seen"].append(float(seen))
        booked_sec = item.get("bookedSec")
        if booked and booked_sec is not None:
            bucket["_booked"].append(float(booked_sec))
    for name, count in any_hits.items():
        found = next((row for row in any_courts if row["name"] == name), None)
        if found is None:
            any_courts.append({"name": name, "booked": count})
        else:
            found["booked"] += count


def _rate(num: int, den: int) -> float | None:
    if not den:
        return None
    return round(num / den, 4)


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def _pct(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"{round(rate * 100)}%"
