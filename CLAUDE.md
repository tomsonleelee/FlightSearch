# FlightSearch

用 Claude Code + agent-browser 搜尋 Google Flights，找最便宜的航班。

## 模型分工

- **Opus**（主對話）：策略規劃 — 選機場、排日期、生成 URL、比價決策、最終推薦
- **Sonnet**（Agent tool, `model: "sonnet"`）：資訊判讀 — 開 URL、解析頁面快照、提取價格與航班資訊

解析頁面時，一律 spawn Sonnet agent 處理，不要用 Opus 讀 snapshot 原文。

## 搜尋流程

### 快速模式（推薦，~5 分鐘）

適用情境：使用者給了明確的月份和天數範圍。

1. 使用者給需求（目的地、日期範圍、艙等、預算、轉機限制）
2. 列出候選機場（目的地附近的主要機場）
3. Opus 挑選 5-8 組代表性的 **機場 + 日期** 組合
   - 涵蓋不同出發日（週間 vs 週末）
   - 涵蓋不同天數（範圍內最短、中間、最長）
4. 用 `tools/build_url.py --batch` 生成所有 Google Flights URL
5. **平行查詢**：每組各啟一個背景 Agent（`run_in_background: true`），直接開 URL 抓結果
6. 匯集、比較、推薦

### 完整模式（需要時才用，~15 分鐘）

適用情境：使用者日期完全彈性（例如「下半年任何時間」），需要先掃日曆找最便宜月份。

**第一階段：日曆探索**
1. 手動操作 Google Flights 日曆視圖（此步無法用 URL 跳過）
2. 每個候選機場各啟一個背景 Agent
3. 匯集結果，找出最便宜的日期和機場

**第二階段：用快速模式流程搜尋**
1. 從日曆結果選最有潛力的組合
2. 按快速模式步驟 4-6 執行

## Google Flights 操作指南

### URL 直接搜尋（優先使用）

用 `tools/build_url.py` 構造搜尋 URL，跳過手動填表單：

```bash
# 單一搜尋
python3 tools/build_url.py TPE ATH 2026-09-01 2026-09-11 --cabin business

# 批次生成（推薦）
python3 tools/build_url.py TPE ATH --cabin business --batch \
    2026-09-01,2026-09-11 \
    2026-09-02,2026-09-11 \
    2026-09-04,2026-09-14

# 參數說明
#   --cabin: economy | premium | business | first
#   --stops: 0=不限(預設) | 1=直飛 | 2=最多1轉
#   --passengers: 乘客數（預設 1）
#   --curr: 幣別（預設 TWD）
```

Agent 操作流程（只需 4 步）：

```bash
# 1. 開啟預填好的搜尋頁面（URL 會自動填入所有搜尋條件）
agent-browser --session <name> open "<generated-url>" && agent-browser --session <name> wait --load networkidle

# 2. 點「搜尋航班」按鈕（URL 會預填表單但不會自動搜尋）
agent-browser --session <name> snapshot -i
agent-browser --session <name> click @<搜尋航班按鈕的ref>
agent-browser --session <name> wait --load networkidle

# 3. 取得搜尋結果
agent-browser --session <name> snapshot -i

# 4. 解析航班資訊（由 Sonnet agent 處理 snapshot 內容）
```

> 注意：URL 預填表單但不會自動觸發搜尋。Agent 需要做一次 snapshot 找到「搜尋航班」按鈕，點擊後再 snapshot 取結果。

### 手動表單操作（僅日曆探索時使用）

只有在需要操作日曆視圖時才需要手動填表單：

```bash
# 開啟 Google Flights
agent-browser open "https://www.google.com/travel/flights?hl=zh-TW&curr=TWD"
agent-browser wait --load networkidle
agent-browser snapshot -i

# 操作搜尋表單（用 snapshot 回傳的 @ref）
# 每步操作後重新 snapshot 取得新 refs
```

### 平行查詢

使用 `--session` 隔離不同查詢：

```bash
agent-browser --session s1 open "<url-1>"
agent-browser --session s2 open "<url-2>"
agent-browser --session s3 open "<url-3>"
```

### Agent prompt 範本（快速模式）

給 Sonnet agent 的 prompt 應包含：
1. 預先生成好的 Google Flights URL
2. 用 `--session <唯一名稱>` 避免衝突
3. 明確要求提取的資訊格式（航空公司、票價、轉機、時間）
4. 完成後 `agent-browser --session <name> close`

### 重要提示

- 操作前先讀 `sites/google-flights.md` 的已知問題和 URL 格式說明
- URL 構造的技術細節記錄在 `sites/google-flights.md`
- 每步操作後用 `agent-browser snapshot -i` 確認狀態
- 頁面載入需要時間，用 `agent-browser wait --load networkidle`
- 遇到新的失敗模式 → 立即更新 `sites/google-flights.md`

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
