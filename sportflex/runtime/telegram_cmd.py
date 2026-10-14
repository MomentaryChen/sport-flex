"""Parse Telegram command arguments and format replies. No browser and no network."""

from __future__ import annotations

import re
from datetime import date, timedelta

from sportflex.core.calendar import calendar_lines
from sportflex.core.models import Slot, slot_matches
from sportflex.core.routine import weekday_token
from sportflex.core.rules import next_release
from sportflex.core.snipe_stats import stats_lines
from sportflex.core.venue import Venue

PERIODS = {
    "上午": ("06:00", "12:00"),
    "下午": ("12:00", "18:00"),
    "晚上": ("18:00", "23:00"),
    "全天": ("", ""),
}

PHASES = {
    "idle": "未設定",
    "armed": "等待開放",
    "preparing": "準備中",
    "ready": "待命",
    "firing": "開搶中",
    "done": "已結束",
    "need_login": "需要登入",
}

TARGET_STATUS = {
    "waiting": "等待",
    "booked": "搶到",
    "skipped": "略過",
    "miss": "沒搶到",
}

RUN_RESULTS = {"booked": "全搶到", "partial": "部分搶到", "missed": "沒搶到"}

_DATE_WORDS = {"今天": 0, "今日": 0, "明天": 1, "後天": 2}
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?")


def command_name(text: str) -> tuple[str, list[str]]:
    name, _, arg = text.partition(" ")
    return name.split("@", 1)[0].lower(), arg.split()


def pop_known(tokens: list[str], choices: list[str]) -> tuple[str | None, list[str]]:
    """Pull out one token that equals a known account, sport, or venue id."""
    folded = {item.casefold(): item for item in choices}
    found: list[str] = []
    rest: list[str] = []
    for token in tokens:
        hit = folded.get(token.casefold())
        if hit:
            found.append(hit)
        else:
            rest.append(token)
    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        raise ValueError("請只指定一個：" + "、".join(unique))
    return (unique[0] if unique else None), rest


def pop_venue(tokens: list[str], venues: list[dict]) -> tuple[str | None, list[str]]:
    exact, rest = pop_known(tokens, [item["id"] for item in venues])
    if exact:
        return exact, rest
    found: list[str] = []
    kept: list[str] = []
    for token in tokens:
        if len(token) < 2:
            kept.append(token)
            continue
        hits = [item["id"] for item in venues if token.casefold() in item["name"].casefold()]
        if not hits:
            kept.append(token)
            continue
        if len(hits) > 1:
            raise ValueError(f"「{token}」對到多個場館：" + "、".join(hits))
        found.append(hits[0])
    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        raise ValueError("請只指定一個場館：" + "、".join(unique))
    return (unique[0] if unique else None), kept


def resolve_date(token: str, today: date) -> str:
    if token in _DATE_WORDS:
        return (today + timedelta(days=_DATE_WORDS[token])).isoformat()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            year, month, day = (int(part) for part in token.split("-"))
            return date(year, month, day).isoformat()
        match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", token)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            found = date(today.year, month, day)
            if found < today:
                found = date(today.year + 1, month, day)
            return found.isoformat()
    except ValueError:
        raise ValueError(f"日期不存在：{token}") from None
    raise ValueError(f"日期看不懂：{token}")


def looks_like_date(token: str) -> bool:
    return token in _DATE_WORDS or bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", token) or re.fullmatch(r"\d{1,2}[/-]\d{1,2}", token)
    )


def parse_clock(token: str) -> str | None:
    if not re.fullmatch(r"\d{1,2}(?::\d{2})?", token):
        return None
    match = _CLOCK.fullmatch(token)
    assert match is not None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"時間不對：{token}")
    return f"{hour:02d}:{minute:02d}"


def parse_window(token: str) -> tuple[str, str] | None:
    """A period name, a single hour, or a start-end range. None when it is not a time at all."""
    if token in PERIODS:
        return PERIODS[token]
    if "-" in token:
        left, _, right = token.partition("-")
        start, end = parse_clock(left), parse_clock(right)
        if start is None or end is None:
            return None
        if end <= start:
            raise ValueError(f"結束時間要晚於開始：{token}")
        return start, end
    start = parse_clock(token)
    if start is None:
        return None
    hour, minute = (int(part) for part in start.split(":"))
    if hour >= 23:
        return start, "24:00"
    return start, f"{hour + 1:02d}:{minute:02d}"


def parse_slots_args(tokens: list[str], today: date) -> tuple[str, str, str]:
    query_date = ""
    window: tuple[str, str] | None = None
    for token in tokens:
        if looks_like_date(token):
            if query_date:
                raise ValueError("日期請只給一個")
            query_date = resolve_date(token, today)
            continue
        parsed = parse_window(token)
        if parsed is None:
            raise ValueError(f"看不懂「{token}」。例子：/slots 明天 晚上")
        if window is not None:
            raise ValueError("時段請只給一個，例如 晚上 或 19:00")
        window = parsed
    start, end = window or ("", "")
    return query_date, start, end


def parse_snipe_tokens(tokens: list[str]) -> list[dict]:
    """`19:00 20:00` is any court; `A 19:00 B 20:00` names the court in front of each time."""
    targets: list[dict] = []
    court = ""
    for token in tokens:
        when = parse_clock(token)
        if when:
            targets.append({"court": court, "time": when})
            court = ""
            continue
        if court:
            raise ValueError(f"「{court}」後面要接開始時間，例如 /snipe {court} 19:00")
        court = token
    if court:
        raise ValueError(f"「{court}」後面要接開始時間，例如 /snipe {court} 19:00")
    if not targets:
        raise ValueError("請給時間，例如 /snipe 19:00 或 /snipe A 19:00")
    return targets


def parse_weekly_args(tokens: list[str]) -> dict:
    """`/weekly 二四 20:00` or `/weekly 週二 週四 A 19:00`. `/weekly off` clears it."""
    if len(tokens) == 1 and tokens[0].casefold() in {"off", "stop", "停", "停止", "停用"}:
        return {"stop": True}
    weekdays: list[int] = []
    rest: list[str] = []
    for token in tokens:
        days = weekday_token(token)
        if days is not None:
            for day in days:
                if day not in weekdays:
                    weekdays.append(day)
            continue
        rest.append(token)
    if not weekdays:
        raise ValueError("請給星期，例如 /weekly 二四 20:00")
    return {"stop": False, "weekdays": weekdays, "targets": parse_snipe_tokens(rest)}


def parse_watch_args(tokens: list[str], today: date) -> dict:
    """`/watch off` stops. Otherwise a date, an optional period, and an optional court name."""
    if len(tokens) == 1 and tokens[0].casefold() in {"off", "stop", "停", "停止"}:
        return {"stop": True}
    query_date = ""
    window: tuple[str, str] | None = None
    court: list[str] = []
    for token in tokens:
        if looks_like_date(token):
            if query_date:
                raise ValueError("日期請只給一個")
            query_date = resolve_date(token, today)
            continue
        if window is None:
            parsed = parse_window(token)
            if parsed is not None:
                window = parsed
                continue
        court.append(token)
    if not query_date:
        raise ValueError("請給日期，例如 /watch 明天 晚上")
    start, end = window or ("", "")
    return {"stop": False, "date": query_date, "timeFrom": start, "timeTo": end, "court": " ".join(court)}


def parse_book_tokens(tokens: list[str], today: date) -> tuple[str, str, str]:
    query_date = ""
    when = ""
    courts: list[str] = []
    for token in tokens:
        if looks_like_date(token):
            if query_date:
                raise ValueError("日期請只給一個")
            query_date = resolve_date(token, today)
            continue
        clock = parse_clock(token)
        if clock and not when:
            when = clock
            continue
        courts.append(token)
    if not query_date or not when:
        raise ValueError("請給日期和時間，例如 /book 明天 19:00 A")
    return query_date, when, " ".join(courts)


def filter_courts(courts: list[dict], query: str) -> list[dict]:
    needle = query.strip().casefold()
    if not needle:
        return list(courts)
    exact = [court for court in courts if needle in {str(court["id"]).casefold(), str(court["name"]).casefold()}]
    if exact:
        return exact
    suffix = [court for court in courts if str(court["name"]).casefold().endswith("_" + needle)]
    if suffix:
        return suffix
    return [
        court
        for court in courts
        if needle in str(court["name"]).casefold() or needle in str(court["id"]).casefold()
    ]


def match_court(courts: list[dict], query: str) -> dict:
    found = filter_courts(courts, query)
    if len(found) == 1:
        return found[0]
    names = "、".join(court["name"] for court in (found or courts)) or "（無）"
    if not found:
        raise ValueError(f"找不到球場「{query}」。可選：{names}")
    raise ValueError(f"「{query}」對到多面：{names}")


def clip(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…（內容過長，已截斷）"


def format_release(venue: Venue) -> str:
    release = next_release(venue.rules)
    return (
        f"{venue.display_name}\n"
        f"下一波 {release['targetLabel']}\n"
        f"{release['opensLabel']} 開放\n"
        f"{venue.rules.describe()}"
    )


def format_courts(venue_name: str, category: str, courts: list[dict]) -> str:
    lines = [f"{venue_name} {category}  {len(courts)} 面"]
    if not courts:
        lines.append("沒有球場")
        return "\n".join(lines)
    for court in courts:
        price = f"${court['minPrice']}  " if court.get("minPrice") else ""
        lines.append(f"{court['name']}  {price}{court['id']}")
    return clip("\n".join(lines))


def format_slots(venue_name: str, board: dict, time_from: str, time_to: str) -> str:
    window = board.get("window") or {}
    query_date = window.get("date") or ""
    head = f"{venue_name} {board.get('category', '')}  {query_date}".rstrip()
    if window.get("start") and window.get("end"):
        head += f"（可查 {window['start']}～{window['end']}）"
    rows: list[str] = []
    for court in board.get("courts") or []:
        for raw in court.get("slots") or []:
            slot = Slot(
                time=raw.get("time") or "",
                bookable=bool(raw.get("bookable")),
                status=raw.get("status") or "",
                price=raw.get("price") or "",
            )
            if not slot.bookable or not slot_matches(slot, time_from or None, time_to or None):
                continue
            price = f" ${slot.price}" if slot.price else ""
            rows.append(f"{slot.time}  {court['name']}{price}")
    lines = [head]
    if not rows:
        lines.append("沒有可預約的時段")
    else:
        lines.extend(rows[:40])
        if len(rows) > 40:
            lines.append(f"…還有 {len(rows) - 40} 筆，加上時段再查，例如 /slots {query_date} 晚上")
    return clip("\n".join(lines))


def format_snipe(venue_name: str, state: dict) -> str:
    release = state.get("release") or {}
    targets = state.get("targets") or []
    when = f"{release.get('targetLabel', '')}（{release.get('opensLabel', '')} 開放）".strip("（）")
    if not targets:
        return f"{venue_name} 目前沒有搶訂\n下一波 {when}\n設定：/snipe 19:00 或 /snipe A 19:00"
    phase = PHASES.get(state.get("phase") or "", state.get("phase") or "")
    lines = [f"{venue_name}（{state.get('account', '')}）{phase}  {when}"]
    for item in targets:
        label = TARGET_STATUS.get(item.get("status") or "", "")
        extra = f"  {label}" if label and item.get("status") not in {"", "waiting"} else ""
        lines.append(f"・{item.get('name') or '任一場'} {item.get('time')}{extra}")
    if state.get("running"):
        lines.append("看到空檔會送出訂單，付款要自己完成。取消：/cancel")
    if state.get("source") == "routine" and state.get("running"):
        lines.append("這一波是每週固定自動排上的。")
    return "\n".join(lines)


def format_routine(venue_name: str, state: dict) -> str:
    if not state.get("enabled"):
        return (
            f"{venue_name} 還沒有每週固定時段\n"
            "設定：/weekly 二四 20:00\n"
            "指定場地：/weekly 二四 A 19:00\n"
            "停用：/weekly off"
        )
    lines = [f"{venue_name} {state.get('label') or '每週固定'}"]
    release = state.get("release") or {}
    if state.get("skipDate") and state.get("skipDate") == release.get("targetDate"):
        lines.append(f"這一波 {release.get('targetLabel', '')} 已取消，之後符合的日子仍會自動排。")
    elif state.get("due"):
        lines.append(
            f"下一波 {release.get('targetLabel', '')} 會在 {release.get('opensLabel', '')} 前自動排好。"
        )
    else:
        lines.append(f"下一波 {release.get('targetLabel', '')} 不是選定的星期，到了會再排。")
    lines.append("停用：/weekly off")
    return "\n".join(lines)


def format_watch(venue_name: str, state: dict) -> str:
    spec = state.get("spec") or {}
    if not state.get("running"):
        return (
            f"{venue_name} 目前沒有盯場\n"
            "設定：/watch 明天 晚上\n"
            "指定場地：/watch 明天 19:00 A\n"
            "停止：/watch off"
        )
    targets = spec.get("targets") or []
    if targets:
        listed = "、".join(f"{item.get('name') or '任一場'} {item.get('time')}" for item in targets)
        bits = [spec.get("category") or "", spec.get("date") or "", listed]
    else:
        window = "–".join(part for part in (spec.get("timeFrom") or "", spec.get("timeTo") or "") if part)
        court = spec.get("courtName") or ("指定場" if spec.get("courtId") else "任一場")
        bits = [spec.get("category") or "", spec.get("date") or "", window or "全天", court]
    lines = [f"盯場 {venue_name} " + " ".join(bit for bit in bits if bit)]
    lines.append("看到空檔會送出訂單，付款要自己完成。停止：/watch off")
    log = state.get("log") or []
    if log:
        lines.append(log[-1])
    return "\n".join(lines)


def format_runs(runs: list[dict], stats: dict | None = None) -> str:
    if not runs:
        return "最近沒有開搶紀錄"
    lines = ["最近開搶", *stats_lines(stats or {})]
    for run in runs[:8]:
        result = RUN_RESULTS.get(run.get("result") or "", run.get("result") or "")
        lines.append(
            f"{run.get('target_date', '')}  {run.get('account', '')}  {result} "
            f"{run.get('booked', 0)}/{run.get('total', 0)}  {run.get('reason', '')}".rstrip()
        )
    return "\n".join(lines)


def format_grab(result: dict) -> str:
    order = result.get("order") or {}
    price = f"  ${result['price']}" if result.get("price") else ""
    lines = [
        f"已送出 {result.get('venueName', '')} {result.get('court', '')}".strip(),
        f"{result.get('date', '')}  {result.get('time', '')}{price}".strip(),
    ]
    if order:
        lines.append(f"訂單已成立：{order['no']}" if order.get("no") else "訂單已成立")
        if order.get("timeoutSec"):
            lines.append(f"請在 {int(order['timeoutSec']) // 60} 分鐘內付款，逾時會被取消")
        lines.append(order.get("paymentUrl") or "請到會員中心 → 待付款")
        if order.get("carrier"):
            lines.append(f"發票請選手機載具 {order['carrier']}")
        lines.extend(calendar_lines(result))
        if order.get("warning"):
            lines.append(f"注意：{order['warning']}")
    else:
        lines.append("停在訂單確認頁，還沒送出訂單。")
        if result.get("url"):
            lines.append(result["url"])
    return "\n".join(line for line in lines if line)
