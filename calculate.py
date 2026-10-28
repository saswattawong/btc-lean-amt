#!/usr/bin/env python3
"""Binance BTCUSDT spot daily trade-level volume profile and exact aggregate VWAP.
Runs after the UTC day ends. No API key required. Std deviation is size-weighted trade-price SD.
"""
import argparse, csv, datetime as dt, hashlib, io, json, math, pathlib, re, time, urllib.error, urllib.request, zipfile
from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR

UTC=dt.timezone.utc
BASE='https://data-api.binance.vision/api/v3/klines'
ARCHIVE='https://data.binance.vision/data/spot/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip'

class ArchiveUnavailable(Exception):
    """The requested completed UTC archive has not been published yet."""

def urlopen_retry(request,timeout,attempts=3):
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(request,timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code==404: raise ArchiveUnavailable(request.full_url) from error
            if error.code not in {408,425,429,500,502,503,504} or attempt+1==attempts: raise
        except (urllib.error.URLError,TimeoutError):
            if attempt+1==attempts: raise
        time.sleep(2**attempt)
    raise RuntimeError('unreachable')

def request_json(url):
    req=urllib.request.Request(url, headers={'User-Agent':'btc-lean-amt/1.0'})
    with urlopen_retry(req,45) as r: return json.load(r)

def date_utc(ms):
    return dt.datetime.fromtimestamp(int(ms)/1000,UTC).date()

def daily_klines(symbol, end_day):
    # Binance 1d candle data close at day boundary. Include 410 days for complete rolling365, prev-month and week levels.
    end_ms=int(dt.datetime.combine(end_day+dt.timedelta(days=1),dt.time(),UTC).timestamp()*1000)
    start_ms=int(dt.datetime.combine(end_day-dt.timedelta(days=405),dt.time(),UTC).timestamp()*1000)
    url=f'{BASE}?symbol={symbol}&interval=1d&startTime={start_ms}&endTime={end_ms-1}&limit=1000'
    rows=request_json(url)
    parsed=[]
    for x in rows:
        day=date_utc(x[0]);
        if day<=end_day:
            parsed.append({'date':day,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume_btc':float(x[5]),'quote_usdt':float(x[7])})
    return parsed,url

def trade_rows_from_csv(fileobj, day):
    """Binance spot archive timestamps from 2025 are microseconds; prior years milliseconds."""
    for row in csv.reader(io.TextIOWrapper(fileobj,encoding='utf-8')):
        if not row or not row[0].isdigit(): continue
        # a, p, q, firstId, lastId, time, isBuyerMaker, bestMatch
        price=Decimal(row[1]); qty=Decimal(row[2]); t=int(row[5]);
        if t>=10**15: t//=1000
        if date_utc(t)!=day: continue
        if price<=0 or qty<=0: continue
        yield price,qty

def sha256_file(path):
    digest=hashlib.sha256()
    with open(path,'rb') as file:
        while block:=file.read(1024*1024): digest.update(block)
    return digest.hexdigest()

def expected_archive_sha256(uri):
    checksum_uri=uri+'.CHECKSUM'
    req=urllib.request.Request(checksum_uri,headers={'User-Agent':'btc-lean-amt/1.0'})
    with urlopen_retry(req,45) as response:
        token=response.read().decode('ascii').strip().split()[0].lower()
    if not re.fullmatch(r'[0-9a-f]{64}',token):
        raise ValueError(f'Invalid Binance checksum response: {checksum_uri}')
    return token,checksum_uri

def download_archive(symbol, day, cache):
    cache.mkdir(parents=True,exist_ok=True)
    dst=cache/f'{symbol}-aggTrades-{day.isoformat()}.zip'
    uri=ARCHIVE.format(symbol=symbol,day=day.isoformat())
    expected,checksum_uri=expected_archive_sha256(uri)
    if dst.exists():
        if zipfile.is_zipfile(dst) and sha256_file(dst)==expected:
            return dst,{'archive_url':uri,'checksum_url':checksum_uri,'sha256':expected}
        dst.unlink()
    tmp=dst.with_suffix('.partial')
    try:
        with urlopen_retry(urllib.request.Request(uri,headers={'User-Agent':'btc-lean-amt/1.0'}),120) as response, open(tmp,'wb') as out:
            while True:
                block=response.read(1024*1024)
                if not block:break
                out.write(block)
        if not zipfile.is_zipfile(tmp): raise ValueError('Downloaded file is not ZIP')
        actual=sha256_file(tmp)
        if actual!=expected: raise ValueError(f'Binance archive checksum mismatch: expected {expected}, got {actual}')
        tmp.replace(dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dst,{'archive_url':uri,'checksum_url':checksum_uri,'sha256':expected}

def volume_profile(rows, step=Decimal('10'), value_fraction=Decimal('0.70')):
    """Trade-exact volume-at-price bucketed to $step. VA expands from POC by adjacent higher-volume bucket."""
    bins=defaultdict(Decimal); vol=Decimal(0); pv=Decimal(0); ppv=Decimal(0); n=0
    for price,qty in rows:
        bucket=(price/step).to_integral_value(rounding=ROUND_FLOOR)*step
        bins[bucket]+=qty;vol+=qty;pv+=price*qty;ppv+=price*price*qty;n+=1
    if n==0: raise ValueError('No valid trades for requested UTC day')
    ordered=sorted(bins); idx=max(range(len(ordered)),key=lambda i:(bins[ordered[i]],-ordered[i]))
    # An empty bucket is valid inside the price range. Expand on numerical grid, not just occupied bins.
    poc=ordered[idx]; low=high=poc; covered=bins[poc]; target=vol*value_fraction
    while covered<target:
        left=low-step;right=high+step
        lv=bins[left] if left>=ordered[0] else Decimal(-1)
        rv=bins[right] if right<=ordered[-1] else Decimal(-1)
        if lv<0 and rv<0: raise ValueError('Volume area cannot expand')
        if rv>lv:high=right;covered+=max(rv,0)
        else:low=left;covered+=max(lv,0)
    vwap=pv/vol;var=max(Decimal(0),ppv/vol-vwap*vwap)
    return {'poc_usdt':float(poc),'vah_usdt':float(high+step),'val_usdt':float(low),
            'vwap_usdt':float(vwap),'trade_price_sd_usdt':math.sqrt(float(var)),
            'vwap_plus_1sd_usdt':float(vwap)+math.sqrt(float(var)),
            'vwap_minus_1sd_usdt':float(vwap)-math.sqrt(float(var)),
            'base_volume_btc':float(vol),'value_area_actual_share':float(covered/vol),
            'aggregated_trade_rows':n,'price_bucket_usdt':float(step)}

def range_vwap(rows):
    vol=sum(x['volume_btc'] for x in rows);quote=sum(x['quote_usdt'] for x in rows)
    return round(quote/vol,5) if vol>0 else None

def compute(day,symbol='BTCUSDT',step=10,cache=pathlib.Path('.cache')):
    generated_at=dt.datetime.now(UTC)
    source_as_of=dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),UTC)
    zip_path,archive_source=download_archive(symbol,day,cache)
    with zipfile.ZipFile(zip_path) as z:
        csv_names=[n for n in z.namelist() if n.endswith('.csv')]
        if len(csv_names)!=1: raise ValueError('Expected one CSV in archive')
        with z.open(csv_names[0]) as file:
            profile=volume_profile(trade_rows_from_csv(file,day),Decimal(str(step)))
    klines,kline_url=daily_klines(symbol,day)
    if not klines or klines[-1]['date']!=day: raise ValueError('Closed daily klines missing')
    day_kline=klines[-1]
    kline_vwap=day_kline['quote_usdt']/day_kline['volume_btc']
    volume_delta=abs(profile['base_volume_btc']-day_kline['volume_btc'])
    vwap_delta=abs(profile['vwap_usdt']-kline_vwap)
    if not math.isclose(profile['base_volume_btc'],day_kline['volume_btc'],rel_tol=1e-12,abs_tol=1e-8):
        raise ValueError(f'aggTrades/kline base-volume mismatch: {volume_delta}')
    if not math.isclose(profile['vwap_usdt'],kline_vwap,rel_tol=1e-12,abs_tol=1e-8):
        raise ValueError(f'aggTrades/kline VWAP mismatch: {vwap_delta}')
    current=[r for r in klines if r['date'].year==day.year and r['date'].month==day.month]
    first=day.replace(day=1)
    prev_end=first-dt.timedelta(days=1)
    prev=[r for r in klines if r['date'].year==prev_end.year and r['date'].month==prev_end.month]
    rolling=[r for r in klines if day-dt.timedelta(days=364)<=r['date']<=day]
    if len(rolling)!=365: raise ValueError(f'Need 365 complete daily bars, got {len(rolling)}')
    # Previous complete 7-day UTC week Mon-Sun
    prev_week_start=day-dt.timedelta(days=day.weekday()+7)
    prev_week=[r for r in klines if prev_week_start<=r['date']<prev_week_start+dt.timedelta(days=7)]
    assert len(prev_week)==7
    prev_day=klines[-2]
    return {'schema':'btc_lean_amt_value_map_v1','generated_at_utc':generated_at.isoformat(),
      'exchange':'Binance','market':'Spot','symbol':symbol,'price_quote':'USDT','session_tz':'UTC',
      'as_of_closed_day_utc':day.isoformat(),'session_start_utc':day.isoformat()+'T00:00:00Z',
      'session_end_utc_exclusive':(day+dt.timedelta(days=1)).isoformat()+'T00:00:00Z',
      'freshness':{'source_as_of_utc':source_as_of.isoformat(timespec='seconds').replace('+00:00','Z'),
          'generated_at_utc':generated_at.isoformat(),
          'source_age_seconds_at_generation':max(0,int((generated_at-source_as_of).total_seconds())),
          'source_closed_day_utc':day.isoformat(),'status_at_generation':'latest_verified_archive',
          'note':'Freshness is measured from the completed source-day boundary, never from file generation time.'},
      'evidence':'A for Binance archived aggregated executions verified by the official companion checksum and cross-checked against the closed daily kline; value area is derived with the documented bucket rule (not identical to TradingView settings)',
      'sources':archive_source | {'kline_api_url':kline_url},
      'integrity_cross_check':{'daily_kline_base_volume_btc':day_kline['volume_btc'],'daily_kline_quote_volume_usdt':day_kline['quote_usdt'],
          'daily_kline_vwap_usdt':kline_vwap,'base_volume_absolute_difference_btc':volume_delta,
          'vwap_absolute_difference_usdt':vwap_delta,'status':'passed'},
      'daily_trade_volume_profile':profile,
      'vwap':{'developing_month_vwap_to_last_closed_utc_day':range_vwap(current),'previous_completed_month_vwap':range_vwap(prev),
              'rolling_365_complete_utc_days_vwap':range_vwap(rolling),
              'note':'VWAP from sum(daily quote volume) / sum(daily base volume). No unsupported multi-day SD bands.'},
      'levels':{'previous_day':{k:prev_day[k] for k in ('date','high','low','close') if k!='date'} | {'date':prev_day['date'].isoformat()},
                'previous_completed_utc_week':{'from':prev_week_start.isoformat(),'to':(prev_week_start+dt.timedelta(days=6)).isoformat(),
                  'high':max(x['high'] for x in prev_week),'low':min(x['low'] for x in prev_week)}},
      'method':{'source':'https://data.binance.vision/ (daily aggTrades ZIP) and https://data-api.binance.vision/api/v3/klines',
                'profile':'Each aggregated executed trade contributes traded BTC qty into floor(trade price / bucket size)*bucket. POC=max BTC qty bucket (tie: lower price). Value Area expands one adjacent bucket at a time from POC, selects side with more volume, until >=70% trade qty. VAH is exclusive upper edge of highest included bucket; VAL inclusive lower edge.',
                'sd':'Daily trade-size-weighted standard deviation of execution price, not candle SD; daily only.',
                'limitations':'Aggregated trades combine executions; volume is preserved. Binance Spot only. Archive for UTC day available next day. No live order book and no intraday developing session. Value Area bucket choice and expansion are explicit methodology choices.'}}

def compute_latest_available(today,symbol='BTCUSDT',step=10,cache=pathlib.Path('.cache'),lookback_days=4,calculator=compute):
    for days_ago in range(1,lookback_days+1):
        day=today-dt.timedelta(days=days_ago)
        try:
            return day,calculator(day,symbol,step,cache)
        except ArchiveUnavailable:
            continue
    raise ArchiveUnavailable(f'No Binance Spot aggTrades archive found in the last {lookback_days} completed UTC days')

def write_if_new_source(data,destination,force=False):
    if destination.exists() and not force:
        try:
            current=json.loads(destination.read_text(encoding='utf-8'))
            if (current.get('schema')==data['schema']
                and current.get('as_of_closed_day_utc')==data['as_of_closed_day_utc']
                and current.get('integrity_cross_check',{}).get('status')=='passed'
                and current.get('freshness',{}).get('source_as_of_utc')==data['freshness']['source_as_of_utc']):
                return current,False
        except (OSError,ValueError):
            pass
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    return data,True

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date',help='UTC closed day YYYY-MM-DD; default newest available completed archive');ap.add_argument('--symbol',default='BTCUSDT');ap.add_argument('--bucket',type=float,default=10);ap.add_argument('--cache',default='.cache');ap.add_argument('--output',default='output/latest.json');ap.add_argument('--lookback-days',type=int,default=4);ap.add_argument('--force',action='store_true');args=ap.parse_args()
    today=dt.datetime.now(UTC).date()
    if args.date:
        day=dt.date.fromisoformat(args.date)
        if day>=today: raise SystemExit('Date must be a completed UTC day')
        data=compute(day,args.symbol,args.bucket,pathlib.Path(args.cache))
    else:
        day,data=compute_latest_available(today,args.symbol,args.bucket,pathlib.Path(args.cache),args.lookback_days)
    dest=pathlib.Path(args.output)
    data,changed=write_if_new_source(data,dest,args.force)
    print(json.dumps({'day':day.isoformat(),'output':str(dest),'vwap':data['daily_trade_volume_profile']['vwap_usdt'],
                      'poc':data['daily_trade_volume_profile']['poc_usdt'],'integrity':data['integrity_cross_check']['status'],'changed':changed}))
if __name__=='__main__':main()

