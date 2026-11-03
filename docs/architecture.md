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
        CX["ClaudeExtractor<br/>schema-checked tool call<br/>(API or claude CLI)"]
    end

    subgraph shared["shared/"]
        MAIL["Mailbox<br/>.eml folder (default),<br/>IMAP / Gmail (opt-in)"]
        LLM["llm.py<br/>replay / live / record<br/>backend: api or cli"]
        TAX["tax.py<br/>GST/HST, QST, PST/RST,<br/>GST/HST + QST number checks"]
        APPR["approvals.py<br/>ApprovalQueue"]
        LEDGER["LedgerAdapter<br/>SQLite (default),<br/>QuickBooks or Xero (opt-in)"]
    end

    API[("Anthropic API<br/>or local claude CLI")]
    FIX[("module/fixtures/*.json<br/>recorded responses")]
    TOK[("runs/token_log.jsonl<br/>tokens, cache hits,<br/>cost by module + task")]
    HUMAN{{"Person:<br/>approve / reject / hold"}}
    OUT["Outputs: invoice PDFs, AP aging,<br/>payslips, PD7A and Revenu Québec summaries,<br/>CPA 005 sample, T4/RL-1 and SAMPLE T4 XML, ROE data,<br/>GST34 working paper, QST worksheet,<br/>exceptions report"]

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
| `shared/llm.py` | One function, `extract()`, that asks for a single `submit` tool call (API) or a bare JSON object (`claude` CLI, `AFO_LLM_BACKEND=cli`), checks it against the JSON schema with `shared/schema.py`, and retries once if it does not fit. `AFO_LLM_MODE=replay` (default) reads a recorded response from disk, `live` calls Claude, `record` calls it and saves the response; fixtures from either backend replay the same way. | Downstream code never parses prose. Tests and CI need no key and cost nothing. The system prompt (rules, chart of accounts) is marked for prompt caching because it repeats on every document in a run. |
| Token log | Every live call appends the model id Claude reported, input/output/cache tokens, estimated cost, billing basis (API or subscription) and latency to `runs/token_log.jsonl`, tagged by run, module, task and document. `llm.token_report()` sums it by module; `evals/run_all.py` turns it into tokens and USD per document. | Gives the cost side of an ROI case per automation, to set against the hours the job took by hand. |
| `shared/approvals.py` | `ApprovalQueue.propose()` records a summary, the payload that would be committed, and any flags. `review()` asks y/n per item; `auto=True` approves only unflagged items and holds the rest. Each run.py writes its queue to `out/`; `decide()` records a person's decision and reason in that file, and both `python -m shared.approvals` and the web UI (`python -m webui`) go through it. | Agents propose, people approve. Flags are the agent's way of saying "a person must look at this". |
| `shared/ledger.py` | SQLite tables for customers, invoices, vendors, bills, transactions and pay runs, with integer cents. `update_customer` / `update_transaction` only touch whitelisted columns. New columns are added by idempotent migrations on connect; provincial sales tax has its own columns (`invoices.provincial_tax_cents`, `bills.other_tax_kind`, `bills.qst_itr_cents`), apart from GST/HST. | Wrapped by `SQLiteLedger`, which implements `shared.protocols.LedgerAdapter`; `shared/adapters/quickbooks.py` and `xero.py` implement the same Protocol and are chosen with `AFO_LEDGER` ([production.md](production.md)). |
| `shared/tax.py` | Provincial GST/HST rates; QST (9.975%), BC and SK PST and MB RST on the pre-GST price, each citing its official page; half-up rounding to the cent (Decimal for 9.975%); GST/HST registration-number format and BN check digit; QST registration-number format. | Shared by invoicing, bills, GST/HST and bookkeeping so rates live in one place. |
| `shared/mail.py` | Reads `.eml` exports; builds them for the synthetic-data generators, with an optional pinned MIME boundary so fixtures are byte-identical across runs. | `EmlMailbox` implements `shared.protocols.Mailbox`; `shared/adapters/imap.py` (read-only IMAP, Gmail by default) implements it too and is chosen with `AFO_MAILBOX`. |

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
