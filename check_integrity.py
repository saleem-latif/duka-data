"""
check_integrity.py — Comprehensive data integrity checks for compiled CSVs.

Checks:
1. File presence (all symbols × all timeframes)
2. Date range coverage
3. Trading day count vs expected
4. 24-hour coverage per day (M5)
5. Bar count consistency between timeframes
6. OHLC sanity (high >= max(open,close), low <= min(open,close), high >= low)
7. Spread sanity (spread > 0, spread within reasonable bounds)
8. Duplicate timestamps
9. Gap detection (missing trading days)
10. Cross-timeframe price consistency (D1 close == last H4 close)
"""

import datetime
from pathlib import Path
import sys

import pandas as pd
import numpy as np

COMPILED_DIR = Path(__file__).parent / "compiled"
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF",
           "AUDUSD", "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"]
TIMEFRAMES = ["M5", "H1", "H4", "D1"]

# Expected bars per day per timeframe (full 24h forex session)
EXPECTED_PER_DAY = {"M5": 288, "H1": 24, "H4": 6, "D1": 1}

# Pip multiplier per pair (for spread sanity)
PIP_MULT = {p: 100 if p.endswith("JPY") else 10000 for p in SYMBOLS}


def check_file_presence():
    print("\n[1] File presence check")
    print("-" * 60)
    missing = []
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            f = COMPILED_DIR / f"{sym}_{tf}.csv"
            if not f.exists():
                missing.append(f"{sym}_{tf}")
    if missing:
        print(f"  FAIL: missing {len(missing)} files: {missing}")
        return False
    print(f"  OK: all {len(SYMBOLS)*len(TIMEFRAMES)} files present")
    return True


def load_df(sym, tf):
    return pd.read_csv(COMPILED_DIR / f"{sym}_{tf}.csv", parse_dates=["time"])


def check_date_coverage():
    print("\n[2] Date range coverage")
    print("-" * 60)
    print(f"  {'Symbol':<8} {'First':<26} {'Last':<26} {'Days':>6}")
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        first = df["time"].min()
        last = df["time"].max()
        days = df["time"].dt.date.nunique()
        # Expect ~1303 trading days for 5 years
        flag = " " if 1290 <= days <= 1310 else "!"
        if flag == "!":
            issues += 1
        print(f"  {sym:<8} {str(first):<26} {str(last):<26} {days:>6} {flag}")
    return issues == 0


def check_24h_coverage():
    print("\n[3] 24-hour coverage check (M5)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        df["hour"] = df["time"].dt.hour
        hours = df["hour"].nunique()
        flag = " " if hours == 24 else "!"
        if flag == "!":
            issues += 1
        # Avg bars per day
        avg_bars = len(df) / df["time"].dt.date.nunique()
        print(f"  {sym:<8} hours={hours}/24  avg_bars/day={avg_bars:.0f}/288  {flag}")
    return issues == 0


def check_bar_counts():
    print("\n[4] Cross-timeframe bar count ratios")
    print("-" * 60)
    print(f"  {'Symbol':<8} {'M5':>9} {'H1':>9} {'H4':>9} {'D1':>9}  {'M5/H1':>7} {'H1/H4':>7}")
    issues = 0
    for sym in SYMBOLS:
        counts = {tf: len(load_df(sym, tf)) for tf in TIMEFRAMES}
        ratio_m5h1 = counts["M5"] / counts["H1"]
        ratio_h1h4 = counts["H1"] / counts["H4"]
        # M5 should be ~12x H1, H1 should be ~4x H4
        flag = " " if (11 <= ratio_m5h1 <= 13 and 3.5 <= ratio_h1h4 <= 4.5) else "!"
        if flag == "!":
            issues += 1
        print(f"  {sym:<8} {counts['M5']:>9,} {counts['H1']:>9,} "
              f"{counts['H4']:>9,} {counts['D1']:>9,}  "
              f"{ratio_m5h1:>7.2f} {ratio_h1h4:>7.2f} {flag}")
    return issues == 0


def check_ohlc_sanity():
    print("\n[5] OHLC sanity (high >= o,c; low <= o,c; high >= low)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            df = load_df(sym, tf)
            bad_high = ((df["high"] < df["open"]) | (df["high"] < df["close"]) |
                        (df["high"] < df["low"])).sum()
            bad_low  = ((df["low"]  > df["open"]) | (df["low"]  > df["close"])).sum()
            if bad_high or bad_low:
                print(f"  {sym}_{tf}: bad_high={bad_high}, bad_low={bad_low}  !")
                issues += 1
    if issues == 0:
        print(f"  OK: all OHLC relationships valid across {len(SYMBOLS)*len(TIMEFRAMES)} files")
    return issues == 0


def check_spread_sanity():
    print("\n[6] Spread sanity (M5)")
    print("-" * 60)
    print(f"  {'Symbol':<8} {'min_pips':>9} {'mean_pips':>10} {'max_pips':>9} {'neg':>5} {'zero':>6}")
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        mult = PIP_MULT[sym]
        spread_pips = df["spread"] * mult
        n_neg = (df["spread"] < 0).sum()
        n_zero = (df["spread"] == 0).sum()
        flag = " "
        if n_neg > 0:
            flag = "!"
            issues += 1
        # Reasonable spread upper bound: 50 pips for majors, 100 for crosses
        if spread_pips.max() > 100:
            flag = "?"
        print(f"  {sym:<8} {spread_pips.min():>9.2f} {spread_pips.mean():>10.2f} "
              f"{spread_pips.max():>9.2f} {n_neg:>5} {n_zero:>6} {flag}")
    return issues == 0


def check_duplicates():
    print("\n[7] Duplicate timestamps")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            df = load_df(sym, tf)
            dups = df["time"].duplicated().sum()
            if dups > 0:
                print(f"  {sym}_{tf}: {dups} duplicates  !")
                issues += 1
    if issues == 0:
        print(f"  OK: no duplicate timestamps in any file")
    return issues == 0


def check_gaps():
    print("\n[8] Trading day gap detection (D1)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "D1")
        df["date"] = df["time"].dt.date
        dates = sorted(df["date"].unique())
        first, last = dates[0], dates[-1]

        # Build expected weekday set
        expected = set()
        d = first
        while d <= last:
            if d.weekday() < 5:  # Mon-Fri
                expected.add(d)
            d += datetime.timedelta(days=1)

        actual = set(dates)
        missing = expected - actual
        # Holidays are expected — flag only if many missing
        if len(missing) > 50:
            print(f"  {sym}: {len(missing)} missing weekdays (likely holidays + issues)  ?")
            issues += 1 if len(missing) > 100 else 0
        else:
            print(f"  {sym}: {len(missing)} missing weekdays (holidays)  OK")
    return issues == 0


def check_price_consistency():
    print("\n[9] Cross-timeframe price consistency (D1 close ~ last H1 close of day)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        d1 = load_df(sym, "D1").set_index("time")
        h1 = load_df(sym, "H1").set_index("time")

        h1_last_per_day = h1.resample("1D")["close"].last()
        # Align indices
        merged = pd.concat([d1["close"].rename("d1_close"),
                            h1_last_per_day.rename("h1_last")], axis=1).dropna()
        diffs = (merged["d1_close"] - merged["h1_last"]).abs()
        max_diff = diffs.max() if len(diffs) else 0
        n_mismatch = (diffs > 1e-5).sum()
        flag = " " if max_diff < 1e-4 else "!"
        if flag == "!":
            issues += 1
        print(f"  {sym}: max_diff={max_diff:.6f}  mismatches>1e-5={n_mismatch}  {flag}")
    return issues == 0


def check_recent_data():
    print("\n[10] Recent data freshness")
    print("-" * 60)
    today = datetime.date.today()
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        last_date = df["time"].max().date()
        days_old = (today - last_date).days
        flag = " " if days_old <= 10 else "!"
        if flag == "!":
            issues += 1
        print(f"  {sym}: last={last_date}  ({days_old} days old)  {flag}")
    return issues == 0


def main():
    print("=" * 60)
    print("DATA INTEGRITY CHECK")
    print("=" * 60)

    checks = [
        ("File presence",          check_file_presence),
        ("Date coverage",          check_date_coverage),
        ("24h coverage",           check_24h_coverage),
        ("Bar count ratios",       check_bar_counts),
        ("OHLC sanity",            check_ohlc_sanity),
        ("Spread sanity",          check_spread_sanity),
        ("Duplicate timestamps",   check_duplicates),
        ("Trading day gaps",       check_gaps),
        ("Price consistency",      check_price_consistency),
        ("Data freshness",         check_recent_data),
    ]

    results = []
    for name, fn in checks:
        try:
            ok = fn()
            results.append((name, ok))
        except Exception as e:
            print(f"  ERROR in {name}: {e}")
            results.append((name, False))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}]  {name}")
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
