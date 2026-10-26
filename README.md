# BTC Daily Value Map for Lean AMT

Generates a verifiable **Binance BTCUSDT Spot** value map from *actual historical executed aggregate trades* for the **last completed UTC day**, rather than inferring POC from OHLCV candles.

## Includes

- Previous closed UTC day: **trade-level VWAP**, POC, VAH, VAL, execution-price **±1 standard deviation** (volume-weighted), volume, and trade count.
- Current developing UTC month (through last completed UTC day): VWAP.
- Previous completed UTC month: VWAP.
- Rolling 365 completed UTC days: VWAP.
- Historical OHLC: previous UTC day and previous fully completed UTC week.
- Binance archive SHA-256 verification against the official companion `.CHECKSUM` file, plus an independent base-volume and VWAP cross-check against the closed daily kline.
- Explicit method, session boundary, as-of, source, $10 price buckets, 70% value area and evidence fields.

**Does NOT promise:** current intra-session or weekly VAH/VAL/POC, exact compatibility with TradingView profile, live OB absorption, multi-day VWAP SD, true exchange-wide global value or a direct live connection to ChatGPT Scheduled Tasks.

## Run locally

Python 3.10+ with standard library only:

```bash
python -m unittest discover -s tests -v
python calculate.py --output output/latest.json
# For a specific already completed UTC day:
python calculate.py --date 2026-10-24 --bucket 10 --output output/2026-10-24.json
```

Data files for BTCUSDT can be substantial; download size and runtime vary, and execution may fail on unavailable/blocked archives. ZIP files are cached in `.cache` and are ignored by Git. Every cached or newly downloaded ZIP must match Binance's companion SHA-256 checksum. The run also fails if aggregate-trade volume/VWAP do not match the corresponding closed daily kline. If an archive is missing or inconsistent, the tool fails rather than inventing or silently reusing a value. The default is yesterday UTC (today UTC minus one).

## Deploy with GitHub Actions

1. Create a GitHub repository and upload the contents of this folder (including `.github/workflows/daily.yml`).
2. In repository **Settings → Actions → General**, allow Actions; grant the `GITHUB_TOKEN` **read/write repository contents** if required by your organization. Consider a dedicated non-sensitive repository for public market data.
3. Run **Actions → BTC Lean AMT daily value map → Run workflow** once.
4. Scheduling is **02:30 UTC = 09:30 Bangkok** (attempt before the noon ChatGPT report) and **10:10 UTC = 17:10 Bangkok** (retry when archive is more likely published); the morning run may fail if Binance has not released the previous UTC day ZIP. GitHub scheduled workflows can run late or be missed. Investigate failed runs; missing data must not be silently re-used.
5. JSON appears at `output/latest.json` and a dated file `output/YYYY-MM-DD.json`. In a public repository its raw URL would be `https://raw.githubusercontent.com/OWNER/REPO/BRANCH/output/latest.json`, substituting the actual values. This link is NOT live until deployed.

**Integration warning:** ChatGPT's own daily automation cannot be assumed to fetch this private GitHub output or local file. For an automated report connection, publish the JSON at an actually accessible URL, and verify the ChatGPT execution can retrieve it. If not, keep AMT value data omitted, as specified by v2.2.

## Method / data integrity

- Binance public historical `aggTrades` CSV, using price and base BTC quantity. For years 2025 onward timestamps in public data are **microseconds**; earlier values may be milliseconds. The script converts as needed and filters trades to the desired UTC day.
- `bucket = floor(price / $10) * $10`. POC: bucket with most traded BTC (tie: lower-priced bucket). Value Area: expand outward from POC selecting higher volume from adjacent next buckets until at least 70% of BTC volume is contained. VAH is the upper **exclusive** bucket boundary; VAL is lower **inclusive**. Alternative TPO/VP implementations may disagree.
- Daily VWAP = sum(price × qty)/sum(qty); daily weighted SD = sqrt(sum(price² × qty)/sum(qty) − VWAP²).
- Month and 365D VWAP calculated exactly as `sum(quoteAssetVolume)/sum(baseAssetVolume)` over **completed UTC daily Binance Spot bars**, using Binance `klines` API; 365 bars are required. No price-dispersion SD invented from daily candles.
- Does **not** require API keys or order permissions.

Official references:
- https://github.com/binance/binance-public-data
- https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md

