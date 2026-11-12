"""Prospective short-horizon spot experiments, separate from Base/v2.

Prices are capacity-weighted displayed-book observations, never actual fills.
Development rules are exploratory. Holdout thresholds are calibrated once using
only complete pre-boundary feature observations, then frozen with provenance.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from .core import now

VERSION = 'short-v1'
CONFIG = {
    'version': VERSION, 'market_universe_version': 'market-v2', 'development_days': 7, 'baseline_hours': 24,
    'development_volume_ratio': '3', 'development_price_return': '0.03',
    'minimum_quote_turnover': '10000', 'quote_currency': 'USDT',
    'holdout_quantile': '0.9', 'holdout_ratio_floor': '1.5',
    'holdout_price_floor': '0.01', 'minimum_calibration_rows': 100,
    'minimum_calibration_instruments': 4, 'entry_delay_seconds': 60,
    'entry_window_seconds': 600, 'bar_arrival_seconds': 600,
    'book_max_age_seconds': 30, 'exit_tolerance_seconds': 600,
    'horizons_hours': [1, 4, 24], 'notional_quote': '100',
    'fees_bps_per_side': [0, 10, 30], 'base_fee_bps': 10,
    'rules': ['volume', 'price', 'combined'], 'cooldown_hours': 4,
    'selection_limit_per_rule_hour': 1, 'max_pending_positions': 48,
    'max_quote_instruments_per_cycle': 48, 'enrollment_days': 30,
    'match_policy': 'same_hour_quote_venue_kind_age_turnover_volatility_and_displayed_depth',
    'matching_age_days': [30,365], 'matching_turnover_log10': True,
    'matching_volatility_bin': '0.01', 'matching_depth_band_fraction': '0.01',
    'matching_spread_bin_bps': 10,
    'mode': 'displayed_book_quote_only_no_transactions',
}


def timestamp(value):
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise ValueError('timezone_required')
    return t.astimezone(timezone.utc)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def dec(value, positive=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        x = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return x if x.is_finite() and x >= 0 and abs(x.adjusted()) <= 100 and (not positive or x > 0) else None


def setup(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS short_experiments(
      version TEXT PRIMARY KEY, created_at TEXT NOT NULL, development_until TEXT NOT NULL,
      config TEXT NOT NULL, calibration_status TEXT NOT NULL DEFAULT 'pending',
      calibration_at TEXT, frozen_thresholds TEXT, calibration_evidence TEXT);
    CREATE TABLE IF NOT EXISTS short_assessments(
      id INTEGER PRIMARY KEY, version TEXT NOT NULL REFERENCES short_experiments(version),
      instrument_key TEXT NOT NULL, target_close TEXT NOT NULL, bar_id INTEGER,
      observed_at TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
      reason TEXT, features TEXT NOT NULL, dedup TEXT NOT NULL UNIQUE);
    CREATE INDEX IF NOT EXISTS short_assessments_time ON short_assessments(version,observed_at);
    CREATE TABLE IF NOT EXISTS short_events(
      id INTEGER PRIMARY KEY, version TEXT NOT NULL REFERENCES short_experiments(version),
      assessment_id INTEGER NOT NULL REFERENCES short_assessments(id),
      instrument_key TEXT NOT NULL, strategy TEXT NOT NULL, phase TEXT NOT NULL,
      target_close TEXT NOT NULL, triggered_at TEXT NOT NULL, actionable_at TEXT NOT NULL,
      entry_deadline TEXT NOT NULL, status TEXT NOT NULL, matched_event_id INTEGER REFERENCES short_events(id),
      evidence TEXT NOT NULL, UNIQUE(version,assessment_id,strategy));
    CREATE TABLE IF NOT EXISTS short_hour_decisions(
      version TEXT NOT NULL REFERENCES short_experiments(version),target_close TEXT NOT NULL,
      strategy TEXT NOT NULL,decided_at TEXT NOT NULL,selection TEXT NOT NULL,
      PRIMARY KEY(version,target_close,strategy));
    CREATE TABLE IF NOT EXISTS short_positions(
      id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE REFERENCES short_events(id),
      status TEXT NOT NULL, entry_at TEXT, quantity_base TEXT, entry_cost_quote TEXT,
      entry_quote_id INTEGER, reason TEXT);
    CREATE TABLE IF NOT EXISTS short_execution_quotes(
      id INTEGER PRIMARY KEY, version TEXT NOT NULL REFERENCES short_experiments(version),
      instrument_key TEXT NOT NULL, request_id INTEGER REFERENCES requests(id),
      observed_at TEXT NOT NULL, source_at TEXT, side TEXT NOT NULL, amount TEXT NOT NULL,
      status TEXT NOT NULL, quantity_base TEXT, gross_quote TEXT, evidence TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS short_outcomes(
      position_id INTEGER NOT NULL REFERENCES short_positions(id), horizon_hours INTEGER NOT NULL,
      observed_at TEXT NOT NULL, status TEXT NOT NULL, quote_id INTEGER REFERENCES short_execution_quotes(id),
      proceeds_quote TEXT, net_return TEXT, stress_returns TEXT NOT NULL,
      PRIMARY KEY(position_id,horizon_hours));
    CREATE TABLE IF NOT EXISTS short_path_marks(
      position_id INTEGER NOT NULL REFERENCES short_positions(id), observed_at TEXT NOT NULL,
      status TEXT NOT NULL, quote_id INTEGER REFERENCES short_execution_quotes(id),
      net_return TEXT, PRIMARY KEY(position_id,observed_at));
    ''')
    config = encoded(CONFIG)
    prior = store.db.execute('SELECT * FROM short_experiments WHERE version=?', (VERSION,)).fetchone()
    if prior and prior['config'] != config:
        raise ValueError('frozen short experiment changed; create a new version')
    if prior is None:
        at = timestamp(now())
        store.db.execute('INSERT INTO short_experiments(version,created_at,development_until,config) VALUES(?,?,?,?)',
                         (VERSION,at.isoformat(),(at+timedelta(days=CONFIG['development_days'])).isoformat(),config))
        store.db.commit()


def universe(store):
    from . import market_data
    if market_data.VERSION != CONFIG['market_universe_version']:
        raise ValueError('market universe version differs from frozen experiment')
    return market_data.universe(store)


def closed_bars(store, instrument_key, asof):
    from .market_data import closed_bars as read
    return read(store,instrument_key,asof)


def book(store, client, run, instrument_key):
    from .market_data import book as read
    return read(store,client,run,instrument_key)


def features(bars, instrument, at, created_at):
    """Use current closed hourly bar + exactly preceding 24 contiguous hours."""
    at = timestamp(at)
    expected = at.replace(minute=0,second=0,microsecond=0)
    evidence = {'target_close':expected.isoformat(), 'baseline_ids':[], 'bar_id':None}
    valid = []
    for b in bars:
        try:
            if b['instrument_key'] != instrument['key']:
                return 'unknown','identity_mismatch',evidence
            end, start, received = (timestamp(b[k]) for k in ('close_at','open_at','received_at'))
            if end > at or received > at:
                continue
            if end-start != timedelta(hours=1) or start.minute or start.second or start.microsecond:
                return 'unknown','unaligned_or_non_hour_bar',evidence
            valid.append((start,end,received,b))
        except (ValueError,TypeError,KeyError):
            return 'unknown','malformed_bar_time',evidence
    valid.sort(key=lambda x:x[0])
    current = [b for b in valid if b[1] == expected]
    if not current:
        return 'unknown','current_closed_bar_unavailable',evidence
    if len(current) != 1:
        return 'unknown','duplicate_hour',evidence
    latest = current[0]
    evidence['bar_id'] = latest[3]['id']
    evidence['current_received_at'] = latest[2].isoformat()
    if expected <= timestamp(created_at):
        return 'unknown','pre_enrollment_bar',evidence
    if latest[2] < expected or latest[2]-expected > timedelta(seconds=CONFIG['bar_arrival_seconds']):
        return 'unknown','bar_arrived_late',evidence
    if at-expected > timedelta(seconds=CONFIG['bar_arrival_seconds']):
        return 'unknown','decision_window_missed',evidence
    series = [x for x in valid if expected-timedelta(hours=25) <= x[0] < expected]
    if len(series) != 25 or len({x[0] for x in series}) != 25:
        return 'unknown','insufficient_or_duplicate_history',evidence
    if any(x[0] != expected-timedelta(hours=25-i) or x[1] != expected-timedelta(hours=24-i) for i,x in enumerate(series)):
        return 'unknown','history_gap',evidence
    turns, returns = [], []
    for _,_,_,b in series:
        if b.get('quote_currency') != instrument.get('quote') or instrument.get('quote') != CONFIG['quote_currency']:
            return 'unknown','quote_unit_mismatch',evidence
        values = {k:dec(b.get(k),positive=True) for k in ('open','high','low','close')}
        turnover = dec(b.get('volume_quote'))
        if turnover is None or any(v is None for v in values.values()):
            return 'unknown','invalid_price_or_turnover',evidence
        if not values['low'] <= min(values['open'],values['close']) <= max(values['open'],values['close']) <= values['high']:
            return 'unknown','invalid_ohlc_order',evidence
        turns.append(turnover)
        returns.append(values['close']/values['open']-1)
    baseline = median(turns[:-1])
    if baseline <= 0:
        return 'unknown','zero_volume_baseline',evidence
    prior_returns = returns[:-1]
    mean = sum(prior_returns)/len(prior_returns)
    volatility = (sum((r-mean)**2 for r in prior_returns)/len(prior_returns)).sqrt()
    age = None
    if instrument.get('launch_at'):
        try:
            launch = timestamp(instrument['launch_at'])
            if launch <= expected:
                age = int((expected-launch).total_seconds()/86400)
        except (TypeError,ValueError):
            pass
    evidence.update(baseline_ids=[x[3]['id'] for x in series[:-1]],
                    baseline_volume_median=str(baseline),volume_ratio=str(turns[-1]/baseline),
                    turnover_quote=str(turns[-1]),price_return=str(returns[-1]),
                    prior_volatility=str(volatility),age_days=age,
                    quote_currency=instrument['quote'], instrument_key=instrument['key'])
    return 'complete',None,evidence


def calibration(store, at):
    exp = store.db.execute('SELECT * FROM short_experiments WHERE version=?',(VERSION,)).fetchone()
    if timestamp(at) < timestamp(exp['development_until']) or exp['calibration_status'] != 'pending':
        return exp
    # Both decision receipt and bar close precede the predefined boundary. A late
    # restart must never calibrate using post-boundary market observations.
    rows = store.db.execute("SELECT * FROM short_assessments WHERE version=? AND status='complete' AND observed_at<? AND target_close<? ORDER BY id",(VERSION,exp['development_until'],exp['development_until'])).fetchall()
    vals = [json.loads(r['features']) for r in rows]
    instruments = {r['instrument_key'] for r in rows}
    evidence = {'assessment_ids':[r['id'] for r in rows],'instruments':len(instruments),
                'boundary':exp['development_until'],'selection':'complete_pre_boundary_only'}
    enough = len(rows)>=CONFIG['minimum_calibration_rows'] and len(instruments)>=CONFIG['minimum_calibration_instruments']
    thresholds = None
    if enough:
        def quantile(values):
            values=sorted(values)
            from decimal import ROUND_CEILING
            index=max(0,int((Decimal(len(values))*Decimal(CONFIG['holdout_quantile'])).to_integral_value(rounding=ROUND_CEILING))-1)
            return values[index]
        thresholds = {'volume_ratio':str(max(Decimal(CONFIG['holdout_ratio_floor']),quantile([Decimal(v['volume_ratio']) for v in vals]))),
                      'price_return':str(max(Decimal(CONFIG['holdout_price_floor']),quantile([Decimal(v['price_return']) for v in vals])))}
    store.db.execute('UPDATE short_experiments SET calibration_status=?,calibration_at=?,frozen_thresholds=?,calibration_evidence=? WHERE version=? AND calibration_status=\'pending\'',
                     ('frozen' if enough else 'insufficient',at,encoded(thresholds) if thresholds else None,encoded(evidence),VERSION))
    store.db.commit()
    return store.db.execute('SELECT * FROM short_experiments WHERE version=?',(VERSION,)).fetchone()


def assess(store, instruments, at, exp):
    phase = 'development' if timestamp(at)<timestamp(exp['development_until']) else 'holdout'
    thresholds = {'volume_ratio':CONFIG['development_volume_ratio'],'price_return':CONFIG['development_price_return']} if phase=='development' else json.loads(exp['frozen_thresholds']) if exp['frozen_thresholds'] else None
    results=[]
    for inst in instruments:
        if inst.get('kind') != 'spot' or inst.get('status') not in ('online','live','listed','tradable'):
            continue
        status,reason,values=features(closed_bars(store,inst['key'],at),inst,at,exp['created_at'])
        if phase=='holdout' and status=='complete' and timestamp(values['target_close'])<=timestamp(exp['development_until']):
            status,reason='unknown','pre_holdout_bar'
        if phase=='holdout' and status=='complete' and thresholds is None:
            status,reason='unknown','calibration_insufficient'
        dedup=hashlib.sha256(encoded([VERSION,inst['key'],values['target_close'],values['bar_id'],status,reason]).encode()).hexdigest()
        cur=store.db.execute('INSERT OR IGNORE INTO short_assessments(version,instrument_key,target_close,bar_id,observed_at,phase,status,reason,features,dedup) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (VERSION,inst['key'],values['target_close'],values['bar_id'],at,phase,status,reason,encoded(values),dedup))
        row=store.db.execute('SELECT * FROM short_assessments WHERE dedup=?',(dedup,)).fetchone()
        # Complete observations are immutable and must not trigger again in later cycles.
        results.append({'row':dict(row),'instrument':inst,'features':values,'new':bool(cur.rowcount), 'thresholds':thresholds})
    store.db.commit()
    return results


def matching_key(item):
    f=item['features'];inst=item['instrument']
    age=f.get('age_days')
    if age is None:
        return None
    turnover=Decimal(f['turnover_quote']);vol=Decimal(f['prior_volatility'])
    if turnover <= 0:
        return None
    return (inst['venue'],inst['kind'],inst['quote'],f['target_close'],
            0 if age<30 else 1 if age<365 else 2,
            int(turnover.log10()),int(vol*100))


def matching_depth(store, inst, at):
    """Use an actually received pre-decision book, never later backfilled depth."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='market_books'").fetchone():
        return None
    row=store.db.execute("SELECT * FROM market_books WHERE instrument_key=? AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1",(inst['key'],at)).fetchone()
    if not row:
        return None
    raw=dict(row)
    raw.update(bids=json.loads(raw['bids']),asks=json.loads(raw['asks']),kind=inst['kind'],
               quote_currency=inst['quote'],size_unit=inst['base'],execution_supported=inst['kind']=='spot')
    result=depth_quote(raw,'buy',CONFIG['notional_quote'],at,inst)
    if result['status']!='quoted':
        return None
    bid,ask=Decimal(raw['bids'][0][0]),Decimal(raw['asks'][0][0])
    band=Decimal(CONFIG['matching_depth_band_fraction'])
    ask_depth=sum(Decimal(p)*Decimal(q) for p,q in raw['asks'] if Decimal(p)<=ask*(1+band))
    bid_depth=sum(Decimal(p)*Decimal(q) for p,q in raw['bids'] if Decimal(p)>=bid*(1-band))
    depth=min(ask_depth,bid_depth)
    if depth<=0:
        return None
    spread=(ask-bid)/((ask+bid)/2)*10000
    return {'book_id':row['id'],'request_id':row['request_id'],'observed_at':row['observed_at'],
            'source_at':row['source_at'],'depth_log10':int(depth.log10()),
            'spread_bucket':int(spread/CONFIG['matching_spread_bin_bps']),
            'displayed_quote_depth':str(depth),'spread_bps':str(spread)}


def create_events(store, assessed, at):
    complete=[a for a in assessed if a['row']['status']=='complete']
    stats={'events':0,'triggered':0,'cooldown':0,'unmatched':0,'matched_controls':0}
    if not complete:
        return stats
    target=complete[0]['row']['target_close']
    decisions={};candidates={a['row']['id']:a for a in complete};triggered_by_rule={}
    for strategy in CONFIG['rules']:
        hits=[]
        for a in complete:
            f=a['features'];th=a['thresholds']
            volume=Decimal(f['volume_ratio'])>=Decimal(th['volume_ratio']) and Decimal(f['turnover_quote'])>=Decimal(CONFIG['minimum_quote_turnover'])
            price=Decimal(f['price_return'])>=Decimal(th['price_return']) and Decimal(f['turnover_quote'])>=Decimal(CONFIG['minimum_quote_turnover'])
            if volume if strategy=='volume' else price if strategy=='price' else volume and price:
                hits.append(a)
        hits.sort(key=lambda a:hashlib.sha256((target+strategy+a['instrument']['key']).encode()).hexdigest())
        triggered_by_rule[strategy]={a['row']['id'] for a in hits}
        prior=store.db.execute('SELECT * FROM short_hour_decisions WHERE version=? AND target_close=? AND strategy=?',(VERSION,target,strategy)).fetchone()
        if prior:
            decisions[strategy]={'at':prior['decided_at'],'selection':json.loads(prior['selection']),'new':False}
        else:
            decisions[strategy]={'at':at,'selection':{'treatments':[a['row']['id'] for a in hits[:CONFIG['selection_limit_per_rule_hour']]],'controls':{},'matching':{},'event_statuses':{}},'new':True}
    all_treatments={candidates[i]['instrument']['key'] for d in decisions.values() for i in d['selection']['treatments'] if i in candidates}
    depths={}
    def depth(a):
        key=a['instrument']['key']
        if key not in depths:
            depths[key]=matching_depth(store,a['instrument'],at)
        return depths[key]
    for strategy,d in decisions.items():
        if not d['new']:
            continue
        used=set()
        for ident in d['selection']['treatments']:
            treatment=candidates[ident];matchkey=matching_key(treatment);td=depth(treatment)
            info={'status':'matching_metadata_or_depth_unknown','treatment_depth':td,'control_depth':None}
            if matchkey is not None and td is not None:
                controls=[a for a in complete if a['instrument']['key'] not in all_treatments|used
                    and matching_key(a)==matchkey]
                controls.sort(key=lambda a:hashlib.sha256((target+strategy+treatment['instrument']['key']+a['instrument']['key']).encode()).hexdigest())
                info['status']='no_same_stratum_control'
                for control in controls:
                    cd=depth(control)
                    if cd and cd['depth_log10']==td['depth_log10'] and cd['spread_bucket']==td['spread_bucket']:
                        d['selection']['controls'][str(ident)]=control['row']['id']
                        used.add(control['instrument']['key'])
                        info.update(status='matched',control_depth=cd)
                        break
            d['selection']['matching'][str(ident)]=info
            available=max(timestamp(d['at']),timestamp(treatment['row']['observed_at']),timestamp(treatment['features']['current_received_at']))
            original_status='selected'
            if available-timestamp(treatment['row']['target_close'])>timedelta(seconds=CONFIG['bar_arrival_seconds']):
                original_status='decision_window_missed'
            prior=store.db.execute("SELECT triggered_at FROM short_events WHERE version=? AND instrument_key=? AND strategy=? AND status='selected' AND triggered_at<? ORDER BY triggered_at DESC LIMIT 1",(VERSION,treatment['instrument']['key'],strategy,available.isoformat())).fetchone()
            if original_status=='selected' and prior and available-timestamp(prior[0])<timedelta(hours=CONFIG['cooldown_hours']):
                original_status='cooldown'
            d['selection']['event_statuses'][str(ident)]=original_status
        store.db.execute('INSERT INTO short_hour_decisions VALUES(?,?,?,?,?)',(VERSION,target,strategy,d['at'],encoded(d['selection'])))
    for strategy,d in decisions.items():
        for a in complete:
            row=a['row'];ident=row['id'];hit=ident in triggered_by_rule[strategy]
            if store.db.execute('SELECT 1 FROM short_events WHERE version=? AND assessment_id=? AND strategy=?',(VERSION,ident,strategy)).fetchone():
                continue
            selected=ident in d['selection']['treatments']
            status=d['selection'].get('event_statuses',{}).get(str(ident),'selected') if selected else 'cohort_frozen' if hit else 'not_triggered'
            if hit:stats['triggered']+=1
            available=max(timestamp(d['at']),timestamp(row['observed_at']),timestamp(a['features']['current_received_at']))
            if available-timestamp(row['target_close'])>timedelta(seconds=CONFIG['bar_arrival_seconds']):
                status='decision_window_missed'
            prior=store.db.execute("SELECT triggered_at FROM short_events WHERE version=? AND instrument_key=? AND strategy=? AND status='selected' ORDER BY triggered_at DESC LIMIT 1",(VERSION,row['instrument_key'],strategy)).fetchone()
            if status=='selected' and prior and available-timestamp(prior[0])<timedelta(hours=CONFIG['cooldown_hours']):
                status='cooldown';stats['cooldown']+=1
            actionable=available+timedelta(seconds=CONFIG['entry_delay_seconds'])
            deadline=actionable+timedelta(seconds=CONFIG['entry_window_seconds'])
            matching=d['selection']['matching'].get(str(ident),{'status':'not_selected'})
            evidence={'features':a['features'],'thresholds':a['thresholds'],'matching':matching,
                      'candidate_match_key':matching_key(a),'cohort_decided_at':d['at']}
            cur=store.db.execute('INSERT INTO short_events(version,assessment_id,instrument_key,strategy,phase,target_close,triggered_at,actionable_at,entry_deadline,status,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (VERSION,ident,row['instrument_key'],strategy,row['phase'],row['target_close'],available.isoformat(),actionable.isoformat(),deadline.isoformat(),status,encoded(evidence)))
            stats['events']+=1
            if status!='selected':
                continue
            treatment_event=cur.lastrowid
            store.db.execute("INSERT INTO short_positions(event_id,status) VALUES(?,'pending')",(treatment_event,))
            control_id=d['selection']['controls'].get(str(ident))
            if control_id is None or control_id not in candidates:
                stats['unmatched']+=1;continue
            control=candidates[control_id]
            evidence={'matched_to':treatment_event,'features':control['features'],'matching':matching,
                      'candidate_match_key':matching_key(control),'cohort_decided_at':d['at']}
            cur=store.db.execute('INSERT OR IGNORE INTO short_events(version,assessment_id,instrument_key,strategy,phase,target_close,triggered_at,actionable_at,entry_deadline,status,matched_event_id,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (VERSION,control_id,control['instrument']['key'],'matched_random:'+strategy,row['phase'],target,available.isoformat(),actionable.isoformat(),deadline.isoformat(),'selected',treatment_event,encoded(evidence)))
            if cur.rowcount:
                store.db.execute("INSERT INTO short_positions(event_id,status) VALUES(?,'pending')",(cur.lastrowid,));stats['matched_controls']+=1
    store.db.commit()
    return stats


def recover_decisions(store, at):
    """Materialize frozen selected samples after any crash, including a new hour.

    Frozen decision time, thresholds, matching identities and assessments are the
    authority. Recovery never relabels today's book as an earlier entry.
    """
    recovered=0
    exp=store.db.execute('SELECT * FROM short_experiments WHERE version=?',(VERSION,)).fetchone()
    for d in store.db.execute('SELECT * FROM short_hour_decisions WHERE version=? ORDER BY decided_at,strategy',(VERSION,)).fetchall():
        chosen=json.loads(d['selection'])
        for ident in chosen['treatments']:
            assessment=store.db.execute('SELECT * FROM short_assessments WHERE id=? AND version=?',(ident,VERSION)).fetchone()
            if assessment is None:
                raise ValueError('frozen assessment evidence missing')
            values=json.loads(assessment['features'])
            available=max(timestamp(d['decided_at']),timestamp(assessment['observed_at']),timestamp(values['current_received_at']))
            actionable=available+timedelta(seconds=CONFIG['entry_delay_seconds'])
            deadline=actionable+timedelta(seconds=CONFIG['entry_window_seconds'])
            status=chosen.get('event_statuses',{}).get(str(ident),'selected')
            existing=store.db.execute('SELECT * FROM short_events WHERE version=? AND assessment_id=? AND strategy=?',(VERSION,ident,d['strategy'])).fetchone()
            if existing:
                event_id=existing['id'];status=existing['status']
            else:
                thresholds={'volume_ratio':CONFIG['development_volume_ratio'],'price_return':CONFIG['development_price_return']} if assessment['phase']=='development' else json.loads(exp['frozen_thresholds']) if exp['frozen_thresholds'] else None
                evidence={'features':values,'thresholds':thresholds,'matching':chosen['matching'].get(str(ident),{}),
                          'cohort_decided_at':d['decided_at'],'recovered_at':at}
                cur=store.db.execute('INSERT INTO short_events(version,assessment_id,instrument_key,strategy,phase,target_close,triggered_at,actionable_at,entry_deadline,status,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (VERSION,ident,assessment['instrument_key'],d['strategy'],assessment['phase'],assessment['target_close'],available.isoformat(),actionable.isoformat(),deadline.isoformat(),status,encoded(evidence)))
                event_id=cur.lastrowid;recovered+=1
            if status!='selected':
                continue
            missed=timestamp(at)>deadline
            store.db.execute('INSERT OR IGNORE INTO short_positions(event_id,status,reason) VALUES(?,?,?)',
                (event_id,'entry_unknown' if missed else 'pending','entry_window_missed' if missed else None))
            control_id=chosen['controls'].get(str(ident))
            if control_id is None:
                continue
            control=store.db.execute('SELECT * FROM short_assessments WHERE id=? AND version=?',(control_id,VERSION)).fetchone()
            if control is None:
                raise ValueError('frozen control evidence missing')
            cur=store.db.execute('INSERT OR IGNORE INTO short_events(version,assessment_id,instrument_key,strategy,phase,target_close,triggered_at,actionable_at,entry_deadline,status,matched_event_id,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (VERSION,control_id,control['instrument_key'],'matched_random:'+d['strategy'],control['phase'],control['target_close'],available.isoformat(),actionable.isoformat(),deadline.isoformat(),'selected',event_id,
                 encoded({'matched_to':event_id,'features':json.loads(control['features']),'matching':chosen['matching'].get(str(ident),{}),'cohort_decided_at':d['decided_at'],'recovered_at':at})))
            record=store.db.execute('SELECT id FROM short_events WHERE version=? AND assessment_id=? AND strategy=?',(VERSION,control_id,'matched_random:'+d['strategy'])).fetchone()
            store.db.execute('INSERT OR IGNORE INTO short_positions(event_id,status,reason) VALUES(?,?,?)',
                (record['id'],'entry_unknown' if missed else 'pending','entry_window_missed' if missed else None))
    store.db.commit()
    return recovered


def frozen_orphans(store):
    absent_events=absent_positions=0
    for d in store.db.execute('SELECT * FROM short_hour_decisions WHERE version=?',(VERSION,)):
        chosen=json.loads(d['selection'])
        for ident in chosen['treatments']:
            status=chosen.get('event_statuses',{}).get(str(ident),'selected')
            if status!='selected':continue
            checks=[(ident,d['strategy'])]
            control=chosen['controls'].get(str(ident))
            if control is not None:checks.append((control,'matched_random:'+d['strategy']))
            for assessment,strategy in checks:
                row=store.db.execute('SELECT e.id,p.id position FROM short_events e LEFT JOIN short_positions p ON p.event_id=e.id WHERE e.version=? AND e.assessment_id=? AND e.strategy=?',(VERSION,assessment,strategy)).fetchone()
                if row is None:absent_events+=1
                elif row['position'] is None:absent_positions+=1
    return {'frozen_selections_missing_events':absent_events,'selected_events_missing_positions':absent_positions}


def depth_quote(raw, side, amount, at, instrument=None):
    """Consume displayed spot depth; does not assert executions or reserves."""
    evidence={'reason':None,'levels_used':0,'mode':'displayed_depth_capacity_quote_not_fill'}
    unknown=lambda reason:dict(status='quote_unknown',quantity_base=None,gross_quote=None,evidence={**evidence,'reason':reason})
    if raw.get('kind')!='spot' or raw.get('execution_supported') is not True or raw.get('quote_currency')!=CONFIG['quote_currency']:
        return unknown('unsupported_book_identity_or_units')
    if not isinstance(raw.get('instrument_key'),str) or not isinstance(raw.get('size_unit'),str):
        return unknown('missing_book_identity_or_units')
    if instrument and (raw['instrument_key']!=instrument['key'] or raw['size_unit']!=instrument['base'] or raw['quote_currency']!=instrument['quote']):
        return unknown('book_instrument_mismatch')
    if raw.get('status')!='quoted':
        return unknown('source_quote_unknown')
    try:
        received,source=timestamp(raw['observed_at']),timestamp(raw['source_at'])
        current=timestamp(at)
    except (KeyError,ValueError,TypeError):
        return unknown('source_time_unknown')
    if received>current+timedelta(seconds=1) or source>received+timedelta(seconds=1) or current-source>timedelta(seconds=CONFIG['book_max_age_seconds']):
        return unknown('stale_or_future_book')
    bids,asks=raw.get('bids'),raw.get('asks')
    if not isinstance(bids,list) or not isinstance(asks,list) or not bids or not asks:
        return unknown('empty_depth')
    parsed=[]
    for side_rows in (bids,asks):
        levels=[]
        try:
            for row in side_rows:
                if not isinstance(row,(list,tuple)) or len(row)!=2:
                    return unknown('invalid_depth')
                p,q=dec(row[0],True),dec(row[1],True)
                if p is None or q is None:
                    return unknown('invalid_depth')
                levels.append((p,q))
        except (TypeError,IndexError):
            return unknown('invalid_depth')
        parsed.append(levels)
    bids,asks=parsed
    if any(a[0]<=b[0] for a,b in zip(bids,bids[1:])) or any(a[0]>=b[0] for a,b in zip(asks,asks[1:])) or bids[0][0]>=asks[0][0]:
        return unknown('unsorted_or_crossed_depth')
    remaining=dec(amount,True)
    if remaining is None or side not in ('buy','sell'):
        return unknown('invalid_order')
    total_quote=Decimal(0);quantity=Decimal(0)
    for price,size in asks if side=='buy' else bids:
        take=min(size,remaining/price) if side=='buy' else min(size,remaining)
        quantity+=take;total_quote+=take*price
        remaining-=take*price if side=='buy' else take
        evidence['levels_used']+=1
        if remaining<=Decimal('1e-24'):
            break
    if remaining>Decimal('1e-24'):
        return unknown('insufficient_displayed_capacity')
    evidence.update(best_bid=str(bids[0][0]),best_ask=str(asks[0][0]),spread_fraction=str((asks[0][0]-bids[0][0])/asks[0][0]))
    return dict(status='quoted',quantity_base=str(quantity),gross_quote=str(total_quote),evidence=evidence)


def execution_quote(store, raw, instrument_key, side, amount, at, instrument=None):
    result=depth_quote(raw,side,amount,at,instrument)
    if raw.get("instrument_key") != instrument_key:
        result=dict(status="quote_unknown",quantity_base=None,gross_quote=None,evidence={"reason":"book_instrument_mismatch"})
    if result['status']=='quoted' and raw.get('request_id') is None:
        result=dict(status='quote_unknown',quantity_base=None,gross_quote=None,evidence={'reason':'raw_request_evidence_missing'})
    cur=store.db.execute('INSERT INTO short_execution_quotes(version,instrument_key,request_id,observed_at,source_at,side,amount,status,quantity_base,gross_quote,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (VERSION,instrument_key,raw.get('request_id'),at,raw.get('source_at'),side,str(amount),result['status'],result['quantity_base'],result['gross_quote'],encoded(result['evidence'])))
    store.db.commit()
    return {'id':cur.lastrowid,**result}


def returns_for(position, gross):
    entry_gross=Decimal(CONFIG['notional_quote']);gross=Decimal(gross)
    return {str(bps):str((gross*(1-Decimal(bps)/10000))/(entry_gross*(1+Decimal(bps)/10000))-1) for bps in CONFIG['fees_bps_per_side']}


def mark_outcome(store, position_id, horizon, at, status, quote=None, position=None):
    stress=returns_for(position,quote['gross_quote']) if quote and quote['status']=='quoted' else {}
    net=stress.get(str(CONFIG['base_fee_bps']))
    proceeds=str(Decimal(quote['gross_quote'])*(1-Decimal(CONFIG['base_fee_bps'])/10000)) if stress else None
    store.db.execute('INSERT OR IGNORE INTO short_outcomes VALUES(?,?,?,?,?,?,?,?)',
        (position_id,horizon,at,status,quote['id'] if quote else None,proceeds,net,encoded(stress)))


def positions(store, client, run, at, instruments):
    keyed={i['key']:i for i in instruments};cache={};stats={'entered':0,'entry_unknown':0,'exits':0,'path_marks':0}
    def fetch(key):
        if key not in cache:
            if len(cache)>=CONFIG['max_quote_instruments_per_cycle']:
                return {'status':'quote_unknown','observed_at':now(),'reason':'cycle_quote_budget'}
            cache[key]=book(store,client,run,key)
        return cache[key]
    for p in store.db.execute('''SELECT p.*,e.instrument_key,e.actionable_at,e.entry_deadline,e.phase
      FROM short_positions p JOIN short_events e ON e.id=p.event_id WHERE p.status='pending' ORDER BY e.actionable_at,p.id''').fetchall():
        check=timestamp(now())
        if check<timestamp(p['actionable_at']):
            continue
        if check>timestamp(p['entry_deadline']):
            store.db.execute("UPDATE short_positions SET status='entry_unknown',reason='entry_window_missed' WHERE id=?",(p['id'],));stats['entry_unknown']+=1;continue
        active=store.db.execute("SELECT COUNT(*) FROM short_positions p WHERE p.status='open' AND NOT EXISTS(SELECT 1 FROM short_outcomes o WHERE o.position_id=p.id AND o.horizon_hours=24)").fetchone()[0]
        if active>=CONFIG['max_pending_positions']:
            store.db.execute("UPDATE short_positions SET status='entry_unknown',reason='position_budget' WHERE id=?",(p['id'],));stats['entry_unknown']+=1;continue
        if p['instrument_key'] not in keyed or keyed[p['instrument_key']]['kind']!='spot':
            store.db.execute("UPDATE short_positions SET status='entry_unknown',reason='instrument_unavailable' WHERE id=?",(p['id'],));stats['entry_unknown']+=1;continue
        raw=fetch(p['instrument_key']);quote_at=now()
        if timestamp(quote_at)>timestamp(p['entry_deadline']):
            store.db.execute("UPDATE short_positions SET status='entry_unknown',reason='entry_request_crossed_deadline' WHERE id=?",(p['id'],));stats['entry_unknown']+=1;continue
        if raw.get('observed_at') and timestamp(raw['observed_at'])<timestamp(p['actionable_at']):
            raw={**raw,'status':'quote_unknown','reason':'book_received_before_actionable'}
        q=execution_quote(store,raw,p['instrument_key'],'buy',CONFIG['notional_quote'],quote_at,keyed[p['instrument_key']])
        if q['status']=='quoted':
            cost=Decimal(q['gross_quote'])*(1+Decimal(CONFIG['base_fee_bps'])/10000)
            store.db.execute("UPDATE short_positions SET status='open',entry_at=?,quantity_base=?,entry_cost_quote=?,entry_quote_id=? WHERE id=?",(quote_at,q['quantity_base'],str(cost),q['id'],p['id']));stats['entered']+=1
        else:
            store.db.execute("UPDATE short_positions SET status='entry_unknown',entry_at=?,entry_quote_id=?,reason='entry_quote_unknown' WHERE id=?",(quote_at,q['id'],p['id']));stats['entry_unknown']+=1
    store.db.commit()
    for p in store.db.execute("SELECT p.*,e.instrument_key FROM short_positions p JOIN short_events e ON e.id=p.event_id WHERE p.status='open' ORDER BY p.entry_at,p.id").fetchall():
        check=timestamp(now());age=check-timestamp(p['entry_at']);due=[]
        for horizon in CONFIG['horizons_hours']:
            if age<timedelta(hours=horizon) or store.db.execute('SELECT 1 FROM short_outcomes WHERE position_id=? AND horizon_hours=?',(p['id'],horizon)).fetchone():
                continue
            if age>timedelta(hours=horizon,seconds=CONFIG['exit_tolerance_seconds']):
                mark_outcome(store,p['id'],horizon,now(),'missed_window');stats['exits']+=1
            else:
                due.append(horizon)
        store.db.commit()
        if age>timedelta(hours=24,seconds=CONFIG['exit_tolerance_seconds']):
            continue
        raw=fetch(p['instrument_key']);quote_at=now()
        q=execution_quote(store,raw,p['instrument_key'],'sell',p['quantity_base'],quote_at,keyed.get(p['instrument_key']))
        stress=returns_for(p,q['gross_quote']) if q['status']=='quoted' else {}
        net=stress.get(str(CONFIG['base_fee_bps']))
        store.db.execute('INSERT OR IGNORE INTO short_path_marks VALUES(?,?,?,?,?)',(p['id'],quote_at,q['status'],q['id'],net));stats['path_marks']+=1
        for horizon in due:
            late=timestamp(quote_at)>timestamp(p['entry_at'])+timedelta(hours=horizon,seconds=CONFIG['exit_tolerance_seconds'])
            mark_outcome(store,p['id'],horizon,quote_at,'missed_window' if late else q['status'],None if late else q,p)
            stats['exits']+=1
    store.db.commit()
    return stats


def cycle(store, client):
    setup(store);at=now();exp=calibration(store,at)
    instruments=universe(store)
    if not instruments:
        return {'status':'universe_unavailable','notes':['no frozen market universe']}
    run=store.start({'module':'short_experiment','version':VERSION,'mode':CONFIG['mode']})
    try:
        recovered=recover_decisions(store,at)
        position_stats=positions(store,client,run,at,instruments)
        at=now();exp=calibration(store,at)
        if timestamp(at)>=timestamp(exp['created_at'])+timedelta(days=CONFIG['enrollment_days']):
            event_stats={'status':'enrollment_complete'};assessed=[]
        else:
            assessed=assess(store,instruments,at,exp)
            event_stats=create_events(store,assessed,at)
        result={'run_id':run,'version':VERSION,'phase':'development' if timestamp(at)<timestamp(exp['development_until']) else 'holdout',
                'recovered_events':recovered,'assessments':len(assessed),'unknown':sum(x['row']['status']!='complete' for x in assessed),
                **position_stats,**event_stats,'calibration_status':exp['calibration_status'],'notes':[]}
        store.finish(run,[])
        return result
    except BaseException:
        store.finish(run,['interrupted']);raise


def report(store):
    setup(store)
    exp=store.db.execute('SELECT * FROM short_experiments WHERE version=?',(VERSION,)).fetchone()
    lines=['# Short-horizon prospective experiment','',f'Generated UTC: {now()}', '',
           f"Version {VERSION}; created {exp['created_at']}; development ends {exp['development_until']}; calibration {exp['calibration_status']}.",
           'Development results are exploratory, never holdout evidence. Threshold calibration uses only complete development observations received before the boundary and is frozen once. Insufficient calibration prevents holdout signals.',
           'Spot long only, 100 USDT displayed-book capacity quote. No orders, fills, leverage, funding charges or wallet operations. Fees assume 10 bps per side; 0/10/30 bps scenarios are assumptions, not account-specific venue fees.',
           'Hours are 1/4/24 from actual entry quote time, with a 10-minute exit window. Missing windows are unknown and never filled with later prices. Sampled paths can miss intraperiod highs/lows and cannot prove stop execution.', '',
           'Controls are matched without replacement within each strategy/hour, excluding all selected treatment assets, on venue/quote/type, listing-age bucket, quote-turnover decade, prior-volatility bucket, displayed depth decade and spread bucket. Missing age or fresh pre-entry depth leaves an explicit unmatched record; turnover is not liquidity. No matched alpha conclusion is established.', '',
           '| Phase | Assessment status | Reason | Records |','|---|---|---|---:|']
    for r in store.db.execute('SELECT phase,status,reason,COUNT(*) n FROM short_assessments GROUP BY phase,status,reason ORDER BY phase,status,reason'):
        lines.append(f"| {r['phase']} | {r['status']} | {r['reason'] or ''} | {r['n']} |")
    lines+=['','Frozen selection recovery audit: '+encoded(frozen_orphans(store)), '', '| Phase | Strategy | Event status | Records |','|---|---|---|---:|']
    for r in store.db.execute('SELECT phase,strategy,status,COUNT(*) n FROM short_events GROUP BY phase,strategy,status'):
        lines.append(f"| {r['phase']} | {r['strategy']} | {r['status']} | {r['n']} |")
    lines+=['','| Position status | Reason | Records |','|---|---|---:|']
    for r in store.db.execute('SELECT status,reason,COUNT(*) n FROM short_positions GROUP BY status,reason'):
        lines.append(f"| {r['status']} | {r['reason'] or ''} | {r['n']} |")
    lines+=['','## Outcomes and missingness','',
            '| Phase | Strategy | Horizon h | Due | Quoted | Unknown | Not evaluated | Unobserved fraction | Median known | Minimum known | Mean without largest winner | Positive fraction known |',
            '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    at=timestamp(now());groups={}
    for p in store.db.execute('''SELECT p.*,e.phase,e.strategy FROM short_positions p JOIN short_events e ON e.id=p.event_id WHERE p.status='open' '''):
        for horizon in CONFIG['horizons_hours']:
            if at<timestamp(p['entry_at'])+timedelta(hours=horizon):
                continue
            g=groups.setdefault((p['phase'],p['strategy'],horizon),{'due':0,'known':[],'unknown':0,'pending':0})
            g['due']+=1
            out=store.db.execute('SELECT * FROM short_outcomes WHERE position_id=? AND horizon_hours=?',(p['id'],horizon)).fetchone()
            if out is None:g['pending']+=1
            elif out['net_return'] is None:g['unknown']+=1
            else:g['known'].append(Decimal(out['net_return']))
    for (phase,strategy,horizon),g in sorted(groups.items()):
        values=sorted(g['known']);without=values[:-1]
        lines.append(f"| {phase} | {strategy} | {horizon} | {g['due']} | {len(values)} | {g['unknown']} | {g['pending']} | {Decimal(g['unknown']+g['pending'])/g['due']} | {median(values) if values else 'undefined'} | {min(values) if values else 'undefined'} | {sum(without)/len(without) if without else 'undefined'} | {sum(v>0 for v in values)/len(values) if values else 'undefined'} |")
    if not groups:
        lines.append('\nNo horizons are due; unknown/failure fractions and performance are undefined, not zero.')
    lines+=['','## Explicit matched pairs','',
            '| Phase | Treatment strategy | Horizon h | Due treatment pairs | Both exits known | Pair incomplete | Mean paired return difference |',
            '|---|---|---:|---:|---:|---:|---:|']
    pairs={}
    for r in store.db.execute("""SELECT c.id control_event,c.phase,t.strategy,t.id treatment_event,
      p.id treatment_position,p.entry_at treatment_entry,p.status treatment_status,
      cp.id control_position,cp.status control_status
      FROM short_events c JOIN short_events t ON t.id=c.matched_event_id
      LEFT JOIN short_positions p ON p.event_id=t.id
      LEFT JOIN short_positions cp ON cp.event_id=c.id WHERE c.matched_event_id IS NOT NULL"""):
        if not r['treatment_entry'] or r['treatment_status']!='open':continue
        for horizon in CONFIG['horizons_hours']:
            if at<timestamp(r['treatment_entry'])+timedelta(hours=horizon):continue
            g=pairs.setdefault((r['phase'],r['strategy'],horizon),{'due':0,'diffs':[]})
            g['due']+=1
            a=store.db.execute('SELECT net_return FROM short_outcomes WHERE position_id=? AND horizon_hours=?',(r['treatment_position'],horizon)).fetchone()
            b=store.db.execute('SELECT net_return FROM short_outcomes WHERE position_id=? AND horizon_hours=?',(r['control_position'],horizon)).fetchone()
            if a and b and a[0] is not None and b[0] is not None:g['diffs'].append(Decimal(a[0])-Decimal(b[0]))
    for (phase,strategy,horizon),g in sorted(pairs.items()):
        values=g['diffs']
        lines.append(f"| {phase} | {strategy} | {horizon} | {g['due']} | {len(values)} | {g['due']-len(values)} | {sum(values)/len(values) if values else 'undefined'} |")
    lines+=['', 'Pairwise results are descriptive and exclude unmatched treatments; matching coverage and unknown exits must accompany them. A matched observation is not causal or independent evidence.']
    lines+=['','## Fee sensitivity (known exits only)','', '| Phase | Strategy | Horizon h | Fee bps/side | Known | Mean return |','|---|---|---:|---:|---:|---:|']
    sensitivity={}
    for r in store.db.execute('''SELECT e.phase,e.strategy,o.horizon_hours,o.stress_returns FROM short_outcomes o JOIN short_positions p ON p.id=o.position_id JOIN short_events e ON e.id=p.event_id'''):
        for fee,value in json.loads(r['stress_returns']).items():
            sensitivity.setdefault((r['phase'],r['strategy'],r['horizon_hours'],fee),[]).append(Decimal(value))
    for (phase,strategy,horizon,fee),values in sorted(sensitivity.items()):
        lines.append(f'| {phase} | {strategy} | {horizon} | {fee} | {len(values)} | {sum(values)/len(values)} |')
    lines+=['','## Sampled excursions (displayed sell depth)','', '| Position | Known sampled marks | Unknown marks | Sampled favorable return | Sampled adverse return |', '|---|---:|---:|---:|---:|']
    for r in store.db.execute('SELECT position_id,COUNT(*) n,SUM(net_return IS NULL) unknown FROM short_path_marks GROUP BY position_id'):
        values=[Decimal(x[0]) for x in store.db.execute('SELECT net_return FROM short_path_marks WHERE position_id=? AND net_return IS NOT NULL',(r['position_id'],))]
        lines.append(f"| {r['position_id']} | {len(values)} | {r['unknown']} | {max(values) if values else 'undefined'} | {min(values) if values else 'undefined'} |")
    lines+=['','Repeated instruments, correlated hours, quote/fill differences, frozen-subset coverage and missing exits limit inference. Unknowns are retained; no profitability claim is established by implementation or test success.','']
    return '\n'.join(lines)
