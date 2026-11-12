import json
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from crypto_observer.core import Store,Fetch
from crypto_observer.market_data import setup,freeze_universe,universe,ingest_bars,closed_bars,book,collect_market,report,POLICY,derivatives

AT='2026-10-26T04:05:00.000000+00:00'
DT=datetime.fromisoformat(AT)
MS=int(DT.timestamp()*1000)

def response(payload,at=AT):
 return Fetch(at,at,200,json.dumps(payload).encode(),payload,None,1)

class Fake:
 def __init__(self,payloads):self.payloads=iter(payloads);self.urls=[]
 def get(self,url,spacing=0.1):
  self.urls.append(url)
  return response(next(self.payloads))

def catalog(kind,symbol='BTCUSDT',**extra):
 return {'symbol':symbol,'category':'SPOT' if kind=='spot' else 'USDT-FUTURES','baseCoin':symbol[:-4],'quoteCoin':'USDT','instrumentType':'crypto','symbolType':'crypto','type':'perpetual','status':'online',**extra}

def payload(data):return {'code':'00000','requestTime':MS,'data':data}

class MarketTests(unittest.TestCase):
 def setUp(self):
  self.s=Store(':memory:');setup(self.s)
  self.run=self.s.start({'test':'market'})
  with patch('crypto_observer.market_data.now',return_value=AT):
   freeze_universe(self.s,Fake([payload([catalog('spot')]),payload([catalog('swap')])]),self.run)
  self.instrument=universe(self.s)[0]
 def tearDown(self):self.s.close()
 def rid(self,p):return self.s.request(self.run,'test','https://test/candles',response(p))
 def bar(self,start,quote='100'):
  return [str(int(start.timestamp()*1000)),'10','12','9','11','10',quote]
 def test_frozen_subset_excludes_stocks_and_does_not_expand(self):
  with StoreContext() as s:
   run=s.start({})
   instruments=[catalog('spot',f'COIN{i}USDT') for i in range(30)]+[catalog('spot','RAPPLEUSDT',instrumentType='stock')]
   frozen=freeze_universe(s,Fake([payload(instruments),payload([catalog('swap')])]),run)
   self.assertEqual(frozen['inventory'],31)
   self.assertEqual(len(universe(s)),25)
   self.assertEqual(s.db.execute('SELECT COUNT(*) FROM market_instruments').fetchone()[0],31)
   no_calls=Fake([])
   self.assertEqual(freeze_universe(s,no_calls,run)['status'],'already_frozen')
   self.assertEqual(no_calls.urls,[])
 def test_closed_canonical_asof_and_future_incomplete_rejected(self):
  rows=[self.bar(DT.replace(hour=2,minute=0)),self.bar(DT.replace(hour=3,minute=0)),self.bar(DT.replace(hour=4,minute=0))]
  p=payload(rows)
  result=ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)
  self.assertEqual((result['accepted'],result['rejected']),(2,1))
  self.assertEqual(closed_bars(self.s,self.instrument['key'],'2026-10-26T04:00:00Z'),[])
  bars=closed_bars(self.s,self.instrument['key'],AT)
  self.assertEqual(len(bars),2)
  self.assertEqual(bars[0]['quote_currency'],'USDT')
  self.assertEqual(bars[0]['volume_quote'],'100')
  self.assertIn('no_exchange_confirm',bars[0]['closure_policy'])
 def test_invalid_schema_unit_numeric_ohlc_and_source_clock(self):
  valid=self.bar(DT.replace(hour=2,minute=0))
  bad_ohlc=valid.copy();bad_ohlc[2]='8'
  bad_units=valid.copy();bad_units[6]='NaN'
  wrong_schema=valid+['ambiguous_volume_field']
  p=payload([bad_ohlc,bad_units,wrong_schema])
  result=ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)
  self.assertEqual(result['accepted'],0)
  self.assertEqual(result['rejected'],3)
  p=payload([valid]);p['requestTime']=MS+999999
  result=ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)
  self.assertEqual(result['accepted'],0)
 def test_duplicate_and_revision_not_overwritten_cross_instrument(self):
  row=self.bar(DT.replace(hour=2,minute=0));p=payload([row,row])
  self.assertEqual(ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)['accepted'],0)
  p=payload([row]);ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)
  row[6]='999';p=payload([row])
  self.assertEqual(ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)['revised'],1)
  self.assertEqual(closed_bars(self.s,self.instrument['key'],AT)[0]['volume_quote'],'100')
  other=[r for r in universe(self.s) if r['kind']=='swap'][0]
  self.assertEqual(closed_bars(self.s,other['key'],AT),[])
 def test_gap_not_filled_and_received_at_not_backdated(self):
  p=payload([self.bar(DT.replace(hour=0,minute=0)),self.bar(DT.replace(hour=2,minute=0))])
  ingest_bars(self.s,self.instrument,self.rid(p),response(p),p)
  bars=closed_bars(self.s,self.instrument['key'],AT)
  self.assertEqual(len(bars),2)
  self.assertTrue(all(r['received_at']==AT for r in bars))
  self.assertEqual(datetime.fromisoformat(bars[1]['open_at'])-datetime.fromisoformat(bars[0]['close_at']),timedelta(hours=1))
 def test_book_order_staleness_identity_and_unknown(self):
  good={'a':[['11','2'],['12','4']],'b':[['10','3'],['9','1']],'ts':str(MS)}
  q=book(self.s,Fake([payload(good)]),self.run,self.instrument['key'])
  self.assertEqual(q['status'],'quoted');self.assertEqual(q['size_unit'],'BTC')
  for bad in [{**good,'a':list(reversed(good['a']))},{**good,'ts':str(MS-60000)},{**good,'symbol':'ETHUSDT'},{**good,'b':[['11','1']]}]:
   self.assertEqual(book(self.s,Fake([payload(bad)]),self.run,self.instrument['key'])['status'],'quote_unknown')
  self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM market_books').fetchone()[0],5)
 def test_derivative_units_and_wrong_identity_retained_unknown(self):
  swap=[r for r in universe(self.s) if r['kind']=='swap'][0]
  values=[payload({'openInterestList':[{'symbol':'BTCUSDT','size':'10'}],'ts':str(MS)}),payload([{'symbol':'BTCUSDT','fundingRate':'-0.001','fundingRateInterval':'8','nextUpdate':str(MS+3600000)}]),payload([{'symbol':'ETHUSDT','price':'10','markPrice':'11','indexPrice':'12','ts':str(MS)}])]
  d=derivatives(self.s,Fake(values),self.run,swap)
  self.assertEqual(d['funding_rate'],'-0.001')
  self.assertIsNone(d['basis_fraction'])
  self.assertEqual(self.s.db.execute('SELECT oi_unit FROM market_derivatives').fetchone()[0],'provider_size_unit_unverified')
 def test_reality_misclassified_crypto_excluded_and_zero_launch_unknown(self):
  with StoreContext() as s:
   run=s.start({})
   rows=[catalog('spot','RSATLUSDT',symbolType='stock',isReality='yes'),
         catalog('spot','ZEROUSDT',launchTime='0'),
         catalog('spot','RWAUSDT',isRwa='YES'),catalog('spot','ISINUSDT',isin='US0000000000')]
   result=freeze_universe(s,Fake([payload(rows),payload([catalog('swap',launchTime='0')])]),run)
   self.assertEqual(result['inventory'],2)
   self.assertEqual({r['instrument'] for r in universe(s)},{'ZEROUSDT','BTCUSDT'})
   self.assertTrue(all(r['launch_at'] is None for r in universe(s)))
 def test_legacy_frozen_universe_preserved_requires_fresh_database(self):
  before=[tuple(x) for x in self.s.db.execute('SELECT * FROM market_instruments')]
  self.s.db.execute("UPDATE market_universes SET version='market-v1'")
  self.s.db.commit()
  with self.assertRaisesRegex(ValueError,'fresh database'):
   freeze_universe(self.s,Fake([]),self.run)
  self.assertEqual([tuple(x) for x in self.s.db.execute('SELECT * FROM market_instruments')],before)
  with self.assertRaisesRegex(ValueError,'fresh database'):
   universe(self.s)
 def test_collection_fetches_books_last_and_exposes_candle_clock_failure(self):
  candle=payload([self.bar(DT.replace(hour=2,minute=0))]);bad_candle=dict(candle,requestTime=MS+999999)
  oi=payload({'openInterestList':[{'symbol':'BTCUSDT','size':'10'}],'ts':str(MS)})
  funding=payload([{'symbol':'BTCUSDT','fundingRate':'0.001','fundingRateInterval':'8','nextUpdate':str(MS+3600000)}])
  prices=payload([{'symbol':'BTCUSDT','price':'10','markPrice':'11','indexPrice':'12','ts':str(MS)}])
  depth=payload({'a':[['11','2']],'b':[['10','3']],'ts':str(MS)})
  client=Fake([bad_candle,candle,oi,funding,prices,depth,depth])
  result=collect_market(self.s,client,2)
  self.assertTrue(any('clock' in note for note in result['notes']))
  self.assertTrue(all('orderbook' in url for url in client.urls[-2:]))
  self.assertTrue(all('orderbook' not in url for url in client.urls[:-2]))
  self.assertEqual(result['bars_added'],1)
  self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM market_books').fetchone()[0],2)
 def test_report_empty_and_provenance(self):
  self.assertIn('USDT is not USD',report(self.s))
  self.assertEqual(self.s.db.execute('PRAGMA foreign_key_check').fetchall(),[])

class StoreContext:
 def __enter__(self):self.store=Store(':memory:');setup(self.store);return self.store
 def __exit__(self,*args):self.store.close()
