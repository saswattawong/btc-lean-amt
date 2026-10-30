import datetime as dt
import io
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from gate_perp_cvd import (
    CONTRACT_ENDPOINT,
    STATE_SCHEMA,
    aggregate_minutes,
    collect_gate_perpetual,
    fetch_trades_range,
    request_json,
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
    def test_retryable_http_error_honors_retry_after(self):
        responses = [
            urllib.error.HTTPError(
                "https://example.invalid",
                429,
                "rate limited",
                {"Retry-After": "2"},
                None,
            ),
            io.BytesIO(b'{"ok": true}'),
        ]
        delays = []

        def urlopen(request, timeout):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch("gate_perp_cvd.urllib.request.urlopen", side_effect=urlopen):
            result = request_json(
                "https://example.invalid",
                attempts=2,
                min_interval_seconds=0,
                monotonic=lambda: 0,
                sleeper=delays.append,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(delays, [2.0])

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
        self.assertTrue(evidence["terminal_short_page_observed"])

    def test_offset_pagination_deduplicates_overlapping_pages(self):
        start = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        trades = [
            {
                "id": index,
                "contract": "BTC_USDT",
                "create_time_ms": start.timestamp() + index + 0.5,
                "size": "1",
                "price": "80000",
            }
            for index in range(7)
        ]
        pages = {
            0: trades[0:3],
            3: trades[2:5],
            6: trades[4:7],
            9: [],
        }

        def requester(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return pages[int(query["offset"][0])]

        result, evidence = fetch_trades_range(
            start,
            start + dt.timedelta(minutes=1),
            requester=requester,
            page_limit=3,
        )
        self.assertEqual([row["id"] for row in result], list(range(7)))
        self.assertEqual(evidence["duplicate_trade_ids_filtered"], 2)
        self.assertEqual(evidence["request_count"], 4)

    def test_offset_pagination_rejects_a_full_page_that_makes_no_progress(self):
        start = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        page = [
            {
                "id": index,
                "contract": "BTC_USDT",
                "create_time_ms": start.timestamp() + index + 0.5,
                "size": "1",
                "price": "80000",
            }
            for index in range(3)
        ]

        with self.assertRaisesRegex(RuntimeError, "made no progress"):
            fetch_trades_range(
                start,
                start + dt.timedelta(minutes=1),
                requester=lambda url: page,
                page_limit=3,
                max_requests=5,
            )

    def test_high_density_range_splits_before_unsafe_offset(self):
        start = dt.datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        trades = [
            {
                "id": index,
                "contract": "BTC_USDT",
                "create_time_ms": start.timestamp() + index * 7 + 0.5,
                "size": "1",
                "price": "80000",
            }
            for index in range(16)
        ]
        observed_offsets = []

        def requester(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            limit = int(query["limit"][0])
            offset = int(query["offset"][0])
            observed_offsets.append(offset)
            matches = [row for row in trades if left <= row["create_time_ms"] < right]
            return list(reversed(matches))[offset:offset + limit]

        result, evidence = fetch_trades_range(
            start,
            start + dt.timedelta(minutes=2),
            requester=requester,
            page_limit=2,
            max_page_offset=4,
            max_requests=100,
        )
        self.assertEqual([row["id"] for row in result], list(range(16)))
        self.assertGreater(evidence["time_split_count"], 0)
        self.assertGreater(evidence["verified_untruncated_segments"], 1)
        self.assertEqual(
            evidence["pagination_method"],
            "adaptive_time_split_offset_until_short_page",
        )
        self.assertLess(max(observed_offsets), 4)

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

    def test_large_scheduler_gap_reconciles_without_resetting_verified_state(self):
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
            self.assertEqual(recovered["coverage"]["validated_closed_1m_klines"], 330)
            self.assertEqual(recovered["windows"]["4h"]["status"], "available")
            integrity = recovered["fetch_integrity"]
            self.assertEqual(integrity["state_recovery_mode"], "gap_recovery")
            self.assertEqual(integrity["detected_state_gap_minutes"], 180)
            self.assertEqual(integrity["persisted_buckets_retained"], 60)
            self.assertIsNone(integrity["state_reset_reason"])

    def test_interrupted_backfill_checkpoints_and_resumes(self):
        as_of = dt.datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        fail_once = {"enabled": True}
        second_chunk_end = int((as_of - dt.timedelta(minutes=90)).timestamp())

        def requester(url):
            if url == CONTRACT_ENDPOINT:
                return contract_payload()
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            left = int(query["from"][0])
            right = int(query["to"][0])
            if fail_once["enabled"] and right <= second_chunk_end:
                fail_once["enabled"] = False
                raise TimeoutError("simulated workflow interruption")
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
            interrupted = collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=120,
                backfill_chunk_minutes=30,
                requester=requester,
            )
            self.assertEqual(interrupted["coverage"]["validated_closed_1m_klines"], 90)
            self.assertEqual(
                interrupted["fetch_integrity"]["historical_backfill"]["status"],
                "partial",
            )
            checkpointed = json.loads(state_path.read_text())
            self.assertEqual(checkpointed["contiguous_tail_closed_1m_buckets"], 90)
            self.assertEqual(checkpointed["stored_closed_1m_buckets"], 150)

            resumed = collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=120,
                backfill_chunk_minutes=30,
                requester=requester,
            )
            # The first run also checkpointed two verified chunks behind the
            # simulated failed segment.  Filling that gap connects those saved
            # chunks, so resumed contiguous coverage jumps from 90 to 270.
            self.assertEqual(resumed["coverage"]["validated_closed_1m_klines"], 270)
            self.assertEqual(resumed["fetch_integrity"]["state_load"]["status"], "loaded")
            self.assertEqual(resumed["fetch_integrity"]["persisted_buckets_retained"], 150)
            self.assertIsNone(resumed["fetch_integrity"]["state_reset_reason"])

    def test_recovery_fetches_only_missing_segments_and_preserves_verified_buckets(self):
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
                    "size": "1" if (second // 60) % 2 else "-1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=120,
                backfill_workers=4,
                requester=requester,
            )
            before = json.loads(state_path.read_text())
            before_rows = {row["open_utc"]: row for row in before["closed_minute_buckets"]}

            recovered = collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=120,
                backfill_workers=4,
                requester=requester,
            )
            chunks = recovered["fetch_integrity"]["historical_backfill"]["chunks"]
            for chunk in chunks:
                cursor = dt.datetime.fromisoformat(
                    chunk["range_start_utc"].replace("Z", "+00:00")
                )
                end = dt.datetime.fromisoformat(
                    chunk["range_end_utc_exclusive"].replace("Z", "+00:00")
                )
                while cursor < end:
                    key = cursor.isoformat(timespec="seconds").replace("+00:00", "Z")
                    self.assertNotIn(key, before_rows)
                    cursor += dt.timedelta(minutes=1)

            after = json.loads(state_path.read_text())
            after_rows = {row["open_utc"]: row for row in after["closed_minute_buckets"]}
            for key, row in before_rows.items():
                # The ten-minute current-tail refresh is intentionally allowed
                # to revalidate recent buckets; deterministic source data keeps
                # their verified payload identical.
                self.assertEqual(after_rows[key], row)
            self.assertEqual(recovered["coverage"]["validated_closed_1m_klines"], 300)
            self.assertEqual(recovered["windows"]["1h"]["status"], "available")
            self.assertEqual(recovered["windows"]["4h"]["status"], "available")
            self.assertEqual(recovered["windows"]["24h"]["status"], "unavailable")

    def test_parallel_backfill_respects_one_shared_request_budget(self):
        as_of = dt.datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        active = 0
        peak_active = 0
        lock = threading.Lock()

        def requester(url):
            nonlocal active, peak_active
            if url == CONTRACT_ENDPOINT:
                return contract_payload()
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                time.sleep(0.02)
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
            finally:
                with lock:
                    active -= 1

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            market = collect_gate_perpetual(
                as_of,
                pathlib.Path(directory) / "state.json",
                bootstrap_minutes=60,
                backfill_minutes=180,
                backfill_chunk_minutes=30,
                backfill_workers=4,
                max_requests=5,
                requester=requester,
            )
            integrity = market["fetch_integrity"]
            self.assertEqual(integrity["request_count"], 5)
            self.assertEqual(integrity["request_budget_remaining"], 0)
            self.assertGreaterEqual(peak_active, 2)
            self.assertEqual(
                integrity["historical_backfill"]["completed_minutes"],
                90,
            )
            self.assertEqual(integrity["historical_backfill"]["status"], "partial")

    def test_atomic_replace_failure_preserves_previous_checkpoint(self):
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
                    "size": "1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            previous = state_path.read_bytes()
            with mock.patch("gate_perp_cvd.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    collect_gate_perpetual(
                        as_of + dt.timedelta(minutes=5),
                        state_path,
                        bootstrap_minutes=60,
                        backfill_minutes=0,
                        requester=requester,
                    )
            self.assertEqual(state_path.read_bytes(), previous)
            self.assertEqual(list(state_path.parent.glob(f".{state_path.name}.*.tmp")), [])

    def test_corrupt_state_is_rejected_and_rebuilt_atomically(self):
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
                    "size": "1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            state_path.write_text('{"schema":', encoding="utf-8")
            market = collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            self.assertEqual(market["coverage"]["validated_closed_1m_klines"], 60)
            self.assertEqual(market["fetch_integrity"]["state_load"]["status"], "rejected")
            self.assertIn("JSONDecodeError", market["fetch_integrity"]["state_load"]["error"])
            rebuilt = json.loads(state_path.read_text())
            self.assertEqual(rebuilt["schema"], STATE_SCHEMA)
            self.assertEqual(rebuilt["contiguous_tail_closed_1m_buckets"], 60)

    def test_state_payload_checksum_detects_silent_bucket_corruption(self):
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
                    "size": "1",
                    "price": "80000",
                }
                for second in range(left, right, 60)
            ]

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            state_path = pathlib.Path(directory) / "state.json"
            collect_gate_perpetual(
                as_of,
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            corrupted = json.loads(state_path.read_text())
            corrupted["closed_minute_buckets"][0]["quote_volume_usdt"] = "999999"
            state_path.write_text(json.dumps(corrupted), encoding="utf-8")

            recovered = collect_gate_perpetual(
                as_of + dt.timedelta(minutes=5),
                state_path,
                bootstrap_minutes=60,
                backfill_minutes=0,
                requester=requester,
            )
            load = recovered["fetch_integrity"]["state_load"]
            self.assertEqual(load["status"], "rejected")
            self.assertIn("checksum mismatch", load["error"])
            self.assertEqual(recovered["coverage"]["validated_closed_1m_klines"], 60)

    def test_full_backfill_produces_exact_contiguous_1440_minutes(self):
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
                    "size": "1" if (second // 60) % 2 else "-1",
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
                backfill_minutes=1_440,
                backfill_chunk_minutes=30,
                requester=requester,
            )
            self.assertEqual(market["status"], "available")
            self.assertEqual(market["coverage"]["validated_closed_1m_klines"], 1_440)
            self.assertEqual(market["windows"]["24h"]["status"], "available")
            self.assertEqual(market["windows"]["24h"]["closed_klines"], 1_440)
            self.assertEqual(market["fetch_integrity"]["target_window_continuity"], "passed")
            state = json.loads(state_path.read_text())
            self.assertEqual(state["stored_closed_1m_buckets"], 1_440)
            self.assertEqual(state["contiguous_tail_closed_1m_buckets"], 1_440)
            self.assertEqual(state["missing_closed_1m_buckets"], 0)
            self.assertEqual(state["missing_ranges"], [])
            self.assertEqual(
                state["closed_minute_buckets"][0]["open_utc"],
                "2026-10-27T12:00:00Z",
            )
            self.assertEqual(
                state["closed_minute_buckets"][-1]["open_utc"],
                "2026-10-28T11:59:00Z",
            )

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
