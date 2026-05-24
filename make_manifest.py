"""
make_manifest.py — Inventory the local Dukascopy dataset
========================================================
Scans compiled/ (and raw/) and writes DATA_MANIFEST.md: a human-readable
catalogue of what data is actually present on disk — per symbol/timeframe
row counts, date ranges, and file sizes — plus the live download status.

The DATA *itself* is gitignored (regenerable from download.py), so this
manifest is too: it describes one machine's local holdings, not repo content.
Re-run after any download to refresh it:

    python3 make_manifest.py

Reads metadata cheaply — counts newlines and seeks to the first/last data
row rather than loading the (multi-hundred-MB) CSVs into memory.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

BASE_DIR     = Path(__file__).parent
COMPILED_DIR = BASE_DIR / "compiled"
RAW_DIR      = BASE_DIR / "raw"
META_FILE    = BASE_DIR / ".download_meta.json"
STATUS_FILE  = BASE_DIR / ".download_status.json"
MANIFEST     = BASE_DIR / "DATA_MANIFEST.md"

TIMEFRAMES = ["M5", "H1", "H4", "D1"]


def count_data_rows(path: Path) -> int:
    """Number of data rows (lines minus the header), counted via newlines."""
    n = 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            n += chunk.count(b"\n")
    return max(n - 1, 0)  # subtract header


def first_last_date(path: Path) -> tuple[str, str]:
    """First and last bar timestamps (date portion), without reading the whole file."""
    with path.open("rb") as f:
        f.readline()                      # header
        first = f.readline().decode("utf-8", "replace").strip()
        # Seek near the end and grab the last non-empty line.
        size = path.stat().st_size
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8", "replace").splitlines()
        last = next((ln for ln in reversed(tail) if ln.strip()), "")
    fd = first.split(",", 1)[0].split(" ")[0] if first else "?"
    ld = last.split(",", 1)[0].split(" ")[0] if last else "?"
    return fd, ld


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GiB"


def discover_symbols() -> dict[str, dict[str, Path]]:
    """Map symbol -> {timeframe -> csv path} from compiled/ filenames."""
    symbols: dict[str, dict[str, Path]] = {}
    if not COMPILED_DIR.is_dir():
        return symbols
    for csv in sorted(COMPILED_DIR.glob("*.csv")):
        stem = csv.stem                     # e.g. EURUSD_M5  or  LIGHTCMDUSD_D1
        if "_" not in stem or stem.startswith("all_pairs"):
            continue                        # skip the combined all_pairs_M5.csv
        sym, tf = stem.rsplit("_", 1)
        if tf not in TIMEFRAMES:
            continue
        symbols.setdefault(sym, {})[tf] = csv
    return symbols


def main() -> None:
    symbols = discover_symbols()
    meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {}
    status = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}

    lines: list[str] = []
    lines.append("# Data Manifest")
    lines.append("")
    lines.append(f"_Generated {datetime.datetime.now().isoformat(timespec='seconds')} "
                 f"by `make_manifest.py`. Local inventory — data is gitignored._")
    lines.append("")

    # Live download status, if any.
    state = status.get("state")
    if state:
        cur = status.get("current_symbol", "")
        prog = status.get("symbol_progress", "")
        sstat = status.get("symbol_status", "")
        lines.append(f"**Download status:** `{state}`"
                     + (f" — {cur} ({prog}, {sstat})" if cur else ""))
        lines.append("")

    if not symbols:
        lines.append("_No compiled data found in `compiled/`._")
        MANIFEST.write_text("\n".join(lines) + "\n")
        print(f"Wrote {MANIFEST} (no data found)")
        return

    # Per-symbol summary, keyed on D1 (or first available tf) for date range.
    lines.append(f"## Symbols ({len(symbols)})")
    lines.append("")
    lines.append("| Symbol | Timeframes | D1 bars | Coverage (D1) | Total size | Meta updated |")
    lines.append("|--------|-----------|--------:|---------------|-----------:|--------------|")

    total_bytes = 0
    detail_blocks: list[str] = []

    for sym in sorted(symbols):
        tfs = symbols[sym]
        present = [tf for tf in TIMEFRAMES if tf in tfs]
        sym_bytes = sum(p.stat().st_size for p in tfs.values())
        total_bytes += sym_bytes

        ref_tf = "D1" if "D1" in tfs else present[0]
        d1_rows = count_data_rows(tfs[ref_tf])
        fd, ld = first_last_date(tfs[ref_tf])
        updated = meta.get(sym, {}).get("updated", "—")

        lines.append(
            f"| {sym} | {', '.join(present)} | {d1_rows:,} | {fd} → {ld} "
            f"| {human_size(sym_bytes)} | {updated} |"
        )

        # Detailed per-timeframe block.
        block = [f"### {sym}", "", "| TF | Rows | Coverage | Size |", "|----|-----:|----------|-----:|"]
        for tf in present:
            p = tfs[tf]
            rows = count_data_rows(p)
            f0, f1 = first_last_date(p)
            block.append(f"| {tf} | {rows:,} | {f0} → {f1} | {human_size(p.stat().st_size)} |")
        detail_blocks.append("\n".join(block))

    lines.append("")
    lines.append(f"**Total compiled size:** {human_size(total_bytes)}")
    lines.append("")

    # Raw tick dumps, if present.
    if RAW_DIR.is_dir():
        raw_files = sorted(RAW_DIR.glob("*.csv"))
        if raw_files:
            raw_bytes = sum(p.stat().st_size for p in raw_files)
            lines.append(f"## Raw (`raw/`) — {len(raw_files)} files, {human_size(raw_bytes)}")
            lines.append("")
            for p in raw_files:
                lines.append(f"- `{p.name}` ({human_size(p.stat().st_size)})")
            lines.append("")

    lines.append("## Per-timeframe detail")
    lines.append("")
    lines.extend(b + "\n" for b in detail_blocks)

    MANIFEST.write_text("\n".join(lines) + "\n")
    print(f"Wrote {MANIFEST} — {len(symbols)} symbols, {human_size(total_bytes)} total")


if __name__ == "__main__":
    main()
