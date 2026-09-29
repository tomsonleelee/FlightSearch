# TFT 常旅客商務艙來源

`fare_aggregator.SOURCES['tft_business']` 已接至原有 bugfare tick；不是新的 timer。

## 範圍與資料

- RSS `https://www.the-frequent-traveler.com.tw/feed/`，Mozilla User-Agent（預設 Python UA 會 403）。只取最新已發表 `asia-biz-deals-YYYYMMDD` 文章，不掃 100 篇歷史文章；停機期間漏過的舊期數不會自動補通知。
- `tft_source.py` 確定性解析文章中的詳細航程卡及可明確解析的摘要卡，不呼叫 LLM、不需要 Facebook 登入。未知城市／含糊機場不猜。
- `tft_notifications.py` 將每個行程報價寫入 `bug_fares` 和現有 normalized tables，原始解析 JSON 保留來源、訂票 URL、明示航段及資料限制。
- 來源標示的商務艙／來回含稅並非本站實際驗價；不保證平躺座椅、庫存或日期可訂。貼文的日期字串保留，訂位頁面才是最終依據。
- 每筆 guid 代表航程／航司／日期／艙等，不含文章日期或價格。同篇重複段落合併；跨日相同行程不重發。首次新行程、或低於該行程已成功通知的所有價格才通知；日期變更屬新行程。每次價格變動仍保留報價記錄。

## 通知

- 沿用 `TPE_REACHABLE_CODES` 出發機場篩選，不新增目的地或回程終點限制。不重新定義／擴張既有機場白名單。
- 非 TPE 出發明示需另買接駁票，成本未含；摘要附來源及訂票連結。
- 3500 字元左右整筆分組傳送，不切斷 HTML 標籤。只有成功傳送的分組才更新 `alerted_at`。
- 無憑證或送出失敗時有符合優惠則 tick 失敗；未標記的優惠下次仍出現在最新文章時重試。網路 timeout 後若平台其實已收件，仍有 at-least-once 重複風險，不宣稱 exactly-once。
- `--no-alert` 儲存但不標記寄出；再次正常執行仍可能通知。`--dry-run` TFT 寫入暫存記憶體 DB、絕不寫正式 DB 或通知。
- 抓取失敗、文章完全無法解析會拋錯，不能把故障当成「沒有新優惠」。部分卡片略過會有 warning，可能漏掉資料不足的優惠。

## 驗證與運維

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python fare_aggregator.py --dry-run
systemctl --user status flightsearch-bugfare.timer
```

首次啟用前以 SQLite backup API 備份正式 DB、真實文章在隔離 DB 經實際 run_tick 重播、核對 eligible/filtered、新資料 normalized rows、寄送失敗重試與重跑 dedup；首次正式 tick 後核對寄出標記及第二輪無重送。原有其他來源與 Alaska/summary timer 狀態不變。

僅停用本來源可將 registry 的 enabled 設為 False；不需刪 DB 或清空 dedup。
