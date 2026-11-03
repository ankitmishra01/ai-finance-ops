# ai-finance-ops

Five human-in-the-loop agents that do recurring finance admin for a Canadian small business: invoicing, bills, payroll, GST/HST and QST, and bookkeeping.

Claude extractors read the messy documents through schema-forced tool calls; tax, payroll and file-format maths is plain tested code (payroll matches the CRA's PDOC calculator to the cent on both cases checked), and nothing is posted, paid or filed without a person's approval.

345 tests and labelled evals run in CI with no API key. The published scores are for a deterministic rules baseline: Claude has not been scored yet ([how to score it](#how-to-score-claude)).

Each agent takes a recurring back-office job, does the reading, matching and drafting, and stops at an approval queue so a person decides what is posted, paid or filed.

All data in this repository is synthetic. Nothing here connects to a real bank, the CRA, or an accounting platform; those boundaries are stubbed behind small adapters. This is a reference build, not a deployed product.

## The five agents

| Module | Recurring job | What the agent does | What a person approves |
|---|---|---|---|
| [01_invoicing](01_invoicing/) | Month-end client invoicing | Finds billable clients in the ops data, reads billing changes from email, builds lines (hourly, retainer, milestone), applies GST/HST by place of supply plus QST, PST or RST where the supplier is registered, renders invoice PDFs | New customers, customer changes, each invoice |
| [02_bills](02_bills/) | Accounts payable intake | Classifies a shared finance mailbox, extracts bills from PDFs and email bodies, checks totals, tax rates and GST/HST and QST numbers, records QST/PST/RST separately, decides ITC and QST input tax refund eligibility, detects duplicates, buckets by due date | Each bill before it is posted |
| [03_payroll](03_payroll/) | Ontario pay run | Reads pay-change emails, computes gross pay and CPP/CPP2, EI, federal and Ontario tax with the CRA T4127 formulas, produces payslips, PD7A remittance summary, a sample CPA 005 payment file and the journal | Each pay line, then the run |
| [04_gst_hst](04_gst_hst/) | Quarterly GST/HST and QST returns | Applies a cited rules file to every transaction, classifies supply type and ITC treatment, computes the GST34 lines and the QST worksheet (Revenu Quebec FPZ-500 lines 203 to 209, with input tax refunds) with transaction-level traces, computes the filing deadline | Flagged transactions, then each return |
| [05_bookkeeping](05_bookkeeping/) | Records and evidence | Reads receipts, matches them to bank and card lines (FX, splits, tips), proposes recategorisations, files evidence by period, reports exceptions | Each evidence link and category change |

## What a run looks like

`./scripts/demo.sh` dry-runs all five agents on the synthetic data in about five seconds and prints a summary of each. The excerpts below were copied from a real run of `uv run python 03_payroll/run.py` and `uv run python 02_bills/run.py` (rules extractor, no API key, 3 November 2026; the bills run was re-captured after QST/PST support was added). Every flag is something the agent will not decide on its own.

```text
$ uv run python 03_payroll/run.py
Pay run 2026-11-01..2026-11-15, pay date 2026-11-13  (extractor: rules)
6 adjustments read from 6 emails

ID   Employee                  Gross    CPP+2      EI       Tax        Net  Flags
---------------------------------------------------------------------------------
E01  Danielle Johnson       3,250.00   184.70   52.98    566.18   2,446.14  
E02  Joshua Walker          6,041.67     0.00    0.00  1,635.23   4,406.44  
E03  Jill Rhodes            3,541.67   189.45    0.00    661.34   2,690.88  reaches CPP annual maximum this period (CPP 189.45)
E04  Patricia Miller        2,666.67   149.99   36.47    394.84   2,085.37  reaches EI annual maximum this period (EI 36.47)
E05  Jeffery Wagner         2,833.33   159.91   46.18    334.40   2,292.84  
E06  Anthony Gonzalez       5,458.33   218.33    0.00  1,202.27   4,037.73  duplicate bonus request in 05_bonus_fwd.eml (same as 04_bonus.eml); counted once; gross changes +38% vs normal period pay 3958.33 (threshold 20%)
E07  Debra Gardner          2,666.00   149.95   43.46    393.31   2,079.28  
E08  Jeffrey Lawrence       2,009.00   110.86   32.75    242.05   1,623.34  
E09  Lisa Smith             1,291.67    68.18   21.05    109.51   1,092.93  new hire: SIN missing or invalid; new hire: TD1/TD1ON not received; basic personal amounts used (claim code 1)
---------------------------------------------------------------------------------
     TOTAL                 29,758.34 1,231.37  232.89  5,539.13  22,754.95
Remittance if all lines approved: $8,560.80

Dry run. Nothing paid, filed or posted. Proposals in out/payroll/
Re-run with --approve to review, or --auto-approve for a demo.
```

```text
$ uv run python 02_bills/run.py
Read 40 messages with the rules extractor: 28 bill, 6 other, 3 receipt, 3 statement

file     vendor                         invoice                 total due        bucket               ITC  flags
01.eml   Ford Properties Ltd.           R-1101          CAD $3,955.00 2026-10-01 overdue          $455.00  -
03.eml   Fitzpatrick Sign Makers        SM-301            CAD $169.50 2026-11-09 due this week      $0.00  GST/HST number '87654321RT001' is not 9 digits + RT + 4 digits
04.eml   Young Marine Consulting        MC-118          CAD $1,035.00 2026-11-11 next 30 days       $0.00  tax rate inconsistent with vendor address: 15.00% charged; expected NS 14% or ON 13%
05.eml   Fowler Cloud Software Inc.     CS-20481          CAD $650.88 2026-11-14 next 30 days      $74.88  -
...
10.eml   Brown Print Shop               PS-6620           CAD $405.50 2026-11-20 next 30 days       $0.00  total mismatch: subtotal $350.00 + tax $45.50 != total $405.50
25.eml   Lewis Analytics Llc            US-2026-1031      USD $299.00 2026-10-31 overdue            $0.00  foreign currency: USD; convert at the Bank of Canada rate before posting
28.eml   Jones Cleaning Services        CL-1044           CAD $293.80 2026-12-01 next 30 days      $33.80  possible duplicate: same vendor, amount and dates as 24.eml
32.eml   Fowler Cloud Software Inc.     CS-20481          CAD $650.88 2026-11-14 next 30 days      $74.88  duplicate: same vendor and invoice number as 05.eml
...
38.eml   Taylor Imprimerie Inc.         IM-0931           CAD $517.39 2026-11-28 next 30 days      $22.50  QST number '123456789TQ001' is not 10 digits + TQ + 4 digits
...

AP aging as of 2026-11-03 (exact duplicates excluded):
  CAD: overdue $4,009.24, due this week $4,039.75, next 30 days $6,943.25, later $1,356.00
  USD: overdue $299.00, due this week $0.00, next 30 days $0.00, later $0.00
Provincial tax on bills: PST $27.60, QST $194.52; QST input tax refunds claimable $134.67 (PST and RST are a cost)

28 bills proposed, 12 flagged for a person.
```

Architecture, with diagrams of `shared/`, the approval gate and the token log: [docs/architecture.md](docs/architecture.md).

## Where things stand

Every model step has two implementations: a deterministic rules baseline, which runs in tests and CI, and a Claude extractor behind a forced JSON-schema tool call. The numbers below are for the **rules baseline only**, computed by `evals/run_all.py` on synthetic labelled sets. Because the rules and the synthetic documents were written together, the scores on familiar formats are flattering; the held-out or unfamiliar-phrasing figures are the honest baseline that the Claude extractor is meant to beat. Claude has not been scored yet and no model responses have been recorded or invented; run `AFO_LLM_MODE=record` with an API key to produce both.

| Module | Rules baseline, familiar formats | Rules baseline, harder cases | Tests |
|---|---|---|---|
| 01_invoicing | 94.1% mean field accuracy on customer details (37 cases) | 7 of 13 misses fall on 9 untuned phrasings and 5 on 3 Quebec-format cases (French address order, accented city); QST/PST charged only where the supplier is registered | 66 |
| 02_bills | 100% classification, 100% fields (34 templated emails) | 50% classification, 40% fields (6 held-out formats); provincial tax amount, kind and QST number read on 5 of 6 bills that carry it (the miss is the French TPS/TVQ invoice) | 77 |
| 03_payroll | Change-request extraction: precision 1.00, recall 0.75 (21 emails) | Deductions (CPP, CPP2, EI, federal and Ontario tax) match CRA PDOC to the cent for the 2 hand-checked employees ([PDOC check](03_payroll/README.md#pdoc-check), 3 Nov 2026) | 50 |
| 04_gst_hst | 93.3% category and ITC treatment (30 labelled); 9 of 9 Quebec rows | Gold labels reproduce all 18 GST34 lines and all 7 QST lines exactly; rules labels match 10 of 18 GST34 lines and 7 of 7 QST lines, every error flagged for review | 75 |
| 05_bookkeeping | Matching precision 1.00, recall 0.98; categorisation 93.9% (82 lines) | French receipt with comma decimals is missed and shows as an orphan | 50 |
| shared/ | Unit tests: ledger migrations and update whitelists, GST/HST and QST number validators, QST/PST/RST rates and rounding, MIME boundary, record/replay | | 27 |
| **Total** | | | **345** |


## How to score Claude

The rules baseline is what CI scores. To score the Claude extractors on the same labelled sets you need an Anthropic API key; nothing below has been run yet.

```bash
uv sync
export ANTHROPIC_API_KEY=sk-ant-...        # keep it out of the repo; .env is gitignored

# 1. Score rules and Claude side by side on every labelled set, and record each response.
#    Results go to evals/results.json; responses to <module>/fixtures/*.json.
AFO_LLM_MODE=record uv run python evals/run_all.py

# 2. What it cost, by module, from runs/token_log.jsonl.
uv run python -c "import json; from shared.llm import token_report; print(json.dumps(token_report(), indent=2))"

# 3. Run one agent with Claude doing the reading.
AFO_LLM_MODE=live uv run python 02_bills/run.py --extractor claude

# Optional: choose the model (default is set in shared/llm.py).
AFO_MODEL=<model-id> AFO_LLM_MODE=record uv run python evals/run_all.py
```

Each eval reports a `claude` (or `claude_extractor`) block next to `rules` when the mode is `live` or `record`; in `replay` mode it says "not run". Check `shared/pricing.yaml` against current Anthropic pricing before quoting a cost figure. Commit recorded fixtures only if they came from a real `record` run.

## Design principles

- **Agents propose, people approve.** Every write goes through `shared/approvals.py`. Every `run.py` is a dry run unless told otherwise.
- **Deterministic where it can be, model where it must be.** Tax maths, payroll deductions and file formats are plain code with tests. Claude reads unstructured documents and classifies edge cases, always through a forced JSON-schema tool call.
- **Token analytics as the ROI ledger.** Every model call logs tokens, cache hits and estimated cost by module and task to `runs/token_log.jsonl`, so the cost of each automation can be set against the hours it replaces.
- **Replayable.** `AFO_LLM_MODE=record` saves each Claude response next to its module (`<module>/fixtures/`) and `replay` serves them back, so a scored run can be reproduced offline. None are committed yet, because none have been recorded; tests and CI use the rules baseline and need no key.

## Run it

```bash
uv sync
uv run pytest -q                      # all tests, no API key needed
uv run python evals/run_all.py        # accuracy on labelled synthetic sets
uv run python 01_invoicing/run.py     # dry run of one module
./scripts/demo.sh                     # dry run of all five, compact summary
AFO_LLM_MODE=live ANTHROPIC_API_KEY=... uv run python 02_bills/run.py
```

## Credit

Inspired by Kieran Donnelly's post on the finance and admin automations he runs at Kwanda AI with Claude Code, rebuilt here for Canadian rules (GST/HST, QST, PST/RST, CPP/EI, PD7A, CPA-005).

## License

MIT
