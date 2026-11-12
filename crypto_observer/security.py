"""Provider evidence, never a declaration that a token is safe."""
import json
from decimal import Decimal
from .core import address, number, now

RISK_FIELDS = ("is_proxy", "is_mintable", "can_take_back_ownership", "owner_change_balance", "hidden_owner", "transfer_pausable", "is_blacklisted", "slippage_modifiable", "is_honeypot", "cannot_sell_all")


def setup(store):
    store.db.executescript("""
    CREATE TABLE IF NOT EXISTS security_observations(
      id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
      token_address TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL,
      assessment TEXT NOT NULL, UNIQUE(request_id,token_address));
    CREATE TABLE IF NOT EXISTS holder_observations(
      id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
      token_address TEXT NOT NULL, observed_at TEXT NOT NULL,
      holder_count INTEGER, top10_raw_fraction TEXT, listed_holders TEXT NOT NULL,
      coverage TEXT NOT NULL, UNIQUE(request_id,token_address));
    """)


def assess(data):
    states = {k: "reported_present" if data.get(k)=="1" else "reported_absent" if data.get(k)=="0" else "unknown" for k in RISK_FIELDS}
    flags = [k for k,v in states.items() if v=="reported_present"]
    taxes = {k:number(data.get(k)) for k in ("buy_tax", "sell_tax")}
    for k,v in taxes.items():
        if v is not None and Decimal(v)>1: taxes[k] = None
    return {"version":"1", "risk_fields":states, "flags":flags, "owner_address":data.get("owner_address"), "taxes":taxes, "source_open":data.get("is_open_source"), "conclusion":"provider_flags_present" if flags else "unverified", "limitations":["provider_report_not_audit", "upgrade_control_not_independently_verified", "role_based_permissions_unknown", "live_sellability_unknown"]}


def holders(data):
    count = number(data.get("holder_count"))
    count = int(Decimal(count)) if count is not None and Decimal(count)==Decimal(count).to_integral_value() and Decimal(count)<=9223372036854775807 else None
    entries, fractions = [], []
    raw = data.get("holders")
    complete = isinstance(raw,list) and len(raw)>0
    for entry in raw if isinstance(raw,list) else []:
        if not isinstance(entry,dict): complete=False; continue
        a, fraction = address(entry.get("address")), number(entry.get("percent"))
        valid = a is not None and len(a)==42 and fraction is not None and Decimal(fraction)<=1
        if not valid: complete=False
        entries.append({"address":a,"fraction":fraction if valid else None,"tag":entry.get("tag"),"is_contract":entry.get("is_contract"),"is_locked":entry.get("is_locked")})
        if valid: fractions.append(Decimal(fraction))
    total = sum(fractions,Decimal(0))
    top = str(total) if complete and total<=1 else None
    # Provider only returns a top-holder list: no full ownership reconstruction.
    return count, top, entries


def scan(store, client, limit=20):
    setup(store)
    tokens = [r[0] for r in store.db.execute("SELECT p.token_address FROM pools p LEFT JOIN security_observations s ON s.token_address=p.token_address WHERE p.token_address IS NOT NULL GROUP BY p.token_address ORDER BY COALESCE(MAX(s.observed_at),''),p.token_address LIMIT ?",(limit,))]
    run = store.start({"module":"security","provider":"goplus","limit":limit})
    notes=[]; observed=0
    try:
        for token in tokens:
            url = "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses="+token
            r = client.get(url,6.1)
            rid = store.request(run,"goplus",url,r)
            payload = r.payload if isinstance(r.payload,dict) else {}
            results = payload.get("result") if isinstance(payload.get("result"),dict) else {}
            data = results.get(token)
            status = "observed" if not r.error and payload.get("code")==1 and isinstance(data,dict) and data else "unknown"
            assessment = assess(data if status=="observed" else {})
            if status=="unknown": notes.append(f"{token}:provider_unavailable:{r.error or payload.get('code')}")
            store.db.execute("INSERT INTO security_observations(request_id,token_address,observed_at,status,assessment) VALUES(?,?,?,?,?)",(rid,token,r.observed_at,status,json.dumps(assessment)))
            count,top,entries = holders(data if status=="observed" else {})
            store.db.execute("INSERT INTO holder_observations(request_id,token_address,observed_at,holder_count,top10_raw_fraction,listed_holders,coverage) VALUES(?,?,?,?,?,?,?)",(rid,token,r.observed_at,count,top,json.dumps(entries),"provider_top_list_only;LP_not_excluded;related_owners_unknown"))
            store.db.commit(); observed += status=="observed"
            if payload.get("code")==4029:
                notes.append("rate_limit_stop;remaining_tokens_not_attempted")
                break
    except BaseException:
        notes.append("interrupted"); store.finish(run,notes); raise
    store.finish(run,notes)
    return {"run_id":run,"tokens":len(tokens),"observed":observed,"notes":notes}


def cases(store,limit=20):
    setup(store)
    rows=store.db.execute("""SELECT s.*,h.holder_count,h.top10_raw_fraction,h.coverage FROM security_observations s
      LEFT JOIN holder_observations h ON h.request_id=s.request_id AND h.token_address=s.token_address
      WHERE s.id=(SELECT MAX(s2.id) FROM security_observations s2 WHERE s2.token_address=s.token_address)
      ORDER BY s.token_address LIMIT ?""",(limit,)).fetchall()
    lines=["# Token research case cards", "", f"Generated UTC: {now()}", "", "Provider evidence only; no safety clearance. LP holdings are not excluded. Address clusters and actual sellability are unknown.", ""]
    for i,r in enumerate(rows,1):
        a=json.loads(r['assessment'])
        prior=store.db.execute("SELECT holder_count,observed_at FROM holder_observations WHERE token_address=? AND holder_count IS NOT NULL ORDER BY id DESC LIMIT 2",(r['token_address'],)).fetchall()
        delta = prior[0][0]-prior[1][0] if len(prior)==2 else None
        onchain=None
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='onchain_observations'").fetchone():
            record=store.db.execute('SELECT block_number,observed_at,assessment FROM onchain_observations WHERE token_address=? ORDER BY id DESC LIMIT 1',(r['token_address'],)).fetchone()
            if record:onchain={'block':record['block_number'],'observed_at':record['observed_at'],'evidence':json.loads(record['assessment'])}
        indexed=None
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='indexed_holder_snapshots'").fetchone():
            record=store.db.execute('SELECT block_number,observed_at,status,holder_count FROM indexed_holder_snapshots WHERE token_address=? ORDER BY id DESC LIMIT 1',(r['token_address'],)).fetchone()
            if record:indexed=dict(record)
        lines += [f"### Independent evidence for {r['token_address']}", '- Onchain probe: '+json.dumps(onchain), '- Event-index holder state: '+json.dumps(indexed)]

        lines += [f"## {i}. {r['token_address']}", f"- Observed: {r['observed_at']}; request_id: {r['request_id']}; provider status: {r['status']}", f"- Provider risk flags: {', '.join(a['flags']) or 'none reported; not proven safe'}", f"- Permission states: {json.dumps(a['risk_fields'])}", f"- Owner reported: {a['owner_address']!r}; taxes: {json.dumps(a['taxes'])}", f"- Holder count: {r['holder_count']}; change between last two available observations: {delta}", f"- Raw listed top-holder fraction: {r['top10_raw_fraction']}; coverage: {r['coverage']}", "- Required next checks: verify admin/implementation onchain; classify LP/vesting/treasury; inspect related funding and product demand; obtain amount-specific executable quotes.", ""]
    return "\n".join(lines)+"\n"
