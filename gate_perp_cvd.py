#!/usr/bin/env python3
"""Rolling Gate BTC_USDT perpetual CVD from public aggressor-side trades.

Gate's public futures trade feed identifies taker direction by the sign of
``size``.  Quote notional is reconstructed from the independently fetched
contract multiplier, trade size, and trade price.  The rolling state contains
only verified, closed one-minute aggregates and is never combined with Binance
Spot observations.
"""

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import pathlib
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation


UTC = dt.timezone.utc
MINUTE = dt.timedelta(minutes=1)
WINDOWS = {"1h": 60, "4h": 240, "24h": 1_440}
STATE_SCHEMA = "btc_gate_perp_cvd_state_v2"
LEGACY_STATE_SCHEMAS = {"btc_gate_perp_cvd_state_v1"}
PROVIDER = "Gate API v4"
MARKET = "Gate BTC_USDT USDT-settled perpetual"
CONTRACT = "BTC_USDT"
CONTRACT_ENDPOINT = "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT"
TRADES_ENDPOINT = "https://api.gateio.ws/api/v4/futures/usdt/trades"
PROVIDER_DOCUMENTATION = "https://www.gate.com/docs/developers/apiv4/en/futures/"
TRADE_PAGE_LIMIT = 1_000
# Gate accepts offset pagination for futures trades but returned HTTP 400 at a
# six-figure offset in a high-density production range.  Split the time range
# before issuing that offset, then prove each leaf range with a short page.
MAX_SAFE_PAGE_OFFSET = 100_000
DEFAULT_BACKFILL_MINUTES = 1_440
DEFAULT_MAX_INCREMENTAL_GAP_MINUTES = 60
DEFAULT_BACKFILL_CHUNK_MINUTES = 30
DEFAULT_BACKFILL_WORKERS = 4
DEFAULT_MAX_REQUESTS = 400
DEFAULT_RUNTIME_BUDGET_SECONDS = 150
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.075
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


class RuntimeBudgetExceeded(RuntimeError):
    """Raised before another API page would exceed the collector deadline."""


class RequestBudgetExceeded(RuntimeError):
    """Raised before another API page would exceed the per-run request cap."""


class CollectionBudget:
    def __init__(self, max_requests, runtime_seconds=None, monotonic=time.monotonic):
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        if runtime_seconds is not None and runtime_seconds <= 0:
            raise ValueError("runtime_seconds must be positive")
        self.max_requests = max_requests
        self.runtime_seconds = runtime_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self.started = monotonic()
        self.requests = 0

    @property
    def elapsed_seconds(self):
        return max(0.0, self._monotonic() - self.started)

    @property
    def remaining_requests(self):
        with self._lock:
            return max(0, self.max_requests - self.requests)

    @property
    def deadline_monotonic(self):
        if self.runtime_seconds is None:
            return None
        return self.started + self.runtime_seconds

    def consume_request(self):
        with self._lock:
            if self.requests >= self.max_requests:
                raise RequestBudgetExceeded(
                    f"Gate request budget exhausted at {self.max_requests} requests"
                )
            if self.runtime_seconds is not None and self.elapsed_seconds >= self.runtime_seconds:
                raise RuntimeBudgetExceeded(
                    f"Gate runtime budget exhausted at {self.runtime_seconds}s"
                )
            self.requests += 1


_last_request_started = 0.0
_request_pacing_lock = threading.Lock()


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if not number.is_finite():
        raise ValueError(f"invalid {field}")
    return number


def _safe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return f"URLError: {str(error.reason).replace(chr(10), ' ')[:160]}"
    text = str(error).replace("\n", " ").strip()[:200]
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def request_json(
    url,
    attempts=4,
    timeout=20,
    min_interval_seconds=DEFAULT_REQUEST_INTERVAL_SECONDS,
    deadline_monotonic=None,
    monotonic=time.monotonic,
    sleeper=time.sleep,
):
    """Request JSON with conservative pacing and bounded retry/backoff.

    Gate documents a 200 requests/10 seconds public-endpoint limit.  The
    default 75ms start-to-start interval stays below that ceiling even before
    network latency is considered.  Retry-After is honored for transient HTTP
    responses, and a collector deadline prevents retries from consuming the
    workflow's publish margin.
    """
    global _last_request_started
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "btc-lean-amt-gate-perp/1.0"},
    )
    for attempt in range(attempts):
        # Concurrent missing-range workers share one start-time limiter.  The
        # lock covers the wait and timestamp update so aggregate request starts
        # cannot exceed the configured rate even when network waits overlap.
        with _request_pacing_lock:
            now = monotonic()
            if deadline_monotonic is not None and now >= deadline_monotonic:
                raise RuntimeBudgetExceeded("Gate runtime budget exhausted before HTTP request")
            delay = max(0.0, min_interval_seconds - (now - _last_request_started))
            if deadline_monotonic is not None and now + delay >= deadline_monotonic:
                raise RuntimeBudgetExceeded("Gate runtime budget exhausted before request pacing")
            if delay:
                sleeper(delay)
            _last_request_started = monotonic()
        try:
            effective_timeout = timeout
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    raise RuntimeBudgetExceeded("Gate runtime budget exhausted before HTTP request")
                effective_timeout = max(1, min(timeout, remaining))
            with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_CODES or attempt + 1 == attempts:
                raise
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                backoff = max(2**attempt, float(retry_after))
            except (TypeError, ValueError):
                backoff = 2**attempt
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            backoff = 2**attempt
        backoff = min(backoff, 30)
        if deadline_monotonic is not None and monotonic() + backoff >= deadline_monotonic:
            raise RuntimeBudgetExceeded("Gate runtime budget exhausted during retry backoff")
        sleeper(backoff)
    raise RuntimeError("unreachable")


def fetch_contract(requester=request_json, budget=None):
    if budget is not None:
        budget.consume_request()
    payload = requester(CONTRACT_ENDPOINT)
    if not isinstance(payload, dict):
        raise ValueError("unexpected Gate contract response")
    multiplier = _decimal(payload.get("quanto_multiplier"), "contract multiplier")
    if (
        payload.get("name") != CONTRACT
        or payload.get("type") != "direct"
        or payload.get("status") != "trading"
        or payload.get("in_delisting") is not False
        or multiplier <= 0
    ):
        raise ValueError("Gate contract metadata did not validate as an active direct perpetual")
    return {
        "name": CONTRACT,
        "type": "direct",
        "settlement_asset": "USDT",
        "underlying_asset": "BTC",
        "quanto_multiplier_btc_per_contract": str(multiplier),
        "status": payload["status"],
        "in_delisting": False,
    }


def _trade_timestamp_seconds(row):
    value = row.get("create_time_ms", row.get("create_time"))
    timestamp = _decimal(value, "trade timestamp")
    # Gate documents create_time_ms as a decimal Unix-seconds value.  Tolerate
    # millisecond epoch values defensively without guessing for other scales.
    if timestamp >= Decimal("1000000000000"):
        timestamp /= Decimal(1_000)
    if timestamp < Decimal("1000000000"):
        raise ValueError("trade timestamp is outside the supported Unix range")
    return timestamp


def _validate_trade(row):
    if not isinstance(row, dict) or row.get("contract") != CONTRACT:
        raise ValueError("unexpected Gate trade record")
    trade_id = int(row["id"])
    timestamp = _trade_timestamp_seconds(row)
    size = _decimal(row.get("size"), "trade size")
    price = _decimal(row.get("price"), "trade price")
    if size == 0 or price <= 0:
        raise ValueError("Gate trade size/price is invalid")
    return {
        "id": trade_id,
        "timestamp_seconds": timestamp,
        "size": size,
        "price": price,
    }


def fetch_trades_range(
    start_utc,
    end_utc,
    requester=request_json,
    page_limit=TRADE_PAGE_LIMIT,
    max_page_offset=MAX_SAFE_PAGE_OFFSET,
    max_requests=DEFAULT_MAX_REQUESTS,
    budget=None,
):
    """Fetch a provably untruncated half-open range with offset pagination.

    Gate truncates a range when more than ``limit`` records match and documents
    ``offset`` for this endpoint.  The closed historical range is paged until a
    short page proves exhaustion; duplicate trade ids are filtered defensively.
    """
    if start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc:
        raise ValueError("trade range must be a positive timezone-aware interval")
    if not 1 <= page_limit <= TRADE_PAGE_LIMIT:
        raise ValueError(f"page_limit must be in [1, {TRADE_PAGE_LIMIT}]")
    if max_page_offset < page_limit:
        raise ValueError("max_page_offset must be at least one page")
    start_second = int(start_utc.timestamp())
    end_second = int(end_utc.timestamp())
    if start_utc.second or start_utc.microsecond or end_utc.second or end_utc.microsecond:
        raise ValueError("trade range must use exact second boundaries")

    owned_budget = budget is None
    budget = budget or CollectionBudget(max_requests)
    requests_before = budget.requests
    duplicates = 0
    split_count = 0
    terminal_offsets = []

    def fetch_partition(left_second, right_second):
        nonlocal duplicates, split_count
        partition = {}
        offset = 0
        while True:
            if owned_budget and budget.requests - requests_before >= max_requests:
                raise RequestBudgetExceeded(
                    f"Gate trade pagination exceeded {max_requests} requests"
                )
            budget.consume_request()
            query = urllib.parse.urlencode(
                {
                    "contract": CONTRACT,
                    "from": left_second,
                    "to": right_second,
                    "limit": page_limit,
                    "offset": offset,
                }
            )
            page = requester(f"{TRADES_ENDPOINT}?{query}")
            if not isinstance(page, list):
                raise ValueError("unexpected Gate trades response")
            page_new_ids = 0
            for raw in page:
                trade = _validate_trade(raw)
                timestamp = trade["timestamp_seconds"]
                if timestamp < left_second or timestamp >= right_second:
                    continue
                if trade["id"] in partition:
                    duplicates += 1
                else:
                    page_new_ids += 1
                partition[trade["id"]] = trade
            if len(page) < page_limit:
                terminal_offsets.append(offset)
                return partition
            if page_new_ids == 0:
                raise RuntimeError("Gate offset pagination made no progress")
            offset += len(page)
            if offset >= max_page_offset:
                midpoint = left_second + (right_second - left_second) // 2
                if midpoint <= left_second or midpoint >= right_second:
                    raise RuntimeError(
                        "Gate trade density exceeds provable offset pagination for one second"
                    )
                # Discard the unproven prefix and verify two disjoint time
                # partitions independently.  Request accounting still includes
                # the abandoned pages so the shared cap remains conservative.
                split_count += 1
                left = fetch_partition(left_second, midpoint)
                right = fetch_partition(midpoint, right_second)
                overlap = set(left).intersection(right)
                if overlap:
                    raise ValueError("adaptive Gate trade partitions overlap")
                left.update(right)
                return left

    trades = fetch_partition(start_second, end_second)

    ordered = sorted(trades.values(), key=lambda item: (item["timestamp_seconds"], item["id"]))
    return ordered, {
        "request_count": budget.requests - requests_before,
        "verified_untruncated_segments": len(terminal_offsets),
        "unique_trades": len(ordered),
        "duplicate_trade_ids_filtered": duplicates,
        "page_limit": page_limit,
        "max_safe_page_offset": max_page_offset,
        "pagination_method": (
            "adaptive_time_split_offset_until_short_page"
            if split_count
            else "offset_until_short_page"
        ),
        "time_split_count": split_count,
        "terminal_short_page_observed": True,
        "terminal_offset": max(terminal_offsets),
        "terminal_offsets": terminal_offsets,
        "range_start_utc": iso_utc(start_utc),
        "range_end_utc_exclusive": iso_utc(end_utc),
    }


def aggregate_minutes(trades, start_utc, end_utc, multiplier):
    buckets = {}
    cursor = start_utc
    while cursor < end_utc:
        buckets[iso_utc(cursor)] = {
            "open_utc": iso_utc(cursor),
            "quote_volume_usdt": Decimal(0),
            "taker_buy_quote_volume_usdt": Decimal(0),
            "trade_count": 0,
        }
        cursor += MINUTE

    start_second = Decimal(str(start_utc.timestamp()))
    end_second = Decimal(str(end_utc.timestamp()))
    for trade in trades:
        timestamp = trade["timestamp_seconds"]
        if timestamp < start_second or timestamp >= end_second:
            raise ValueError("trade fell outside the aggregation range")
        minute_second = int(timestamp // 60) * 60
        key = iso_utc(dt.datetime.fromtimestamp(minute_second, UTC))
        bucket = buckets.get(key)
        if bucket is None:
            raise ValueError("trade minute was not initialized")
        quote_notional = abs(trade["size"]) * multiplier * trade["price"]
        bucket["quote_volume_usdt"] += quote_notional
        if trade["size"] > 0:
            bucket["taker_buy_quote_volume_usdt"] += quote_notional
        bucket["trade_count"] += 1
    return buckets


def _serialized_bucket(bucket):
    return {
        "open_utc": bucket["open_utc"],
        "quote_volume_usdt": str(bucket["quote_volume_usdt"]),
        "taker_buy_quote_volume_usdt": str(bucket["taker_buy_quote_volume_usdt"]),
        "trade_count": int(bucket["trade_count"]),
    }


def _bucket_payload_sha256(rows):
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_state(path, multiplier):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = data.get("schema")
    if (
        schema not in {STATE_SCHEMA, *LEGACY_STATE_SCHEMAS}
        or data.get("provider") != PROVIDER
        or data.get("contract") != CONTRACT
        or _decimal(data.get("contract_multiplier_btc"), "state multiplier") != multiplier
    ):
        raise ValueError("existing Gate CVD state provenance does not match the active contract")
    raw_buckets = data.get("closed_minute_buckets")
    if not isinstance(raw_buckets, list) or not raw_buckets:
        raise ValueError("state contains no closed-minute buckets")
    if schema == STATE_SCHEMA:
        declared_digest = data.get("bucket_payload_sha256")
        if declared_digest != _bucket_payload_sha256(raw_buckets):
            raise ValueError("state bucket payload checksum mismatch")
    buckets = {}
    for raw in raw_buckets:
        if not isinstance(raw, dict):
            raise ValueError("state bucket is malformed")
        opened = parse_utc(raw["open_utc"])
        if opened.second or opened.microsecond:
            raise ValueError("state bucket is not minute-aligned")
        quote = _decimal(raw["quote_volume_usdt"], "state quote volume")
        buy = _decimal(raw["taker_buy_quote_volume_usdt"], "state taker-buy volume")
        count = int(raw["trade_count"])
        if quote < 0 or buy < 0 or buy > quote or count < 0:
            raise ValueError("state bucket values are invalid")
        key = iso_utc(opened)
        if key in buckets:
            raise ValueError("state contains a duplicate minute")
        buckets[key] = {
            "open_utc": key,
            "quote_volume_usdt": quote,
            "taker_buy_quote_volume_usdt": buy,
            "trade_count": count,
        }
    declared_count = data.get(
        "stored_closed_1m_buckets",
        data.get("validated_closed_1m_buckets"),
    )
    if declared_count is not None and int(declared_count) != len(buckets):
        raise ValueError("state bucket count does not match its payload")
    return buckets


def load_state_recoverable(path, multiplier):
    if not path.exists():
        return {}, {
            "status": "missing",
            "schema": None,
            "error": None,
            "loaded_buckets": 0,
        }
    schema = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        schema = raw.get("schema") if isinstance(raw, dict) else None
        buckets = load_state(path, multiplier)
        return buckets, {
            "status": "migrated" if schema in LEGACY_STATE_SCHEMAS else "loaded",
            "schema": schema,
            "error": None,
            "loaded_buckets": len(buckets),
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {}, {
            "status": "rejected",
            "schema": schema,
            "error": _safe_error(error),
            "loaded_buckets": 0,
        }


def trailing_buckets(buckets, as_of_utc, limit=1_440):
    result = []
    cursor = as_of_utc - MINUTE
    while len(result) < limit:
        bucket = buckets.get(iso_utc(cursor))
        if bucket is None:
            break
        result.append(bucket)
        cursor -= MINUTE
    result.reverse()
    return result


def _missing_ranges(buckets, start_utc, end_utc):
    """Return missing minute-aligned half-open ranges in chronological order."""
    ranges = []
    missing_start = None
    cursor = start_utc
    while cursor < end_utc:
        missing = iso_utc(cursor) not in buckets
        if missing and missing_start is None:
            missing_start = cursor
        elif not missing and missing_start is not None:
            ranges.append((missing_start, cursor))
            missing_start = None
        cursor += MINUTE
    if missing_start is not None:
        ranges.append((missing_start, end_utc))
    return ranges


def _validate_contiguous_tail(rows, as_of_utc):
    expected = as_of_utc - dt.timedelta(minutes=len(rows))
    for row in rows:
        opened = parse_utc(row["open_utc"])
        if opened != expected:
            raise ValueError("verified Gate state tail is not UTC-minute contiguous")
        expected += MINUTE
    if expected != as_of_utc:
        raise ValueError("verified Gate state tail does not end at the requested boundary")


def _write_state(path, buckets, multiplier, target_start, target_end):
    ordered = [
        buckets[key]
        for key in sorted(buckets, key=parse_utc)
        if target_start <= parse_utc(key) < target_end
    ]
    contiguous = trailing_buckets(buckets, target_end)
    missing = _missing_ranges(buckets, target_start, target_end)
    serialized = [_serialized_bucket(row) for row in ordered]
    state = {
        "schema": STATE_SCHEMA,
        "provider": PROVIDER,
        "provider_documentation": PROVIDER_DOCUMENTATION,
        "contract": CONTRACT,
        "contract_multiplier_btc": str(multiplier),
        "updated_at_utc": iso_utc(dt.datetime.now(UTC)),
        "target_start_utc": iso_utc(target_start),
        "target_end_utc_exclusive": iso_utc(target_end),
        "stored_closed_1m_buckets": len(ordered),
        "bucket_payload_sha256": _bucket_payload_sha256(serialized),
        "contiguous_tail_closed_1m_buckets": len(contiguous),
        "missing_closed_1m_buckets": 1_440 - len(ordered),
        "target_window_continuity": "passed" if len(contiguous) == 1_440 else "incomplete",
        "missing_ranges": [
            {
                "start_utc": iso_utc(start),
                "end_utc_exclusive": iso_utc(end),
                "minutes": int((end - start).total_seconds() // 60),
            }
            for start, end in missing
        ],
        "closed_minute_buckets": serialized,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump(state, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        # Persist the directory entry where supported.  Windows does not allow
        # opening directories this way, so the atomic replace above remains the
        # available guarantee there.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return state


def _missing_chunks(buckets, start_utc, end_utc, chunk_minutes, requested_minutes):
    """Plan disjoint missing segments newest-first without touching verified buckets."""
    chunks = []
    remaining = requested_minutes
    for range_start, range_end in reversed(_missing_ranges(buckets, start_utc, end_utc)):
        cursor = range_end
        while cursor > range_start and remaining > 0:
            minutes = min(
                chunk_minutes,
                remaining,
                int((cursor - range_start).total_seconds() // 60),
            )
            chunk_start = cursor - dt.timedelta(minutes=minutes)
            chunks.append((chunk_start, cursor))
            cursor = chunk_start
            remaining -= minutes
        if remaining == 0:
            break
    return chunks


def _unavailable_window(minutes, end_utc, reason):
    return {
        "status": "unavailable",
        "start_utc": iso_utc(end_utc - dt.timedelta(minutes=minutes)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": "N/A",
        "quote_volume_usdt": "N/A",
        "taker_buy_quote_volume_usdt": "N/A",
        "taker_sell_quote_volume_usdt": "N/A",
        "cvd_quote_usdt": "N/A",
        "taker_buy_share": "N/A",
        "taker_buy_share_pct": "N/A",
        "normalized_cvd": "N/A",
        "error": reason,
    }


def _window_stats(rows, minutes, end_utc):
    if len(rows) < minutes:
        return _unavailable_window(
            minutes,
            end_utc,
            f"insufficient verified rolling coverage: {len(rows)}/{minutes} closed minutes",
        )
    selected = rows[-minutes:]
    quote = sum((row["quote_volume_usdt"] for row in selected), Decimal(0))
    buy = sum((row["taker_buy_quote_volume_usdt"] for row in selected), Decimal(0))
    if quote <= 0:
        return _unavailable_window(minutes, end_utc, "verified window has zero quote volume")
    sell = quote - buy
    cvd = buy - sell
    share = buy / quote
    return {
        "status": "available",
        "start_utc": iso_utc(end_utc - dt.timedelta(minutes=minutes)),
        "end_utc_exclusive": iso_utc(end_utc),
        "closed_klines": minutes,
        "quote_volume_usdt": round(float(quote), 8),
        "taker_buy_quote_volume_usdt": round(float(buy), 8),
        "taker_sell_quote_volume_usdt": round(float(sell), 8),
        "cvd_quote_usdt": round(float(cvd), 8),
        "taker_buy_share": round(float(share), 10),
        "taker_buy_share_pct": round(float(share * 100), 8),
        "normalized_cvd": round(float(cvd / quote), 10),
        "error": None,
    }


def collect_gate_perpetual(
    as_of_utc,
    state_path,
    bootstrap_minutes=60,
    backfill_minutes=DEFAULT_BACKFILL_MINUTES,
    max_incremental_gap_minutes=DEFAULT_MAX_INCREMENTAL_GAP_MINUTES,
    backfill_chunk_minutes=DEFAULT_BACKFILL_CHUNK_MINUTES,
    backfill_workers=DEFAULT_BACKFILL_WORKERS,
    overlap_minutes=10,
    requester=request_json,
    max_requests=DEFAULT_MAX_REQUESTS,
    runtime_budget_seconds=DEFAULT_RUNTIME_BUDGET_SECONDS,
):
    if as_of_utc.tzinfo is None or as_of_utc.second or as_of_utc.microsecond:
        raise ValueError("as_of_utc must be an exact timezone-aware minute boundary")
    as_of_utc = as_of_utc.astimezone(UTC)
    state_path = pathlib.Path(state_path)
    desired_start = as_of_utc - dt.timedelta(minutes=1_440)
    if not 1 <= bootstrap_minutes <= 1_440:
        raise ValueError("bootstrap_minutes must be in [1, 1440]")
    if not 0 <= backfill_minutes <= 1_440:
        raise ValueError("backfill_minutes must be in [0, 1440]")
    if max_incremental_gap_minutes < 1:
        raise ValueError("max_incremental_gap_minutes must be positive")
    if not 1 <= backfill_chunk_minutes <= 1_440:
        raise ValueError("backfill_chunk_minutes must be in [1, 1440]")
    if not 1 <= backfill_workers <= 16:
        raise ValueError("backfill_workers must be in [1, 16]")
    if overlap_minutes < 1:
        raise ValueError("overlap_minutes must be positive")
    budget = CollectionBudget(max_requests, runtime_budget_seconds)
    if requester is request_json:
        def active_requester(url):
            return request_json(url, deadline_monotonic=budget.deadline_monotonic)
    else:
        active_requester = requester

    contract = fetch_contract(active_requester, budget=budget)
    multiplier = _decimal(contract["quanto_multiplier_btc_per_contract"], "contract multiplier")
    loaded, state_load = load_state_recoverable(state_path, multiplier)
    retained = {
        key: value
        for key, value in loaded.items()
        if desired_start <= parse_utc(key) < as_of_utc
    }
    discarded_outside_target = len(loaded) - len(retained)
    persisted_buckets_retained = len(retained)
    latest_open = max((parse_utc(key) for key in retained), default=None)
    gap_minutes = (
        max(0, int((as_of_utc - (latest_open + MINUTE)).total_seconds() // 60))
        if latest_open is not None
        else None
    )
    if latest_open is None:
        recovery_mode = "bootstrap"
        fetch_start = max(desired_start, as_of_utc - dt.timedelta(minutes=bootstrap_minutes))
    elif gap_minutes > max_incremental_gap_minutes:
        # Preserve every validated in-window bucket.  Refresh a recent tail so
        # the snapshot stays current, then reconcile the gap newest-first.
        recovery_mode = "gap_recovery"
        fetch_start = max(desired_start, as_of_utc - dt.timedelta(minutes=bootstrap_minutes))
    else:
        recovery_mode = "incremental"
        fetch_start = max(desired_start, latest_open - dt.timedelta(minutes=overlap_minutes - 1))

    trades, refresh_evidence = fetch_trades_range(
        fetch_start,
        as_of_utc,
        requester=active_requester,
        max_requests=max_requests,
        budget=budget,
    )
    replacement = aggregate_minutes(trades, fetch_start, as_of_utc, multiplier)
    retained.update(replacement)
    rows = trailing_buckets(retained, as_of_utc)
    if not rows:
        raise ValueError("Gate returned no verifiable closed-minute coverage")
    _validate_contiguous_tail(rows, as_of_utc)

    latest_trade = max((trade["timestamp_seconds"] for trade in trades), default=None)
    if latest_trade is None:
        raise ValueError("Gate returned no trades in the refreshed range")
    latest_trade_utc = dt.datetime.fromtimestamp(float(latest_trade), UTC)
    if latest_trade_utc >= as_of_utc:
        raise ValueError("Gate source trade is not inside the requested closed range")
    if latest_trade_utc < as_of_utc - dt.timedelta(minutes=2):
        raise ValueError("Gate latest trade is too old for the requested intraday boundary")
    checkpoint_count = 1
    state = _write_state(state_path, retained, multiplier, desired_start, as_of_utc)

    initial_missing_ranges = _missing_ranges(retained, desired_start, as_of_utc)
    missing_minutes = sum(
        int((end - start).total_seconds() // 60)
        for start, end in initial_missing_ranges
    )
    backfill_evidence = {
        "status": "not_needed" if missing_minutes == 0 else "disabled",
        "requested_minutes": 0,
        "completed_minutes": 0,
        "error": None,
        "request_count": 0,
        "verified_untruncated_segments": 0,
        "unique_trades": 0,
        "duplicate_trade_ids_filtered": 0,
        "time_split_count": 0,
        "chunks": [],
    }
    if missing_minutes > 0 and backfill_minutes > 0:
        requested_minutes = min(backfill_minutes, missing_minutes)
        completed_minutes = 0
        verified_chunks = []
        backfill_errors = []
        backfill_exceptions = []
        requests_before_backfill = budget.requests
        planned_chunks = _missing_chunks(
            retained,
            desired_start,
            as_of_utc,
            backfill_chunk_minutes,
            requested_minutes,
        )

        def fetch_missing_chunk(chunk):
            chunk_start, chunk_end = chunk
            chunk_trades, chunk_evidence = fetch_trades_range(
                chunk_start,
                chunk_end,
                requester=active_requester,
                max_requests=max_requests,
                budget=budget,
            )
            chunk_buckets = aggregate_minutes(
                chunk_trades,
                chunk_start,
                chunk_end,
                multiplier,
            )
            return chunk_start, chunk_end, chunk_buckets, chunk_evidence

        # Network waits, not CPU work, dominated the observed 210-second run.
        # Fetch independent missing segments concurrently while the shared
        # budget and start-time limiter enforce one global cap and rate.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(backfill_workers, len(planned_chunks))
        ) as executor:
            futures = {
                executor.submit(fetch_missing_chunk, chunk): chunk
                for chunk in planned_chunks
            }
            for future in concurrent.futures.as_completed(futures):
                chunk_start, chunk_end = futures[future]
                try:
                    _, _, chunk_buckets, chunk_evidence = future.result()
                    # The plan was derived exclusively from missing ranges.  A
                    # second guard prevents a future refactor from overwriting
                    # an already verified minute during recovery.
                    if any(key in retained for key in chunk_buckets):
                        raise ValueError("backfill segment overlaps a verified state bucket")
                    retained.update(chunk_buckets)
                    state = _write_state(
                        state_path,
                        retained,
                        multiplier,
                        desired_start,
                        as_of_utc,
                    )
                    checkpoint_count += 1
                    completed_minutes += int((chunk_end - chunk_start).total_seconds() // 60)
                    verified_chunks.append(chunk_evidence)
                except (
                    KeyError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    TimeoutError,
                ) as error:
                    backfill_exceptions.append(error)
                    backfill_errors.append(_safe_error(error))

        verified_chunks.sort(key=lambda item: item["range_start_utc"], reverse=True)
        rows = trailing_buckets(retained, as_of_utc)
        _validate_contiguous_tail(rows, as_of_utc)

        backfill_requests = budget.requests - requests_before_backfill
        if completed_minutes == requested_minutes:
            backfill_status = "passed"
        elif completed_minutes > 0:
            backfill_status = "partial"
        elif budget.remaining_requests < 1 or isinstance(
            backfill_exceptions[0] if backfill_exceptions else None,
            (RuntimeBudgetExceeded, RequestBudgetExceeded),
        ):
            backfill_status = "deferred"
        else:
            backfill_status = "failed"
        backfill_evidence = {
            "status": backfill_status,
            "requested_minutes": requested_minutes,
            "completed_minutes": completed_minutes,
            "error": backfill_errors[0] if backfill_errors else (
                "per-run collection budget exhausted"
                if completed_minutes < requested_minutes
                else None
            ),
            "request_count": backfill_requests,
            "verified_untruncated_segments": sum(
                item["verified_untruncated_segments"] for item in verified_chunks
            ),
            "unique_trades": sum(item["unique_trades"] for item in verified_chunks),
            "duplicate_trade_ids_filtered": sum(
                item["duplicate_trade_ids_filtered"] for item in verified_chunks
            ),
            "time_split_count": sum(item["time_split_count"] for item in verified_chunks),
            "page_limit": TRADE_PAGE_LIMIT,
            "pagination_method": (
                "adaptive_time_split_offset_until_short_page"
                if any(item["time_split_count"] for item in verified_chunks)
                else "offset_until_short_page"
            ),
            "worker_count": min(backfill_workers, len(planned_chunks)),
            "planned_segments": len(planned_chunks),
            "range_start_utc": rows[0]["open_utc"] if completed_minutes else None,
            "range_end_utc_exclusive": (
                verified_chunks[0]["range_end_utc_exclusive"] if verified_chunks else None
            ),
            "chunks": verified_chunks,
        }

    rows = trailing_buckets(retained, as_of_utc)
    _validate_contiguous_tail(rows, as_of_utc)
    total_requests = budget.requests
    total_segments = refresh_evidence["verified_untruncated_segments"] + int(
        backfill_evidence.get("verified_untruncated_segments", 0)
    )
    total_unique_trades = refresh_evidence["unique_trades"] + int(
        backfill_evidence.get("unique_trades", 0)
    )
    total_duplicates = refresh_evidence["duplicate_trade_ids_filtered"] + int(
        backfill_evidence.get("duplicate_trade_ids_filtered", 0)
    )
    total_time_splits = refresh_evidence["time_split_count"] + int(
        backfill_evidence.get("time_split_count", 0)
    )
    included_range_start = rows[0]["open_utc"]

    windows = {
        name: _window_stats(rows, minutes, as_of_utc)
        for name, minutes in WINDOWS.items()
    }
    fully_available = len(rows) == 1_440 and all(
        window["status"] == "available" for window in windows.values()
    )
    return {
        "status": "available" if fully_available else "partial",
        "market": MARKET,
        "exchange": "Gate",
        "provider": PROVIDER,
        "provider_documentation": PROVIDER_DOCUMENTATION,
        "source_endpoint": TRADES_ENDPOINT,
        "contract_metadata_endpoint": CONTRACT_ENDPOINT,
        "endpoints_attempted": [CONTRACT_ENDPOINT, TRADES_ENDPOINT],
        "source_timestamp_utc": iso_utc(latest_trade_utc),
        "contract": contract,
        "coverage": {
            "start_utc": rows[0]["open_utc"],
            "end_utc_exclusive": iso_utc(as_of_utc),
            "expected_closed_1m_klines": 1_440,
            "validated_closed_1m_klines": len(rows),
            "candle_completeness_pct": round(len(rows) / 1_440 * 100, 8),
        },
        "aggressor_side_rule": "positive trade size = taker buy; negative trade size = taker sell",
        "quote_notional_formula": "abs(size_contracts) * quanto_multiplier_btc_per_contract * price_usdt",
        "windows": windows,
        "fetch_integrity": {
            "status": "passed",
            "state_schema": STATE_SCHEMA,
            "time_range_untruncated": True,
            "state_continuity_verified": True,
            "target_window_continuity": "passed" if len(rows) == 1_440 else "incomplete",
            "state_reset_reason": None,
            "state_load": state_load,
            "state_recovery_mode": recovery_mode,
            "detected_state_gap_minutes": gap_minutes,
            "persisted_buckets_retained": persisted_buckets_retained,
            "discarded_buckets_outside_target": discarded_outside_target,
            "checkpoint_count": checkpoint_count,
            "stored_verified_minutes": state["stored_closed_1m_buckets"],
            "missing_verified_minutes": state["missing_closed_1m_buckets"],
            "request_count": total_requests,
            "request_budget": max_requests,
            "request_budget_remaining": budget.remaining_requests,
            "runtime_budget_seconds": runtime_budget_seconds,
            "runtime_elapsed_seconds": round(budget.elapsed_seconds, 3),
            "backfill_workers": backfill_workers,
            "verified_untruncated_segments": total_segments,
            "unique_trades": total_unique_trades,
            "duplicate_trade_ids_filtered": total_duplicates,
            "time_split_count": total_time_splits,
            "page_limit": TRADE_PAGE_LIMIT,
            "range_start_utc": included_range_start,
            "range_end_utc_exclusive": iso_utc(as_of_utc),
            "current_refresh": refresh_evidence,
            "historical_backfill": backfill_evidence,
        },
        "error": None,
    }


__all__ = [
    "CONTRACT",
    "CONTRACT_ENDPOINT",
    "DEFAULT_BACKFILL_MINUTES",
    "DEFAULT_BACKFILL_CHUNK_MINUTES",
    "DEFAULT_BACKFILL_WORKERS",
    "DEFAULT_MAX_INCREMENTAL_GAP_MINUTES",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_RUNTIME_BUDGET_SECONDS",
    "MARKET",
    "PROVIDER",
    "PROVIDER_DOCUMENTATION",
    "STATE_SCHEMA",
    "TRADES_ENDPOINT",
    "CollectionBudget",
    "RequestBudgetExceeded",
    "RuntimeBudgetExceeded",
    "aggregate_minutes",
    "collect_gate_perpetual",
    "fetch_contract",
    "fetch_trades_range",
    "load_state",
    "trailing_buckets",
]
