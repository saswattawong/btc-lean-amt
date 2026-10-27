#!/usr/bin/env python3
"""Hourly Binance BTCUSDT Spot and futures CVD sourced from CoinGlass.

CoinGlass publishes hourly taker-buy and taker-sell USD volume for each
exchange/pair. This job validates 24 synchronized, closed UTC hours per market
and derives rolling 1h, 4h and 24h CVD without exposing the API key in output.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
HOUR_MS = 3_600_000
PROVIDER = "CoinGlass"
PROVIDER_BASE_URL = "https://open-api-v4.coinglass.com"
SPOT_ENDPOINT = f"{PROVIDER_BASE_URL}/api/spot/taker-buy-sell-volume/history"
FUTURES_ENDPOINT = f"{PROVIDER_BASE_URL}/api/futures/v2/taker-buy-sell-volume/history"
WINDOWS = {"1h": 1, "4h": 4, "24h": 24}


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def closed_hour_boundary(now):
    """Return the latest safe, closed top-of-hour UTC boundary."""
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


def request_json(url, api_key, attempts=3):
    if not api_key:
        raise ValueError("COINGLASS_API_KEY is required")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "CG-API-KEY": api_key,
            "User-Agent": "btc-lean-amt-cvd/2.0",
        },
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)


def fetch_hourly(endpoint, symbol, start_ms, end_ms, api_key, requester=request_json):
    """Fetch CoinGlass hourly values for the half-open interval [start, end)."""
    params = urllib.parse.urlencode(
        {
            "exchange": "Binance",
            "symbol": symbol,
            "interval": "h1",
            "limit": 24,
            "start_time": start_ms,
            "end_time": end_ms - 1,
        }
    )
    payload = requester(f"{endpoint}?{params}", api_key)
    code = payload.get("code") if isinstance(payload, dict) else None
    message = payload.get("msg") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or str(code) != "0":
        raise ValueError(f"Unexpected CoinGlass response: code={code}, msg={message}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("CoinGlass response data must be a list")
    return rows


def _decimal(value, field, market, timestamp):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{market}: invalid {field} at {timestamp}") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{market}: invalid {field} at {timestamp}")
    return number


def validate_hourly(rows, start_ms, end_ms, market):
    if end_ms <= start_ms or (end_ms - start_ms) % HOUR_MS:
        raise ValueError("Range must use exact one-hour boundaries")
    expected_times = list(range(start_ms, end_ms, HOUR_MS))
    by_time = {}

    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError(f"{market}: malformed CoinGlass row")
        try:
            timestamp = int(raw.get("time"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{market}: invalid timestamp") from error
        if timestamp < 1_000_000_000_000:
            timestamp *= 1000
        if timestamp in by_time:
            raise ValueError(f"{market}: duplicate interval at {timestamp}")
        if start_ms <= timestamp < end_ms:
            by_time[timestamp] = raw

    missing = [value for value in expected_times if value not in by_time]
    if missing or len(by_time) != len(expected_times):
        raise ValueError(
            f"{market}: expected {len(expected_times)} contiguous closed 1h intervals; "
            f"got {len(by_time)}, missing={missing[:3]}"
        )

    validated = []
    for timestamp in expected_times:
        raw = by_time[timestamp]
        buy = _decimal(raw.get("taker_buy_volume_usd"), "taker buy volume", market, timestamp)
        sell = _decimal(raw.get("taker_sell_volume_usd"), "taker sell volume", market, timestamp)
        if buy + sell <= 0:
            raise ValueError(f"{market}: zero total volume at {timestamp}")
        validated.append({"open_ms": timestamp, "taker_buy_usd": buy, "taker_sell_usd": sell})
    return validated


def window_stats(rows, hours, end_utc):
    selected = rows[-hours:]
    if len(selected) != hours:
        raise ValueError(f"Need {hours} closed hourly intervals, got {len(selected)}")
    taker_buy = sum((row["taker_buy_usd"] for row in selected), Decimal(0))
    taker_sell = sum((row["taker_sell_usd"] for row in selected), Decimal(0))
    total = taker_buy + taker_sell
    if total <= 0:
        raise ValueError(f"Zero total volume in {hours}-hour window")
    cvd = taker_buy - taker_sell
    buy_share = taker_buy / total
    normalized_cvd = cvd / total
    return {
        "start_utc": iso_utc(end_utc - dt.timedelta(hours=hours)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_1h_intervals": hours,
        "total_volume_usd": round(float(total), 8),
        "taker_buy_volume_usd": round(float(taker_buy), 8),
        "taker_sell_volume_usd": round(float(taker_sell), 8),
        "cvd_usd": round(float(cvd), 8),
        "taker_buy_share": round(float(buy_share), 10),
        "taker_buy_share_pct": round(float(buy_share * 100), 8),
        "normalized_cvd": round(float(normalized_cvd), 10),
    }


def compute(as_of_utc, api_key, symbol="BTCUSDT", fetcher=fetch_hourly):
    if as_of_utc.tzinfo is None:
        raise ValueError("as_of_utc must be timezone-aware")
    as_of_utc = as_of_utc.astimezone(UTC)
    if as_of_utc.minute or as_of_utc.second or as_of_utc.microsecond:
        raise ValueError("as_of_utc must be an exact top-of-hour boundary")

    start_utc = as_of_utc - dt.timedelta(hours=24)
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(as_of_utc.timestamp() * 1000)
    market_config = {
        "spot": {"label": "Binance Spot", "endpoint": SPOT_ENDPOINT},
        "usd_m_futures": {"label": "Binance USD-M Futures perpetual", "endpoint": FUTURES_ENDPOINT},
    }

    markets = {}
    for key, config in market_config.items():
        raw = fetcher(config["endpoint"], symbol, start_ms, end_ms, api_key)
        rows = validate_hourly(raw, start_ms, end_ms, config["label"])
        markets[key] = {
            "market": config["label"],
            "provider": PROVIDER,
            "source_endpoint": config["endpoint"],
            "validated_closed_1h_intervals": len(rows),
            "windows": {name: window_stats(rows, hours, as_of_utc) for name, hours in WINDOWS.items()},
        }

    return {
        "schema": "btc_coinglass_usd_cvd_v1",
        "generated_at_utc": iso_utc(dt.datetime.now(UTC)),
        "provider": PROVIDER,
        "source_exchange": "Binance",
        "symbol": symbol,
        "unit": "USD",
        "source_interval": "1h",
        "as_of_utc": iso_utc(as_of_utc),
        "range_start_utc": iso_utc(start_utc),
        "window_semantics": "Half-open UTC intervals [start_utc, end_utc_exclusive) using CoinGlass hourly taker-buy and taker-sell USD volume for Binance; both markets share the same boundaries.",
        "definitions": {
            "cvd_usd": "sum(taker_buy_volume_usd - taker_sell_volume_usd)",
            "taker_buy_share": "sum(taker_buy_volume_usd) / sum(total_volume_usd), in [0, 1]",
            "normalized_cvd": "cvd_usd / total_volume_usd = 2 * taker_buy_share - 1, in [-1, 1]",
        },
        "integrity": {
            "status": "passed",
            "required_closed_1h_intervals_per_market": 24,
            "synchronized_utc_boundary": True,
            "provider_data_not_binance_raw_klines": True,
        },
        "markets": markets,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", help="Exact top-of-hour UTC boundary, e.g. 2026-10-27T12:00:00Z")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--output", default="output/latest_cvd.json")
    args = parser.parse_args()

    api_key = os.environ.get("COINGLASS_API_KEY")
    if not api_key:
        raise SystemExit("COINGLASS_API_KEY is required")
    as_of = parse_as_of(args.as_of) if args.as_of else closed_hour_boundary(dt.datetime.now(UTC))
    data = compute(as_of, api_key, args.symbol)
    destination = pathlib.Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(destination), "as_of_utc": data["as_of_utc"], "integrity": data["integrity"]["status"]}))


if __name__ == "__main__":
    main()
