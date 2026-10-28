#!/usr/bin/env python3
"""Validated intraday BTCUSDT market snapshot from public Binance APIs.

The output intentionally tolerates provider-specific access failures. A market
is published only after its full trailing 24 hours of synchronized, closed 1m
klines validate; unavailable markets and point-in-time derivatives metrics are
explicitly represented as N/A instead of failing the usable Spot snapshot.
"""

import argparse
import datetime as dt
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
MINUTE_MS = 60_000
WINDOWS = {"1h": 60, "4h": 240, "24h": 1_440}
MAX_SOURCE_AGE_SECONDS = 20 * 60
SPOT_KLINE_ENDPOINTS = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
)
USD_M_FUTURES_KLINE_ENDPOINTS = (
    "https://fapi.binance.com/fapi/v1/klines",
    "https://fapi1.binance.com/fapi/v1/klines",
    "https://fapi2.binance.com/fapi/v1/klines",
)
FUNDING_ENDPOINTS = (
    "https://fapi.binance.com/fapi/v1/premiumIndex",
    "https://fapi1.binance.com/fapi/v1/premiumIndex",
)
OPEN_INTEREST_ENDPOINTS = (
    "https://fapi.binance.com/fapi/v1/openInterest",
    "https://fapi1.binance.com/fapi/v1/openInterest",
)


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC timezone")
    return parsed.astimezone(UTC)


def closed_minute_boundary(now, settle_seconds=30):
    """Return an exclusive boundary after allowing the latest candle to settle."""
    safe = now.astimezone(UTC) - dt.timedelta(seconds=settle_seconds)
    return safe.replace(second=0, microsecond=0)


def parse_as_of(value):
    parsed = parse_utc(value)
    if parsed.second or parsed.microsecond:
        raise ValueError("--as-of must be an exact one-minute UTC boundary")
    return parsed


def _safe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        reason = str(error.reason).replace("\n", " ")[:160]
        return f"URLError: {reason}"
    message = str(error).replace("\n", " ").strip()[:200]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def request_json(url, attempts=2, timeout=20):
    request = urllib.request.Request(url, headers={"User-Agent": "btc-lean-amt-intraday/2.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable")


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
            raise ValueError("unexpected kline response")
        if not page:
            break
        last_open = None
        for row in page:
            if not isinstance(row, list) or len(row) < 11:
                raise ValueError("malformed kline response")
            open_ms = int(row[0])
            last_open = open_ms
            if start_ms <= open_ms < end_ms:
                rows.append(row)
        if last_open is None or last_open < cursor:
            raise ValueError("non-advancing kline page")
        cursor = last_open + MINUTE_MS
    return rows


def fetch_with_failover(endpoints, symbol, start_ms, end_ms, fetcher=fetch_klines):
    errors = []
    for endpoint in endpoints:
        try:
            return fetcher(endpoint, symbol, start_ms, end_ms), endpoint, errors
        except (OSError, ValueError, TimeoutError) as error:
            errors.append(f"{endpoint}: {_safe_error(error)}")
    raise RuntimeError("all endpoints failed; " + "; ".join(errors))


def _decimal(value, field, market, timestamp):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{market}: invalid {field} at {timestamp}") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{market}: invalid {field} at {timestamp}")
    return number


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
        quote_volume = _decimal(row[7], "quote volume", market, open_ms)
        taker_buy_quote = _decimal(row[10], "taker-buy quote volume", market, open_ms)
        close_price = _decimal(row[4], "close price", market, open_ms)
        if close_price <= 0 or taker_buy_quote > quote_volume:
            raise ValueError(f"{market}: invalid price/volume fields at {open_ms}")
        validated.append(
            {
                "open_ms": open_ms,
                "close_ms": close_ms,
                "close_price": close_price,
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
    return {
        "status": "available",
        "start_utc": iso_utc(end_utc - dt.timedelta(minutes=minutes)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": minutes,
        "quote_volume_usdt": round(float(quote_volume), 8),
        "taker_buy_quote_volume_usdt": round(float(taker_buy), 8),
        "taker_sell_quote_volume_usdt": round(float(taker_sell), 8),
        "cvd_quote_usdt": round(float(cvd), 8),
        "taker_buy_share": round(float(buy_share), 10),
        "taker_buy_share_pct": round(float(buy_share * 100), 8),
        "normalized_cvd": round(float(cvd / quote_volume), 10),
        "error": None,
    }


def unavailable_window(minutes, end_utc, reason):
    return {
        "status": "unavailable",
        "start_utc": iso_utc(end_utc - dt.timedelta(minutes=minutes)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": "N/A",
        "quote_volume_usdt": "N/A",
        "taker_buy_quote_volume_usdt": "N/A",
        "taker_sell_quote_volume_usdt": "N/A",
        "cvd_quote_usdt": "N/A",
        "taker_buy_share": "N/A",
        "taker_buy_share_pct": "N/A",
        "normalized_cvd": "N/A",
        "error": reason,
    }


def unavailable_market(label, provider, start_utc, end_utc, reason, endpoints):
    return {
        "status": "unavailable",
        "market": label,
        "provider": provider,
        "source_endpoint": "N/A",
        "endpoints_attempted": list(endpoints),
        "source_timestamp_utc": "N/A",
        "coverage": {
            "start_utc": iso_utc(start_utc),
            "end_utc_exclusive": iso_utc(end_utc),
            "expected_closed_1m_klines": 1_440,
            "validated_closed_1m_klines": "N/A",
            "candle_completeness_pct": "N/A",
        },
        "windows": {
            name: unavailable_window(minutes, end_utc, reason)
            for name, minutes in WINDOWS.items()
        },
        "error": reason,
    }


def collect_market(label, provider, endpoints, symbol, start_utc, end_utc, fetcher=fetch_klines):
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    try:
        raw, endpoint, failover_errors = fetch_with_failover(
            endpoints, symbol, start_ms, end_ms, fetcher=fetcher
        )
        rows = validate_klines(raw, start_ms, end_ms, label)
        first_open = dt.datetime.fromtimestamp(rows[0]["open_ms"] / 1000, UTC)
        last_close = dt.datetime.fromtimestamp(rows[-1]["close_ms"] / 1000, UTC)
        return {
            "status": "available",
            "market": label,
            "provider": provider,
            "source_endpoint": endpoint,
            "endpoints_attempted": list(endpoints),
            "source_timestamp_utc": iso_utc(last_close),
            "coverage": {
                "start_utc": iso_utc(start_utc),
                "end_utc_exclusive": iso_utc(end_utc),
                "first_candle_open_utc": iso_utc(first_open),
                "last_candle_close_utc": iso_utc(last_close),
                "expected_closed_1m_klines": 1_440,
                "validated_closed_1m_klines": len(rows),
                "candle_completeness_pct": 100.0,
            },
            "windows": {
                name: window_stats(rows, minutes, end_utc)
                for name, minutes in WINDOWS.items()
            },
            "last_closed_price_usdt": round(float(rows[-1]["close_price"]), 8),
            "failover_errors": failover_errors,
            "error": None,
        }
    except (OSError, RuntimeError, ValueError, TimeoutError) as error:
        return unavailable_market(
            label, provider, start_utc, end_utc, _safe_error(error), endpoints
        )


def _point_unavailable(provider, coverage, reason, endpoints):
    return {
        "status": "unavailable",
        "provider": provider,
        "coverage": coverage,
        "source_endpoint": "N/A",
        "source_timestamp_utc": "N/A",
        "value": "N/A",
        "error": reason,
        "endpoints_attempted": list(endpoints),
    }


def _request_point_with_failover(endpoints, symbol, requester=request_json):
    errors = []
    for endpoint in endpoints:
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}{urllib.parse.urlencode({'symbol': symbol})}"
        try:
            payload = requester(url)
            if not isinstance(payload, dict):
                raise ValueError("unexpected object response")
            return payload, endpoint, errors
        except (OSError, ValueError, TimeoutError) as error:
            errors.append(f"{endpoint}: {_safe_error(error)}")
    raise RuntimeError("all endpoints failed; " + "; ".join(errors))


def collect_derivatives(symbol="BTCUSDT", generated_at=None, requester=request_json):
    generated_at = generated_at or dt.datetime.now(UTC)
    provider = "Binance USD-M Futures REST API"
    result = {}

    try:
        payload, endpoint, failover_errors = _request_point_with_failover(
            FUNDING_ENDPOINTS, symbol, requester
        )
        timestamp = dt.datetime.fromtimestamp(int(payload["time"]) / 1000, UTC)
        rate = Decimal(str(payload["lastFundingRate"]))
        result["funding"] = {
            "status": "available",
            "provider": provider,
            "coverage": "point-in-time current perpetual funding snapshot",
            "source_endpoint": endpoint,
            "source_timestamp_utc": iso_utc(timestamp),
            "age_seconds_at_generation": max(0, int((generated_at - timestamp).total_seconds())),
            "value": {
                "last_funding_rate": float(rate),
                "last_funding_rate_pct": float(rate * 100),
                "mark_price_usdt": float(Decimal(str(payload["markPrice"]))),
                "index_price_usdt": float(Decimal(str(payload["indexPrice"]))),
                "next_funding_time_utc": iso_utc(
                    dt.datetime.fromtimestamp(int(payload["nextFundingTime"]) / 1000, UTC)
                ),
            },
            "failover_errors": failover_errors,
            "error": None,
        }
    except (KeyError, InvalidOperation, OSError, RuntimeError, ValueError, TimeoutError) as error:
        result["funding"] = _point_unavailable(
            provider,
            "point-in-time current perpetual funding snapshot",
            _safe_error(error),
            FUNDING_ENDPOINTS,
        )

    try:
        payload, endpoint, failover_errors = _request_point_with_failover(
            OPEN_INTEREST_ENDPOINTS, symbol, requester
        )
        timestamp = dt.datetime.fromtimestamp(int(payload["time"]) / 1000, UTC)
        result["open_interest"] = {
            "status": "available",
            "provider": provider,
            "coverage": "point-in-time BTCUSDT perpetual open-interest snapshot",
            "source_endpoint": endpoint,
            "source_timestamp_utc": iso_utc(timestamp),
            "age_seconds_at_generation": max(0, int((generated_at - timestamp).total_seconds())),
            "value": {
                "open_interest_btc": float(Decimal(str(payload["openInterest"])))
            },
            "failover_errors": failover_errors,
            "error": None,
        }
    except (KeyError, InvalidOperation, OSError, RuntimeError, ValueError, TimeoutError) as error:
        result["open_interest"] = _point_unavailable(
            provider,
            "point-in-time BTCUSDT perpetual open-interest snapshot",
            _safe_error(error),
            OPEN_INTEREST_ENDPOINTS,
        )
    return result


def compute(as_of_utc, symbol="BTCUSDT", fetcher=fetch_klines, generated_at=None, derivatives=None):
    if as_of_utc.tzinfo is None:
        raise ValueError("as_of_utc must be timezone-aware")
    as_of_utc = as_of_utc.astimezone(UTC)
    if as_of_utc.second or as_of_utc.microsecond:
        raise ValueError("as_of_utc must be an exact one-minute boundary")
    generated_at = (generated_at or dt.datetime.now(UTC)).astimezone(UTC)
    start_utc = as_of_utc - dt.timedelta(hours=24)

    markets = {
        "spot": collect_market(
            "Binance Spot",
            "Binance Spot Market Data API",
            SPOT_KLINE_ENDPOINTS,
            symbol,
            start_utc,
            as_of_utc,
            fetcher,
        ),
        "usd_m_futures": collect_market(
            "Binance USD-M Futures perpetual",
            "Binance USD-M Futures REST API",
            USD_M_FUTURES_KLINE_ENDPOINTS,
            symbol,
            start_utc,
            as_of_utc,
            fetcher,
        ),
    }
    available = [key for key, market in markets.items() if market["status"] == "available"]
    source_age = max(0, int((generated_at - as_of_utc).total_seconds()))
    spot = markets["spot"]
    if spot["status"] == "available":
        spot_price = {
            "status": "available",
            "provider": spot["provider"],
            "coverage": "close of the latest validated closed BTCUSDT 1m Spot candle",
            "source_endpoint": spot["source_endpoint"],
            "source_timestamp_utc": spot["source_timestamp_utc"],
            "price_usdt": spot["last_closed_price_usdt"],
            "error": None,
        }
    else:
        spot_price = {
            "status": "unavailable",
            "provider": "Binance Spot Market Data API",
            "coverage": "close of the latest validated closed BTCUSDT 1m Spot candle",
            "source_endpoint": "N/A",
            "source_timestamp_utc": "N/A",
            "price_usdt": "N/A",
            "error": spot["error"],
        }

    return {
        "schema": "btc_binance_intraday_v1",
        "generated_at_utc": iso_utc(generated_at),
        "symbol": symbol,
        "quote_asset": "USDT",
        "interval": "1m",
        "as_of_utc": iso_utc(as_of_utc),
        "range_start_utc": iso_utc(start_utc),
        "freshness": {
            "source_as_of_utc": iso_utc(as_of_utc),
            "generated_at_utc": iso_utc(generated_at),
            "source_age_seconds_at_generation": source_age,
            "max_source_age_seconds": MAX_SOURCE_AGE_SECONDS,
            "status_at_generation": "fresh" if source_age <= MAX_SOURCE_AGE_SECONDS else "stale",
            "note": "Freshness is measured from the last closed source candle, never from file generation time.",
        },
        "window_semantics": "Trailing half-open UTC intervals [start_utc, end_utc_exclusive) using only validated closed 1m klines; every available market is requested on the same boundary.",
        "definitions": {
            "cvd_quote_usdt": "sum(taker_buy_quote_volume - taker_sell_quote_volume), where taker_sell_quote_volume = quote_volume - taker_buy_quote_volume",
            "taker_buy_share": "sum(taker_buy_quote_volume) / sum(quote_volume), in [0, 1]",
            "normalized_cvd": "cvd_quote_usdt / quote_volume_usdt = 2 * taker_buy_share - 1, in [-1, 1]",
        },
        "integrity": {
            "status": "passed" if len(available) == 2 else "partial" if available else "unavailable",
            "requested_synchronized_utc_boundary": True,
            "required_closed_1m_klines_per_available_market": 1_440,
            "available_markets": available,
            "unavailable_markets": [key for key in markets if key not in available],
        },
        "markets": markets,
        "btc_spot_price": spot_price,
        "derivatives": derivatives or {
            "funding": _point_unavailable(
                "Binance USD-M Futures REST API",
                "point-in-time current perpetual funding snapshot",
                "not collected",
                FUNDING_ENDPOINTS,
            ),
            "open_interest": _point_unavailable(
                "Binance USD-M Futures REST API",
                "point-in-time BTCUSDT perpetual open-interest snapshot",
                "not collected",
                OPEN_INTEREST_ENDPOINTS,
            ),
        },
    }


def _quality(data):
    markets = sum(
        market.get("status") == "available"
        for market in data.get("markets", {}).values()
    )
    points = sum(
        item.get("status") == "available"
        for item in data.get("derivatives", {}).values()
    )
    return markets * 10 + points


def write_if_fresher(data, destination):
    if destination.exists():
        try:
            current = json.loads(destination.read_text(encoding="utf-8"))
            if current.get("schema") == data["schema"]:
                current_as_of = parse_utc(current["as_of_utc"])
                new_as_of = parse_utc(data["as_of_utc"])
                if current_as_of > new_as_of:
                    return False
                if current_as_of == new_as_of and _quality(current) >= _quality(data):
                    return False
        except (KeyError, OSError, TypeError, ValueError):
            pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--as-of",
        help="Exact closed-minute UTC boundary, e.g. 2026-10-27T12:00:00Z; default is latest safe boundary",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--output", default="output/latest_intraday.json")
    args = parser.parse_args()

    as_of = parse_as_of(args.as_of) if args.as_of else closed_minute_boundary(dt.datetime.now(UTC))
    generated_at = dt.datetime.now(UTC)
    derivatives = collect_derivatives(args.symbol, generated_at)
    data = compute(as_of, args.symbol, generated_at=generated_at, derivatives=derivatives)
    destination = pathlib.Path(args.output)
    changed = write_if_fresher(data, destination)
    print(
        json.dumps(
            {
                "output": str(destination),
                "as_of_utc": data["as_of_utc"],
                "freshness": data["freshness"]["status_at_generation"],
                "integrity": data["integrity"]["status"],
                "available_markets": data["integrity"]["available_markets"],
                "changed": changed,
            }
        )
    )


if __name__ == "__main__":
    main()
