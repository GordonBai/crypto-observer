"""Runtime evidence without changing frozen research configuration."""
import hashlib
import time
from pathlib import Path


def provenance(mode):
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {'runtime_schema': 1, 'mode': mode, 'code_sha256': digest.hexdigest()}


class StepTimer:
    def __init__(self, health, name, clock):
        self.health, self.name, self.clock = health, name, clock

    def __enter__(self):
        self.started = time.monotonic()
        self.record = {'started_at': self.clock()}
        self.health.setdefault('step_timings', {})[self.name] = self.record
        return self

    def __exit__(self, *error):
        self.record.update(finished_at=self.clock(), elapsed_seconds=time.monotonic()-self.started)


class BudgetClient:
    """Admission budget for logical requests, not cancellation of an in-flight request.

    Underlying client retains timeout/retry behavior and raw attempt evidence.
    """
    def __init__(self, client, seconds=None, requests=None, clock=time.monotonic):
        if seconds is not None and seconds <= 0 or requests is not None and requests <= 0:
            raise ValueError('budgets must be positive')
        self.client, self.seconds, self.requests, self.clock = client, seconds, requests, clock
        self.started, self.calls = clock(), 0

    def get(self, *args, **kwargs):
        if self.requests is not None and self.calls >= self.requests:
            raise RuntimeError('runtime_request_budget_exhausted')
        if self.seconds is not None and self.clock()-self.started >= self.seconds:
            raise RuntimeError('runtime_network_time_budget_exhausted')
        self.calls += 1
        return self.client.get(*args, **kwargs)

    def evidence(self):
        return {'seconds_limit': self.seconds, 'logical_request_limit': self.requests,
                'logical_requests_started': self.calls, 'elapsed_seconds': self.clock()-self.started,
                'hard_cancellation': False}
