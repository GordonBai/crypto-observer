"""Frozen-rule prospective paper experiments using read-only route quotes."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from .core import now, number
from .security import setup as security_setup
from .holder_index import setup as holder_setup

USDC='0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
CONFIG={'version':'v2','liquidity_min':50000,'max_snapshot_age_minutes':120,'holder_growth_min':0.2,'holder_growth_interval_hours':[20,28],'buy_fraction_min':0.7,'min_transactions_24h':20,'horizons_days':[7,30,90],'quote_notional_usdc':100,'stress_bps':[0,50,200],'success_net_return':1.0,'rules':['liquidity','buy_pressure','holder_growth','matched_random'],'selection_limit_per_rule':2,'entry_delay_minutes':15,'exit_tolerance_hours':24,'enrollment_days':30,'temporal_split_after_days':15,'control_policy':'without_replacement_exclude_selected_treatments','holder_source_policy':'same_source_reconciled_and_caught_up'}


def setup(store):
 security_setup(store)
 holder_setup(store)
 store.db.executescript("""
 CREATE TABLE IF NOT EXISTS experiments(version TEXT PRIMARY KEY,created_at TEXT NOT NULL,config TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS paper_signals(id INTEGER PRIMARY KEY,version TEXT NOT NULL REFERENCES experiments(version),cohort TEXT NOT NULL,strategy TEXT NOT NULL,token_address TEXT NOT NULL,pool_address TEXT NOT NULL,decided_at TEXT NOT NULL,snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),evidence TEXT NOT NULL,UNIQUE(version,cohort,strategy,token_address));
 CREATE TABLE IF NOT EXISTS route_quotes(id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL REFERENCES requests(id),token_in TEXT NOT NULL,token_out TEXT NOT NULL,amount_in TEXT NOT NULL,amount_out TEXT,gas_usd TEXT,status TEXT NOT NULL,observed_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS paper_positions(id INTEGER PRIMARY KEY,signal_id INTEGER UNIQUE NOT NULL REFERENCES paper_signals(id),entry_quote_id INTEGER NOT NULL REFERENCES route_quotes(id),entry_at TEXT NOT NULL,quantity_raw TEXT,notional_usdc TEXT NOT NULL,entry_gas_usd TEXT,entry_status TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS cohort_decisions(version TEXT NOT NULL,cohort TEXT NOT NULL,decided_at TEXT NOT NULL,selection_limit INTEGER NOT NULL,selections TEXT NOT NULL,PRIMARY KEY(version,cohort));
 CREATE TABLE IF NOT EXISTS paper_matches(treatment_signal_id INTEGER PRIMARY KEY REFERENCES paper_signals(id),control_signal_id INTEGER NOT NULL REFERENCES paper_signals(id));
 CREATE TABLE IF NOT EXISTS daily_position_marks(position_id INTEGER NOT NULL REFERENCES paper_positions(id),day TEXT NOT NULL,quote_id INTEGER NOT NULL REFERENCES route_quotes(id),observed_at TEXT NOT NULL,status TEXT NOT NULL,net_value_usdc TEXT,PRIMARY KEY(position_id,day));
 CREATE TABLE IF NOT EXISTS paper_marks(id INTEGER PRIMARY KEY,position_id INTEGER NOT NULL REFERENCES paper_positions(id),quote_id INTEGER NOT NULL REFERENCES route_quotes(id),horizon_days INTEGER NOT NULL,observed_at TEXT NOT NULL,status TEXT NOT NULL,net_value_usdc TEXT,UNIQUE(position_id,horizon_days));
 """)
 encoded=json.dumps(CONFIG,sort_keys=True)
 prior=store.db.execute('SELECT config FROM experiments WHERE version=?',(CONFIG['version'],)).fetchone()
 if prior and prior[0]!=encoded: raise ValueError('frozen experiment changed; create a new version')
 store.db.execute('INSERT OR IGNORE INTO experiments VALUES(?,?,?)',(CONFIG['version'],now(),encoded)); store.db.commit()


def quote(store,client,run,token_in,token_out,amount):
 if not str(amount).isdigit() or int(amount)<=0: raise ValueError('positive raw amount required')
 url='https://aggregator-api.kyberswap.com/base/api/v1/routes?'+urlencode({'tokenIn':token_in,'tokenOut':token_out,'amountIn':str(amount),'gasInclude':'true'})
 r=client.get(url,0.6); rid=store.request(run,'kyberswap_routes',url,r)
 p=r.payload if isinstance(r.payload,dict) else {}
 summary=(p.get('data') or {}).get('routeSummary') if isinstance(p.get('data'),dict) else None
 summary=summary if isinstance(summary,dict) else {}
 out=number(summary.get('amountOut')); gas=number(summary.get('gasUsd'))
 valid=not r.error and p.get('code')==0 and out is not None and Decimal(out)>0 and Decimal(out)==Decimal(out).to_integral_value() and str(summary.get('amountIn'))==str(amount) and str(summary.get('tokenIn','')).lower()==token_in and str(summary.get('tokenOut','')).lower()==token_out
 status='quoted' if valid and gas is not None else 'quote_unknown'
 cur=store.db.execute('INSERT INTO route_quotes(request_id,token_in,token_out,amount_in,amount_out,gas_usd,status,observed_at) VALUES(?,?,?,?,?,?,?,?)',(rid,token_in,token_out,str(amount),str(int(Decimal(out))) if valid else None,gas,status,r.observed_at)); store.db.commit()
 return dict(id=cur.lastrowid,amount_out=str(int(Decimal(out))) if valid else None,gas_usd=gas,status=status,observed_at=r.observed_at)


def candidates(store,asof):
 asof=datetime.fromisoformat(asof).astimezone(timezone.utc).isoformat(timespec="microseconds")
 cutoff=(datetime.fromisoformat(asof)-timedelta(minutes=CONFIG['max_snapshot_age_minutes'])).isoformat(timespec='microseconds')
 rows=store.db.execute("""SELECT s.*,p.first_seen_at FROM snapshots s JOIN pools p ON p.chain=s.chain AND p.address=s.pool_address
 WHERE s.id=(SELECT s2.id FROM snapshots s2 WHERE s2.pool_address=s.pool_address AND s2.chain=s.chain AND s2.observed_at<=? ORDER BY s2.observed_at DESC,s2.id DESC LIMIT 1)
 AND s.observed_at>=? AND s.token_address IS NOT NULL ORDER BY s.token_address,s.pool_address""",(asof,cutoff)).fetchall()
 best={}
 for row in rows:
  r=dict(row); liq=number(r['liquidity_usd']); price=number(r['price_usd'])
  if liq is None or Decimal(liq)<CONFIG['liquidity_min'] or price is None or Decimal(price)<=0 or r['token_address']==USDC: continue
  if r['token_address'] not in best or Decimal(liq)>Decimal(best[r['token_address']]['liquidity_usd']): best[r['token_address']]=r
 result=[]
 for r in best.values():
  growth=None
  holder_source=None
  # Prefer a complete event index; never compare a provider count against an indexed count.
  table_exists=store.db.execute("SELECT 1 FROM sqlite_master WHERE name='indexed_holder_snapshots'").fetchone()
  if table_exists:
   h=store.db.execute("SELECT * FROM indexed_holder_snapshots WHERE token_address=? AND observed_at<=? AND observed_at>=? AND status='event_supply_reconciled' AND caught_up=1 AND holder_count>0 ORDER BY observed_at DESC,id DESC LIMIT 1",(r['token_address'],asof,cutoff)).fetchone()
   if h:
    t=datetime.fromisoformat(h['observed_at']); lo=(t-timedelta(hours=28)).isoformat(); hi=(t-timedelta(hours=20)).isoformat()
    old=store.db.execute("SELECT holder_count FROM indexed_holder_snapshots WHERE token_address=? AND observed_at BETWEEN ? AND ? AND status='event_supply_reconciled' AND caught_up=1 AND holder_count>0 ORDER BY observed_at DESC,id DESC LIMIT 1",(r['token_address'],lo,hi)).fetchone()
    if old:
     growth=h['holder_count']/old[0]-1
     holder_source='event_index_supply_reconciled'
  buys,sells=r['buys_24h'],r['sells_24h']; total=(buys+sells) if buys is not None and sells is not None else None
  pressure=buys/total if total and total>=CONFIG['min_transactions_24h'] else None
  r['features']={'holder_growth_24h':growth,'holder_source':holder_source,'buy_fraction':pressure,'stratum':str(int(Decimal(r['liquidity_usd']).log10()))+(':new' if datetime.fromisoformat(asof)-datetime.fromisoformat(r['first_seen_at'])<timedelta(days=1) else ':older')}
  result.append(r)
 return result


def selection(rows,cohort,limit):
 selections=[]
 for strategy in CONFIG['rules'][:-1]:
  eligible=[r for r in rows if strategy=='liquidity' or (strategy=='buy_pressure' and r['features']['buy_fraction'] is not None and r['features']['buy_fraction']>=CONFIG['buy_fraction_min']) or (strategy=='holder_growth' and r['features']['holder_growth_24h'] is not None and r['features']['holder_growth_24h']>=CONFIG['holder_growth_min'])]
  eligible.sort(key=lambda r: hashlib.sha256((cohort+strategy+r['token_address']).encode()).hexdigest())
  treatments=eligible[:limit]
  treatment_tokens={r['token_address'] for r in treatments}
  used_controls=set()
  for r in treatments:
   selections.append((strategy,r))
   controls=[c for c in rows if c['features']['stratum']==r['features']['stratum'] and c['token_address'] not in treatment_tokens|used_controls]
   controls.sort(key=lambda c:hashlib.sha256((cohort+strategy+r['token_address']+c['token_address']).encode()).hexdigest())
   if controls:
    c=dict(controls[0]); c['matched_to']=r['token_address']
    used_controls.add(c['token_address'])
    selections.append(('matched_random:'+strategy,c))
 return selections


def open_positions(store,client,limit=2):
 setup(store)
 if not 1<=limit<=CONFIG['selection_limit_per_rule']: raise ValueError('selection limit exceeds frozen experiment budget')
 decided=now(); cohort=decided[:10]
 created=store.db.execute('SELECT created_at FROM experiments WHERE version=?',(CONFIG['version'],)).fetchone()[0]
 if datetime.fromisoformat(decided)>=datetime.fromisoformat(created)+timedelta(days=CONFIG['enrollment_days']):
  return {'opened':0,'entry_unknown':0,'status':'enrollment_complete'}
 prior=store.db.execute('SELECT * FROM cohort_decisions WHERE version=? AND cohort=?',(CONFIG['version'],cohort)).fetchone()
 if prior:
  decided=prior['decided_at']; chosen=json.loads(prior['selections']); eligible_count=None
 else:
  rows=candidates(store,decided); eligible_count=len(rows)
  # Empty feed must not freeze an entire daily cohort before the feed recovers.
  if not rows:return {'opened':0,'entry_unknown':0,'status':'no_fresh_candidates'}
  chosen=selection(rows,cohort,limit)
  store.db.execute('INSERT INTO cohort_decisions VALUES(?,?,?,?,?)',(CONFIG['version'],cohort,decided,limit,json.dumps(chosen)))
  store.db.commit()
 run=store.start({'module':'paper_open','version':CONFIG['version'],'limit_per_rule':limit,'cohort':cohort})
 opened=0; unknown=0
 try:
  signals={}
  for strategy,r in chosen:
   evidence=dict(r['features']); evidence['matched_to']=r.get('matched_to')
   store.db.execute('INSERT OR IGNORE INTO paper_signals(version,cohort,strategy,token_address,pool_address,decided_at,snapshot_id,evidence) VALUES(?,?,?,?,?,?,?,?)',(CONFIG['version'],cohort,strategy,r['token_address'],r['pool_address'],decided,r['id'],json.dumps(evidence)))
   signal=store.db.execute('SELECT id FROM paper_signals WHERE version=? AND cohort=? AND strategy=? AND token_address=?',(CONFIG['version'],cohort,strategy,r['token_address'])).fetchone()[0]
   signals[(strategy,r['token_address'])]=signal
  for strategy,r in chosen:
   if strategy.startswith('matched_random:'):
    treatment=signals.get((strategy.split(':',1)[1],r['matched_to']))
    store.db.execute('INSERT OR IGNORE INTO paper_matches VALUES(?,?)',(treatment,signals[(strategy,r['token_address'])]))
  store.db.commit()
  for strategy,r in chosen:
   signal=signals[(strategy,r['token_address'])]
   if store.db.execute('SELECT 1 FROM paper_positions WHERE signal_id=?',(signal,)).fetchone():continue
   if datetime.fromisoformat(now())-datetime.fromisoformat(decided)>timedelta(minutes=CONFIG['entry_delay_minutes']):
    # Record a failed entry rather than opening a stale signal after a restart.
    timestamp=now(); raw={'reason':'entry_window_missed'}
    from .core import Fetch
    rid=store.request(run,'local_policy','policy:entry_window',Fetch(timestamp,timestamp,None,json.dumps(raw).encode(),raw,None,1))
    cur=store.db.execute("INSERT INTO route_quotes(request_id,token_in,token_out,amount_in,status,observed_at) VALUES(?,?,?,?,?,?)",(rid,USDC,r['token_address'],'100000000','entry_window_missed',timestamp))
    store.db.execute('INSERT INTO paper_positions(signal_id,entry_quote_id,entry_at,notional_usdc,entry_status) VALUES(?,?,?,?,?)',(signal,cur.lastrowid,timestamp,'100','entry_window_missed'))
    store.db.commit(); unknown+=1;continue
   q=quote(store,client,run,USDC,r['token_address'],int(CONFIG['quote_notional_usdc']*1000000))
   status=q['status']; qty=q['amount_out'] if status=='quoted' else None
   pos=store.db.execute('INSERT INTO paper_positions(signal_id,entry_quote_id,entry_at,quantity_raw,notional_usdc,entry_gas_usd,entry_status) VALUES(?,?,?,?,?,?,?)',(signal,q['id'],q['observed_at'],qty,str(CONFIG['quote_notional_usdc']),q['gas_usd'],status)).lastrowid
   store.db.commit()
   if status=='quoted':
    opened+=1
    exit_quote=quote(store,client,run,r['token_address'],USDC,qty)
    save_mark(store,pos,exit_quote,0)
    save_daily(store,pos,exit_quote)
   else:unknown+=1
 except BaseException:
  store.finish(run,['interrupted']);raise
 store.finish(run,[] if unknown==0 else ['entry_quotes_unknown:'+str(unknown)])
 return {'run_id':run,'eligible_tokens':eligible_count,'opened':opened,'entry_unknown':unknown,'horizons':CONFIG['horizons_days'],'mode':'paper_quote_only_no_transactions'}


def save_daily(store,position,q):
 value=str(Decimal(q['amount_out'])/1000000-Decimal(q['gas_usd'])) if q['status']=='quoted' else None
 store.db.execute('INSERT OR IGNORE INTO daily_position_marks VALUES(?,?,?,?,?,?)',(position,q['observed_at'][:10],q['id'],q['observed_at'],q['status'],value));store.db.commit()


def save_mark(store,position,q,horizon):
 value=None
 if q['status']=='quoted' and q['gas_usd'] is not None:
  value=str(Decimal(q['amount_out'])/1000000-Decimal(q['gas_usd']))
 store.db.execute('INSERT OR IGNORE INTO paper_marks(position_id,quote_id,horizon_days,observed_at,status,net_value_usdc) VALUES(?,?,?,?,?,?)',(position,q['id'],horizon,q['observed_at'],q['status'],value)); store.db.commit()


def recover_abandoned_signals(store,run,at):
 # A crash can leave a frozen decision without a position, even across UTC days.
 cutoff=(at-timedelta(minutes=CONFIG['entry_delay_minutes'])).isoformat()
 pending=store.db.execute('SELECT s.* FROM paper_signals s LEFT JOIN paper_positions p ON p.signal_id=s.id WHERE p.id IS NULL AND s.decided_at<?',(cutoff,)).fetchall()
 for signal in pending:
  timestamp=now();raw={'reason':'entry_window_missed_after_restart'}
  from .core import Fetch
  rid=store.request(run,'local_policy','policy:entry_window',Fetch(timestamp,timestamp,None,json.dumps(raw).encode(),raw,None,1))
  cur=store.db.execute('INSERT INTO route_quotes(request_id,token_in,token_out,amount_in,status,observed_at) VALUES(?,?,?,?,?,?)',(rid,USDC,signal['token_address'],'100000000','entry_window_missed',timestamp))
  store.db.execute('INSERT INTO paper_positions(signal_id,entry_quote_id,entry_at,notional_usdc,entry_status) VALUES(?,?,?,?,?)',(signal['id'],cur.lastrowid,timestamp,'100','entry_window_missed'))
 store.db.commit()
 return len(pending)


def evaluate(store,client):
 setup(store);run=store.start({'module':'paper_evaluate'});count=0;daily=0;at=datetime.fromisoformat(now());notes=[]
 recovered=recover_abandoned_signals(store,run,at)
 rows=store.db.execute('SELECT p.*,s.token_address,s.version FROM paper_positions p JOIN paper_signals s ON s.id=p.signal_id WHERE p.entry_status=?',('quoted',)).fetchall()
 try:
  for p in rows:
   config=json.loads(store.db.execute('SELECT config FROM experiments WHERE version=?',(p['version'],)).fetchone()[0])
   age=at-datetime.fromisoformat(p['entry_at']); due=[]
   for horizon in config['horizons_days']:
    if age<timedelta(days=horizon):continue
    if store.db.execute('SELECT 1 FROM paper_marks WHERE position_id=? AND horizon_days=?',(p['id'],horizon)).fetchone():continue
    if age>timedelta(days=horizon,hours=config.get('exit_tolerance_hours',24)):
     # Never label today's quote as the price at a missed historical horizon.
     timestamp=now(); raw={'reason':'horizon_window_missed','horizon_days':horizon}
     from .core import Fetch
     rid=store.request(run,'local_policy','policy:exit_window',Fetch(timestamp,timestamp,None,json.dumps(raw).encode(),raw,None,1))
     cur=store.db.execute('INSERT INTO route_quotes(request_id,token_in,token_out,amount_in,status,observed_at) VALUES(?,?,?,?,?,?)',(rid,p['token_address'],USDC,p['quantity_raw'],'missed_window',timestamp))
     save_mark(store,p['id'],{'id':cur.lastrowid,'status':'missed_window','observed_at':timestamp,'gas_usd':None,'amount_out':None},horizon)
     notes.append(f"position:{p['id']}:horizon:{horizon}:missed_window");count+=1
    else:due.append(horizon)
   need_daily=age<timedelta(days=max(config['horizons_days'])) and not store.db.execute('SELECT 1 FROM daily_position_marks WHERE position_id=? AND day=?',(p['id'],at.date().isoformat())).fetchone()
   if due or need_daily:
    q=quote(store,client,run,p['token_address'],USDC,p['quantity_raw'])
    for h in due:save_mark(store,p['id'],q,h);count+=1
    if need_daily:save_daily(store,p['id'],q);daily+=1
 except BaseException:
  store.finish(run,['interrupted']);raise
 store.finish(run,notes)
 return {'run_id':run,'due_evaluations':count,'daily_marks':daily,'positions':len(rows),'abandoned_signals':recovered,'notes':notes}


def summary(store):
 setup(store)
 lines=['# Prospective paper experiment', '', 'Frozen config: '+json.dumps(CONFIG,sort_keys=True), '', 'All values are route quotes, never actual fills. Taxes, approvals, MEV and failed execution may differ. No wallet or funds used.', '', 'Horizon 0 compares independent entry/exit quotes against observed market state. It does not simulate the first quote changing reserves and is not an executable sequential round-trip cost. Thin pools, unsupported routes and token restrictions can make the comparison misleading. Missing exits remain unknown; they are not silently dropped.', '', '| Strategy | Selected | Entry quoted | Entry unknown |', '|---|---:|---:|---:|']
 for r in store.db.execute("SELECT s.version,s.strategy,COUNT(*),SUM(p.entry_status='quoted'),SUM(p.entry_status!='quoted') FROM paper_signals s JOIN paper_positions p ON p.signal_id=s.id GROUP BY s.version,s.strategy"):
  lines.append(f'| {r[0]}:{r[1]} | {r[2]} | {r[3]} | {r[4]} |')
 lines += ['', '| Position | Strategy | Horizon days | Status | Quote return after estimated gas |', '|---|---|---:|---|---:|']
 for r in store.db.execute('SELECT p.id,s.strategy,p.notional_usdc,p.entry_gas_usd,m.horizon_days,m.status,m.net_value_usdc FROM paper_positions p JOIN paper_signals s ON s.id=p.signal_id JOIN paper_marks m ON m.position_id=p.id ORDER BY p.id,m.horizon_days'):
  ret=str(Decimal(r['net_value_usdc'])/(Decimal(r['notional_usdc'])+Decimal(r['entry_gas_usd']))-1) if r['net_value_usdc'] is not None and r['entry_gas_usd'] is not None else 'unknown'
  lines.append(f"| {r['id']} | {r['strategy']} | {r['horizon_days']} | {r['status']} | {ret} |")
 lines+=['', '7/30/90-day conclusions require elapsed time and sufficient matched cohorts. No profitability claim is made.']
 return '\n'.join(lines)+'\n'
