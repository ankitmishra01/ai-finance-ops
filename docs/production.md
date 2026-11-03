# Production readiness of the boundaries

This repository is a reference build on synthetic data. This page describes what changes when the
agents touch a real ledger, a real mailbox and real people's pay and tax data, what the code
already does about it, and what an operator still has to provide. Nothing here has been run
against a live QuickBooks company, Xero organisation or mailbox.

## The boundaries

| Boundary | Interface | Default | Opt-in |
|---|---|---|---|
| Accounting platform | `shared.protocols.LedgerAdapter` | `SQLiteLedger` | `QuickBooksLedger` (`AFO_LEDGER=quickbooks`), `XeroLedger` (`AFO_LEDGER=xero`) |
| Mailbox | `shared.protocols.Mailbox` | `EmlMailbox` | `ImapMailbox` (`AFO_MAILBOX=imap`) |
| Model | `shared.llm.extract` | replay from fixtures | Anthropic API (`AFO_LLM_BACKEND=api`) or local `claude` CLI (`AFO_LLM_BACKEND=cli`), with `AFO_LLM_MODE=live` or `record` |

Agents only ever call the interfaces, and only after the approval gate (below). An adapter
that is selected but not configured fails at start-up with `AdapterConfigError`, naming the
missing variables and never their values.

## Security

### Secrets

- **Environment only.** Every credential is read from the environment at start-up: `QBO_*`,
  `XERO_*`, `AFO_IMAP_*`, `ANTHROPIC_API_KEY`. None is read from a file in the repository, and
  `.env` is gitignored. In production, inject them from a secret manager (AWS Secrets Manager, GCP
  Secret Manager, 1Password, Vault) into the process environment; do not bake them into images.
- **Least privilege.** QuickBooks: the `com.intuit.quickbooks.accounting` scope only. Xero:
  `accounting.transactions` and `accounting.contacts`, plus `offline_access` for the refresh token;
  not `accounting.settings`. Gmail: prefer XOAUTH2 with a token for a dedicated finance mailbox
  over an app password for a person's account. The IMAP adapter opens the folder read-only and
  fetches with `BODY.PEEK[]`, so it cannot delete, move or mark mail even if asked to.
- **Refresh-token rotation.** Intuit and Xero both issue a new refresh token on refresh and
  retire the old one. `shared.adapters.http.OAuth2` keeps the new token in memory and passes it to
  an `on_rotate` callback; production must supply a callback that writes it back to the secret
  store, or the next process start will fail to authenticate.
- **No secrets in logs or errors.** `OAuth2` and `ImapMailbox` override `repr` so tokens and
  passwords do not appear in tracebacks. Token-endpoint errors report the OAuth `error` code only,
  not the response body. API errors include the platform's error object (for example a
  QuickBooks `Fault` with its code) and the URL without its query string.
- **Model calls.** The CLI backend runs with `--tools ""` (no file, shell or web tools),
  `--safe-mode` (no CLAUDE.md, skills, hooks or MCP servers from the operator's machine) and
  `--no-session-persistence`. The API backend sends only the system prompt, the document and the
  schema.

### PII

Payroll emails carry names, salaries and sometimes SINs; bills and receipts carry vendor and
customer contact details; bank lines carry spending patterns.

- **Minimise what reaches the model.** Extractors send the document text the task needs and the
  schema; payroll never sends the employee master file, only the roster of names to match. Before
  real use, redact SINs and bank account numbers before the prompt is built (the payroll module
  flags a missing or invalid SIN but does not need the number itself to extract a change request).
- **Data retention.** Check the Anthropic data-retention terms that apply to your account and
  whether zero data retention is available to you before sending payroll data. A Claude
  subscription used through the CLI is governed by consumer or team terms, not API terms; do not
  use the CLI backend for real employee data unless those terms allow it.
- **Fixtures are copies of model output.** `AFO_LLM_MODE=record` writes every response to
  `<module>/fixtures/`. On real data those files contain PII: keep `record` for synthetic or
  consented test sets, and never commit fixtures recorded from production documents.
- **Outputs on disk.** `out/` holds payslips, the pay register, proposals and evidence files. It
  is gitignored; in production put it on encrypted storage with access limited to the finance
  team, and set a retention period (CRA requires books and records to be kept six years from the
  end of the last tax year they relate to).

## Audit log

What is recorded today:

- **Proposals and decisions.** Each `run.py` writes `proposals.json` from `ApprovalQueue.write()`:
  every proposal's kind, summary, the exact payload that would be committed, the agent's flags and
  the decision (`approved`, `rejected`, `held`, `pending`).
- **Model calls.** `runs/token_log.jsonl` has one row per call: timestamp, run id, module, task,
  a document key (hash of system prompt, prompt and schema), backend, the model id the API or CLI
  reported, tokens, estimated cost, billing basis and latency. It contains no document text.
- **Recorded responses.** In `record` mode, the model's structured answer for each document.

What production needs on top:

- **Who and when.** Record the reviewer's identity and a timestamp on every decision. The CLI's
  `ask` callback is the place to attach them; a web review screen would pass the signed-in user.
- **Append-only storage.** Ship proposals, decisions and token rows to an append-only store
  (object storage with object lock, or a write-once table), keyed by run id, so a decision cannot
  be edited after the fact.
- **Link to the platform.** Store the id the platform returned (QuickBooks `Id`, Xero
  `InvoiceID` / `ManualJournalID`) next to the approved payload and the idempotency key that
  produced it, so every ledger entry traces back to the email, the extraction and the person who
  approved it.

## Idempotency

A finance write must be safe to repeat: a timeout after the platform committed, a crash between
two approvals, or a person re-running yesterday's batch must not create a second bill or invoice.

- **Platform keys.** Each create carries a key derived from its content
  (`shared.adapters.http.idempotency_key`): the QuickBooks `requestid` query parameter, which
  makes QuickBooks return the original object for a repeated request, and Xero's
  `Idempotency-Key` header. Because the key is a hash of the payload, a retry of the same approved
  item reuses it, while a corrected payload gets a new one.
- **Natural keys.** Customers and vendors are looked up by name before they are created
  (`upsert_*`). Bills are checked for duplicates by vendor and invoice number before they are
  proposed (`02_bills`), and invoicing flags an invoice when the ledger already has one for that
  customer and period.
- **Mailbox.** IMAP messages are named `imap/<folder>/<UIDVALIDITY>-<UID>.eml`, which is stable,
  so a production run can keep the set of message ids it has proposed and skip them next time.
- **Model calls.** Fixtures are keyed by the hash of system prompt, prompt and schema, so the same
  document always maps to the same recorded answer.
- **Limits.** Idempotency keys are honoured by the platform for a limited window, so they protect
  against retries, not against re-running a month-old batch. Keep the natural-key checks on.
  SQLite has no request key: the local stand-in relies on the natural-key checks alone.

## Retries

- **HTTP.** `JsonClient` retries 429 and 500, 502, 503, 504 up to three times, honouring
  `Retry-After` (capped at 60 seconds) and otherwise backing off exponentially from one second.
  A 401 triggers one token refresh and one retry. Other 4xx responses are not retried: a
  validation fault (for example QuickBooks code 6140, duplicate document number) needs a person.
  Every retry of a create reuses the same idempotency key, which is what makes retrying a POST
  safe.
- **Platform limits.** QuickBooks and Xero both rate-limit per company or tenant per minute and per
  day; batch approvals should post sequentially, not in parallel, and a day's run should stay well
  under the daily limit.
- **Model calls.** The Anthropic SDK retries 408, 409, 429, 5xx and connection errors itself
  (twice by default). On top of that, `shared.llm` asks once more if the answer does not fit the
  schema, then raises `ModelOutputError`; a refusal raises immediately. Nothing is guessed: a
  document the model could not read becomes an error for a person, not a default value.

## The approval gate and segregation of duties

Segregation of duties means no single actor can initiate, approve and record the same
transaction. Here the agent is an actor too, and the controls map like this:

| Duty | Who | Where in the code |
|---|---|---|
| Prepare: read the source document, extract, compute tax and deductions, propose | The agent | each module's extractor and plain-code checks, then `ApprovalQueue.propose()` |
| Flag for review: anything unusual | The agent, by rule | `flags` on the proposal (totals mismatch, tax rate inconsistent with province, invalid GST/HST number, possible duplicate, foreign currency, CPP or EI maximum reached, gross change over threshold, missing TD1) |
| Approve | A person who did not prepare it | `ApprovalQueue.review()`; `auto=True` approves only unflagged items and holds every flagged one |
| Record | The adapter, only for approved items | `post_approved` / `commit` paths call the `LedgerAdapter` after review, never before |
| Authorise in the ledger, pay, file | A second person, in the platform | Xero documents are created as `DRAFT` by default (`XERO_DOCUMENT_STATUS`), so authorising them is a separate step in Xero; the CPA 005 payment file and the GST34 are outputs for a person to submit, never sent by the agent |
| Reconcile and review | Someone outside AP and payroll | `05_bookkeeping` exceptions report; evidence links and category changes are themselves proposals |

What makes the mapping hold in production:

- **The agent cannot approve.** There is no code path from a proposal to the ledger that skips
  `review()`, and `--auto-approve` is a demo setting that still holds flagged items. In
  production, disable `--auto-approve` or restrict it to a named low-risk kind under a threshold.
- **Dependent approvals.** An invoice to a new or changed customer is held unless the customer
  change was approved too (`01_invoicing.enforce_dependencies`), so a changed bank or email
  address cannot ride through on an invoice approval.
- **Separate credentials.** Give the agent's OAuth app a platform user role that can create
  drafts but not approve bills or pay them, so the ledger enforces what the code intends.
- **Payroll is two decisions.** Each pay line, then the run, as in the module; the remittance and
  payment file are produced for a person to send.
- **Evidence of review.** The reviewer's identity on each decision (see the audit log) is what
  lets an auditor test the control rather than take it on trust.
