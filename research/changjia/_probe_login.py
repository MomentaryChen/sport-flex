import urllib.request

url = "https://changjia.sporetrofit.com/login/"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    html = resp.read().decode("utf-8", "replace")
open("login.html", "w", encoding="utf-8").write(html)
print("len", len(html), "url", resp.geturl())
