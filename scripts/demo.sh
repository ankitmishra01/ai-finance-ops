#!/usr/bin/env bash
# Dry-run all five agents on the synthetic data and print a compact summary of each.
# Nothing is approved, posted, paid or filed. Full output of each run: out/demo/<module>.log
#
#   ./scripts/demo.sh            # rules extractor, no API key needed
#   ./scripts/demo.sh -v         # also print every full run
set -euo pipefail
cd "$(dirname "$0")/.."
export AFO_LLM_MODE="${AFO_LLM_MODE:-replay}"
VERBOSE="${1:-}"
LOGS=out/demo
mkdir -p "$LOGS"

run() {  # run <module> <summary-filter...>
  local mod="$1"; shift
  local log="$LOGS/$mod.log"
  printf '\n\033[1m== %s\033[0m\n' "$mod"
  if ! uv run --quiet python "$mod/run.py" >"$log" 2>&1; then
    echo "FAILED, see $log"; tail -20 "$log"; return 1
  fi
  if [[ "$VERBOSE" == "-v" ]]; then cat "$log"; else "$@" <"$log"; fi
}

run 01_invoicing  grep -E '^(Invoicing|[0-9]+ invoices)|DRY RUN'
run 02_bills      grep -E '^(Read [0-9]+|  CAD:|  USD:|[0-9]+ bills proposed)|Dry run'
run 03_payroll    grep -E '^(Pay run|[0-9]+ adjustments|ID |     TOTAL|Remittance)|Dry run'
run 04_gst_hst    grep -E 'classifier|line 10[159]|line 115|due 20|flagged for review|Dry run'
run 05_bookkeeping grep -E '^(Bookkeeping|[0-9]+ transactions|Proposals:)|^  [A-Z][a-z].*: [0-9]+$|Dry run'

printf '\nToken log (live/record runs only): runs/token_log.jsonl\nFull logs: %s/\n' "$LOGS"
