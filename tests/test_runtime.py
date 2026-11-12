import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from crypto_observer.core import Store
from crypto_observer.runtime import cycle

class RuntimeTests(unittest.TestCase):
 def test_module_failure_preserves_health_and_other_reports(self):
  with tempfile.TemporaryDirectory() as folder:
   store=Store(str(Path(folder)/'test.sqlite3'))
   try:
    with patch('crypto_observer.runtime.collect',side_effect=RuntimeError('feed unavailable')), patch('crypto_observer.runtime.evaluate',return_value={}), patch('crypto_observer.runtime.open_positions',return_value={}), patch('crypto_observer.runtime.scan',return_value={}),patch('crypto_observer.runtime.index_holders',return_value={}),patch('crypto_observer.runtime.probe',return_value={}):
     result=cycle(store,None,str(Path(folder)/'reports'))
    self.assertEqual(result['status'],'failed')
    self.assertEqual(result['errors'][0]['step'],'collect')
    self.assertEqual(result['database_quick_check'],'ok')
    self.assertTrue((Path(folder)/'reports'/'evaluation.md').exists())
    self.assertEqual(json.loads((Path(folder)/'reports'/'health.json').read_text())['status'],'failed')
   finally:store.close()

from datetime import datetime
from test_observer import Fake,pool,TOKEN
from test_paper_lifecycle import Quotes
from crypto_observer.core import collect
from crypto_observer.paper import open_positions

class ExpiredRuntimeTests(unittest.TestCase):
 def test_expiry_records_missed_windows_without_network(self):
  with tempfile.TemporaryDirectory() as folder:
   store=Store(str(Path(folder)/'test.sqlite3'))
   try:
    item=pool();item['attributes']['base_token_price_usd']='0.01'
    collect(store,Fake([{'data':[item]},{'pairs':[]}]),1,20)
    with patch('crypto_observer.paper.now',return_value='2026-10-24T12:01:00+00:00'):
     opened=open_positions(store,Quotes(['2026-10-24T12:01:00+00:00']),1)
    self.assertEqual(opened['opened'],1)
    class NoNetwork:
     def get(self,*args,**kwargs):raise AssertionError('no real network allowed')
    future='2027-03-01T12:01:00+00:00'
    with patch('crypto_observer.runtime.now',return_value=future),patch('crypto_observer.paper.now',return_value=future):
     result=cycle(store,NoNetwork(),str(Path(folder)/'reports'))
    self.assertEqual(result['status'],'observation_window_complete')
    self.assertEqual(result['steps']['paper_finalize_offline']['due_evaluations'],3)
    self.assertNotIn('collect',result['steps'])
    statuses=store.db.execute('SELECT status FROM paper_marks WHERE horizon_days IN (7,30,90)').fetchall()
    self.assertEqual(len(statuses),3)
    self.assertTrue(all(r[0]=='missed_window' for r in statuses))
    self.assertTrue((Path(folder)/'reports'/'audit.md').exists())
   finally:store.close()
