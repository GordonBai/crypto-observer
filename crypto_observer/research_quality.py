"""Read-only diagnostics; never change enrollment, matching, or historical outcomes."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import random
from .study_plan import capacity
from pathlib import Path
import sqlite3


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')  # One consistent view while the collector continues writing.
    return db


def capacity_plan(budget, interval_seconds, freshness_seconds, retained):
    if min(budget, interval_seconds, freshness_seconds) <= 0 or retained < 0:
        raise ValueError('positive budgets/times and nonnegative retained required')
    waves = math.ceil(retained / budget)
    return {'retained': retained, 'budget_per_cycle': budget,
            'estimated_rotation_seconds': waves * interval_seconds,
            'maximum_bounded_universe': capacity(budget, interval_seconds, freshness_seconds)['maximum_assets'],
            'assumptions': 'No growth, no failures, fixed spacing; not an SLA or admission decision.'}


def short_quality(db, asof):
    study = db.execute('SELECT * FROM short_experiments ORDER BY created_at DESC LIMIT 1').fetchone()
    if study is None:
        return {'status': 'study_missing'}
    config = json.loads(study['config'])
    start = timestamp(study['created_at'])
    boundary = timestamp(study['development_until'])
    end = min(asof, start + timedelta(days=config['enrollment_days']))
    grace = timedelta(seconds=config['bar_arrival_seconds'])
    instruments = [r[0] for r in db.execute("SELECT key FROM market_instruments WHERE enrolled=1 AND kind='spot'")]
    # Only hour opportunities whose full decision window has elapsed are scored.
    hours = []
    hour = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while hour <= end and hour + grace <= asof:
        hours.append(hour)
        hour += timedelta(hours=1)
    rows = db.execute('SELECT * FROM short_assessments WHERE version=? AND observed_at<=?',
                      (study['version'], asof.isoformat())).fetchall()
    observed, complete, reasons = set(), set(), Counter()
    for r in rows:
        close = timestamp(r['target_close'])
        key = (r['instrument_key'], close)
        if r['instrument_key'] not in instruments or close not in hours:
            continue
        observed.add(key)
        timely = close <= timestamp(r['observed_at']) <= close + grace
        features = json.loads(r['features']) if 'features' in r.keys() else {}
        received = features.get('current_received_at')
        timely = timely and received is not None and close <= timestamp(received) <= close + grace
        if r['status'] == 'complete' and timely:
            complete.add(key)
        else:
            reasons[r['reason'] or 'incomplete_or_late'] += 1
    coverage = []
    for key in instruments:
        n = sum((key, h) in complete for h in hours)
        coverage.append({'instrument': key, 'expected_hours': len(hours), 'timely_complete_hours': n,
                         'never_assessed_hours': sum((key, h) not in observed for h in hours),
                         'timely_complete_fraction': n / len(hours) if hours else None})
    events = db.execute('SELECT * FROM short_events WHERE version=? AND triggered_at<=?',
                        (study['version'], asof.isoformat())).fetchall()
    groups = {}
    for phase in ('development', 'holdout'):
        for strategy in config['rules']:
            selected = [e for e in events if e['phase'] == phase and e['strategy'] == strategy and e['status'] == 'selected']
            assets = Counter(e['instrument_key'] for e in selected)
            matching = Counter(json.loads(e['evidence']).get('matching', {}).get('status', 'unknown') for e in selected)
            outcomes, entries = Counter(), Counter()
            for e in selected:
                p = db.execute('SELECT * FROM short_positions WHERE event_id=?', (e['id'],)).fetchone()
                entries[p['status'] if p else 'position_missing'] += 1
                for horizon in config['horizons_hours']:
                    if p is None or p['entry_at'] is None or p['status'] in ('entry_unknown', 'pending'):
                        status = 'entry_unavailable'
                    elif timestamp(p['entry_at']) + timedelta(hours=horizon) > asof:
                        status = 'not_due'
                    else:
                        o = db.execute('SELECT status FROM short_outcomes WHERE position_id=? AND horizon_hours=? AND observed_at<=?',
                                       (p['id'], horizon, asof.isoformat())).fetchone()
                        status = o['status'] if o else 'due_unrecorded'
                    outcomes[f'{horizon}h:{status}'] += 1
            groups[f'{phase}:{strategy}'] = {'selected_events': len(selected), 'distinct_assets': len(assets),
                'asset_counts': dict(assets), 'largest_asset_fraction': max(assets.values()) / len(selected) if selected else None,
                'matching': dict(matching), 'matched_fraction': matching['matched'] / len(selected) if selected else None,
                'entry_statuses': dict(entries), 'horizon_statuses': dict(outcomes)}
    return {'version': study['version'], 'asof': asof.isoformat(), 'study_start': start.isoformat(),
            'development_until': boundary.isoformat(), 'expected_asset_hours': len(hours)*len(instruments),
            'timely_complete_asset_hours': len(complete), 'per_instrument': coverage,
            'assessment_failure_rows': dict(reasons), 'groups': groups,
            'denominator': 'Every enrolled spot instrument and elapsed UTC close after enrollment through enrollment end; full decision grace must have elapsed. Includes warm-up and offline hours. Failure rows may repeat an asset-hour.',
            'limitations': ['Historical asof does not reconstruct mutable position status; use current audits or immutable saved reports.',
                           'Events, overlapping horizons, repeated assets and cross-rule selections are not independent samples.',
                           'No confidence or causal claim: few independent asset/time clusters; matched results exclude unmatched events.']}


def pair_quality(db, asof):
    """Exact ledger pairs only; asset bootstrap is exploratory, not time-robust."""
    groups = {}
    rows = db.execute("""SELECT t.instrument_key,t.target_close,t.strategy,t.phase,
        t.evidence treatment_evidence,c.evidence control_evidence,
        tp.id treatment_position,cp.id control_position
        FROM short_events c JOIN short_events t ON t.id=c.matched_event_id
        LEFT JOIN short_positions tp ON tp.event_id=t.id
        LEFT JOIN short_positions cp ON cp.event_id=c.id
        WHERE c.triggered_at<=? AND t.triggered_at<=?""", (asof.isoformat(),asof.isoformat())).fetchall()
    for r in rows:
        for h in (1,4,24):
            key=f"{r['phase']}:{r['strategy']}:{h}h"
            g=groups.setdefault(key,{'ledger_pairs':0,'complete_pairs':0,'incomplete_pairs':0,
                'asset_differences':defaultdict(list),'dates':set(), 'covariate_differences':defaultdict(list)})
            g['ledger_pairs']+=1
            values=[]
            for ident in (r['treatment_position'],r['control_position']):
                o=db.execute('SELECT status,net_return FROM short_outcomes WHERE position_id=? AND horizon_hours=? AND observed_at<=?',(ident,h,asof.isoformat())).fetchone()
                values.append(float(o['net_return']) if o and o['status']=='quoted' and o['net_return'] is not None else None)
            if None in values:
                g['incomplete_pairs']+=1
                continue
            g['complete_pairs']+=1
            g['asset_differences'][r['instrument_key']].append(values[0]-values[1])
            g['dates'].add(r['target_close'][:10])
            t=json.loads(r['treatment_evidence']).get('features',{})
            c=json.loads(r['control_evidence']).get('features',{})
            for field in ('age_days','turnover_quote','prior_volatility'):
                if t.get(field) is not None and c.get(field) is not None:
                    g['covariate_differences'][field].append(float(t[field])-float(c[field]))
    for g in groups.values():
        assets=g.pop('asset_differences'); values=[v for vs in assets.values() for v in vs]
        g['distinct_assets']=len(assets);g['distinct_days']=len(g.pop('dates'))
        g['mean_pair_difference']=sum(values)/len(values) if values else None
        cov=g.pop('covariate_differences')
        g['covariate_balance']={field:{'pairs_with_values':len(v),'mean_signed_difference':sum(v)/len(v)} for field,v in cov.items()}
        g['covariate_balance_scope']='Complete pairs only, available recorded features; no population balance or causality claim.'
        g['missing_sensitivity']={'scenario_mean_differences': {str(delta):(sum(values)+g['incomplete_pairs']*delta)/g['ledger_pairs'] for delta in (-1,0,1)}, 'note':'Assign each incomplete pair a hypothetical treatment-minus-control difference of -100/0/+100 percentage points, including pending pairs. Scenarios only, not bounds or observed returns.'}
        g['asset_bootstrap']={'status':'insufficient_clusters','minimum_assets':20}
        if len(assets)>=20:
            rng=random.Random(0); blocks=list(assets.values()); draws=[]
            for _ in range(2000):
                sample=[v for b in rng.choices(blocks,k=len(blocks)) for v in b]
                draws.append(sum(sample)/len(sample))
            draws.sort()
            g['asset_bootstrap']={'status':'exploratory','seed':0,'replicates':2000,
                 'percentile_95':[draws[49],draws[1949]],'warning':'Asset clusters only; common time shocks and serial dependence unresolved. Not a confirmatory interval.'}
    return groups


def holder_quality(db):
    counts = Counter()
    by_asset = defaultdict(list)
    for r in db.execute('SELECT token_address, observed_at, status, caught_up FROM indexed_holder_snapshots'):
        counts[f"{r['status']}:caught_up={r['caught_up']}"] += 1
        if r['caught_up'] and r['status'] == 'event_supply_reconciled':
            by_asset[r['token_address']].append(timestamp(r['observed_at']))
    paired = {}
    for token, values in by_asset.items():
        values.sort()
        eligible = sum(any(timedelta(hours=20) <= current - old <= timedelta(hours=28)
                           for old in values[:i]) for i, current in enumerate(values))
        if eligible:
            paired[token] = eligible
    return {'snapshot_quality': dict(counts), 'qualified_assets': len(by_asset),
            'assets_with_repeated_qualified_snapshots': sum(len(v)>1 for v in by_asset.values()),
            'assets_with_20_28h_pairs': len(paired), 'qualified_pair_endpoints': paired,
            'note': 'Pairs establish temporal input availability only, not current freshness, positive holder counts, strategy eligibility, beneficial ownership or growth.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--short-db', default='data/short_term_v2.sqlite3')
    parser.add_argument('--base-db', default='data/observer.sqlite3')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    at = datetime.now(timezone.utc)
    short, base = readonly(args.short_db), readonly(args.base_db)
    try:
        result = {'generated_at': at.isoformat(), 'mode': 'read_only_diagnostic',
                  'short': short_quality(short, at), 'pairs': pair_quality(short, at), 'holders': holder_quality(base),
                  'base_capacity_nominal': capacity_plan(300, 900, 7200, base.execute('SELECT COUNT(*) FROM pools').fetchone()[0])}
    finally:
        short.close()
        base.close()
    content = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        output = Path(args.output)
        if output.resolve() in {Path(args.short_db).resolve(), Path(args.base_db).resolve()}:
            parser.error('output must not overwrite an input database')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content)
    else:
        print(content, end='')


if __name__ == '__main__':
    main()
