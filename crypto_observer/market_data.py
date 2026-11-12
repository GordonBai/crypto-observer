"""Independent Bitget public market observations. No order or account API exists here."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import urlencode
from .core import now, number
from .short_windows import signed_number

VENUE = 'bitget'
BASE = 'https://api.bitget.com'
VERSION = 'market-v2'
POLICY = {'venue':VENUE, 'quote':'USDT', 'instrument_type':'crypto',
          'categories':['SPOT','USDT-FUTURES'], 'symbol_type':'crypto',
          'exclude_reality_rwa_isin':True, 'launch_zero_policy':'unknown', 'selection':'sha256(key)_ascending_per_kind',
          'spot_limit':24, 'swap_limit':12, 'bar_seconds':3600, 'closure_grace_seconds':5,
          'book_max_age_seconds':30, 'source_clock_tolerance_seconds':120}


def utc(value):
    dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone required')
    return dt.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def millis(value):
    if isinstance(value,bool) or not str(value).isdigit():
        raise ValueError('invalid millisecond timestamp')
    return datetime.fromtimestamp(int(value)/1000,timezone.utc)


def setup(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS market_universes(
      version TEXT PRIMARY KEY, created_at TEXT NOT NULL, config TEXT NOT NULL,
      request_ids TEXT NOT NULL, inventory_hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS market_instruments(
      key TEXT PRIMARY KEY, venue TEXT NOT NULL, instrument TEXT NOT NULL,
      kind TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL, status TEXT NOT NULL,
      launch_at TEXT, metadata TEXT NOT NULL, request_id INTEGER NOT NULL REFERENCES requests(id),
      frozen_at TEXT NOT NULL, enrolled INTEGER NOT NULL, selected_rank INTEGER NOT NULL,
      last_attempt_at TEXT);
    CREATE TABLE IF NOT EXISTS market_bars(
      id INTEGER PRIMARY KEY, instrument_key TEXT NOT NULL REFERENCES market_instruments(key),
      request_id INTEGER NOT NULL REFERENCES requests(id), open_at TEXT NOT NULL, close_at TEXT NOT NULL,
      open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
      volume_base TEXT NOT NULL, volume_quote TEXT NOT NULL, quote_currency TEXT NOT NULL,
      received_at TEXT NOT NULL, closure_policy TEXT NOT NULL,
      UNIQUE(instrument_key,open_at));
    CREATE INDEX IF NOT EXISTS market_bars_asof ON market_bars(instrument_key,open_at,received_at);
    CREATE TABLE IF NOT EXISTS market_ingests(
      id INTEGER PRIMARY KEY, instrument_key TEXT NOT NULL REFERENCES market_instruments(key),
      request_id INTEGER NOT NULL REFERENCES requests(id), observed_at TEXT NOT NULL,
      source_at TEXT, accepted INTEGER NOT NULL, rejected INTEGER NOT NULL,
      revised INTEGER NOT NULL, notes TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS market_books(
      id INTEGER PRIMARY KEY, instrument_key TEXT NOT NULL REFERENCES market_instruments(key),
      request_id INTEGER NOT NULL REFERENCES requests(id), observed_at TEXT NOT NULL, source_at TEXT,
      status TEXT NOT NULL,bids TEXT NOT NULL,asks TEXT NOT NULL,spread_bps TEXT,notes TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS market_derivatives(
      id INTEGER PRIMARY KEY, instrument_key TEXT NOT NULL REFERENCES market_instruments(key),
      observed_at TEXT NOT NULL, request_ids TEXT NOT NULL,
      open_interest TEXT,oi_unit TEXT NOT NULL,funding_rate TEXT,funding_interval_hours TEXT,
      next_funding_at TEXT,market_price TEXT,mark_price TEXT,index_price TEXT,basis_fraction TEXT,
      source_times TEXT NOT NULL,notes TEXT NOT NULL);
    ''')


def fetch(store, client, run, path, params=None):
    # All paths are hardcoded public reads. No secrets or alternate hosts accepted.
    if not path.startswith('/api/v') or any(x in path for x in ('/trade/','/account/')):
        raise ValueError('public market path required')
    url = BASE+path+('?' + urlencode(params) if params else '')
    r = client.get(url,0.12)
    rid = store.request(run,'bitget_public',url,r)
    payload = r.payload if isinstance(r.payload,dict) else {}
    return rid,r,payload if not r.error and payload.get('code')=='00000' else {}


def check_version(store):
    active=store.db.execute('SELECT 1 FROM market_universes WHERE version=?',(VERSION,)).fetchone()
    prior=store.db.execute('SELECT 1 FROM market_universes LIMIT 1').fetchone()
    if prior and not active:
        raise ValueError('historical market universe belongs to another version; preserve this database and use a fresh database for '+VERSION)


def freeze_universe(store,client,run):
    check_version(store)
    existing=store.db.execute('SELECT config FROM market_universes WHERE version=?',(VERSION,)).fetchone()
    if existing:
        if json.loads(existing[0]) != POLICY:
            raise ValueError('frozen market policy changed; use a new version')
        return {'status':'already_frozen'}
    inventory=[]; request_ids=[]
    for category,kind in [('SPOT','spot'),('USDT-FUTURES','swap')]:
        rid,r,p = fetch(store,client,run,'/api/v3/market/instruments',{'category':category})
        request_ids.append(rid)
        data=p.get('data')
        if not isinstance(data,list) or not data:
            return {'status':'universe_unavailable','notes':[category+':catalog_unavailable']}
        keys=set()
        for item in data:
            if not isinstance(item,dict):
                continue
            if item.get('category')!=category or item.get('quoteCoin')!='USDT' or item.get('status')!='online' or item.get('instrumentType')!='crypto':
                continue
            if item.get('symbolType')!='crypto' or item.get('isReality')=='yes' or item.get('isRwa')=='YES' or item.get('isin'):
                continue
            if kind=='swap' and item.get('type')!='perpetual':
                continue
            symbol,base=item.get('symbol'),item.get('baseCoin')
            if not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9]{2,64}',symbol) or not isinstance(base,str) or not base:
                continue
            key=f'{VENUE}:{kind}:{symbol}'
            if key in keys:
                return {'status':'universe_unavailable','notes':['duplicate_instrument:'+key]}
            keys.add(key)
            launch=None
            try:
                if int(item.get('launchTime') or 0)<=0:
                    raise ValueError('launch timestamp unknown')
                launch=iso(millis(item.get('launchTime')))
            except (ValueError,OverflowError,OSError,TypeError):
                pass
            inventory.append({'key':key,'venue':VENUE,'instrument':symbol,'kind':kind,
                'base':base,'quote':'USDT','status':'online','launch_at':launch,'metadata':json.dumps(item,sort_keys=True),'request_id':rid})
        if not keys:
            return {'status':'universe_unavailable','notes':[category+':no_supported_instruments']}
    at=now()
    for kind in ('spot','swap'):
        ranked=sorted((x for x in inventory if x['kind']==kind),key=lambda x:hashlib.sha256(x['key'].encode()).hexdigest())
        for rank,item in enumerate(ranked,1):
            store.db.execute('''INSERT INTO market_instruments
             (key,venue,instrument,kind,base,quote,status,launch_at,metadata,request_id,frozen_at,enrolled,selected_rank)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
             (*[item[k] for k in ('key','venue','instrument','kind','base','quote','status','launch_at','metadata','request_id')],at,int(rank<=POLICY[kind+'_limit']),rank))
    digest=hashlib.sha256(json.dumps(sorted(x['key'] for x in inventory)).encode()).hexdigest()
    store.db.execute('INSERT INTO market_universes VALUES(?,?,?,?,?)',(VERSION,at,json.dumps(POLICY,sort_keys=True),json.dumps(request_ids),digest))
    store.db.commit()
    return {'status':'frozen','inventory':len(inventory),'enrolled':sum(min(POLICY[k+'_limit'],sum(x['kind']==k for x in inventory)) for k in ('spot','swap')),'inventory_hash':digest}


def universe(store):
    setup(store)
    check_version(store)
    return [dict(r) for r in store.db.execute('SELECT * FROM market_instruments WHERE enrolled=1 ORDER BY kind,selected_rank')]


def ingest_bars(store,instrument,rid,response,payload):
    notes=[]; rows=[]; rejected=0; revised=0
    data=payload.get('data')
    try:
        source_time=millis(payload.get('requestTime'))
        received=utc(response.observed_at)
        if abs((source_time-received).total_seconds())>POLICY['source_clock_tolerance_seconds']:
            raise ValueError('source_clock_mismatch')
        cutoff=min(source_time,received)-timedelta(seconds=POLICY['closure_grace_seconds'])
    except (ValueError,TypeError,OverflowError,OSError):
        cutoff=None;source_time=None;notes.append('source_clock_unknown_or_mismatch')
    if not isinstance(data,list):
        data=[];notes.append('candles_unavailable')
    seen={};duplicates=set();occurrences={}
    for raw in data:
        try:
            if cutoff is None or not isinstance(raw,list) or len(raw)!=7:
                raise ValueError('invalid_bar_schema_or_clock')
            start=millis(raw[0]);end=start+timedelta(hours=1)
            if int(raw[0])%3600000 or end>cutoff:
                raise ValueError('unaligned_or_unclosed_bar')
            values=[number(v) for v in raw[1:7]]
            if any(v is None for v in values):
                raise ValueError('invalid_ohlcv')
            o,h,l,c,vb,vq=map(Decimal,values)
            if min(o,h,l,c)<=0 or not l<=min(o,c)<=max(o,c)<=h:
                raise ValueError('invalid_ohlc_order')
            key=iso(start)
            occurrences[key]=occurrences.get(key,0)+1
            if key in seen:
                duplicates.add(key)
            seen[key]=(key,iso(end),*values)
        except (ValueError,TypeError,OverflowError,OSError):
            rejected+=1
    for key,row in sorted(seen.items()):
        if key in duplicates:
            rejected+=occurrences[key];notes.append('duplicate_bar_open');continue
        prior=store.db.execute('SELECT open,high,low,close,volume_base,volume_quote FROM market_bars WHERE instrument_key=? AND open_at=?',(instrument['key'],key)).fetchone()
        if prior:
            if list(prior)!=list(row[2:]):
                revised+=1
            continue
        rows.append((instrument['key'],rid,*row,instrument['quote'],iso(received),'hour_boundary_plus_5s_no_exchange_confirm_flag'))
    store.db.executemany('''INSERT INTO market_bars
      (instrument_key,request_id,open_at,close_at,open,high,low,close,volume_base,volume_quote,quote_currency,received_at,closure_policy)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',rows)
    if rejected:notes.append('invalid_unclosed_or_duplicate_bars:'+str(rejected))
    if revised:notes.append('revised_bars_preserved_original:'+str(revised))
    store.db.execute('INSERT INTO market_ingests(instrument_key,request_id,observed_at,source_at,accepted,rejected,revised,notes) VALUES(?,?,?,?,?,?,?,?)',
       (instrument['key'],rid,iso(utc(response.observed_at)),iso(source_time) if source_time else None,len(rows),rejected,revised,json.dumps(notes)))
    store.db.execute('UPDATE market_instruments SET last_attempt_at=? WHERE key=?',(iso(utc(response.observed_at)),instrument['key']))
    store.db.commit()
    return {'instrument_key':instrument['key'],'accepted':len(rows),'rejected':rejected,'revised':revised,'notes':notes}


def closed_bars(store,instrument_key,asof):
    setup(store)
    check_version(store)
    at=iso(utc(asof))
    # Availability is the original local receipt, never the historical candle close.
    return [dict(r) for r in store.db.execute('''SELECT * FROM
      (SELECT * FROM market_bars WHERE instrument_key=? AND received_at<=? AND close_at<=?
       ORDER BY open_at DESC LIMIT 32) ORDER BY open_at''',(instrument_key,at,at))]


def _levels(raw,reverse=False):
    if not isinstance(raw,list) or not raw:
        raise ValueError('empty_book_side')
    levels=[]
    for row in raw:
        if not isinstance(row,list) or len(row)!=2:
            raise ValueError('invalid_book_level')
        p,s=number(row[0]),number(row[1])
        if p is None or s is None or Decimal(p)<=0 or Decimal(s)<=0:
            raise ValueError('invalid_book_number')
        levels.append([p,s])
    prices=[Decimal(x[0]) for x in levels]
    if prices!=sorted(prices,reverse=reverse) or len(prices)!=len(set(prices)):
        raise ValueError('unsorted_or_duplicate_prices')
    return levels


def book(store,client,run,instrument_key):
    setup(store)
    check_version(store)
    item=store.db.execute('SELECT * FROM market_instruments WHERE key=?',(instrument_key,)).fetchone()
    if not item or item['venue']!=VENUE or item['kind'] not in ('spot','swap') or item['quote']!='USDT':
        raise ValueError('unknown_or_unsupported_instrument')
    category='SPOT' if item['kind']=='spot' else 'USDT-FUTURES'
    rid,r,p=fetch(store,client,run,'/api/v3/market/orderbook',{'category':category,'symbol':item['instrument'],'limit':50})
    bids=[];asks=[];source=None;spread=None;notes=[]
    try:
        data=p.get('data')
        if not isinstance(data,dict) or (data.get('symbol') is not None and data['symbol']!=item['instrument']):
            raise ValueError('wrong_identity_or_schema')
        source=millis(data.get('ts'))
        age=(utc(r.observed_at)-source).total_seconds()
        if age>POLICY['book_max_age_seconds'] or age < -5:
            raise ValueError('stale_or_future_book')
        bids=_levels(data.get('b'),True);asks=_levels(data.get('a'))
        bid,ask=Decimal(bids[0][0]),Decimal(asks[0][0])
        if bid>=ask:
            raise ValueError('crossed_or_locked_book')
        spread=str((ask-bid)/((ask+bid)/2)*10000)
        status='quoted'
    except (ValueError,TypeError,OverflowError,OSError) as exc:
        bids=[];asks=[];notes.append(str(exc));status='quote_unknown'
    result={'request_id':rid,'observed_at':iso(utc(r.observed_at)),'source_at':iso(source) if source else None,
            'status':status,'bids':bids,'asks':asks,'spread_bps':spread,'instrument_key':instrument_key,
            'quote_currency':item['quote'],'size_unit':item['base'] if item['kind']=='spot' else 'provider_quantity_unit_unverified',
            'execution_supported':item['kind']=='spot','kind':item['kind'],'notes':notes}
    store.db.execute('INSERT INTO market_books(instrument_key,request_id,observed_at,source_at,status,bids,asks,spread_bps,notes) VALUES(?,?,?,?,?,?,?,?,?)',
        (instrument_key,rid,result['observed_at'],result['source_at'],status,json.dumps(bids),json.dumps(asks),spread,json.dumps(notes)))
    store.db.commit()
    return result


def derivatives(store,client,run,instrument):
    if instrument['kind']!='swap':
        raise ValueError('swap only')
    request_ids=[];times={};notes=[];values={};symbol=instrument['instrument']
    params={'symbol':symbol,'productType':'USDT-FUTURES'}
    for name,path in [('oi','open-interest'),('funding','current-fund-rate'),('prices','symbol-price')]:
        rid,r,p=fetch(store,client,run,'/api/v2/mix/market/'+path,params)
        request_ids.append(rid)
        try:
            data=p.get('data')
            if name=='oi':
                if not isinstance(data,dict):raise ValueError('missing_oi')
                entries=data.get('openInterestList')
                if not isinstance(entries,list) or len(entries)!=1 or entries[0].get('symbol')!=symbol:
                    raise ValueError('wrong_oi_identity')
                values['open_interest']=number(entries[0].get('size'))
                times[name]=iso(millis(data.get('ts')))
            else:
                if not isinstance(data,list) or len(data)!=1 or data[0].get('symbol')!=symbol:
                    raise ValueError('wrong_metric_identity')
                row=data[0]
                if name=='funding':
                    values['funding_rate']=signed_number(row.get('fundingRate'))
                    values['funding_interval_hours']=number(row.get('fundingRateInterval'))
                    values['next_funding_at']=iso(millis(row.get('nextUpdate')))
                    times[name]=iso(millis(p.get('requestTime')))
                else:
                    for src,dst in [('price','market_price'),('markPrice','mark_price'),('indexPrice','index_price')]:
                        values[dst]=number(row.get(src))
                    times[name]=iso(millis(row.get('ts')))
            if abs((utc(r.observed_at)-utc(times[name])).total_seconds())>120:
                raise ValueError('stale_metric')
        except (ValueError,KeyError,TypeError,OverflowError,OSError) as exc:
            notes.append(name+':'+str(exc))
            for key in {'oi':['open_interest'],'funding':['funding_rate','funding_interval_hours','next_funding_at'],'prices':['market_price','mark_price','index_price']}[name]:
                values[key]=None
    market,index=values.get('market_price'),values.get('index_price')
    values['basis_fraction']=str(Decimal(market)/Decimal(index)-1) if market and index and Decimal(index)>0 else None
    cols=['open_interest','funding_rate','funding_interval_hours','next_funding_at','market_price','mark_price','index_price','basis_fraction']
    store.db.execute('''INSERT INTO market_derivatives
      (instrument_key,observed_at,request_ids,open_interest,oi_unit,funding_rate,funding_interval_hours,next_funding_at,market_price,mark_price,index_price,basis_fraction,source_times,notes)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (instrument['key'],now(),json.dumps(request_ids),values.get('open_interest'),'provider_size_unit_unverified',*[values.get(k) for k in cols[1:]],json.dumps(times),json.dumps(notes)))
    store.db.commit()
    return {'instrument_key':instrument['key'],**values,'notes':notes}


def collect_market(store,client,limit=6):
    if not 1<=limit<=100:
        raise ValueError('market limit must be 1..100')
    setup(store)
    run=store.start({'module':'market_data','version':VERSION,'limit':limit})
    results=[];notes=[];diagnostics=[]
    try:
        frozen=freeze_universe(store,client,run)
        if frozen.get('status')=='universe_unavailable':
            notes+=frozen['notes']
        else:
            instruments=store.db.execute("SELECT * FROM market_instruments WHERE enrolled=1 ORDER BY COALESCE(last_attempt_at,''),key LIMIT ?",(limit,)).fetchall()
            for instrument in instruments:
                category='SPOT' if instrument['kind']=='spot' else 'USDT-FUTURES'
                rid,r,p=fetch(store,client,run,'/api/v3/market/history-candles',
                    {'category':category,'symbol':instrument['instrument'],'interval':'1H','limit':32,'type':'market'})
                result=ingest_bars(store,instrument,rid,r,p)
                results.append(result)
                if not p:notes.append(instrument['key']+':candles_unavailable')
                notes.extend(instrument['key']+':'+note for note in result['notes'])
                if instrument['kind']=='swap':
                    diagnostics.append(derivatives(store,client,run,instrument))
            # Fetch books as the last phase: early books must not age through all
            # candle/derivative requests before the event decision consumes them.
            for instrument in instruments:
                q=book(store,client,run,instrument['key'])
                if q['status']!='quoted':notes.append(instrument['key']+':book_unknown')
        store.finish(run,notes)
    except BaseException:
        store.finish(run,['interrupted']);raise
    return {'run_id':run,'universe':frozen,'instruments_attempted':len(results),'bars_added':sum(x['accepted'] for x in results),
            'bar_results':results,'swap_diagnostics':diagnostics,'notes':notes}


def report(store):
    setup(store)
    check_version(store)
    db=store.db
    frozen=db.execute('SELECT * FROM market_universes WHERE version=?',(VERSION,)).fetchone()
    lines=['# Independent exchange observation readiness','','Generated UTC: '+now(),'',
      'Venue Bitget; quote USDT is not USD. Public reads only. All current online USDT instruments classified crypto by both instrumentType and symbolType, excluding Reality/RWA/ISIN instruments, are inventoried once; a deterministic hash subset (24 spot, 12 perpetual) is frozen for bounded observation. New listings do not silently enter this version.',
      'Hourly bars use same-source native quote turnover, not rolling-window differences or base-volume price multiplication. Only aligned hours closed before both server/local time minus 5s are admitted; Bitget has no explicit confirmation flag. Original first-received values remain immutable; later changes are counted as revisions, not rewritten.',
      'History is available only after its actual receipt. Retrospectively fetched bars are baseline material, not past actionable events. No real trading, derivatives execution, inferred beneficial owners or investment alpha is established.', '']
    if not frozen:
        return '\n'.join(lines+['Universe not yet frozen; no observations.',''])
    lines+=['Universe frozen UTC (status is enrollment-time status, not current listing assurance): '+frozen['created_at'], 'Inventory SHA256: '+frozen['inventory_hash'],'',
      '| Kind | Frozen inventory | Enrolled | Ever attempted | Stored bars | Latest receipt UTC |',
      '|---|---:|---:|---:|---:|---|']
    for kind in ('spot','swap'):
        row=db.execute('''SELECT COUNT(*) inventory,SUM(enrolled) enrolled,SUM(last_attempt_at IS NOT NULL) attempted
         FROM market_instruments WHERE kind=?''',(kind,)).fetchone()
        bars=db.execute('''SELECT COUNT(*) n,MAX(received_at) latest FROM market_bars b JOIN market_instruments i ON i.key=b.instrument_key WHERE i.kind=?''',(kind,)).fetchone()
        lines.append(f"| {kind} | {row['inventory']} | {row['enrolled'] or 0} | {row['attempted'] or 0} | {bars['n']} | {bars['latest'] or 'unknown'} |")
    counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('market_ingests','market_books','market_derivatives')}
    counts['unknown_books']=db.execute("SELECT COUNT(*) FROM market_books WHERE status!='quoted'").fetchone()[0]
    counts['revised_bars']=db.execute('SELECT COALESCE(SUM(revised),0) FROM market_ingests').fetchone()[0]
    lines+=['','Diagnostics: '+json.dumps(counts), '',
      '## Coverage / capacity', '',
      'Inventory retention does not imply all-market short-term coverage. Collection rotates the enrolled subset; the configured per-cycle budget and offline gaps determine actionable coverage. Reports must assess every enrolled instrument, including unavailable/delisted/error samples.', '',
      '| Kind | Enrolled instruments | Never attempted | Latest attempt age max seconds | Observed interval count | Max interval seconds | Gaps >10m |',
      '|---|---:|---:|---:|---:|---:|---:|']
    for kind in ('spot','swap'):
        instruments=[dict(x) for x in db.execute('SELECT * FROM market_instruments WHERE kind=? AND enrolled=1',(kind,))]
        ages=[(utc(now())-utc(x['last_attempt_at'])).total_seconds() for x in instruments if x['last_attempt_at']]
        gaps=db.execute('''WITH seq AS (SELECT observed_at,LAG(observed_at) OVER (PARTITION BY instrument_key ORDER BY id) previous FROM market_ingests
        WHERE instrument_key IN (SELECT key FROM market_instruments WHERE kind=? AND enrolled=1))
        SELECT (julianday(observed_at)-julianday(previous))*86400 gap FROM seq WHERE previous IS NOT NULL''',(kind,)).fetchall()
        values=[max(0,r[0]) for r in gaps]
        lines.append(f"| {kind} | {len(instruments)} | {len(instruments)-len(ages)} | {round(max(ages),1) if ages else 'unknown'} | {len(values)} | {round(max(values),1) if values else 'unknown'} | {sum(x>600 for x in values)} |")
    lines+=['', 'Derivative diagnostics retain raw OI, funding, market/mark/index prices and separate source times. OI retains provider size with unverified units and is not converted to USD or contract counts; USD OI changes are not net-long inflow. Basis is contemporaneous market/index difference, not spot/perpetual cross-venue arbitrage evidence. No historical OI coverage is claimed.', '']
    return '\n'.join(lines)
