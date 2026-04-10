# AI 爬蟲工具研究 — Playwright 替代方案評估

> 研究日期：2026-04-10
> 背景：評估是否有比 Playwright 更省資源的 AI 爬蟲工具，用於爬取 Google Flights 等動態網頁

---

## 1. 主流 AI 爬蟲工具一覽

### A. Firecrawl

| 項目 | 說明 |
|------|------|
| **官網** | https://www.firecrawl.dev/ |
| **GitHub** | https://github.com/firecrawl/firecrawl （81K+ stars） |
| **類型** | API 服務 + 可自架（自架版尚未 production-ready） |
| **核心功能** | 將網頁轉為乾淨的 Markdown / JSON，專為 LLM 設計 |
| **API 端點** | scrape、crawl、search、map、agent、interact（6 個端點） |
| **動態內容** | ✅ JS 渲染、模擬點擊/捲動/填表（/interact 端點） |
| **反爬機制** | ✅ 內建 proxy 輪換、rate limiting |
| **背景** | Y Combinator 投資，Series A $14.5M，350,000+ 開發者使用 |

**定價（credit 制，1 credit ≈ 1 頁）：**

| 方案 | 月費 | Credits | 每頁成本 |
|------|------|---------|---------|
| Free | $0 | 500（終生，非每月） | — |
| Hobby | $16 | 3,000 | $0.0053 |
| Standard | $83 | 100,000 | $0.00083 |
| Growth | $333 | 500,000 | $0.00067 |
| Enterprise | 洽談 | 自訂 | — |

⚠️ **注意**：Extract 功能有 5x credit 乘數，實際消耗比標示多。

---

### B. Crawl4AI

| 項目 | 說明 |
|------|------|
| **官網** | https://docs.crawl4ai.com/ |
| **GitHub** | https://github.com/unclecode/crawl4ai （60K+ stars） |
| **類型** | 完全開源（MIT License），可自架 |
| **核心功能** | 將網頁轉為 LLM-ready Markdown，專為 RAG pipeline 設計 |
| **動態內容** | ✅ 透過 Playwright 引擎渲染 JS，支援自訂 JS 腳本模擬行為 |
| **資料提取** | CSS selector、XPath、LLM-based extraction、cosine similarity 語意過濾 |
| **效能** | 號稱比同類工具快 6 倍，支援多 URL 並行爬取 |
| **離線運作** | ✅ 可搭配本地模型（Ollama），完全不需雲端 |

**定價：**

| 方案 | 費用 | 說明 |
|------|------|------|
| 自架 | **免費** | 需自備伺服器（Docker 20.10+，至少 4GB RAM） |
| 自架 + LLM | ~$100–300/月（50K 頁） | 伺服器 + LLM API 費用（如 OpenAI） |
| Apify 託管 | Apify 計費 | 免去自架維護 |

---

### C. ScrapeGraphAI

| 項目 | 說明 |
|------|------|
| **官網** | https://scrapegraphai.com/ |
| **GitHub** | https://github.com/ScrapeGraphAI/Scrapegraph-ai |
| **類型** | 開源 Python 套件 + 付費 API 服務 |
| **核心功能** | 用**自然語言**描述想提取的資料，LLM 自動理解並提取 |
| **動態內容** | ✅ 內建 Playwright headless 瀏覽器渲染 |
| **LLM 支援** | OpenAI、Azure、Gemini、Groq、Ollama 本地模型 |
| **自修復** | ✅ 網站結構變更時 AI 自動適應，維護成本降低 ~70% |
| **管線類型** | SmartScraperGraph（單頁）、Multi（多頁）、SearchGraph（搜尋+爬取）、ScriptCreator（產生 Python 腳本） |

**定價：**

| 方案 | 月費 | 頁數 |
|------|------|------|
| 開源版 | 免費 | 自備 LLM key |
| API Starter | $19 | 10,000 頁 |
| API Pro | 洽談 | 更多頁數 |

---

### D. Browser-Use

| 項目 | 說明 |
|------|------|
| **官網** | https://browser-use.com/ |
| **GitHub** | https://github.com/browser-use/browser-use （60K+ stars） |
| **類型** | 開源 Python 套件（需 Python ≥3.11） |
| **核心功能** | 讓 AI Agent 像人一樣操作瀏覽器，自動識別頁面互動元素 |
| **底層** | 基於 Playwright，加上 LLM-native 控制層 |
| **效能** | 基準測試 89.1% 成功率 |
| **資金** | 種子輪 $17M+ |

**定價：** 開源免費，另有 Browser Use Cloud（需 API key，費用另計）

---

### E. Skyvern

| 項目 | 說明 |
|------|------|
| **官網** | https://www.skyvern.com/ |
| **GitHub** | https://github.com/Skyvern-AI/skyvern |
| **類型** | 開源 + 雲端服務 |
| **核心功能** | 用 LLM + Computer Vision 理解網頁，自動適應不同網站佈局 |
| **特色** | CAPTCHA 解決、2FA 支援、反偵測機制、proxy 內建 |
| **並行** | ✅ 支援大規模並行任務 |

**定價：**

| 方案 | 費用 |
|------|------|
| 開源自架 | 免費 |
| Cloud Free | 1,000 credits 起 |
| Cloud Pay-as-you-go | $0.05/step |
| Enterprise | 洽談 |

---

### F. Stagehand + Browserbase

| 項目 | 說明 |
|------|------|
| **官網** | https://www.stagehand.dev/ / https://www.browserbase.com/ |
| **GitHub** | https://github.com/browserbase/stagehand |
| **類型** | SDK + 雲端瀏覽器基礎設施 |
| **核心功能** | Playwright 等級的控制力 + AI 原語（act、extract、observe） |
| **特色** | 持久化瀏覽器 context、反偵測優化、即時 session 檢視 |
| **資金** | Series B $40M（2025 年 6 月），50M+ sessions/年 |

**定價：** ~$100/月起，按 session 計費，100 小時 ≈ 3,000 頁級任務

---

## 2. 與 Playwright 的比較

### 優缺點對照表

| 面向 | Playwright（傳統） | AI 爬蟲工具 |
|------|-------------------|-------------|
| **資源消耗** | 🔴 高 — 每個頁面啟動完整瀏覽器實例，佔大量 CPU/RAM | 🟢 低（API 模式）— 運算在雲端；自架模式仍需瀏覽器但有優化 |
| **開發效率** | 🔴 需手寫 CSS/XPath selector，網站改版就壞 | 🟢 自然語言描述 or LLM 自動適應，維護成本降 70% |
| **穩定性** | 🟡 selector 脆弱，Google Flights 頻繁改版 | 🟢 AI 自修復，佈局變更自動適應 |
| **反爬繞過** | 🔴 需自己處理 proxy、指紋、CAPTCHA | 🟢 多數工具內建反爬機制 |
| **成本** | 🟢 免費開源 + 自己的伺服器費用 | 🟡 API 需付費，自架需 LLM 費用 |
| **精確控制** | 🟢 完全掌控每個操作步驟 | 🟡 AI 有不確定性，關鍵路徑可能需要 fallback |
| **速度** | 🟡 受限於瀏覽器渲染速度 | 🟡 API 模式有網路延遲，但省去本地渲染 |
| **資料品質** | 🟡 拿到原始 HTML 需自己 parse | 🟢 直接輸出結構化 JSON/Markdown |

### 資源消耗具體比較

| 方案 | CPU 使用 | RAM 使用 | 適合規模 |
|------|---------|---------|---------|
| Playwright 本地 | 每實例 ~1 CPU core | 每實例 ~200-500MB | 小規模（<100 頁/次） |
| Crawl4AI 自架 | 同上（底層仍是 Playwright） | 最低 4GB | 中規模，有並行優化 |
| Firecrawl API | 本地幾乎不佔 | 本地幾乎不佔 | 大規模，但受 credit 限制 |
| Browser-Use | 同 Playwright（本地模式） | 同 Playwright | 中規模，LLM 呼叫有延遲 |

---

## 3. 適不適合爬 Google Flights？

### Google Flights 的技術挑戰

| 挑戰 | 說明 |
|------|------|
| **重度 SPA** | React 應用，所有內容透過 JS 動態渲染 |
| **複雜互動** | 需要輸入出發地/目的地、選日期、等待搜尋結果載入 |
| **反爬嚴格** | Google 有強大的 bot 偵測（reCAPTCHA、行為分析） |
| **頻繁改版** | UI 結構經常變動，selector 容易失效 |
| **載入時間長** | 搜尋結果需要 3-8 秒才完整載入 |

### 各工具適用性評估

| 工具 | 適合度 | 原因 |
|------|--------|------|
| **Firecrawl /interact** | ⭐⭐⭐⭐ | 支援互動式操作（填表、點擊、等待），雲端執行不佔本地資源，但 Google 反爬可能攔截 |
| **Crawl4AI** | ⭐⭐⭐ | 可自訂 JS 腳本模擬互動，但底層仍是 Playwright，資源消耗類似；優勢在 LLM 自動適應結構變更 |
| **ScrapeGraphAI** | ⭐⭐⭐ | 自然語言提取很方便，但 Google 反爬是最大瓶頸 |
| **Browser-Use** | ⭐⭐⭐⭐ | AI Agent 模式最接近真人操作，89.1% 成功率，但需 LLM 呼叫成本 |
| **Skyvern** | ⭐⭐⭐⭐ | Computer Vision 辨識頁面，不依賴 selector，CAPTCHA 解決能力強 |
| **Stagehand + Browserbase** | ⭐⭐⭐⭐⭐ | 雲端瀏覽器 + AI 控制，反偵測最成熟，50M+ sessions 驗證過，但成本較高 |
| **Playwright（原方案）** | ⭐⭐ | 可行但維護成本高、資源消耗大、selector 脆弱 |

### 最大瓶頸：Google 反爬

**不論用什麼工具，Google Flights 的反爬是核心問題。** 以下是繞過策略：

| 策略 | 說明 |
|------|------|
| Residential Proxy | 使用住宅 IP 池（Bright Data、Oxylabs），降低被封風險 |
| 指紋偽裝 | 隨機化 User-Agent、螢幕解析度、語言等 |
| 行為模擬 | 隨機延遲、滑鼠移動、捲動模擬 |
| 雲端瀏覽器 | Browserbase 等服務已針對反偵測優化 |
| **替代方案** | 改用 Google Flights Scraper API（Apify/ScraperAPI），讓別人處理反爬 |

---

## 4. 成本與免費方案總整理

| 工具 | 免費方案 | 付費起價 | 自架可能性 | 爬 Google Flights 月成本估計（1,000 頁/天） |
|------|---------|---------|-----------|-------------------------------------------|
| **Playwright** | ✅ 完全免費 | — | ✅ | ~$50–100（伺服器 + proxy） |
| **Firecrawl** | 500 credits（終生） | $16/月 | 🟡 有但未 production-ready | $83–333/月（Standard–Growth） |
| **Crawl4AI** | ✅ 完全免費開源 | — | ✅ Docker | ~$100–200（伺服器 + LLM API） |
| **ScrapeGraphAI** | ✅ 開源版免費 | $19/月 | ✅ | ~$50–150（伺服器 + LLM API） |
| **Browser-Use** | ✅ 開源免費 | Cloud 另計 | ✅ | ~$100–200（伺服器 + LLM API） |
| **Skyvern** | 1,000 credits | $0.05/step | ✅ | ~$150–450/月（cloud） |
| **Browserbase + Stagehand** | 有限免費額度 | ~$100/月 | ❌ 雲端服務 | ~$200–500/月 |
| **Apify Google Flights Scraper** | 有限免費額度 | ~$49/月 | ❌ | ~$49–199/月（最省心） |

---

## 5. 針對 FlightSearch 專案的建議

### 方案比較

| 方案 | 優點 | 缺點 | 適合場景 |
|------|------|------|---------|
| **A. Apify Google Flights Scraper** | 最省心，有人維護反爬 | 依賴第三方，價格可能變 | MVP / 概念驗證 |
| **B. Crawl4AI 自架 + Residential Proxy** | 免費開源、完全掌控、LLM 自適應 | 需自己處理 Google 反爬、需維護伺服器 | 中期正式方案 |
| **C. Firecrawl API /interact** | 省資源、好用、開發快 | credit 制有隱藏成本、Google 反爬不確定 | 開發速度優先 |
| **D. Browserbase + Stagehand** | 反偵測最成熟、穩定性最高 | 成本最高 | 商業級、高可靠性需求 |

### 推薦路線

```
Phase 1 (MVP):     Apify Google Flights Scraper
                    └─ 最快上線，驗證需求
                    └─ ~$49–99/月

Phase 2 (正式版):   Crawl4AI 自架 + Residential Proxy
                    └─ 成本可控，完全掌控
                    └─ ~$100–200/月
                    └─ 搭配 LLM 自動適應 Google Flights 改版

Phase 3 (擴展):     混合架構
                    └─ Crawl4AI 為主力
                    └─ Firecrawl /interact 做 fallback
                    └─ 加入多 POS 比價（多國 proxy）
```

### 結論

> **不建議繼續用純 Playwright 爬 Google Flights。** Crawl4AI 是最佳平衡點 — 底層同樣是 Playwright 但加了 LLM 自適應和並行優化，而且完全免費開源。短期可先用 Apify 現成的 Google Flights Scraper 快速驗證，中期再遷移到自架 Crawl4AI。
