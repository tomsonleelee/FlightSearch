# FlightSearch

搜尋 Google Flights 找最便宜航班。三層工具鏈：URL 生成 → 策略組合 → Playwright 自動搜尋。

## 工具鏈

```
build_url.py  →  combo_search.py  →  search_flights.py
（生成 URL）     （生成多策略 URL）    （Playwright 自動搜尋）
                                           │
                                    price_tracker.py  →  prices.db (SQLite)
                                    （排程掃描 + 存儲）        │
                                                        price_alert.py → Telegram
                                                        （Z-score 偵測）

award_search.py（獨立工具 — Patchright 搜 Alaska Airlines 里程票）
```

- **`tools/build_url.py`**：構造 Google Flights 搜尋 URL（protobuf 編碼）
- **`tools/combo_search.py`**：生成組合票策略（baseline / open jaw / reverse / split）的搜尋 URL
- **`tools/search_flights.py`**：Playwright 無頭瀏覽器自動搜尋，直接輸出結構化結果
- **`tools/price_tracker.py`**：排程掃描監控航線，存入 SQLite
- **`tools/price_alert.py`**：Z-score 異常偵測 + Telegram 通知
- **`tools/award_search.py`**：Alaska Airlines 里程票搜尋（Patchright 反偵測瀏覽器）+ 月曆視圖

## 模型分工

- **Opus**（主對話）：策略規劃 — 選機場、排日期、決定搜尋策略、比價決策、最終推薦
- **Sonnet**（Agent tool）：僅用於需要 agent-browser 手動操作的場景（日曆探索）

一般搜尋不需要 LLM 介入 — `search_flights.py` 直接輸出結構化結果。

## 搜尋流程

### 快速模式（推薦，~2 分鐘）

適用情境：使用者給了明確的月份和天數範圍。

1. 使用者給需求（目的地、日期範圍、艙等、預算、轉機限制）
2. 挑選 5-8 組代表性的 **機場 + 日期** 組合
3. 用 `build_url.py --batch` 生成 URL
4. 用 `search_flights.py --parallel` 平行搜尋所有 URL
5. 匯集、比較、推薦

```bash
# 步驟 3：生成 URL
python3 tools/build_url.py TPE ATH --cabin business --batch \
    2026-09-01,2026-09-11 \
    2026-09-04,2026-09-14

# 步驟 4：平行搜尋（直接輸出結果表格）
python3 tools/search_flights.py --parallel --top 5 \
    --labels "9/1-9/11,9/4-9/14" \
    "<url1>" "<url2>"
```

### 組合票模式（~3 分鐘）

適用情境：使用者想找比直接來回票更便宜的組合。

**概念：**
- **Baseline**：標準來回票
- **Open Jaw**：去程飛 A→B，回程從附近城市 C→A，中間 B→C 自補（三段單程）
- **反向票**：買目的地出發的來回票 + 補一張單程去程
- **拆票**：經便宜樞紐拆成多段單程

**流程：**
1. 使用者給需求
2. 用 `combo_search.py` 生成所有策略的 URL
3. 去重 URL，分批用 `search_flights.py --parallel` 搜尋
4. 加總各策略的各段票價，跟 baseline 比較
5. 推薦最便宜的組合

```bash
# 步驟 2：生成策略 URL
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin business --json

# 步驟 3：去重後寫入檔案，平行搜尋
python3 tools/search_flights.py --parallel --top 3 --format json \
    --labels "Baseline RT,OW TPE→ATH,OW IST→TPE,..." \
    --file urls.txt
```

**重要：** 組合票每段分別查價，加總比較。補票段價格也要納入。

**策略篩選原則：**
- Open Jaw：優先選目的地附近有便宜交通連接的城市
- 反向票：適用於目的地所在國家的航空公司有促銷時
- 拆票：適用於有已知便宜樞紐的長途航線（如亞洲→歐洲經 BKK/IST）

**組合票適用情境（實測經驗）：**
- 商務艙的單程票約為來回票的 60-70%，三段加總通常超過來回票
- 經濟艙的單程/來回價差較小，組合票更可能划算
- 當 baseline 來回票定價已經很有競爭力時（如阿提哈德中東航線），組合票很難贏
- 組合票更適合：兩端都是主要樞紐、有大量廉航競爭、或來回票定價偏高的航線

### 完整模式（需要時才用，~15 分鐘）

適用情境：使用者日期完全彈性（例如「下半年任何時間」），需要先掃日曆找最便宜月份。

**第一階段：日曆探索（需 agent-browser）**
1. 用 agent-browser 手動操作 Google Flights 日曆視圖
2. 每個候選機場各啟一個背景 Sonnet Agent
3. 匯集結果，找出最便宜的日期和機場

**第二階段：用快速模式搜尋**
1. 從日曆結果選最有潛力的組合
2. 用 `search_flights.py` 平行搜尋

## 工具參考

### build_url.py — URL 生成

```bash
# 來回票
python3 tools/build_url.py TPE ATH 2026-09-01 2026-09-11 --cabin business

# 單程（不給 return_date）
python3 tools/build_url.py TPE ATH 2026-09-01 --cabin economy

# 批次生成
python3 tools/build_url.py TPE ATH --cabin business --batch \
    2026-09-01,2026-09-11 \
    2026-09-04,2026-09-14

# 參數
#   --cabin: economy | premium | business | first
#   --stops: 0=不限(預設) | 1=直飛 | 2=最多1轉
#   --passengers: 乘客數（預設 1）
#   --curr: 幣別（預設 TWD）
```

> **注意：** `--multi`（multi-city URL）已不建議使用。Google Flights 的 `/search` endpoint 會靜默將 multi-city URL 改寫為來回票。Open Jaw 改用三段單程替代。

### combo_search.py — 組合票策略生成

```bash
# 生成所有策略
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin business

# JSON 輸出（方便程式解析）
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --cabin business --json

# 只生成特定策略
python3 tools/combo_search.py TPE ATH 2026-09-01 2026-09-11 --types baseline open_jaw

# 可選策略類型：baseline, open_jaw, reverse, split
```

輸出結構（JSON 模式）：每個策略包含 `type`、`name`、`desc`、`segments`（每段有 `label` 和 `url`）。

### search_flights.py — Playwright 自動搜尋

無需 LLM 介入，直接用 Playwright 開啟 URL → 點搜尋 → 解析 aria-label → 輸出結果。

```bash
# 單一 URL
python3 tools/search_flights.py "<google-flights-url>"

# 多 URL 平行（推薦）
python3 tools/search_flights.py --parallel --top 5 \
    --labels "搜尋1,搜尋2,搜尋3" \
    "<url1>" "<url2>" "<url3>"

# 從檔案讀取 URL（一行一個）
python3 tools/search_flights.py --parallel --top 3 --format json \
    --labels "label1,label2" --file urls.txt

# 參數
#   --parallel: 平行執行（每 URL 一個子程序，各自獨立瀏覽器）
#   --top N: 每個 URL 最多回傳 N 筆結果（預設 10）
#   --format: table（人類可讀，預設）| json（程式解析）
#   --labels: 逗號分隔的標籤，對應每個 URL
#   --file: 從檔案讀取 URL
```

輸出欄位：航空公司、票價（TWD）、轉機次數、飛行時間、出發/抵達時間、轉機明細。

**技術細節：**
- 每個搜尋在獨立 incognito context 中執行（避免 session 互相干擾）
- 自動偵測單程 URL 並切換表單為「單程」模式
- 平行模式使用 subprocess（Playwright sync API 不支援 threading）
- 解析 Google Flights DOM：`li.pIav2d` 結果卡片、`.JMc5Xc` 的 `aria-label` 屬性

### price_tracker.py — 價格追蹤

定期掃描 watchlist.json 中的監控航線，存入 SQLite。

```bash
# 掃描所有監控航線，存入 DB
python3 tools/price_tracker.py

# 掃描後自動跑異常偵測
python3 tools/price_tracker.py --alert

# 只看 URL（不實際搜尋）
python3 tools/price_tracker.py --dry-run

# 自訂 watchlist
python3 tools/price_tracker.py --watchlist path/to/watchlist.json
```

**流程：** 讀 watchlist.json → `build_url` 生成 URL → `search_flights` 執行搜尋 → 寫入 `data/prices.db`

**設定檔（tools/watchlist.json）：** 定義監控航線、Z-score 閾值、通知設定。

### price_alert.py — 異常偵測

讀取歷史價格，用 Z-score 偵測異常低價，支援 Telegram 通知。

```bash
# 檢查異常
python3 tools/price_alert.py

# 啟用 Telegram 通知
python3 tools/price_alert.py --notify

# 顯示歷史價格摘要
python3 tools/price_alert.py --summary
```

**Z-score 邏輯：**
1. 每次掃描取該航線最低價，形成時間序列
2. 計算 mean 和 stdev（需至少 `min_samples` 筆）
3. `z = (current_min - mean) / stdev`
4. `z < z_threshold`（預設 -2.0）時觸發警報
5. 同航線同天只警報一次（alerts 表去重）

**Telegram 設定：** watchlist.json 啟用 + `.env` 設定 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`

### award_search.py — Alaska Airlines 里程票搜尋

用 Patchright（反偵測 Playwright）搜尋 Alaska Airlines 里程票。獨立工具，不依賴其他 tools。

```bash
# 安裝 Patchright
pip install patchright && patchright install chromium

# 單程搜尋
python3 tools/award_search.py SEA LAX 2026-10-01

# 來回搜尋
python3 tools/award_search.py SEA LAX 2026-10-01 --return-date 2026-10-08

# 日期區間（逐日搜尋）
python3 tools/award_search.py SEA LAX --start 2026-10-01 --end 2026-10-03

# 月曆視圖（整月最低里程數）
python3 tools/award_search.py SEA NRT 2026-10-01 --calendar

# JSON 輸出
python3 tools/award_search.py SEA LAX 2026-10-01 --format json
```

**技術細節：**
- 預設 headed 模式（Akamai 會擋 headless）
- 雙路搜尋：直接 URL → 表單 fallback
- 月曆視圖讀取 `<shoulder-dates>` 元件的 JSON，單次請求涵蓋整月
- `--headless` 可選但可能無結果

## agent-browser（僅日曆探索）

agent-browser 僅在需要手動操作日曆視圖時使用：

```bash
agent-browser open "https://www.google.com/travel/flights?hl=zh-TW&curr=TWD"
agent-browser wait --load networkidle
agent-browser snapshot -i
# 用 snapshot 回傳的 @ref 操作表單
```

用 `--session` 隔離平行查詢：
```bash
agent-browser --session s1 open "<url-1>"
agent-browser --session s2 open "<url-2>"
```

## 已知限制

- **Multi-city URL 不可用**：Google `/search` endpoint 會靜默改寫為來回票。Open Jaw 改用單程票替代。
- **Reverse 策略在冷門航線可能失敗**：如 ATH→TPE 方向 Google 可能沒有索引。
- **單程票定價不利**：商務艙單程約為來回 60-70%，組合策略需 3 段以上時很難贏過 baseline。

## 失敗記錄

每個網站一個檔案，放在 `sites/` 目錄下。格式：

```markdown
# <網站名> 操作筆記

## 已知問題
- [日期] 問題描述 → 解決方式

## 操作技巧
- 具體的 selector 或操作順序筆記
```

發現新問題時立即更新，這是給未來 session 的經驗累積。

## 結果輸出

搜尋結果存放在 `results/` 目錄：
- 檔名：`YYYY-MM-DD_出發地_目的地.md`
- 內容：搜尋條件摘要 + 航班比較表格 + 推薦
