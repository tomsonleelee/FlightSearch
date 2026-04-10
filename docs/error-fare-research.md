# Error Fare & Cheap Flight Search Research

> Research Date: 2026-04-10

---

## 1. What is an Error Fare?

### Definition

Error Fare (also known as Mistake Fare / Glitch Fare) refers to airline tickets sold at abnormally low prices due to pricing errors by airlines or OTAs. Discounts can reach 50%–90% off the regular price.

### How They Happen

| Cause | Description | Example |
|-------|-------------|---------|
| **Human input error** | Missing a digit, omitting fuel surcharge | $1,500 entered as $150 |
| **Currency conversion error** | System treats local currency as USD, incorrect exchange rate | Danish Krone price displayed as USD |
| **GDS system glitch** | Amadeus/Sabre/Travelport transmission error | Fare rules not properly applied |
| **AI pricing tool bug** | Airlines increasingly use AI dynamic pricing, more glitches | 2025 error fares doubled compared to 2024 ([Going.com stats](https://www.going.com/guides/mistake-fares)) |
| **New route/alliance setup error** | Missing fees when setting up new routes or codeshare | Fare rule conflicts between alliance partners |

### Will Airlines Cancel?

| Metric | Data |
|--------|------|
| **Cancellation rate** | ~10%–30% ([Secret Flying](https://www.secretflying.com/errorfare/) reports ~85% honored) |
| **Cancellation window** | Usually within 72 hours |
| **Legal standing** | Airlines are **not legally required** to honor error fares, but **cannot retroactively charge you more** |
| **Tend to honor** | Qatar Airways, Emirates (value brand reputation) |
| **Tend to cancel** | US domestic carriers are stricter |

**Recommended strategy**: After booking an error fare, do NOT book hotels or other arrangements. Wait 48–72 hours to confirm the ticket hasn't been cancelled.

---

## 2. Tools & Methods

### A. Error Fare Tracking Websites/Services

| Service | Type | Price | Features |
|---------|------|-------|----------|
| [**Secret Flying**](https://www.secretflying.com/errorfare/) | Website + App | Free | Error fare section, global routes, fuel dump tool |
| [**Going.com**](https://www.going.com/) (formerly Scott's Cheap Flights) | Subscription | Free / $49/yr / $199/yr | 35+ person team, 183 airports, Elite members get mistake fares first |
| [**Dollar Flight Club**](https://dollarflightclub.com/) | Subscription | $69/yr / $99/yr | Manual search, 30 airports |
| [**Jack's Flight Club**](https://jacksflightclub.com/) | Subscription | Free / £35/yr | Computer scans all routes/airlines, paid users get alerts hours earlier |
| [**ErrorFareAlerts.com**](https://errorfarealerts.com/) | Free | Free | Pure error fare notifications |
| [**The Points Guy**](https://thepointsguy.com/) | Media | Free | Comprehensive, includes mistake fare reporting and cancellation tracking |
| [**FlyerTalk**](https://www.flyertalk.com/) | Forum | Free | Oldest and most professional flight deal community, Mileage Run section |

### B. API / Scraping Methods for Price Anomaly Detection

| Method | Tool | Cost | Description |
|--------|------|------|-------------|
| **Amadeus Self-Service API** | [Flight Offers Search](https://developers.amadeus.com/self-service/category/flights/api-doc/flight-offers-price) | Pay per use (~$0.35–$2.40/search) | Official GDS API, real fares and rules |
| **Skyscanner API** | Partner application | Commercial | Scans 1,200+ airlines, redirect model |
| **Google Flights scraper** | [ScraperAPI](https://www.scraperapi.com/solutions/google-flights-scraper/) / [Apify](https://apify.com/simpleapi/google-flights-scraper) | ~$49–$499/mo | No official API, requires scraping, watch for anti-bot |
| **Skyscanner scraper** | [Apify Skyscanner Scraper](https://apify.com/jupri/skyscanner-flight/api) | Apify billing | Can get historical price trends |
| **FlightAPI.io** | [flightapi.io](https://www.flightapi.io/) | Free tier available | Real-time flight price API |

**Detection logic**: Build historical price time series → Calculate standard deviation → Trigger alert when price falls below mean - 2σ

### C. Community Role

| Platform | Function | Importance |
|----------|----------|------------|
| **Reddit** r/TravelDeals, r/flights | Real-time sharing + community validation | ⭐⭐⭐ Medium speed, validation valuable |
| **Telegram** | 900+ flight deal groups | ⭐⭐⭐⭐ Real-time push, fastest |
| **FlyerTalk forum** | Mileage Run Discussion section | ⭐⭐⭐⭐⭐ Most professional, veteran community |
| **Discord** | Secret Flying and other service Discords | ⭐⭐⭐⭐ Real-time alerts |
| **Twitter/X** | @SecretFlying and similar accounts | ⭐⭐⭐ Fast spread but low info density |

---

## 3. Advanced Search Techniques

### A. Hidden City Ticketing

| Item | Description |
|------|-------------|
| **Principle** | Book A→B→C but deplane at B, because A→C with connection is sometimes cheaper than A→B direct |
| **Tool** | [Skiplagged.com](https://blog.skiplagged.com/skiplagged-the-ultimate-guide-to-hidden-city-travel/) — search engine specifically for hidden city fares |
| **Savings** | 30%–50% |
| **Limitations** | ❌ Carry-on only (checked bags go to final destination) ❌ One-way only (return gets cancelled) ❌ Cannot earn miles ❌ Risk of account ban if detected |
| **Risk level** | 🟡 Medium — Legal but violates airline ToS, not recommended for frequent use |

### B. Fuel Dumping

| Item | Description |
|------|-------------|
| **Principle** | Fuel surcharge (YQ/YR) can be 75%+ of ticket price; adding a specific third segment ("3X strike") makes the system recalculate and remove the fuel surcharge |
| **Tools** | [ITA Matrix](https://matrix.itasoftware.com/) (view fare structure) + [Secret Flying Fuel Dump Tool](https://www.secretflying.com/posts/fuel-dumping-tool-step-by-step-guide/) |
| **Technique** | Use ITA Matrix to find "low base fare + high YQ" tickets → Add 3rd strike segment → Verify YQ removal |
| **Savings** | Hundreds of dollars (most effective for long-haul business class) |
| **Risk** | 🔴 High — Never call airline to modify; may be cancelled if discovered |

### C. Multi-Currency / Multi-POS Search

| Item | Description |
|------|-------------|
| **Principle** | Same flight shows different prices on different country websites/currencies due to market-specific pricing |
| **Real example** | Avianca same flight: USD $137 vs Colombian Peso equivalent $61.59 — **55% savings** |
| **How to** | Use VPN to switch countries → Search on airline's local website or Expedia country versions → Pay with **no foreign transaction fee credit card** |
| **Priority check** | ① Destination country website ② Airline's home country website ③ Third country with favorable exchange rate |
| **Tools** | Expedia Global Sites (switch country at bottom), airline country websites, Google Flights currency switch |
| **Risk** | 🟢 Low — Completely legal, doesn't violate any ToS |

### D. Different Origin Pricing

| Technique | Description |
|-----------|-------------|
| **Positioning Flight** | If nearby city has much cheaper departure, buy a budget flight there first |
| **Nearby Airport** | Google Flights "nearby airports" feature auto-compares surrounding airport prices |
| **Open Jaw** | A→B then C→A, self-transfer in between, sometimes cheaper than round trip |

### E. Award Ticket Sweet Spots (2025–2026)

| Sweet Spot | Description |
|-----------|-------------|
| **Virgin Atlantic → ANA First Class** | US West → Tokyo round trip 145,000 miles (retail $20,000+) |
| **Singapore KrisFlyer → IST Business** | 68,000 miles, 37% cheaper than other European cities |
| **Flying Blue Promo Rewards** | Monthly specific route 25% off mileage redemption |
| **Alaska Atmos short-haul** | Under 700 miles one-way only 4,500 points |

⚠️ **Important trend**: Since 2025, major airlines are accelerating shift from fixed award charts to **dynamic pricing** (e.g., Lufthansa Miles & More), sweet spots are diminishing.

---

## 4. Technical Feasibility Assessment (FlightSearch Project)

### A. Periodic Scanning for Price Anomaly Detection

| Aspect | Assessment |
|--------|------------|
| **Feasibility** | ✅ **Technically fully feasible** |
| **Data source** | Amadeus API (most reliable) or Google Flights / Skyscanner scrapers |
| **Architecture** | Cron Job periodic queries → Store in time series DB (InfluxDB / ClickHouse) → Calculate Z-score → Alert when below -2σ |
| **Cost** | 100 routes × 4 times/day = 400/day × $0.50/query ≈ **$200/day = $6,000/month** (Amadeus); scraping is cheaper but less stable |
| **Challenges** | Price volatility is inherently high, need sufficient historical data to avoid false positives; error fares typically exist only hours, scan frequency must be high enough |

### B. Multi-Currency / Multi-POS Comparison

| Aspect | Assessment |
|--------|------------|
| **Feasibility** | ✅ **Feasible but complex to implement** |
| **Method** | Use different POS codes with Amadeus API (supports market parameter) or scraper with proxy for country IP switching |
| **Architecture** | Query same route with 5–10 different POS → Convert to unified currency → Compare price differences → Recommend cheapest purchasing channel |
| **Cost** | Query volume ×5–10, API cost also ×5–10 |
| **Legal risk** | 🟢 Multi-POS queries are legal, but some airline API ToS may restrict this |

### C. Historical Price Comparison for Anomaly Detection

| Aspect | Assessment |
|--------|------------|
| **Feasibility** | ✅ **Feasible — this is the core feature** |
| **Technical approach** | Time series DB stores daily prices → Sliding window for moving average and std dev → ML model (GRU / XGBoost) predicts normal price range → Alert on threshold deviation |
| **ML reference** | Research shows GRU model has lowest MAE for fare prediction, XGBoost achieves 0.869 accuracy |
| **Cold start** | New routes have no historical data, need at least 30–90 days of data accumulation |
| **Recommended tools** | Python (pandas + scikit-learn) + InfluxDB/TimescaleDB + Grafana visualization |

### Recommended Technical Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│ Data Source  │────▶│  Price Store  │────▶│ Anomaly Engine │
│             │     │              │     │                │
│ • Amadeus   │     │ • InfluxDB   │     │ • Z-score      │
│ • Scraper   │     │ • TimescaleDB│     │ • GRU Model    │
│ • Multi-POS │     │              │     │ • Rules Engine │
└─────────────┘     └──────────────┘     └───────┬────────┘
                                                 │
                                          ┌──────▼────────┐
                                          │  Alert System  │
                                          │               │
                                          │ • Telegram Bot │
                                          │ • Email        │
                                          │ • Push         │
                                          └───────────────┘
```

### Cost-Benefit Analysis

| Tier | Est. Monthly Cost | Suitable For |
|------|-------------------|--------------|
| **Light**: Google Flights scraper + SQLite + simple Z-score | $50–100 (proxy + hosting) | Personal use, proof of concept |
| **Mid**: Amadeus API + TimescaleDB + basic ML | $500–2,000 | Small product, limited routes |
| **Heavy**: Multi-API + multi-POS + advanced ML + real-time alerts | $5,000–10,000+ | Commercial service, competing with Going.com |

---

## Summary & Recommendations

1. **Quickest ROI**: Start with "historical price anomaly detection" using scraper + simple statistics — low cost, MVP can be validated quickly
2. **Highest commercial value**: "Multi-POS comparison" is the true differentiator — few products on the market do this
3. **Error fare tracking**: Rather than building from scratch, **integrate existing services** (Secret Flying RSS, Telegram channels) and add custom filters
4. **Award sweet spots**: Due to dynamic pricing trends, long-term value of this feature is declining
5. **Hidden city / Fuel dump**: High legal and ToS risk — **not recommended** for a formal product
