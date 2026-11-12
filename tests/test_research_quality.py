import sqlite3
import tempfile
import unittest
from pathlib import Path
from crypto_observer.research_quality import capacity_plan, holder_quality, readonly, timestamp, short_quality, pair_quality
import json


class QualityTests(unittest.TestCase):
    def test_capacity_includes_partial_wave(self):
        self.assertEqual(capacity_plan(300, 900, 7200, 301)['estimated_rotation_seconds'], 1800)
        self.assertEqual(capacity_plan(300, 900, 7200, 301)['maximum_bounded_universe'], 2400)
        with self.assertRaises(ValueError):
            capacity_plan(0, 900, 7200, 1)

    def test_readonly_rejects_write_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'db.sqlite'
            sqlite3.connect(path).close()
            db = readonly(path)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE forbidden(x)')
            db.close()
            with self.assertRaises(sqlite3.OperationalError):
                readonly(Path(directory)/'missing')

    def test_holder_pair_requires_caught_up_and_time(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.execute('CREATE TABLE indexed_holder_snapshots(token_address,observed_at,status,caught_up)')
        for asset, hour, caught in [('a',0,1),('a',24,1),('b',0,0),('b',24,1),('c',0,1),('c',48,1)]:
            from datetime import timedelta
            at = timestamp('2026-01-01T00:00:00+00:00') + timedelta(hours=hour)
            db.execute('INSERT INTO indexed_holder_snapshots VALUES(?,?,?,?)',(asset,at.isoformat(),'event_supply_reconciled',caught))
        result = holder_quality(db)
        self.assertEqual(result['assets_with_20_28h_pairs'], 1)
        self.assertEqual(result['qualified_pair_endpoints'], {'a':1})

    def test_expected_hours_include_never_observed(self):
        db=sqlite3.connect(':memory:'); db.row_factory=sqlite3.Row
        self.addCleanup(db.close)
        db.executescript('''CREATE TABLE short_experiments(version,created_at,development_until,config);
        CREATE TABLE market_instruments(key,enrolled,kind);
        CREATE TABLE short_assessments(version,observed_at,target_close,instrument_key,status,reason);
        CREATE TABLE short_events(version,triggered_at,phase,strategy,status);''')
        config={'enrollment_days':30,'bar_arrival_seconds':600,'rules':[]}
        db.execute('INSERT INTO short_experiments VALUES(?,?,?,?)',('v','2026-01-01T00:05:00+00:00','2026-01-08T00:05:00+00:00',json.dumps(config)))
        db.executemany('INSERT INTO market_instruments VALUES(?,1,?)',[('a','spot'),('b','spot'),('c','swap')])
        result=short_quality(db,timestamp('2026-01-01T02:09:00+00:00'))
        self.assertEqual(result['expected_asset_hours'],2)
        self.assertEqual(result['timely_complete_asset_hours'],0)
        self.assertEqual(result['per_instrument'][0]['never_assessed_hours'],1)

    def test_pair_join_retains_incomplete_and_repeated_asset(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""CREATE TABLE short_events(id,instrument_key,target_close,strategy,phase,evidence,matched_event_id,triggered_at);
        CREATE TABLE short_positions(id,event_id);
        CREATE TABLE short_outcomes(position_id,horizon_hours,observed_at,status,net_return);""")
        at='2026-01-01T00:00:00+00:00'
        for ident, asset, match in [(1,'a',None),(2,'b',1),(3,'a',None),(4,'c',3)]:
            db.execute('INSERT INTO short_events VALUES(?,?,?,?,?,?,?,?)',(ident,asset,at,'volume','holdout','{}',match,at))
            db.execute('INSERT INTO short_positions VALUES(?,?)',(ident,ident))
        for ident, value in [(1,'0.1'),(2,'0.02'),(3,'-0.1')]:
            db.execute('INSERT INTO short_outcomes VALUES(?,24,?,?,?)',(ident,at,'quoted',value))
        g=pair_quality(db,timestamp(at))['holdout:volume:24h']
        self.assertEqual(g['ledger_pairs'],2)
        self.assertEqual(g['complete_pairs'],1)
        self.assertEqual(g['incomplete_pairs'],1)
        self.assertAlmostEqual(g['mean_pair_difference'],0.08)
        self.assertEqual(g['asset_bootstrap']['status'],'insufficient_clusters')


if __name__ == '__main__':
    unittest.main()
