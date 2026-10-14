# Sport Flex

[English](README.md) · [中文](README.zh-TW.md)

Sport Flex 查詢已設定場館的球場與可預約時段，送出預約，並停在付款前。本機網頁與選用的 Telegram 機器人也可以盯已開放的時段，或在場館釋出下一天名額時嘗試訂場。

> **免責附註。** 此工具不負任何責任。軟體依現況提供，沒有任何保證。使用風險由你自行承擔。詳見 [免責聲明](#免責聲明)。

## 做什麼

- 依 `venues/` 列出場館、球場與可預約時段。
- 每個帳號的登入狀態存在各自的瀏覽器 profile。
- 送出預約後把付款留給你。程式不會代為付款。
- 可從網頁或 Telegram 在開放時刻搶訂，也可以盯已經開放的時段。
- 開搶結果與事件在本機保留 30 天。

內建場館是長佳機電經營的新北市新店國民運動中心（`xindian`，平台 `changjia`）。要加別的場館，在 `venues/` 放一份 YAML 即可。

## 需求

- Python 3.12 或更新版本
- Google Chrome（Playwright 會啟動 `chrome` 通道）
- 場館帳號。帳號與密碼寫在 `.env`，不要提交進 git

## 安裝

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chrome
```

在 Linux 上若缺少 Chrome 的系統函式庫：

```bash
sudo .venv/bin/playwright install-deps chrome
```

在專案根目錄建立 `.env`（已被 gitignore）：

```bash
SPORT_FLEX_DEFAULT_ACCOUNT=你的帳號
SPORT_FLEX_DEFAULT_PASSWORD=你的密碼
# 選用。手機條碼載具，斜線加 7 碼，例如 /ABC1234。省略則開個人發票。
SPORT_FLEX_DEFAULT_INVOICE_CARRIER=

# 選用。Telegram 通知與指令。
SPORT_FLEX_TELEGRAM_BOT_TOKEN=
SPORT_FLEX_TELEGRAM_CHAT_ID=
# 多台機器共用同一個機器人時，驗證碼圖片上的名稱。例如 docker、筆電。
SPORT_FLEX_INSTANCE=
```

`config/accounts.yaml` 只寫 `.env` 的鍵名，不寫密碼。同一平台可以放多個帳號（例如家人）；每個帳號要用不同的瀏覽器 profile。

先登入一次，讓 profile 記住登入狀態：

```bash
python -m sportflex login --account default
```

## 指令

在專案根目錄、已啟用虛擬環境時執行（`python -m sportflex …`）。

| 指令 | 用途 |
| --- | --- |
| `venues` | 列出已設定的場館與帳號 |
| `courts --venue xindian --category 羽球` | 列出球場 |
| `slots --venue xindian --date 2026-10-09 --from 18:00` | 查可預約時段 |
| `slots --name 羽球場_A --scan` | 掃描整個可預約日期區間 |
| `login --account default` | 打開登入頁；登入狀態留在該帳號的 profile |
| `book --name 羽球場_A --date 2026-10-10 --time 20:00` | 送出一個時段，停在付款前 |
| `ui` | 打開網頁，位址 `http://127.0.0.1:8765` |
| `history --days 3` | 看最近的開搶結果與事件 |

省略 `--venue` 時用 `venues/` 的第一個場館。省略 `--account` 時用該平台的第一個帳號。`book` 會送出預約，不會代按付款。若要停在確認頁、不送出訂單，設 `SPORT_FLEX_SUBMIT_ORDER=0`。

`ui` 的選項：`--port`（預設 `8765`）、`--host`（只有打算對外開放時才用 `0.0.0.0`）、`--no-browser`（不要自動打開本機瀏覽器）。

## 網頁

`python -m sportflex ui` 提供的頁面可以：

- 輸入驗證碼並登入
- 看場地時段（全天、上午、下午、晚上）
- 對尚未開放的場次開始或取消搶訂
- 盯已開放的時段，有空位就訂
- 查看本機的開搶與通知紀錄

這個頁面本身沒有登入保護。不要把連接埠 `8765` 公開到本機以外。

## Telegram

`.env` 同時有 `SPORT_FLEX_TELEGRAM_BOT_TOKEN` 與 `SPORT_FLEX_TELEGRAM_CHAT_ID` 時，通知會送到該對話，機器人也會接受其中的指令。一個 bot token 只能有一個 long poll，所以一個機器人只跑一台伺服器。

登入失效時會先嘗試辨識驗證碼兩次。兩次都不對，才把圖片傳過來。直接回覆那張圖片上的英數即可重新登入。圖片開頭是 `[伺服器 · 帳號]`，用來區分本機、Docker 與多個帳號。

| 指令 | 用途 |
| --- | --- |
| `/status` | 登入與搶訂狀態 |
| `/login [帳號]` | 重新取驗證碼 |
| `/release` | 下一波開放時間 |
| `/slots [日期] [時段]` | 可預約時段 |
| `/snipe 19:00 [20:00]` | 搶下一波（任一場） |
| `/snipe A 19:00` | 指定場地 |
| `/cancel` | 取消搶訂 |
| `/watch 明天 晚上` | 盯已開放的時段，有空就訂 |
| `/watch off` | 停止盯場 |
| `/book 明天 19:00 [場地]` | 訂已開放的時段（會再請你 `/ok`） |
| `/courts` | 球場清單 |
| `/history` | 最近開搶 |
| `/help` | 本說明 |

日期可寫 `今天`、`明天`、`後天`、`10/20`。時段可寫 `上午`、`下午`、`晚上` 或 `19:00`。多個帳號時，在指令裡加上帳號 id。`SPORT_FLEX_TELEGRAM_RELOGIN=0` 會關閉 Telegram 重新登入。

## 場館與規則

`venues/` 裡每個檔案是一座場館：id、名稱、平台、運動種類、預約規則與備註。引擎會用到的規則包含時區、可預約天數、每日開放時間、每段時長、同一帳號每日時數上限，以及查詢間隔。內建的 `xindian` 記錄新店中心公開的窗口（14 天、每天 06:00 開放、每次 1 小時、同一天最多 2 小時且須不同場地）。這些數字是你自己維護的備註，場館可以隨時更改。

## Docker

```bash
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml logs -f
docker compose -f infra/docker-compose.yml down
```

打開 `http://127.0.0.1:8765/`，用頁面上的驗證碼登入一次。登入狀態存在 `sportflex-profiles` volume。容器裡的 Chrome profile 與本機 `python -m sportflex ui` 分開，兩邊不會搶同一個 profile。compose 只把網頁綁在 `127.0.0.1`（`SPORT_FLEX_PORT`，預設 `8765`）。

## 測試

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## 免責聲明

**此工具不負任何責任，也不承擔任何賠償或義務。**

Sport Flex 依「現況」與「現有」提供，沒有任何明示或默示保證，包括特定用途適用性、資料正確，或運作不中斷。

作者、貢獻者與此工具，對安裝、執行或無法執行所造成的一切不負責，包括：

- 沒訂到、重複訂、只訂到一部分，或不想要的預約
- 場館費用、取消費、不退費，或未付款而被取消的訂單
- 帳號受限、停權、驗證碼失敗，或場館、平台的操作頻率限制
- 付款、發票、個人資料，或你在場館網站輸入的任何內容
- 時段狀態、價格或預約規則不正確
- 任何損失、損害、請求或爭議

是否使用由你自行決定。你必須遵守各場館的使用條款與適用法律。付款前請自行核對每一筆訂單。**此工具不會代為付款。** 它與任何運動中心或訂場平台沒有關係，也未受其認可或營運。

使用、複製或修改此工具，即表示你接受本免責聲明。
