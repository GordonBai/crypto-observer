"""Bounded scheduled collection cycle and local health artifacts."""
import json
import os
import shutil
import traceback
from datetime import datetime,timedelta
from pathlib import Path
from .core import now,collect,report
from .security import scan,cases
from .onchain import probe
from .holder_index import index as index_holders,report as holder_report
from .paper import setup as paper_setup,open_positions,evaluate,summary
from .evaluation import report as evaluation_report
from .audit import report as audit_report
from .short_windows import report as window_report


def atomic_write(path,content):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(content)
    temporary.replace(path)


def cycle(store,client,output_dir='reports',pages=3,refresh_limit=300,holder_limit=1):
    root=Path(output_dir)
    root.mkdir(parents=True,exist_ok=True)
    paper_setup(store)
    experiment=store.db.execute("SELECT created_at FROM experiments WHERE version='v2'").fetchone()
    at=datetime.fromisoformat(now())
    deadline=datetime.fromisoformat(experiment[0])+timedelta(days=121)
    health={'started_at':now(),'observation_until':deadline.isoformat(),'steps':{},'errors':[],'disk_free_bytes':shutil.disk_usage(root).free}
    expired=at>=deadline
    if health['disk_free_bytes']<512*1024*1024:
        raise RuntimeError('less than 512 MiB free; no collection performed')
    def step(name,fn):
        # The evolving health artifact shows the current module before network IO.
        health['current_step']=name
        atomic_write(root/'health.json',json.dumps(health,indent=2)+'\n')
        try:
            value=fn()
            health['steps'][name]=value
            return value
        except Exception as exc:
            health['errors'].append({'step':name,'type':type(exc).__name__,'message':str(exc)[:500]})
            health['steps'][name]={'status':'failed'}
            return None
    if expired:
        class OfflineClient:
            def get(self,*args,**kwargs):
                raise RuntimeError('network disabled after observation deadline')
        # All v2 entries were bounded to the first 30 days. After day 121 all
        # 90-day horizons plus the 24h tolerance have elapsed. evaluate records
        # missed windows locally; the guard prevents any accidental live fetch.
        step('paper_finalize_offline',lambda:evaluate(store,OfflineClient()))
    else:
        step('collect',lambda:collect(store,client,pages,refresh_limit))
        step('paper_evaluate',lambda:evaluate(store,client))
        step('paper_open',lambda:open_positions(store,client,2))
        # Rotate enrichment coverage. Risk scans need only a small budget per cycle.
        step('security',lambda:scan(store,client,3))
        step('holder_index_new',lambda:index_holders(store,client,holder_limit,4))
        step('holder_index_refresh',lambda:index_holders(store,client,holder_limit,4,refresh_existing=True))
        # Independent permission check is budgeted to one token per invocation.
        step('onchain',lambda:probe(store,client,1))
    for filename,fn in [('windows.md',window_report),('latest.md',report),('cases.md',cases),('holders.md',holder_report),('paper.md',summary),('evaluation.md',evaluation_report),('audit.md',audit_report)]:
        def write_report(filename=filename,fn=fn):
            atomic_write(root/filename,fn(store));return {'path':str(root/filename)}
        step('report:'+filename,write_report)
    integrity=store.db.execute('PRAGMA quick_check').fetchone()[0]
    health['database_quick_check']=integrity
    health['disk_database_bytes']=Path(store.db.execute('PRAGMA database_list').fetchone()[2]).stat().st_size
    partial=any(isinstance(value,dict) and value.get('notes') for value in health['steps'].values())
    health['status']='failed' if health['errors'] or integrity!='ok' else 'observation_window_complete' if expired else 'degraded' if partial else 'ok'
    health['current_step']=None
    health['finished_at']=now()
    atomic_write(root/'health.json',json.dumps(health,indent=2)+'\n')
    # Append a compact evidence record, no full payloads or credentials.
    with (root/'cycles.jsonl').open('a') as file:
        file.write(json.dumps(health)+'\n')
    return health
