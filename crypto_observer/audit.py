"""Evidence snapshot: engineering completion is distinct from research maturity."""
from datetime import datetime,timedelta
from pathlib import Path
import hashlib
import json
from .core import now
from .paper import setup,CONFIG


def review_evidence(store,root):
    """Check that the reviewed cohort and referenced quotes exist in this DB."""
    paths={
        'decisions':root/'research/review_decisions_20261024.json',
        'evidence':root/'research/review_evidence_20261024.json',
        'search':root/'research/exact_address_search_20261024.json',
        'report':root/'research/initial_review_20261024.md',
    }
    result={'complete':False,'cases':0,'product_identity_verified':0,'errors':[]}
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='review_quote_checks'").fetchone():
        result['errors'].append('review_database_evidence_missing')
        return result
    if not all(path.is_file() for path in paths.values()):
        result['errors'].append('review_artifacts_missing')
        return result
    try:
        decisions=json.loads(paths['decisions'].read_text())['cases']
        records=json.loads(paths['evidence'].read_text())['records']
        searches=json.loads(paths['search'].read_text())['results']
        tokens={r['token'] for r in decisions}
        cohort={r[0] for r in store.db.execute('SELECT token_address FROM security_observations GROUP BY token_address ORDER BY MIN(id) LIMIT 20')}
        valid=len(decisions)==20 and len(tokens)==20 and tokens==cohort=={r['token'] for r in records}=={r['token'] for r in searches}
        for r in decisions:
            quote=store.db.execute('SELECT token_address FROM review_quote_checks WHERE id=?',(r['quote_check_id'],)).fetchone()
            valid=valid and bool(quote) and quote[0]==r['token'] and r['review_status']=='initial_review_complete' and not r['paper_cohort_changed']
        for r in records:
            snap=r['pool_snapshot']
            valid=valid and bool(store.db.execute('SELECT 1 FROM snapshots WHERE id=? AND token_address=?',(snap['id'],r['token'])).fetchone())
        result['complete']=bool(valid)
        result['cases']=len(tokens)
        result['product_identity_verified']=sum(r['identity']=='verified' for r in decisions)
        result['artifact_sha256']={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in paths.items()}
        result['scope']='technical_and_product_evidence_initial_review_not_safety_or_issuer_confirmation'
        if not valid:result['errors'].append('cohort_or_database_references_not_verified')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['errors'].append(type(exc).__name__)
    return result


def coverage(store,root):
    path=root/'reports/cycles.jsonl'
    result={'completed_cycles':0,'elapsed_days':0,'one_week_elapsed':False,'max_gap_seconds':None,'failed_cycles':0,'degraded_cycles':0}
    if not path.exists():return result
    runs=[]
    for line in path.read_text().splitlines():
        try:
            r=json.loads(line)
            if not r.get('finished_at'):continue
            # A log line alone is insufficient: link each cycle to its completed collection run.
            rid=r['steps']['collect']['run_id']
            row=store.db.execute('SELECT started_at,finished_at,config FROM runs WHERE id=?',(rid,)).fetchone()
            if row and row['finished_at'] and json.loads(row['config']).get('chain')=='base' and datetime.fromisoformat(r['started_at'])<=datetime.fromisoformat(row['started_at'])<=datetime.fromisoformat(row['finished_at'])<=datetime.fromisoformat(r['finished_at']):
                runs.append(r)
        except (ValueError,KeyError,TypeError):continue
    if not runs:return result
    times=sorted(datetime.fromisoformat(r['started_at']) for r in runs)
    elapsed=(datetime.fromisoformat(now())-times[0]).total_seconds()/86400
    gaps=[(b-a).total_seconds() for a,b in zip(times,times[1:])]
    result.update(completed_cycles=len(runs),elapsed_days=elapsed,one_week_elapsed=elapsed>=7,max_gap_seconds=max(gaps) if gaps else None,failed_cycles=sum(r['status']=='failed' for r in runs),degraded_cycles=sum(r['status']=='degraded' for r in runs),coverage_note='Cycle spacing is observed, not proof of complete chain discovery. Sleep/offline gaps and pagination caps remain material.')
    return result


def audit(store):
    setup(store)
    db=store.db
    counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('runs','requests','request_attempts','pools','snapshots','security_observations','onchain_observations','transfer_events','indexed_holder_snapshots','paper_signals','paper_positions','paper_marks','daily_position_marks','paper_matches')}
    config=db.execute('SELECT created_at FROM experiments WHERE version=?',(CONFIG['version'],)).fetchone()
    start=datetime.fromisoformat(config[0])
    at=datetime.fromisoformat(now())
    orphan_signals=db.execute('SELECT COUNT(*) FROM paper_signals s LEFT JOIN paper_positions p ON p.signal_id=s.id WHERE p.id IS NULL').fetchone()[0]
    holder_repeats=db.execute('SELECT COUNT(*) FROM (SELECT token_address FROM indexed_holder_snapshots GROUP BY token_address HAVING COUNT(*)>=2)').fetchone()[0]
    reconciled=db.execute("SELECT COUNT(*) FROM indexed_holder_snapshots WHERE status='event_supply_reconciled' AND caught_up=1").fetchone()[0]
    horizons=[]
    for horizon in CONFIG['horizons_days']:
        rows=db.execute('''SELECT p.entry_at,m.status FROM paper_positions p JOIN paper_signals s ON s.id=p.signal_id
            LEFT JOIN paper_marks m ON m.position_id=p.id AND m.horizon_days=?
            WHERE s.version=? AND p.entry_status='quoted' ''',(horizon,CONFIG['version'])).fetchall()
        due=sum(at>=datetime.fromisoformat(r['entry_at'])+timedelta(days=horizon) for r in rows)
        earliest=min((datetime.fromisoformat(r['entry_at']) for r in rows),default=None)
        horizons.append({'days':horizon,'positions':len(rows),'due':due,'evaluated':sum(r['status'] is not None for r in rows),'quoted_exits':sum(r['status']=='quoted' for r in rows),'earliest_due':(earliest+timedelta(days=horizon)).isoformat() if earliest else None})
    root=Path(__file__).resolve().parents[1]
    reviewed=review_evidence(store,root)
    runtime=coverage(store,root)
    remaining=[]
    if not runtime['one_week_elapsed']:remaining.append('one_week_runtime_coverage_audit')
    if not reviewed['complete']:remaining.append('20_case_technical_and_product_review')
    if any(not h['positions'] or h['due']<h['positions'] or h['evaluated']<h['positions'] for h in horizons):remaining.append('matured_7_30_90_day_outcomes')
    # This conclusion requires an explicit research assessment, not merely a passing counter.
    remaining.append('held_out_cost_capacity_and_return_conclusions')
    return {'generated_at':now(),'counts':counts,'sqlite_integrity':db.execute('PRAGMA quick_check').fetchone()[0],
            'foreign_key_violations':len(db.execute('PRAGMA foreign_key_check').fetchall()),
            'unpositioned_signals':orphan_signals,'repeated_holder_tokens':holder_repeats,
            'caught_up_reconciled_holder_snapshots':reconciled,'experiment':CONFIG['version'],
            'runtime_coverage':runtime,'case_review':reviewed,
            'horizons':horizons,'completion':'not_complete' if remaining else 'requires_goal_completion_review',
            'remaining_requirements':remaining,
            'interpretation':'Live infrastructure evidence is not investment alpha evidence. Independent quotes do not simulate entry reserve changes. Unknown failures stay in denominators or are reported separately.'}


def report(store):
    return '# Milestone evidence audit\n\n```json\n'+json.dumps(audit(store),indent=2)+'\n```\n'
