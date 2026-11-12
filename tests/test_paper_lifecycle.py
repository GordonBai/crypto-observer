import json
import unittest
from datetime import datetime
from urllib.parse import urlparse,parse_qs
from unittest.mock import patch
from crypto_observer.core import Store,Fetch,collect
from crypto_observer.paper import CONFIG,setup,open_positions,evaluate,USDC
from test_observer import Fake,pool,TOKEN

class Quotes:
 def __init__(self,clock):self.clock=clock;self.calls=0
 def get(self,url,spacing):
  self.calls+=1
  p=parse_qs(urlparse(url).query)
  summary={'tokenIn':p['tokenIn'][0],'tokenOut':p['tokenOut'][0],'amountIn':p['amountIn'][0],'amountOut':'1000000000000000000' if p['tokenIn'][0]==USDC else '99000000','gasUsd':'0.1'}
  payload={'code':0,'data':{'routeSummary':summary}}
  return Fetch(self.clock[0],self.clock[0],200,json.dumps(payload).encode(),payload,None,1)

class LifecycleTests(unittest.TestCase):
 def setUp(self):
  self.store=Store(':memory:')
  a=pool();a['attributes']['base_token_price_usd']='0.01'
  b=pool();b['attributes']['address']='0x'+'c'*40;b['relationships']['base_token']['data']['id']='base_0x'+'d'*40;b['attributes']['base_token_price_usd']='0.02'
  collect(self.store,Fake([{'data':[a,b]},{'pairs':[]}]),1,20)
  self.clock=['2026-10-24T12:01:00+00:00']
  self.quotes=Quotes(self.clock)
 def tearDown(self):self.store.close()
 def test_cohort_frozen_repeat_does_not_add_positions(self):
  with patch('crypto_observer.paper.now',side_effect=lambda:self.clock[0]):
   first=open_positions(self.store,self.quotes,1)
   n=self.store.db.execute('SELECT COUNT(*) FROM paper_positions').fetchone()[0]
   second=open_positions(self.store,self.quotes,2)
   self.assertGreater(first['opened'],0)
   self.assertEqual(second['opened'],0)
   self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_positions').fetchone()[0],n)
   self.assertEqual(self.store.db.execute('SELECT selection_limit FROM cohort_decisions').fetchone()[0],1)
 def test_missed_horizon_not_backfilled_and_daily_recorded(self):
  with patch('crypto_observer.paper.now',side_effect=lambda:self.clock[0]):
   open_positions(self.store,self.quotes,1)
   self.clock[0]='2026-11-23T13:01:00+00:00'
   result=evaluate(self.store,self.quotes)
   seven=self.store.db.execute('SELECT status,net_value_usdc FROM paper_marks WHERE horizon_days=7').fetchall()
   thirty=self.store.db.execute('SELECT status FROM paper_marks WHERE horizon_days=30').fetchall()
   self.assertTrue(seven)
   self.assertTrue(all(r[0]=='missed_window' and r[1] is None for r in seven))
   self.assertTrue(all(r[0]=='quoted' for r in thirty))
   self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM paper_marks WHERE horizon_days=90').fetchone()[0],0)
   self.assertGreater(result['daily_marks'],0)
 def test_entry_restart_after_window_records_unknown(self):
  with patch('crypto_observer.paper.now',side_effect=lambda:self.clock[0]):
   # Simulate interruption immediately after the immutable cohort decision is persisted.
   def interrupted(*args,**kwargs):raise RuntimeError('interrupted')
   with patch('crypto_observer.paper.quote',side_effect=interrupted):
    with self.assertRaises(RuntimeError):open_positions(self.store,self.quotes,1)
   self.clock[0]='2026-10-24T13:00:00+00:00'
   result=open_positions(self.store,self.quotes,1)
   self.assertEqual(result['opened'],0)
   self.assertGreater(result['entry_unknown'],0)
   self.assertEqual(self.quotes.calls,0)
   self.assertEqual(self.store.db.execute('SELECT entry_status FROM paper_positions LIMIT 1').fetchone()[0],'entry_window_missed')

 def test_cross_day_restart_preserves_unentered_signals(self):
  with patch('crypto_observer.paper.now',side_effect=lambda:self.clock[0]):
   with patch('crypto_observer.paper.quote',side_effect=RuntimeError('interrupted')):
    with self.assertRaises(RuntimeError):open_positions(self.store,self.quotes,1)
   self.clock[0]='2026-10-25T13:00:00+00:00'
   result=evaluate(self.store,self.quotes)
   self.assertGreater(result['abandoned_signals'],0)
   self.assertEqual(self.quotes.calls,0)
   remaining=self.store.db.execute('SELECT COUNT(*) FROM paper_signals s LEFT JOIN paper_positions p ON p.signal_id=s.id WHERE p.id IS NULL').fetchone()[0]
   self.assertEqual(remaining,0)
