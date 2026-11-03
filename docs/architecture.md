# Architecture

Five agents share one small library. Each agent reads documents, proposes changes, and stops. Nothing reaches the ledger, a payment file or a filing until a person approves it.

```mermaid
flowchart LR
    subgraph inputs["Synthetic inputs (data/synthetic/)"]
        MB[".eml mailboxes<br/>PDF bills and receipts"]
        OPS["ops data: contracts, timesheets,<br/>roster, bank and card lines"]
    end

    subgraph modules["Agents (one folder each, run.py = dry run by default)"]
        M1["01_invoicing"]
        M2["02_bills"]
        M3["03_payroll"]
        M4["04_gst_hst"]
        M5["05_bookkeeping"]
    end

    subgraph extract["Extractor, chosen per run"]
        RX["RulesExtractor<br/>deterministic baseline<br/>(tests, CI, evals)"]
        CX["ClaudeExtractor<br/>forced JSON-schema tool call"]
    end

    subgraph shared["shared/"]
        MAIL["mail.py<br/>read_mailbox, build_eml"]
        LLM["llm.py<br/>replay / live / record"]
        TAX["tax.py<br/>GST/HST/PST rates,<br/>GST/HST number check"]
        APPR["approvals.py<br/>ApprovalQueue"]
        LEDGER["ledger.py<br/>SQLite stand-in for<br/>QuickBooks / Xero / Wave"]
    end

    API[("Anthropic API")]
    FIX[("module/fixtures/*.json<br/>recorded responses")]
    TOK[("runs/token_log.jsonl<br/>tokens, cache hits,<br/>cost by module + task")]
    HUMAN{{"Person:<br/>approve / reject / hold"}}
    OUT["Outputs: invoice PDFs, AP aging,<br/>payslips, PD7A summary, CPA 005 sample,<br/>GST34 working paper, exceptions report"]

    MB --> MAIL --> modules
    OPS --> modules
    modules --> extract
    CX --> LLM
    LLM -- "live / record" --> API
    LLM -- "record writes, replay reads" --> FIX
    LLM -- "every live call" --> TOK
    modules -- "deterministic maths:<br/>tax, deductions, matching" --> TAX
    modules -- "propose(kind, summary, payload, flags)" --> APPR
    APPR --> HUMAN
    HUMAN -- "approved items only" --> LEDGER
    HUMAN -- "approved items only" --> OUT
```

## The pieces

| Piece | What it does | Why it is shaped this way |
|---|---|---|
| `shared/llm.py` | One function, `extract()`, that forces a single tool call validated against a JSON schema. `AFO_LLM_MODE=replay` (default) reads a recorded response from disk, `live` calls the API, `record` calls it and saves the response. | Downstream code never parses prose. Tests and CI need no key and cost nothing. The system prompt (rules, chart of accounts) is marked for prompt caching because it repeats on every document in a run. |
| Token log | Every live call appends model, input/output/cache tokens, estimated cost and latency to `runs/token_log.jsonl`, tagged by module and task. `llm.token_report()` sums it by module. | Gives the cost side of an ROI case per automation, to set against the hours the job took by hand. |
| `shared/approvals.py` | `ApprovalQueue.propose()` records a summary, the payload that would be committed, and any flags. `review()` asks y/n per item; `auto=True` approves only unflagged items and holds the rest. | Agents propose, people approve. Flags are the agent's way of saying "a person must look at this". |
| `shared/ledger.py` | SQLite tables for customers, invoices, vendors, bills, transactions and pay runs, with integer cents. `update_customer` / `update_transaction` only touch whitelisted columns. New columns are added by idempotent migrations on connect. | A real adapter (QuickBooks, Xero) can replace it without changing agent logic. |
| `shared/tax.py` | Provincial GST/HST/PST rates, half-up rounding to the cent, GST/HST registration-number format and BN check digit. | Shared by invoicing, bills, GST/HST and bookkeeping so rates live in one place. |
| `shared/mail.py` | Reads `.eml` exports; builds them for the synthetic-data generators, with an optional pinned MIME boundary so fixtures are byte-identical across runs. | Stands in for an IMAP or Gmail adapter. |

## One run, end to end (02_bills)

```mermaid
sequenceDiagram
    participant R as run.py
    participant M as mail.read_mailbox
    participant X as Extractor
    participant B as bills.py (plain code)
    participant Q as ApprovalQueue
    participant P as Person
    participant L as ledger

    R->>M: inbox/*.eml
    M-->>R: Mail[] with PDF attachments
    loop each message
        R->>X: classify + extract fields
        X-->>R: Extraction (schema-valid)
        R->>B: totals, tax rate vs province, GST/HST number,<br/>ITC tier, duplicates, due date, bucket
        B-->>R: BillReview + flags
        R->>Q: propose("bill", summary, payload, flags)
    end
    R-->>P: AP aging + proposals (dry run stops here)
    P->>Q: --approve: y/n per bill
    Q->>L: approved bills only (create_bill)
```
