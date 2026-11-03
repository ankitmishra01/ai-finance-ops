"""Reads pay-change request emails into structured adjustments.

This is the only step in the payroll module that uses a model. Two implementations
share one interface:

- RulesExtractor: regular expressions and keyword heuristics. Deterministic; runs in
  tests and CI; it is the baseline the eval scores.
- ClaudeExtractor: calls shared.llm.extract with the JSON schema below. Selected with
  `--extractor claude`, or automatically when AFO_LLM_MODE is live or record.

Both return a list of plain dicts that match ADJUSTMENT_SCHEMA. Nothing here touches
money: amounts are read, never computed. Gross pay and deductions are calculated in
payroll.py and payroll_calc.py.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Protocol

from shared import llm
from shared.mail import Mail

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

TYPES = ["raise", "unpaid_leave", "new_hire", "bonus", "hours_correction"]
FIELDS = ["type", "employee_name", "effective_date", "bonus_amount", "new_annual_salary",
          "new_hourly_rate", "unpaid_days", "extra_hours", "sin_provided", "td1_provided"]

ADJUSTMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "adjustments": {
            "type": "array",
            "description": "One entry per distinct pay change requested in the email. Empty if the email asks for no change to this pay run.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": TYPES},
                    "employee_name": {"type": "string", "description": "Full name exactly as it appears in the employee roster, or as written in the email for a new hire."},
                    "effective_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD. Raise: date the new rate starts. New hire: first day of work. Unpaid leave: first day of leave if given. Otherwise null."},
                    "bonus_amount": {"type": ["number", "null"], "description": "Bonus in dollars, gross. Only for type bonus."},
                    "new_annual_salary": {"type": ["number", "null"], "description": "New or starting annual salary in dollars, for raise or new_hire on salary."},
                    "new_hourly_rate": {"type": ["number", "null"], "description": "New or starting hourly rate in dollars, for raise or new_hire paid hourly."},
                    "unpaid_days": {"type": ["number", "null"], "description": "Number of unpaid working days in this pay period. Only for unpaid_leave."},
                    "extra_hours": {"type": ["number", "null"], "description": "Hours to add (positive) or remove (negative) at the regular rate. Only for hours_correction."},
                    "sin_provided": {"type": ["boolean", "null"], "description": "New hire only: true if the email says the SIN was received, false if it says it is missing or to follow, null if not mentioned."},
                    "td1_provided": {"type": ["boolean", "null"], "description": "New hire only: true if the email says TD1 forms were received, false if missing or to follow, null if not mentioned."},
                    "evidence": {"type": "string", "description": "The shortest verbatim quote from the email that supports this adjustment."},
                },
                "required": ["type", "employee_name", "effective_date", "bonus_amount", "new_annual_salary",
                             "new_hourly_rate", "unpaid_days", "extra_hours", "sin_provided", "td1_provided", "evidence"],
            },
        }
    },
    "required": ["adjustments"],
}

SYSTEM = """You read emails sent to the payroll administrator of a small Ontario employer and extract the pay changes they request for the current pay run.

Rules:
- Return one adjustment per distinct change. An email can contain zero, one or several.
- Only extract changes that are being requested or confirmed. Do not extract a change that is cancelled, deferred to a later pay run, hypothetical, or merely asked about. A forwarded copy of an earlier request is still a request; return it, and the payroll system will detect the duplicate.
- type must be one of: raise (new salary or hourly rate), unpaid_leave (unpaid days off), new_hire (a person starting work), bonus (one-time payment), hours_correction (hours to add to or remove from an hourly employee's timesheet).
- employee_name: use the full name from the roster supplied in the message when the email refers to someone by first name, last name or nickname. For a new hire not on the roster, use the name as written.
- Convert all dates to YYYY-MM-DD using the pay period year. "the 9th" in a November email means the 9th of November.
- Amounts are in Canadian dollars. Write $78k as 78000. Do not compute anything: if an email gives a percentage raise but no new salary, set new_annual_salary to null and quote the percentage in evidence.
- unpaid_days counts working days. "a day and a half" is 1.5. "Wednesday to Friday" is 3.
- Leave fields that do not apply to the type as null.
- evidence is a short verbatim quote, never a paraphrase."""


class Extractor(Protocol):
    name: str

    def extract(self, mail: Mail, *, roster: list[str], period_start: date) -> list[dict]: ...


def _blank(type_: str, name: str) -> dict:
    d = {k: None for k in FIELDS}
    d.update(type=type_, employee_name=name, evidence="")
    return d


# ---------------------------------------------------------------- rules baseline

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "half": 0.5}

MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s?(k|K)?"


def _money(m: re.Match) -> float:
    whole = float(m.group(1).replace(",", ""))
    frac = float("0." + m.group(2)) if m.group(2) else 0.0
    v = whole + frac
    return v * 1000 if m.group(3) else v


def _nearest_money(s: str, pos: int) -> re.Match | None:
    """The dollar amount closest to a keyword, for sentences that mention several amounts."""
    ms = list(re.finditer(MONEY, s))
    return min(ms, key=lambda m: abs(m.start() - pos)) if ms else None


def _dates(text: str, year: int) -> list[date]:
    out = []
    for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)\b", text):
        out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"\b(?<!-)(1[0-2]|0?[1-9])[-/](\d{1,2})\b(?!-)", text):
        try:
            out.append(date(year, int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text, re.I):
        out.append(date(year, MONTHS[m.group(1).lower()], int(m.group(2))))
    return out


def _names_in(text: str, roster: list[str]) -> list[str]:
    found = []
    low = text.lower()
    for full in roster:
        parts = full.lower().split()
        if full.lower() in low or any(re.search(rf"\b{re.escape(p)}\b", low) for p in parts if len(p) > 2):
            found.append(full)
    return found


def _new_person(text: str) -> str | None:
    m = re.search(r"(?:new hire|welcome|hired|joining us|joins us)[^A-Z]{0,40}([A-Z][a-z]+(?:[- ][A-Z][a-z']+)+)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([A-Z][a-z]+ [A-Z][a-z']+) (?:starts|will start|is starting|joins|begins)", text)
    return m.group(1) if m else None


def _days(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?|one|two|three|four|five|half)\s+(?:unpaid\s+)?(?:working\s+)?days?", text, re.I)
    if not m:
        return None
    tok = m.group(1).lower()
    return float(NUM_WORDS.get(tok, tok))


class RulesExtractor:
    """Keyword and regex baseline. One sentence-level pass per adjustment type."""

    name = "rules"

    def extract(self, mail: Mail, *, roster: list[str], period_start: date) -> list[dict]:
        body = mail.body
        text = f"{mail.subject}\n{body}"
        low = text.lower()
        year = period_start.year
        out: list[dict] = []
        if re.search(r"\b(cancel|ignore|disregard|hold off|not this (pay|cycle|run)|next (pay|cycle|run))", low):
            return out

        sentences = re.split(r"(?<=[.!?])\s+|\n+", body)
        names = _names_in(text, roster)
        primary = names[0] if names else ""

        def who(sentence: str) -> str:
            n = _names_in(sentence, roster)
            return n[0] if n else primary

        for s in sentences:
            sl = s.lower()
            if "bonus" in sl and (m := _nearest_money(s, sl.index("bonus"))):
                a = _blank("bonus", who(s))
                a["bonus_amount"] = _money(m)
                a["evidence"] = s.strip()
                out.append(a)
            elif re.search(r"\b(raise|increase|salary (goes|is going|moves|will be)|bump|new rate|merit)", sl) and (m := re.search(MONEY, s)):
                a = _blank("raise", who(s))
                v = _money(m)
                if re.search(r"/\s?h(ou)?r|an hour|per hour|hourly", sl) or v < 500:
                    a["new_hourly_rate"] = v
                else:
                    a["new_annual_salary"] = v
                d = _dates(s, year)
                a["effective_date"] = d[0].isoformat() if d else None
                a["evidence"] = s.strip()
                out.append(a)
            elif "unpaid" in sl:
                a = _blank("unpaid_leave", who(s))
                a["unpaid_days"] = _days(s)
                d = _dates(s, year)
                a["effective_date"] = d[0].isoformat() if d else None
                a["evidence"] = s.strip()
                out.append(a)
            elif re.search(r"\b(missed|missing|add|short|forgot|extra)\b.*\bhours?\b|\bhours?\b.*\b(missed|missing|short)\b", sl) \
                    and (m := re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)", sl)):
                a = _blank("hours_correction", who(s))
                hrs = float(m.group(1))
                a["extra_hours"] = -hrs if re.search(r"\b(remove|deduct|over-?paid|too many)\b", sl) else hrs
                a["evidence"] = s.strip()
                out.append(a)

        if re.search(r"new hire|welcome aboard|\bstarts\b|start date|first day|joining us|joins us|onboard", low):
            name = _new_person(text) or ""
            a = _blank("new_hire", name)
            d = _dates(text, year)
            a["effective_date"] = d[0].isoformat() if d else None
            for s in sentences:
                if (m := re.search(MONEY, s)) and not re.search(r"bonus", s, re.I):
                    v = _money(m)
                    if v < 500:
                        a["new_hourly_rate"] = v
                    else:
                        a["new_annual_salary"] = v
                    break
            a["sin_provided"] = _provided(low, r"\bsin\b|social insurance")
            a["td1_provided"] = _provided(low, r"\btd1")
            a["evidence"] = next((s.strip() for s in sentences if re.search(r"start|first day|join", s, re.I)), "")
            out.append(a)
        # One adjustment per type and person per email (several sentences often restate it).
        seen, unique = set(), []
        for a in out:
            k = (a["type"], a["employee_name"])
            if k not in seen:
                seen.add(k)
                unique.append(a)
        return unique


def _provided(low: str, pat: str) -> bool | None:
    for s in re.split(r"(?<=[.!?])\s+|\n+", low):
        if re.search(pat, s):
            if re.search(r"\b(not|no|missing|pending|to follow|still need|haven't|hasn't|waiting|outstanding|yet)\b", s):
                return False
            return True
    return None


# ---------------------------------------------------------------- Claude

class ClaudeExtractor:
    name = "claude"

    def __init__(self, fixtures_dir: Path = FIXTURES) -> None:
        self.fixtures_dir = fixtures_dir

    def extract(self, mail: Mail, *, roster: list[str], period_start: date) -> list[dict]:
        prompt = (
            f"Pay period starts {period_start.isoformat()}.\n"
            f"Employee roster: {', '.join(roster)}\n\n"
            f"From: {mail.sender}\nDate: {mail.date}\nSubject: {mail.subject}\n\n{mail.body}"
        )
        out = llm.extract(module="payroll", task="change_request", system=SYSTEM, prompt=prompt,
                          schema=ADJUSTMENT_SCHEMA, fixtures_dir=self.fixtures_dir, max_tokens=1024)
        return [{k: a.get(k) for k in FIELDS + ["evidence"]} for a in out.get("adjustments", [])]


def default_extractor(choice: str | None = None) -> Extractor:
    if choice == "claude" or (choice is None and os.environ.get("AFO_LLM_MODE") in {"live", "record"}):
        return ClaudeExtractor()
    return RulesExtractor()
