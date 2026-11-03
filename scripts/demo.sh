#!/usr/bin/env bash
# Dry-run all five agents on the synthetic data and print a compact summary of each.
# Nothing is approved, posted, paid or filed. Full output of each run: out/demo/<name>.log
#
#   ./scripts/demo.sh            # rules extractor, no API key needed
#   ./scripts/demo.sh -v         # also print every full run
set -euo pipefail
cd "$(dirname "$0")/.."
export AFO_LLM_MODE="${AFO_LLM_MODE:-replay}"
VERBOSE="${1:-}"
LOGS=out/demo
mkdir -p "$LOGS"

run() {  # [NAME=.. SCRIPT=.. ARGS=..] run <module> <summary-filter...>
  local mod="$1"; shift
  local name="${NAME:-$mod}"
  local log="$LOGS/$name.log"
  printf '\n\033[1m== %s\033[0m\n' "$name"
  # shellcheck disable=SC2086
  if ! uv run --quiet python "$mod/${SCRIPT:-run.py}" ${ARGS:-} >"$log" 2>&1; then
    echo "FAILED, see $log"; tail -20 "$log"; return 1
  fi
  if [[ "$VERBOSE" == "-v" ]]; then cat "$log"; else "$@" <"$log"; fi
}

run 01_invoicing  grep -E '^(Invoicing|[0-9]+ invoices)|DRY RUN'
run 02_bills      grep -E '^(Read [0-9]+|  CAD:|  USD:|[0-9]+ bills proposed)|Dry run'
run 03_payroll    grep -E '^(Pay run|[0-9]+ adjustments|ID |     TOTAL|Remittance)|Dry run'
NAME=03_payroll_montreal ARGS="--data data/synthetic/payroll_qc --out out/payroll_qc" \
  run 03_payroll  grep -E '^(Pay run|[0-9]+ adjustments|ID |     TOTAL|Remittance)|Dry run'
NAME=03_payroll_yearend_montreal SCRIPT=payroll_yearend.py ARGS="--data data/synthetic/payroll_qc" \
  run 03_payroll  grep -E '^(Year-end|[0-9]+ pay runs|T4 slips|RL-1 slips|ROE|Outputs)'
run 04_gst_hst    grep -E 'classifier|line 10[159]|line 115|QST line 209|due 20|flagged for review|Dry run'
run 05_bookkeeping grep -E '^(Bookkeeping|[0-9]+ transactions|Proposals:)|^  [A-Z][a-z].*: [0-9]+$|Dry run'

printf '\nToken log (live/record runs only): runs/token_log.jsonl\nFull logs: %s/\n' "$LOGS"
