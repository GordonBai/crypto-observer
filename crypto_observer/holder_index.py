"""Bounded ERC-20 Transfer indexing with explicit coverage and supply reconciliation.

Balances are event-derived, not proof that a nonstandard token emits honest events.
All RPC data remains in requests/rpc_inputs. There is no paid provider dependency.
"""
import json
from collections import defaultdict
from decimal import Decimal
from .core import now, address
from .onchain import RPC, setup as rpc_setup

TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZERO = '0x' + '0' * 40
KNOWN_INFRA={ZERO:'zero_address','0x000000000000000000000000000000000000dead':'conventional_burn_address','0x498581ff718922c3f8e6a244956af099b2652b2b':'Uniswap_v4_Base_PoolManager'}
# PoolManager source: https://developers.uniswap.org/deployments (verified 2026-10-24).


def setup(store):
    rpc_setup(store)
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS transfer_index_state(
      token_address TEXT PRIMARY KEY, deployment_block INTEGER, cursor_block INTEGER,
      cursor_hash TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS transfer_events(
      token_address TEXT NOT NULL, block_number INTEGER NOT NULL, block_hash TEXT NOT NULL,
      tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL, sender TEXT NOT NULL,
      recipient TEXT NOT NULL, amount_raw TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(id),
      PRIMARY KEY(token_address,block_hash,tx_hash,log_index));
    CREATE TABLE IF NOT EXISTS indexed_holder_snapshots(
      id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), token_address TEXT NOT NULL,
      observed_at TEXT NOT NULL, block_number INTEGER NOT NULL, block_hash TEXT,
      status TEXT NOT NULL, holder_count INTEGER, total_supply_raw TEXT,
      event_supply_raw TEXT, top_holders TEXT NOT NULL, limitations TEXT NOT NULL);
    ''')
    columns={r[1] for r in store.db.execute('PRAGMA table_info(indexed_holder_snapshots)')}
    if 'caught_up' not in columns:
        store.db.execute('ALTER TABLE indexed_holder_snapshots ADD COLUMN caught_up INTEGER NOT NULL DEFAULT 0')
        store.db.commit()
    if 'balance_checks' not in columns:
        store.db.execute("ALTER TABLE indexed_holder_snapshots ADD COLUMN balance_checks TEXT NOT NULL DEFAULT '[]'")
        store.db.commit()


def decode_event(item, token, first, last):
    if not isinstance(item, dict) or item.get('removed') is not False:
        raise ValueError('removed_or_unverified_log')
    topics = item.get('topics')
    if not isinstance(topics, list) or len(topics) != 3 or topics[0].lower() != TRANSFER:
        raise ValueError('not_standard_erc20_transfer')
    if str(item.get('address', '')).lower() != token:
        raise ValueError('wrong_token')
    participants = []
    for topic in topics[1:]:
        if len(topic) != 66 or int(topic[2:26], 16) != 0:
            raise ValueError('malformed_address_topic')
        participants.append('0x' + topic[-40:].lower())
    amount = item.get('data', '')
    if len(amount) != 66:
        raise ValueError('malformed_uint256')
    block = int(item['blockNumber'], 16)
    if not first <= block <= last:
        raise ValueError('out_of_range_log')
    block_hash, tx = item['blockHash'], item['transactionHash']
    if len(block_hash) != 66 or len(tx) != 66:
        raise ValueError('missing_log_identity')
    return (token, block, block_hash, tx, int(item['logIndex'], 16), *participants, str(int(amount, 16)))


def reconstruct(events):
    balances = defaultdict(int)
    for sender, recipient, amount in events:
        n = int(amount)
        if sender != ZERO:
            balances[sender] -= n
        if recipient != ZERO:
            balances[recipient] += n
    return dict(balances)


def find_deployment(rpc, token, head):
    """Infer first nonempty code via archive reads; redeployments remain a caveat."""
    code, err, _ = rpc.call('eth_getCode', [token, hex(head)])
    if err or code in (None, '0x', '0x0'):
        return None, 'code_unavailable'
    genesis, err, _ = rpc.call('eth_getCode', [token, '0x0'])
    if err:
        return None, 'archive_unavailable'
    if genesis not in ('0x', '0x0'):
        return 0, None
    low, high = 0, head
    while low + 1 < high:
        mid = (low + high) // 2
        code, err, _ = rpc.call('eth_getCode', [token, hex(mid)])
        if err:
            return None, 'archive_unavailable'
        if code in ('0x', '0x0'):
            low = mid
        else:
            high = mid
    return high, None


def index(store, client, limit=1, chunks=4, span=500, url='https://mainnet.base.org', token=None, refresh_existing=False):
    setup(store)
    run = store.start({'module': 'holder_index', 'limit': limit, 'chunks': chunks, 'span': span})
    rpc = RPC(store, client, run, url)
    log_rpc = RPC(store, client, run, 'https://base-rpc.publicnode.com')
    notes, results = [], []
    try:
        chain, err, _ = rpc.call('eth_chainId', [])
        if err or chain != '0x2105':
            raise ValueError('base_chain_not_verified')
        log_chain, log_error, _ = log_rpc.call('eth_chainId', [])
        if log_error or log_chain != '0x2105':
            raise ValueError('log_provider_chain_not_verified')
        head, err, _ = rpc.call('eth_blockNumber', [])
        if err:
            raise ValueError('head_unavailable')
        head = max(0, int(head, 16) - 20)
        tokens = store.db.execute('''SELECT p.token_address FROM pools p
          LEFT JOIN transfer_index_state i ON i.token_address=p.token_address
          WHERE p.token_address IS NOT NULL GROUP BY p.token_address
          ORDER BY COALESCE(MAX(i.updated_at),''),p.token_address LIMIT ?''', (limit,)).fetchall()
        if refresh_existing:
            known=store.db.execute("SELECT token_address FROM transfer_index_state WHERE deployment_block IS NOT NULL ORDER BY updated_at,token_address LIMIT ?",(limit,)).fetchall()
            if known:tokens=known
        if token is not None:
            if address(token)!=token or len(token)!=42 or not store.db.execute('SELECT 1 FROM pools WHERE token_address=?',(token,)).fetchone():
                raise ValueError('token must be a discovered lowercase Base token address')
            tokens=[(token,)]
        for row in tokens:
            token = row[0]
            state = store.db.execute('SELECT * FROM transfer_index_state WHERE token_address=?', (token,)).fetchone()
            if state is None or state['deployment_block'] is None:
                deployment, error = find_deployment(rpc, token, head)
                if error:
                    store.db.execute('INSERT OR REPLACE INTO transfer_index_state VALUES(?,NULL,NULL,NULL,?,?)', (token,error,now()))
                    store.db.commit()
                    notes.append(f'{token}:{error}')
                    continue
                cursor, cursor_hash = deployment - 1, None
            else:
                deployment, cursor, cursor_hash = state['deployment_block'], state['cursor_block'], state['cursor_hash']
            if cursor_hash:
                block, error, _ = rpc.call('eth_getBlockByNumber', [hex(cursor), False])
                if error or not isinstance(block, dict) or block.get('hash') != cursor_hash:
                    notes.append(f'{token}:checkpoint_hash_unverified_or_reorg')
                    continue
            failed = False
            for _ in range(chunks):
                first, last = cursor + 1, min(cursor + span, head)
                if first > last:
                    break
                logs, error, _ = log_rpc.call('eth_getLogs', [{'address':token, 'fromBlock':hex(first), 'toBlock':hex(last), 'topics':[TRANSFER]}])
                if error or not isinstance(logs, list):
                    notes.append(f'{token}:logs_unavailable:{error}')
                    failed = True
                    break
                try:
                    decoded = [decode_event(item, token, first, last) for item in logs]
                except (ValueError, KeyError, TypeError) as exc:
                    notes.append(f'{token}:invalid_logs:{type(exc).__name__}')
                    failed = True
                    break
                header, error, _ = rpc.call('eth_getBlockByNumber', [hex(last), False])
                if error or not isinstance(header,dict) or not header.get('hash'):
                    notes.append(f'{token}:checkpoint_unavailable')
                    failed = True
                    break
                other_header, other_error, _ = log_rpc.call('eth_getBlockByNumber', [hex(last), False])
                if other_error or not isinstance(other_header,dict) or other_header.get('hash')!=header['hash']:
                    notes.append(f'{token}:provider_checkpoint_hash_mismatch')
                    failed=True
                    break
                cursor, cursor_hash = last, header['hash']
                with store.db:
                    store.db.executemany('INSERT OR IGNORE INTO transfer_events VALUES(?,?,?,?,?,?,?,?,?)', [(*item,run) for item in decoded])
                    store.db.execute('INSERT OR REPLACE INTO transfer_index_state VALUES(?,?,?,?,?,?)', (token,deployment,cursor,cursor_hash,'caught_up' if cursor==head else 'catching_up',now()))
            if cursor < deployment:
                # Preserve the inferred origin even when the first log request fails.
                store.db.execute('INSERT OR REPLACE INTO transfer_index_state VALUES(?,?,?,?,?,?)', (token,deployment,cursor,cursor_hash,'logs_unavailable',now()))
                store.db.commit()
                continue
            events = store.db.execute('SELECT sender,recipient,amount_raw FROM transfer_events WHERE token_address=? ORDER BY block_number,log_index', (token,)).fetchall()
            balances = reconstruct(events)
            supply_word, error, observed_at = rpc.call('eth_call', [{'to':token,'data':'0x18160ddd'},hex(cursor)])
            try:
                supply = int(supply_word,16) if not error and len(supply_word)==66 else None
            except (ValueError, TypeError):
                supply = None
            event_supply = sum(balances.values())
            nonnegative = all(value>=0 for value in balances.values())
            reconciled = supply is not None and supply==event_supply and nonnegative
            status = 'event_supply_reconciled' if reconciled else 'unreconciled_unknown'
            top = sorted(((a,n) for a,n in balances.items() if n>0), key=lambda x:(-x[1],x[0]))[:20]
            data = [{'address':a,'balance_raw':str(n),'fraction':str(Decimal(n)/supply) if supply else None} for a,n in top]
            checks=[]
            for holder,expected in top[:3]:
                word,check_error,_=rpc.call('eth_call',[{'to':token,'data':'0x70a08231'+holder[2:].rjust(64,'0')},hex(cursor)])
                try:
                    actual=int(word,16) if not check_error and len(word)==66 else None
                except (ValueError,TypeError):
                    actual=None
                checks.append({'address':holder,'expected_raw':str(expected),'actual_raw':str(actual) if actual is not None else None,'error':check_error})
                if actual is not None and actual!=expected:
                    reconciled=False
                    status='balance_mismatch_unknown'
            # Unknown individual checks do not erase the aggregate evidence, but remain explicit.

            limits = ['only_top_three_balanceOf_spot_checks_not_full_balance_verification','rebase_or_dishonest_events_possible','first_code_assumes_no_redeployment','LPs_and_contracts_not_excluded','head_minus_20_not_finalized']
            store.db.execute('INSERT INTO indexed_holder_snapshots(run_id,token_address,observed_at,block_number,block_hash,status,holder_count,total_supply_raw,event_supply_raw,top_holders,limitations) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (run,token,observed_at,cursor,cursor_hash,status,sum(n>0 for n in balances.values()) if reconciled else None,str(supply) if supply is not None else None,str(event_supply),json.dumps(data),json.dumps(limits)))
            store.db.execute('UPDATE indexed_holder_snapshots SET caught_up=?,balance_checks=? WHERE id=last_insert_rowid()', (int(cursor==head),json.dumps(checks)))
            store.db.commit()
            results.append({'token':token,'through_block':cursor,'head':head,'caught_up':cursor==head,'status':status,'holder_count':sum(n>0 for n in balances.values()) if reconciled else None,'events':len(events),'request_failed':failed})
    except BaseException:
        store.finish(run, notes + ['interrupted'])
        raise
    store.finish(run,notes)
    return {'run_id':run,'results':results,'notes':notes}


def report(store):
    setup(store)
    lines=['# Holder history and address associations', '', f'Generated UTC: {now()}', '', 'Event-derived balances are accepted only when the aggregate reconciles with onchain totalSupply and no derived balance is negative. This is not proof that every individual balance is correct.', '', '| Token | Through block | Status | Derived holders | Indexed events |', '|---|---:|---|---:|---:|']
    latest=store.db.execute('''SELECT h.* FROM indexed_holder_snapshots h WHERE h.id=(SELECT MAX(h2.id) FROM indexed_holder_snapshots h2 WHERE h2.token_address=h.token_address) ORDER BY h.token_address''').fetchall()
    shared=defaultdict(set)
    for r in latest:
        count=store.db.execute('SELECT COUNT(*) FROM transfer_events WHERE token_address=?',(r['token_address'],)).fetchone()[0]
        lines.append(f"| {r['token_address']} | {r['block_number']} | {r['status']} | {r['holder_count']} | {count} |")
        if r['status']=='event_supply_reconciled':
            for h in json.loads(r['top_holders']):
                shared[h['address']].add(r['token_address'])
    lines+=['', '## Shared top-holder addresses', '', 'Address overlap is a factual association, not evidence of common beneficial ownership or manipulation. LPs, routers, exchanges and treasury addresses require manual classification.']
    found=False
    for a,tokens in sorted(shared.items()):
        if len(tokens)>1:
            if a in KNOWN_INFRA:
                lines.append(f'- Shared infrastructure (excluded from owner-link hypotheses): {a} / {KNOWN_INFRA[a]}.')
                continue
            found=True; lines.append(f'- Unclassified overlap {a}: '+', '.join(sorted(tokens)))
    if not found: lines.append('No unclassified shared top-holder address found in currently reconciled samples.')
    lines+=['', '## Source selection and cost', '', 'GoPlus free API provided risk fields but returned empty/zero holder metadata for sampled new tokens, and rate-limited requests. Blockscout public holder route encountered an access challenge; paid Base API requires credentials. Neither is assumed to supply complete holders.', 'Primary research source: bounded public RPC Transfer indexing, preserving a deployment-to-checkpoint coverage cursor and full raw responses. No subscription purchased. Runtime and archive availability are practical costs.', 'Historical snapshots become known at observed_at; old block state is never presented as information available to the observer before collection. No complete owner identity or shared-funder inference is claimed.']
    return '\n'.join(lines)+'\n'
