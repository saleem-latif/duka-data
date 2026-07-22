# Data Quality Report

_Last validated 2026-07-06 against `compiled/` (14 symbols × M5/H1/H4/D1 = 56 files, ~1.4 GiB).
Reproduce with `python3 check_integrity.py` (11/11 checks passing)._

## Verdict

| Dimension | Grade | Summary |
|---|---|---|
| **Completeness** | A | Full ~20-year FX history, all 4 timeframes present for every symbol, near-perfect intra-week coverage. |
| **Accuracy** | A | Every D1 bar reproduces **exactly** from its M5 constituents; all price outliers map to real market events. |
| **Quality** | A | No duplicate/misordered/negative/NaN values across all 56 files after cleaning. |

The 11 FX pairs are backtest-grade over 2006-05-24 → 2026-05-18. The three CFDs
(XAUUSD, LIGHTCMDUSD, USA500IDXUSD) are sound but carry the structural caveats
listed below (shorter history, session gaps, unreliable volume).

## What was validated

- **Structure** — all 56 files present; timestamps strictly increasing, UTC, no
  duplicates, no NaNs in OHLC.
- **OHLC invariants** — `high ≥ max(o,c,l)`, `low ≤ min(o,c,h)`, all prices > 0.
- **Cross-timeframe reconciliation** — daily OHLC rebuilt from raw M5 matches the
  shipped `*_D1.csv` with **zero mismatches and 0.0 relative error** for all 14
  symbols. D1/H4/H1 are faithful aggregations of one tick base.
- **Outliers are real** — the largest 5-min moves correspond to documented events:
  SNB franc de-peg (2015-01-15), sterling flash crash (2016-10-06), Brexit
  (2016-06-24), JPY flash crash (2019-01-02), WTI negative-oil (2020-04-21), COVID
  crash (2020-03-16). No spike-and-revert bad-tick artifacts.
- **FX spreads** are realistic institutional levels (EURUSD mean 0.62 pip,
  USDJPY 0.76 pip).

## Cleaning applied (2026-07-06)

1. **AUDUSD M5 — 19 negative spreads clipped to 0.** All fell in 2007–2009
   (early-Dukascopy bid/ask crossings, ≤11 pips). Only `AUDUSD_M5.csv` was
   affected; H1/H4/D1 use aggregated means that were already non-negative, and
   OHLC reconciliation is unaffected. The surgical rewrite left every other line
   byte-identical.
2. **`all_pairs_M5.csv` rebuilt.** It had previously contained only the last
   download run's symbols (LIGHTCMDUSD + USA500IDXUSD). It is now a proper
   long-format combination of **all 14 symbols** (19,195,808 rows, sorted by
   `symbol` then `time`, with a `symbol` column). Note: it mixes FX and CFDs —
   filter on `symbol` and do not aggregate across instruments blindly.
3. **`DATA_MANIFEST.md` regenerated** — the old copy was stale (listed 11 symbols
   and AUDJPY/GBPJPY at ~1,300 D1 bars; both are now backfilled to 5,212).
4. **`check_integrity.py` upgraded** — auto-discovers all symbols (incl. CFDs),
   adds the M5→D1 reconciliation check, and separates correctness gates from
   informational coverage/session/freshness reporting.

## Known caveats (inherent — not defects to fix)

- **CFD volume is unreliable.** Zero-volume bar share: LIGHTCMDUSD ~49%,
  USA500IDXUSD ~16%, XAUUSD ~14%. Do **not** build volume-based signals on the
  CFDs. FX volume is clean (0% zero).
- **CFD session structure.** Metals/energy/indices are not 24×5. They show many
  intraday session gaps by design and shorter history (XAUUSD from 2006,
  USA500IDXUSD from 2012, LIGHTCMDUSD from 2013). USA500/LIGHTCMD also have
  multi-week gaps in their early 2013 data.
- **Isolated FX feed gap.** USDJPY/EURJPY have a single ~6.7-day gap in June 2009
  (a Dukascopy feed outage, not a compiler issue).
- **Freshness.** Data ends 2026-05-18 (FX) / 2026-05-22 (CFD). Collection is
  paused; run `python3 download.py --incremental` to top up to the present.
