import json
import unittest
from crypto_observer.core import Store, Fetch, normalize, collect
from crypto_observer.short_windows import extract, report
from test_observer import Fake, pool, POOL, TOKEN


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
    def tearDown(self):
        self.store.close()

    def test_signed_return_and_untrusted_fields(self):
        item = {'volume':{'m5':'-1','h1':0},'priceChange':{'m5':'-12.5','h1':'NaN'},
                'txns':{'m5':{'buys':'1e999999','sells':False}}}
        rows = extract(item,'dexscreener')
        self.assertEqual(len(rows),4)
        m5 = rows[0]
        self.assertIsNone(m5[3])
        self.assertEqual(m5[4],'-12.5')
        self.assertIsNone(m5[5]);self.assertIsNone(m5[6])
        self.assertEqual(rows[1][3],'0')
        self.assertIsNone(rows[1][4])
        self.assertIsNone(m5[-2])

    def test_missing_and_malformed_windows_stay_unknown(self):
        item = {'attributes':{'volume_usd':[], 'transactions':{'m5':7}}}
        rows = extract(item,'geckoterminal')
        self.assertEqual(len(rows),6)
        self.assertTrue(all(row[3] is None and row[5] is None for row in rows))
        self.assertEqual(extract({},'unrecognized'),[])

    def test_live_path_dedup_raw_link_and_v2_fields_preserved(self):
        item=pool()
        item['attributes'].update({'volume_usd':{'h24':'1000','m5':'12.25'},
                                   'price_change_percentage':{'m5':'-5.5'}})
        collect(self.store,Fake([{'data':[item,item]},{'pairs':[]}]),1,100)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM snapshot_windows').fetchone()[0],6)
        row=self.store.db.execute('''SELECT w.*,s.volume_24h_usd,s.buys_24h,r.body
          FROM snapshot_windows w JOIN snapshots s ON s.id=w.snapshot_id
          JOIN requests r ON r.id=s.request_id WHERE w.window='m5' ''').fetchone()
        self.assertEqual(row['price_change_pct'],'-5.5')
        self.assertEqual(row['volume_usd'],'12.25')
        self.assertEqual(row['volume_24h_usd'],'1000')
        self.assertEqual(row['buys_24h'],5)
        self.assertIsNone(row['source_at'])
        self.assertEqual(json.loads(row['body'])['data'][0],item)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_per_pool_coverage_not_global_frequency(self):
        run=self.store.start({})
        a=pool();b=pool();b['attributes']['address']='0x'+'c'*40
        # Globally a record every 15m; each particular pool only every 30m.
        for minute,item in [(0,a),(15,b),(30,a),(45,b)]:
            at=f'2026-10-25T12:{minute:02d}:00+00:00'
            fetch=Fetch(at,at,200,json.dumps(item).encode(),item,None,1)
            rid=self.store.request(run,'geckoterminal','test',fetch)
            self.store.save(rid,'geckoterminal',at,normalize(item,'geckoterminal'),raw_item=item)
        text=report(self.store)
        self.assertIn('| geckoterminal | 2 |',text)
        self.assertIn('| 2 | 1800.0 | 2 |',text)
        self.assertIn('not closed, non-overlapping candles',text)
        self.assertIn('Market-data staleness cannot be measured',text)

    def test_old_snapshots_are_not_silently_backfilled(self):
        item=pool();run=self.store.start({})
        at='2026-10-25T12:00:00Z'
        rid=self.store.request(run,'geckoterminal','test',Fetch(at,at,200,b'{}',{},None,1))
        self.store.save(rid,'geckoterminal',at,normalize(item,'geckoterminal'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM snapshot_windows').fetchone()[0],0)
        self.assertIn('No new window observations yet',report(self.store))

from unittest.mock import patch
from crypto_observer.short_windows import coverage_metrics


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
    def tearDown(self):
        self.store.close()
    def save(self, address, at, windows=True):
        item=pool();item['attributes']['address']=address
        run=self.store.start({})
        rid=self.store.request(run,'geckoterminal','test',Fetch(at,at,200,b'{}',{},None,1))
        self.store.save(rid,'geckoterminal',at,normalize(item,'geckoterminal'),raw_item=item if windows else None)

    def test_all_retained_pools_include_stale_and_never_window_observed(self):
        at='2026-10-26T12:00:00+00:00'
        self.save(POOL,'2026-10-26T11:50:00+00:00')
        self.save('0x'+'c'*40,'2026-10-26T10:00:00+00:00')
        self.save('0x'+'d'*40,'2026-10-26T11:59:00+00:00',windows=False)
        self.store.mark(['0x'+'c'*40],at,'source_missing')
        data=coverage_metrics(self.store,at)
        gt=next(r for r in data['sources'] if r['source']=='geckoterminal')
        dex=next(r for r in data['sources'] if r['source']=='dexscreener')
        self.assertEqual(gt['retained_pool_denominator'],3)
        self.assertEqual(gt['covered_30m'],1)
        self.assertEqual(gt['coverage_30m_fraction'],1/3)
        self.assertEqual(gt['stale_observed_30m'],1)
        self.assertEqual(gt['never_observed_pools'],1)
        self.assertEqual(gt['uncovered_30m'],2)
        self.assertEqual(dex['never_observed_pools'],3)
        self.assertEqual(data['last_refresh_status']['source_missing'],1)
        self.assertIsNone(gt['gap_p95_seconds'])

    def test_invalid_and_future_times_do_not_count_as_covered(self):
        self.save(POOL,'bad')
        self.save('0x'+'c'*40,'2026-10-27T12:00:00+00:00')
        self.save('0x'+'d'*40,'2026-10-26T11:59:00')
        gt=next(r for r in coverage_metrics(self.store,'2026-10-26T12:00:00Z')['sources'] if r['source']=='geckoterminal')
        self.assertEqual(gt['invalid_or_future_times'],3)
        self.assertEqual(gt['covered_30m'],0)
        self.assertEqual(gt['never_observed_pools'],3)
        with patch('crypto_observer.short_windows.now',return_value='2026-10-26T12:00:00Z'):
            # Original report also needs robust malformed timestamp handling.
            self.assertIn('Invalid/future timestamps',report(self.store))

    def test_capacity_uses_actual_collection_intervals_and_budget(self):
        for suffix in 'abcd':
            self.save('0x'+suffix*40,'2026-10-26T10:00:00+00:00',windows=False)
        for start,end in [('2026-10-26T10:00:00Z','2026-10-26T10:02:00Z'),('2026-10-26T10:20:00Z','2026-10-26T10:22:00Z')]:
            self.store.db.execute('INSERT INTO runs(started_at,finished_at,status,config) VALUES(?,?,?,?)',
              (start,end,'ok',json.dumps({'chain':'base','refresh_limit':3})))
        data=coverage_metrics(self.store,'2026-10-26T12:00:00Z',budget=3)
        self.assertEqual(data['capacity_estimate']['fixed_universe_waves'],2)
        self.assertEqual(data['capacity_estimate']['estimated_rotation_seconds'],2400)
        self.assertEqual(data['capacity_estimate']['collection_duration_p50_seconds'],120)
        self.assertIsNone(coverage_metrics(self.store,'2026-10-26T12:00:00Z',budget=300)['capacity_estimate']['estimated_rotation_seconds'])

    def test_empty_universe_coverage_fraction_is_unknown(self):
        data=coverage_metrics(self.store,'2026-10-26T12:00:00Z')
        self.assertIsNone(data['sources'][0]['coverage_30m_fraction'])
        self.assertIsNone(data['capacity_estimate']['estimated_rotation_seconds'])
