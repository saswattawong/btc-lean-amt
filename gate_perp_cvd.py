#!/usr/bin/env python3
"""Rolling Gate BTC_USDT perpetual CVD from public aggressor-side trades.

Gate's public futures trade feed identifies taker direction by the sign of
``size``.  Quote notional is reconstructed from the independently fetched
contract multiplier, trade size, and trade price.  The rolling state contains
only verified, closed one-minute aggregates and is never combined with Binance
Spot observations.
"""

import datetime as dt
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
MINUTE = dt.timedelta(minutes=1)
WINDOWS = {"1h": 60, "4h": 240, "24h": 1_440}
STATE_SCHEMA = "btc_gate_perp_cvd_state_v1"
PROVIDER = "Gate API v4"
MARKET = "Gate BTC_USDT USDT-settled perpetual"
CONTRACT = "BTC_USDT"
CONTRACT_ENDPOINT = "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT"
TRADES_ENDPOINT = "https://api.gateio.ws/api/v4/futures/usdt/trades"
PROVIDER_DOCUMENTATION = "https://www.gate.com/docs/developers/apiv4/en/futures/"
TRADE_PAGE_LIMIT = 1_000


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if not number.is_finite():
        raise ValueError(f"invalid {field}")
    return number


def _safe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"URLError: {str(error.reason).replace(chr(10), ' ')[:160]}"
    text = str(error).replace("\n", " ").strip()[:200]
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def request_json(url, attempts=3, timeout=20):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "btc-lean-amt-gate-perp/1.0"},
    )
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


def fetch_contract(requester=request_json):
    payload = requester(CONTRACT_ENDPOINT)
    if not isinstance(payload, dict):
        raise ValueError("unexpected Gate contract response")
    multiplier = _decimal(payload.get("quanto_multiplier"), "contract multiplier")
    if (
        payload.get("name") != CONTRACT
        or payload.get("type") != "direct"
        or payload.get("status") != "trading"
        or payload.get("in_delisting") is not False
        or multiplier <= 0
    ):
        raise ValueError("Gate contract metadata did not validate as an active direct perpetual")
    return {
        "name": CONTRACT,
        "type": "direct",
        "settlement_asset": "USDT",
        "underlying_asset": "BTC",
        "quanto_multiplier_btc_per_contract": str(multiplier),
        "status": payload["status"],
        "in_delisting": False,
    }


def _trade_timestamp_seconds(row):
    value = row.get("create_time_ms", row.get("create_time"))
    timestamp = _decimal(value, "trade timestamp")
    # Gate documents create_time_ms as a decimal Unix-seconds value.  Tolerate
    # millisecond epoch values defensively without guessing for other scales.
    if timestamp >= Decimal("1000000000000"):
        timestamp /= Decimal(1_000)
    if timestamp < Decimal("1000000000"):
        raise ValueError("trade timestamp is outside the supported Unix range")
    return timestamp


def _validate_trade(row):
    if not isinstance(row, dict) or row.get("contract") != CONTRACT:
        raise ValueError("unexpected Gate trade record")
    trade_id = int(row["id"])
    timestamp = _trade_timestamp_seconds(row)
    size = _decimal(row.get("size"), "trade size")
    price = _decimal(row.get("price"), "trade price")
    if size == 0 or price <= 0:
        raise ValueError("Gate trade size/price is invalid")
    return {
        "id": trade_id,
        "timestamp_seconds": timestamp,
        "size": size,
        "price": price,
    }


def fetch_trades_range(
    start_utc,
    end_utc,
    requester=request_json,
    page_limit=TRADE_PAGE_LIMIT,
    max_requests=160,
):
    """Fetch a provably untruncated half-open range by recursively time-slicing.

    Gate truncates a range when more than ``limit`` records match.  A segment is
    accepted only when fewer than ``limit`` rows are returned; full pages are
    split until the response is untruncated or the safety limits are reached.
    """
    if start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc:
        raise ValueError("trade range must be a positive timezone-aware interval")
    start_second = int(start_utc.timestamp())
    end_second = int(end_utc.timestamp())
    if start_utc.second or start_utc.microsecond or end_utc.second or end_utc.microsecond:
        raise ValueError("trade range must use exact second boundaries")

    pending = [(start_second, end_second)]
    accepted_segments = []
    trades = {}
    requests = 0
    duplicates = 0
    while pending:
        left, right = pending.pop()
        requests += 1
        if requests > max_requests:
            raise RuntimeError(f"Gate trade pagination exceeded {max_requests} requests")
        query = urllib.parse.urlencode(
            {"contract": CONTRACT, "from": left, "to": right, "limit": page_limit}
        )
        page = requester(f"{TRADES_ENDPOINT}?{query}")
        if not isinstance(page, list):
            raise ValueError("unexpected Gate trades response")
        if len(page) >= page_limit:
            if right - left <= 1:
                raise RuntimeError("Gate trade range exceeds the page limit within one second")
            midpoint = left + (right - left) // 2
            pending.extend(((midpoint, right), (left, midpoint)))
            continue

        accepted_segments.append((left, right))
        for raw in page:
            trade = _validate_trade(raw)
            timestamp = trade["timestamp_seconds"]
            if timestamp < start_second or timestamp >= end_second:
                continue
            if trade["id"] in trades:
                duplicates += 1
            trades[trade["id"]] = trade

    ordered = sorted(trades.values(), key=lambda item: (item["timestamp_seconds"], item["id"]))
    return ordered, {
        "request_count": requests,
        "verified_untruncated_segments": len(accepted_segments),
        "unique_trades": len(ordered),
        "duplicate_trade_ids_filtered": duplicates,
        "page_limit": page_limit,
        "range_start_utc": iso_utc(start_utc),
        "range_end_utc_exclusive": iso_utc(end_utc),
    }


def aggregate_minutes(trades, start_utc, end_utc, multiplier):
    buckets = {}
    cursor = start_utc
    while cursor < end_utc:
        buckets[iso_utc(cursor)] = {
            "open_utc": iso_utc(cursor),
            "quote_volume_usdt": Decimal(0),
            "taker_buy_quote_volume_usdt": Decimal(0),
            "trade_count": 0,
        }
        cursor += MINUTE

    start_second = Decimal(str(start_utc.timestamp()))
    end_second = Decimal(str(end_utc.timestamp()))
    for trade in trades:
        timestamp = trade["timestamp_seconds"]
        if timestamp < start_second or timestamp >= end_second:
            raise ValueError("trade fell outside the aggregation range")
        minute_second = int(timestamp // 60) * 60
        key = iso_utc(dt.datetime.fromtimestamp(minute_second, UTC))
        bucket = buckets.get(key)
        if bucket is None:
            raise ValueError("trade minute was not initialized")
        quote_notional = abs(trade["size"]) * multiplier * trade["price"]
        bucket["quote_volume_usdt"] += quote_notional
        if trade["size"] > 0:
            bucket["taker_buy_quote_volume_usdt"] += quote_notional
        bucket["trade_count"] += 1
    return buckets


def _serialized_bucket(bucket):
    return {
        "open_utc": bucket["open_utc"],
        "quote_volume_usdt": str(bucket["quote_volume_usdt"]),
        "taker_buy_quote_volume_usdt": str(bucket["taker_buy_quote_volume_usdt"]),
        "trade_count": int(bucket["trade_count"]),
    }


def load_state(path, multiplier):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data.get("schema") != STATE_SCHEMA
        or data.get("provider") != PROVIDER
        or data.get("contract") != CONTRACT
        or _decimal(data.get("contract_multiplier_btc"), "state multiplier") != multiplier
    ):
        raise ValueError("existing Gate CVD state provenance does not match the active contract")
    buckets = {}
    for raw in data.get("closed_minute_buckets", []):
        opened = parse_utc(raw["open_utc"])
        if opened.second or opened.microsecond:
            raise ValueError("state bucket is not minute-aligned")
        quote = _decimal(raw["quote_volume_usdt"], "state quote volume")
        buy = _decimal(raw["taker_buy_quote_volume_usdt"], "state taker-buy volume")
        count = int(raw["trade_count"])
        if quote < 0 or buy < 0 or buy > quote or count < 0:
            raise ValueError("state bucket values are invalid")
        key = iso_utc(opened)
        if key in buckets:
            raise ValueError("state contains a duplicate minute")
        buckets[key] = {
            "open_utc": key,
            "quote_volume_usdt": quote,
            "taker_buy_quote_volume_usdt": buy,
            "trade_count": count,
        }
    return buckets


def trailing_buckets(buckets, as_of_utc, limit=1_440):
    result = []
    cursor = as_of_utc - MINUTE
    while len(result) < limit:
        bucket = buckets.get(iso_utc(cursor))
        if bucket is None:
            break
        result.append(bucket)
        cursor -= MINUTE
    result.reverse()
    return result


def _unavailable_window(minutes, end_utc, reason):
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


def _window_stats(rows, minutes, end_utc):
    if len(rows) < minutes:
        return _unavailable_window(
            minutes,
            end_utc,
            f"insufficient verified rolling coverage: {len(rows)}/{minutes} closed minutes",
        )
    selected = rows[-minutes:]
    quote = sum((row["quote_volume_usdt"] for row in selected), Decimal(0))
    buy = sum((row["taker_buy_quote_volume_usdt"] for row in selected), Decimal(0))
    if quote <= 0:
        return _unavailable_window(minutes, end_utc, "verified window has zero quote volume")
    sell = quote - buy
    cvd = buy - sell
    share = buy / quote
    return {
        "status": "available",
        "start_utc": iso_utc(end_utc - dt.timedelta(minutes=minutes)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": minutes,
        "quote_volume_usdt": round(float(quote), 8),
        "taker_buy_quote_volume_usdt": round(float(buy), 8),
        "taker_sell_quote_volume_usdt": round(float(sell), 8),
        "cvd_quote_usdt": round(float(cvd), 8),
        "taker_buy_share": round(float(share), 10),
        "taker_buy_share_pct": round(float(share * 100), 8),
        "normalized_cvd": round(float(cvd / quote), 10),
        "error": None,
    }


def collect_gate_perpetual(
    as_of_utc,
    state_path,
    bootstrap_minutes=60,
    overlap_minutes=10,
    requester=request_json,
    max_requests=160,
):
    if as_of_utc.tzinfo is None or as_of_utc.second or as_of_utc.microsecond:
        raise ValueError("as_of_utc must be an exact timezone-aware minute boundary")
    as_of_utc = as_of_utc.astimezone(UTC)
    state_path = pathlib.Path(state_path)
    contract = fetch_contract(requester)
    multiplier = _decimal(contract["quanto_multiplier_btc_per_contract"], "contract multiplier")
    buckets = load_state(state_path, multiplier)
    desired_start = as_of_utc - dt.timedelta(minutes=1_440)

    if buckets:
        latest_open = max(parse_utc(key) for key in buckets)
        fetch_start = max(desired_start, latest_open - dt.timedelta(minutes=overlap_minutes - 1))
    else:
        fetch_start = max(desired_start, as_of_utc - dt.timedelta(minutes=bootstrap_minutes))

    trades, fetch_evidence = fetch_trades_range(
        fetch_start,
        as_of_utc,
        requester=requester,
        max_requests=max_requests,
    )
    replacement = aggregate_minutes(trades, fetch_start, as_of_utc, multiplier)
    retained = {
        key: value
        for key, value in buckets.items()
        if desired_start <= parse_utc(key) < fetch_start
    }
    retained.update(replacement)
    rows = trailing_buckets(retained, as_of_utc)
    if not rows:
        raise ValueError("Gate returned no verifiable closed-minute coverage")

    latest_trade = max((trade["timestamp_seconds"] for trade in trades), default=None)
    if latest_trade is None:
        raise ValueError("Gate returned no trades in the refreshed range")
    latest_trade_utc = dt.datetime.fromtimestamp(float(latest_trade), UTC)
    if latest_trade_utc >= as_of_utc:
        raise ValueError("Gate source trade is not inside the requested closed range")
    if latest_trade_utc < as_of_utc - dt.timedelta(minutes=2):
        raise ValueError("Gate latest trade is too old for the requested intraday boundary")

    serialized = [_serialized_bucket(row) for row in rows]
    state = {
        "schema": STATE_SCHEMA,
        "provider": PROVIDER,
        "provider_documentation": PROVIDER_DOCUMENTATION,
        "contract": CONTRACT,
        "contract_multiplier_btc": str(multiplier),
        "updated_at_utc": iso_utc(dt.datetime.now(UTC)),
        "coverage_start_utc": serialized[0]["open_utc"],
        "coverage_end_utc_exclusive": iso_utc(as_of_utc),
        "validated_closed_1m_buckets": len(serialized),
        "closed_minute_buckets": serialized,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)

    windows = {
        name: _window_stats(rows, minutes, as_of_utc)
        for name, minutes in WINDOWS.items()
    }
    fully_available = len(rows) == 1_440 and all(
        window["status"] == "available" for window in windows.values()
    )
    return {
        "status": "available" if fully_available else "partial",
        "market": MARKET,
        "exchange": "Gate",
        "provider": PROVIDER,
        "provider_documentation": PROVIDER_DOCUMENTATION,
        "source_endpoint": TRADES_ENDPOINT,
        "contract_metadata_endpoint": CONTRACT_ENDPOINT,
        "endpoints_attempted": [CONTRACT_ENDPOINT, TRADES_ENDPOINT],
        "source_timestamp_utc": iso_utc(latest_trade_utc),
        "contract": contract,
        "coverage": {
            "start_utc": rows[0]["open_utc"],
            "end_utc_exclusive": iso_utc(as_of_utc),
            "expected_closed_1m_klines": 1_440,
            "validated_closed_1m_klines": len(rows),
            "candle_completeness_pct": round(len(rows) / 1_440 * 100, 8),
        },
        "aggressor_side_rule": "positive trade size = taker buy; negative trade size = taker sell",
        "quote_notional_formula": "abs(size_contracts) * quanto_multiplier_btc_per_contract * price_usdt",
        "windows": windows,
        "fetch_integrity": {
            "status": "passed",
            "state_schema": STATE_SCHEMA,
            "time_range_untruncated": True,
            **fetch_evidence,
        },
        "error": None,
    }


__all__ = [
    "CONTRACT",
    "CONTRACT_ENDPOINT",
    "MARKET",
    "PROVIDER",
    "PROVIDER_DOCUMENTATION",
    "STATE_SCHEMA",
    "TRADES_ENDPOINT",
    "aggregate_minutes",
    "collect_gate_perpetual",
    "fetch_contract",
    "fetch_trades_range",
    "load_state",
    "trailing_buckets",
]
