# Quote evidence audit and isolated sensitivity

These tools never update experiment tables, use no network, and do not change the frozen quote model.

```sh
python3 -m crypto_observer.quote_audit --db data/observer.sqlite3 --output reports/quote_audit_base.json
python3 -m crypto_observer.quote_audit --db data/short_term_v2.sqlite3 --output reports/short_term/quote_audit.json
python3 -m crypto_observer.quote_audit --book normalized-book.json --output reports/book_stress.json
python3 -m unittest discover -s tests -p test_quote_audit.py -v
```

The database CLI opens a read-only, consistent SQLite snapshot. Outcomes and unknown entries are retained in the audit population. A case is an outcome at a horizon, not an independent trade. The population manifest hash identifies the exact selection input. Save the report when fixing a review cohort: rerunning against an enlarged database can change the sample.

Sampling is deterministic SHA256 ordering by seed and case ID within version, phase (where supplied) and unknown/extreme/normal strata. Absolute return at least 50% is the fixed diagnostic extreme threshold. Base's sorting metric is mark value divided by notional minus one, excluding entry gas; it is not the portfolio return. By default up to five cases per stratum are selected. The audit includes original quote fields, references, request times, status and retained-body SHA checks. Missing quotes and missed windows remain explicitly missing. HTTP errors are distinguished, but automated classification does not infer a rug, blacklist or executable route. Full identity/precision/route/liquidity interpretation still requires case review of the raw response and independent supporting evidence.

The book input is the normalized spot representation accepted by the existing depth validator: `instrument_key`, `kind: spot`, `quote_currency: USDT`, `size_unit`, `execution_supported: true`, `status: quoted`, timezone-aware `observed_at` and `source_at`, and ordered `bids`/`asks` arrays of `[price, base_quantity]` strings. Swaps remain unsupported.

Independent book stress uses 10/100/1000 USDT buy amounts, retained depth fractions 1/0.5/0.25, and delays 0/5/30 seconds. Costs show 0/10/30 bps entry fees. It reuses the original age/identity/depth validator without changing it. Delay evaluates the same frozen book for staleness; it does **not** estimate later market prices. No exits, completed fills, AMM reserve changes or realized returns are claimed. Unsupported and insufficient-capacity cases stay unknown.

Local validation on 2026-11-11: deterministic selection, tampered/missing request hashes, read-only missed-window retention and depth/staleness stress covered by tests. Live read-only audit smoke passed on both formal databases. Manual case interpretation, local AMM state simulation and future prospective execution-model validation remain outstanding.
