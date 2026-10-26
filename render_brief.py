#!/usr/bin/env python3
"""Render the verified JSON as a small, dashboard-ready public Markdown brief."""
import argparse
import json
import pathlib


def money(value):
    return f"${float(value):,.2f}"


def render(data):
    daily = data["daily_trade_volume_profile"]
    migration = data["daily_poc_migration"]
    weekly = data["previous_completed_week_trade_volume_profile"]
    integrity = data["integrity_cross_check"]
    if integrity.get("status") != "passed":
        raise ValueError("Daily integrity check did not pass")
    if weekly.get("integrity_status") != "passed":
        raise ValueError("Weekly integrity check did not pass")
    if migration.get("to_day_utc") != data["as_of_closed_day_utc"]:
        raise ValueError("POC migration date does not match as-of date")

    return f"""# BTC Lean AMT Daily Brief

**Status:** VERIFIED — Binance Spot archive checksum and daily/weekly kline cross-checks passed.

- Report reference date (UTC): **{data['report_reference_date_utc']}**
- Last completed session (UTC): **{data['as_of_closed_day_utc']}**
- Generated at: **{data['generated_at_utc']}**

## Daily profile — {data['as_of_closed_day_utc']} UTC

- VAH: **{money(daily['vah_usdt'])}**
- VAL: **{money(daily['val_usdt'])}**
- POC: **{money(daily['poc_usdt'])}**
- VWAP: **{money(daily['vwap_usdt'])}**
- Value-area share: **{daily['value_area_actual_share']:.4%}**

## Daily POC migration

- {migration['from_day_utc']} → {migration['to_day_utc']}: **{money(migration['previous_poc_usdt'])} → {money(migration['current_poc_usdt'])}**
- Direction: **{migration['direction']}** ({migration['delta_usdt']:+,.2f} USDT)

## Previous completed UTC week — {weekly['from_utc']} to {weekly['to_utc']}

- VAH: **{money(weekly['vah_usdt'])}**
- VAL: **{money(weekly['val_usdt'])}**
- POC: **{money(weekly['poc_usdt'])}**
- VWAP: **{money(weekly['vwap_usdt'])}**
- Value-area share: **{weekly['value_area_actual_share']:.4%}**

## Longer VWAP references (through the last completed UTC day)

- Developing month VWAP: **{money(data['vwap']['developing_month_vwap_to_last_closed_utc_day'])}**
- Previous completed month VWAP: **{money(data['vwap']['previous_completed_month_vwap'])}**
- Rolling 365 completed UTC days VWAP: **{money(data['vwap']['rolling_365_complete_utc_days_vwap'])}**

Historical closed-session data only; not a live price, order-book signal, or trade recommendation. The exact calculation method and source URLs are in `latest.json`.
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="output/latest.json")
    parser.add_argument("--output", default="output/latest.md")
    args = parser.parse_args()
    source = pathlib.Path(args.input)
    destination = pathlib.Path(args.output)
    data = json.loads(source.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(data), encoding="utf-8")
    print(json.dumps({"as_of": data["as_of_closed_day_utc"], "output": str(destination)}))


if __name__ == "__main__":
    main()
