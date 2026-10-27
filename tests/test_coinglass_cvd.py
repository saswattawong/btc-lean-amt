import datetime as dt
import pathlib
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from calculate_coinglass_cvd import FUTURES_ENDPOINT, HOUR_MS, SPOT_ENDPOINT, UTC, closed_hour_boundary, compute, fetch_hourly, parse_as_of, validate_hourly, window_stats


def hour(timestamp, buy="60", sell="40"):
    return {"time": timestamp, "taker_buy_volume_usd": buy, "taker_sell_volume_usd": sell}


class CoinGlassCvdTest(unittest.TestCase):
    def test_closed_hour_boundary(self):
        self.assertEqual(closed_hour_boundary(dt.datetime(2026, 10, 27, 12, 7, tzinfo=UTC)), dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
        self.assertEqual(closed_hour_boundary(dt.datetime(2026, 10, 27, 12, 1, tzinfo=UTC)), dt.datetime(2026, 10, 27, 11, 0, tzinfo=UTC))

    def test_parse_as_of_requires_top_of_hour_and_timezone(self):
        self.assertEqual(parse_as_of("2026-10-27T12:00:00Z"), dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC))
        with self.assertRaisesRegex(ValueError, "top-of-hour"):
            parse_as_of("2026-10-27T12:01:00Z")
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_as_of("2026-10-27T12:00:00")

    def test_fetch_hourly_builds_bounded_authenticated_request(self):
        calls = []

        def requester(url, api_key):
            calls.append((url, api_key))
            return {"code": "0", "data": []}

        fetch_hourly("https://example.test/history", "BTCUSDT", 0, HOUR_MS, "secret", requester)
        self.assertEqual(calls[0][1], "secret")
        self.assertIn("exchange=Binance", calls[0][0])
        self.assertIn("interval=h1", calls[0][0])
        self.assertIn("end_time=3599999", calls[0][0])
        self.assertNotIn("secret", calls[0][0])

    def test_fetch_hourly_rejects_provider_error(self):
        def requester(url, api_key):
            return {"code": "400", "msg": "bad request"}

        with self.assertRaisesRegex(ValueError, "code=400"):
            fetch_hourly("https://example.test", "BTCUSDT", 0, HOUR_MS, "secret", requester)

    def test_validation_accepts_seconds_and_rejects_missing_hour(self):
        start = 1_799_971_200_000
        end = start + 3 * HOUR_MS
        rows = [hour((start + index * HOUR_MS) // 1000) for index in range(3)]
        self.assertEqual(len(validate_hourly(rows, start, end, "test")), 3)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            validate_hourly(rows[:-1], start, end, "test")

    def test_validation_rejects_duplicate_and_bad_volume(self):
        start = 1_799_971_200_000
        end = start + HOUR_MS
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_hourly([hour(start), hour(start)], start, end, "test")
        with self.assertRaisesRegex(ValueError, "invalid taker buy"):
            validate_hourly([hour(start, buy="NaN")], start, end, "test")

    def test_usd_cvd_and_normalized_buy_share(self):
        end = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        rows = [{"open_ms": index, "taker_buy_usd": Decimal("60"), "taker_sell_usd": Decimal("40")} for index in range(4)]
        result = window_stats(rows, 4, end)
        self.assertEqual(result["total_volume_usd"], 400)
        self.assertEqual(result["taker_buy_volume_usd"], 240)
        self.assertEqual(result["taker_sell_volume_usd"], 160)
        self.assertEqual(result["cvd_usd"], 80)
        self.assertEqual(result["taker_buy_share"], 0.6)
        self.assertEqual(result["normalized_cvd"], 0.2)

    def test_compute_uses_synchronized_boundaries_and_honest_schema(self):
        as_of = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        calls = []

        def fake_fetch(endpoint, symbol, start_ms, end_ms, api_key):
            calls.append((endpoint, symbol, start_ms, end_ms, api_key))
            return [hour(value) for value in range(start_ms, end_ms, HOUR_MS)]

        result = compute(as_of, "secret", fetcher=fake_fetch)
        self.assertEqual([call[0] for call in calls], [SPOT_ENDPOINT, FUTURES_ENDPOINT])
        self.assertEqual(calls[0][2:4], calls[1][2:4])
        self.assertEqual(result["schema"], "btc_coinglass_usd_cvd_v1")
        self.assertEqual(result["unit"], "USD")
        self.assertTrue(result["integrity"]["provider_data_not_binance_raw_klines"])
        for market in ("spot", "usd_m_futures"):
            self.assertEqual(result["markets"][market]["validated_closed_1h_intervals"], 24)
            self.assertEqual(result["markets"][market]["windows"]["1h"]["cvd_usd"], 20)
            self.assertEqual(result["markets"][market]["windows"]["4h"]["cvd_usd"], 80)
            self.assertEqual(result["markets"][market]["windows"]["24h"]["cvd_usd"], 480)


if __name__ == "__main__":
    unittest.main()
