#!/usr/bin/env python3
"""Strict structural, integrity and source-age validation for published JSON."""

import argparse
import datetime as dt
import json
import pathlib
import re


UTC = dt.timezone.utc
WINDOWS = {"1h": 60, "4h": 240, "24h": 1_440}
NA_FIELDS = {
    "closed_klines",
    "quote_volume_usdt",
    "taker_buy_quote_volume_usdt",
    "taker_sell_quote_volume_usdt",
    "cvd_quote_usdt",
    "taker_buy_share",
    "taker_buy_share_pct",
    "normalized_cvd",
}


def parse_utc(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _assert(condition, message):
    if not condition:
        raise ValueError(message)


def _validate_freshness(data, now, max_age_seconds):
    block = data.get("freshness", {})
    source = parse_utc(block["source_as_of_utc"])
    generated = parse_utc(data["generated_at_utc"])
    _assert(block.get("generated_at_utc") == data["generated_at_utc"], "freshness generation mismatch")
    _assert(source <= generated + dt.timedelta(seconds=5), "source timestamp is after generation")
    declared = block.get("source_age_seconds_at_generation")
    actual_at_generation = max(0, int((generated - source).total_seconds()))
    _assert(abs(declared - actual_at_generation) <= 1, "declared source age is incorrect")
    age_now = max(0, int((now - source).total_seconds()))
    _assert(age_now <= max_age_seconds, f"source is stale: {age_now}s > {max_age_seconds}s")
    return {"source_as_of_utc": block["source_as_of_utc"], "source_age_seconds_now": age_now}


def validate_intraday(data, now=None, max_age_seconds=1_200):
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    _assert(data.get("schema") == "btc_binance_intraday_v1", "unexpected intraday schema")
    _assert(data.get("symbol") == "BTCUSDT", "unexpected symbol")
    _assert(data.get("interval") == "1m", "unexpected interval")
    freshness = _validate_freshness(data, now, max_age_seconds)
    _assert(data["as_of_utc"] == data["freshness"]["source_as_of_utc"], "as-of/freshness mismatch")
    _assert(data.get("integrity", {}).get("requested_synchronized_utc_boundary") is True, "boundary is not synchronized")

    markets = data.get("markets", {})
    _assert(set(markets) == {"spot", "usd_m_futures"}, "market set is incomplete")
    _assert(markets["spot"].get("status") == "available", "validated Spot market is required")
    for name, market in markets.items():
        _assert(bool(market.get("provider")), f"{name}: provider missing")
        _assert(set(market.get("windows", {})) == set(WINDOWS), f"{name}: window set mismatch")
        if market.get("status") == "available":
            coverage = market.get("coverage", {})
            _assert(coverage.get("validated_closed_1m_klines") == 1_440, f"{name}: incomplete candles")
            _assert(coverage.get("candle_completeness_pct") == 100.0, f"{name}: completeness mismatch")
            _assert(coverage.get("end_utc_exclusive") == data["as_of_utc"], f"{name}: boundary mismatch")
            for window, minutes in WINDOWS.items():
                values = market["windows"][window]
                _assert(values.get("status") == "available", f"{name}/{window}: unavailable")
                _assert(values.get("closed_klines") == minutes, f"{name}/{window}: candle count mismatch")
                _assert(values.get("end_utc_exclusive") == data["as_of_utc"], f"{name}/{window}: end mismatch")
                _assert(-1 <= values.get("normalized_cvd") <= 1, f"{name}/{window}: normalized CVD invalid")
                _assert(0 <= values.get("taker_buy_share") <= 1, f"{name}/{window}: buy share invalid")
        else:
            _assert(market.get("status") == "unavailable", f"{name}: invalid status")
            _assert(bool(market.get("error")), f"{name}: unavailable without error")
            for window in WINDOWS:
                values = market["windows"][window]
                _assert(values.get("status") == "unavailable", f"{name}/{window}: invalid unavailable status")
                _assert(all(values.get(field) == "N/A" for field in NA_FIELDS), f"{name}/{window}: N/A fields missing")

    price = data.get("btc_spot_price", {})
    _assert(price.get("status") == "available", "validated Spot price is required")
    _assert(isinstance(price.get("price_usdt"), (int, float)) and price["price_usdt"] > 0, "invalid Spot price")
    _assert(bool(price.get("provider")) and bool(price.get("source_timestamp_utc")), "Spot price metadata missing")

    derivatives = data.get("derivatives", {})
    _assert(set(derivatives) == {"funding", "open_interest"}, "derivatives set mismatch")
    for name, metric in derivatives.items():
        _assert(bool(metric.get("provider")) and bool(metric.get("coverage")), f"{name}: metadata missing")
        if metric.get("status") == "available":
            _assert(isinstance(metric.get("value"), dict), f"{name}: value missing")
            _assert(metric.get("source_timestamp_utc") != "N/A", f"{name}: timestamp missing")
        else:
            _assert(metric.get("status") == "unavailable", f"{name}: invalid status")
            _assert(metric.get("value") == "N/A" and bool(metric.get("error")), f"{name}: explicit N/A missing")
    return {"kind": "intraday", **freshness, "integrity": data["integrity"]["status"]}


def validate_archive_cvd(data, now=None, max_age_seconds=259_200):
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    _assert(data.get("schema") == "btc_binance_quote_cvd_v1", "unexpected archive CVD schema")
    _assert(data.get("provider") == "Binance Data Vision", "unexpected archive provider")
    _assert(data.get("integrity", {}).get("status") == "passed", "archive integrity failed")
    _assert(data["integrity"].get("synchronized_utc_boundary") is True, "archive boundary mismatch")
    _assert(data["integrity"].get("archive_sha256_verified") is True, "archive checksum not verified")
    freshness = _validate_freshness(data, now, max_age_seconds)
    _assert(data["as_of_utc"] == data["freshness"]["source_as_of_utc"], "archive as-of/freshness mismatch")
    for name in ("spot", "usd_m_futures"):
        market = data.get("markets", {}).get(name, {})
        _assert(market.get("validated_closed_1m_klines") == 1_440, f"{name}: incomplete archive")
        source = market.get("source_archive", {})
        _assert(re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")) is not None, f"{name}: invalid sha256")
        _assert(source.get("checksum_url", "").endswith(".CHECKSUM"), f"{name}: checksum URL missing")
    return {"kind": "archive-cvd", **freshness, "integrity": "passed"}


def validate_value_map(data, now=None, max_age_seconds=259_200):
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    _assert(data.get("schema") == "btc_lean_amt_value_map_v1", "unexpected value-map schema")
    _assert(data.get("integrity_cross_check", {}).get("status") == "passed", "daily integrity failed")
    _assert(data.get("previous_completed_week_trade_volume_profile", {}).get("integrity_status") == "passed", "weekly integrity failed")
    _assert(data.get("daily_poc_migration", {}).get("to_day_utc") == data.get("as_of_closed_day_utc"), "migration day mismatch")
    freshness = _validate_freshness(data, now, max_age_seconds)
    _assert(data.get("session_end_utc_exclusive") == data["freshness"]["source_as_of_utc"], "value-map as-of/freshness mismatch")
    source = data.get("sources", {})
    _assert(re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")) is not None, "value-map sha256 invalid")
    _assert(source.get("checksum_url", "").endswith(".CHECKSUM"), "value-map checksum URL missing")
    return {"kind": "value-map", **freshness, "integrity": "passed"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("intraday", "archive-cvd", "value-map"))
    parser.add_argument("path")
    parser.add_argument("--max-age-seconds", type=int)
    args = parser.parse_args()
    data = json.loads(pathlib.Path(args.path).read_text(encoding="utf-8"))
    validators = {
        "intraday": (validate_intraday, 1_200),
        "archive-cvd": (validate_archive_cvd, 259_200),
        "value-map": (validate_value_map, 259_200),
    }
    validator, default_age = validators[args.kind]
    result = validator(data, max_age_seconds=args.max_age_seconds or default_age)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
