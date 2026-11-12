import json
import unittest
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from unittest.mock import patch
from crypto_observer.core import Store,Fetch
from crypto_observer import short_experiment as se

AT='2026-10-26T12:05:00+00:00'
CREATED='2026-10-26T00:00:00+00:00'
INST={'key':'bitget:spot:AAAUSDT','venue':'bitget','instrument':'AAAUSDT','kind':'spot','base':'AAA','quote':'USDT','status':'online','launch_at':'2025-01-01T00:00:00+00:00'}


def bars(inst=INST,at=AT,volume='30000',change='0.04'):
    end=se.timestamp(at).replace(minute=0,second=0,microsecond=0)
    rows=[]
    for i in range(25):
        start=end-timedelta(hours=25-i);close=Decimal('100')*(1+Decimal(change)) if i==24 else Decimal('100')
        rows.append({'id':i+1,'instrument_key':inst['key'],'open_at':start.isoformat(),'close_at':(start+timedelta(hours=1)).isoformat(),
                     'received_at':at,'open':'100','high':str(max(close,Decimal('100'))),'low':'100','close':str(close),
                     'volume_quote':volume if i==24 else '10000','quote_currency':'USDT'})
    return rows


def raw_book(inst=INST,at=AT):
    return {'instrument_key':inst['key'],'kind':'spot','execution_supported':True,'size_unit':inst['base'],'quote_currency':'USDT',
            'observed_at':at,'source_at':at,'status':'quoted','request_id':None,
            'bids':[['99','100'],['98','100']],'asks':[['100','100'],['101','100']]}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        with patch.object(se,'now',return_value=CREATED):se.setup(self.store)
    def tearDown(self):self.store.close()
    def linked_book(self,at):
        raw=raw_book(at=at);run=self.store.start({'test':'book'})
        raw['request_id']=self.store.request(run,'test_book','test:book',Fetch(at,at,200,json.dumps(raw).encode(),raw,None,1))
        return raw
    def exp(self):return self.store.db.execute('SELECT * FROM short_experiments').fetchone()
    def assess(self,instruments=None,at=AT,rows=None):
        instruments=instruments or [INST]
        with patch.object(se,'closed_bars',side_effect=lambda s,k,t:rows if rows is not None else bars(next(i for i in instruments if i['key']==k),at)):
            return se.assess(self.store,instruments,at,self.exp())
    def create(self,assessed,at=AT):
        return se.create_events(self.store,assessed,at)
    def pending(self):
        self.create(self.assess())
        return self.store.db.execute('SELECT * FROM short_positions ORDER BY id LIMIT 1').fetchone()

    def test_baseline_excludes_current_and_records_ids(self):
        status,reason,f=se.features(bars(),INST,AT,CREATED)
        self.assertEqual(status,'complete')
        self.assertEqual(f['volume_ratio'],'3')
        self.assertEqual(f['baseline_ids'],list(range(1,25)))
        self.assertEqual(f['bar_id'],25)
        changed=bars(volume='90000')
        self.assertEqual(se.features(changed,INST,AT,CREATED)[2]['baseline_volume_median'],'10000')

    def test_gap_late_unclosed_wrong_unit_do_not_trigger(self):
        cases=[]
        missing=bars();missing.pop(3);cases.append(missing)
        late=bars();late[-1]['received_at']='2026-10-26T12:11:00+00:00';cases.append(late)
        future=bars();future[-1]['close_at']='2026-10-26T13:00:00+00:00';cases.append(future)
        unit=bars();unit[5]['quote_currency']='AAA';cases.append(unit)
        duplicate=bars();duplicate[-2]=dict(duplicate[-3]);cases.append(duplicate)
        zero=bars();[x.update(volume_quote='0') for x in zero[:-1]];cases.append(zero)
        for rows in cases:
            self.assertEqual(se.features(rows,INST,AT,CREATED)[0],'unknown')
        self.assertEqual(se.features(bars(),INST,AT,AT)[1],'pre_enrollment_bar')

    def test_depth_consumes_levels_and_rejects_capacity_or_wrong_identity(self):
        raw=raw_book();raw['asks']=[['100','0.5'],['110','1']]
        q=se.depth_quote(raw,'buy','100',AT,INST)
        self.assertEqual(q['status'],'quoted')
        self.assertEqual(Decimal(q['gross_quote']),100)
        self.assertLess(Decimal(q['quantity_base']),1)
        self.assertEqual(q['evidence']['levels_used'],2)
        self.assertEqual(se.depth_quote(raw,'buy','10000',AT,INST)['status'],'quote_unknown')
        raw['instrument_key']='bitget:spot:OTHERUSDT'
        self.assertEqual(se.depth_quote(raw,'buy','100',AT,INST)['evidence']['reason'],'book_instrument_mismatch')

    def test_book_staleness_units_order_and_no_midpoint_fallback(self):
        for change in [{'source_at':'2026-10-26T12:00:00+00:00'},{'kind':'swap'},{'size_unit':'contracts'},
                       {'quote_currency':'USD'},{'bids':[['101','1']]},{'asks':[]}]:
            raw={**raw_book(),**change}
            self.assertEqual(se.depth_quote(raw,'buy','100',AT,INST)['status'],'quote_unknown')
        self.assertEqual(se.depth_quote(raw_book(),'sell','1',AT,INST)['gross_quote'],'99')

    def test_event_freeze_restart_and_later_arrivals_do_not_expand_budget(self):
        first=self.assess()
        self.create(first)
        counts=self.store.db.execute('SELECT COUNT(*) FROM short_events').fetchone()[0]
        self.create(self.assess())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM short_events').fetchone()[0],counts)
        other={**INST,'key':'bitget:spot:BBBUSDT','instrument':'BBBUSDT','base':'BBB'}
        assessed=self.assess([INST,other])
        self.create(assessed)
        for strategy in se.CONFIG['rules']:
            n=self.store.db.execute("SELECT COUNT(*) FROM short_events WHERE strategy=? AND status='selected'",(strategy,)).fetchone()[0]
            self.assertEqual(n,1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM short_hour_decisions').fetchone()[0],3)

    def test_unknown_and_nontrigger_assessments_are_retained(self):
        self.create(self.assess(rows=bars(volume='10000',change='0')))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM short_events WHERE status='not_triggered'").fetchone()[0],3)
        self.assess(rows=[])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM short_assessments WHERE status='unknown'").fetchone()[0],1)

    def test_actual_matching_and_without_replacement_links(self):
        other={**INST,'key':'bitget:spot:BBBUSDT','instrument':'BBBUSDT','base':'BBB'}
        # Matched control has similar turnover decade and prior volatility but no trigger.
        with patch.object(se,'closed_bars',side_effect=lambda s,k,t:bars(INST) if k==INST['key'] else bars(other,volume='20000',change='0')):
            assessed=se.assess(self.store,[INST,other],AT,self.exp())
        with patch.object(se,'matching_depth',return_value={'depth_log10':4,'spread_bucket':10,'book_id':1}):
            result=self.create(assessed)
        self.assertEqual(result['matched_controls'],3)
        links=self.store.db.execute("SELECT * FROM short_events WHERE strategy LIKE 'matched_random:%'").fetchall()
        self.assertEqual(len(links),3)
        self.assertTrue(all(x['matched_event_id'] is not None and x['instrument_key']==other['key'] for x in links))
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_missing_matching_age_does_not_fake_control(self):
        other={**INST,'key':'bitget:spot:BBBUSDT','instrument':'BBBUSDT','base':'BBB','launch_at':None}
        with patch.object(se,'closed_bars',side_effect=lambda s,k,t:bars(INST) if k==INST['key'] else bars(other,volume='20000',change='0')):
            assessed=se.assess(self.store,[INST,other],AT,self.exp())
        with patch.object(se,'matching_depth',return_value={'depth_log10':4,'spread_bucket':10}):
            self.create(assessed)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM short_events WHERE strategy LIKE 'matched_random:%'").fetchone()[0],0)

    def test_entry_delay_missed_window_and_restart_terminal(self):
        self.pending();run=self.store.start({})
        with patch.object(se,'now',return_value=AT),patch.object(se,'book') as b:
            se.positions(self.store,None,run,AT,[INST]);b.assert_not_called()
        late='2026-10-26T12:17:00+00:00'
        with patch.object(se,'now',return_value=late),patch.object(se,'book') as b:
            se.positions(self.store,None,run,late,[INST]);b.assert_not_called()
            se.positions(self.store,None,run,late,[INST]);b.assert_not_called()
        statuses=self.store.db.execute('SELECT status,reason FROM short_positions').fetchall()
        self.assertTrue(all(x['status']=='entry_unknown' and x['reason']=='entry_window_missed' for x in statuses))

    def test_real_quote_clock_entry_and_missed_horizons_without_network(self):
        self.pending();run=self.store.start({});entry='2026-10-26T12:06:00+00:00'
        with patch.object(se,'now',return_value=entry),patch.object(se,'book',return_value=self.linked_book(entry)):
            se.positions(self.store,None,run,entry,[INST])
        row=self.store.db.execute('SELECT * FROM short_positions LIMIT 1').fetchone()
        self.assertEqual(row['entry_at'],entry)
        self.assertEqual(row['quantity_base'],'1')
        self.assertEqual(row['entry_cost_quote'],'100.100')
        late='2026-10-28T12:06:00+00:00'
        with patch.object(se,'now',return_value=late),patch.object(se,'book') as b:
            se.positions(self.store,None,run,late,[INST]);b.assert_not_called()
        marks=self.store.db.execute('SELECT status FROM short_outcomes').fetchall()
        self.assertEqual(len(marks),9)
        self.assertTrue(all(x[0]=='missed_window' for x in marks))

    def test_1h_exit_quote_and_fee_sensitivity_not_future_backfill(self):
        self.pending();run=self.store.start({});entry='2026-10-26T12:06:00+00:00'
        with patch.object(se,'now',return_value=entry),patch.object(se,'book',return_value=self.linked_book(entry)):
            se.positions(self.store,None,run,entry,[INST])
        exit_at='2026-10-26T13:07:00+00:00'
        with patch.object(se,'now',return_value=exit_at),patch.object(se,'book',return_value=self.linked_book(exit_at)):
            se.positions(self.store,None,run,exit_at,[INST])
        mark=self.store.db.execute('SELECT * FROM short_outcomes LIMIT 1').fetchone()
        stress=json.loads(mark['stress_returns'])
        self.assertEqual(Decimal(stress['0']),Decimal('-0.01'))
        self.assertLess(Decimal(stress['30']),Decimal(stress['10']))
        self.assertEqual(mark['horizon_hours'],1)
        with patch.object(se,'now',return_value=exit_at):
            self.assertIn('Sampled favorable return',se.report(self.store))

    def test_calibration_freezes_once_only_pre_boundary_complete_samples(self):
        exp=self.exp();boundary=exp['development_until']
        # 100 rows / 4 assets observed during development, and one extreme late row.
        for i in range(101):
            f={'volume_ratio':'2' if i<100 else '99999','price_return':'0.02' if i<100 else '100'}
            observed='2026-10-27T12:00:00+00:00' if i<100 else '2026-11-03T12:00:00+00:00'
            self.store.db.execute('INSERT INTO short_assessments(version,instrument_key,target_close,bar_id,observed_at,phase,status,features,dedup) VALUES(?,?,?,?,?,?,?,?,?)',
                (se.VERSION,str(i%4),'2026-10-27T11:00:00+00:00',i,observed,'development','complete',json.dumps(f),str(i)))
        self.store.db.commit()
        frozen=se.calibration(self.store,'2026-11-04T12:00:00+00:00')
        thresholds=json.loads(frozen['frozen_thresholds'])
        self.assertEqual(thresholds,{'volume_ratio':'2','price_return':'0.02'})
        self.assertEqual(len(json.loads(frozen['calibration_evidence'])['assessment_ids']),100)
        again=se.calibration(self.store,'2026-11-05T12:00:00+00:00')
        self.assertEqual(again['frozen_thresholds'],frozen['frozen_thresholds'])

    def test_insufficient_calibration_blocks_holdout_and_config_change(self):
        self.assertEqual(se.calibration(self.store,'2026-11-04T12:00:00+00:00')['calibration_status'],'insufficient')
        with patch.dict(se.CONFIG,{'base_fee_bps':20}):
            with self.assertRaises(ValueError):se.setup(self.store)

    def test_cycle_rechecks_time_after_network_before_deciding(self):
        clocks=iter([AT,'2026-10-26T12:20:00+00:00'])
        with patch.object(se,'now',side_effect=lambda:next(clocks)),patch.object(se,'universe',return_value=[INST]),patch.object(se,'positions',return_value={}),patch.object(se,'closed_bars',return_value=bars()):
            result=se.cycle(self.store,None)
        self.assertEqual(result['unknown'],1)
        self.assertEqual(self.store.db.execute('SELECT reason FROM short_assessments').fetchone()[0],'decision_window_missed')


    def test_decision_only_crash_recovers_original_entries_as_missed_across_day(self):
        self.create(self.assess())
        # Simulate a durable decision surviving a crash before events/positions.
        self.store.db.execute('DELETE FROM short_positions')
        self.store.db.execute('DELETE FROM short_events')
        self.store.db.commit()
        self.assertEqual(se.frozen_orphans(self.store)['frozen_selections_missing_events'],3)
        late='2026-10-27T12:05:00+00:00'
        count=se.recover_decisions(self.store,late)
        self.assertEqual(count,3)
        self.assertEqual(se.frozen_orphans(self.store),{'frozen_selections_missing_events':0,'selected_events_missing_positions':0})
        self.assertEqual(se.recover_decisions(self.store,late),0)
        records=self.store.db.execute('SELECT p.status,p.reason,e.triggered_at,e.actionable_at FROM short_positions p JOIN short_events e ON e.id=p.event_id').fetchall()
        self.assertTrue(all(x['status']=='entry_unknown' and x['reason']=='entry_window_missed' for x in records))
        self.assertTrue(all(se.timestamp(x['triggered_at'])==se.timestamp(AT) for x in records))
        self.assertTrue(all(se.timestamp(x['actionable_at'])==se.timestamp(AT)+timedelta(seconds=60) for x in records))

    def test_recovery_rebuilds_controls_and_missing_position_links(self):
        other={**INST,'key':'bitget:spot:BBBUSDT','instrument':'BBBUSDT','base':'BBB'}
        with patch.object(se,'closed_bars',side_effect=lambda s,k,t:bars(INST) if k==INST['key'] else bars(other,volume='20000',change='0')):
            assessed=se.assess(self.store,[INST,other],AT,self.exp())
        with patch.object(se,'matching_depth',return_value={'depth_log10':4,'spread_bucket':10,'book_id':1}):
            self.create(assessed)
        self.store.db.execute('DELETE FROM short_positions')
        self.store.db.execute('DELETE FROM short_events WHERE matched_event_id IS NOT NULL')
        self.store.db.execute('DELETE FROM short_events')
        self.store.db.commit()
        se.recover_decisions(self.store,'2026-10-26T13:05:00+00:00')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM short_positions').fetchone()[0],6)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM short_events WHERE matched_event_id IS NOT NULL').fetchone()[0],3)
        self.assertEqual(se.frozen_orphans(self.store)['frozen_selections_missing_events'],0)

    def test_matching_depth_uses_only_received_fresh_source_books(self):
        self.store.db.executescript('''CREATE TABLE market_books(id INTEGER PRIMARY KEY,instrument_key TEXT,request_id INTEGER,observed_at TEXT,source_at TEXT,status TEXT,bids TEXT,asks TEXT);''')
        raw=raw_book()
        self.store.db.execute('INSERT INTO market_books VALUES(?,?,?,?,?,?,?,?)',(1,INST['key'],None,AT,AT,'quoted',json.dumps(raw['bids']),json.dumps(raw['asks'])))
        result=se.matching_depth(self.store,INST,AT)
        self.assertEqual(result['book_id'],1)
        self.assertEqual(result['depth_log10'],3)
        self.assertIsNone(se.matching_depth(self.store,INST,'2026-10-26T12:06:00+00:00'))
        self.assertIsNone(se.matching_depth(self.store,INST,'2026-10-26T12:04:00+00:00'))

    def test_cooldown_merges_continuous_trigger_without_changing_sample(self):
        self.create(self.assess())
        future='2026-10-26T13:05:00+00:00'
        assessed=self.assess(at=future)
        self.create(assessed,future)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM short_events WHERE status='cooldown'").fetchone()[0],3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM short_positions').fetchone()[0],3)
