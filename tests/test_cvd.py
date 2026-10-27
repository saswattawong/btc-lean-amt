import datetime as dt
import pathlib
import sys
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from calculate_cvd import (
    MINUTE_MS,
    UTC,
    closed_hour_boundary,
    compute,
    fetch_klines_with_failover,
    parse_as_of,
    validate_klines,
    window_stats,
)


def kline(open_ms, quote="100", buy="60"):
    return [
        open_ms,
        "1",
        "1",
        "1",
        "1",
        "1",
        open_ms + MINUTE_MS - 1,
        quote,
        1,
        "1",
        buy,
        "0",
    ]


class CvdTest(unittest.TestCase):
    def test_closed_hour_boundary(self):
        self.assertEqual(
            closed_hour_boundary(dt.datetime(2026, 10, 27, 12, 7, tzinfo=UTC)),
            dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            closed_hour_boundary(dt.datetime(2026, 10, 27, 12, 1, tzinfo=UTC)),
            dt.datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
        )

    def test_parse_as_of_requires_top_of_hour_and_timezone(self):
        self.assertEqual(
            parse_as_of("2026-10-27T12:00:00Z"),
            dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        )
        with self.assertRaisesRegex(ValueError, "top-of-hour"):
            parse_as_of("2026-10-27T12:01:00Z")
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_as_of("2026-10-27T12:00:00")

    def test_quote_cvd_and_normalized_buy_share(self):
        end = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        rows = [
            {"open_ms": index, "quote_volume": Decimal("100"), "taker_buy_quote": Decimal("60")}
            for index in range(60)
        ]
        result = window_stats(rows, 60, end)
        self.assertEqual(result["quote_volume_usdt"], 6000)
        self.assertEqual(result["taker_buy_quote_volume_usdt"], 3600)
        self.assertEqual(result["taker_sell_quote_volume_usdt"], 2400)
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
        as_of = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        calls = []

        def fake_fetch(endpoint, symbol, start_ms, end_ms):
            calls.append((endpoint, symbol, start_ms, end_ms))
            return [kline(value) for value in range(start_ms, end_ms, MINUTE_MS)]

        result = compute(as_of, fetcher=fake_fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2:], calls[1][2:])
        self.assertEqual(result["integrity"]["status"], "passed")
        for market in ("spot", "usd_m_futures"):
            windows = result["markets"][market]["windows"]
            self.assertEqual(windows["1h"]["closed_klines"], 60)
            self.assertEqual(windows["4h"]["closed_klines"], 240)
            self.assertEqual(windows["24h"]["closed_klines"], 1440)
            self.assertEqual(windows["1h"]["cvd_quote_usdt"], 1200)
            self.assertEqual(windows["4h"]["cvd_quote_usdt"], 4800)
            self.assertEqual(windows["24h"]["cvd_quote_usdt"], 28800)

    def test_endpoint_failover_records_the_working_source(self):
        endpoints = ("https://first.invalid/klines", "https://second.test/klines")
        calls = []

        def fake_fetch(endpoint, symbol, start_ms, end_ms):
            calls.append(endpoint)
            if endpoint == endpoints[0]:
                raise TimeoutError("unavailable")
            return [kline(start_ms)]

        rows, source = fetch_klines_with_failover(
            endpoints, "BTCUSDT", 0, MINUTE_MS, fetcher=fake_fetch
        )
        self.assertEqual(calls, list(endpoints))
        self.assertEqual(source, endpoints[1])
        self.assertEqual(rows, [kline(0)])

    def test_compute_uses_endpoint_failover_in_production_path(self):
        as_of = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        start_ms = int((as_of - dt.timedelta(hours=24)).timestamp() * 1000)
        rows = [
            kline(value)
            for value in range(start_ms, int(as_of.timestamp() * 1000), MINUTE_MS)
        ]

        with mock.patch(
            "calculate_cvd.fetch_klines_with_failover",
            side_effect=[
                (rows, "https://spot.test/klines"),
                (rows, "https://futures.test/klines"),
            ],
        ) as failover:
            result = compute(as_of)

        self.assertEqual(failover.call_count, 2)
        self.assertEqual(
            result["markets"]["spot"]["source_endpoint"],
            "https://spot.test/klines",
        )
        self.assertEqual(
            result["markets"]["usd_m_futures"]["source_endpoint"],
            "https://futures.test/klines",
        )


if __name__ == "__main__":
    unittest.main()

