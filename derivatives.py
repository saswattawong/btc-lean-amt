#!/usr/bin/env python3
"""Strict per-exchange BTC perpetual OI and funding evidence.

Only documented public exchange endpoints are used. Settled funding is kept
separate from the live or indicative rate for the next settlement. Open
interest is reported in BTC and USD; historical changes use only the same
exchange, instrument, and native history endpoint.
"""

import datetime as dt
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
FUNDING_INTERVAL_SECONDS = 8 * 60 * 60
DELTA_WINDOWS = {"1h": 60 * 60, "4h": 4 * 60 * 60, "24h": 24 * 60 * 60}
EXCHANGES = ("binance", "bybit", "okx", "gate")

# Binance documents only fapi.binance.com for USD-M Futures. Do not relabel
# api1-api4 (Spot) or an undocumented HTML/WAF mirror as a Futures fallback.
BINANCE_BASES = ("https://fapi.binance.com",)
BYBIT_BASES = ("https://api.bybit.com", "https://api.bytick.com")
OKX_BASES = ("https://www.okx.com",)
GATE_BASES = ("https://api.gateio.ws", "https://fx-api.gateio.ws")


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"URLError: {str(error.reason).replace(chr(10), ' ')[:160]}"
    message = str(error).replace("\n", " ").strip()[:240]
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
    filtered = {key: value for key, value in parameters.items() if value is not None}
    return f"{endpoint}?{urllib.parse.urlencode(filtered)}" if filtered else endpoint


def _timestamp_ms(value):
    return dt.datetime.fromtimestamp(int(value) / 1000, UTC)


def _timestamp_seconds(value):
    return dt.datetime.fromtimestamp(int(value), UTC)


def _request_paths(bases, path, parameters, requester):
    errors = []
    attempted = []
    for base in bases:
        url = _url(f"{base}{path}", **parameters)
        attempted.append(url)
        try:
            return requester(url), url, attempted
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            errors.append(f"{url}: {_safe_error(error)}")
    raise RuntimeError("all documented endpoints failed; " + "; ".join(errors))


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


def _prediction_unavailable(endpoint, error, attempted=None):
    return {
        "status": "unavailable",
        "classification": "predicted_not_settled",
        "source_endpoint": endpoint,
        "observed_at_utc": "N/A",
        "settles_at_utc": "N/A",
        "funding_interval_hours": "N/A",
        "predicted_funding_rate": "N/A",
        "predicted_funding_rate_pct": "N/A",
        "error": _safe_error(error) if isinstance(error, Exception) else str(error),
        "endpoints_attempted": list(attempted or (endpoint,)),
    }


def _prediction_available(endpoint, observed_at, settles_at, rate, interval_hours=8, **extra):
    if settles_at <= observed_at or interval_hours != 8:
        raise ValueError("predicted funding must target a future 8-hour settlement")
    rate = _signed_decimal(rate, "predicted funding rate")
    return {
        "status": "available",
        "classification": "predicted_not_settled",
        "source_endpoint": endpoint,
        "observed_at_utc": iso_utc(observed_at),
        "settles_at_utc": iso_utc(settles_at),
        "funding_interval_hours": 8,
        "predicted_funding_rate": float(rate),
        "predicted_funding_rate_pct": float(rate * 100),
        "error": None,
        **extra,
    }


def _funding_available(exchange, instrument, endpoint, settled_at, rate, generated_at, predicted, **extra):
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
        "predicted": predicted,
        "error": None,
        **extra,
    }


def _funding_unavailable(exchange, instrument, endpoint, error, predicted=None, attempted=None):
    return {
        "status": "unavailable",
        "exchange": exchange,
        "instrument": instrument,
        "source_endpoint": endpoint,
        "source_timestamp_utc": "N/A",
        "predicted": predicted or _prediction_unavailable(endpoint, "not collected"),
        "error": _safe_error(error) if isinstance(error, Exception) else str(error),
        "endpoints_attempted": list(attempted or (endpoint,)),
    }


def _unavailable_delta(reason, endpoint="N/A"):
    return {
        "status": "unavailable",
        "start_utc": "N/A",
        "end_utc": "N/A",
        "source_endpoint": endpoint,
        "open_interest_btc": "N/A",
        "open_interest_usd": "N/A",
        "error": reason,
    }


def _change_block(start, end, unit):
    change = end - start
    return {
        "status": "available",
        "unit": unit,
        "from": float(start),
        "to": float(end),
        "absolute_change": float(change),
        "percent_change": float(change / start * 100) if start else "N/A",
    }


def _history_deltas(samples, endpoint, anchor=None, tolerance_seconds=12 * 60):
    """Build strict changes from (time, BTC, USD-or-None) samples."""
    normalized = []
    for timestamp, btc, usd in samples:
        normalized.append((timestamp.astimezone(UTC), _decimal(btc, "OI BTC"),
                           None if usd is None else _decimal(usd, "OI USD")))
    normalized.sort(key=lambda item: item[0])
    if not normalized:
        return {name: _unavailable_delta("provider history returned no samples", endpoint)
                for name in DELTA_WINDOWS}
    if anchor:
        current = (
            anchor[0].astimezone(UTC),
            _decimal(anchor[1], "current OI BTC"),
            None if anchor[2] is None else _decimal(anchor[2], "current OI USD"),
        )
    else:
        current = normalized[-1]
    current_time, current_btc, current_usd = current
    result = {}
    for name, seconds in DELTA_WINDOWS.items():
        target = current_time - dt.timedelta(seconds=seconds)
        candidates = [item for item in normalized if item[0] < current_time]
        if not candidates:
            result[name] = _unavailable_delta("no earlier same-provider sample", endpoint)
            continue
        previous = min(candidates, key=lambda item: abs((item[0] - target).total_seconds()))
        drift = abs((previous[0] - target).total_seconds())
        if drift > tolerance_seconds:
            result[name] = _unavailable_delta(
                f"no same-provider sample within {tolerance_seconds}s of the {name} boundary", endpoint
            )
            continue
        previous_time, previous_btc, previous_usd = previous
        usd_change = (
            _change_block(previous_usd, current_usd, "USD")
            if previous_usd is not None and current_usd is not None
            else {"status": "unavailable", "unit": "USD", "error": "provider history has no same-instrument USD value"}
        )
        result[name] = {
            "status": "available",
            "start_utc": iso_utc(previous_time),
            "end_utc": iso_utc(current_time),
            "source_endpoint": endpoint,
            "boundary_drift_seconds": int(drift),
            "open_interest_btc": _change_block(previous_btc, current_btc, "BTC"),
            "open_interest_usd": usd_change,
            "error": None,
        }
    return result


def _oi_available(exchange, instrument, endpoint, timestamp, btc, usd, generated_at, deltas, **extra):
    return {
        "status": "available",
        "exchange": exchange,
        "instrument": instrument,
        "source_endpoint": endpoint,
        "source_timestamp_utc": iso_utc(timestamp),
        "open_interest_btc": float(_decimal(btc, "open interest BTC")),
        "open_interest_usd": float(_decimal(usd, "open interest USD")),
        "units": {"open_interest_btc": "BTC", "open_interest_usd": "USD"},
        "age_seconds_at_generation": max(0, int((generated_at - timestamp).total_seconds())),
        "historical_deltas": deltas,
        "error": None,
        **extra,
    }


def _oi_unavailable(exchange, instrument, endpoint, error, attempted=None):
    reason = _safe_error(error) if isinstance(error, Exception) else str(error)
    return {
        "status": "unavailable",
        "exchange": exchange,
        "instrument": instrument,
        "source_endpoint": endpoint,
        "source_timestamp_utc": "N/A",
        "historical_deltas": {name: _unavailable_delta(reason, endpoint) for name in DELTA_WINDOWS},
        "error": reason,
        "endpoints_attempted": list(attempted or (endpoint,)),
    }


def _binance(generated_at, requester):
    instrument = "BTCUSDT USD-M perpetual"
    premium_url = f"{BINANCE_BASES[0]}/fapi/v1/premiumIndex"
    try:
        premium, premium_url, attempted = _request_paths(
            BINANCE_BASES, "/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}, requester
        )
        if not isinstance(premium, dict) or premium.get("symbol") != "BTCUSDT":
            raise ValueError("unexpected Binance premium-index response")
        prediction = _prediction_available(
            premium_url, _timestamp_ms(premium["time"]), _timestamp_ms(premium["nextFundingTime"]),
            premium["lastFundingRate"], provider_field="lastFundingRate",
            provider_semantics="current rate associated with nextFundingTime; never treated as settled",
            endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        prediction = _prediction_unavailable(premium_url, error)

    funding_url = f"{BINANCE_BASES[0]}/fapi/v1/fundingRate"
    try:
        payload, funding_url, attempted = _request_paths(
            BINANCE_BASES, "/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 2}, requester
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected Binance funding response")
        settled_at, rate, record = _settled_record(
            payload, "fundingTime", "fundingRate", generated_at, _timestamp_ms
        )
        funding = _funding_available(
            "Binance", instrument, funding_url, settled_at, rate, generated_at, prediction,
            mark_price_usdt=float(_decimal(record["markPrice"], "markPrice"))
            if record.get("markPrice") not in {None, ""} else "N/A",
            endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _funding_unavailable("Binance", instrument, funding_url, error, prediction)

    oi_url = f"{BINANCE_BASES[0]}/futures/data/openInterestHist"
    try:
        records, oi_url, attempted = _request_paths(
            BINANCE_BASES, "/futures/data/openInterestHist",
            {"symbol": "BTCUSDT", "period": "5m", "limit": 300}, requester,
        )
        if not isinstance(records, list) or not records:
            raise ValueError("unexpected Binance OI history response")
        samples = [
            (_timestamp_ms(item["timestamp"]), item["sumOpenInterest"], item["sumOpenInterestValue"])
            for item in records if item.get("symbol") == "BTCUSDT"
        ]
        if not samples:
            raise ValueError("Binance OI history did not contain BTCUSDT")
        latest = max(samples, key=lambda item: item[0])
        open_interest = _oi_available(
            "Binance", instrument, oi_url, latest[0], latest[1], latest[2], generated_at,
            _history_deltas(samples, oi_url, anchor=latest),
            provider_fields={"btc": "sumOpenInterest", "usd": "sumOpenInterestValue"},
            history_interval="5m", endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _oi_unavailable("Binance", instrument, oi_url, error)
    return funding, open_interest


def _bybit(generated_at, requester):
    instrument = "BTCUSDT linear perpetual"
    ticker_url = f"{BYBIT_BASES[0]}/v5/market/tickers"
    ticker = None
    try:
        payload, ticker_url, attempted_ticker = _request_paths(
            BYBIT_BASES, "/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"}, requester
        )
        if payload.get("retCode") != 0 or not payload.get("result", {}).get("list"):
            raise ValueError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        ticker = payload["result"]["list"][0]
        ticker_observed = _timestamp_ms(payload.get("time", int(generated_at.timestamp() * 1000)))
        prediction = _prediction_available(
            ticker_url, ticker_observed, _timestamp_ms(ticker["nextFundingTime"]), ticker["fundingRate"],
            interval_hours=int(ticker.get("fundingIntervalHour", 8)), provider_field="fundingRate",
            provider_semantics="current rate associated with nextFundingTime; never treated as settled",
            endpoints_attempted=attempted_ticker,
        )
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        prediction = _prediction_unavailable(ticker_url, error)

    funding_url = f"{BYBIT_BASES[0]}/v5/market/funding/history"
    try:
        payload, funding_url, attempted = _request_paths(
            BYBIT_BASES, "/v5/market/funding/history",
            {"category": "linear", "symbol": "BTCUSDT", "limit": 2}, requester,
        )
        if payload.get("retCode") != 0:
            raise ValueError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        settled_at, rate, _ = _settled_record(
            payload["result"]["list"], "fundingRateTimestamp", "fundingRate", generated_at, _timestamp_ms
        )
        funding = _funding_available(
            "Bybit", instrument, funding_url, settled_at, rate, generated_at, prediction,
            endpoints_attempted=attempted,
        )
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _funding_unavailable("Bybit", instrument, funding_url, error, prediction)

    oi_url = f"{BYBIT_BASES[0]}/v5/market/open-interest"
    try:
        recent, oi_url, attempted = _request_paths(
            BYBIT_BASES, "/v5/market/open-interest",
            {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min", "limit": 200}, requester,
        )
        target_ms = int((generated_at - dt.timedelta(hours=24)).timestamp() * 1000)
        older, older_url, older_attempted = _request_paths(
            BYBIT_BASES, "/v5/market/open-interest",
            {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min",
             "startTime": target_ms - 10 * 60 * 1000, "endTime": target_ms + 10 * 60 * 1000, "limit": 10}, requester,
        )
        for item in (recent, older):
            if item.get("retCode") != 0:
                raise ValueError(f"Bybit error {item.get('retCode')}: {item.get('retMsg')}")
        if ticker is None:
            raise ValueError("Bybit ticker unavailable; cannot verify current OI in BTC and USD")
        observed = ticker_observed
        current_btc = _decimal(ticker["openInterest"], "openInterest")
        current_usd = _decimal(ticker["openInterestValue"], "openInterestValue")
        samples = []
        for item in (recent, older):
            for record in item.get("result", {}).get("list", []):
                samples.append((_timestamp_ms(record["timestamp"]), record["openInterest"], None))
        samples.append((observed, current_btc, current_usd))
        open_interest = _oi_available(
            "Bybit", instrument, ticker_url, observed, current_btc, current_usd, generated_at,
            _history_deltas(samples, [oi_url, older_url], anchor=(observed, current_btc, current_usd)),
            provider_fields={"btc": "openInterest", "usd": "openInterestValue"},
            history_source_endpoints=[oi_url, older_url], history_interval="5min",
            endpoints_attempted=attempted + older_attempted,
        )
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _oi_unavailable("Bybit", instrument, oi_url, error)
    return funding, open_interest


def _okx(generated_at, requester):
    instrument = "BTC-USDT-SWAP"
    prediction_url = f"{OKX_BASES[0]}/api/v5/public/funding-rate"
    try:
        payload, prediction_url, attempted_prediction = _request_paths(
            OKX_BASES, "/api/v5/public/funding-rate", {"instId": instrument}, requester
        )
        if payload.get("code") != "0" or not payload.get("data"):
            raise ValueError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        record = payload["data"][0]
        prediction = _prediction_available(
            prediction_url, _timestamp_ms(record["ts"]), _timestamp_ms(record["fundingTime"]),
            record["fundingRate"], provider_field="fundingRate",
            provider_semantics="OKX predicted upcoming settlement rate",
            endpoints_attempted=attempted_prediction,
        )
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        prediction = _prediction_unavailable(prediction_url, error)

    funding_url = f"{OKX_BASES[0]}/api/v5/public/funding-rate-history"
    try:
        payload, funding_url, attempted = _request_paths(
            OKX_BASES, "/api/v5/public/funding-rate-history", {"instId": instrument, "limit": 2}, requester
        )
        if payload.get("code") != "0":
            raise ValueError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        settled_at, rate, _ = _settled_record(
            payload["data"], "fundingTime", "realizedRate", generated_at, _timestamp_ms
        )
        funding = _funding_available(
            "OKX", instrument, funding_url, settled_at, rate, generated_at, prediction,
            provider_field="realizedRate", endpoints_attempted=attempted,
        )
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _funding_unavailable("OKX", instrument, funding_url, error, prediction)

    oi_url = f"{OKX_BASES[0]}/api/v5/public/open-interest"
    try:
        payload, oi_url, attempted = _request_paths(
            OKX_BASES, "/api/v5/public/open-interest", {"instType": "SWAP", "instId": instrument}, requester
        )
        if payload.get("code") != "0" or not payload.get("data"):
            raise ValueError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
        record = payload["data"][0]
        reason = "OKX public API has no same-instrument historical OI series; aggregate BTC history is not substituted"
        deltas = {name: _unavailable_delta(reason, oi_url) for name in DELTA_WINDOWS}
        open_interest = _oi_available(
            "OKX", instrument, oi_url, _timestamp_ms(record["ts"]), record["oiCcy"], record["oiUsd"],
            generated_at, deltas, provider_fields={"btc": "oiCcy", "usd": "oiUsd"},
            open_interest_contracts=float(_decimal(record["oi"], "oi")), endpoints_attempted=attempted,
        )
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _oi_unavailable("OKX", instrument, oi_url, error)
    return funding, open_interest


def _gate(generated_at, requester):
    instrument = "BTC_USDT USDT-settled perpetual"
    contract_url = f"{GATE_BASES[0]}/api/v4/futures/usdt/contracts/BTC_USDT"
    contract = None
    try:
        contract, contract_url, attempted_contract = _request_paths(
            GATE_BASES, "/api/v4/futures/usdt/contracts/BTC_USDT", {}, requester
        )
        if not isinstance(contract, dict) or contract.get("name") != "BTC_USDT":
            raise ValueError("unexpected Gate contract response")
        interval = int(contract["funding_interval"])
        prediction = _prediction_available(
            contract_url, generated_at, _timestamp_seconds(contract["funding_next_apply"]),
            contract["funding_rate_indicative"], interval_hours=interval // 3600,
            provider_field="funding_rate_indicative", provider_semantics="Gate indicative, not settled",
            endpoints_attempted=attempted_contract,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        prediction = _prediction_unavailable(contract_url, error)

    funding_url = f"{GATE_BASES[0]}/api/v4/futures/usdt/funding_rate"
    try:
        records, funding_url, attempted = _request_paths(
            GATE_BASES, "/api/v4/futures/usdt/funding_rate", {"contract": "BTC_USDT", "limit": 2}, requester
        )
        if not isinstance(records, list):
            raise ValueError("unexpected Gate funding response")
        settled_at, rate, _ = _settled_record(records, "t", "r", generated_at, _timestamp_seconds)
        funding = _funding_available(
            "Gate", instrument, funding_url, settled_at, rate, generated_at, prediction,
            endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        funding = _funding_unavailable("Gate", instrument, funding_url, error, prediction)

    stats_url = f"{GATE_BASES[0]}/api/v4/futures/usdt/contract_stats"
    try:
        if contract is None:
            raise ValueError("Gate contract metadata unavailable; cannot convert contracts to BTC")
        multiplier = _decimal(contract["quanto_multiplier"], "quanto_multiplier")
        # Contract.position_size is documented as current total *long* size;
        # ContractStat.open_interest is documented as total OI. Never splice
        # those different measurements into one historical series.
        current_long_contracts = _decimal(contract["position_size"], "position_size")
        stats, stats_url, attempted = _request_paths(
            GATE_BASES, "/api/v4/futures/usdt/contract_stats",
            {"contract": "BTC_USDT", "from": int((generated_at - dt.timedelta(hours=25)).timestamp()),
             "interval": "5m", "limit": 500}, requester,
        )
        if not isinstance(stats, list):
            raise ValueError("unexpected Gate contract-stats response")
        stat_samples = [
            (_timestamp_seconds(item["time"]),
             _decimal(item["open_interest"], "open_interest") * multiplier,
             item.get("open_interest_usd"),
             _decimal(item["open_interest"], "open_interest"))
            for item in stats
        ]
        if not stat_samples:
            raise ValueError("Gate contract stats returned no OI samples")
        latest = max(stat_samples, key=lambda item: item[0])
        samples = [item[:3] for item in stat_samples]
        open_interest = _oi_available(
            "Gate", instrument, stats_url, latest[0], latest[1], latest[2], generated_at,
            _history_deltas(samples, stats_url, anchor=latest[:3]),
            timestamp_semantics="Gate contract-stats sample time",
            provider_fields={"btc": "open_interest * quanto_multiplier", "usd": "open_interest_usd"},
            history_source_endpoint=stats_url, history_interval="5m",
            open_interest_contracts=float(latest[3]),
            quanto_multiplier_btc_per_contract=float(multiplier),
            contract_position_size_long_only=float(current_long_contracts),
            contract_position_size_semantics="current total long position; not used as total OI",
            endpoints_attempted=attempted,
        )
    except (KeyError, OSError, RuntimeError, ValueError, TimeoutError) as error:
        open_interest = _oi_unavailable("Gate", instrument, stats_url, error)
    return funding, open_interest


COLLECTORS = {"binance": _binance, "bybit": _bybit, "okx": _okx, "gate": _gate}


def _metric(kind, values):
    available = [item for item in values.values() if item["status"] == "available"]
    coverage = (
        "settled 8-hour funding plus separately labelled predicted/current rates by exchange"
        if kind == "funding"
        else "BTC and USD OI plus same-provider 1h/4h/24h deltas by exchange"
    )
    if not available:
        return {
            "status": "unavailable", "provider": "Official public exchange REST APIs",
            "coverage": coverage, "source_endpoint": "N/A", "source_timestamp_utc": "N/A",
            "value": "N/A", "error": "all exchange settled/current metrics unavailable",
            "by_exchange": values,
        }
    latest = max(item["source_timestamp_utc"] for item in available)
    return {
        "status": "available", "provider": "Official public exchange REST APIs",
        "coverage": coverage, "source_endpoint": [item["source_endpoint"] for item in available],
        "source_timestamp_utc": latest,
        "value": {
            "measurement": "settled and predicted funding kept separate" if kind == "funding"
            else "open interest in BTC and USD with native historical deltas",
            "units": {"funding": "rate"} if kind == "funding" else {"btc": "BTC", "usd": "USD"},
            "aggregation": "none", "by_exchange": values,
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
        futures = {name: executor.submit(COLLECTORS[name], generated_at, requester) for name in EXCHANGES}
        collected = {name: futures[name].result() for name in EXCHANGES}
    funding = {name: collected[name][0] for name in EXCHANGES}
    open_interest = {name: collected[name][1] for name in EXCHANGES}
    return {"funding": _metric("funding", funding), "open_interest": _metric("open_interest", open_interest)}


def unavailable_derivatives(reason="not collected"):
    funding = {
        name: _funding_unavailable(name.title(), "BTC perpetual", "N/A", reason)
        for name in EXCHANGES
    }
    open_interest = {
        name: _oi_unavailable(name.title(), "BTC perpetual", "N/A", reason)
        for name in EXCHANGES
    }
    return {"funding": _metric("funding", funding), "open_interest": _metric("open_interest", open_interest)}
