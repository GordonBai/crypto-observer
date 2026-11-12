import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from crypto_observer.core import Store
from crypto_observer.short_runtime import cycle


class ShortRuntimeTests(unittest.TestCase):
    def test_failure_is_recorded_and_reports_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(str(Path(folder)/'short.sqlite3'))
            try:
                with patch('crypto_observer.market_data.collect_market',side_effect=RuntimeError('feed failed')), \
                     patch('crypto_observer.short_experiment.cycle',return_value={'assessments':0}), \
                     patch('crypto_observer.market_data.report',return_value='market unavailable'), \
                     patch('crypto_observer.short_experiment.report',return_value='no results'):
                    result=cycle(store,None,folder)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['errors'][0]['step'],'market_collect')
                self.assertEqual(result['database_quick_check'],'ok')
                self.assertEqual(json.loads((Path(folder)/'health.json').read_text())['current_step'],None)
                self.assertTrue((Path(folder)/'experiment.md').is_file())
                self.assertEqual(len((Path(folder)/'cycles.jsonl').read_text().splitlines()),1)
            finally:
                store.close()

    def test_deadline_disables_network_but_finalizes(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(str(Path(folder)/'short.sqlite3'))
            try:
                store.db.execute('CREATE TABLE short_runtime_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
                store.db.execute("INSERT INTO short_runtime_meta VALUES('created_at','2026-10-01T00:00:00+00:00')")
                store.db.commit()
                from crypto_observer.short_experiment import setup
                with patch('crypto_observer.short_experiment.now',return_value='2026-10-01T00:00:00+00:00'):
                    setup(store)
                def finalize(store,client):
                    with self.assertRaisesRegex(RuntimeError,'network disabled'):
                        client.get('https://example.com')
                    return {'missed':1}
                with patch('crypto_observer.short_runtime.now',return_value='2026-11-03T00:00:00+00:00'), \
                     patch('crypto_observer.market_data.collect_market') as collect, \
                     patch('crypto_observer.short_experiment.cycle',side_effect=finalize), \
                     patch('crypto_observer.market_data.report',return_value='market'), \
                     patch('crypto_observer.short_experiment.report',return_value='expired'):
                    result=cycle(store,None,folder)
                collect.assert_not_called()
                self.assertEqual(result['status'],'observation_window_complete')
                self.assertEqual(result['steps']['experiment_finalize_offline']['missed'],1)
            finally:
                store.close()

    def test_older_runtime_metadata_cannot_expire_a_later_study(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(str(Path(folder)/'short.sqlite3'))
            try:
                store.db.execute('CREATE TABLE short_runtime_meta(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
                store.db.execute("INSERT INTO short_runtime_meta VALUES('created_at','2026-10-01T00:00:00+00:00')")
                store.db.commit()
                from crypto_observer.short_experiment import setup
                with patch('crypto_observer.short_experiment.now',return_value='2026-11-02T00:00:00+00:00'):
                    setup(store)
                with patch('crypto_observer.short_runtime.now',return_value='2026-11-03T00:00:00+00:00'), \
                     patch('crypto_observer.market_data.collect_market',return_value={}) as collect, \
                     patch('crypto_observer.short_experiment.cycle',return_value={}), \
                     patch('crypto_observer.market_data.report',return_value='market'), \
                     patch('crypto_observer.short_experiment.report',return_value='study'):
                    result=cycle(store,None,folder)
                collect.assert_called_once()
                self.assertEqual(result['status'],'ok')
                self.assertEqual(result['observation_until'],'2026-12-04T00:00:00+00:00')
            finally:
                store.close()

    def test_cli_default_db_keeps_base_database_untouched(self):
        from crypto_observer.cli import main
        with tempfile.TemporaryDirectory() as folder:
            previous=os.getcwd()
            try:
                os.chdir(folder)
                Path('data').mkdir()
                Path('data/observer.sqlite3').write_bytes(b'base sentinel')
                with patch('crypto_observer.market_data.report',return_value='independent market report'),patch('builtins.print'):
                    self.assertEqual(main(['market-report']),0)
                self.assertEqual(Path('data/observer.sqlite3').read_bytes(),b'base sentinel')
                self.assertTrue(Path('data/short_term_v2.sqlite3').is_file())
            finally:
                os.chdir(previous)
