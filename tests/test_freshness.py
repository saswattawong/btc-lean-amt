import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from calculate_cvd import UTC
from validate_outputs import validate_intraday, validate_value_map


class FreshnessTest(unittest.TestCase):
    def test_recent_generation_never_masks_old_intraday_source(self):
        source = dt.datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
        generated = dt.datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        data = {
            "schema": "btc_binance_intraday_v1",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "generated_at_utc": generated.isoformat().replace("+00:00", "Z"),
            "freshness": {
                "source_as_of_utc": source.isoformat().replace("+00:00", "Z"),
                "generated_at_utc": generated.isoformat().replace("+00:00", "Z"),
                "source_age_seconds_at_generation": 7200,
            },
        }
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_intraday(data, now=generated, max_age_seconds=1200)

    def test_value_map_validator_preserves_checksum_and_source_age(self):
        source = dt.datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
        generated = dt.datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
        data = {
            "schema": "btc_lean_amt_value_map_v1",
            "generated_at_utc": generated.isoformat(),
            "as_of_closed_day_utc": "2026-10-26",
            "session_end_utc_exclusive": "2026-10-27T00:00:00Z",
            "freshness": {
                "source_as_of_utc": "2026-10-27T00:00:00Z",
                "generated_at_utc": generated.isoformat(),
                "source_age_seconds_at_generation": 8 * 60 * 60,
            },
            "integrity_cross_check": {"status": "passed"},
            "previous_completed_week_trade_volume_profile": {"integrity_status": "passed"},
            "daily_poc_migration": {"to_day_utc": "2026-10-26"},
            "sources": {
                "sha256": "a" * 64,
                "checksum_url": "https://data.binance.vision/example.zip.CHECKSUM",
            },
        }
        result = validate_value_map(
            data,
            now=generated + dt.timedelta(minutes=1),
            max_age_seconds=100_000,
        )
        self.assertEqual(result["source_as_of_utc"], source.isoformat().replace("+00:00", "Z"))


if __name__ == "__main__":
    unittest.main()
