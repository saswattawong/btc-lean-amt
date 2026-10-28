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

## Verified archive Spot / USD-M Futures CVD

`calculate_archive_cvd.py` produces `output/latest_cvd.json` from the free official Binance Data Vision daily archives for BTCUSDT Spot and USD-M perpetual. It requires no API key or paid data plan. Both ZIPs and their companion `.CHECKSUM` files are downloaded directly from Binance, and the output is rejected unless each SHA-256 matches and both markets contain exactly 1,440 contiguous, closed one-minute candles on identical UTC boundaries. The output keeps the compatible schema `btc_binance_quote_cvd_v1`.

For each market and window:

- `cvd_quote_usdt = sum(taker buy quote volume - taker sell quote volume)`
- `taker sell quote volume = total quote volume - taker buy quote volume`
- `taker_buy_share = taker buy quote volume / total quote volume`
- `normalized_cvd = cvd / total quote volume = 2 * taker_buy_share - 1`

The separate **Binance Data Vision BTCUSDT CVD** workflow checks at minute 17 of every UTC hour and retries automatically while the newest common archive is unavailable. Candidate days are tried newest-first. Each candidate must pass checksum, exactly 1,440 closed candles per market, and synchronized-boundary validation before selection; `archive_selection.attempts` preserves unavailable or invalid candidates instead of presenting an older day as current. The workflow rejects output older than 48 hours and uses a scoped retrying publisher so an unrelated output commit cannot cause the previous rebase conflict. Binance documents that a completed day's daily files become available the next day, so this feed is intentionally delayed closed-session evidence rather than live intraday CVD. `generated_at_utc` is never used as the source timestamp: the JSON carries `freshness.source_as_of_utc` and `freshness.source_age_seconds_at_generation`. The public dashboard input is:

- `https://raw.githubusercontent.com/saswattawong/btc-lean-amt/main/output/latest_cvd.json`

Run it locally with:

```bash
python -m unittest discover -s tests -v
python calculate_archive_cvd.py --output output/latest_cvd.json
# Reproduce one exact completed UTC day:
python calculate_archive_cvd.py --date 2026-10-26 --output output/latest_cvd.json
```

## Validated intraday snapshot

`calculate_cvd.py` publishes a separate `output/latest_intraday.json`; it never replaces the checksum-verified archive `latest_cvd.json`. The output schema is `btc_multi_provider_intraday_v2`. The **BTCUSDT validated intraday snapshot** workflow is scheduled every five minutes on GitHub's best-effort scheduler. Unit tests run in the separate CI workflow, not in the scheduled collector. Overlapping collector runs are cancelled so delayed jobs do not build a stale queue.

Binance Spot remains a 1,440-candle trailing source and supplies `btc_spot_price`. Intraday USDT-perpetual CVD is independently sourced from Gate's public `BTC_USDT` aggressor-side trades because Binance USD-M REST has returned HTTP 451 on GitHub-hosted runners. Gate trade size sign supplies taker direction; quote notional is `abs(size_contracts) * quanto_multiplier_btc_per_contract * price_usdt`, where the multiplier is validated from Gate's contract endpoint on every run. Spot and perpetual values retain separate providers, endpoints, timestamps, instruments, coverage, and integrity and are never merged.

Gate minute aggregates persist in `output/perp_cvd_state.json`. A new state bootstraps one verified hour, so 1H can become available immediately while 4H/24H remain explicit `N/A` until enough contiguous scheduled observations accumulate. Every trade query is recursively time-sliced until each response is below Gate's page limit; a range that cannot be proven untruncated is rejected. Binance funding and open interest remain point-in-time metrics and stay `N/A` when their official endpoints are inaccessible; this P0/P1 change does not implement P2 derivatives history.

The freshness gate is 15 minutes. `monitor_intraday_freshness.py` measures the age of the previously published `source_as_of_utc` at collector start, keeps a rolling 288-observation SLO ledger in `output/intraday_freshness.json`, and marks scheduled runs failed after recovery/publish when the pre-collection age exceeded 900 seconds. The target is age <=15 minutes for at least 95% of the 288 checked rounds. GitHub schedules still provide no execution-time SLA.

Public intraday input:

- `https://raw.githubusercontent.com/saswattawong/btc-lean-amt/main/output/latest_intraday.json`

Run and validate it locally with:

```bash
python calculate_cvd.py --output output/latest_intraday.json
python validate_outputs.py intraday output/latest_intraday.json --max-age-seconds 900
```

## Run locally

Python 3.10+ with standard library only:

```bash
python -m unittest discover -s tests -v
python calculate.py --output output/latest.json
# For a specific already completed UTC day:
python calculate.py --date 2026-10-24 --bucket 10 --output output/2026-10-24.json
```

Data files for BTCUSDT can be substantial; download size and runtime vary, and execution may fail on unavailable/blocked archives. ZIP files are cached in `.cache` and are ignored by Git. Every cached or newly downloaded ZIP must match Binance's companion SHA-256 checksum. The run also fails if aggregate-trade volume/VWAP do not match the corresponding closed daily kline. If the newest completed Spot/Futures archive pair is unavailable or invalid, the default command tries older completed UTC days within a four-day lookback. The selected source day and every failed candidate remain explicit in `archive_selection`; no failed candidate contributes values.

## Deploy with GitHub Actions

1. Create a GitHub repository and upload the contents of this folder (including `.github/workflows/daily.yml`).
2. In repository **Settings → Actions → General**, allow Actions; grant the `GITHUB_TOKEN` **read/write repository contents** if required by your organization. Consider a dedicated non-sensitive repository for public market data.
3. Run **Actions → BTC Lean AMT daily value map → Run workflow** once.
4. Scheduling is **02:30, 03:30 and 04:00 UTC = 09:30, 10:30 and 11:00 Bangkok**, with a **10:10 UTC = 17:10 Bangkok** recovery retry. Scheduled retries are skipped once a complete, integrity-passed result for the target day exists. The morning archive may still be unavailable, and GitHub scheduled workflows can run late or be missed. Investigate failed runs; missing data must not be silently re-used.
5. Machine JSON appears at `output/latest.json` and `output/YYYY-MM-DD.json`. A dashboard-ready Markdown fallback appears at `output/latest.md` and `output/YYYY-MM-DD.md`. For this public deployment, use:
   - JSON: `https://raw.githubusercontent.com/saswattawong/btc-lean-amt/main/output/latest.json`
   - Brief: `https://raw.githubusercontent.com/saswattawong/btc-lean-amt/main/output/latest.md`

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
- https://www.gate.com/docs/developers/apiv4/en/futures/


## v2.2 enhanced postprocessing (October 2026)

The daily workflow now runs `python enrich.py --input output/latest.json` after the original checksum/volume-verified daily calculation and **before** publishing JSON. This additional pass downloads the preceding completed UTC day's aggTrades for daily POC migration and all seven daily aggTrades archives for the **previous completed UTC week** to calculate weekly POC, VAH, VAL and VWAP. The weekly result is checked against the sum of the seven closed spot klines. This costs more bandwidth/runtime; a first run can be slow and the workflow may fail within its 45-minute timeout. No stale output should be presented as newly calculated. Failed workflow runs should be inspected under the Actions tab.

New JSON fields once the updated Actions workflow successfully runs: `report_reference_date_utc` (one UTC day after `as_of_closed_day_utc`), `daily_poc_migration`, and `previous_completed_week_trade_volume_profile`. The `levels.previous_day` now refers to the last completed UTC day **as of the report reference date**, fixing the original one-day offset. Note: `output/latest.json` already committed before this upgrade retains the original schema until the next successful workflow publish.

The ChatGPT daily task tries to retrieve https://raw.githubusercontent.com/saswattawong/btc-lean-amt/main/output/latest.json, but access is not guaranteed for a private repo or blocked HTTP fetch. Never assume the automation can read a connector-authorized GitHub file just because the connector can. Omit the Value Map when not independently fetched and validated. All calculated metrics are historical closed-session data, not live indicators.
