"""Finish the logged-in survey slowly enough to avoid the rate limit."""

from __future__ import annotations

import time

from sportflex.client import browser, fetch_availability, iter_dates, list_courts, open_location_list

PAUSE = 2.5


def main() -> None:
    lines: list[str] = []

    def log(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    with browser(headless=True) as page:
        open_location_list(page, None)
        categories = page.locator("#CategoryBtnDiv span").evaluate_all(
            "els => els.map(el => ({buttonId: el.id, label: el.textContent.trim()}))"
        )
        time.sleep(PAUSE)
        for item in categories:
            if item["label"] == "羽球":
                continue
            with page.expect_response(lambda response: "ajax/createTable/" in response.url, timeout=30_000) as info:
                page.locator(f"#{item['buttonId']}").click()
            body = info.value.text().replace("\n", " ")
            courts = list_courts(page)
            log(f"\n## {item['label']}  {len(courts)} 面")
            if not courts:
                log(body[:240])
            for court in courts:
                log(f"- {court.name} | {court.venue} | 最低 {court.min_hours} 小時 | ${court.price}/時 | {court.lid}/{court.lsid}")
            time.sleep(PAUSE)

        page.locator("#BADMINTON_btn").click()
        page.wait_for_timeout(1500)
        courts = [court for court in list_courts(page) if court.name != "羽球場_B"]
        for court in courts:
            open_rows = _open_slots(page, court, log)
            log(f"\n羽球 {court.name} ({court.venue})  空檔 {len(open_rows)}")
            for query_date, when, price in open_rows:
                log(f"  {query_date}  {when}  ${price}")

    from pathlib import Path

    previous = Path("output/analysis.txt").read_text(encoding="utf-8") if Path("output/analysis.txt").exists() else ""
    Path("output/analysis.txt").write_text(previous + "\n\n# 續查\n" + "\n".join(lines), encoding="utf-8")
    print("REST_DONE", flush=True)


def _open_slots(page, court, log) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    first = _fetch(page, court, "", log)
    if first is None:
        return rows
    cached = {first.query_date: first}
    for query_date in iter_dates(first.reserving_start, first.reserving_end):
        availability = cached.get(query_date) or _fetch(page, court, query_date, log)
        if availability is None:
            continue
        for slot in availability.slots:
            if slot.bookable:
                rows.append((availability.query_date or query_date, slot.time, slot.price))
        time.sleep(PAUSE)
    return rows


def _fetch(page, court, query_date, log):
    for attempt in range(4):
        try:
            return fetch_availability(page, court, query_date)
        except Exception as exc:
            log(f"  retry {court.name} {query_date or 'today'}：{exc}")
            time.sleep(8 * (attempt + 1))
    return None


if __name__ == "__main__":
    main()
