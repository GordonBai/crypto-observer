import unittest
from crypto_observer.security import assess, holders, scan, cases
from crypto_observer.core import Store
from test_observer import Fake, pool, TOKEN
from crypto_observer.core import collect

class SecurityTests(unittest.TestCase):
    def test_unknown_not_safe(self):
        a=assess({})
        self.assertEqual(a['conclusion'],'unverified')
        self.assertTrue(all(v=='unknown' for v in a['risk_fields'].values()))
    def test_present_and_absent(self):
        a=assess({'is_mintable':'1','is_proxy':'0','sell_tax':'2'})
        self.assertIn('is_mintable',a['flags'])
        self.assertEqual(a['risk_fields']['is_proxy'],'reported_absent')
        self.assertIsNone(a['taxes']['sell_tax'])
    def test_holder_fraction(self):
        c,t,e=holders({'holder_count':'12','holders':[{'address':TOKEN,'percent':'0.25'}]})
        self.assertEqual((c,t),(12,'0.25'))
    def test_invalid_holder_data(self):
        for d in ({},{'holder_count':'1.2'},{'holder_count':'NaN'},{'holders':[{'address':TOKEN,'percent':'2'}]}):
            c,t,e=holders(d); self.assertIsNone(t)
    def test_history_and_missing(self):
        s=Store(':memory:')
        try:
            collect(s,Fake([{'data':[pool()]},{'pairs':[]}]),1,20)
            scan(s,Fake([{'code':1,'result':{TOKEN:{'holder_count':'10','is_proxy':'0'}}}]),20)
            scan(s,Fake([{'code':1,'result':{}}]),20)
            self.assertEqual(s.db.execute('select count(*) from security_observations').fetchone()[0],2)
            self.assertIn('provider status: unknown',cases(s))
        finally: s.close()
