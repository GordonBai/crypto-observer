"""Opt-in short-v1 quote servicing; no collection, calibration, or selection.

The CLI supplies the same database lock as short-cycle. This services frozen
position rules without changing their ordering; it is not an EDF scheduler.
"""
from datetime import datetime, timedelta
from pathlib import Path
import json
from .core import now
from .runtime_metrics import BudgetClient, StepTimer, provenance


def service(store, client, output_dir='reports/short_term', network_budget_seconds=None, request_budget=None):
    from .short_experiment import VERSION, positions, universe
    from .short_runtime import atomic_write
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'short_experiments' not in tables:
        raise RuntimeError('quote worker requires an existing frozen experiment')
    study = store.db.execute('SELECT * FROM short_experiments WHERE version=?', (VERSION,)).fetchone()
    if study is None:
        raise RuntimeError('quote worker requires an existing frozen experiment')
    at = now()
    started = datetime.fromisoformat(study['created_at'])
    current = datetime.fromisoformat(at)
    if current < started:
        raise RuntimeError('quote worker cannot run before experiment creation')
    deadline = started + timedelta(days=32)
    expired = current >= deadline
    budget = BudgetClient(client, network_budget_seconds, request_budget)
    class OfflineClient:
        def get(self, *args, **kwargs):
            raise RuntimeError('quote worker observation expired; network disabled')
    active_client = OfflineClient() if expired else budget
    evidence = {'started_at': at, 'observation_until': deadline.isoformat(),
                'provenance': provenance('quote-worker'), 'errors': [],
                'selection_enabled': False, 'expiry_guard_active': expired}
    run = store.start({'module': 'short_quote_worker', 'version': VERSION})
    root = Path(output_dir)/'quote-worker'
    try:
        with StepTimer(evidence, 'positions', now):
            evidence['result'] = positions(store, active_client, run, at, universe(store))
        store.finish(run, [])
        evidence['status'] = 'observation_window_complete' if expired else 'ok'
    except Exception as exc:
        store.finish(run, ['quote_worker_failed'])
        evidence['status'] = 'failed'
        evidence['errors'].append({'type': type(exc).__name__, 'message': str(exc)[:500]})
    evidence['network_budget'] = budget.evidence()
    evidence['finished_at'] = now()
    atomic_write(root/'health.json', json.dumps(evidence, indent=2)+'\n')
    with (root/'cycles.jsonl').open('a') as stream:
        stream.write(json.dumps(evidence)+'\n')
    return evidence
