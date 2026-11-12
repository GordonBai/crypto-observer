import unittest
from decimal import Decimal
from crypto_observer.evaluation import drawdown,summarize_returns
class EvaluationTests(unittest.TestCase):
 def test_drawdown(self):self.assertEqual(drawdown([100,120,60,90]),Decimal('0.5'))
 def test_unknown_path(self):self.assertIsNone(drawdown([100,None,50]))
 def test_missing_not_dropped(self):
  r=summarize_returns([Decimal(1),None])
  self.assertEqual((r['total'],r['known'],r['unknown']),(2,1,1))
  self.assertEqual(r['mean_unknown_zero_proceeds_stress'],'0')
 def test_no_fabricated_mean(self):self.assertIsNone(summarize_returns([])['mean_known'])

from datetime import date
from crypto_observer.core import Store
from crypto_observer.paper import setup
from crypto_observer.evaluation import portfolio_path
class PortfolioTests(unittest.TestCase):
 def test_missing_daily_mark_not_carried(self):
  s=Store(':memory:');setup(s)
  try:
   p={'id':999,'entry_at':'2026-10-24T12:00:00+00:00','notional_usdc':'100','entry_gas_usd':'1'}
   path,capacity=portfolio_path(s,[p],7,date(2026,10,25))
   self.assertFalse(capacity)
   self.assertIsNone(path[0]['equity'])
   self.assertIsNone(path[1]['equity'])
   self.assertEqual(path[1]['unknown_zero_equity'],Decimal(9899))
  finally:s.close()
 def test_capacity_exceeded_is_explicit(self):
  s=Store(':memory:');setup(s)
  try:
   p={'id':999,'entry_at':'2026-10-24T12:00:00+00:00','notional_usdc':'10001','entry_gas_usd':'1'}
   path,capacity=portfolio_path(s,[p],7,date(2026,10,24))
   self.assertTrue(capacity)
  finally:s.close()


from crypto_observer.evaluation import exit_observation_rates
class ObservationRateTests(unittest.TestCase):
 def test_no_due_is_undefined_not_zero_failure(self):
  rates=exit_observation_rates([])
  self.assertIsNone(rates['unobserved_fraction'])
  self.assertIsNone(rates['quote_unavailable_fraction'])
 def test_operational_miss_separate_from_quote_failure(self):
  rates=exit_observation_rates(['quoted','quote_unknown','missed_window','not_evaluated'])
  self.assertEqual(rates['due'],4)
  self.assertEqual(rates['quote_unavailable'],1)
  self.assertEqual(rates['missed_window'],1)
  self.assertEqual(rates['unobserved_fraction'],'0.75')
  self.assertEqual(rates['quote_unavailable_fraction'],'0.25')
