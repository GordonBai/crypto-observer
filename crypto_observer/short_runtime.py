"""Independent bounded short-term observer; never changes Base paper v2."""
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from .core import now


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(content)
    temporary.replace(path)


def cycle(store, client, output_dir='reports/short_term', market_limit=36):
    from .market_data import collect_market, report as market_report
    from .short_experiment import cycle as experiment_cycle, report as experiment_report, setup as experiment_setup, VERSION as study_version
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 512*1024*1024:
        raise RuntimeError('less than 512 MiB free; no collection performed')
    store.db.execute('CREATE TABLE IF NOT EXISTS short_runtime_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
    store.db.execute('INSERT OR IGNORE INTO short_runtime_meta VALUES(?,?)', ('created_at',now()))
    store.db.commit()
    experiment_setup(store)
    created = store.db.execute("SELECT created_at FROM short_experiments WHERE version=?", (study_version,)).fetchone()[0]
    # Thirty days of enrollment followed by up to 24h outcomes and tolerance.
    # Any longer research run needs an explicitly versioned new study.
    deadline = datetime.fromisoformat(created)+timedelta(days=32)
    expired = datetime.fromisoformat(now()) >= deadline
    health = {'started_at':now(),'observation_until':deadline.isoformat(),
              'mode':'public_data_and_paper_only','errors':[],'steps':{},'current_step':None,
              'disk_free_bytes':shutil.disk_usage(root).free}
    def step(name, fn):
        health['current_step'] = name
        atomic_write(root/'health.json',json.dumps(health,indent=2)+'\n')
        try:
            result = fn()
            health['steps'][name] = result
            return result
        except Exception as exc:
            health['errors'].append({'step':name,'type':type(exc).__name__,'message':str(exc)[:500]})
            health['steps'][name] = {'status':'failed'}
            return None
    if expired:
        # Deliberately do not fetch quotes after the observation deadline. The
        # experiment cycle must still resolve missed windows without network.
        class OfflineClient:
            def get(self,*args,**kwargs):
                raise RuntimeError('short-term observation deadline passed; network disabled')
        step('experiment_finalize_offline',lambda:experiment_cycle(store,OfflineClient()))
    else:
        step('market_collect',lambda:collect_market(store,client,market_limit))
        step('short_experiment',lambda:experiment_cycle(store,client))
    for filename,fn in [('market.md',market_report),('experiment.md',experiment_report)]:
        def write_report(filename=filename,fn=fn):
            atomic_write(root/filename,fn(store))
            return {'path':str(root/filename)}
        step('report:'+filename,write_report)
    health['database_quick_check'] = store.db.execute('PRAGMA quick_check').fetchone()[0]
    notes = any(isinstance(v,dict) and v.get('notes') for v in health['steps'].values())
    health['status'] = ('failed' if health['errors'] or health['database_quick_check']!='ok'
                        else 'observation_window_complete' if expired else 'degraded' if notes else 'ok')
    health['current_step'] = None
    health['finished_at'] = now()
    atomic_write(root/'health.json',json.dumps(health,indent=2)+'\n')
    with (root/'cycles.jsonl').open('a') as file:
        file.write(json.dumps(health)+'\n')
    atomic_write(root/'audit.md',audit_report(store,root))
    return health


def audit(store, output_dir='reports/short_term'):
    """Read current evidence; no maturity claim from successful code or counters."""
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    names = ['market_instruments','market_bars','market_ingests','market_books','market_derivatives',
             'short_assessments','short_events','short_positions','short_outcomes','short_path_marks']
    counts = {name:store.db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] if name in tables else None for name in names}
    result = {'generated_at':now(),'counts':counts,
              'sqlite_integrity':store.db.execute('PRAGMA quick_check').fetchone()[0],
              'foreign_key_violations':len(store.db.execute('PRAGMA foreign_key_check').fetchall()),
              'mode':'public_data_and_displayed_book_paper_quotes_only',
              'completion':'not_complete','remaining':['continuous_coverage_assessment','real_1_4_24_hour_outcomes','frozen_holdout_assessment']}
    if 'market_instruments' in tables:
        result['enrolled_by_kind']={r[0]:r[1] for r in store.db.execute('SELECT kind,COUNT(*) FROM market_instruments WHERE enrolled=1 GROUP BY kind')}
        result['never_attempted_enrolled']=store.db.execute('SELECT COUNT(*) FROM market_instruments WHERE enrolled=1 AND last_attempt_at IS NULL').fetchone()[0]
    if 'short_experiments' in tables:
        study=store.db.execute('SELECT version,created_at,development_until,calibration_status,calibration_at FROM short_experiments ORDER BY created_at DESC LIMIT 1').fetchone()
        result['study']=dict(study) if study else None
    due=0
    if {'short_positions','short_outcomes'}<=tables:
        at=datetime.fromisoformat(now())
        for row in store.db.execute("SELECT id,entry_at FROM short_positions WHERE status='open' AND entry_at IS NOT NULL"):
            for hours in (1,4,24):
                if at>=datetime.fromisoformat(row['entry_at'])+timedelta(hours=hours):
                    due+=1
        result['due_position_horizons']=due
        result['outcome_status_counts']={r[0]:r[1] for r in store.db.execute('SELECT status,COUNT(*) FROM short_outcomes GROUP BY status')}
    path=Path(output_dir)/'cycles.jsonl'
    cycles=[]
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row=json.loads(line)
                run_id=row['steps']['market_collect']['run_id']
                run=store.db.execute('SELECT started_at,finished_at,config FROM runs WHERE id=?',(run_id,)).fetchone()
                if run and run['finished_at'] and json.loads(run['config']).get('module')=='market_data' and row.get('finished_at'):
                    if datetime.fromisoformat(row['started_at'])<=datetime.fromisoformat(run['started_at'])<=datetime.fromisoformat(run['finished_at'])<=datetime.fromisoformat(row['finished_at']):
                        cycles.append(row)
            except (KeyError,ValueError,TypeError):
                continue
    times=sorted(datetime.fromisoformat(r['started_at']) for r in cycles)
    gaps=[(b-a).total_seconds() for a,b in zip(times,times[1:])]
    result['runtime_coverage']={'verified_cycles':len(cycles),'failed_cycles':sum(r.get('status')=='failed' for r in cycles),
        'max_start_gap_seconds':max(gaps) if gaps else None,
        'elapsed_hours':(datetime.fromisoformat(now())-times[0]).total_seconds()/3600 if times else None,
        'note':'Only completed logged cycles linked to actual market collection runs count. Local sleep/offline gaps remain possible.'}
    result['limitations']=['Closed by venue request clock and hour boundary plus grace; venue supplies no finality flag.',
                          'Displayed depth is not a fill or guarantee of executable prices.',
                          'A deterministic bounded sample does not cover every listed market.',
                          'Derivative observations have separate unverified quantity units and are not traded.']
    return result


def audit_report(store,output_dir='reports/short_term'):
    return '# Independent short-term evidence audit\n\n```json\n'+json.dumps(audit(store,output_dir),indent=2)+'\n```\n'
