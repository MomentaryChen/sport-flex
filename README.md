# Sport Flex

好時段往往在開放的那一秒就沒了。Sport Flex 把查表、等到開放、看到空檔就送出，收成同一個畫面。訂單由它送出，付款仍由你自己完成。

## 查空檔，順便把下一波排好

![查表、設定搶訂目標](docs/screenshots/board.png)

選運動、日期和上午／下午／晚上。綠色格子是還能搶的空檔，點一下就送出訂單。左邊把還沒開放的場次排好：指定場地，或選「任一場」讓它在開放瞬間挑第一個有空的。

## 開放的當下

![開搶進行中](docs/screenshots/snipe.png)

倒數結束就開始查。開搶前後會先暫停手動查表，避免把預約頁帶走。每一筆目標停在「等待中」，搶到會通知你去付款。

## 每一次花了幾秒

![開搶歷史](docs/screenshots/history.png)

每一波都留著：幾秒看到空檔、幾秒送到確認頁、有沒有搶滿。沒搶到的，可以改去盯已經開放的時段，等人退訂。

## 開始用

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python -m sportflex ui
```

帳號放在專案根目錄的 `.env`，不要寫進設定檔：

```bash
SPORT_FLEX_DEFAULT_ACCOUNT=
SPORT_FLEX_DEFAULT_PASSWORD=
```

瀏覽器開起來之後，先登入一次。之後這個帳號的登入狀態會留在自己的瀏覽器設定裡。

## 畫面上不顯示館方正式名稱

下拉選單、確認視窗、Telegram 和指令輸出，都只用你取的簡稱。正式名稱留在 `venues/*.yaml` 的 `name`，不會被印出來。

```yaml
id: main
label: 一號館          # 畫面上看到的名字
name: 正式名稱只放這裡
provider: changjia
categories: [羽球]
```

簡稱可以隨時改，例如「一號館」、「平日館」。多館時靠簡稱分辨，不必把館名攤在畫面上。

## 其他

- 盯已經開放、目前額滿的時段，一有空檔就送出訂單。
- 同一個平台可以掛多個帳號，各自的瀏覽器同時搶。
- 有設定 Telegram 時，驗證碼和搶到的結果會傳過去。
- `python -m sportflex --help` 可以用指令查場、看時段、送出預約。
