import unittest
from unittest.mock import patch
from crypto_observer.core import Store,collect
from crypto_observer.holder_index import index,TRANSFER,ZERO
from test_observer import Fake,pool,TOKEN

A='0x'+'1'*40
B='0x'+'2'*40

class FakeRPC:
    head=50
    removed=False
    mismatch=False
    reorg=False
    def __init__(self,store,client,run,url):self.url=url
    def call(self,method,params):
        at='2026-10-24T12:01:00+00:00'
        if method=='eth_chainId':return '0x2105',None,at
        if method=='eth_blockNumber':return hex(self.head),None,at
        if method=='eth_getCode':return ('0x6000' if int(params[1],16)>=10 else '0x'),None,at
        if method=='eth_getBlockByNumber':
            n=int(params[0],16)
            return {'hash':'0x'+format(n+(1000 if self.reorg else 0),'064x')},None,at
        if method=='eth_getLogs':
            query=params[0];first=int(query['fromBlock'],16);last=int(query['toBlock'],16)
            items=[]
            for block,sender,recipient,n in [(10,ZERO,A,100),(20,A,B,30)]:
                if first<=block<=last:
                    items.append({'address':TOKEN,'removed':self.removed,'blockNumber':hex(block),'blockHash':'0x'+format(block,'064x'),'transactionHash':'0x'+format(block+100,'064x'),'logIndex':'0x0','topics':[TRANSFER,'0x'+sender[2:].rjust(64,'0'),'0x'+recipient[2:].rjust(64,'0')],'data':'0x'+format(n,'064x')})
            return items,None,at
        if method=='eth_call':
            data=params[0]['data']
            n=100 if data=='0x18160ddd' else (71 if self.mismatch else 70) if data.endswith(A[2:]) else 30
            return '0x'+format(n,'064x'),None,at
        raise AssertionError(method)

class HolderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:')
        collect(self.s,Fake([{'data':[pool()]},{'pairs':[]}]),1,20)
        FakeRPC.head=50;FakeRPC.removed=False;FakeRPC.mismatch=False;FakeRPC.reorg=False
    def tearDown(self):self.s.close()
    def test_index_reconciles_and_resumes_without_duplicate_events(self):
        with patch('crypto_observer.holder_index.RPC',FakeRPC):
            one=index(self.s,None,token=TOKEN)
            FakeRPC.head=60
            two=index(self.s,None,token=TOKEN)
        self.assertEqual(one['results'][0]['holder_count'],2)
        self.assertEqual(two['results'][0]['through_block'],40)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM transfer_events').fetchone()[0],2)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM indexed_holder_snapshots').fetchone()[0],2)
    def test_balance_mismatch_keeps_holder_count_unknown(self):
        FakeRPC.mismatch=True
        with patch('crypto_observer.holder_index.RPC',FakeRPC):result=index(self.s,None,token=TOKEN)
        self.assertIsNone(result['results'][0]['holder_count'])
        self.assertEqual(result['results'][0]['status'],'balance_mismatch_unknown')
    def test_removed_event_does_not_advance_checkpoint(self):
        FakeRPC.removed=True
        with patch('crypto_observer.holder_index.RPC',FakeRPC):result=index(self.s,None,token=TOKEN)
        self.assertTrue(result['notes'])
        self.assertEqual(self.s.db.execute('SELECT cursor_block FROM transfer_index_state').fetchone()[0],9)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM transfer_events').fetchone()[0],0)
    def test_reorg_does_not_reuse_old_state(self):
        with patch('crypto_observer.holder_index.RPC',FakeRPC):
            index(self.s,None,token=TOKEN)
            FakeRPC.reorg=True
            result=index(self.s,None,token=TOKEN)
        self.assertTrue(any('reorg' in x for x in result['notes']))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM indexed_holder_snapshots').fetchone()[0],1)
