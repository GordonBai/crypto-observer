import unittest
from crypto_observer.onchain import delegatecall, word_address, RPC
class OnchainTests(unittest.TestCase):
 def test_skip_push(self):
  self.assertFalse(delegatecall('0x60f400'))
  self.assertTrue(delegatecall('0x60f4f4'))
  self.assertIsNone(delegatecall('0xzz'))
 def test_word(self):
  self.assertEqual(word_address('0x'+'0'*24+'a'*40),'0x'+'a'*40)
  self.assertIsNone(word_address('0x'))
  self.assertIsNone(word_address('0x'+'f'*64))
 def test_readonly(self):
  with self.assertRaises(ValueError): RPC(None,None,None).call('eth_sendRawTransaction',[])
