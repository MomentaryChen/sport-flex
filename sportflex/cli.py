from __future__ import annotations

import argparse
import sys

from sportflex.core.accounts import load_accounts, pick_account
from sportflex.core.engine import grab
from sportflex.core.models import iter_dates, slot_matches
from sportflex.core.venue import get_venue, load_venues
from sportflex.providers import create_provider, provider_class
from sportflex.runtime.browser import open_page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sportflex",
        description="多場館訂場：查球場、查可預約時段、送出預約並停在付款前。",
        epilog=(
            "例子：\n"
            "  python -m sportflex venues\n"
            "  python -m sportflex courts --venue xindian --category 羽球\n"
            "  python -m sportflex slots --venue xindian --date 2026-10-09 --from 18:00\n"
            "  python -m sportflex slots --name 羽球場_A --scan\n"
            "  python -m sportflex login --account default\n"
            "  python -m sportflex book --name 羽球場_A --date 2026-10-10 --time 20:00\n"
            "  python -m sportflex ui\n"
            "\n"
            "--venue 省略時用 venues/ 的第一個場館；--account 省略時用該平台的第一個帳號。\n"
            "book 會送出預約，但不會代按付款。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--show", action="store_true", help="顯示瀏覽器視窗")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("venues", help="列出已設定的場館與帳號")

    courts = sub.add_parser("courts", help="列出場館的球場")
    _add_filters(courts)

    slots = sub.add_parser("slots", help="查可預約時段")
    _add_filters(slots)
    slots.add_argument("--date", help="日期 YYYY-MM-DD，預設為今天")
    slots.add_argument("--from", dest="time_from", help="只看這個開始時間之後，例如 18:00")
    slots.add_argument("--to", dest="time_to", help="不包含這個開始時間，例如 22:00")
    slots.add_argument("--scan", action="store_true", help="掃描整個可預約日期區間")
    slots.add_argument("--all", action="store_true", help="連已被預約的時段一起列出")

    login = sub.add_parser("login", help="打開登入頁，登入狀態會留在該帳號的瀏覽器 profile")
    login.add_argument("--venue")
    login.add_argument("--account")

    ui = sub.add_parser("ui", help="打開搶場網頁")
    ui.add_argument("--port", type=int, default=8765)

    book = sub.add_parser("book", help="送出一個時段的預約，停在付款前")
    _add_filters(book)
    book.add_argument("--date", required=True, help="YYYY-MM-DD")
    book.add_argument("--time", required=True, help="開始時間或完整時段，例如 20:00 或 20:00 - 21:00")

    args = parser.parse_args(argv)
    try:
        if args.command == "venues":
            return _venues()
        if args.command == "courts":
            return _courts(args)
        if args.command == "slots":
            return _slots(args)
        if args.command == "login":
            return _login(args)
        if args.command == "ui":
            from sportflex.web.app import serve

            serve(port=args.port)
            return 0
        if args.command == "book":
            return _book(args)
    except Exception as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    return 0


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--venue", help="場館 id，見 venues 指令")
    parser.add_argument("--account", help="帳號 id，見 config/accounts.yaml")
    parser.add_argument("--category", help="運動種類，預設為場館設定的第一個")
    parser.add_argument("--name", help="球場名稱關鍵字或 id，例如 羽球場_A")


def _target(args):
    venue = get_venue(args.venue)
    account = pick_account(venue.provider, args.account)
    category = getattr(args, "category", None) or venue.categories[0]
    return venue, account, category


def _filtered(courts, name: str | None):
    if not name:
        return courts
    needle = name.casefold()
    return [court for court in courts if needle in court.name.casefold() or needle == court.id.casefold()]


def _venues() -> int:
    for venue in load_venues().values():
        print(f"{venue.id}\t{venue.name}\t平台 {venue.provider}\t{'、'.join(venue.categories)}")
        print(f"\t{venue.rules.describe()}")
    print()
    for account in load_accounts().values():
        info = account.public()
        print(f"帳號 {account.id}\t{account.display}\t平台 {account.provider}\t{info['username'] or '（.env 未設定）'}")
    return 0


def _courts(args) -> int:
    venue, account, category = _target(args)
    with open_page(account, headless=not args.show) as page:
        courts = _filtered(create_provider(venue.provider, page).list_courts(venue, category), args.name)
    print(f"{venue.name} {category}  {len(courts)} 筆")
    for court in courts:
        print(f"{court.name}\t{court.venue}\t最低 {court.min_hours} 小時\t${court.price}/時\t{court.id}")
    return 0


def _slots(args) -> int:
    venue, account, category = _target(args)
    with open_page(account, headless=not args.show) as page:
        provider = create_provider(venue.provider, page)
        courts = _filtered(provider.list_courts(venue, category), args.name)
        if not courts:
            print("沒有符合的球場")
            return 0
        print(f"{venue.name} {category}  可查 {len(courts)} 面球場")
        for court in courts:
            first = provider.availability(court, args.date or "")
            dates = [args.date] if args.date else [first.query_date]
            if args.scan:
                dates = list(iter_dates(first.window_start, first.window_end))
            print(f"\n{court.name}  {court.venue}  可預約 {first.window_start} ~ {first.window_end}")
            cached = {first.query_date: first}
            for query_date in dates:
                availability = cached.get(query_date) or provider.availability(court, query_date)
                shown = [
                    slot
                    for slot in availability.slots
                    if slot_matches(slot, args.time_from, args.time_to) and (args.all or slot.bookable)
                ]
                if not shown:
                    if args.scan or args.date:
                        print(f"  {query_date}  無符合時段")
                    continue
                print(f"  {query_date}")
                for slot in shown:
                    price = f"${slot.price}" if slot.price else ""
                    print(f"    {slot.time}\t{slot.status}\t{price}")
    return 0


def _login(args) -> int:
    venue = get_venue(args.venue)
    account = pick_account(venue.provider, args.account)
    with open_page(account, headless=False) as page:
        page.goto(provider_class(venue.provider).login_url, wait_until="domcontentloaded")
        print(f"請在瀏覽器以帳號「{account.display}」完成登入，登入狀態會存在 {account.profile_dir}。完成後回到這裡按 Enter。")
        input()
    return 0


def _book(args) -> int:
    venue, account, category = _target(args)
    with open_page(account, headless=False) as page:
        provider = create_provider(venue.provider, page)
        courts = _filtered(provider.list_courts(venue, category), args.name)
        if len(courts) != 1:
            names = "、".join(court.name for court in courts) or "（無）"
            raise RuntimeError(f"球場必須剛好對到一面，目前是：{names}")
        court = courts[0]
        availability = provider.availability(court, args.date)
        slot = next(
            (item for item in availability.slots if item.time == args.time or item.time.startswith(args.time)),
            None,
        )
        if slot is None:
            raise RuntimeError("找不到時段 " + args.time + "。可選：" + "、".join(item.time for item in availability.slots))
        if not slot.bookable:
            raise RuntimeError(f"{slot.time} 目前不可預約（{slot.status}）")
        result = grab(provider, venue, court, args.date, slot)
        print(f"已送出 {venue.name} {court.name}  {args.date}  {slot.time}  ${slot.price}")
        if result["pressed"]:
            print(f"已按「{result['pressed']}」")
        print(f"目前頁面：{result['url']}")
        print("不會代按付款。請在瀏覽器核對並自行付款，完成後按 Enter 關閉。")
        input()
    return 0
