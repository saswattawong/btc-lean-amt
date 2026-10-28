import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from calculate_archive_cvd import (
    ARCHIVES,
    ArchiveUnavailable,
    UTC,
    archive_url,
    compute,
    download_verified_archive,
    find_latest_common_day,
    normalize_epoch_ms,
    validate_klines,
    window_stats,
    write_if_changed,
)
from validate_outputs import validate_archive_cvd


DAY = dt.date(2026, 10, 26)
TEMP_ROOT = pathlib.Path(__file__).resolve().parent


def raw_rows(day=DAY, count=1_440, microseconds=False, buy="60", total="100"):
    start = int(dt.datetime.combine(day, dt.time(), UTC).timestamp() * 1000)
    scale = 1_000 if microseconds else 1
    rows = []
    for index in range(count):
        open_ms = start + index * 60_000
        close_ms = open_ms + 59_999
        rows.append(
            [
                str(open_ms * scale),
                "1",
                "1",
                "1",
                "1",
                "1",
                str(close_ms * scale + (999 if microseconds else 0)),
                total,
                "1",
                "1",
                buy,
                "0",
            ]
        )
    return rows


def make_zip(path, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BTCUSDT-1m.csv", buffer.getvalue())


class ArchiveCvdTest(unittest.TestCase):
    def test_timestamp_normalization(self):
        self.assertEqual(normalize_epoch_ms(1_798_000_000_000), 1_798_000_000_000)
        self.assertEqual(normalize_epoch_ms(1_798_000_000_000_000), 1_798_000_000_000)

    def test_urls_are_official_spot_and_usd_m_archives(self):
        self.assertIn("/data/spot/daily/klines/", archive_url("spot", "BTCUSDT", DAY))
        self.assertIn("/data/futures/um/daily/klines/", archive_url("usd_m_futures", "BTCUSDT", DAY))

    def test_validation_accepts_exact_1440_and_microseconds(self):
        rows = validate_klines(raw_rows(microseconds=True), DAY, "spot")
        self.assertEqual(len(rows), 1_440)
        self.assertEqual(rows[0]["quote_volume"], Decimal("100"))
        self.assertEqual(rows[0]["taker_buy_quote"], Decimal("60"))

    def test_validation_rejects_missing_duplicate_and_bad_volume(self):
        with self.assertRaisesRegex(ValueError, "expected 1440"):
            validate_klines(raw_rows(count=1_439), DAY, "test")
        duplicate = raw_rows()
        duplicate[-1] = duplicate[-2]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_klines(duplicate, DAY, "test")
        bad = raw_rows()
        bad[0][10] = "101"
        with self.assertRaisesRegex(ValueError, "exceeds total"):
            validate_klines(bad, DAY, "test")

    def test_window_math(self):
        end = dt.datetime(2026, 10, 27, tzinfo=UTC)
        rows = [
            {"open_ms": index, "quote_volume": Decimal("100"), "taker_buy_quote": Decimal("60")}
            for index in range(240)
        ]
        result = window_stats(rows, 240, end)
        self.assertEqual(result["quote_volume_usdt"], 24_000)
        self.assertEqual(result["taker_buy_quote_volume_usdt"], 14_400)
        self.assertEqual(result["taker_sell_quote_volume_usdt"], 9_600)
        self.assertEqual(result["cvd_quote_usdt"], 4_800)
        self.assertEqual(result["taker_buy_share_pct"], 60)
        self.assertEqual(result["normalized_cvd"], 0.2)

    def test_checksum_is_required_and_verified(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = pathlib.Path(directory)
            source = root / "source.zip"
            destination = root / "cache" / "archive.zip"
            make_zip(source, raw_rows())
            payload = source.read_bytes()
            checksum = hashlib.sha256(payload).hexdigest()

            def requester(url):
                return (checksum + "  source.zip\n").encode() if url.endswith(".CHECKSUM") else payload

            path, evidence = download_verified_archive("https://example.test/source.zip", destination, requester)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(evidence["sha256"], checksum)

            def wrong_checksum(url):
                return ("0" * 64 + "  source.zip\n").encode() if url.endswith(".CHECKSUM") else payload

            destination.unlink()
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                download_verified_archive("https://example.test/source.zip", destination, wrong_checksum)

    def test_latest_common_day_falls_back_only_when_archive_unavailable(self):
        today = dt.date(2026, 10, 28)
        calls = []

        def downloader(market, symbol, day, cache):
            calls.append((market, day))
            if day == dt.date(2026, 10, 27) and market == "usd_m_futures":
                raise ArchiveUnavailable("not published")
            return pathlib.Path(f"{market}.zip"), {"sha256": "a" * 64}

        day, downloaded = find_latest_common_day(today, "BTCUSDT", pathlib.Path("cache"), downloader=downloader)
        self.assertEqual(day, dt.date(2026, 10, 26))
        self.assertEqual(set(downloaded), set(ARCHIVES))
        self.assertIn(("usd_m_futures", dt.date(2026, 10, 27)), calls)

    def test_compute_produces_compatible_schema_and_synchronized_windows(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = pathlib.Path(directory)
            spot = root / "spot.zip"
            futures = root / "futures.zip"
            make_zip(spot, raw_rows(microseconds=True, buy="55"))
            make_zip(futures, raw_rows(buy="45"))
            evidence = {
                "archive_url": "https://data.binance.vision/example.zip",
                "checksum_url": "https://data.binance.vision/example.zip.CHECKSUM",
                "sha256": "a" * 64,
            }
            result = compute(
                DAY,
                {"spot": (spot, evidence), "usd_m_futures": (futures, evidence)},
            )
            self.assertEqual(result["schema"], "btc_binance_quote_cvd_v1")
            self.assertEqual(result["as_of_utc"], "2026-10-27T00:00:00Z")
            self.assertEqual(result["freshness"]["source_as_of_utc"], result["as_of_utc"])
            self.assertTrue(result["integrity"]["archive_sha256_verified"])
            self.assertEqual(result["markets"]["spot"]["validated_closed_1m_klines"], 1_440)
            self.assertEqual(result["markets"]["spot"]["windows"]["24h"]["normalized_cvd"], 0.1)
            self.assertEqual(result["markets"]["usd_m_futures"]["windows"]["24h"]["normalized_cvd"], -0.1)
            self.assertEqual(
                result["markets"]["spot"]["windows"]["1h"]["end_utc_exclusive"],
                result["markets"]["usd_m_futures"]["windows"]["1h"]["end_utc_exclusive"],
            )
            generated = dt.datetime.fromisoformat(result["generated_at_utc"].replace("Z", "+00:00"))
            summary = validate_archive_cvd(
                result,
                now=generated + dt.timedelta(minutes=1),
                max_age_seconds=500_000,
            )
            self.assertEqual(summary["integrity"], "passed")

    def test_write_if_changed_does_not_churn_same_verified_day(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            output = pathlib.Path(directory) / "latest.json"
            data = {
                "schema": "btc_binance_quote_cvd_v1",
                "as_of_utc": "2026-10-27T00:00:00Z",
                "generated_at_utc": "first",
                "integrity": {"status": "passed", "archive_sha256_verified": True},
            }
            self.assertTrue(write_if_changed(data, output))
            updated = dict(data, generated_at_utc="second")
            self.assertFalse(write_if_changed(updated, output))
            self.assertEqual(json.loads(output.read_text())["generated_at_utc"], "first")


if __name__ == "__main__":
    unittest.main()
