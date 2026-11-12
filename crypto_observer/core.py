from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

NORMALIZER_VERSION = "2"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def address(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    # Base v4 pool ids can be 32 bytes; token ids are checked separately.
    if len(value) not in (42, 66):
        return None
    try:
        int(value[2:], 16)
    except ValueError:
        return None
    return value.lower()


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
        return str(d) if d.is_finite() and d >= 0 else None
    except (InvalidOperation, ValueError):
        return None


@dataclass
class Fetch:
    started_at: str
    observed_at: str
    status: int | None
    body: bytes
    payload: object
    error: str | None
    attempts: int
    history: list = field(default_factory=list)


class Client:
    def __init__(self, timeout=20, retries=2, opener=urlopen, sleep=time.sleep):
        self.timeout, self.retries = timeout, retries
        self.opener, self.sleep = opener, sleep
        self.last = {}

    def get(self, url, spacing=0.3, body=None):
        request_body = json.dumps(body).encode() if body is not None else None
        host = url.split("/")[2]
        wait = spacing - (time.monotonic() - self.last.get(host, 0))
        if wait > 0:
            self.sleep(wait)
        started = now()
        history = []
        for attempt in range(1, self.retries + 2):
            attempt_started = now()
            status, body, payload, error, retryable = None, b"", None, None, False
            delay = min(2 ** attempt, 10)
            try:
                req = Request(url, data=request_body, headers={"User-Agent": "crypto-observer/0.1", "Accept": "application/json", "Content-Type":"application/json"})
                with self.opener(req, timeout=self.timeout) as response:
                    status, body = response.status, response.read()
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    error = "invalid_json"
            except HTTPError as exc:
                status, body = exc.code, exc.read()
                exc.close()
                error = f"http_{status}"
                retryable = status == 429 or status >= 500
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                if retry_after.isdigit():
                    delay = min(int(retry_after), 60)
            except (URLError, TimeoutError, OSError) as exc:
                error = f"transport:{type(exc).__name__}"
                retryable = True
            attempt_observed = now()
            history.append({"attempt":attempt,"started_at":attempt_started,"observed_at":attempt_observed,"status":status,"error":error,"body":body})
            self.last[host] = time.monotonic()
            if error and retryable and attempt <= self.retries:
                self.sleep(max(delay, spacing))
                continue
            return Fetch(started, attempt_observed, status, body, payload, error, attempt, history)
        raise AssertionError("unreachable")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES(1);
        INSERT OR IGNORE INTO schema_version VALUES(2);
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, config TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS requests(
          id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
          source TEXT NOT NULL, url TEXT NOT NULL, started_at TEXT NOT NULL,
          observed_at TEXT NOT NULL, status INTEGER, attempts INTEGER NOT NULL,
          error TEXT, sha256 TEXT NOT NULL, body BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS request_attempts(
          request_id INTEGER NOT NULL REFERENCES requests(id), attempt INTEGER NOT NULL,
          started_at TEXT NOT NULL, observed_at TEXT NOT NULL, status INTEGER,
          error TEXT, sha256 TEXT NOT NULL, body BLOB NOT NULL,
          PRIMARY KEY(request_id,attempt));
        CREATE TABLE IF NOT EXISTS pools(
          chain TEXT NOT NULL, address TEXT NOT NULL, token_address TEXT,
          name TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          last_attempt_at TEXT, last_refresh_status TEXT,
          PRIMARY KEY(chain,address));
        CREATE TABLE IF NOT EXISTS snapshots(
          id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id),
          chain TEXT NOT NULL, pool_address TEXT NOT NULL, observed_at TEXT NOT NULL,
          source TEXT NOT NULL, token_address TEXT, pool_created_at TEXT,
          price_usd TEXT, liquidity_usd TEXT, fdv_usd TEXT, market_cap_usd TEXT,
          volume_24h_usd TEXT, buys_24h INTEGER, sells_24h INTEGER,
          flags TEXT NOT NULL, normalizer_version TEXT NOT NULL,
          UNIQUE(request_id,chain,pool_address),
          FOREIGN KEY(chain,pool_address) REFERENCES pools(chain,address));
        CREATE INDEX IF NOT EXISTS snapshots_pool ON snapshots(chain,pool_address,id);
        """)

        from .short_windows import setup as window_setup
        window_setup(self)

    def close(self):
        self.db.close()

    def start(self, config):
        cur = self.db.execute("INSERT INTO runs(started_at,status,config) VALUES(?,?,?)", (now(), "running", json.dumps(config)))
        self.db.commit()
        return cur.lastrowid

    def finish(self, run_id, notes):
        self.db.execute("UPDATE runs SET finished_at=?,status=?,notes=? WHERE id=?", (now(), "partial" if notes else "ok", json.dumps(notes), run_id))
        self.db.commit()

    def request(self, run_id, source, url, result):
        cur = self.db.execute("INSERT INTO requests(run_id,source,url,started_at,observed_at,status,attempts,error,sha256,body) VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, source, url, result.started_at, result.observed_at, result.status, result.attempts, result.error, hashlib.sha256(result.body).hexdigest(), result.body))
        history = result.history or [{"attempt":result.attempts,"started_at":result.started_at,"observed_at":result.observed_at,"status":result.status,"error":result.error,"body":result.body}]
        self.db.executemany("INSERT INTO request_attempts VALUES(?,?,?,?,?,?,?,?)", [(cur.lastrowid,h["attempt"],h["started_at"],h["observed_at"],h["status"],h["error"],hashlib.sha256(h["body"]).hexdigest(),h["body"]) for h in history])
        self.db.commit()
        return cur.lastrowid

    def save(self, request_id, source, observed_at, pool, raw_item=None):
        flags = list(pool["flags"])
        token = pool.get("token_address")
        self.db.execute("""INSERT INTO pools(chain,address,token_address,name,first_seen_at,last_seen_at)
            VALUES('base',?,?,?,?,?) ON CONFLICT(chain,address) DO UPDATE SET
            last_seen_at=excluded.last_seen_at, name=COALESCE(excluded.name,pools.name),
            token_address=COALESCE(excluded.token_address,pools.token_address)""", (pool["address"], token, pool.get("name"), observed_at, observed_at))
        keys = ["token_address", "pool_created_at", "price_usd", "liquidity_usd", "fdv_usd", "market_cap_usd", "volume_24h_usd", "buys_24h", "sells_24h"]
        self.db.execute("""INSERT OR IGNORE INTO snapshots(request_id,chain,pool_address,observed_at,source,token_address,pool_created_at,price_usd,liquidity_usd,fdv_usd,market_cap_usd,volume_24h_usd,buys_24h,sells_24h,flags,normalizer_version) VALUES(?,'base',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (request_id, pool["address"], observed_at, source, *[pool.get(k) for k in keys], json.dumps(flags), NORMALIZER_VERSION))
        if raw_item is not None:
            from .short_windows import save as save_windows
            snapshot_id = self.db.execute("SELECT id FROM snapshots WHERE request_id=? AND chain='base' AND pool_address=?", (request_id,pool["address"])).fetchone()[0]
            save_windows(self,snapshot_id,source,raw_item)
        self.db.commit()

    def tracking(self, limit):
        return self.db.execute("SELECT address FROM pools WHERE chain='base' ORDER BY COALESCE(last_attempt_at,''), first_seen_at,address LIMIT ?", (limit,)).fetchall()

    def mark(self, pools, timestamp, status):
        self.db.executemany("UPDATE pools SET last_attempt_at=?,last_refresh_status=? WHERE chain='base' AND address=?", [(timestamp, status, p) for p in pools])
        self.db.commit()


def normalize(item, source):
    if not isinstance(item, dict):
        raise ValueError("pool_not_object")
    if source == "geckoterminal":
        a = item.get("attributes") or {}
        if not isinstance(a, dict):
            raise ValueError("attributes_not_object")
        pool_id = address(a.get("address"))
        token_id = (((item.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        token = address(token_id.removeprefix("base_"))
        txns = (a.get("transactions") or {}).get("h24") or {}
        p = dict(address=pool_id, token_address=token, name=a.get("name"), pool_created_at=a.get("pool_created_at"),
                 price_usd=a.get("base_token_price_usd"), liquidity_usd=a.get("reserve_in_usd"),
                 fdv_usd=a.get("fdv_usd"), market_cap_usd=a.get("market_cap_usd"),
                 volume_24h_usd=(a.get("volume_usd") or {}).get("h24"))
    else:
        if item.get("chainId") != "base":
            raise ValueError("wrong_chain")
        token = address((item.get("baseToken") or {}).get("address"))
        txns = (item.get("txns") or {}).get("h24") or {}
        created = item.get("pairCreatedAt")
        try:
            created = datetime.fromtimestamp(float(created)/1000, timezone.utc).isoformat() if created is not None else None
        except (TypeError, ValueError, OverflowError, OSError):
            created = None
        p = dict(address=address(item.get("pairAddress")), token_address=token, name=(item.get("baseToken") or {}).get("symbol"), pool_created_at=created,
                 price_usd=item.get("priceUsd"), liquidity_usd=(item.get("liquidity") or {}).get("usd"),
                 fdv_usd=item.get("fdv"), market_cap_usd=item.get("marketCap"), volume_24h_usd=(item.get("volume") or {}).get("h24"))
    if p["address"] is None:
        raise ValueError("invalid_pool_address")
    if token and len(token) != 42:
        p["token_address"] = None
    flags = ["holders_unknown", "contract_risk_unknown", "sellability_unknown", "circulating_supply_unverified"]
    for key in ("price_usd", "liquidity_usd", "fdv_usd", "market_cap_usd", "volume_24h_usd"):
        raw = p[key]
        p[key] = number(raw)
        if p[key] is None:
            flags.append(key + ("_missing" if raw is None else "_invalid"))
    for key, source_key in (("buys_24h", "buys"), ("sells_24h", "sells")):
        raw = txns.get(source_key)
        parsed = number(raw)
        value = Decimal(parsed) if parsed is not None else None
        p[key] = int(value) if value is not None and value <= 9223372036854775807 and value == value.to_integral_value() else None
        if p[key] is None:
            flags.append(key + ("_missing" if raw is None else "_invalid"))
    if p["sells_24h"] == 0:
        flags.append("no_observed_sells_not_proof_of_honeypot")
    p["flags"] = flags
    return p


def collect(store, client, pages=2, refresh_limit=100):
    run = store.start(dict(chain="base", pages=pages, refresh_limit=refresh_limit, normalizer=NORMALIZER_VERSION))
    notes, discovered = [], 0
    def fetch(source, url, spacing):
        r = client.get(url, spacing)
        rid = store.request(run, source, url, r)
        if r.error:
            notes.append(f"{source}:{r.error}")
        return rid, r
    try:
        for page in range(1, pages+1):
            url = f"https://api.geckoterminal.com/api/v2/networks/base/new_pools?page={page}"
            rid, r = fetch("geckoterminal", url, 2.1)
            data = r.payload.get("data") if isinstance(r.payload, dict) else None
            if r.error or not isinstance(data, list):
                if not r.error:
                    notes.append("geckoterminal:unexpected_schema")
                break
            for item in data:
                try:
                    store.save(rid, "geckoterminal", r.observed_at, normalize(item, "geckoterminal"), raw_item=item)
                    discovered += 1
                except (ValueError, TypeError, AttributeError) as exc:
                    notes.append(f"normalize:geckoterminal:{type(exc).__name__}")
            if len(data) < 20:
                break
            if page == pages:
                notes.append("discovery_page_cap_reached:coverage_incomplete")
        tracked = [r["address"] for r in store.tracking(refresh_limit)]
        for offset in range(0, len(tracked), 20):
            chunk = tracked[offset:offset+20]
            url = "https://api.dexscreener.com/latest/dex/pairs/base/" + ",".join(chunk)
            rid, r = fetch("dexscreener", url, 0.3)
            pairs = r.payload.get("pairs") if isinstance(r.payload, dict) else None
            if isinstance(r.payload, dict) and "pairs" in r.payload and pairs is None:
                pairs = []
            if r.error or not isinstance(pairs, list):
                store.mark(chunk, r.observed_at, "request_error" if r.error else "unknown_schema")
                if not r.error:
                    notes.append("dexscreener:unexpected_schema")
                continue
            seen = set()
            for item in pairs:
                try:
                    p = normalize(item, "dexscreener")
                    if p["address"] not in chunk:
                        notes.append("dexscreener:unexpected_pool")
                        continue
                    store.save(rid, "dexscreener", r.observed_at, p, raw_item=item)
                    seen.add(p["address"])
                except (ValueError, TypeError, AttributeError):
                    notes.append("normalize:dexscreener:invalid_record")
            store.mark(list(seen), r.observed_at, "observed")
            missing = sorted(set(chunk)-seen)
            store.mark(missing, r.observed_at, "source_missing")
            if missing:
                notes.append(f"dexscreener:source_missing:{len(missing)}")
    except BaseException:
        notes.append("run_interrupted")
        store.finish(run, notes)
        raise
    store.finish(run, notes)
    return dict(run_id=run, discovery_records=discovered, refresh_attempts=len(tracked), notes=notes)


def report(store):
    rows = store.db.execute("""SELECT p.*,s.source,s.observed_at,s.price_usd,s.liquidity_usd,
        s.fdv_usd,s.market_cap_usd,s.flags FROM pools p JOIN snapshots s ON s.id=
        (SELECT MAX(s2.id) FROM snapshots s2 WHERE s2.chain=p.chain AND s2.pool_address=p.address)
        ORDER BY p.first_seen_at,p.address""").fetchall()
    n = len(rows)
    counts = {table: store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("runs", "requests", "snapshots")}
    errors = store.db.execute("SELECT COUNT(*) FROM requests WHERE error IS NOT NULL").fetchone()[0]
    latest = store.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    lines = ["# Base observation quality report", "", f"Generated at: {now()}", "", f"Pools: {n}; runs: {counts['runs']}; requests: {counts['requests']}; snapshots: {counts['snapshots']}; failed requests: {errors}.", "", "## Latest observations", "", "| Field | Missing / pools |", "|---|---:|"]
    for field in ("token_address", "price_usd", "liquidity_usd", "market_cap_usd", "fdv_usd"):
        lines.append(f"| {field} | {sum(r[field] is None for r in rows)} / {n} |")
    missing = sum(r["last_refresh_status"] == "source_missing" for r in rows)
    lines.extend(["", f"Pools absent from last attempted refresh: {missing}. Absence is not evidence of zero value or fraud.", "", "All circulating supplies are unverified; holders, contract safety and sellability remain unknown.", "Discovery is a provider subset. Refresh is budgeted and rotates by oldest attempt; observations can be stale."])
    if latest:
        lines.extend(["", f"Latest run: {latest['id']} / {latest['status']}", f"Coverage/config: {latest['config']}", f"Notes: {latest['notes']}"])
    lines.extend(["", "## Research leads (liquidity >= $50K only; not risk-cleared)", "", "| Pool address | Observed UTC | Source | Liquidity USD | FDV USD | Market cap USD | Refresh status |", "|---|---|---|---:|---:|---:|---|"])
    for r in rows:
        if r["liquidity_usd"] is not None and Decimal(r["liquidity_usd"]) >= 50000:
            lines.append(f"| {r['address']} | {r['observed_at']} | {r['source']} | {r['liquidity_usd']} | {r['fdv_usd'] or 'unknown'} | {r['market_cap_usd'] or 'unknown'} | {r['last_refresh_status'] or 'not attempted'} |")
    return "\n".join(lines) + "\n"
