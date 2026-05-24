# duka-data

Self-contained pipeline that downloads raw tick data from Dukascopy's public
datafeed, decodes the LZMA `.bi5` files, and compiles **M5 / H1 / H4 / D1** OHLC
bars (with bid/ask spread) for FX pairs, metals, energy, and indices.

Pure Python + pandas/numpy — no broker SDK or trading-platform dependency.

> **The data is not in this repo.** Compiled CSVs run to ~1.3 GB and exceed
> GitHub's 100 MiB/file limit, so `compiled/`, `raw/`, and `logs/` are
> gitignored. The data is fully reproducible from `download.py` — clone, install,
> and run the command below to regenerate it from source.

## Quick start

```bash
pip install -r requirements.txt

# Full 20-year history for the research universe:
python3 download.py \
  --symbols EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD \
            EURGBP EURJPY GBPJPY AUDJPY XAUUSD LIGHTCMDUSD \
  --start 2006-05-24 --end 2026-05-19
```

Output lands in `compiled/<SYMBOL>_<TF>.csv`. A full 20y / 13-symbol pull takes
many hours and ~1.5 GB — run it overnight.

Day-to-day, top up with:

```bash
python3 download.py --incremental
```

See **[USAGE.md](USAGE.md)** for the full CLI, CSV schema, bi5 format, and
monitoring details.

## Research universe

| Group | Symbols (Dukascopy names) |
|-------|---------------------------|
| FX majors | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD |
| FX crosses | EURGBP, EURJPY, GBPJPY, AUDJPY |
| Metals | XAUUSD (gold), XAGUSD (silver) |
| Energy | LIGHTCMDUSD (WTI), BRENTCMDUSD (Brent) |
| Indices | USA500IDXUSD (S&P 500), USA30IDXUSD (Dow), USATECHIDXUSD (Nasdaq 100) |

> **Note:** non-FX symbols use Dukascopy's CFD naming and a **1000** point-divider
> (3 decimals), wired up in `download.py` via `DIV_1000_INSTRUMENTS`. They are
> CFDs — expect session gaps and shorter history than FX (energy/index feeds
> generally don't reach back to 2006), so don't merge their bars naively with FX.

## Tooling

| File | Purpose |
|------|---------|
| `download.py` | Tick downloader + OHLC compiler (the core) |
| `check_integrity.py` | Validate compiled CSVs (gaps, ordering, bad bars) |
| `make_manifest.py` | Write `DATA_MANIFEST.md` — local inventory of what's on disk |
| `queue_extra_downloads.sh` | Wait for an in-flight run to finish, then fetch more symbols |
| `download_20y_batch.sh` | Batch helper for long multi-symbol pulls |

After any download, refresh the local inventory:

```bash
python3 make_manifest.py   # regenerates DATA_MANIFEST.md (gitignored)
```

## Data source

Dukascopy public datafeed (`datafeed.dukascopy.com`). This repo ships only the
downloader; redistributing Dukascopy's data is subject to their terms, which is
the other reason the compiled output stays out of version control.

## License & disclaimer

Released under the [MIT License](LICENSE) — provided **as-is, without warranty
of any kind**.

This is personal research tooling. It is **not** financial advice and carries
no guarantee of data accuracy or fitness for trading. You are responsible for
complying with [Dukascopy's terms of use](https://www.dukascopy.com/) when
accessing their datafeed; this project downloads data for personal/research use
only and does **not** redistribute it.
