# ai-finance-ops

Five reference MVPs of human-in-the-loop AI agents for small-business finance admin in Canada, built with Claude. Each one takes a recurring back-office job, does the reading, matching and drafting, and stops at an approval queue so a person decides what is posted, paid or filed.

All data in this repository is synthetic. Nothing here connects to a real bank, the CRA, or an accounting platform; those boundaries are stubbed behind small adapters.

<!-- MODULE TABLE -->

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
