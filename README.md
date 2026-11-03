# ai-finance-ops

Five reference MVPs of human-in-the-loop AI agents for small-business finance admin in Canada, built with Claude. Each one takes a recurring back-office job, does the reading, matching and drafting, and stops at an approval queue so a person decides what is posted, paid or filed.

All data in this repository is synthetic. Nothing here connects to a real bank, the CRA, or an accounting platform; those boundaries are stubbed behind small adapters.

## The five agents

| Module | Recurring job | What the agent does | What a person approves |
|---|---|---|---|
| [01_invoicing](01_invoicing/) | Month-end client invoicing | Finds billable clients in the ops data, reads billing changes from email, builds lines (hourly, retainer, milestone), applies GST/HST by place of supply, renders invoice PDFs | New customers, customer changes, each invoice |
| [02_bills](02_bills/) | Accounts payable intake | Classifies a shared finance mailbox, extracts bills from PDFs and email bodies, checks totals, tax rate and GST/HST number, decides ITC eligibility, detects duplicates, buckets by due date | Each bill before it is posted |
| [03_payroll](03_payroll/) | Ontario pay run | Reads pay-change emails, computes gross pay and CPP/CPP2, EI, federal and Ontario tax with the CRA T4127 formulas, produces payslips, PD7A remittance summary, a sample CPA 005 payment file and the journal | Each pay line, then the run |
| [04_gst_hst](04_gst_hst/) | Quarterly GST/HST return | Applies a cited rules file to every transaction, classifies supply type and ITC treatment, computes the GST34 lines with a transaction-level working paper, computes the filing deadline | Flagged transactions, then the return |
| [05_bookkeeping](05_bookkeeping/) | Records and evidence | Reads receipts, matches them to bank and card lines (FX, splits, tips), proposes recategorisations, files evidence by period, reports exceptions | Each evidence link and category change |

## What a run looks like

`./scripts/demo.sh` dry-runs all five agents on the synthetic data in about five seconds and prints a summary of each. The excerpts below were copied from a real run of `uv run python 03_payroll/run.py` and `uv run python 02_bills/run.py` (rules extractor, no API key, 3 November 2026). Every flag is something the agent will not decide on its own.

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
Read 36 messages with the rules extractor: 24 bill, 6 other, 3 receipt, 3 statement

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

AP aging as of 2026-11-03 (exact duplicates excluded):
  CAD: overdue $4,009.24, due this week $4,039.75, next 30 days $4,423.73, later $1,356.00
  USD: overdue $299.00, due this week $0.00, next 30 days $0.00, later $0.00

24 bills proposed, 11 flagged for a person.
```

Architecture, with diagrams of `shared/`, the approval gate and the token log: [docs/architecture.md](docs/architecture.md).

## Where things stand

Every model step has two implementations: a deterministic rules baseline, which runs in tests and CI, and a Claude extractor behind a forced JSON-schema tool call. The numbers below are for the **rules baseline only**, computed by `evals/run_all.py` on synthetic labelled sets. Because the rules and the synthetic documents were written together, the scores on familiar formats are flattering; the held-out or unfamiliar-phrasing figures are the honest baseline that the Claude extractor is meant to beat. Claude has not been scored yet and no model responses have been recorded or invented; run `AFO_LLM_MODE=record` with an API key to produce both.

| Module | Rules baseline, familiar formats | Rules baseline, harder cases | Tests |
|---|---|---|---|
| 01_invoicing | 96.1% mean field accuracy on customer details (34 cases) | 7 of 8 misses fall on 9 untuned phrasings | 54 |
| 02_bills | 100% classification, 100% fields (30 templated emails) | 50% classification, 40% fields (6 held-out formats) | 65 |
| 03_payroll | Change-request extraction: precision 1.00, recall 0.75 (21 emails) | Deduction maths hand-checked from T4127 for 2 employees; not yet compared with CRA PDOC | 50 |
| 04_gst_hst | 93.3% category and ITC treatment (30 labelled) | Gold labels reproduce all 18 GST34 lines exactly; rules labels match 10 of 18, every error flagged for review | 61 |
| 05_bookkeeping | Matching precision 1.00, recall 0.98; categorisation 93.9% (82 lines) | French receipt with comma decimals is missed and shows as an orphan | 50 |


## Design principles

- **Agents propose, people approve.** Every write goes through `shared/approvals.py`. Every `run.py` is a dry run unless told otherwise.
- **Deterministic where it can be, model where it must be.** Tax maths, payroll deductions and file formats are plain code with tests. Claude reads unstructured documents and classifies edge cases, always through a forced JSON-schema tool call.
- **Token analytics as the ROI ledger.** Every model call logs tokens, cache hits and estimated cost by module and task to `runs/token_log.jsonl`, so the cost of each automation can be set against the hours it replaces.
- **Replayable.** Recorded model responses live next to each module, so tests and CI run without an API key.

## Run it

```bash
uv sync
uv run pytest -q                      # all tests, replayed model responses
uv run python evals/run_all.py        # accuracy on labelled synthetic sets
uv run python 01_invoicing/run.py     # dry run of one module
./scripts/demo.sh                     # dry run of all five, compact summary
AFO_LLM_MODE=live ANTHROPIC_API_KEY=... uv run python 02_bills/run.py
```

## Credit

Inspired by Kieran Donnelly's post on the finance and admin automations he runs at Kwanda AI with Claude Code, rebuilt here for Canadian rules (GST/HST, CPP/EI, PD7A, CPA-005).

## License

MIT
