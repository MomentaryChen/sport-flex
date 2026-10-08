import json
import urllib.parse
import urllib.request

CATEGORIES = [
    {"LID": "", "ItemID": "Badminton", "Name": "羽球", "ListOrder": "0"},
    {"LID": "", "ItemID": "Basketball", "Name": "籃球", "ListOrder": "1"},
    {"LID": "", "ItemID": "Billiard", "Name": "撞球", "ListOrder": "2"},
    {"LID": "", "ItemID": "Squash", "Name": "壁球", "ListOrder": "3"},
    {"LID": "", "ItemID": "TableTennis", "Name": "桌球", "ListOrder": "4"},
    {"LID": "", "ItemID": "Volleyball", "Name": "排球", "ListOrder": "5"},
]

BASE = "https://changjia.sporetrofit.com"


def post(path: str, fields: dict) -> str:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=body)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> None:
    html = post(
        "/Location/LocationSubList/",
        {
            "CategoryID": "Badminton",
            "CategoryArrayStr": json.dumps(CATEGORIES, ensure_ascii=False),
            "redirectFromIndex": "true",
        },
    )
    open("sublist.html", "w", encoding="utf-8").write(html)
    print("page len", len(html))
    idx = html.find("CategoryBtnDiv")
    open("buttons_snip.txt", "w", encoding="utf-8").write(html[idx : idx + 8000])

    table = post(
        "/Location/LocationSubList/ajax/createTable/",
        {
            "LID": "",
            "LIDName": "",
            "CategoryID": "Badminton",
            "CategoryName": "羽球",
            "CategoryArrayStr": json.dumps(CATEGORIES, ensure_ascii=False),
            "CategoryImgURL": "",
            "address": "",
            "phone": "",
            "businessHours": "",
            "imageUrl": "",
            "redirectFromIndex": "true",
            "redirectFromSearch": "false",
            "redirectFromFilter": "false",
            "startDate": "",
            "endDate": "",
            "startTime": "",
            "endTime": "",
        },
    )
    open("table.html", "w", encoding="utf-8").write(table)
    print("table len", len(table))


if __name__ == "__main__":
    main()
