"""
download.py — Direct Dukascopy bi5 tick downloader & OHLC compiler
===================================================================
Downloads raw tick data from Dukascopy's public datafeed, decodes the
LZMA-compressed bi5 files, and resamples into M5/H1/H4/D1 OHLC bars
with bid/ask spread information.

No external trading libraries required — uses only stdlib + pandas + numpy.

Tick bi5 format (per hour file):
- URL: http://datafeed.dukascopy.com/datafeed/{PAIR}/{year}/{month_0idx:02d}/{day:02d}/{hour:02d}h_ticks.bi5
- Month is 0-indexed (January=00, December=11)
- LZMA compressed
- Each tick record is 20 bytes (big-endian):
    int32:   milliseconds offset from start of hour
    int32:   ask price (raw integer, divide by point_divider)
    int32:   bid price (raw integer, divide by point_divider)
    float32: ask volume
    float32: bid volume
- JPY pairs: point_divider = 1000
- Standard pairs: point_divider = 100000

Output: M5, H1, H4, D1 CSVs with columns:
  time, open, high, low, close, volume, spread

Monitoring: writes real-time status to .download_status.json and logs to logs/

Design: Research data acquisition only. No live trading code.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import lzma
import resource
import struct
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "http://datafeed.dukascopy.com/datafeed"
TICK_RECORD_SIZE = 20  # bytes per tick

JPY_PAIRS = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"}

# Non-FX instruments (metals, energy, indices). Dukascopy stores these with a
# 1000 point-divider (3 implied decimals), same as JPY pairs — NOT 100000.
# Symbol = Dukascopy datafeed instrument name (verified against the live feed).
DIV_1000_INSTRUMENTS = {
    "XAUUSD", "XAGUSD",                                  # gold, silver
    "LIGHTCMDUSD", "BRENTCMDUSD",                        # WTI, Brent crude
    "USA500IDXUSD", "USA30IDXUSD", "USATECHIDXUSD",      # S&P500, Dow, Nasdaq100
}

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "USDCAD", "NZDUSD",       # majors
    "EURGBP", "EURJPY", "GBPJPY",       # crosses
]

DEFAULT_YEARS = 5
DAY_WORKERS = 3    # parallel days downloading at once (bumped from 2 → 3*2=6 concurrent, watch for 503s)
HOUR_WORKERS = 2   # parallel hours within each day (3*2=6 max concurrent requests; ~upper edge of ~5-10 req/s limit)

BASE_DIR     = Path(__file__).parent
RAW_DIR      = BASE_DIR / "raw"
COMPILED_DIR = BASE_DIR / "compiled"
LOG_DIR      = BASE_DIR / "logs"
META_FILE    = BASE_DIR / ".download_meta.json"
STATUS_FILE  = BASE_DIR / ".download_status.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure file + console logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"download_{datetime.date.today()}.log"

    logger = logging.getLogger("duka")
    logger.setLevel(logging.DEBUG)

    # File handler — detailed
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
    ))

    # Console handler — progress only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Status file (real-time monitoring)
# ---------------------------------------------------------------------------

_status: dict = {}


def status_update(**kwargs):
    """Update .download_status.json with current state."""
    _status.update(kwargs)
    _status["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        STATUS_FILE.write_text(json.dumps(_status, indent=2, default=str))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Performance instrumentation
# ---------------------------------------------------------------------------
# `ru_maxrss` is *peak* process RSS since start. Unit differs by OS: bytes on
# macOS, kilobytes on Linux. We track peak (not current) because the stdlib has
# no portable way to read current RSS; peak deltas between samples still tell
# us when memory grew.
_RU_MAXRSS_TO_BYTES = 1 if sys.platform == "darwin" else 1024


def rss_peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RU_MAXRSS_TO_BYTES / (1024 * 1024)


class Perf:
    """Threadsafe counters & timers used to debug perf across worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {}
        self.day_durations: list[float] = []   # wall-clock seconds per day

    def add(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def record_day(self, seconds: float) -> None:
        with self._lock:
            self.day_durations.append(seconds)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            snap = dict(self.counters)
            if self.day_durations:
                ds = sorted(self.day_durations)
                snap["day_p50_s"] = ds[len(ds) // 2]
                snap["day_p95_s"] = ds[int(len(ds) * 0.95)]
                snap["day_max_s"] = ds[-1]
            return snap

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.day_durations.clear()


PERF = Perf()


def _fmt_bytes(n: float) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n:.0f} B"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_point_divider(pair: str) -> int:
    pair = pair.upper().replace("/", "")
    return 1000 if (pair in JPY_PAIRS or pair in DIV_1000_INSTRUMENTS) else 100000


def _trading_days(start: datetime.date, end: datetime.date):
    """Yield weekdays (Mon-Fri) in [start, end)."""
    d = start
    while d < end:
        if d.weekday() < 5:
            yield d
        d += datetime.timedelta(days=1)


# ---------------------------------------------------------------------------
# Download & Decode
# ---------------------------------------------------------------------------

def download_hour_bi5(pair: str, dt: datetime.datetime, retries: int = 4) -> bytes | None:
    """Download a single hour's tick bi5 file with exponential backoff."""
    month_0idx = dt.month - 1
    url = (
        f"{BASE_URL}/{pair}/{dt.year}/{month_0idx:02d}/"
        f"{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"
    )
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        t0 = time.perf_counter()
        try:
            PERF.add("http_requests")
            resp = urllib.request.urlopen(req, timeout=30)
            data = resp.read()
            PERF.add("net_seconds", time.perf_counter() - t0)
            if len(data) == 0:
                PERF.add("http_empty")
                return None
            PERF.add("bytes_downloaded", len(data))
            return data
        except urllib.error.HTTPError as e:
            PERF.add("net_seconds", time.perf_counter() - t0)
            if e.code == 404:
                PERF.add("http_404")
                return None
            PERF.add("http_retries")
            if attempt < retries - 1:
                delay = 2 ** attempt + 1  # 2s, 3s, 5s, 9s
                time.sleep(delay)
                continue
            PERF.add("http_failures")
            log.debug(f"HTTP {e.code} after {retries} retries: {url}")
            return None
        except Exception as e:
            PERF.add("net_seconds", time.perf_counter() - t0)
            PERF.add("http_retries")
            if attempt < retries - 1:
                delay = 2 ** attempt + 1
                time.sleep(delay)
                continue
            PERF.add("http_failures")
            log.debug(f"Failed after {retries} retries: {url} — {e}")
            return None
    return None


# bi5 tick record laid out as a numpy structured dtype for vectorized decode.
TICK_DTYPE = np.dtype([
    ("ms_offset", ">i4"),
    ("ask_raw",   ">i4"),
    ("bid_raw",   ">i4"),
    ("ask_vol",   ">f4"),
    ("bid_vol",   ">f4"),
])

_EMPTY_TICK_DF = pd.DataFrame({
    "time":    pd.Series(dtype="datetime64[ns, UTC]"),
    "bid":     pd.Series(dtype=np.float64),
    "ask":     pd.Series(dtype=np.float64),
    "bid_vol": pd.Series(dtype=np.float32),
    "ask_vol": pd.Series(dtype=np.float32),
})


def decode_ticks(
    data: bytes,
    hour_start: datetime.datetime,
    point_divider: int,
) -> pd.DataFrame:
    """Decode LZMA bi5 tick data into a DataFrame (vectorized via numpy)."""
    t0 = time.perf_counter()
    try:
        decompressed = lzma.decompress(data)
    except lzma.LZMAError:
        PERF.add("decode_seconds", time.perf_counter() - t0)
        PERF.add("decode_errors")
        return _EMPTY_TICK_DF

    PERF.add("bytes_decompressed", len(decompressed))
    n_ticks = len(decompressed) // TICK_RECORD_SIZE
    if n_ticks == 0:
        PERF.add("decode_seconds", time.perf_counter() - t0)
        return _EMPTY_TICK_DF

    arr = np.frombuffer(decompressed, dtype=TICK_DTYPE, count=n_ticks)

    # Drop ticks with both volumes zero (matches prior behavior).
    mask = (arr["ask_vol"] != 0.0) | (arr["bid_vol"] != 0.0)
    if not mask.all():
        arr = arr[mask]
    if arr.size == 0:
        return _EMPTY_TICK_DF

    # Build timestamps vectorized: hour_start (naive UTC) + ms offsets, then tag UTC.
    hour_ns = np.datetime64(hour_start.replace(tzinfo=None), "ns")
    times = hour_ns + arr["ms_offset"].astype("timedelta64[ms]")

    bid = arr["bid_raw"].astype(np.float64) / point_divider
    ask = arr["ask_raw"].astype(np.float64) / point_divider

    df = pd.DataFrame({
        "time":    pd.DatetimeIndex(times).tz_localize("UTC"),
        "bid":     bid,
        "ask":     ask,
        "bid_vol": np.asarray(arr["bid_vol"], dtype=np.float32).copy(),
        "ask_vol": np.asarray(arr["ask_vol"], dtype=np.float32).copy(),
    })
    PERF.add("ticks_decoded", len(df))
    PERF.add("decode_seconds", time.perf_counter() - t0)
    return df


def _download_and_decode_hour(pair: str, day: datetime.date, hour: int, point_divider: int) -> pd.DataFrame:
    """Download + decode a single hour. Used as a thread target."""
    hour_start = datetime.datetime(
        day.year, day.month, day.day, hour,
        tzinfo=datetime.timezone.utc,
    )
    data = download_hour_bi5(pair, hour_start)
    if data is None:
        return _EMPTY_TICK_DF
    return decode_ticks(data, hour_start, point_divider)


def download_day_ticks(
    pair: str,
    day: datetime.date,
    point_divider: int,
) -> tuple[pd.DataFrame, int]:
    """Download all 24 hours for one day and return (M5 bars, tick_count).

    Resamples to M5 inside this function so callers never hold raw ticks for
    more than one day at a time — keeps memory flat regardless of date range.
    5-minute bars divide cleanly into a day, so per-day resample is identical
    to a global resample.
    """
    day_t0 = time.perf_counter()
    hour_frames: list[pd.DataFrame] = []

    with ThreadPoolExecutor(max_workers=HOUR_WORKERS) as pool:
        futures = {
            pool.submit(_download_and_decode_hour, pair, day, h, point_divider): h
            for h in range(24)
        }
        for future in as_completed(futures):
            try:
                tdf = future.result()
                if not tdf.empty:
                    hour_frames.append(tdf)
            except Exception as e:
                PERF.add("worker_exceptions")
                log.debug(f"hour worker exception {pair} {day}: {e}")

    if not hour_frames:
        PERF.record_day(time.perf_counter() - day_t0)
        return _EMPTY_M5_DF.copy(), 0

    ticks_df = pd.concat(hour_frames, ignore_index=True)
    tick_count = len(ticks_df)

    rs_t0 = time.perf_counter()
    m5 = ticks_to_m5(ticks_df)
    PERF.add("resample_seconds", time.perf_counter() - rs_t0)

    del ticks_df, hour_frames
    PERF.record_day(time.perf_counter() - day_t0)
    return m5, tick_count


# ---------------------------------------------------------------------------
# Tick -> OHLC Resampling
# ---------------------------------------------------------------------------

_M5_COLUMNS = ["time", "open", "high", "low", "close", "volume", "spread"]
_EMPTY_M5_DF = pd.DataFrame(columns=_M5_COLUMNS)


def ticks_to_m5(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a tick DataFrame to M5 OHLC bars with spread.

    Uses bid price for OHLC, mean (ask - bid) for spread, sum of bid+ask
    volumes for volume.
    """
    if df.empty:
        return _EMPTY_M5_DF.copy()

    df = df.set_index("time").sort_index()
    df["spread"] = df["ask"] - df["bid"]
    df["volume"] = df["bid_vol"] + df["ask_vol"]

    m5 = df.resample("5min").agg({
        "bid":    ["first", "max", "min", "last"],
        "volume": "sum",
        "spread": "mean",
    })
    m5.columns = ["open", "high", "low", "close", "volume", "spread"]
    m5 = m5.dropna(subset=["open"])

    for col in ("open", "high", "low", "close", "spread"):
        m5[col] = m5[col].round(5)
    m5["volume"] = m5["volume"].round(1)

    return m5.reset_index()


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample M5 DataFrame to a higher timeframe."""
    df2 = df.set_index("time")
    resampled = df2.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
        "spread": "mean",
    })
    resampled = resampled.dropna(subset=["open"])
    resampled["spread"] = resampled["spread"].round(5)
    resampled["volume"] = resampled["volume"].round(1)
    return resampled.reset_index().rename(columns={resampled.index.name or "time": "time"})


# ---------------------------------------------------------------------------
# Metadata (for incremental refresh)
# ---------------------------------------------------------------------------

def load_meta() -> dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text())
    return {}


def save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, indent=2, default=str))


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def download_symbol(
    pair: str,
    start: datetime.date,
    end: datetime.date,
    incremental: bool = False,
    symbol_idx: int = 0,
    total_symbols: int = 1,
) -> pd.DataFrame | None:
    """Download tick data for a symbol, resample to M5, and save all timeframes."""
    point_divider = get_point_divider(pair)
    meta = load_meta()

    # Incremental: adjust start date
    existing_m5 = COMPILED_DIR / f"{pair}_M5.csv"
    existing_df = None
    if incremental and pair in meta and existing_m5.exists():
        last_date = datetime.date.fromisoformat(meta[pair]["last_date"])
        if last_date >= end - datetime.timedelta(days=1):
            log.info(f"  [{pair}] Already up to date ({last_date})")
            status_update(
                current_symbol=pair,
                symbol_progress=f"{symbol_idx}/{total_symbols}",
                symbol_status="up_to_date",
            )
            return pd.read_csv(existing_m5, parse_dates=["time"])
        start = last_date + datetime.timedelta(days=1)
        existing_df = pd.read_csv(existing_m5, parse_dates=["time"])
        log.info(f"  [{pair}] Incremental: {start} -> {end} (have data through {last_date})")

    days = list(_trading_days(start, end))
    if not days:
        log.info(f"  [{pair}] No trading days in range")
        return existing_df

    log.info(f"  [{pair}] Downloading {len(days)} trading days ({days[0]} -> {days[-1]})...")
    status_update(
        current_symbol=pair,
        symbol_progress=f"{symbol_idx}/{total_symbols}",
        symbol_status="downloading",
        days_total=len(days),
        days_completed=0,
        days_failed=0,
        ticks_total=0,
    )

    # Reset perf counters so this symbol's totals are clean.
    PERF.reset()
    rss_at_start_mb = rss_peak_mb()
    rss_at_last_log_mb = rss_at_start_mb
    log.info(f"    [{pair}] RSS at start: {rss_at_start_mb:.0f} MB (peak)")

    m5_frames: list[pd.DataFrame] = []
    completed = 0
    failed_days = 0
    ticks_total = 0
    sym_t0 = time.time()

    with ThreadPoolExecutor(max_workers=DAY_WORKERS) as pool:
        futures = {
            pool.submit(download_day_ticks, pair, day, point_divider): day
            for day in days
        }
        for future in as_completed(futures):
            day = futures[future]
            try:
                day_m5, day_tick_count = future.result()
                if not day_m5.empty:
                    m5_frames.append(day_m5)
                ticks_total += day_tick_count
                completed += 1

                if completed % 20 == 0 or completed == len(days):
                    elapsed = time.time() - sym_t0
                    rate = completed / elapsed if elapsed > 0 else 0
                    eta_s = (len(days) - completed) / rate if rate > 0 else 0
                    eta_m = eta_s / 60
                    snap = PERF.snapshot()
                    rss_mb = rss_peak_mb()
                    rss_delta = rss_mb - rss_at_last_log_mb
                    rss_at_last_log_mb = rss_mb
                    mb_dl = snap.get("bytes_downloaded", 0) / (1024 ** 2)
                    net_s = snap.get("net_seconds", 0)
                    dec_s = snap.get("decode_seconds", 0)
                    res_s = snap.get("resample_seconds", 0)
                    retries = int(snap.get("http_retries", 0))
                    failures = int(snap.get("http_failures", 0))
                    day_p95 = snap.get("day_p95_s", 0)
                    day_max = snap.get("day_max_s", 0)

                    log.info(
                        f"    [{pair}] {completed}/{len(days)} days  "
                        f"{ticks_total:,} ticks  "
                        f"{rate:.1f} days/s  "
                        f"ETA {eta_m:.0f}m  "
                        f"RSS {rss_mb:.0f}MB (+{rss_delta:.0f})  "
                        f"DL {mb_dl:.0f}MB  "
                        f"net/dec/res {net_s:.0f}/{dec_s:.0f}/{res_s:.0f}s  "
                        f"retries={retries} fail={failures}  "
                        f"day p95/max {day_p95:.1f}/{day_max:.1f}s"
                    )
                    status_update(
                        days_completed=completed,
                        days_failed=failed_days,
                        ticks_total=ticks_total,
                        rate_days_per_sec=round(rate, 2),
                        eta_minutes=round(eta_m, 1),
                        rss_peak_mb=round(rss_mb, 1),
                        bytes_downloaded_mb=round(mb_dl, 1),
                        net_seconds=round(net_s, 1),
                        decode_seconds=round(dec_s, 1),
                        resample_seconds=round(res_s, 1),
                        http_retries=retries,
                        http_failures=failures,
                        day_p95_seconds=round(day_p95, 2),
                        day_max_seconds=round(day_max, 2),
                    )

                log.debug(f"    [{pair}] {day}: {day_tick_count:,} ticks")

            except Exception as e:
                failed_days += 1
                log.warning(f"    [{pair}] Failed day {day}: {e}")

    if failed_days:
        log.warning(f"    [{pair}] {failed_days}/{len(days)} days failed")

    log.info(f"  [{pair}] Combining {len(m5_frames)} days of M5 bars...")
    status_update(symbol_status="combining")
    concat_t0 = time.perf_counter()
    if m5_frames:
        m5_df = pd.concat(m5_frames, ignore_index=True).sort_values("time").reset_index(drop=True)
    else:
        m5_df = _EMPTY_M5_DF.copy()
    del m5_frames
    concat_seconds = time.perf_counter() - concat_t0
    log.info(f"    [{pair}] Concat+sort done in {concat_seconds:.1f}s, RSS {rss_peak_mb():.0f}MB peak")

    # End-of-symbol perf summary
    snap = PERF.snapshot()
    sym_elapsed = time.time() - sym_t0
    log.info(
        f"    [{pair}] PERF summary: "
        f"elapsed {sym_elapsed/60:.1f}m  "
        f"DL {_fmt_bytes(snap.get('bytes_downloaded', 0))} "
        f"(decompressed {_fmt_bytes(snap.get('bytes_decompressed', 0))})  "
        f"ticks {int(snap.get('ticks_decoded', 0)):,}  "
        f"net {snap.get('net_seconds', 0):.0f}s  "
        f"decode {snap.get('decode_seconds', 0):.0f}s  "
        f"resample {snap.get('resample_seconds', 0):.0f}s  "
        f"concat {concat_seconds:.1f}s  "
        f"http: {int(snap.get('http_requests', 0))} req / "
        f"{int(snap.get('http_404', 0))} 404 / "
        f"{int(snap.get('http_retries', 0))} retries / "
        f"{int(snap.get('http_failures', 0))} fail  "
        f"day p50/p95/max {snap.get('day_p50_s', 0):.1f}/{snap.get('day_p95_s', 0):.1f}/{snap.get('day_max_s', 0):.1f}s  "
        f"RSS peak {rss_peak_mb():.0f}MB (Δ +{rss_peak_mb() - rss_at_start_mb:.0f})"
    )

    if m5_df.empty and existing_df is None:
        log.warning(f"  [{pair}] No data retrieved")
        return None

    # Merge with existing data if incremental
    if existing_df is not None and not m5_df.empty:
        m5_df = pd.concat([existing_df, m5_df], ignore_index=True)
        m5_df = m5_df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    elif existing_df is not None:
        m5_df = existing_df

    # Save M5
    status_update(symbol_status="saving")
    m5_path = COMPILED_DIR / f"{pair}_M5.csv"
    m5_df.to_csv(m5_path, index=False)

    # Resample and save higher timeframes
    for tf_name, rule in [("H1", "1h"), ("H4", "4h"), ("D1", "1D")]:
        tf_df = resample_ohlc(m5_df, rule)
        tf_path = COMPILED_DIR / f"{pair}_{tf_name}.csv"
        tf_df.to_csv(tf_path, index=False)
        log.debug(f"  [{pair}] {tf_name}: {len(tf_df):,} bars -> {tf_path}")

    # Update metadata
    last_ts = m5_df["time"].max()
    last_date_str = str(last_ts.date() if hasattr(last_ts, "date") else pd.Timestamp(last_ts).date())
    meta[pair] = {
        "last_date": last_date_str,
        "bars_m5": len(m5_df),
        "updated": str(datetime.date.today()),
    }
    save_meta(meta)

    date_range = f"{m5_df['time'].min()} -> {m5_df['time'].max()}"
    unique_days = m5_df["time"].dt.date.nunique()
    log.info(f"  [{pair}] Done: {len(m5_df):,} M5 bars, {unique_days} days  ({date_range})")
    status_update(
        symbol_status="completed",
        bars_m5=len(m5_df),
        unique_days=unique_days,
    )

    return m5_df


def run(
    symbols: list[str],
    years: int = DEFAULT_YEARS,
    incremental: bool = False,
    start_override: datetime.date | None = None,
    end_override: datetime.date | None = None,
):
    """Run the full download pipeline for all symbols."""
    end = end_override or (datetime.date.today() - datetime.timedelta(days=1))
    start = start_override or (end - datetime.timedelta(days=365 * years))

    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    header = (
        f"{'='*60}\n"
        f"Dukascopy Direct Tick Downloader\n"
        f"{'='*60}\n"
        f"  Symbols:     {', '.join(symbols)}\n"
        f"  Range:       {start} -> {end}\n"
        f"  Incremental: {incremental}\n"
        f"  Day workers: {DAY_WORKERS}  |  Hour workers: {HOUR_WORKERS}\n"
        f"  Output:      {COMPILED_DIR}/\n"
        f"  Log:         {LOG_DIR}/\n"
        f"  Status:      {STATUS_FILE}\n"
        f"{'='*60}"
    )
    log.info(header)

    status_update(
        state="running",
        symbols=symbols,
        date_range=f"{start} -> {end}",
        incremental=incremental,
        started=datetime.datetime.now().isoformat(timespec="seconds"),
        symbols_completed=[],
        symbols_remaining=list(symbols),
    )

    frames = []
    t0 = time.time()
    completed_symbols = []

    for i, sym in enumerate(symbols, 1):
        sym_t0 = time.time()
        status_update(symbols_remaining=[s for s in symbols if s not in completed_symbols and s != sym])

        df = download_symbol(sym, start, end, incremental=incremental,
                             symbol_idx=i, total_symbols=len(symbols))
        elapsed = time.time() - sym_t0

        if df is not None:
            frames.append((sym, df))
            completed_symbols.append(sym)
            log.info(f"  [{sym}] Finished in {elapsed/60:.1f}m\n")
        else:
            log.warning(f"  [{sym}] No data\n")

        status_update(symbols_completed=completed_symbols)

    # Combined M5 file
    if frames:
        log.info("Building combined all_pairs_M5.csv...")
        combined = pd.concat(
            [df.assign(symbol=sym) for sym, df in frames],
            ignore_index=True,
        ).sort_values(["symbol", "time"]).reset_index(drop=True)
        combined_path = COMPILED_DIR / "all_pairs_M5.csv"
        combined.to_csv(combined_path, index=False)

    total = time.time() - t0
    total_m = total / 60

    summary_lines = [
        f"\n{'='*60}",
        f"COMPLETE  ({total_m:.1f} minutes total)",
        f"{'='*60}",
    ]
    for sym, df in frames:
        date_min = df["time"].min()
        date_max = df["time"].max()
        unique_days = df["time"].dt.date.nunique()
        summary_lines.append(
            f"  {sym:<10} {len(df):>9,} M5 bars  {unique_days:>5} days   {date_min} -> {date_max}"
        )
    summary_lines.append(f"\n  Timeframes: M5, H1, H4, D1")
    summary_lines.append(f"  Files in:   {COMPILED_DIR}/")

    summary = "\n".join(summary_lines)
    log.info(summary)

    status_update(
        state="completed",
        total_minutes=round(total_m, 1),
        finished=datetime.datetime.now().isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Dukascopy tick data and compile to OHLC bars",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=SYMBOLS,
        help=f"Pairs to download (default: {' '.join(SYMBOLS)})",
    )
    parser.add_argument(
        "--years", type=int, default=DEFAULT_YEARS,
        help=f"Years of history (default: {DEFAULT_YEARS})",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date override (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date override (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="Only download new data since last run",
    )
    args = parser.parse_args()

    start = datetime.date.fromisoformat(args.start) if args.start else None
    end = datetime.date.fromisoformat(args.end) if args.end else None

    run(
        symbols=[s.upper() for s in args.symbols],
        years=args.years,
        incremental=args.incremental,
        start_override=start,
        end_override=end,
    )


if __name__ == "__main__":
    main()
