import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from crypto_observer.core import Store
from crypto_observer.quote_worker import service
from crypto_observer.short_experiment import setup


class QuoteWorkerTests(unittest.TestCase):
    def test_missing_study_fails_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(str(Path(folder)/'db'))
            try:
                client = Mock()
                with self.assertRaisesRegex(RuntimeError, 'existing frozen'):
                    service(store, client, folder)
                client.get.assert_not_called()
            finally:
                store.close()

    def test_expired_study_disables_network_and_does_not_select(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(str(Path(folder)/'db'))
            try:
                with patch('crypto_observer.short_experiment.now', return_value='2026-10-01T00:00:00+00:00'):
                    setup(store)
                def check(store, client, *args):
                    with self.assertRaisesRegex(RuntimeError, 'network disabled'):
                        client.get('url')
                    return {'exits': 0}
                with patch('crypto_observer.quote_worker.now', return_value='2026-12-01T00:00:00+00:00'), patch('crypto_observer.short_experiment.positions', side_effect=check), patch('crypto_observer.short_experiment.universe', return_value=[]), patch('crypto_observer.short_experiment.create_events') as select:
                    result = service(store, Mock(), folder)
                select.assert_not_called()
                self.assertEqual(result['status'], 'observation_window_complete')
                self.assertFalse((Path(folder)/'health.json').exists())
                self.assertTrue((Path(folder)/'quote-worker/health.json').exists())
            finally:
                store.close()
