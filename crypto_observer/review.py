"""Fixed first-20 review cohort; evidence capture is separate from strategy trading."""
import json
from decimal import Decimal
from .core import now
from .paper import setup as paper_setup,quote,USDC


def cohort(store,limit=20):
    return [r[0] for r in store.db.execute('SELECT token_address FROM security_observations GROUP BY token_address ORDER BY MIN(id) LIMIT ?',(limit,))]


def setup(store):
    paper_setup(store)
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS review_quote_checks(
      id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES runs(id),
      token_address TEXT NOT NULL,checked_at TEXT NOT NULL,
      buy_quote_id INTEGER NOT NULL REFERENCES route_quotes(id),
      sell_quote_id INTEGER REFERENCES route_quotes(id),status TEXT NOT NULL,
      roundtrip_return TEXT);
    ''')


def check_quotes(store,client,limit=20):
    setup(store)
    run=store.start({'module':'review_quotes','cohort':'first_security_observed_20','limit':limit,'notional_usdc':100})
    results=[]
    try:
        for token in cohort(store,limit):
            buy=quote(store,client,run,USDC,token,100000000)
            sell=None
            net=None
            if buy['status']=='quoted':
                sell=quote(store,client,run,token,USDC,buy['amount_out'])
                if sell['status']=='quoted':
                    net=str((Decimal(sell['amount_out'])/1000000-Decimal(sell['gas_usd']))/(Decimal(100)+Decimal(buy['gas_usd']))-1)
            status='roundtrip_quoted' if net is not None else 'exit_unknown' if buy['status']=='quoted' else 'entry_unknown'
            at=sell['observed_at'] if sell else buy['observed_at']
            store.db.execute('INSERT INTO review_quote_checks(run_id,token_address,checked_at,buy_quote_id,sell_quote_id,status,roundtrip_return) VALUES(?,?,?,?,?,?,?)',(run,token,at,buy['id'],sell['id'] if sell else None,status,net))
            store.db.commit()
            results.append({'token':token,'status':status,'roundtrip_return':net})
    except BaseException:
        store.finish(run,['interrupted']);raise
    notes=['unknown_quotes_present'] if any(r['status']!='roundtrip_quoted' for r in results) else []
    store.finish(run,notes)
    return {'run_id':run,'count':len(results),'results':results,'notes':notes}


def evidence(store,limit=20):
    setup(store)
    metadata={}
    for request in store.db.execute("SELECT id,observed_at,body FROM requests WHERE source='dexscreener' ORDER BY id"):
        try:payload=json.loads(request['body'])
        except ValueError:continue
        for pool in payload.get('pairs') or []:
            token=(pool.get('baseToken') or {}).get('address','').lower()
            if token:
                metadata[token]={'request_id':request['id'],'observed_at':request['observed_at'],'name':str((pool.get('baseToken') or {}).get('name',''))[:200],'symbol':str((pool.get('baseToken') or {}).get('symbol',''))[:80],'websites':(pool.get('info') or {}).get('websites',[]),'socials':(pool.get('info') or {}).get('socials',[])}
    records=[]
    for token in cohort(store,limit):
        def latest(table):
            row=store.db.execute('SELECT * FROM '+table+' WHERE token_address=? ORDER BY id DESC LIMIT 1',(token,)).fetchone()
            return dict(row) if row else None
        pool=store.db.execute('''SELECT s.* FROM snapshots s WHERE s.token_address=?
          AND s.id=(SELECT MAX(s2.id) FROM snapshots s2 WHERE s2.pool_address=s.pool_address)
          ORDER BY CAST(s.liquidity_usd AS REAL) DESC LIMIT 1''',(token,)).fetchone()
        record={'token':token,'metadata':metadata.get(token,{}),'pool_snapshot':dict(pool) if pool else None}
        for table in ('security_observations','onchain_observations','indexed_holder_snapshots','review_quote_checks'):
            record[table]=latest(table)
        for table,key in [('security_observations','assessment'),('onchain_observations','assessment'),('indexed_holder_snapshots','top_holders'),('indexed_holder_snapshots','balance_checks')]:
            if record[table]:record[table][key]=json.loads(record[table][key])
        records.append(record)
    return {'captured_at':now(),'selection':'first 20 distinct tokens observed by security module, kept regardless of outcome','records':records}
