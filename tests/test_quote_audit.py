import hashlib
import sqlite3
import unittest
from crypto_observer.quote_audit import select_cases, request_evidence, sensitivity, audit

class AuditTests(unittest.TestCase):
    def test_selection_strata_order_independent(self):
        cases=[dict(id=str(i),version='v',return_value=None) for i in range(9)]
        for i,r in enumerate(cases):r['return']=[None,'-.8','.1'][i%3]
        a=select_cases(cases,per_stratum=1)
        self.assertEqual(a,select_cases(list(reversed(cases)),per_stratum=1))
        self.assertEqual({r['stratum'] for r in a},{'unknown','extreme','normal'})
    def test_sha_and_missing(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.row_factory=sqlite3.Row
        db.execute('create table requests(id,status,error,sha256,body,started_at,observed_at)')
        db.execute('insert into requests values(1,200,NULL,?,?,?,?)',(hashlib.sha256(b'{}').hexdigest(),b'{}','a','b'))
        self.assertEqual(request_evidence(db,1)['integrity'],'verified')
        db.execute("update requests set body=x'00'")
        self.assertEqual(request_evidence(db,1)['integrity'],'sha256_mismatch')
        self.assertEqual(request_evidence(db,2)['integrity'],'missing')
    def test_capacity_and_staleness_stress(self):
        raw=dict(kind='spot',execution_supported=True,quote_currency='USDT',instrument_key='x',size_unit='X',status='quoted',
            observed_at='2026-01-01T00:00:00+00:00',source_at='2026-01-01T00:00:00+00:00',bids=[['9','20']],asks=[['10','20']])
        result=sensitivity(raw,('100',),('1','.25'),(0,31))['scenarios']
        self.assertEqual(result[0]['result']['status'],'quoted')
        self.assertEqual(result[1]['result']['evidence']['reason'],'stale_or_future_book')
        self.assertEqual(result[2]['result']['evidence']['reason'],'insufficient_displayed_capacity')
        self.assertEqual(raw['asks'],[['10','20']])
        with self.assertRaises(ValueError):sensitivity(raw,retained_depth=('2',))

    def test_readonly_audit_preserves_missed_outcome(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.row_factory=sqlite3.Row
        db.executescript("""
        CREATE TABLE short_events(id,version,phase,instrument_key,strategy);
        CREATE TABLE short_positions(id,event_id,entry_quote_id,status);
        CREATE TABLE short_outcomes(position_id,horizon_hours,status,net_return,quote_id);
        CREATE TABLE short_execution_quotes(id,request_id,evidence);
        INSERT INTO short_events VALUES(1,'short-v1','holdout','coin','volume');
        INSERT INTO short_positions VALUES(1,1,NULL,'open');
        INSERT INTO short_outcomes VALUES(1,24,'missed_window',NULL,NULL);
        """)
        db.execute('PRAGMA query_only=ON')
        before=db.total_changes
        r=audit(db)
        self.assertEqual(r['population'],1)
        self.assertEqual(r['cases'][0]['original_status'],'missed_window')
        self.assertEqual(r['cases'][0]['stratum'],'unknown')
        self.assertEqual(db.total_changes,before)
        self.assertEqual(r['population_manifest_sha256'],audit(db)['population_manifest_sha256'])
