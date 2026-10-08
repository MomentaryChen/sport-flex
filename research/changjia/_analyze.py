"""Log in with .env credentials, then summarize courts and open slots."""

from __future__ import annotations

import time
from pathlib import Path

from sportflex.client import (
    BASE,
    browser,
    fetch_availability,
    iter_dates,
    list_courts,
    open_location_list,
)

OUT = Path("output")
CAPTCHA = OUT / "captcha.png"
ANSWER = OUT / "captcha.txt"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def wait_answer(timeout: float = 120) -> str:
    if ANSWER.exists():
        ANSWER.unlink()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ANSWER.exists():
            code = ANSWER.read_text(encoding="utf-8").strip()
            if code:
                return code
        time.sleep(0.4)
    raise TimeoutError("等待驗證碼逾時")


def login(page, account: str, password: str) -> None:
    page.goto(BASE + "/", wait_until="domcontentloaded")
    banner = ""
    if page.locator("div[onclick='reservationAction()']").count():
        banner = page.locator("div[onclick='reservationAction()']").inner_text()
    if "點我登入" not in banner and page.locator("#locationList").count():
        print("LOGIN_SKIP already signed in", flush=True)
        return
    page.goto(BASE + "/login/", wait_until="domcontentloaded")
    if page.locator("#inputEmailAccount").count() == 0:
        print("LOGIN_SKIP", page.url)
        return
    page.locator("#inputEmailAccount").fill(account)
    page.locator("#inputPassword").fill(password)
    for attempt in range(3):
        page.locator("#captchaContainer img").screenshot(path=str(CAPTCHA))
        print(f"CAPTCHA_READY attempt={attempt + 1}", flush=True)
        code = wait_answer()
        page.locator("#verification_code").fill(code)
        with page.expect_response(lambda response: "ajax/login.php" in response.url, timeout=30_000) as info:
            page.locator("input.btn-login").click()
        payload = info.value.json()
        print("LOGIN_RESULT", payload.get("success"), payload.get("msg", ""), flush=True)
        if payload.get("success"):
            confirm = page.locator(".swal2-confirm")
            if confirm.count():
                confirm.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1500)
            return
        page.locator("#verification_code").fill("")
        reload = page.locator("#captchaContainer button")
        if reload.count():
            reload.click()
            page.wait_for_timeout(600)
    raise RuntimeError("登入失敗")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    env = load_env()
    lines: list[str] = []

    def log(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    with browser(headless=True) as page:
        login(page, env["account"], env["password"])
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        banner = page.locator("div[onclick='reservationAction()']").inner_text().strip()
        log(f"首頁預約提示：{banner}")
        log(f"登入後網址：{page.url}")

        from sportflex.client import show_category

        label = open_location_list(page, None)
        categories = page.locator("#CategoryBtnDiv span").evaluate_all(
            "els => els.map(el => ({buttonId: el.id, label: el.textContent.trim()}))"
        )
        catalog: list[tuple[str, list]] = []
        for item in categories:
            if item["label"] != label:
                problem = show_category(page, item["buttonId"])
                if problem:
                    log(f"\n## {item['label']}  {problem}")
                    catalog.append((item["label"], []))
                    continue
            courts = list_courts(page)
            catalog.append((item["label"], courts))
            log(f"\n## {item['label']}  {len(courts)} 面")
            for court in courts:
                log(f"- {court.name} | {court.venue} | 最低 {court.min_hours} 小時 | ${court.price}/時 | {court.lid}/{court.lsid}")

        log("\n## 可預約時段")
        for category, courts in catalog:
            for court in courts:
                try:
                    first = fetch_availability(page, court, "")
                except Exception as exc:
                    log(f"\n{category} {court.name} 查詢失敗：{exc}")
                    continue
                window = f"{first.reserving_start} ~ {first.reserving_end}"
                open_rows = []
                by_date = {first.query_date: first}
                for query_date in iter_dates(first.reserving_start, first.reserving_end):
                    availability = by_date.get(query_date)
                    if availability is None:
                        try:
                            availability = fetch_availability(page, court, query_date)
                        except Exception as exc:
                            log(f"  {query_date} 查詢失敗：{exc}")
                            continue
                    for slot in availability.slots:
                        if slot.bookable:
                            open_rows.append((availability.query_date or query_date, slot.time, slot.price))
                log(f"\n{category} {court.name} ({court.venue})  區間 {window}  空檔 {len(open_rows)}")
                for query_date, when, price in open_rows:
                    log(f"  {query_date}  {when}  ${price}")

    Path("output/analysis.txt").write_text("\n".join(lines), encoding="utf-8")
    print("ANALYSIS_DONE", flush=True)


if __name__ == "__main__":
    main()
