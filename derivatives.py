#!/usr/bin/env python3
"""Per-exchange BTC perpetual open interest and settled 8-hour funding.

Only official public exchange endpoints are used.  Values remain separated by
exchange and an unavailable provider is explicit; no cross-exchange total or
substitute value is produced.
"""

import datetime as dt
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
FUNDING_INTERVAL_SECONDS = 8 * 60 * 60
EXCHANGES = ("binance", "bybit", "okx", "gate")

BINANCE_FUNDING_ENDPOINTS = (
    "https://fapi.binance.com/fapi/v1/fundingRate",
    "https://fapi1.binance.com/fapi/v1/fundingRate",
)
BINANCE_OI_ENDPOINTS = (
    "https://fapi.binance.com/fapi/v1/openInterest",
    "https://fapi1.binance.com/fapi/v1/openInterest",
)
BYBIT_FUNDING_ENDPOINT = "https://api.bybit.com/v5/market/funding/history"
BYBIT_OI_ENDPOINT = "https://api.bybit.com/v5/market/open-interest"
OKX_FUNDING_ENDPOINT = "https://www.okx.com/api/v5/public/funding-rate-history"
OKX_OI_ENDPOINT = "https://www.okx.com/api/v5/public/open-interest"
GATE_FUNDING_ENDPOINT = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
GATE_CONTRACT_ENDPOINT = "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT"


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"URLError: {str(error.reason).replace(chr(10), ' ')[:160]}"
    message = str(error).replace("\n", " ").strip()[:200]
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid {field}")
    return number


def _signed_decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if not number.is_finite():
        raise ValueError(f"invalid {field}")
    return number


def _url(endpoint, **parameters):
    return f"{endpoint}?{urllib.parse.urlencode(parameters)}"


def _timestamp_ms(value):
    return dt.datetime.fromtimestamp(int(value) / 1000, UTC)


def _timestamp_seconds(value):
    return dt.datetime.fromtimestamp(int(value), UTC)


def _unavailable(exchange, instrument, endpoint, error, attempted=None):
    return {
        "status": "unavailable",
        "exchange": exchange,
        "instrument": instrument,
        "source_endpoint": endpoint,
        "source_timestamp_utc": "N/A",
        "error": _safe_error(error) if isinstance(error, Exception) else str(error),
        "endpoints_attempted": list(attempted or (endpoint,)),
    }


def _request_failover(endpoints, parameters, requester):
    errors = []
    attempted = []
    for endpoint in endpoints:
        url = _url(endpoint, **parameters)
        attempted.append(url)
        try:
            return requester(url), url, attempted
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            errors.append(f"{endpoint}: {_safe_error(error)}")
    raise RuntimeError("all endpoints failed; " + "; ".join(errors))


def _settled_record(records, timestamp_key, rate_key, generated_at, timestamp_parser):
    parsed = []
    for record in records:
        timestamp = timestamp_parser(record[timestamp_key])
        if timestamp <= generated_at + dt.timedelta(seconds=5):
            parsed.append((timestamp, record))
    parsed.sort(key=lambda item: item[0])
    if len(parsed) < 2:
        raise ValueError("need two completed funding records to verify the 8-hour interval")
    previous, latest = parsed[-2], parsed[-1]
    interval = (latest[0] - previous[0]).total_seconds()
    if abs(interval - FUNDING_INTERVAL_SECONDS) > 5:
        raise ValueError(f"latest settled funding interval is {interval}s, not 28800s")
    rate = _signed_decimal(latest[1][rate_key], rate_key)
    return latest[0], rate, latest[1]


def _funding_available(exchange, instrument, endpoint, settled_at, rate, generated_at, **extra):
    return {
        "status": "available",
        "exchange": exchange,
        "instrument": instrument,
        "source_endpoint": endpoint,
        "source_timestamp_utc": iso_utc(settled_at),
        "settled_at_utc": iso_utc(settled_at),
        "funding_interval_hours": 8,
        "settled_funding_rate": float(rate),
        "settled_funding_rate_pct": float(rate * 100),
        "age_seconds_at_generation": max(0, int((generated_at - settled_at).total_seconds())),
        "error": None,
        **extra,
    }


def _oi_available(exchange, instrument, endpoint, timestamp, value, generated_at, **extra):
    return {
        "status": "available",
        "exchange": exchange,
        "instrument": instrument,
        "source_endpoint": endpoint,
        "source_timestamp_utc": iso_utc(timestamp),
        "open_interest_btc": float(value),
        "unit": "BTC",
        "age_seconds_at_generation": max(0, int((generated_at - timestamp).total_seconds())),
        "error": None,
        **extra,
    }


def _binance(generated_at, requester):
    instrument = "BTCUSDT USD-M perpetual"
    try:
        payload, endpoint, attempted = _request_failover(
            BINANCE_FUNDING_ENDPOINTS,
            {"symbol": "BTCUSDT", "limit": 2},
            requester,
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected Binance funding response")
        settled_at, rate, record = _settled_record(
            payload, "fundingTime", "fundingRate", generated_at, _timestamp_ms
        )
        funding = _funding_available(
            "Binance", instrument, endpoint, settled_at, rate, generated_at,
            mark_price_usdt=float(_decimal(record["markPrice"], "markPrice"))
            if record.get("markPrice") not in {None, ""} else "N/A",
            endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _unavailable(
            "Binance", instrument, BINANCE_FUNDING_ENDPOINTS[0], error,
            [_url(item, symbol="BTCUSDT", limit=2) for item in BINANCE_FUNDING_ENDPOINTS],
        )

    try:
        payload, endpoint, attempted = _request_failover(
            BINANCE_OI_ENDPOINTS, {"symbol": "BTCUSDT"}, requester
        )
        if not isinstance(payload, dict) or payload.get("symbol") != "BTCUSDT":
            raise ValueError("unexpected Binance open-interest response")
        timestamp = _timestamp_ms(payload["time"])
        value = _decimal(payload["openInterest"], "openInterest")
        open_interest = _oi_available(
            "Binance", instrument, endpoint, timestamp, value, generated_at,
            provider_field="openInterest",
            unit_evidence="BTCUSDT USD-M base-asset quantity",
            endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _unavailable(
            "Binance", instrument, BINANCE_OI_ENDPOINTS[0], error,
            [_url(item, symbol="BTCUSDT") for item in BINANCE_OI_ENDPOINTS],
        )
    return funding, open_interest


def _bybit(generated_at, requester):
    instrument = "BTCUSDT linear perpetual"
    funding_url = _url(
        BYBIT_FUNDING_ENDPOINT, category="linear", symbol="BTCUSDT", limit=2
    )
    try:
        payload = requester(funding_url)
        if payload.get("retCode") != 0:
            raise ValueError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        records = payload["result"]["list"]
        settled_at, rate, _ = _settled_record(
            records, "fundingRateTimestamp", "fundingRate", generated_at, _timestamp_ms
        )
        funding = _funding_available(
            "Bybit", instrument, funding_url, settled_at, rate, generated_at
        )
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _unavailable("Bybit", instrument, funding_url, error)

    oi_url = _url(
        BYBIT_OI_ENDPOINT,
        category="linear",
        symbol="BTCUSDT",
        intervalTime="5min",
        limit=1,
    )
    try:
        payload = requester(oi_url)
        if payload.get("retCode") != 0:
            raise ValueError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        record = payload["result"]["list"][0]
        timestamp = _timestamp_ms(record["timestamp"])
        value = _decimal(record["openInterest"], "openInterest")
        open_interest = _oi_available(
            "Bybit", instrument, oi_url, timestamp, value, generated_at,
            provider_field="openInterest",
            unit_evidence="Bybit documents BTCUSDT linear open interest in BTC",
            observation_interval="5min",
        )
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _unavailable("Bybit", instrument, oi_url, error)
    return funding, open_interest


def _okx(generated_at, requester):
    instrument = "BTC-USDT-SWAP"
    funding_url = _url(OKX_FUNDING_ENDPOINT, instId=instrument, limit=2)
    try:
        payload = requester(funding_url)
        if payload.get("code") != "0":
            raise ValueError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        records = payload["data"]
        settled_at, rate, _ = _settled_record(
            records, "fundingTime", "realizedRate", generated_at, _timestamp_ms
        )
        funding = _funding_available(
            "OKX", instrument, funding_url, settled_at, rate, generated_at,
            provider_field="realizedRate",
        )
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _unavailable("OKX", instrument, funding_url, error)

    oi_url = _url(OKX_OI_ENDPOINT, instType="SWAP", instId=instrument)
    try:
        payload = requester(oi_url)
        if payload.get("code") != "0":
            raise ValueError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        record = payload["data"][0]
        timestamp = _timestamp_ms(record["ts"])
        value = _decimal(record["oiCcy"], "oiCcy")
        open_interest = _oi_available(
            "OKX", instrument, oi_url, timestamp, value, generated_at,
            provider_field="oiCcy",
            unit_evidence="OKX oiCcy is open interest in coin units",
            open_interest_contracts=float(_decimal(record["oi"], "oi")),
            open_interest_usd=float(_decimal(record["oiUsd"], "oiUsd")),
        )
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _unavailable("OKX", instrument, oi_url, error)
    return funding, open_interest


def _gate(generated_at, requester):
    instrument = "BTC_USDT USDT-settled perpetual"
    funding_url = _url(GATE_FUNDING_ENDPOINT, contract="BTC_USDT", limit=2)
    try:
        records = requester(funding_url)
        if not isinstance(records, list):
            raise ValueError("unexpected Gate funding response")
        settled_at, rate, _ = _settled_record(
            records, "t", "r", generated_at, _timestamp_seconds
        )
        funding = _funding_available(
            "Gate", instrument, funding_url, settled_at, rate, generated_at
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _unavailable("Gate", instrument, funding_url, error)

    try:
        payload = requester(GATE_CONTRACT_ENDPOINT)
        if not isinstance(payload, dict) or payload.get("name") != "BTC_USDT":
            raise ValueError("unexpected Gate contract response")
        contracts = _decimal(payload["position_size"], "position_size")
        multiplier = _decimal(payload["quanto_multiplier"], "quanto_multiplier")
        value = contracts * multiplier
        open_interest = _oi_available(
            "Gate", instrument, GATE_CONTRACT_ENDPOINT, generated_at, value, generated_at,
            timestamp_semantics="collector observation time; Gate contract response has no data timestamp",
            provider_field="position_size * quanto_multiplier",
            unit_evidence="Gate quanto_multiplier is BTC represented by one contract",
            open_interest_contracts=float(contracts),
            quanto_multiplier_btc_per_contract=float(multiplier),
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _unavailable("Gate", instrument, GATE_CONTRACT_ENDPOINT, error)
    return funding, open_interest


COLLECTORS = {
    "binance": _binance,
    "bybit": _bybit,
    "okx": _okx,
    "gate": _gate,
}


def _metric(kind, values):
    available = [item for item in values.values() if item["status"] == "available"]
    if not available:
        return {
            "status": "unavailable",
            "provider": "Official public exchange REST APIs",
            "coverage": "per-exchange BTC perpetual metrics; no cross-exchange aggregation",
            "source_endpoint": "N/A",
            "source_timestamp_utc": "N/A",
            "value": "N/A",
            "error": "all exchange endpoints unavailable",
            "by_exchange": values,
        }
    latest = max(item["source_timestamp_utc"] for item in available)
    return {
        "status": "available",
        "provider": "Official public exchange REST APIs",
        "coverage": "per-exchange BTC perpetual metrics; no cross-exchange aggregation",
        "source_endpoint": [item["source_endpoint"] for item in available],
        "source_timestamp_utc": latest,
        "value": {
            "measurement": (
                "latest settled 8-hour funding rate" if kind == "funding"
                else "open interest in BTC units"
            ),
            "unit": "rate" if kind == "funding" else "BTC",
            "aggregation": "none",
            "by_exchange": values,
        },
        "available_exchanges": [name for name, item in values.items() if item["status"] == "available"],
        "unavailable_exchanges": [name for name, item in values.items() if item["status"] == "unavailable"],
        "error": None,
    }


def collect_derivatives(symbol="BTCUSDT", generated_at=None, requester=None):
    if symbol != "BTCUSDT":
        raise ValueError("multi-exchange derivatives collector currently supports BTCUSDT only")
    if requester is None:
        raise ValueError("requester is required")
    generated_at = (generated_at or dt.datetime.now(UTC)).astimezone(UTC)
    with ThreadPoolExecutor(max_workers=len(EXCHANGES)) as executor:
        futures = {
            name: executor.submit(COLLECTORS[name], generated_at, requester)
            for name in EXCHANGES
        }
        collected = {name: futures[name].result() for name in EXCHANGES}
    funding = {name: collected[name][0] for name in EXCHANGES}
    open_interest = {name: collected[name][1] for name in EXCHANGES}
    return {
        "funding": _metric("funding", funding),
        "open_interest": _metric("open_interest", open_interest),
    }


def unavailable_derivatives(reason="not collected"):
    values = {
        name: _unavailable(name.title(), "BTC perpetual", "N/A", reason, ())
        for name in EXCHANGES
    }
    return {
        "funding": {
            "status": "unavailable",
            "provider": "Official public exchange REST APIs",
            "coverage": "latest settled 8-hour funding rate by exchange",
            "source_endpoint": "N/A",
            "source_timestamp_utc": "N/A",
            "value": "N/A",
            "error": reason,
            "by_exchange": values,
        },
        "open_interest": {
            "status": "unavailable",
            "provider": "Official public exchange REST APIs",
            "coverage": "open interest in BTC units by exchange",
            "source_endpoint": "N/A",
            "source_timestamp_utc": "N/A",
            "value": "N/A",
            "error": reason,
            "by_exchange": values,
        },
    }
