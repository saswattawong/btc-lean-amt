#!/usr/bin/env python3
"""Record pre-collection intraday freshness observations and rolling SLO."""

import argparse
import datetime as dt
import json
import os
import pathlib


UTC = dt.timezone.utc
SCHEMA = "btc_intraday_freshness_slo_v1"


def iso_utc(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def observe(snapshot, ledger=None, observed_at=None, target_age_seconds=900, retain=2_016):
    observed_at = (observed_at or dt.datetime.now(UTC)).astimezone(UTC)
    freshness = snapshot.get("freshness", {})
    source = parse_utc(freshness["source_as_of_utc"])
    generated = parse_utc(snapshot["generated_at_utc"])
    if source > generated + dt.timedelta(seconds=5) or generated > observed_at + dt.timedelta(seconds=5):
        raise ValueError("snapshot timestamps are not chronologically valid")
    source_age = max(0, int((observed_at - source).total_seconds()))
    observation = {
        "observed_at_utc": iso_utc(observed_at),
        "source_as_of_utc": iso_utc(source),
        "snapshot_generated_at_utc": iso_utc(generated),
        "source_age_seconds": source_age,
        "target_age_seconds": target_age_seconds,
        "within_target": source_age <= target_age_seconds,
        "snapshot_schema": snapshot.get("schema"),
        "snapshot_integrity": snapshot.get("integrity", {}).get("status", "unknown"),
    }

    observations = []
    if ledger:
        if ledger.get("schema") != SCHEMA:
            raise ValueError("unexpected freshness ledger schema")
        observations = list(ledger.get("observations", []))
    observations.append(observation)
    observations = observations[-retain:]

    rolling = observations[-288:]
    passed = sum(item.get("within_target") is True for item in rolling)
    rate = round(passed / len(rolling) * 100, 6) if rolling else 0.0
    result = {
        "schema": SCHEMA,
        "updated_at_utc": iso_utc(observed_at),
        "measurement": "Age of the previously published source_as_of timestamp at collector start.",
        "target": {
            "source_age_seconds_lte": target_age_seconds,
            "pass_rate_pct_gte": 95.0,
            "rolling_observations": 288,
        },
        "rolling_24h": {
            "observations": len(rolling),
            "passed": passed,
            "failed": len(rolling) - passed,
            "pass_rate_pct": rate,
            "target_met": rate >= 95.0 if len(rolling) == 288 else "insufficient_observations",
        },
        "latest_observation": observation,
        "observations": observations,
    }
    return result


def _append(path, text):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    parser.add_argument("--ledger", default="output/intraday_freshness.json")
    parser.add_argument("--target-age-seconds", type=int, default=900)
    parser.add_argument("--retain", type=int, default=2_016)
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--github-summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args()

    snapshot = json.loads(pathlib.Path(args.snapshot).read_text(encoding="utf-8"))
    ledger_path = pathlib.Path(args.ledger)
    existing = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else None
    result = observe(
        snapshot,
        existing,
        target_age_seconds=args.target_age_seconds,
        retain=args.retain,
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    latest = result["latest_observation"]
    rolling = result["rolling_24h"]
    _append(
        args.github_output,
        f"within_target={str(latest['within_target']).lower()}\n"
        f"source_age_seconds={latest['source_age_seconds']}\n"
        f"rolling_pass_rate_pct={rolling['pass_rate_pct']}\n",
    )
    _append(
        args.github_summary,
        "## Intraday freshness before collection\n\n"
        f"- Source as of: `{latest['source_as_of_utc']}`\n"
        f"- Age: `{latest['source_age_seconds']}s` (target `<= {args.target_age_seconds}s`)\n"
        f"- Within target: `{latest['within_target']}`\n"
        f"- Rolling observations: `{rolling['observations']}/288`\n"
        f"- Rolling pass rate: `{rolling['pass_rate_pct']}%` (target `>= 95%`)\n",
    )
    print(json.dumps({"latest": latest, "rolling_24h": rolling}, sort_keys=True))


if __name__ == "__main__":
    main()
