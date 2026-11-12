import unittest
from crypto_observer.core import Store, collect
from crypto_observer.paper import setup, candidates, quote, selection, CONFIG
from test_observer import Fake, pool, TOKEN
class PaperTests(unittest.TestCase):
 def test_no_future_or_stale_snapshot(self):
  s=Store(':memory:')
  try:
   collect(s,Fake([{'data':[pool()]},{'pairs':[]}]),1,20)
   setup(s)
   self.assertEqual(candidates(s,'2026-10-24T11:59:00+00:00'),[])
   self.assertEqual(candidates(s,'2026-10-25T12:00:00+00:00'),[])
  finally:s.close()
 def test_quote_identity_validation(self):
  s=Store(':memory:')
  try:
   setup(s);run=s.start({})
   q=quote(s,Fake([{'code':0,'data':{'routeSummary':{'amountIn':'100','amountOut':'200','gasUsd':'0.1','tokenIn':TOKEN,'tokenOut':TOKEN}}}]),run,TOKEN,'0x'+'c'*40,100)
   self.assertEqual(q['status'],'quote_unknown')
  finally:s.close()
 def test_config_frozen(self):
  s=Store(':memory:')
  try:
   setup(s);s.db.execute("UPDATE experiments SET config='{}'")
   with self.assertRaises(ValueError):setup(s)
  finally:s.close()
 def test_stratified_controls_and_determinism(self):
  rows=[{'token_address':'a','features':{'stratum':'4:new','buy_fraction':0.8,'holder_growth_24h':None}}, {'token_address':'b','features':{'stratum':'4:new','buy_fraction':None,'holder_growth_24h':None}}, {'token_address':'c','features':{'stratum':'5:new','buy_fraction':None,'holder_growth_24h':None}}]
  a=selection(rows,'date',1);self.assertEqual(a,selection(rows,'date',1))
  selected=[r for strategy,r in a if strategy=='buy_pressure'][0]
  control=[r for strategy,r in a if strategy=='matched_random:buy_pressure'][0]
  self.assertEqual(selected['features']['stratum'],control['features']['stratum'])
  self.assertNotEqual(selected['token_address'],control['token_address'])
