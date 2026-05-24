#!/bin/bash
# Download 20y data for USDCAD, EURJPY, GBPJPY in 5-year chunks.
# Each chunk compiles separately, then we merge at the end.
# This avoids the OOM that killed the full 20y single-pass download.

set -e
cd /Users/saleem-latif/forex/duka-data
CHUNKS_DIR=compiled/chunks
mkdir -p "$CHUNKS_DIR"

PAIRS="USDCAD EURJPY GBPJPY"
RANGES="2006-04-17:2011-04-01 2011-04-01:2016-04-01 2016-04-01:2021-04-01"
LABELS="2006_2011 2011_2016 2016_2021"

for PAIR in $PAIRS; do
    echo "========================================"
    echo "  PAIR: $PAIR"
    echo "========================================"

    i=0
    for RANGE in $RANGES; do
        START="${RANGE%%:*}"
        END="${RANGE##*:}"
        LABEL=$(echo "$LABELS" | cut -d' ' -f$((i+1)))
        i=$((i+1))

        # Skip if chunk already exists
        if [ -f "$CHUNKS_DIR/${PAIR}_M5_${LABEL}.csv" ]; then
            echo "  Chunk $LABEL already exists, skipping"
            continue
        fi

        echo "  Downloading $PAIR $START -> $END ..."
        python3 download.py --symbols "$PAIR" --start "$START" --end "$END"

        # Back up compiled files
        for TF in M5 H1 H4 D1; do
            cp "compiled/${PAIR}_${TF}.csv" "$CHUNKS_DIR/${PAIR}_${TF}_${LABEL}.csv"
        done
        echo "  Chunk $LABEL backed up"
    done

    # Back up existing 2021-2026 data if not already done
    if [ ! -f "$CHUNKS_DIR/${PAIR}_M5_2021_2026.csv" ]; then
        # The current compiled files might be from the last chunk download.
        # We need the original 2021-2026 data. Re-download it.
        echo "  Downloading $PAIR 2021-04-01 -> 2026-04-21 ..."
        python3 download.py --symbols "$PAIR" --start "2021-04-01" --end "2026-04-21"
        for TF in M5 H1 H4 D1; do
            cp "compiled/${PAIR}_${TF}.csv" "$CHUNKS_DIR/${PAIR}_${TF}_2021_2026.csv"
        done
        echo "  Chunk 2021_2026 backed up"
    fi

    # Merge all chunks
    echo "  Merging all chunks for $PAIR ..."
    python3 -c "
import pandas as pd
from pathlib import Path
CHUNKS = Path('compiled/chunks')
OUT = Path('compiled')
pair = '$PAIR'
for tf in ['M5', 'H1', 'H4', 'D1']:
    files = sorted(CHUNKS.glob(f'{pair}_{tf}_*.csv'))
    dfs = [pd.read_csv(f, parse_dates=['time']) for f in files]
    merged = pd.concat(dfs, ignore_index=True).drop_duplicates(subset='time').sort_values('time').reset_index(drop=True)
    merged.to_csv(OUT / f'{pair}_{tf}.csv', index=False)
    print(f'  {pair}_{tf}: {len(merged):,} bars ({merged.time.min()} -> {merged.time.max()})')
"
    echo "  $PAIR complete!"
    echo
done

echo "ALL DONE"
