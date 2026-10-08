import json
import urllib.parse
import urllib.request

BASE = "https://changjia.sporetrofit.com"


def post(path: str, fields: dict, opener) -> str:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": BASE + "/Location/LocationSubList/LocationSub/Reserve/",
            "Origin": BASE,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with opener.open(req, timeout=40) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> None:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.open(urllib.request.Request(BASE + "/", headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
    raw = post(
        "/api/getRequestData.php",
        {
            "serviceName": "getResLocationAvailableData",
            "LID": "XDSC",
            "LSID": "7F2BBadminton01",
            "QueryDate": "",
        },
        opener,
    )
    open("avail.json", "w", encoding="utf-8").write(raw)
    data = json.loads(raw)
    result = data.get("ResultData") or {}
    summary = {
        "keys": list(data.keys()),
        "result_keys": list(result.keys()) if isinstance(result, dict) else type(result).__name__,
        "start": result.get("ReservingStart") if isinstance(result, dict) else None,
        "end": result.get("ReservingEnd") if isinstance(result, dict) else None,
        "available_type": type(result.get("AvailableData")).__name__ if isinstance(result, dict) else None,
    }
    avail = result.get("AvailableData") if isinstance(result, dict) else None
    if isinstance(avail, dict):
        summary["available_count"] = len(avail)
        first_key = next(iter(avail))
        sample = avail[first_key]
        summary["first_key"] = first_key
        summary["sample_type"] = type(sample).__name__
        if isinstance(sample, dict):
            summary["sample_keys"] = list(sample.keys())[:20]
            summary["sample"] = {k: sample[k] for k in list(sample)[:8]}
        elif isinstance(sample, list):
            summary["sample_len"] = len(sample)
            summary["sample0"] = sample[0] if sample else None
        else:
            summary["sample"] = str(sample)[:400]
    elif isinstance(avail, list):
        summary["available_count"] = len(avail)
        summary["sample0"] = avail[0] if avail else None
    open("avail_summary.json", "w", encoding="utf-8").write(json.dumps(summary, ensure_ascii=False, indent=2))

    js = urllib.request.urlopen(BASE + "/js/generateDataArrayToolBox.js", timeout=30).read().decode("utf-8", "replace")
    open("generateDataArrayToolBox.js", "w", encoding="utf-8").write(js)


if __name__ == "__main__":
    main()
