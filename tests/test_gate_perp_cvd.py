import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.parse
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from gate_perp_cvd import (
    CONTRACT_ENDPOINT,
    STATE_SCHEMA,
    aggregate_minutes,
    collect_gate_perpetual,
    fetch_trades_range,
)


UTC = dt.timezone.utc
TEMP_ROOT = pathlib.Path(__file__).resolve().parent


def contract_payload():
    return {
        "name": "BTC_USDT",
        "type": "direct",
        "quanto_multiplier": "0.0001",
        "status": "trading",
        "in_delisting": False,
    }


class GatePerpCvdTest(unittest.TestCase):
    def test_offset_pagination_proves_untruncated_trade_range(self):
        start = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        trades = [
            {
                "id": index,
                "contract": "BTC_USDT",
                "create_time_ms": start.timestamp() + index * 10 + 0.5,
                "size": "1" if index % 2 == 0 else "-1",
                "price": "80000",
            }
            for index in range(12)
        ]

        def requester(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            limit = int(query["limit"][0])
            offset = int(query["offset"][0])
            matches = [row for row in trades if left <= row["create_time_ms"] < right]
            return list(reversed(matches))[offset:offset + limit]

        result, evidence = fetch_trades_range(
            start,
            start + dt.timedelta(minutes=2),
            requester=requester,
            page_limit=3,
            max_requests=40,
        )
        self.assertEqual(len(result), len(trades))
        self.assertEqual(evidence["request_count"], 5)
        self.assertEqual(evidence["unique_trades"], 12)
        self.assertEqual(evidence["pagination_method"], "offset_until_short_page")

    def test_quote_notional_and_taker_sign_are_explicit(self):
        start = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        trades = [
            {"id": 1, "timestamp_seconds": Decimal(str(start.timestamp() + 1)), "size": Decimal("2"), "price": Decimal("80000")},
            {"id": 2, "timestamp_seconds": Decimal(str(start.timestamp() + 2)), "size": Decimal("-1"), "price": Decimal("80000")},
        ]
        result = aggregate_minutes(trades, start, start + dt.timedelta(minutes=1), Decimal("0.0001"))
        bucket = next(iter(result.values()))
        self.assertEqual(bucket["quote_volume_usdt"], Decimal("24"))
        self.assertEqual(bucket["taker_buy_quote_volume_usdt"], Decimal("16"))
        self.assertEqual(bucket["trade_count"], 2)

    def test_rolling_state_exposes_only_windows_with_verified_coverage(self):
        as_of = dt.datetime(2026, 10, 28, 1, 0, tzinfo=UTC)

        def requester(url):
            if url == CONTRACT_ENDPOINT:
                return contract_payload()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            return [
                {
                    "id": second,
                    "contract": "BTC_USDT",
                    "create_time_ms": second + 30.0,
                    "size": "2" if (second // 60) % 2 == 0 else "-1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            market = collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            self.assertEqual(market["status"], "partial")
            self.assertEqual(market["coverage"]["validated_closed_1m_klines"], 60)
            self.assertEqual(market["windows"]["1h"]["status"], "available")
            self.assertEqual(market["windows"]["4h"]["cvd_quote_usdt"], "N/A")
            self.assertTrue(market["fetch_integrity"]["time_range_untruncated"])
            state = json.loads(state_path.read_text())
            self.assertEqual(state["schema"], STATE_SCHEMA)

            later = collect_gate_perpetual(
                as_of + dt.timedelta(minutes=10),
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            self.assertEqual(later["coverage"]["validated_closed_1m_klines"], 70)
            self.assertEqual(later["coverage"]["end_utc_exclusive"], "2026-10-28T01:10:00Z")

    def test_incremental_backfill_reaches_4h_immediately_and_24h_in_seven_rounds(self):
        as_of = dt.datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

        def requester(url):
            if url == CONTRACT_ENDPOINT:
                return contract_payload()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            return [
                {
                    "id": second,
                    "contract": "BTC_USDT",
                    "create_time_ms": second + 30.0,
                    "size": "1" if (second // 60) % 2 == 0 else "-1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            market = None
            for round_index in range(7):
                market = collect_gate_perpetual(
                    as_of + dt.timedelta(minutes=5 * round_index),
                    state_path,
                    bootstrap_minutes=60,
                    backfill_minutes=210,
                    requester=requester,
                )
                if round_index == 0:
                    self.assertEqual(market["coverage"]["validated_closed_1m_klines"], 270)
                    self.assertEqual(market["windows"]["4h"]["status"], "available")
                    self.assertEqual(market["windows"]["24h"]["status"], "unavailable")
                    self.assertEqual(
                        market["fetch_integrity"]["historical_backfill"]["status"],
                        "passed",
                    )

            self.assertIsNotNone(market)
            self.assertEqual(market["coverage"]["validated_closed_1m_klines"], 1_440)
            self.assertEqual(market["windows"]["24h"]["status"], "available")
            self.assertEqual(market["status"], "available")

    def test_large_scheduler_gap_rebuilds_current_tail_before_backfill(self):
        first_as_of = dt.datetime(2026, 10, 28, 1, 0, tzinfo=UTC)

        def requester(url):
            if url == CONTRACT_ENDPOINT:
                return contract_payload()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            return [
                {
                    "id": second,
                    "contract": "BTC_USDT",
                    "create_time_ms": second + 30.0,
                    "size": "1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            collect_gate_perpetual(
                first_as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            recovered = collect_gate_perpetual(
                first_as_of + dt.timedelta(hours=3),
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=210,
                max_incremental_gap_minutes=60,
                requester=requester,
            )
            self.assertEqual(recovered["coverage"]["validated_closed_1m_klines"], 270)
            self.assertEqual(recovered["windows"]["4h"]["status"], "available")
            self.assertIn("state gap 180m", recovered["fetch_integrity"]["state_reset_reason"])

    def test_failed_backfill_is_explicit_and_never_merged(self):
        as_of = dt.datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
        refresh_start = int((as_of - dt.timedelta(minutes=60)).timestamp())

        def requester(url):
            if url == CONTRACT_ENDPOINT:
                return contract_payload()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            if right <= refresh_start:
                raise ValueError("historical provider failure")
            return [
                {
                    "id": second,
                    "contract": "BTC_USDT",
                    "create_time_ms": second + 30.0,
                    "size": "1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            market = collect_gate_perpetual(
                as_of,
                pathlib.Path(directory) / "state.json",
                bootstrap_minutes=60,
                backfill_minutes=210,
                requester=requester,
            )
            self.assertEqual(market["coverage"]["validated_closed_1m_klines"], 60)
            self.assertEqual(market["windows"]["4h"]["status"], "unavailable")
            self.assertEqual(
                market["fetch_integrity"]["historical_backfill"]["status"],
                "failed",
            )
            self.assertIn(
                "historical provider failure",
                market["fetch_integrity"]["historical_backfill"]["error"],
            )


if __name__ == "__main__":
    unittest.main()
