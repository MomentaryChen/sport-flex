import json
import urllib.parse
import urllib.request

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
body = urllib.parse.urlencode(
    {
        "LID": "",
        "LIDName": "",
        "CategoryID": "Basketball",
        "CategoryName": "籃球",
        "CategoryArrayStr": CATEGORIES,
        "redirectFromIndex": "true",
        "redirectFromSearch": "false",
        "redirectFromFilter": "false",
        "startDate": "",
        "endDate": "",
        "startTime": "",
        "endTime": "",
    }
).encode()
req = urllib.request.Request(
    "https://changjia.sporetrofit.com/Location/LocationSubList/ajax/createTable/",
    data=body,
)
with urllib.request.urlopen(req, timeout=40) as resp:
    html = resp.read().decode("utf-8", "replace")
open("bb.html", "w", encoding="utf-8").write(html)
print("len", len(html), "forms", html.count("<form"), "筆", "筆" in html)
