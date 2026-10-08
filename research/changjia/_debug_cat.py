from sportflex.client import browser

with browser(headless=True) as page:
    with page.expect_response(lambda response: "ajax/createTable/" in response.url, timeout=30_000) as first:
        page.goto("https://changjia.sporetrofit.com/", wait_until="domcontentloaded")
        page.locator("#locationList").click()
    print("first", first.value.status, "forms", page.locator("#tableContainer form").count())
    print("buttons", page.locator("#CategoryBtnDiv").inner_text())
    print("active", page.locator("#CategoryBtnDiv span.active").inner_text())
    with page.expect_response(lambda response: "ajax/createTable/" in response.url, timeout=30_000) as second:
        page.locator("#BASKETBALL_btn").click()
    body = second.value.text()
    post_data = second.value.request.post_data or ""
    print("post", post_data[:500])
    print("body", body[:500].replace("\n", " | "))
