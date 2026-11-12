"""Read-only, reproducible quote diagnostics; never rewrites research outcomes."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from .short_experiment import depth_quote


def select_cases(cases, seed='quote-audit-v1', per_stratum=5):
    if per_stratum < 1:
        raise ValueError('per_stratum must be positive')
    groups = defaultdict(list)
    for case in cases:
        value = case.get('return')
        group = 'unknown' if value is None else 'extreme' if abs(Decimal(str(value))) >= Decimal('.5') else 'normal'
        groups[(case['version'], case.get('phase', 'unspecified'), group)].append(case)
    selected = []
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda r: hashlib.sha256((seed + ':' + r['id']).encode()).hexdigest())
        selected.extend(dict(r, stratum=key[-1], stratum_population=len(rows)) for r in rows[:per_stratum])
    return selected


def request_evidence(db, request_id):
    if request_id is None:
        return {'request_id': None, 'integrity': 'missing', 'diagnosis': 'request_missing'}
    row = db.execute('SELECT id,status,error,sha256,body,started_at,observed_at FROM requests WHERE id=?', (request_id,)).fetchone()
    if row is None:
        return {'request_id': request_id, 'integrity': 'missing', 'diagnosis': 'request_missing'}
    body = row['body']
    body = body.encode() if isinstance(body, str) else bytes(body)
    valid = hashlib.sha256(body).hexdigest() == row['sha256']
    return {'request_id': request_id, 'integrity': 'verified' if valid else 'sha256_mismatch',
            'sha256': row['sha256'], 'http_status': row['status'],
            'started_at': row['started_at'], 'received_at': row['observed_at'],
            'diagnosis': 'request_error' if row['error'] or (row['status'] is not None and row['status'] >= 400) else 'requires_payload_review'}


def audit(db, seed='quote-audit-v1', per_stratum=5):
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cases = []
    if 'short_outcomes' in tables:
        for r in db.execute('''SELECT o.*, p.entry_quote_id,e.version,e.phase,e.instrument_key,e.strategy
          FROM short_outcomes o JOIN short_positions p ON p.id=o.position_id JOIN short_events e ON e.id=p.event_id'''):
            cases.append({'id': f"short:{r['position_id']}:{r['horizon_hours']}", 'version': r['version'], 'phase': r['phase'],
                'asset': r['instrument_key'], 'strategy': r['strategy'], 'original_status': r['status'], 'return': r['net_return'],
                'quote_table': 'short_execution_quotes', 'entry_quote_id': r['entry_quote_id'], 'exit_quote_id': r['quote_id']})
        for r in db.execute("SELECT p.*,e.version,e.phase,e.instrument_key,e.strategy FROM short_positions p JOIN short_events e ON e.id=p.event_id WHERE p.status='entry_unknown'"):
            cases.append({'id':f"short:{r['id']}:entry",'version':r['version'],'phase':r['phase'],'asset':r['instrument_key'],
                'strategy':r['strategy'],'original_status':r['status'],'return':None,'quote_table':'short_execution_quotes',
                'entry_quote_id':r['entry_quote_id'],'exit_quote_id':None})
    if 'paper_marks' in tables:
        for r in db.execute('''SELECT m.*,p.entry_quote_id,p.notional_usdc,s.version,s.token_address,s.strategy
          FROM paper_marks m JOIN paper_positions p ON p.id=m.position_id JOIN paper_signals s ON s.id=p.signal_id'''):
            value = None if r['net_value_usdc'] is None else str(Decimal(r['net_value_usdc']) / Decimal(r['notional_usdc']) - 1)
            cases.append({'id':f"base:{r['id']}",'version':r['version'],'asset':r['token_address'],'strategy':r['strategy'],
                'original_status':r['status'],'return':value,'quote_table':'route_quotes','entry_quote_id':r['entry_quote_id'],'exit_quote_id':r['quote_id']})
        for r in db.execute("SELECT p.*,s.version,s.token_address,s.strategy FROM paper_positions p JOIN paper_signals s ON s.id=p.signal_id WHERE p.entry_status!='quoted'"):
            cases.append({'id':f"base:{r['id']}:entry",'version':r['version'],'asset':r['token_address'],
                'strategy':r['strategy'],'original_status':r['entry_status'],'return':None,'quote_table':'route_quotes',
                'entry_quote_id':r['entry_quote_id'],'exit_quote_id':None})
    selected = select_cases(cases, seed, per_stratum)
    for case in selected:
        case['quotes'] = {}
        for side in ('entry', 'exit'):
            quote_id = case[side + '_quote_id']
            row = db.execute('SELECT * FROM ' + case['quote_table'] + ' WHERE id=?', (quote_id,)).fetchone()
            if row is None:
                case['quotes'][side] = {'quote_id': quote_id, 'diagnosis': 'quote_record_missing_or_window_missed'}
                continue
            quote = dict(row)
            evidence = request_evidence(db, quote.get('request_id'))
            if 'evidence' in quote:
                quote['evidence'] = json.loads(quote['evidence'])
            case['quotes'][side] = {'quote': quote, 'request': evidence}
        case['review_status'] = 'manual_identity_units_route_liquidity_review_required'
    return {'audit_version':'quote-audit-v1','mode':'read_only_diagnostics_not_execution', 'seed':seed,
        'per_stratum':per_stratum,'selection':'SHA256(seed:case_id), per version/phase/stratum; abs return >= 0.5 is extreme',
        'population':len(cases),'population_manifest_sha256':hashlib.sha256(json.dumps(sorted(cases,key=lambda c:c['id']),sort_keys=True).encode()).hexdigest(),'original_status_counts':dict(Counter(c['original_status'] for c in cases)),
        'cases':selected,'limitations':['Sampling is diagnostic, not representative return estimation.',
        'Base normalized mark/notional change excludes entry gas; it is an audit sorting metric, not portfolio return.',
        'Raw SHA verifies retained bytes, not truth of venue data. Missing/no route does not establish fraud.',
        'Independent AMM quotes do not apply hypothetical entry reserve changes.']}


def sensitivity(raw, amounts=('10','100','1000'), retained_depth=('1','.5','.25'), delays=(0,5,30)):
    """Frozen book stress, not a forecast of future liquidity or simulated fills."""
    results=[]
    for amount in amounts:
        if not Decimal(amount).is_finite() or Decimal(amount)<=0:
            raise ValueError('amount must be finite and positive')
        for fraction in retained_depth:
            f=Decimal(fraction)
            if not f.is_finite() or not 0 < f <= 1:
                raise ValueError('retained depth must be in (0,1]')
            for delay in delays:
                if delay < 0:
                    raise ValueError('delay must be nonnegative')
                adjusted=dict(raw)
                for side in ('asks','bids'):
                    adjusted[side]=[[p,str(Decimal(q)*f)] for p,q in raw[side]]
                at=(datetime.fromisoformat(raw['observed_at'])+timedelta(seconds=delay)).isoformat()
                result=depth_quote(adjusted,'buy',amount,at)
                fees={str(bps):str(Decimal(amount)*(1+Decimal(bps)/10000)) for bps in (0,10,30)}
                results.append({'notional_quote':amount,'retained_depth':fraction,'delay_seconds':delay,
                    'entry_cost_fee_scenarios':fees,'result':result})
    return {'version':'book-sensitivity-v1','mode':'frozen_displayed_book_stress_not_fill',
        'limitations':['Delay only tests staleness of the same book; it does not predict later prices.',
            'Depth haircut is hypothetical. No exit return or executed capacity is established.'], 'scenarios':results}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--db');source.add_argument('--book',help='normalized spot book JSON for independent sensitivity')
    parser.add_argument('--seed',default='quote-audit-v1');parser.add_argument('--per-stratum',type=int,default=5)
    parser.add_argument('--output')
    args=parser.parse_args(argv)
    if args.db:
        uri=Path(args.db).resolve().as_uri()+'?mode=ro'
        with closing(sqlite3.connect(uri,uri=True)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            result=audit(db,args.seed,args.per_stratum)
    else:
        result=sensitivity(json.loads(Path(args.book).read_text()))
    text=json.dumps(result,indent=2)+'\n'
    if args.output:
        path=Path(args.output)
        if path.resolve()==Path(args.db or args.book).resolve():
            parser.error('output must not overwrite the input')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    else:
        print(text,end='')


if __name__=='__main__':
    main()
