import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from crypto_observer.core import Store
from crypto_observer.runtime import cycle
from crypto_observer.runtime_metrics import StepTimer, provenance


class RuntimeMetricsTests(unittest.TestCase):
    def test_failure_has_timing(self):
        health = {}
        with self.assertRaises(RuntimeError):
            with StepTimer(health, 'request', lambda: 'timestamp'):
                raise RuntimeError('failure')
        self.assertGreaterEqual(health['step_timings']['request']['elapsed_seconds'], 0)
        self.assertEqual(health['step_timings']['request']['finished_at'], 'timestamp')

    def test_provenance_is_stable_and_mode_explicit(self):
        self.assertEqual(provenance('full'), provenance('full'))
        self.assertEqual(len(provenance('full')['code_sha256']), 64)
        self.assertEqual(provenance('critical')['mode'], 'critical')

    def test_critical_skips_maintenance_and_isolates_artifacts(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            store = Store(str(Path(folder)/'test.sqlite3'))
            stack.callback(store.close)
            calls = {name: stack.enter_context(patch('crypto_observer.runtime.'+name, return_value={}))
                     for name in ['collect','evaluate','open_positions','scan','index_holders','probe','window_report']}
            result = cycle(store, None, folder, mode='critical')
            calls['collect'].assert_called_once()
            calls['evaluate'].assert_called_once()
            calls['open_positions'].assert_called_once()
            for name in ['scan','index_holders','probe','window_report']:
                calls[name].assert_not_called()
            self.assertEqual(result['database_quick_check'], 'not_run_critical')
            self.assertEqual(result['status'], 'ok')
            self.assertFalse((Path(folder)/'cycles.jsonl').exists())
            self.assertTrue((Path(folder)/'critical/cycles.jsonl').exists())
            self.assertIn('collect', result['step_timings'])

    def test_short_maintenance_does_not_collect_or_select(self):
        from crypto_observer.short_runtime import cycle as short_cycle
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            store = Store(str(Path(folder)/'short.sqlite3'))
            stack.callback(store.close)
            collect = stack.enter_context(patch('crypto_observer.market_data.collect_market'))
            experiment = stack.enter_context(patch('crypto_observer.short_experiment.cycle'))
            stack.enter_context(patch('crypto_observer.market_data.report', return_value='report'))
            stack.enter_context(patch('crypto_observer.short_experiment.report', return_value='report'))
            result = short_cycle(store, None, folder, mode='maintenance')
            collect.assert_not_called()
            experiment.assert_not_called()
            self.assertTrue((Path(folder)/'maintenance/health.json').exists())
            self.assertFalse((Path(folder)/'cycles.jsonl').exists())
            self.assertEqual(result['database_quick_check'], 'ok')

    def test_request_budget_preserves_completed_response_and_denies_next(self):
        from crypto_observer.runtime_metrics import BudgetClient
        from unittest.mock import Mock
        inner = Mock()
        inner.get.return_value = 'raw-response'
        wrapper = BudgetClient(inner, requests=1)
        self.assertEqual(wrapper.get('url'), 'raw-response')
        with self.assertRaisesRegex(RuntimeError, 'request_budget'):
            wrapper.get('url')
        inner.get.assert_called_once_with('url')

    def test_time_budget_denies_new_request_without_hard_cancellation(self):
        from crypto_observer.runtime_metrics import BudgetClient
        from unittest.mock import Mock
        inner = Mock()
        clock = Mock(side_effect=[0, 11])
        wrapper = BudgetClient(inner, seconds=10, clock=clock)
        with self.assertRaisesRegex(RuntimeError, 'time_budget'):
            wrapper.get('url')
        inner.get.assert_not_called()

    def test_expired_base_maintenance_does_not_enrich(self):
        from crypto_observer.paper import setup
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            store = Store(str(Path(folder)/'db'))
            stack.callback(store.close)
            with patch('crypto_observer.paper.now', return_value='2026-01-01T00:00:00+00:00'):
                setup(store)
            stack.enter_context(patch('crypto_observer.runtime.now', return_value='2027-01-01T00:00:00+00:00'))
            scans = [stack.enter_context(patch('crypto_observer.runtime.'+name))
                     for name in ['collect','scan','index_holders','probe','evaluate','open_positions']]
            result = cycle(store, None, folder, mode='maintenance')
            for scan in scans:
                scan.assert_not_called()
            self.assertEqual(result['status'], 'observation_window_complete')
