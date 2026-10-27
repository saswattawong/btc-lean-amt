#!/usr/bin/env python3
"""Verified Binance Spot and USD-M Futures CVD from free daily archives.

Binance Data Vision publishes the completed UTC day's one-minute klines on the
following day.  Each archive is verified against Binance's companion SHA-256
file before exactly 1,440 synchronized candles per market are accepted.
"""

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import time
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
MINUTE_MS = 60_000
WINDOWS = {"1h": 60, "4h": 240, "24h": 1_440}
ARCHIVES = {
    "spot": {
        "label": "Binance Spot",
        "url": "https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{day}.zip",
    },
    "usd_m_futures": {
        "label": "Binance USD-M Futures perpetual",
        "url": "https://data.binance.vision/data/futures/um/daily/klines/{symbol}/1m/{symbol}-1m-{day}.zip",
    },
}


class ArchiveUnavailable(Exception):
    """The requested completed UTC day has not been published yet."""


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def day_boundary(day):
    return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), UTC)


def normalize_epoch_ms(value):
    """Normalize Binance millisecond/microsecond timestamps to milliseconds."""
    timestamp = int(value)
    while timestamp >= 100_000_000_000_000:
        timestamp //= 1_000
    return timestamp


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def request_bytes(url, attempts=3):
    request = urllib.request.Request(url, headers={"User-Agent": "btc-lean-amt-cvd/3.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ArchiveUnavailable(url) from error
            if error.code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def expected_checksum(url, requester=request_bytes):
    checksum_url = url + ".CHECKSUM"
    text = requester(checksum_url).decode("ascii").strip()
    token = text.split()[0].lower() if text else ""
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError(f"Invalid Binance checksum response: {checksum_url}")
    return token, checksum_url


def download_verified_archive(url, destination, requester=request_bytes):
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected, checksum_url = expected_checksum(url, requester)
    if destination.exists() and zipfile.is_zipfile(destination) and sha256_file(destination) == expected:
        return destination, {
            "archive_url": url,
            "checksum_url": checksum_url,
            "sha256": expected,
        }

    payload = requester(url)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        temporary.write_bytes(payload)
        if not zipfile.is_zipfile(temporary):
            raise ValueError(f"Downloaded file is not a ZIP: {url}")
        actual = sha256_file(temporary)
        if actual != expected:
            raise ValueError(f"Binance archive checksum mismatch: expected {expected}, got {actual}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination, {
        "archive_url": url,
        "checksum_url": checksum_url,
        "sha256": expected,
    }


def _decimal(value, field, market, timestamp):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{market}: invalid {field} at {timestamp}") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{market}: invalid {field} at {timestamp}")
    return number


def kline_rows_from_zip(path):
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"Expected exactly one CSV in {path}, got {len(names)}")
        with archive.open(names[0]) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))


def validate_klines(rows, day, market):
    start_utc = dt.datetime.combine(day, dt.time(), UTC)
    end_utc = start_utc + dt.timedelta(days=1)
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    expected_opens = list(range(start_ms, end_ms, MINUTE_MS))
    by_open = {}

    for raw in rows:
        if not raw or not str(raw[0]).isdigit():
            continue
        if len(raw) < 11:
            raise ValueError(f"{market}: malformed Binance kline row")
        open_ms = normalize_epoch_ms(raw[0])
        if not start_ms <= open_ms < end_ms:
            raise ValueError(f"{market}: kline outside requested UTC day at {open_ms}")
        if open_ms in by_open:
            raise ValueError(f"{market}: duplicate kline at {open_ms}")
        by_open[open_ms] = raw

    missing = [value for value in expected_opens if value not in by_open]
    if missing or len(by_open) != 1_440:
        raise ValueError(
            f"{market}: expected 1440 contiguous closed 1m klines; "
            f"got {len(by_open)}, missing={missing[:3]}"
        )

    validated = []
    for open_ms in expected_opens:
        raw = by_open[open_ms]
        close_ms = normalize_epoch_ms(raw[6])
        if close_ms != open_ms + MINUTE_MS - 1:
            raise ValueError(f"{market}: unclosed or misaligned kline at {open_ms}")
        quote_volume = _decimal(raw[7], "quote volume", market, open_ms)
        taker_buy_quote = _decimal(raw[10], "taker-buy quote volume", market, open_ms)
        if taker_buy_quote > quote_volume:
            raise ValueError(f"{market}: taker-buy quote volume exceeds total at {open_ms}")
        validated.append(
            {
                "open_ms": open_ms,
                "quote_volume": quote_volume,
                "taker_buy_quote": taker_buy_quote,
            }
        )
    return validated


def window_stats(rows, minutes, end_utc):
    selected = rows[-minutes:]
    if len(selected) != minutes:
        raise ValueError(f"Need {minutes} closed klines, got {len(selected)}")
    quote_volume = sum((row["quote_volume"] for row in selected), Decimal(0))
    taker_buy = sum((row["taker_buy_quote"] for row in selected), Decimal(0))
    if quote_volume <= 0:
        raise ValueError(f"Zero quote volume in {minutes}-minute window")
    taker_sell = quote_volume - taker_buy
    cvd = taker_buy - taker_sell
    buy_share = taker_buy / quote_volume
    normalized_cvd = cvd / quote_volume
    return {
        "start_utc": iso_utc(end_utc - dt.timedelta(minutes=minutes)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": minutes,
        "quote_volume_usdt": round(float(quote_volume), 8),
        "taker_buy_quote_volume_usdt": round(float(taker_buy), 8),
        "taker_sell_quote_volume_usdt": round(float(taker_sell), 8),
        "cvd_quote_usdt": round(float(cvd), 8),
        "taker_buy_share": round(float(buy_share), 10),
        "taker_buy_share_pct": round(float(buy_share * 100), 8),
        "normalized_cvd": round(float(normalized_cvd), 10),
    }


def archive_url(market, symbol, day):
    return ARCHIVES[market]["url"].format(symbol=symbol, day=day.isoformat())


def download_market(market, symbol, day, cache, requester=request_bytes):
    url = archive_url(market, symbol, day)
    destination = cache / market / f"{symbol}-1m-{day.isoformat()}.zip"
    return download_verified_archive(url, destination, requester)


def find_latest_common_day(today, symbol, cache, lookback_days=4, downloader=download_market):
    for days_ago in range(1, lookback_days + 1):
        day = today - dt.timedelta(days=days_ago)
        downloaded = {}
        try:
            for market in ARCHIVES:
                downloaded[market] = downloader(market, symbol, day, cache)
            return day, downloaded
        except ArchiveUnavailable:
            continue
    raise ArchiveUnavailable(
        f"No common Binance Spot/USD-M daily archive found in the last {lookback_days} completed UTC days"
    )


def compute(day, downloaded, symbol="BTCUSDT"):
    end_utc = day_boundary(day)
    markets = {}
    open_sequences = {}
    for key, config in ARCHIVES.items():
        path, source = downloaded[key]
        rows = validate_klines(kline_rows_from_zip(path), day, config["label"])
        open_sequences[key] = [row["open_ms"] for row in rows]
        markets[key] = {
            "market": config["label"],
            "source_archive": source,
            "validated_closed_1m_klines": len(rows),
            "windows": {
                name: window_stats(rows, minutes, end_utc)
                for name, minutes in WINDOWS.items()
            },
        }
    if open_sequences["spot"] != open_sequences["usd_m_futures"]:
        raise ValueError("Spot and USD-M Futures one-minute boundaries are not synchronized")

    return {
        "schema": "btc_binance_quote_cvd_v1",
        "generated_at_utc": iso_utc(dt.datetime.now(UTC)),
        "provider": "Binance Data Vision",
        "symbol": symbol,
        "quote_asset": "USDT",
        "interval": "1m",
        "source_closed_day_utc": day.isoformat(),
        "as_of_utc": iso_utc(end_utc),
        "range_start_utc": iso_utc(end_utc - dt.timedelta(days=1)),
        "data_freshness": "Completed UTC-day archives are published by Binance on the following day; this is verified closed-session data, not live intraday data.",
        "window_semantics": "Half-open UTC intervals [start_utc, end_utc_exclusive) using only checksum-verified closed one-minute Binance klines; both markets share the same boundaries.",
        "definitions": {
            "cvd_quote_usdt": "sum(taker_buy_quote_volume - taker_sell_quote_volume), where taker_sell_quote_volume = quote_volume - taker_buy_quote_volume",
            "taker_buy_share": "sum(taker_buy_quote_volume) / sum(quote_volume), in [0, 1]",
            "normalized_cvd": "cvd_quote_usdt / quote_volume_usdt = 2 * taker_buy_share - 1, in [-1, 1]",
        },
        "integrity": {
            "status": "passed",
            "required_closed_1m_klines_per_market": 1_440,
            "synchronized_utc_boundary": True,
            "archive_sha256_verified": True,
        },
        "markets": markets,
    }


def write_if_changed(data, destination):
    if destination.exists():
        try:
            current = json.loads(destination.read_text(encoding="utf-8"))
            if (
                current.get("schema") == data["schema"]
                and current.get("as_of_utc") == data["as_of_utc"]
                and current.get("integrity", {}).get("status") == "passed"
                and current.get("integrity", {}).get("archive_sha256_verified") is True
            ):
                return False
        except (OSError, ValueError):
            pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Completed UTC day YYYY-MM-DD; default is newest common published archive")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--cache", default=".cache/cvd")
    parser.add_argument("--output", default="output/latest_cvd.json")
    parser.add_argument("--lookback-days", type=int, default=4)
    args = parser.parse_args()

    cache = pathlib.Path(args.cache)
    if args.date:
        day = dt.date.fromisoformat(args.date)
        if day >= dt.datetime.now(UTC).date():
            raise SystemExit("--date must be a completed UTC day")
        downloaded = {
            market: download_market(market, args.symbol, day, cache)
            for market in ARCHIVES
        }
    else:
        day, downloaded = find_latest_common_day(
            dt.datetime.now(UTC).date(), args.symbol, cache, args.lookback_days
        )

    data = compute(day, downloaded, args.symbol)
    destination = pathlib.Path(args.output)
    changed = write_if_changed(data, destination)
    print(
        json.dumps(
            {
                "output": str(destination),
                "as_of_utc": data["as_of_utc"],
                "integrity": data["integrity"]["status"],
                "changed": changed,
            }
        )
    )


if __name__ == "__main__":
    main()
