import unittest
from crypto_observer.holder_index import TRANSFER,ZERO,decode_event,reconstruct,find_deployment
TOKEN='0x'+'a'*40
A='0x'+'b'*40
B='0x'+'c'*40
class HolderIndexTests(unittest.TestCase):
    def test_balances_mint_transfer_burn(self):
        balances=reconstruct([(ZERO,A,'100'),(A,B,'30'),(B,ZERO,'10')])
        self.assertEqual(balances,{A:70,B:20})
        self.assertEqual(sum(balances.values()),90)
    def test_negative_not_hidden(self):
        self.assertEqual(reconstruct([(A,B,'10')])[A],-10)
    def test_decode_and_reject_removed(self):
        item={'removed':False,'address':TOKEN,'topics':[TRANSFER,'0x'+'0'*24+'b'*40,'0x'+'0'*24+'c'*40],'data':'0x'+format(123,'064x'),'blockNumber':'0xa','blockHash':'0x'+'1'*64,'transactionHash':'0x'+'2'*64,'logIndex':'0x0'}
        self.assertEqual(decode_event(item,TOKEN,10,20)[-1],'123')
        item['removed']=True
        with self.assertRaises(ValueError):decode_event(item,TOKEN,10,20)
    def test_deployment_archive_search(self):
        class Fake:
            def call(self,method,params):return ('0x1234' if int(params[1],16)>=57 else '0x',None,'now')
        self.assertEqual(find_deployment(Fake(),TOKEN,100),(57,None))
    def test_archive_errors_not_genesis(self):
        class Fake:
            def call(self,method,params):return ('0x1234',None,'now') if params[1]!='0x0' else (None,'no archive','now')
        self.assertEqual(find_deployment(Fake(),TOKEN,100),(None,'archive_unavailable'))
