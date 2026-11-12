"""Version-separated, missingness-aware evaluation of prospective quote cohorts."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from .core import now
from .paper import setup


def drawdown(values):
    if not values or any(v is None for v in values):
        return None
    peak, worst = Decimal(values[0]), Decimal(0)
    for value in values:
        value = Decimal(value)
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1 - value / peak)
    return worst


def summarize_returns(values, success=Decimal(1)):
    known = [Decimal(v) for v in values if v is not None]
    total, n = len(values), len(known)
    return {
        'total': total, 'known': n, 'unknown': total - n,
        'mean_known': str(sum(known) / n) if n else None,
        'precision_at_selected_k_known': str(sum(v >= success for v in known) / Decimal(n)) if n else None,
        'mean_unknown_zero_proceeds_stress': str((sum(known) - Decimal(total - n)) / total) if total else None,
    }


def exit_observation_rates(statuses):
    total=len(statuses)
    known=sum(s=='quoted' for s in statuses)
    unavailable=sum(s=='quote_unknown' for s in statuses)
    missed=sum(s=='missed_window' for s in statuses)
    pending=sum(s=='not_evaluated' for s in statuses)
    return {'due':total,'quoted':known,'quote_unavailable':unavailable,'missed_window':missed,'not_evaluated':pending,
            'unobserved_fraction':str(Decimal(total-known)/total) if total else None,
            'quote_unavailable_fraction':str(Decimal(unavailable)/total) if total else None}


def version_configs(store):
    return {r['version']: (json.loads(r['config']), datetime.fromisoformat(r['created_at']))
            for r in store.db.execute('SELECT * FROM experiments')}


def portfolio_path(store, positions, horizon, asof_day, capital=Decimal(10000)):
    """Separate fixed-capital portfolio for each strategy/version/horizon.

    Each position spends entry notional+estimated gas once. A terminal quote closes
    that horizon's position. Unknown daily marks stay unknown, never stale-filled.
    The zero-proceeds path is a pessimistic scenario, not observed performance.
    """
    if not positions:
        return [], False
    first = min(datetime.fromisoformat(p['entry_at']).date() for p in positions)
    path, capacity_failed = [], False
    day = first
    while day <= asof_day:
        cash, value, zero_value = capital, Decimal(0), Decimal(0)
        unknown = 0
        for p in positions:
            if datetime.fromisoformat(p['entry_at']).date() > day:
                continue
            cost = Decimal(p['notional_usdc']) + Decimal(p['entry_gas_usd'])
            cash -= cost
            if cash < 0:
                capacity_failed = True
            terminal = store.db.execute('SELECT * FROM paper_marks WHERE position_id=? AND horizon_days=?', (p['id'], horizon)).fetchone()
            if terminal and datetime.fromisoformat(terminal['observed_at']).date() <= day:
                mark = terminal
                settled = True
            else:
                mark = store.db.execute('SELECT * FROM daily_position_marks WHERE position_id=? AND day=?', (p['id'], day.isoformat())).fetchone()
                settled = False
            if mark and mark['status'] == 'quoted' and mark['net_value_usdc'] is not None:
                amount = Decimal(mark['net_value_usdc'])
                if settled:
                    cash += amount
                else:
                    value += amount
                zero_value += Decimal(0) if settled else amount
            else:
                unknown += 1
        equity = cash + value if unknown == 0 else None
        path.append({'day': day.isoformat(), 'equity': equity,
                     'unknown_positions': unknown, 'unknown_zero_equity': cash + zero_value})
        day += timedelta(days=1)
    return path, capacity_failed


def report(store):
    setup(store)
    at = datetime.fromisoformat(now())
    configs = version_configs(store)
    rows = store.db.execute('SELECT p.*,s.strategy,s.cohort,s.token_address,s.version FROM paper_positions p JOIN paper_signals s ON s.id=p.signal_id ORDER BY s.cohort,p.id').fetchall()
    cohorts = {(r['version'], r['cohort']) for r in rows}
    lines = ['# Experiment evaluation', '', f'Generated UTC: {now()}', '',
             'Quote-based paper results; no realised trading P&L. UTC cohorts may share assets and are not assumed independent.',
             'Independent quotes do not simulate our hypothetical entry changing pool reserves. Thin-pool entry/exit comparisons and large hypothetical orders cannot be interpreted as realised execution costs. Route coverage, liquidity changes and token-specific restrictions remain material limitations.',
             'Experiments are separated by immutable version. v2 uses a predeclared temporal split 15 days after enrollment starts; v1 is a legacy diagnostic.',
             f'Observed version/day cohorts: {len(cohorts)}.', '',
             '| Version | Strategy | Split | Horizon | Extra friction per side bps | Due | Known | Unknown | Mean known | Unknown-zero-proceeds stress | Precision@selected-K known |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    grouped = defaultdict(list)
    statuses = defaultdict(int)
    status_groups = defaultdict(list)
    entry_groups = defaultdict(list)
    unknown_entries = defaultdict(int)
    portfolios = defaultdict(list)
    premature = 0
    for r in rows:
        config, created = configs[r['version']]
        entry_groups[(r['version'],r['strategy'])].append(r['entry_status'])
        boundary = created + timedelta(days=config.get('temporal_split_after_days', 15))
        split = ('development' if datetime.fromisoformat(r['entry_at']) < boundary else 'held_out') if r['version'] != 'v1' else 'legacy_diagnostic'
        if r['entry_status'] != 'quoted':
            unknown_entries[r['version'] + ':' + r['strategy']] += 1
            continue
        portfolios[(r['version'], r['strategy'])].append(r)
        for horizon in config['horizons_days']:
            if at < datetime.fromisoformat(r['entry_at']) + timedelta(days=horizon):
                premature += 1
                continue
            mark = store.db.execute('SELECT m.*,q.amount_out,q.gas_usd FROM paper_marks m JOIN route_quotes q ON q.id=m.quote_id WHERE m.position_id=? AND m.horizon_days=?', (r['id'], horizon)).fetchone()
            statuses[(r['version'], horizon, mark['status'] if mark else 'not_evaluated')] += 1
            status_groups[(r['version'],r['strategy'],horizon)].append(mark['status'] if mark else 'not_evaluated')
            for bps in config['stress_bps']:
                ret = None
                if mark and mark['status'] == 'quoted' and mark['amount_out'] is not None and mark['gas_usd'] is not None and r['entry_gas_usd'] is not None:
                    friction = Decimal(bps) / 10000
                    proceeds = Decimal(mark['amount_out']) / 1000000 * (1-friction) - Decimal(mark['gas_usd'])
                    basis = Decimal(r['notional_usdc']) * (1+friction) + Decimal(r['entry_gas_usd'])
                    ret = proceeds / basis - 1
                grouped[(r['version'], r['strategy'], split, horizon, bps)].append(ret)
    for (version, strategy, split, horizon, bps), values in sorted(grouped.items()):
        a = summarize_returns(values, Decimal(str(configs[version][0]['success_net_return'])))
        lines.append(f"| {version} | {strategy} | {split} | {horizon} | {bps} | {a['total']} | {a['known']} | {a['unknown']} | {a['mean_known']} | {a['mean_unknown_zero_proceeds_stress']} | {a['precision_at_selected_k_known']} |")
    lines += ['', f'Not-yet-due position/horizon pairs: {premature}. Entry unknown by version/strategy: {json.dumps(dict(unknown_entries))}.',
              'Exit status counts: ' + json.dumps([{'version': k[0], 'horizon': k[1], 'status': k[2], 'count': v} for k,v in sorted(statuses.items())]),
              'Unknown-zero-proceeds is a hypothetical loss scenario. It does not establish fraud, a rug, or actual loss. Mean-known results alone have missingness bias.', '',
              '## Calendar portfolios', '',
              'Each version/strategy/horizon is a separate hypothetical $10,000 account. Entry notionals and estimated gas consume cash; each holding horizon closes independently. Assets are not bought or sold.',
              'Equity requires a same-UTC-day quote for every active position. Missing marks are not carried forward. Drawdown is sampled daily and can miss intraday lows.', '',
              'Portfolio tables follow the explicit observation-rate denominators below.']
    # Keep failure denominators explicit; an operational miss is not a fraud label.
    lines += ['', '## Entry and exit observation rates', '',
              '| Version | Strategy | Entry records | Entry unknown | Entry unknown fraction |',
              '|---|---|---:|---:|---:|']
    for (version,strategy),values in sorted(entry_groups.items()):
        unknown=sum(value!='quoted' for value in values)
        fraction=str(Decimal(unknown)/len(values)) if values else None
        lines.append(f'| {version} | {strategy} | {len(values)} | {unknown} | {fraction} |')
    lines += ['', '| Version | Strategy | Horizon | Due funded | Quoted | Quote unavailable | Window missed | Not evaluated | Unobserved fraction | Quote unavailable fraction |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    if not status_groups:
        lines.append('No horizon is due. Exit observation/failure fractions are undefined, not zero.')
    for (version,strategy,horizon),values in sorted(status_groups.items()):
        rates=exit_observation_rates(values)
        lines.append(f"| {version} | {strategy} | {horizon} | {rates['due']} | {rates['quoted']} | {rates['quote_unavailable']} | {rates['missed_window']} | {rates['not_evaluated']} | {rates['unobserved_fraction']} | {rates['quote_unavailable_fraction']} |")
    lines += ['', 'These are quote/observation failure rates. Rug/fraud rates remain unclassified without independent evidence. Missing entry or exit quotes are not silently treated as successful execution.', '',
              '## Portfolio paths by version and horizon', '',
              '| Version | Strategy | Horizon | Funded positions | Last equity | Sampled portfolio MDD | Unknown-zero MDD scenario | Capacity exceeded | Unknown position-days |',
              '|---|---|---:|---:|---:|---:|---:|---|---:|']
    for (version,strategy), positions in sorted(portfolios.items()):
        for horizon in configs[version][0]['horizons_days']:
            path, capacity = portfolio_path(store, positions, horizon, at.date())
            observed = [Decimal(10000)] + [p['equity'] for p in path]
            pessimistic = [Decimal(10000)] + [p['unknown_zero_equity'] for p in path]
            lines.append(f"| {version} | {strategy} | {horizon} | {len(positions)} | {path[-1]['equity'] if path else None} | {drawdown(observed)} | {drawdown(pessimistic)} | {capacity} | {sum(p['unknown_positions'] for p in path)} |")
    lines += ['', '## Matched controls and inference', '',
              f"Recorded v2 treatment/control links: {store.db.execute('SELECT COUNT(*) FROM paper_matches').fetchone()[0]}.",
              'Controls use the same age/liquidity bucket and sampling time, without replacement within a strategy. Their outcomes remain observational. v1 links were not recorded and must not be reconstructed post hoc.',
              'No statistical alpha conclusion is available before matured held-out cohorts. Repeated assets, overlapping horizons, quote/fill gaps, unknown exits and incomplete discovery limit inference.']
    return '\n'.join(lines) + '\n'
