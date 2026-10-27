#!/usr/bin/env python3
"""Hourly Binance BTCUSDT Spot and USD-M Futures quote-volume CVD.

The calculation uses closed one-minute klines on one synchronized, top-of-hour
UTC boundary.  CVD is taker-buy quote volume minus taker-sell quote volume.
"""

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal


UTC = dt.timezone.utc
MINUTE_MS = 60_000
SPOT_KLINES = ("https://data-api.binance.vision/api/v3/klines",)
USD_M_FUTURES_KLINES = tuple(
    f"https://fapi{suffix}.binance.com/fapi/v1/klines"
    for suffix in ("", "1", "2", "3", "4")
)
WINDOWS = {"1h": 60, "4h": 240, "24h": 1_440}


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def closed_hour_boundary(now):
    """Return the most recent top-of-hour UTC boundary.

    The live workflow runs after minute seven. If called in the first two minutes
    of an hour, use the previous hour to allow the final candle to settle.
    """
    now = now.astimezone(UTC)
    boundary = now.replace(minute=0, second=0, microsecond=0)
    if now < boundary + dt.timedelta(minutes=2):
        boundary -= dt.timedelta(hours=1)
    return boundary


def parse_as_of(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a UTC timezone")
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("--as-of must be an exact top-of-hour UTC boundary")
    return parsed


def request_json(url, attempts=3):
    request = urllib.request.Request(url, headers={"User-Agent": "btc-lean-amt-cvd/1.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            if attempt + 1 == attempts:
                raise
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2 ** attempt)


def fetch_klines(endpoint, symbol, start_ms, end_ms, requester=request_json):
    """Fetch [start_ms, end_ms) one-minute klines, paginating at 1,000 rows."""
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": 1000,
            }
        )
        page = requester(f"{endpoint}?{params}")
        if not isinstance(page, list):
            raise ValueError(f"Unexpected Binance kline response from {endpoint}")
        if not page:
            break
        last_open = None
        for row in page:
            if not isinstance(row, list) or len(row) < 11:
                raise ValueError(f"Malformed Binance kline from {endpoint}")
            open_ms = int(row[0])
            last_open = open_ms
            if start_ms <= open_ms < end_ms:
                rows.append(row)
        if last_open is None or last_open < cursor:
            raise ValueError(f"Non-advancing Binance kline page from {endpoint}")
        cursor = last_open + MINUTE_MS
    return rows


def fetch_klines_with_failover(endpoints, symbol, start_ms, end_ms, fetcher=fetch_klines):
    """Try Binance-owned REST hosts in order and return rows plus the host used."""
    errors = []
    for endpoint in endpoints:
        try:
            return fetcher(endpoint, symbol, start_ms, end_ms), endpoint
        except (
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
        ) as error:
            status = getattr(error, "code", None)
            detail = f"HTTP {status}" if status is not None else type(error).__name__
            errors.append(f"{endpoint}: {detail}")
            print(f"Binance endpoint unavailable ({detail}): {endpoint}", file=sys.stderr)
    raise RuntimeError("All Binance endpoints failed: " + "; ".join(errors))


def validate_klines(rows, start_ms, end_ms, market):
    expected_count = (end_ms - start_ms) // MINUTE_MS
    if end_ms <= start_ms or (end_ms - start_ms) % MINUTE_MS:
        raise ValueError("Kline range must use exact one-minute boundaries")

    by_open = {}
    for row in rows:
        open_ms = int(row[0])
        if open_ms in by_open:
            raise ValueError(f"{market}: duplicate kline at {open_ms}")
        by_open[open_ms] = row

    expected_opens = list(range(start_ms, end_ms, MINUTE_MS))
    missing = [value for value in expected_opens if value not in by_open]
    unexpected = [value for value in by_open if value < start_ms or value >= end_ms]
    if missing or unexpected or len(rows) != expected_count:
        raise ValueError(
            f"{market}: expected {expected_count} contiguous closed 1m klines; "
            f"got {len(rows)}, missing={missing[:3]}, unexpected={unexpected[:3]}"
        )

    validated = []
    for open_ms in expected_opens:
        row = by_open[open_ms]
        close_ms = int(row[6])
        if close_ms != open_ms + MINUTE_MS - 1 or close_ms >= end_ms:
            raise ValueError(f"{market}: unclosed or misaligned kline at {open_ms}")
        quote_volume = Decimal(str(row[7]))
        taker_buy_quote = Decimal(str(row[10]))
        if quote_volume < 0 or taker_buy_quote < 0 or taker_buy_quote > quote_volume:
            raise ValueError(f"{market}: invalid quote-volume fields at {open_ms}")
        validated.append(
            {
                "open_ms": open_ms,
                "quote_volume": quote_volume,
                "taker_buy_quote": taker_buy_quote,
            }
        )
    return validated


def window_stats(rows, minutes, end_utc):
    selected = rows[-minutes:]
    if len(selected) != minutes:
        raise ValueError(f"Need {minutes} closed klines, got {len(selected)}")
    quote_volume = sum((row["quote_volume"] for row in selected), Decimal(0))
    taker_buy = sum((row["taker_buy_quote"] for row in selected), Decimal(0))
    if quote_volume <= 0:
        raise ValueError(f"Zero quote volume in {minutes}-minute window")
    taker_sell = quote_volume - taker_buy
    cvd = taker_buy - taker_sell
    buy_share = taker_buy / quote_volume
    normalized_cvd = cvd / quote_volume
    start_utc = end_utc - dt.timedelta(minutes=minutes)
    return {
        "start_utc": iso_utc(start_utc),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": minutes,
        "quote_volume_usdt": round(float(quote_volume), 8),
        "taker_buy_quote_volume_usdt": round(float(taker_buy), 8),
        "taker_sell_quote_volume_usdt": round(float(taker_sell), 8),
        "cvd_quote_usdt": round(float(cvd), 8),
        "taker_buy_share": round(float(buy_share), 10),
        "taker_buy_share_pct": round(float(buy_share * 100), 8),
        "normalized_cvd": round(float(normalized_cvd), 10),
    }


def compute(as_of_utc, symbol="BTCUSDT", fetcher=None):
    if as_of_utc.tzinfo is None:
        raise ValueError("as_of_utc must be timezone-aware")
    as_of_utc = as_of_utc.astimezone(UTC)
    if as_of_utc.minute or as_of_utc.second or as_of_utc.microsecond:
        raise ValueError("as_of_utc must be an exact top-of-hour boundary")

    start_utc = as_of_utc - dt.timedelta(hours=24)
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(as_of_utc.timestamp() * 1000)
    market_config = {
        "spot": {"label": "Binance Spot", "endpoints": SPOT_KLINES},
        "usd_m_futures": {
            "label": "Binance USD-M Futures perpetual",
            "endpoints": USD_M_FUTURES_KLINES,
        },
    }

    markets = {}
    for key, config in market_config.items():
        if fetcher is None:
            raw, source_endpoint = fetch_klines_with_failover(
                config["endpoints"], symbol, start_ms, end_ms
            )
        else:
            source_endpoint = config["endpoints"][0]
            raw = fetcher(source_endpoint, symbol, start_ms, end_ms)
        rows = validate_klines(raw, start_ms, end_ms, config["label"])
        markets[key] = {
            "market": config["label"],
            "source_endpoint": source_endpoint,
            "validated_closed_1m_klines": len(rows),
            "windows": {
                name: window_stats(rows, minutes, as_of_utc)
                for name, minutes in WINDOWS.items()
            },
        }

    return {
        "schema": "btc_binance_quote_cvd_v1",
        "generated_at_utc": iso_utc(dt.datetime.now(UTC)),
        "symbol": symbol,
        "quote_asset": "USDT",
        "interval": "1m",
        "as_of_utc": iso_utc(as_of_utc),
        "range_start_utc": iso_utc(start_utc),
        "window_semantics": "Half-open UTC intervals [start_utc, end_utc_exclusive) using only closed 1-minute klines; both markets share the same boundaries.",
        "definitions": {
            "cvd_quote_usdt": "sum(taker_buy_quote_volume - taker_sell_quote_volume), where taker_sell_quote_volume = quote_volume - taker_buy_quote_volume",
            "taker_buy_share": "sum(taker_buy_quote_volume) / sum(quote_volume), in [0, 1]",
            "normalized_cvd": "cvd_quote_usdt / quote_volume_usdt = 2 * taker_buy_share - 1, in [-1, 1]",
        },
        "integrity": {
            "status": "passed",
            "required_closed_1m_klines_per_market": 1440,
            "synchronized_utc_boundary": True,
        },
        "markets": markets,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--as-of",
        help="Exact top-of-hour UTC boundary, e.g. 2026-10-27T12:00:00Z; default is latest safe boundary",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--output", default="output/latest_cvd.json")
    args = parser.parse_args()

    as_of = parse_as_of(args.as_of) if args.as_of else closed_hour_boundary(dt.datetime.now(UTC))
    data = compute(as_of, args.symbol)
    destination = pathlib.Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(destination),
                "as_of_utc": data["as_of_utc"],
                "integrity": data["integrity"]["status"],
            }
        )
    )


if __name__ == "__main__":
    main()

