import unittest
from unittest.mock import patch
from crypto_observer.core import Store,collect
from crypto_observer.security import scan
from crypto_observer.review import check_quotes,evidence
from test_observer import Fake,pool,TOKEN
from test_paper_lifecycle import Quotes

class ReviewTests(unittest.TestCase):
 def test_quote_checks_do_not_open_strategy_positions(self):
  s=Store(':memory:')
  try:
   collect(s,Fake([{'data':[pool()]},{'pairs':[]}]),1,20)
   scan(s,Fake([{'code':1,'result':{TOKEN:{'is_proxy':'0'}}}]),1)
   result=check_quotes(s,Quotes(['2026-10-24T12:01:00+00:00']),1)
   self.assertEqual(result['results'][0]['status'],'roundtrip_quoted')
   self.assertEqual(s.db.execute('SELECT COUNT(*) FROM paper_positions').fetchone()[0],0)
   self.assertEqual(len(evidence(s,1)['records']),1)
  finally:s.close()
 def test_entry_failure_keeps_review_case(self):
  s=Store(':memory:')
  try:
   collect(s,Fake([{'data':[pool()]},{'pairs':[]}]),1,20)
   scan(s,Fake([{'code':1,'result':{TOKEN:{'is_proxy':'0'}}}]),1)
   result=check_quotes(s,Fake([{'code':400,'message':'no route'}]),1)
   self.assertEqual(result['results'][0]['status'],'entry_unknown')
   self.assertEqual(s.db.execute('SELECT COUNT(*) FROM review_quote_checks').fetchone()[0],1)
  finally:s.close()
