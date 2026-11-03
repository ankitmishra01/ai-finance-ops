# 02_bills: accounts payable intake

Supplier invoices arrive in a shared finance mailbox mixed in with statements, payment receipts, newsletters and resends. Someone has to pick out the real bills, key them in, check the tax, decide whether the GST/HST can be claimed as an input tax credit (ITC) and the Quebec QST as an input tax refund (ITR), spot duplicates, and work out what is due when. This module does the reading and checking, then stops at an approval queue. A person decides what reaches the ledger.

## Run it

```bash
uv run python 02_bills/run.py                     # dry run: proposals + AP aging, no ledger writes
uv run python 02_bills/run.py --approve           # approve or reject each bill at the prompt
uv run python 02_bills/run.py --auto-approve      # demo: clean bills approved, flagged bills held
AFO_LLM_MODE=live ANTHROPIC_API_KEY=... uv run python 02_bills/run.py   # Claude reads the mail
uv run python 02_bills/bills_data.py              # regenerate the synthetic mailbox (seed 42)
uv run python evals/eval_bills.py                 # accuracy against the labelled set
```

Outputs go to `out/bills/`: `ap_aging.csv`, `ap_aging.md`, `proposals.json`, and `ledger.db` once something is approved. `--as-of` defaults to the date stored with the synthetic set (2026-11-03) so the buckets come out the same on every run.

## Workflow

```mermaid
flowchart TD
    A[".eml mailbox"] --> B["Read body + PDF text (pypdf)"]
    B --> C{"Extractor: classify<br/>bill / statement / receipt / other"}
    C -- "statement, receipt, other" --> S["Skipped, listed in the run output"]
    C -- bill --> D["Extract vendor, GST/HST no., QST no., invoice no., date,<br/>subtotal, GST/HST, QST/PST/RST, total, currency, terms, category"]
    D --> E["Deterministic checks<br/>totals, tax rates vs province, BN and QST no. format,<br/>ITC tier, QST refund, duplicates, due date"]
    E --> F["ApprovalQueue proposal with flags"]
    F --> G{"Person reviews"}
    G -- approve --> H["shared.ledger vendors + bills"]
    G -- "reject / held" --> I["Stays out of the ledger"]
    E --> J["AP aging CSV + markdown"]
```

## What is automated and what a person approves

| Step | Who | How |
|---|---|---|
| Classify each email | Extractor | `RulesExtractor` (regex) by default; `ClaudeExtractor` with `--extractor claude` or when `AFO_LLM_MODE` is `live`/`record` |
| Read bill fields | Extractor | Same; Claude is called through `shared.llm.extract` with a schema-checked tool call |
| Suggest an expense account | Extractor | Keyword map (rules) or a constrained enum (Claude), from an 11-line chart of accounts in `bills_extract.py` |
| Totals, tax rate, GST/HST number, ITC tier, duplicates, due date, bucket | Code | `bills.py`, all tested |
| QST/PST/RST rate, QST number, QST input tax refund | Code | `bills.provincial_rate_check`, `bills.qst_itr_assessment` |
| Post a bill to the ledger | **Person** | `--approve` asks y/N per bill. `--auto-approve` approves only bills with no flags and holds the rest |
| Pay a bill | Not done | Out of scope; the aging report shows what is due |

Flags raised for a person:

- `duplicate`: same vendor and invoice number as an earlier email or a bill already in the ledger (vendor names compared without case, punctuation or Inc./Ltd.)
- `possible duplicate`: similar vendor name, total within 1% and invoice dates within 7 days, under a different number
- `total mismatch`: subtotal + GST/HST + PST/QST differs from the printed total by more than one cent
- `tax rate inconsistent with vendor address`: the GST/HST charged matches neither the vendor's province rate nor the recipient's province rate (see limitations)
- `missing GST/HST registration number` on a bill of $100 or more that charges tax
- GST/HST number not in the form 9 digits + `RT` + 4 digits, or failing the business-number check digit
- `foreign currency`
- `provincial tax inconsistent with vendor address`: the QST, PST or RST charged is not the vendor province's rate on the subtotal (QC 9.975%, BC 7%, SK 6%, MB 7%), names the wrong tax, or comes from a vendor in a province without one
- QST number not in the form 10 digits + `TQ` + 4 digits (including `NR` numbers from the specified QST system), or `missing QST registration number` on a QST bill of $100 or more when the recipient is QST-registered
- `missing fields` when the vendor, invoice number, date or total could not be read

## Input tax credit rules applied

An ITC can only be claimed if the registrant holds documents with the information prescribed by the Input Tax Credit Information (GST/HST) Regulations, s. 3, under subsection 169(4) of the Excise Tax Act. The information required depends on the total paid or payable on the document:

| Total | Information required (cumulative) |
|---|---|
| under $100 | supplier name, invoice date, total amount |
| $100 to $499.99 | plus the supplier's GST/HST registration number and the tax amount (or a statement that tax is included and the rate) |
| $500 or more | plus the recipient's name, the terms of payment, and a description of each supply |

The module also:

- treats an ITC as not applicable when no GST/HST was charged;
- holds the ITC (`needs_review`, $0) when the tax rate or total is inconsistent, since tax charged in error is not tax payable;
- counts 50% of the tax on meals and entertainment, per ETA s. 236;
- refuses an ITC on a foreign-currency invoice until it is converted to CAD;
- never treats PST, RST or QST as an ITC.

It assumes every purchase is used 100% in commercial activities.

## Provincial sales tax and QST input tax refunds

QST, PST and RST are recorded apart from GST/HST: `other_tax_cents` holds the amount, `other_tax_kind` says which tax it is (as printed, or from the vendor's province), and `qst_number` holds the supplier's QST number. All three reach the ledger (`bills.other_tax_kind`, `bills.qst_itr_cents`, `vendors.qst_number`), the AP aging CSV and the proposals.

PST and RST are a cost and are posted with the expense. QST can come back as an input tax refund. `qst_itr_assessment` allows it when:

- the recipient is registered for QST (`recipient_qst_registered` in `labels.json`; the synthetic Tamarack Test Studio is);
- from a total of $100, the invoice shows a QST number in the 10 digits + `TQ` + 4 digits form;
- the amount matches the vendor province's rate and the totals add up (otherwise the refund is held as `needs_review`);
- the invoice is in CAD.

Meals and entertainment get 50% of the QST, rounded up to the cent, as for the GST/HST.

Sources: Revenu Québec, [Input Tax Credits and Input Tax Refunds](https://www.revenuquebec.ca/en/businesses/consumption-taxes/gsthst-and-qst/itcs-and-itrs/) and [Meal and Entertainment Expenses](https://www.revenuquebec.ca/en/businesses/consumption-taxes/gsthst-and-qst/special-cases-gsthst-and-qst/meal-and-entertainment-expenses-itcs-and-itrs/). revenuquebec.ca refused automated fetches while this was built, so these rules were taken from search summaries of those pages. Read them before relying on the code.

## Regulatory references

- Input Tax Credit Information (GST/HST) Regulations, SOR/91-45, s. 3: https://laws-lois.justice.gc.ca/eng/regulations/SOR-91-45/section-3.html
- CRA, GST/HST Memorandum 8.4, *Documentary Requirements for Claiming Input Tax Credits*: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/8-4/documentary-requirements-claiming-input-tax-credits.html. The memorandum dates from 2012 and still shows the old $30 and $150 thresholds. The regulations were amended in 2021 to $100 and $500, and those are the thresholds used here.
- GST/HST rates by province (in `shared/tax.py`): https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/charge-collect-which-rate/calculator.html
- Excise Tax Act, s. 169 (ITCs) and s. 236 (meals and entertainment): https://laws-lois.justice.gc.ca/eng/acts/E-15/

## Synthetic data

`bills_data.py` (Faker `en_CA`, seed 42) writes 40 `.eml` messages to `data/synthetic/bills/inbox/` and their ground truth to `labels.json`. The output is byte-identical between runs: the PDF writer runs in invariant mode and the MIME boundary is pinned. All names, addresses and GST/HST numbers are invented, and every email domain ends in `.test`.

- **Templated (34):** PDF invoices in three layouts, invoices pasted into the email body, three statements, three receipts, five marketing or internal emails, and these planted cases: a resent duplicate, a re-issue under a new number, a Nova Scotia vendor charging 15%, a $508.50 contractor bill with no registration number, a US vendor billing in USD with no GST, a printed total that is $10 too high, a malformed registration number, a meals bill, a BC bill with PST and a 2/10 Net 30 bill. Four more were added with QST support, dated 3 November so they come after every earlier message and files 01 to 36 kept their bytes: a Quebec law firm (GST + QST, valid QST number), a Quebec caterer (meals, so half the QST), a Quebec printer whose QST number is malformed, and a Saskatchewan supplier charging 6% PST.
- **Holdout (6):** formats the rules baseline was not written for: a prose invoice with the vendor only in the signature, a quote-style table PDF, a Quebec invoice with French labels (TPS/TVQ), a card-charge receipt that mentions an invoice, an account summary that never says "statement", and a forwarded invoice.

## Eval

`evals/eval_bills.py` scores email classification and ten extracted fields (vendor, GST/HST number, invoice number, date, subtotal, tax, total, currency, terms, category), split into templated and holdout. Terms are compared by meaning: `Net 14` and "due within 14 days" count as equal. The eval also scores flag precision and recall against the planted cases. Claude is scored only in `live` or `record` mode. No recorded Claude responses are committed, so CI scores the rules baseline only.

RulesExtractor results on this data (from `uv run python evals/eval_bills.py`):

| Split | Messages | Classification accuracy | Bills | Mean field accuracy |
|---|---|---|---|---|
| templated | 34 | 1.00 | 23 | 1.00 |
| holdout | 6 | 0.50 | 4 | 0.40 |
| all | 40 | 0.925 | 27 | 0.911 |

Flags across all 40 messages: 8 true positives, 0 missed, 8 false positives. All 8 are `missing fields` or `total mismatch` flags on four holdout messages the rules misread, two of which are not bills at all. These flags are what keeps such a message out of auto-approval.

Provincial tax, on the 6 bills that carry it: the amount, the tax kind and the QST number are each read correctly on 5 of 6. The miss is the French holdout invoice (TPS/TVQ labels), which the rules do not classify as a bill at all. With the recipient QST-registered, the engine claims $119.70 on the law firm, $14.97 (half of $29.93) on the caterer, nothing on the printer with the malformed number, and nothing on the BC and SK PST: $134.67 in all.

The templated score is high because the regexes were written against those three layouts, so it says little about real mail. The holdout split is the honest number for the baseline, and it is the gap the Claude extractor is meant to close. Run the eval in `record` mode to measure Claude.

## Limitations

- The tax-rate check accepts either the vendor's province rate or the recipient's. Real place-of-supply rules (ETA Schedule IX) depend on what was supplied and where it was delivered, so a correct rate can still be flagged, and an incorrect rate that happens to match the vendor's province can pass. A Quebec supplier charging GST + QST to an Ontario customer passes the check for this reason.
- The check-digit test on the business number is a typo screen, not a registry lookup. Confirm numbers in the CRA GST/HST Registry before claiming large ITCs.
- No foreign-exchange conversion. USD bills are reported in their own currency and held.
- Text is read from the PDF text layer only. Scanned image PDFs would need OCR.
- The provincial-tax rate check uses the vendor's province only. QST, PST and RST follow their own place-of-supply rules, so a supplier that correctly charges another province's tax is flagged for a person.
- QST refunds: no apportionment for mixed use, and neither the large-business ITR restrictions nor the sales-based ceiling on meal refunds is modelled. The rules extractor does not read French TPS/TVQ labels.
- The rules baseline has known bugs that are left in place so the holdout score stays honest. For example, its fallback terms regex reads "HST 13% on net 162.50" as "net 162". Those bills are flagged `missing fields` and held.
- Duplicate detection only compares against this run and bills already in the ledger, not paid history.
