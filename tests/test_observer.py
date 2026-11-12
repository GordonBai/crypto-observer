import io
import json
import unittest
from urllib.error import HTTPError
from crypto_observer.core import Client, Fetch, Store, collect, normalize, number, report

POOL = "0x" + "a"*40
TOKEN = "0x" + "b"*40

def pool():
    return {"attributes": {"address": POOL, "reserve_in_usd": "100000", "fdv_usd": "200000", "market_cap_usd": None, "transactions": {"h24": {"buys": 5, "sells": 0}}}, "relationships": {"base_token": {"data": {"id": "base_"+TOKEN}}}}

class Fake:
    def __init__(self, values): self.values = iter(values)
    def get(self, url, spacing):
        value = next(self.values)
        return Fetch("2026-10-24T12:00:00Z", "2026-10-24T12:00:01Z", 200, json.dumps(value).encode(), value, None, 1)

class Tests(unittest.TestCase):
    def setUp(self): self.s = Store(":memory:")
    def tearDown(self): self.s.close()
    def test_unknowns(self):
        p = normalize(pool(), "geckoterminal")
        self.assertIsNone(p["market_cap_usd"])
        self.assertEqual(p["fdv_usd"], "200000")
        self.assertEqual(p["token_address"], TOKEN)
        self.assertIn("sellability_unknown", p["flags"])
        self.assertIn("no_observed_sells_not_proof_of_honeypot", p["flags"])
    def test_invalid_numbers(self):
        for x in (None, True, "NaN", "Infinity", -1, "bad"): self.assertIsNone(number(x))
        self.assertEqual(number(0), "0")
    def test_invalid_address(self):
        p = pool(); p["attributes"]["address"] = "fake"
        with self.assertRaises(ValueError): normalize(p, "geckoterminal")
    def test_v4_pool_id(self):
        p = pool(); p["attributes"]["address"] = "0x"+"c"*64
        self.assertEqual(len(normalize(p, "geckoterminal")["address"]), 66)
    def test_dedup_and_history(self):
        for _ in range(2): collect(self.s, Fake([{"data": [pool(),pool()]}, {"pairs": []}]), 1, 100)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM pools").fetchone()[0], 1)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 4)
    def test_missing_refresh_retained(self):
        collect(self.s, Fake([{"data": [pool()]}, {"pairs": None}]), 1, 100)
        self.assertEqual(self.s.db.execute("SELECT last_refresh_status FROM pools").fetchone()[0], "source_missing")
        self.assertIn("Absence is not evidence", report(self.s))
    def test_schema_error_recorded(self):
        result = collect(self.s, Fake([{"unexpected": 1}]), 1, 100)
        self.assertIn("geckoterminal:unexpected_schema", result["notes"])
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
    def test_page_cap(self):
        r = collect(self.s, Fake([{"data": [pool()]*20}, {"pairs": []}]), 1, 100)
        self.assertIn("discovery_page_cap_reached:coverage_incomplete", r["notes"])
    def test_empty_report(self): self.assertIn("Pools: 0", report(self.s))
    def test_successful_refresh(self):
        d = {"chainId": "base", "pairAddress": POOL, "baseToken": {"address": TOKEN}, "priceUsd": "0.01", "liquidity": {"usd": 90000}}
        collect(self.s, Fake([{"data": [pool()]}, {"pairs": [d]}]), 1, 100)
        self.assertEqual(self.s.db.execute("SELECT last_refresh_status FROM pools").fetchone()[0], "observed")
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)
    def test_http_error_body(self):
        def opener(request, timeout): raise HTTPError(request.full_url,404,"missing",{},io.BytesIO(b"missing"))
        r = Client(opener=opener, sleep=lambda _:None).get("https://example.com/api")
        self.assertEqual((r.error,r.attempts,r.body), ("http_404",1,b"missing"))
    def test_rate_limit_retry(self):
        calls, sleeps = [], []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"data": []}'
        def opener(request,timeout):
            calls.append(1)
            if len(calls)==1: raise HTTPError(request.full_url,429,"slow",{"Retry-After":"3"},io.BytesIO(b"limited"))
            return Response()
        r = Client(opener=opener,sleep=sleeps.append).get("https://example.com/api")
        self.assertEqual(r.attempts,2)
        self.assertEqual([h["status"] for h in r.history],[429,200])
        run=self.s.start({})
        self.s.request(run,"test","https://example.com/api",r)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM request_attempts").fetchone()[0],2)
        self.assertIsNone(r.error)
        self.assertIn(3,sleeps)

    def test_untrusted_counter_overflow_becomes_unknown(self):
        item=pool()
        item['attributes']['transactions']['h24']['buys']='1e999999'
        p=normalize(item,'geckoterminal')
        self.assertIsNone(p['buys_24h'])
        self.assertIn('buys_24h_invalid',p['flags'])
