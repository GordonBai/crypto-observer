import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from crypto_observer.study_plan import capacity, freeze


class StudyPlanTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.spec = dict(version='prospective-3', hypothesis='Test volume signal',
            primary_horizon_hours=24, benchmark='matched', cost_model='displayed book',
            matching_policy='fixed', unknown_policy='retain', analysis_plan='asset/day blocks',
            decision_rules='inconclusive if insufficient', start_at='2026-02-01T00:00:00+00:00',
            end_at='2026-03-01T00:00:00+00:00', instruments=['venue:spot:A'],
            capacity=dict(budget=1, cadence_seconds=300, maximum_age_seconds=600))

    def test_capacity_uses_measured_cadence(self):
        self.assertEqual(capacity(300, 1200, 7200)['maximum_assets'], 1800)
        self.assertEqual(capacity(1, 601, 600)['maximum_assets'], 0)

    def test_freeze_is_exclusive_and_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'plan.json'
            a = freeze(self.spec, p, self.now)
            with self.assertRaises(FileExistsError):
                freeze(self.spec, p, self.now)
            b = freeze(self.spec, Path(d)/'second.json', self.now)
            self.assertEqual(a['sha256'], b['sha256'])

    def test_rejects_old_versions_past_dates_and_overcapacity(self):
        for field, value in [('version', 'short-v1'), ('start_at', '2025-01-01T00:00:00+00:00'),
                             ('instruments', ['a', 'b', 'c']), ('instruments', ['a', 'a'])]:
            spec = copy.deepcopy(self.spec)
            spec[field] = value
            with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
                freeze(spec, Path(d)/'plan.json', self.now)
