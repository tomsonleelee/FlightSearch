# Google Flights 操作筆記

## 網址

- 首頁：https://www.google.com/travel/flights
- 探索：https://www.google.com/travel/explore

## URL 直接構造搜尋（重要：可跳過手動填表）

Google Flights 搜尋結果 URL 使用 `tfs` 參數，格式為 **URL-safe base64 編碼的 Protobuf**。
日期、機場代碼、艙等等參數都是明文嵌在 protobuf 中，可以直接構造。

### URL 格式

```
https://www.google.com/travel/flights/search?tfs=<base64-protobuf>&tfu=KgIIAw&hl=zh-TW&curr=TWD
```

- `tfs`：搜尋條件（protobuf，見下方解碼工具）
- `tfu=KgIIAw`：固定值，必填
- `hl=zh-TW`、`curr=TWD`：語言幣別
- URL 會預填表單但**不會自動搜尋**，需點「搜尋航班」按鈕

### Protobuf 結構分析

解碼後的二進位結構（以 TPE→ATH 商務艙 2026-09-01/09-11 為例）：

```
field 1 (varint) = 28          # 固定常數，不明用途
field 2 (varint) = 1           # 乘客數
field 3 (bytes)  = <去程 leg>  # 出發段
  field 2 (string) = "2026-09-01"   # 日期 YYYY-MM-DD
  field 13 (bytes) = <origin>        # 出發機場
    field 1 = 1
    field 2 = "TPE"
  field 14 (bytes) = <dest>          # 目的地機場
    field 1 = 1
    field 2 = "ATH"
field 3 (bytes)  = <回程 leg>  # 回程段（來回票才有）
  field 2 = "2026-09-11"
  field 13 = ATH
  field 14 = TPE
field 8 (varint) = 0           # 轉機限制 0=任意 1=直飛 2=最多1轉
field 9 (varint) = 3           # 艙等 1=經濟 2=豪華經濟 3=商務 4=頭等
field 14 (varint) = 1          # 固定常數
<固定尾段>                     # 82010b08ffffffffffffffffff01980101
```

### Python 構造工具

```python
import base64

def encode_varint(n):
    result = b''
    while True:
        bits = n & 0x7F; n >>= 7
        result += bytes([bits | (0x80 if n else 0)])
        if not n: break
    return result

def encode_field_varint(f, v): return encode_varint((f<<3)|0) + encode_varint(v)
def encode_field_bytes(f, d): return encode_varint((f<<3)|2) + encode_varint(len(d)) + d

def encode_airport(iata):
    return encode_field_varint(1, 1) + encode_field_bytes(2, iata.encode())

def encode_leg(date, origin, dest):
    return (encode_field_bytes(2, date.encode()) +
            encode_field_bytes(13, encode_airport(origin)) +
            encode_field_bytes(14, encode_airport(dest)))

def build_google_flights_url(depart_date, origin, dest,
                              return_date=None, passengers=1,
                              cabin=1, stops=0,
                              hl='zh-TW', curr='TWD'):
    # cabin: 1=economy 2=premium_economy 3=business 4=first
    # stops: 0=any 1=nonstop_only 2=max_1_stop
    proto = (encode_field_varint(1, 28) +
             encode_field_varint(2, passengers) +
             encode_field_bytes(3, encode_leg(depart_date, origin, dest)))
    if return_date:
        proto += encode_field_bytes(3, encode_leg(return_date, dest, origin))
    proto += (encode_field_varint(8, stops) +
              encode_field_varint(9, cabin) +
              encode_field_varint(14, 1) +
              bytes.fromhex('82010b08ffffffffffffffffff01980101'))
    tfs = base64.urlsafe_b64encode(proto).decode().rstrip('=')
    return f'https://www.google.com/travel/flights?tfs={tfs}&tfu=KgIIAw&hl={hl}&curr={curr}'

# 範例：TPE→ATH 商務艙 來回
url = build_google_flights_url('2026-09-01', 'TPE', 'ATH',
                                return_date='2026-09-11',
                                cabin=3, stops=0)
```

### 實驗結論（2026-04-05）

| URL 格式 | 結果 |
|---------|------|
| `/flights/search?tfs=<protobuf>` | **可行**，完美預填搜尋條件（需點搜尋按鈕觸發） |
| `?q=Flights from TPE...` | 無效，忽略 q 參數，回到空白首頁 |
| `/travel/explore?q=...` | 無效，只設定出發城市，其他條件被忽略 |

**最佳做法**：用 Python 構造 `tfs` protobuf，直接開 URL，完全不需要手動填表單。

## 操作技巧

- 頁面語言和幣別會跟隨瀏覽器 locale，如需指定可在 URL 加 `?hl=zh-TW&curr=TWD`
- 搜尋表單的欄位是 autocomplete，輸入 IATA code 後需等下拉選單出現再點選
- 日曆視圖可以看整個月的每日最低價，適合第一階段探索
- 搜尋結果預設按「最佳航班」排序，可切換為「價格」排序

## 已知問題

- [2026-04-05] 日期欄位無法用 `agent-browser click @ref` 直接選取遠期月份 — 解決方式：直接用 `agent-browser fill @ref "2026-09-01"` 填入日期字串，Google Flights 能自動識別
- [2026-04-05] 日期網格（Date Grid）視窗只顯示有限範圍（約 ±3-4 天的出發日期 × 7-9天回程範圍），無法一次看到整個月 — 需要多次點擊「向右/向下捲動」按鈕來瀏覽
- [2026-04-05] 日期網格「向下捲動」移動的是回程日期（縱軸），「向右捲動」移動的是出發日期（橫軸）
- [2026-04-05] 較遠未來的日期（6月以後）在日曆選擇器中不會預載價格；需先選一個出發日期再點搜尋，才能從日曆中看到對應月份的出發日最低價
- [2026-04-05] 頁面可能出現「Travel restricted」橫幅（不影響搜尋功能）

## 操作技巧

- 日曆選擇器：選定出發日後重新開啟日曆，可以看到所有日期的最低票價（含目前出發日及附近日期）
- 搜尋後在「尋找最優惠的價格」區塊可以點「日期網格」查看出發/回程組合的矩陣票價
- 目的地輸入 IATA 代碼（如 ATH）後等 2 秒，下拉會出現正確機場選項
- 先用 `fill` 輸入出發/回程日期（格式 YYYY-MM-DD），比操作日曆 UI 更可靠
