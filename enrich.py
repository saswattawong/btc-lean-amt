#!/usr/bin/env python3
"""Add report-date-correct levels, daily POC migration, and last completed UTC-week trade profile.
Requires calculate.py to have generated output/latest.json first.
"""
import argparse, datetime as dt, itertools, json, pathlib, zipfile
from decimal import Decimal
from calculate import UTC, download_archive, trade_rows_from_csv, volume_profile, daily_klines

def trade_iterator(symbol, days, cache):
    for day in days:
        path, _ = download_archive(symbol, day, cache)
        with zipfile.ZipFile(path) as z:
            names = [name for name in z.namelist() if name.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"Archive for {day} needs exactly one CSV")
            with z.open(names[0]) as f:
                yield from trade_rows_from_csv(f, day)

def enrich(data, cache, include_weekly=True):
    day = dt.date.fromisoformat(data["as_of_closed_day_utc"])
    report_day = day + dt.timedelta(days=1)
    symbol = data["symbol"]
    klines, _ = daily_klines(symbol, day)
    day_rows = {r["date"]:r for r in klines}
    if day not in day_rows:
        raise ValueError("Missing last closed UTC daily kline")
    yesterday = day_rows[day]
    week_start = report_day - dt.timedelta(days=report_day.weekday()+7)
    week_days = [week_start + dt.timedelta(days=i) for i in range(7)]
    if not all(d in day_rows for d in week_days):
        raise ValueError("Missing a previous completed UTC week daily kline")
    weekly_rows = [day_rows[d] for d in week_days]
    data["report_reference_date_utc"] = report_day.isoformat()
    data["levels"] = {
      "previous_day": {"date":day.isoformat(), "high":yesterday["high"],"low":yesterday["low"],"close":yesterday["close"]},
      "previous_completed_utc_week": {"from":week_days[0].isoformat(),"to":week_days[-1].isoformat(),
          "high":max(x["high"] for x in weekly_rows),"low":min(x["low"] for x in weekly_rows)}
    }
    prior = day-dt.timedelta(days=1)
    prev_profile = volume_profile(trade_iterator(symbol,[prior],cache),Decimal(str(data["daily_trade_volume_profile"]["price_bucket_usdt"])))
    curr_poc = data["daily_trade_volume_profile"]["poc_usdt"]
    prev_poc = prev_profile["poc_usdt"]
    data["daily_poc_migration"] = {"from_day_utc":prior.isoformat(),"to_day_utc":day.isoformat(),
        "previous_poc_usdt":prev_poc,"current_poc_usdt":curr_poc,"delta_usdt":round(curr_poc-prev_poc,2),
        "direction":"higher" if curr_poc>prev_poc else "lower" if curr_poc<prev_poc else "unchanged",
        "method":"Same Binance spot aggTrades, same price bucket and value-area rules"}
    if include_weekly:
        profile=volume_profile(trade_iterator(symbol,week_days,cache),Decimal(str(data["daily_trade_volume_profile"]["price_bucket_usdt"])))
        k_volume=sum(r["volume_btc"] for r in weekly_rows)
        k_quote=sum(r["quote_usdt"] for r in weekly_rows)
        if abs(profile["base_volume_btc"]-k_volume)>max(1e-7,k_volume*1e-10):
            raise ValueError("Weekly aggTrades/kline base-volume mismatch")
        if abs(profile["vwap_usdt"]-k_quote/k_volume)>1e-5:
            raise ValueError("Weekly aggTrades/kline VWAP mismatch")
        data["previous_completed_week_trade_volume_profile"] = {
            "from_utc":week_days[0].isoformat(),"to_utc":week_days[-1].isoformat(),
            **profile,"integrity_status":"passed", "source":"Binance Spot daily aggTrades archives, 7 completed UTC days"
        }
    data["evidence_note"] = "A when checksum and Binance kline volume/VWAP cross-check pass; levels are for report_reference_date_utc, NOT current intraday OHLC. Daily and weekly volume profiles use actual Binance Spot aggTrades."
    return data

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",default="output/latest.json")
    ap.add_argument("--cache",default=".cache")
    ap.add_argument("--skip-weekly",action="store_true")
    args=ap.parse_args()
    p=pathlib.Path(args.input)
    data=json.loads(p.read_text())
    updated=enrich(data,pathlib.Path(args.cache),not args.skip_weekly)
    p.write_text(json.dumps(updated,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps({"report_reference_date":updated["report_reference_date_utc"],
       "poc_delta":updated["daily_poc_migration"]["delta_usdt"],
       "weekly_poc":updated.get("previous_completed_week_trade_volume_profile",{}).get("poc_usdt"),
       "level_day":updated["levels"]["previous_day"]["date"]}))
if __name__=="__main__": main()
