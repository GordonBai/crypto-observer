"""Offline preregistration and bounded-universe planning for future studies.

This module never enrolls assets or changes the active frozen experiments.
"""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def capacity(budget, cadence_seconds, maximum_age_seconds):
    if any(not math.isfinite(x) or x <= 0 for x in
           (budget, cadence_seconds, maximum_age_seconds)):
        raise ValueError('capacity inputs must be finite and positive')
    if int(budget) != budget:
        raise ValueError('budget must be an integer')
    waves = math.floor(maximum_age_seconds / cadence_seconds)
    return {'maximum_assets': int(budget * waves), 'waves': waves,
            'assumption': 'Measured cadence, fixed budget, no outages or new arrivals; not an SLA.'}


def freeze(spec, output, now=None):
    now = now or datetime.now(timezone.utc)
    required = ('version', 'hypothesis', 'primary_horizon_hours', 'benchmark',
                'cost_model', 'matching_policy', 'unknown_policy', 'analysis_plan',
                'decision_rules', 'start_at', 'end_at', 'instruments', 'capacity')
    if any(not spec.get(k) for k in required):
        raise ValueError('nonempty preregistration fields required: ' + ', '.join(required))
    if spec['version'] in ('v1', 'v2', 'short-v1', 'market-v1', 'market-v2'):
        raise ValueError('existing frozen version cannot be replaced')
    start, end = (datetime.fromisoformat(spec[k]) for k in ('start_at', 'end_at'))
    if start.tzinfo is None or end.tzinfo is None or not now < start < end:
        raise ValueError('timezone-aware future start and later end required')
    instruments = spec['instruments']
    if (not isinstance(instruments, list) or not instruments or
            any(not isinstance(x, str) or not x.strip() for x in instruments) or
            len(set(instruments)) != len(instruments)):
        raise ValueError('unique nonempty instrument identities required')
    limit = capacity(**spec['capacity'])
    if len(instruments) > limit['maximum_assets']:
        raise ValueError('universe exceeds declared observation capacity')
    if not isinstance(spec['primary_horizon_hours'], (int, float)) or spec['primary_horizon_hours'] <= 0:
        raise ValueError('positive primary horizon required')
    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False, allow_nan=False)
    record = {'schema': 1, 'frozen_at': now.isoformat(),
              'sha256': hashlib.sha256(payload.encode()).hexdigest(),
              'spec': spec, 'capacity_estimate': limit,
              'status': 'preregistered_only_not_started'}
    # Exclusive creation prevents accidental replacement of a frozen plan.
    with Path(output).open('x') as file:
        file.write(json.dumps(record, indent=2, ensure_ascii=False) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    freeze(json.loads(args.spec.read_text()), args.output)


if __name__ == '__main__':
    main()
