import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from calculate_cvd import (
    MINUTE_MS,
    UTC,
    closed_minute_boundary,
    collect_derivatives,
    compute,
    parse_as_of,
    validate_klines,
    window_stats,
    write_if_fresher,
)
from validate_outputs import validate_intraday


def kline(open_ms, quote="100", buy="60", close="80000"):
    return [
        open_ms,
        close,
        close,
        close,
        close,
        "1",
        open_ms + MINUTE_MS - 1,
        quote,
        1,
        "1",
        buy,
        "0",
    ]


def gate_perp_market(as_of, count=1_440):
    start = as_of - dt.timedelta(minutes=count)
    rows = [
        {"open_ms": index, "quote_volume": Decimal("100"), "taker_buy_quote": Decimal("55")}
        for index in range(count)
    ]
    windows = {}
    for name, minutes in {"1h": 60, "4h": 240, "24h": 1_440}.items():
        if count >= minutes:
            windows[name] = window_stats(rows, minutes, as_of)
        else:
            windows[name] = {
                "status": "unavailable",
                "start_utc": (as_of - dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
                "end_utc_exclusive": as_of.isoformat().replace("+00:00", "Z"),
                **{field: "N/A" for field in (
                    "closed_klines", "quote_volume_usdt", "taker_buy_quote_volume_usdt",
                    "taker_sell_quote_volume_usdt", "cvd_quote_usdt", "taker_buy_share",
                    "taker_buy_share_pct", "normalized_cvd",
                )},
                "error": "insufficient verified rolling coverage",
            }
    return {
        "status": "available" if count == 1_440 else "partial",
        "market": "Gate BTC_USDT USDT-settled perpetual",
        "exchange": "Gate",
        "provider": "Gate API v4",
        "source_endpoint": "https://api.gateio.ws/api/v4/futures/usdt/trades",
        "contract_metadata_endpoint": "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT",
        "source_timestamp_utc": (as_of - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "coverage": {
            "start_utc": start.isoformat().replace("+00:00", "Z"),
            "end_utc_exclusive": as_of.isoformat().replace("+00:00", "Z"),
            "expected_closed_1m_klines": 1_440,
            "validated_closed_1m_klines": count,
            "candle_completeness_pct": round(count / 1_440 * 100, 8),
        },
        "windows": windows,
        "fetch_integrity": {
            "status": "passed",
            "time_range_untruncated": True,
            "state_continuity_verified": True,
            "target_window_continuity": "passed" if count == 1_440 else "incomplete",
            "state_reset_reason": None,
            "stored_verified_minutes": count,
            "missing_verified_minutes": 1_440 - count,
            "historical_backfill": {"status": "not_needed" if count == 1_440 else "passed"},
        },
        "error": None,
    }


class CvdTest(unittest.TestCase):
    def test_closed_minute_boundary_allows_settlement(self):
        self.assertEqual(
            closed_minute_boundary(dt.datetime(2026, 10, 27, 12, 7, 45, tzinfo=UTC)),
            dt.datetime(2026, 10, 27, 12, 7, tzinfo=UTC),
        )
        self.assertEqual(
            closed_minute_boundary(dt.datetime(2026, 10, 27, 12, 7, 10, tzinfo=UTC)),
            dt.datetime(2026, 10, 27, 12, 6, tzinfo=UTC),
        )

    def test_parse_as_of_requires_minute_boundary_and_timezone(self):
        self.assertEqual(
            parse_as_of("2026-10-27T12:03:00Z"),
            dt.datetime(2026, 10, 27, 12, 3, tzinfo=UTC),
        )
        with self.assertRaisesRegex(ValueError, "one-minute"):
            parse_as_of("2026-10-27T12:03:01Z")
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_as_of("2026-10-27T12:03:00")

    def test_quote_cvd_and_normalized_buy_share(self):
        end = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        rows = [
            {"open_ms": index, "quote_volume": Decimal("100"), "taker_buy_quote": Decimal("60")}
            for index in range(60)
        ]
        result = window_stats(rows, 60, end)
        self.assertEqual(result["quote_volume_usdt"], 6000)
        self.assertEqual(result["cvd_quote_usdt"], 1200)
        self.assertEqual(result["taker_buy_share"], 0.6)
        self.assertEqual(result["normalized_cvd"], 0.2)

    def test_validation_rejects_missing_or_unclosed_minute(self):
        start = 0
        end = 3 * MINUTE_MS
        with self.assertRaisesRegex(ValueError, "contiguous"):
            validate_klines([kline(0), kline(2 * MINUTE_MS)], start, end, "test")
        bad = [kline(index * MINUTE_MS) for index in range(3)]
        bad[-1][6] += 1
        with self.assertRaisesRegex(ValueError, "unclosed or misaligned"):
            validate_klines(bad, start, end, "test")

    def test_compute_uses_identical_boundaries_and_exact_windows(self):
        as_of = dt.datetime(2026, 10, 27, 12, 3, tzinfo=UTC)
        generated = dt.datetime(2026, 10, 27, 12, 4, tzinfo=UTC)
        calls = []

        def fake_fetch(endpoint, symbol, start_ms, end_ms):
            calls.append((endpoint, symbol, start_ms, end_ms))
            return [kline(value) for value in range(start_ms, end_ms, MINUTE_MS)]

        result = compute(
            as_of,
            fetcher=fake_fetch,
            generated_at=generated,
            perp_market=gate_perp_market(as_of),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["schema"], "btc_multi_provider_intraday_v2")
        self.assertEqual(result["integrity"]["status"], "passed")
        self.assertTrue(result["integrity"]["cross_provider_values_not_merged"])
        self.assertEqual(result["freshness"]["source_age_seconds_at_generation"], 60)
        self.assertEqual(result["btc_spot_price"]["price_usdt"], 80000)
        for market in ("spot", "usd_m_futures"):
            windows = result["markets"][market]["windows"]
            self.assertEqual(windows["1h"]["closed_klines"], 60)
            self.assertEqual(windows["4h"]["closed_klines"], 240)
            self.assertEqual(windows["24h"]["closed_klines"], 1440)

    def test_dashboard_freshness_interpretation_accepts_up_to_four_hours(self):
        as_of = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

        def fake_fetch(endpoint, symbol, start_ms, end_ms):
            return [kline(value) for value in range(start_ms, end_ms, MINUTE_MS)]

        current = compute(
            as_of,
            fetcher=fake_fetch,
            generated_at=as_of + dt.timedelta(hours=4),
            perp_market=gate_perp_market(as_of),
        )
        stale = compute(
            as_of,
            fetcher=fake_fetch,
            generated_at=as_of + dt.timedelta(hours=4, seconds=1),
            perp_market=gate_perp_market(as_of),
        )
        self.assertEqual(current["freshness"]["max_source_age_seconds"], 14_400)
        self.assertEqual(current["freshness"]["source_age_seconds_at_generation"], 14_400)
        self.assertEqual(current["freshness"]["status_at_generation"], "fresh")
        self.assertEqual(stale["freshness"]["status_at_generation"], "stale")

    def test_inaccessible_futures_is_explicit_na_but_spot_validates(self):
        as_of = dt.datetime(2026, 10, 27, 12, 3, tzinfo=UTC)
        generated = dt.datetime(2026, 10, 27, 12, 4, tzinfo=UTC)

        def fake_fetch(endpoint, symbol, start_ms, end_ms):
            return [kline(value) for value in range(start_ms, end_ms, MINUTE_MS)]

        def inaccessible_perp(*args, **kwargs):
            raise ValueError("HTTP 451")

        result = compute(
            as_of,
            fetcher=fake_fetch,
            generated_at=generated,
            perp_collector=inaccessible_perp,
        )
        self.assertEqual(result["integrity"]["status"], "partial")
        self.assertEqual(result["markets"]["usd_m_futures"]["status"], "unavailable")
        self.assertEqual(
            result["markets"]["usd_m_futures"]["windows"]["24h"]["cvd_quote_usdt"],
            "N/A",
        )
        summary = validate_intraday(
            result,
            now=dt.datetime(2026, 10, 27, 12, 10, tzinfo=UTC),
        )
        self.assertEqual(summary["integrity"], "partial")

    def test_derivatives_are_available_only_with_valid_provider_payloads(self):
        generated = dt.datetime(2026, 10, 27, 12, 4, tzinfo=UTC)
        source_ms = int(dt.datetime(2026, 10, 27, 12, 3, tzinfo=UTC).timestamp() * 1000)

        def requester(url):
            if "premiumIndex" in url:
                return {
                    "time": source_ms,
                    "lastFundingRate": "0.0001",
                    "markPrice": "80010",
                    "indexPrice": "80000",
                    "nextFundingTime": source_ms + 8 * 60 * 60 * 1000,
                }
            return {"time": source_ms, "openInterest": "12345.67"}

        result = collect_derivatives(generated_at=generated, requester=requester)
        self.assertEqual(result["funding"]["status"], "available")
        self.assertEqual(result["funding"]["value"]["last_funding_rate_pct"], 0.01)
        self.assertEqual(result["open_interest"]["value"]["open_interest_btc"], 12345.67)

    def test_write_if_fresher_does_not_refresh_same_source_age(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as directory:
            output = pathlib.Path(directory) / "intraday.json"
            base = {
            "schema": "btc_multi_provider_intraday_v2",
                "as_of_utc": "2026-10-27T12:03:00Z",
                "generated_at_utc": "2026-10-27T12:04:00Z",
                "markets": {"spot": {"status": "available"}},
                "derivatives": {},
            }
            self.assertTrue(write_if_fresher(base, output))
            newer_generation = dict(base, generated_at_utc="2026-10-27T12:05:00Z")
            self.assertFalse(write_if_fresher(newer_generation, output))
            self.assertEqual(json.loads(output.read_text())["generated_at_utc"], base["generated_at_utc"])


if __name__ == "__main__":
    unittest.main()
