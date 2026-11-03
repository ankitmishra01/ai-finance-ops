"""Transaction classifiers.

The only model step in this module: read a free-text ledger line and decide its
category, supply type, place of supply and ITC treatment, with a confidence.
Two interchangeable implementations:

- RulesExtractor: regexes compiled from rules.yaml. Deterministic; used in tests,
  CI and by default.
- ClaudeExtractor: shared.llm.extract with a forced JSON-schema tool call. The
  system prompt carries the rules file, so it is cached across the quarter.

Neither computes tax. The engine in gst_hst.py does that from the category.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

import yaml

from gst_hst import ITC_TREATMENTS, PLACES, SUPPLY_TYPES, Classification, Rules, Txn

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Extractor(Protocol):
    name: str

    def classify(self, txn: Txn) -> Classification: ...


# --------------------------------------------------------------------------- rules baseline

class RulesExtractor:
    """First matching category (in rules.yaml order) whose direction fits the bank sign."""

    name = "rules"

    def __init__(self, rules: Rules):
        self.rules = rules

    def classify(self, txn: Txn) -> Classification:
        r = self.rules
        hits = []
        for cat, res in r.keyword_res.items():
            d = r.category(cat)["direction"]
            if d not in ("any", txn.direction):
                continue
            matched = [m.group(0) for m in (rx.search(txn.description) for rx in res) if m]
            if matched:
                hits.append((cat, matched))
        if hits:
            cat, matched = hits[0]
            # Several categories matching is a sign of ambiguity: lower confidence a little.
            conf = 0.9 if len(hits) == 1 else 0.8
            evidence = ", ".join(matched)
            reason = ""
        else:
            cat = r.raw["fallback"][txn.direction]
            conf = float(r.raw["fallback"]["confidence"])
            evidence = ""
            reason = f"no rule matched the description; defaulted to {cat}"

        c = r.category(cat)
        place = self._place(cat, txn)
        if place == "UNKNOWN":
            conf = min(conf, 0.6)
            reason = reason or "no province or country in the description"
        return Classification(cat, c["supply_type"], place, c["itc_treatment"], conf, evidence, reason)

    def _place(self, cat: str, txn: Txn) -> str:
        if cat in ("sale_domestic", "credit_note", "sale_exempt_residential_rent"):
            p = self.rules.place_from_text(txn.description)
            return "UNKNOWN" if p == "OUTSIDE_CANADA" else p
        if cat in ("sale_export", "foreign_digital_service"):
            return "OUTSIDE_CANADA"
        return "NOT_APPLICABLE"


# --------------------------------------------------------------------------- Claude

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string", "enum": None},  # filled from rules.yaml
        "supply_type": {"type": "string", "enum": SUPPLY_TYPES},
        "place_of_supply": {"type": "string", "enum": PLACES},
        "itc_treatment": {"type": "string", "enum": ITC_TREATMENTS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "string", "description": "The words in the description that decided the category, quoted."},
        "needs_human_reason": {"type": "string",
                               "description": "Empty if none. Otherwise one sentence on what a person must check."},
    },
    "required": ["category", "supply_type", "place_of_supply", "itc_treatment", "confidence", "evidence",
                 "needs_human_reason"],
}

SYSTEM_TEMPLATE = """You classify lines from the bank and ledger export of a small Canadian business for its GST/HST return.

The business: a GST/HST registrant resident in Ontario that sells advisory and consulting services, files quarterly under the regular method (not the Quick Method), and also rents out one long-term residential unit. Amounts are in CAD.

Your job is reading, not arithmetic. For each transaction choose:
1. category: exactly one key from the RULES below. Read each category's `meaning`. Respect `direction`: "in" categories are money received (positive amount), "out" categories are money paid (negative amount); a credit note to a customer is money out.
2. supply_type and itc_treatment: copy the values the RULES give for the category you chose. If the facts in the description clearly contradict them, still copy the rules values, and explain the contradiction in needs_human_reason.
3. place_of_supply:
   - sales, credit notes and rent: the province of the customer's (or property's) address as written in the description, using the two-letter code. A city is enough (Calgary -> AB). A customer outside Canada -> OUTSIDE_CANADA. If no location is given, UNKNOWN; do not guess from the customer's name.
   - foreign digital services: OUTSIDE_CANADA.
   - all other purchases and transfers: NOT_APPLICABLE.
4. confidence: your probability, from 0 to 1, that the category is right. Use 0.9 or more only when the description states what was bought or sold. Use 0.6 or less when the description is only a merchant code, an order number or a marketplace name, or when it could reasonably be a personal expense.
5. evidence: quote the words that decided it.
6. needs_human_reason: empty string unless a person should look. Always fill it for: anything that may be personal; a foreign vendor that may have charged GST/HST; a refund or credit note; a description that does not say what was bought.

Do not compute or restate tax amounts. Do not invent facts that are not in the description. The gst_hst and supplier_bn fields are shown so you can tell whether tax was charged; do not use them to pick the province.

RULES (YAML; categories are listed in priority order, so prefer the earlier one when two fit equally):
{rules}
"""


def rules_for_prompt(rules: Rules) -> str:
    """The parts of rules.yaml the model needs: categories (minus regexes), types and treatments."""
    cats = {
        k: {f: v for f, v in c.items() if f in ("direction", "supply_type", "itc_treatment", "meaning", "cite")}
        for k, c in rules.categories.items()
    }
    doc = {
        "supply_types": {k: v["meaning"] for k, v in rules.raw["supply_types"].items()},
        "itc_treatments": {k: v["meaning"] for k, v in rules.raw["itc_treatments"].items()},
        "categories": cats,
    }
    return yaml.safe_dump(doc, sort_keys=False, width=110, allow_unicode=True)


class ClaudeExtractor:
    name = "claude"

    def __init__(self, rules: Rules, fixtures_dir: Path = FIXTURES):
        self.rules = rules
        self.fixtures_dir = fixtures_dir
        self.system = SYSTEM_TEMPLATE.format(rules=rules_for_prompt(rules))
        self.schema = json.loads(json.dumps(SCHEMA))
        self.schema["properties"]["category"]["enum"] = list(rules.categories)

    @staticmethod
    def prompt(txn: Txn) -> str:
        return json.dumps({
            "txn_id": txn.txn_id,
            "date": txn.date.isoformat(),
            "description": txn.description,
            "direction": "money in" if txn.direction == "in" else "money out",
            "amount": f"{txn.amount_cents / 100:.2f}",
            "gst_hst_shown": f"{txn.tax_cents / 100:.2f}",
            "supplier_bn_present": bool(txn.supplier_bn),
        }, indent=2)

    def classify(self, txn: Txn) -> Classification:
        from shared import llm

        out = llm.extract(module="gst_hst", task="classify_txn", system=self.system, prompt=self.prompt(txn),
                          schema=self.schema, fixtures_dir=self.fixtures_dir, max_tokens=512)
        return Classification(out["category"], out["supply_type"], out["place_of_supply"], out["itc_treatment"],
                              float(out["confidence"]), out.get("evidence", ""), out.get("needs_human_reason", ""))


def default_extractor_name() -> str:
    return "claude" if os.environ.get("AFO_LLM_MODE", "replay") in ("live", "record") else "rules"


def make_extractor(name: str | None, rules: Rules) -> Extractor:
    name = name or default_extractor_name()
    return ClaudeExtractor(rules) if name == "claude" else RulesExtractor(rules)


def classify_all(extractor: Extractor, txns: list[Txn]) -> dict[str, Classification]:
    return {t.txn_id: extractor.classify(t) for t in txns}
