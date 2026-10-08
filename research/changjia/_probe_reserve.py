import json
import re
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


def post(path: str, fields: dict) -> tuple[str, str]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": BASE + "/Location/LocationSubList/LocationSub/",
        },
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.geturl(), resp.read().decode("utf-8", "replace")


def main() -> None:
    fields = {
        "LID": "XDSC",
        "LIDName": "新北市新店國民運動中心",
        "CategoryID": "Badminton",
        "CategoryName": "羽球",
        "LSID": "7F2BBadminton01",
        "LSIDName": "羽球場_A",
        "CategoryArrayStr": CATEGORIES,
        "imageUrls": "",
        "minPrice": "290",
        "description": "",
        "charge": "",
        "CategoryImgURL": "",
        "address": "",
        "phone": "",
        "businessHours": "",
        "imageUrl": "",
        "startDate": "",
        "endDate": "",
        "startTime": "",
        "endTime": "",
        "redirectFromIndex": "true",
        "redirectFromSearch": "false",
        "redirectFromFilter": "false",
        "date": "",
        "timeSlot": "",
        "price": "",
        "payImmediatelyAfterBooking": "",
        "reserveMode": "",
    }
    final, html = post("/Location/LocationSubList/LocationSub/Reserve/", fields)
    open("reserve.html", "w", encoding="utf-8").write(html)
    urls = sorted(set(re.findall(r"""['\"]([^'\"]*(?:ajax|Reserve|time|slot|calendar)[^'\"]*)['\"]""", html, re.I)))
    funcs = sorted(set(re.findall(r"function\s+([A-Za-z0-9_]+)", html)))
    open("reserve_meta.txt", "w", encoding="utf-8").write(
        f"url={final}\nlen={len(html)}\n\nURLS\n" + "\n".join(urls) + "\n\nFUNCS\n" + "\n".join(funcs)
    )


if __name__ == "__main__":
    main()
