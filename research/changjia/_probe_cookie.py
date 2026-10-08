import json
import urllib.parse
import urllib.request

BASE = "https://changjia.sporetrofit.com"
CATEGORIES = json.dumps(
    [
        {"LID": "", "ItemID": "Badminton", "Name": "羽球", "ListOrder": "0"},
        {"LID": "", "ItemID": "Basketball", "Name": "籃球", "ListOrder": "1"},
        {"LID": "", "ItemID": "Billiard", "Name": "撞球", "ListOrder": "2"},
        {"LID": "", "ItemID": "Squash", "Name": "壁球", "ListOrder": "3"},
        {"LID": "", "ItemID": "TableTennis", "Name": "桌球", "ListOrder": "4"},
        {"LID": "", "ItemID": "Volleyball", "Name": "排球", "ListOrder": "5"},
    ],
    ensure_ascii=False,
)
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
opener.open(BASE + "/", timeout=30).read()


def table(category_id, name):
    body = urllib.parse.urlencode(
        {
            "LID": "",
            "LIDName": "",
            "CategoryID": category_id,
            "CategoryName": name,
            "CategoryArrayStr": CATEGORIES,
            "redirectFromIndex": "true",
            "redirectFromSearch": "false",
            "redirectFromFilter": "false",
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/Location/LocationSubList/ajax/createTable/",
        data=body,
        headers={"Referer": BASE + "/Location/LocationSubList/", "X-Requested-With": "XMLHttpRequest"},
    )
    html = opener.open(req, timeout=30).read().decode("utf-8", "replace")
    print(category_id, "forms", html.count("<form"), "login", "請登入" in html, "len", len(html))


table("Badminton", "羽球")
table("Basketball", "籃球")
table("TableTennis", "桌球")
