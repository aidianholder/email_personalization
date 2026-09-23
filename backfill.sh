#!/usr/bin/env bash
# Backfill historical ADG stories in weekly chunks.
#   ./backfill.sh 90                          # last 90 days
#   ./backfill.sh --from 2025-08-01 --to 2025-11-19
# Idempotent: re-running skips stories already fetched and embedded.
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
RUN="$PY manage.py"
FROM=""; TO=""

if [[ "${1:-}" == "--from" ]]; then
    FROM="$2"; TO="${4:-$(date +%Y-%m-%d)}"
else
    DAYS="${1:-90}"
    FROM=$(date -v-${DAYS}d +%Y-%m-%d)
    TO=$(date +%Y-%m-%d)
fi

# Convert to epoch seconds so chunking is plain arithmetic.
cur=$(date -j -f "%Y-%m-%d" "$FROM" +%s)
end=$(date -j -f "%Y-%m-%d" "$TO" +%s)
WEEK=$((7 * 86400))
FAILED=0

echo "backfilling $FROM .. $TO"
while ((cur < end)); do
    next=$((cur + WEEK)); ((next > end)) && next=$end
    start=$(date -j -f "%s" "$cur"  +%Y-%m-%dT00:00:00)
    stop=$(date  -j -f "%s" "$next" +%Y-%m-%dT00:00:00)
    echo "=== $start .. $stop ==="
    # Corpus mean is skipped per chunk and computed once at the end.
    if ! $RUN run --since "$start" --until "$stop" --no-corpus-mean; then
        echo "!!! chunk failed: $start .. $stop"
        FAILED=$((FAILED + 1))
    fi
    cur=$next
done

echo "=== recomputing corpus mean ==="
$RUN corpus_mean
echo "=== backfill done ($FAILED failed chunks) ==="
