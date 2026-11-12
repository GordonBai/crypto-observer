"""Versioned provider rolling windows; observation diagnostics, not candle bars."""
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from .core import number, now

VERSION = '1'
WINDOWS = {'m5':300, 'm15':900, 'm30':1800, 'h1':3600, 'h6':21600, 'h24':86400}


def setup(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS snapshot_windows(
      snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
      feature_version TEXT NOT NULL, window TEXT NOT NULL, window_seconds INTEGER NOT NULL,
      volume_usd TEXT, price_change_pct TEXT, buys INTEGER, sells INTEGER,
      buyers INTEGER, sellers INTEGER, quote_address TEXT, venue TEXT,
      source_at TEXT, flags TEXT NOT NULL,
      PRIMARY KEY(snapshot_id,feature_version,window));
    ''')


def mapping(value):
    return value if isinstance(value, dict) else {}


def signed_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return str(parsed) if parsed.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def count(value):
    parsed = number(value)
    if parsed is None:
        return None
    parsed = Decimal(parsed)
    return int(parsed) if parsed <= 9223372036854775807 and parsed == parsed.to_integral_value() else None


def extract(item, source):
    # No source timestamp is defined for these pair snapshot fields. Receipt time
    # is available via snapshots/requests, not a fabricated market event time.
    from .core import address
    if source == 'geckoterminal':
        a = mapping(item.get('attributes'))
        volume, changes, txns = (mapping(a.get(k)) for k in ('volume_usd','price_change_percentage','transactions'))
        relationships = mapping(item.get('relationships'))
        quote = mapping(mapping(relationships.get('quote_token')).get('data')).get('id','')
        quote = address(quote.removeprefix('base_')) if isinstance(quote,str) else None
        venue = mapping(mapping(relationships.get('dex')).get('data')).get('id')
        windows = WINDOWS
    elif source == 'dexscreener':
        volume, changes, txns = (mapping(item.get(k)) for k in ('volume','priceChange','txns'))
        quote = address(mapping(item.get('quoteToken')).get('address'))
        venue = item.get('dexId')
        windows = {w:WINDOWS[w] for w in ('m5','h1','h6','h24')}
    else:
        return []
    if quote and len(quote) != 42:
        quote = None
    result = []
    for window, seconds in windows.items():
        transactions = mapping(txns.get(window))
        values = {'volume_usd':number(volume.get(window)),
                  'price_change_pct':signed_number(changes.get(window)),
                  **{k:count(transactions.get(k)) for k in ('buys','sells','buyers','sellers')}}
        flags = ['rolling_provider_window_not_closed_candle','source_timestamp_unknown']
        flags += [k+'_unknown' for k,v in values.items() if v is None]
        if quote is None:
            flags.append('quote_identity_unknown')
        result.append((VERSION,window,seconds,*values.values(),quote,venue if isinstance(venue,str) else None,None,json.dumps(flags)))
    return result


def save(store, snapshot_id, source, item):
    store.db.executemany('INSERT OR IGNORE INTO snapshot_windows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         [(snapshot_id,*row) for row in extract(item,source)])


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def percentile(values, fraction):
    # Nearest-rank percentiles describe observed samples, not gap guarantees.
    values = sorted(values)
    return values[max(0, math.ceil(len(values)*fraction)-1)] if values else None


def coverage_metrics(store, at=None, budget=300):
    """Include all retained pools so disappearance cannot improve coverage."""
    at = timestamp(at or now())
    if at is None:
        raise ValueError('timezone-aware observation time required')
    total = store.db.execute('SELECT COUNT(*) FROM pools').fetchone()[0]
    observed = store.db.execute("""SELECT DISTINCT s.id,s.source,s.chain,s.pool_address,s.observed_at
      FROM snapshots s JOIN snapshot_windows w ON w.snapshot_id=s.id""").fetchall()
    groups = defaultdict(lambda: defaultdict(list))
    invalid = Counter()
    for row in observed:
        source = row['source']
        instant = timestamp(row['observed_at'])
        if instant is None or instant > at:
            invalid[source] += 1
            continue
        groups[source][(row['chain'],row['pool_address'])].append(instant)
    sources = sorted(set(groups)|set(invalid)|{'dexscreener','geckoterminal'})
    results = []
    for source in sources:
        ages, gaps = [], []
        for times in groups[source].values():
            times.sort()
            ages.append((at-times[-1]).total_seconds())
            gaps.extend((b-a).total_seconds() for a,b in zip(times,times[1:]))
        covered = sum(age <= 1800 for age in ages)
        results.append({'source':source,'retained_pool_denominator':total,
          'observed_pools':len(ages),'never_observed_pools':total-len(ages),
          'covered_30m':covered,'coverage_30m_fraction':covered/total if total else None,
          'uncovered_30m':total-covered,'stale_observed_30m':sum(age>1800 for age in ages),
          'age_p50_seconds':percentile(ages,.5),'age_p95_seconds':percentile(ages,.95),
          'age_min_seconds':min(ages,default=None),
          'age_max_seconds':max(ages,default=None),'intervals':len(gaps),
          'gap_p50_seconds':percentile(gaps,.5),'gap_p95_seconds':percentile(gaps,.95),
          'gap_max_seconds':max(gaps,default=None),'gaps_over_5m':sum(gap>300 for gap in gaps),
          'gaps_over_30m':sum(gap>1800 for gap in gaps),'invalid_or_future_times':invalid[source]})
    statuses = dict(Counter(row[0] or 'never_attempted' for row in store.db.execute('SELECT last_refresh_status FROM pools')))
    # Use observed collection runs with the specified budget, not the nominal
    # launchd timer. This remains a fixed-universe capacity estimate only.
    runs = []
    for row in store.db.execute('SELECT started_at,finished_at,config FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1000'):
        try:
            config = json.loads(row['config'])
        except (TypeError, ValueError):
            continue
        if not isinstance(config,dict) or config.get('chain')!='base' or config.get('refresh_limit')!=budget:
            continue
        start,end = timestamp(row['started_at']),timestamp(row['finished_at'])
        if start and end and start<=end<=at:
            runs.append((start,end))
        if len(runs)>=20:
            break
    runs.sort()
    starts = [(b[0]-a[0]).total_seconds() for a,b in zip(runs,runs[1:])]
    durations = [(end-start).total_seconds() for start,end in runs]
    waves = math.ceil(total/budget) if budget>0 else None
    cadence = percentile(starts,.5)
    capacity = {'refresh_budget_per_collection':budget,'retained_pools':total,
      'fixed_universe_waves':waves,'sampled_collection_runs':len(runs),
      'observed_start_interval_p50_seconds':cadence,
      'observed_start_interval_p95_seconds':percentile(starts,.95),
      'collection_duration_p50_seconds':percentile(durations,.5),
      'estimated_rotation_seconds':waves*cadence if waves is not None and cadence is not None else None}
    return {'generated_at':at.isoformat(),'sources':results,'last_refresh_status':statuses,'capacity_estimate':capacity}


def coverage_report(store, metrics=None):
    metrics = metrics if metrics is not None else coverage_metrics(store)
    def value(x):
        return 'unknown' if x is None else round(x,1) if isinstance(x,float) else x
    lines = ['## Retained-universe coverage and capacity', '',
      'Every source uses ALL retained chain/pool identities as its denominator, including never observed, stale and missing pools. This is a conservative monitoring denominator, not proof that the provider supports every pool or that all pools are still trading.',
      'Age is local receipt age. A recent receipt does not prove recent market data. Sources are never pooled to conceal a gap; malformed/future receipt times do not count as coverage.', '',
      '| Source | Retained pools | Window-observed pools | Never observed | Covered <=30m | Coverage fraction | Not covered <=30m | Observed but stale >30m | Invalid/future timestamps |',
      '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in metrics['sources']:
        lines.append('| '+ ' | '.join(str(round(r[k],6) if k=='coverage_30m_fraction' and r[k] is not None else value(r[k])) for k in ('source','retained_pool_denominator','observed_pools','never_observed_pools','covered_30m','coverage_30m_fraction','uncovered_30m','stale_observed_30m','invalid_or_future_times'))+' |')
    lines += ['', '| Source | Receipt age p50 seconds | Age p95 | Age max | Intervals | Gap p50 seconds | Gap p95 | Gap max | Gaps >5m | Gaps >30m |',
      '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in metrics['sources']:
        lines.append('| '+' | '.join(str(value(r[k])) for k in ('source','age_p50_seconds','age_p95_seconds','age_max_seconds','intervals','gap_p50_seconds','gap_p95_seconds','gap_max_seconds','gaps_over_5m','gaps_over_30m'))+' |')
    lines += ['', 'Percentiles describe pools with valid window observations only; never-observed pools remain visible in the denominator above. Receipt history starts when feature version 1 was installed; no historical feature backfill is performed.', '',
      'Latest DEX refresh state across all retained pools (not a GeckoTerminal source status): '+json.dumps(metrics['last_refresh_status'],sort_keys=True)+'.', '',
      'Fixed-universe capacity estimate (not a service SLA or coverage guarantee):', '',
      '```json',json.dumps(metrics['capacity_estimate'],indent=2),'```', '',
      'Estimated rotation = ceil(retained pools / refresh budget) × observed median collection-start interval. It assumes no new pools, unchanged rotation, available sources and continued scheduling. Growth, missing responses, offline periods and workload changes can make it worse. A nominal 15-minute timer is not a per-pool cadence.', '']
    return '\n'.join(lines)


def report(store):
    rows = store.db.execute('''SELECT s.source,w.window,w.window_seconds,COUNT(*) n,
      SUM(w.volume_usd IS NULL) volume_unknown,SUM(w.price_change_pct IS NULL) change_unknown,
      SUM(w.buys IS NULL OR w.sells IS NULL) txns_unknown,SUM(w.source_at IS NULL) time_unknown
      FROM snapshot_windows w JOIN snapshots s ON s.id=w.snapshot_id
      GROUP BY s.source,w.window,w.window_seconds ORDER BY s.source,w.window_seconds''').fetchall()
    lines = ['# Short-window observation readiness', '', 'Generated UTC: '+now(), '',
      'Feature version 1. Provider rolling windows are not closed, non-overlapping candles. USD volume is gross turnover, not net inflow. Buy/sell counts are not money flow.',
      'Source market timestamps are unknown; requests.started_at and snapshots.observed_at preserve local request/receipt times only. Market-data staleness cannot be measured from receipt time.',
      'This Base rolling-window layer itself does not backfill history, trigger anomalies or establish H1/H2 effectiveness. Independent closed-bar short-term experiment reports live in reports/short_term. v2 is unchanged.', '',
      '| Source | Window seconds | Rows | Volume unknown | Price change unknown | Txn counts unknown | Source time unknown |',
      '|---|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        lines.append(f"| {r['source']} | {r['window_seconds']} | {r['n']} | {r['volume_unknown']} | {r['change_unknown']} | {r['txns_unknown']} | {r['time_unknown']} |")
    if not rows:
        lines.append('\nNo new window observations yet; existing raw history is not relabeled as new prospective evidence.')
    lines += ['', '## Actual per-pool receipt coverage', '',
      'Intervals below compare successive observations of the same source/chain/pool with feature rows, not global service starts. Rotation, missing pools and offline periods all affect them. No gaps are interpolated.', '',
      '| Source | Observed pools | Latest receipt age seconds | Intervals | Max gap seconds | Gaps >5m | Gaps >30m |',
      '|---|---:|---:|---:|---:|---:|---:|']
    metrics = coverage_metrics(store)
    for r in metrics['sources']:
        age = round(r['age_min_seconds'],1) if r['age_min_seconds'] is not None else 'unknown'
        gap = round(r['gap_max_seconds'],1) if r['gap_max_seconds'] is not None else 'unknown'
        lines.append(f"| {r['source']} | {r['observed_pools']} | {age} | {r['intervals']} | {gap} | {r['gaps_over_5m']} | {r['gaps_over_30m']} |")
    lines += ['', 'These Base rolling snapshots do not verify 5-minute paths or provide a 24-closed-hour relative-volume baseline. Rolling h24 differences must not be interpreted as hourly turnover. The independent short-term experiment uses separately verified closed bars, frozen rules and delay-aware outcomes.', '']
    lines += ['', coverage_report(store,metrics)]
    return '\n'.join(lines)
