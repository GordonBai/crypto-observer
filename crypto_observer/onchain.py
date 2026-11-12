"""Read-only, block-pinned Base probes. No transaction methods supported."""
import hashlib
import json
from .core import address

SLOTS={
 "implementation":"0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
 "admin":"0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
 "beacon":"0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"}
ALLOWED={"eth_chainId","eth_blockNumber","eth_getBlockByNumber","eth_getCode","eth_getStorageAt","eth_call","eth_getLogs"}


def setup(store):
 store.db.executescript("""
 CREATE TABLE IF NOT EXISTS rpc_inputs(request_id INTEGER PRIMARY KEY REFERENCES requests(id),payload TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS onchain_observations(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES runs(id),token_address TEXT NOT NULL,observed_at TEXT NOT NULL,block_number TEXT,assessment TEXT NOT NULL);
 """)


def word_address(word):
 if not isinstance(word,str) or len(word)!=66 or not word.startswith('0x'): return None
 try: n=int(word,16)
 except ValueError: return None
 if n>=2**160: return None
 return '0x'+word[-40:].lower()


def delegatecall(code):
 # Parse opcodes; bytes inside PUSH data are not instructions.
 if not isinstance(code,str) or not code.startswith('0x'): return None
 try: b=bytes.fromhex(code[2:])
 except ValueError: return None
 i=0
 while i<len(b):
  op=b[i]; i+=1
  if op==0xf4: return True
  if 0x60<=op<=0x7f: i+=op-0x5f
 return False


class RPC:
 def __init__(self,store,client,run,url='https://mainnet.base.org'):
  self.store,self.client,self.run,self.url=store,client,run,url
 def call(self,method,params):
  if method not in ALLOWED: raise ValueError('only read-only RPC supported')
  payload={'jsonrpc':'2.0','id':1,'method':method,'params':params}
  r=self.client.get(self.url,2.1,body=payload)
  # Store a public label, not an RPC credential-bearing URL.
  rid=self.store.request(self.run,'base_rpc','base_rpc:'+method,r)
  self.store.db.execute('INSERT INTO rpc_inputs VALUES(?,?)',(rid,json.dumps(payload)))
  self.store.db.commit()
  if r.error: return None,r.error,r.observed_at
  p=r.payload
  if not isinstance(p,dict) or p.get('id')!=1 or 'result' not in p:
   return None,'rpc_error:'+str(p.get('error') if isinstance(p,dict) else 'invalid_schema'),r.observed_at
  return p['result'],None,r.observed_at


def probe(store,client,limit=3,url='https://mainnet.base.org'):
 setup(store)
 run=store.start({'module':'onchain','limit':limit,'chain_id':8453})
 rpc=RPC(store,client,run,url); notes=[]; n=0
 try:
  chain,err,_=rpc.call('eth_chainId',[])
  if err or chain!='0x2105':
   notes.append('chain_not_verified:'+str(err or chain)); store.finish(run,notes)
   return {'run_id':run,'observed':0,'notes':notes}
  block,err,_=rpc.call('eth_blockNumber',[])
  if err or not isinstance(block,str) or not block.startswith('0x'):
   notes.append('head_unavailable'); store.finish(run,notes)
   return {'run_id':run,'observed':0,'notes':notes}
  # Use a confirmed block behind the current head; finality still not guaranteed.
  block=hex(max(int(block,16)-20,0))
  header,herr,_=rpc.call('eth_getBlockByNumber',[block,False])
  tokens=[r[0] for r in store.db.execute("""SELECT p.token_address FROM pools p LEFT JOIN onchain_observations o ON o.token_address=p.token_address
    WHERE p.token_address IS NOT NULL GROUP BY p.token_address ORDER BY COALESCE(MAX(o.observed_at),''),p.token_address LIMIT ?""",(limit,))]
  for token in tokens:
   evidence={'block_hash':header.get('hash') if isinstance(header,dict) else None,'block_error':herr,'finality':'head_minus_20_not_finalized','mint_freeze_blacklist':'unknown_without_source_and_role_analysis'}
   code,cerr,at=rpc.call('eth_getCode',[token,block])
   evidence['code_error']=cerr
   evidence['has_code']=bool(code and code!='0x') if not cerr else None
   evidence['code_sha256']=hashlib.sha256(code.encode()).hexdigest() if isinstance(code,str) else None
   evidence['delegatecall_opcode']=delegatecall(code)
   for label,slot in SLOTS.items():
    word,error,at=rpc.call('eth_getStorageAt',[token,slot,block])
    evidence[label]=word_address(word); evidence[label+'_error']=error
   owner,error,at=rpc.call('eth_call',[{'to':token,'data':'0x8da5cb5b'},block])
   evidence['owner']=word_address(owner); evidence['owner_error']=error
   evidence['conclusion']='unverified'
   evidence['limitations']=['standard_slots_only','zero_owner_does_not_exclude_other_roles','proxy_slot_does_not_prove_upgrade_authority','provider_block_may_reorg']
   store.db.execute('INSERT INTO onchain_observations(run_id,token_address,observed_at,block_number,assessment) VALUES(?,?,?,?,?)',(run,token,at,block,json.dumps(evidence)))
   store.db.commit(); n+=1
 except BaseException:
  notes.append('interrupted'); store.finish(run,notes); raise
 store.finish(run,notes)
 return {'run_id':run,'observed':n,'block_number':block,'notes':notes}
