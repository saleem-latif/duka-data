#!/usr/bin/env bash
# Waits for the in-progress 20y download (the GBPJPY run) to finish, then
# downloads the additional research instruments at full 20y depth:
#   - AUDJPY      : full rebuild (currently only 5y on disk)
#   - XAUUSD      : gold        (divider 1000, added to download.py)
#   - LIGHTCMDUSD : WTI crude   (divider 1000, added to download.py)
#
# Runs sequentially AFTER the current download so the two don't compete for
# Dukascopy's rate limit. Launched detached via nohup; logs to queued_extra.log.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/logs/queued_extra.log"
WAIT_PID="${1:-23679}"          # PID of the running download to wait on

cd "$DIR" || exit 1

echo "[$(date '+%F %T')] queue script started; waiting on PID $WAIT_PID to finish..." >> "$LOG"
while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
done
echo "[$(date '+%F %T')] PID $WAIT_PID exited; starting extra downloads." >> "$LOG"

python3 -u download.py \
    --symbols AUDJPY XAUUSD LIGHTCMDUSD \
    --start 2006-05-24 --end 2026-05-19 \
    >> "$LOG" 2>&1

echo "[$(date '+%F %T')] extra downloads finished (exit $?)." >> "$LOG"
