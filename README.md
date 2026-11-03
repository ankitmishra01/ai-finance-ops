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
AFO_LLM_MODE=live ANTHROPIC_API_KEY=... uv run python 02_bills/run.py
```

## Credit

Inspired by Kieran Donnelly's post on the finance and admin automations he runs at Kwanda AI with Claude Code, rebuilt here for Canadian rules (GST/HST, CPP/EI, PD7A, CPA-005).

## License

MIT
