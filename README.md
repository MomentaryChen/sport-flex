# Sport Flex

[中文](README.md) · [English](README.en.md)

> **免責附註。** 此工具不負任何責任。軟體依現況提供，沒有任何保證。使用風險由你自行承擔。詳見 [免責聲明](#免責聲明)。

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
.venv/bin/playwright install chrome
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

- 每週固定時段：例如每週二、四 20:00 任一場。套用一次之後，每天 06:00 釋出前會自動排上第 14 天，不必再每天手動設。Telegram：`/weekly 二四 20:00`。14 天內已開放、符合星期的日子會列成候選，點選要盯的日期，有人退訂就送出訂單。Telegram 加上「補」全選：`/weekly 二四 20:00 補`，或只挑某幾天：`/weekly 二四 20:00 補 10/20 10/22`。
- 已付款的場次，會在不退費期限的前一天上午提醒要不要取消。期限從場館設定的取消規則讀，例如「前 2 天內不退費」。取消仍要自己到櫃台辦。
- 盯已經開放、目前額滿的時段，一有空檔就送出訂單。
- 同一個平台可以掛多個帳號，各自的瀏覽器同時搶。
- `.env` 的 `SPORT_FLEX_ENV` 決定環境：`development`（預設）或 `production`。只有 `production` 會送出訂單、使用 Telegram，並佔住「一台機器只跑一份」的鎖；`development` 只查不訂，可以同時開多份。
- 有設定 Telegram 時（限 `production`），驗證碼和搶到的結果會傳過去。在 `.env` 加上 `SPORT_FLEX_TELEGRAM_BOT_TOKEN` 與 `SPORT_FLEX_TELEGRAM_CHAT_ID`。
- Telegram 的備援：`SPORT_FLEX_DISCORD_WEBHOOK_URL`（Discord webhook），以及 `SPORT_FLEX_NTFY_URL`（ntfy 主題網址，例如 `https://ntfy.sh/你的主題`；私人主題再加 `SPORT_FLEX_NTFY_TOKEN`）。有填就會和 Telegram 一起送出搶到、付款提醒和登出，其中一條送不到時另一條仍會到。驗證碼圖片仍只走 Telegram，因為要能直接回覆。ntfy 預設優先級 4，可用 `SPORT_FLEX_NTFY_PRIORITY` 設成 1–5。
- `python -m sportflex --help` 可以用指令查場、看時段、送出預約。
- Docker：`docker compose -f infra/docker-compose.yml up -d --build`，然後開 `http://127.0.0.1:8765/`。瀏覽器掛掉或卡住沒有回應會自動重開；重開不了時容器會自己重啟。
- 要讓其他電腦連進來（`--host 0.0.0.0`）時，請在 `.env` 設 `SPORT_FLEX_UI_TOKEN`，第一次開 `/?token=…` 之後就記住了。

## 免責聲明

**此工具不負任何責任，也不承擔任何賠償或義務。**

Sport Flex 依「現況」與「現有」提供，沒有任何明示或默示保證，包括特定用途適用性、資料正確，或運作不中斷。

作者、貢獻者與此工具，對安裝、執行或無法執行所造成的一切不負責，包括沒訂到、重複訂、不想要的預約、場館費用與取消費、帳號受限、付款與個人資料，以及任何損失或爭議。

付款前請自行核對訂單。**此工具不會代為付款。** 它與任何運動中心或訂場平台沒有關係。使用、複製或修改此工具，即表示你接受本免責聲明。
