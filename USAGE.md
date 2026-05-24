# duka-data — Dukascopy Tick Downloader & OHLC Compiler

Direct bi5 tick downloader that fetches raw tick data from Dukascopy's public datafeed, decodes LZMA-compressed bi5 files, and resamples into M5/H1/H4/D1 OHLC bars with bid/ask spread data.

No external trading libraries required — only pandas and numpy.

## Quick Start

```bash
pip install -r requirements.txt
python3 download.py
```

This downloads 5 years of data for 10 major/cross pairs across 4 timeframes.

## CLI Options

```bash
# Full 5-year download (default)
python3 download.py

# Specific pairs
python3 download.py --symbols EURUSD GBPUSD USDJPY

# Custom history length
python3 download.py --years 2

# Custom date range
python3 download.py --start 2020-01-01 --end 2025-01-01

# Incremental update (only fetch new days since last run)
python3 download.py --incremental
```

### Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--symbols` | All 10 pairs | Space-separated list of pairs to download |
| `--years` | 5 | Years of history to fetch |
| `--start` | Computed from `--years` | Start date (YYYY-MM-DD), overrides `--years` |
| `--end` | Yesterday | End date (YYYY-MM-DD) |
| `--incremental` | Off | Only download days after last successful run |

### Default Symbols

**Majors:** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD
**Crosses:** EURGBP, EURJPY, GBPJPY

## Output

### Directory Structure

```
duka-data/
├── download.py
├── requirements.txt
├── .download_meta.json      # Tracks last download date per symbol (for --incremental)
├── compiled/
│   ├── EURUSD_M5.csv        # 5-minute bars
│   ├── EURUSD_H1.csv        # 1-hour bars (resampled from M5)
│   ├── EURUSD_H4.csv        # 4-hour bars (resampled from M5)
│   ├── EURUSD_D1.csv        # Daily bars (resampled from M5)
│   ├── ...                  # Same for all other pairs
│   └── all_pairs_M5.csv     # Combined M5 file with symbol column
└── raw/                     # (legacy, not used by new downloader)
```

### CSV Format

All compiled CSVs share the same column format:

```csv
time,open,high,low,close,volume,spread
2025-06-02 00:00:00+00:00,1.13527,1.13604,1.13520,1.13591,2040.8,0.00007
```

| Column | Description |
|--------|-------------|
| `time` | Bar open time in UTC (ISO 8601) |
| `open` | Opening bid price (5 decimal places) |
| `high` | Highest bid price in bar |
| `low` | Lowest bid price in bar |
| `close` | Closing bid price |
| `volume` | Total volume (bid_vol + ask_vol) |
| `spread` | Mean bid-ask spread during bar (5 decimal places) |

### Timeframe Resampling

All higher timeframes (H1, H4, D1) are resampled directly from M5 data:

- **open** = first M5 open in period
- **high** = max M5 high in period
- **low** = min M5 low in period
- **close** = last M5 close in period
- **volume** = sum of M5 volumes
- **spread** = mean of M5 spreads

This guarantees price consistency across all timeframes (no mixed data sources).

## Data Source Details

### Dukascopy bi5 Tick Format

- **URL pattern:** `http://datafeed.dukascopy.com/datafeed/{PAIR}/{year}/{month_0idx}/{day}/{hour}h_ticks.bi5`
- **Month indexing:** 0-based (January = 00, December = 11)
- **Compression:** LZMA (despite .bi5 extension)
- **Record size:** 20 bytes per tick (big-endian)

| Bytes | Type | Field |
|-------|------|-------|
| 0-3 | int32 | Milliseconds offset from hour start |
| 4-7 | int32 | Ask price (raw integer) |
| 8-11 | int32 | Bid price (raw integer) |
| 12-15 | float32 | Ask volume |
| 16-19 | float32 | Bid volume |

**Price conversion:** Divide raw integer by point divider:
- JPY pairs (USDJPY, EURJPY, GBPJPY, etc.): divide by 1,000
- Standard pairs: divide by 100,000

### Coverage

- **History:** Back to 2003+ for major pairs
- **Hours:** Full 24h session (Sunday open to Friday close, UTC)
- **Weekends:** Automatically skipped (Mon-Fri only)
- **Holidays:** Empty hours are silently skipped

## Incremental Mode

The `--incremental` flag enables efficient daily updates:

1. Reads `.download_meta.json` to find the last downloaded date per symbol
2. Only fetches days after that date
3. Merges new M5 bars with existing data (deduplicates on timestamp)
4. Re-resamples all higher timeframes from the full M5 dataset
5. Updates the metadata file

Typical workflow:

```bash
# Initial full download
python3 download.py --years 5

# Daily update (e.g., via cron)
python3 download.py --incremental
```

## Monitoring

### Real-time status file

While running, the downloader writes `.download_status.json` in the project root. Poll it to see progress:

```bash
# One-shot check
cat .download_status.json | python3 -m json.tool

# Live watch (updates every ~20 days of progress)
watch -n5 cat .download_status.json
```

Example status:

```json
{
  "state": "running",
  "current_symbol": "EURUSD",
  "symbol_progress": "1/10",
  "symbol_status": "downloading",
  "days_total": 1303,
  "days_completed": 400,
  "days_failed": 0,
  "ticks_total": 36501465,
  "rate_days_per_sec": 2.1,
  "eta_minutes": 7.2,
  "symbols_completed": [],
  "symbols_remaining": ["GBPUSD", "USDJPY", "..."]
}
```

### Log files

Detailed logs are written to `logs/download_YYYY-MM-DD.log`:

```bash
# Follow the log
tail -f logs/download_2026-04-01.log
```

The log contains:
- **INFO** — progress milestones (every 20 days), symbol completion, summary
- **DEBUG** — every individual day's tick count, timeframe file writes
- **WARNING** — failed days, missing data

### Metadata file

`.download_meta.json` is updated after each symbol completes:

```bash
cat .download_meta.json
```

Shows last downloaded date, M5 bar count, and update timestamp per symbol.

## Performance

- **Parallelism:** 8 days downloading in parallel, each day downloads 6 hours in parallel
- **Rate:** ~2-3 days/second (~120-180 days/minute)
- **Storage:** ~5-10 MB per pair-year at M5 resolution
- **Full 5-year run (10 pairs):** ~1-2 hours depending on network

## Dependencies

```
pandas>=2.0
numpy>=1.24
```

All other imports are Python standard library (lzma, struct, urllib, json, concurrent.futures, logging).
