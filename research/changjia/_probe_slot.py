import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = "https://changjia.sporetrofit.com"
CATEGORIES = [
    {"LID": "", "ItemID": "Badminton", "Name": "羽球", "ListOrder": "0"},
    {"LID": "", "ItemID": "Basketball", "Name": "籃球", "ListOrder": "1"},
    {"LID": "", "ItemID": "Billiard", "Name": "撞球", "ListOrder": "2"},
    {"LID": "", "ItemID": "Squash", "Name": "壁球", "ListOrder": "3"},
    {"LID": "", "ItemID": "TableTennis", "Name": "桌球", "ListOrder": "4"},
    {"LID": "", "ItemID": "Volleyball", "Name": "排球", "ListOrder": "5"},
]


def post(path: str, fields: dict) -> str:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"User-Agent": "Mozilla/5.0", "Referer": BASE + "/Location/LocationSubList/"},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        final = resp.geturl()
        html = resp.read().decode("utf-8", "replace")
        return final, html


def main() -> None:
    fields = {
        "CategoryArrayStr": json.dumps(CATEGORIES, ensure_ascii=False),
        "LID": "XDSC",
        "LIDName": "新北市新店國民運動中心",
        "CategoryID": "Badminton",
        "CategoryName": "羽球",
        "LSID": "7F2BBadminton01",
        "LSIDName": "羽球場_A",
        "LSIDEnName": "羽球場_A",
        "minDuration": "1",
        "minPrice": "290",
        "imageUrls": "",
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
    }
    final, html = post("/Location/LocationSubList/LocationSub/", fields)
    open("locationsub.html", "w", encoding="utf-8").write(html)
    open("locationsub_meta.txt", "w", encoding="utf-8").write(f"url={final}\nlen={len(html)}\n")

    urls = sorted(set(re.findall(r"""(?:url|action|href)\s*[:=]\s*['\"]([^'\"]+)['\"]""", html)))
    ajax = [u for u in urls if "ajax" in u.lower() or "api" in u.lower() or "time" in u.lower() or "slot" in u.lower() or "calendar" in u.lower()]
    open("locationsub_urls.txt", "w", encoding="utf-8").write("\n".join(urls))
    open("locationsub_ajax.txt", "w", encoding="utf-8").write("\n".join(ajax) or "(none)")

    # pull function names that look like booking/time
    funcs = sorted(set(re.findall(r"function\s+(\w+)", html)))
    open("locationsub_funcs.txt", "w", encoding="utf-8").write("\n".join(funcs))


if __name__ == "__main__":
    main()
