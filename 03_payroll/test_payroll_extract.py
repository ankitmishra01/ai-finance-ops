"""Extractor interface: rules baseline behaviour, schema shape, and the Claude path in replay mode."""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

from payroll_extract import (ADJUSTMENT_SCHEMA, FIELDS, ClaudeExtractor, RulesExtractor, default_extractor)
from shared import llm
from shared.mail import Mail

ROSTER = ["Danielle Johnson", "Anthony Gonzalez", "Debra Gardner"]
START = date(2026, 11, 1)


def mail(body, subject="x"):
    return Mail(Path("t.eml"), "a@b.test", subject, "Mon, 02 Nov 2026 09:00:00 -0500", body)


def test_rules_raise_with_k_and_date():
    [a] = RulesExtractor().extract(mail("Danielle's salary is going to $91k as of November 1st."),
                                   roster=ROSTER, period_start=START)
    assert (a["type"], a["employee_name"], a["new_annual_salary"], a["effective_date"]) == \
        ("raise", "Danielle Johnson", 91000.0, "2026-11-01")


def test_rules_bonus_and_hours_in_one_email():
    out = RulesExtractor().extract(mail("Anthony Gonzalez has a $750 bonus.\nDebra Gardner missed 3.5 hours last week."),
                                   roster=ROSTER, period_start=START)
    assert {(a["type"], a["employee_name"]) for a in out} == {("bonus", "Anthony Gonzalez"),
                                                               ("hours_correction", "Debra Gardner")}


def test_rules_cancelled_request_yields_nothing():
    assert RulesExtractor().extract(mail("Please disregard the bonus for Anthony; next run instead."),
                                    roster=ROSTER, period_start=START) == []


def test_rules_new_hire_missing_documents():
    [a] = RulesExtractor().extract(mail("Welcome aboard: Lisa Smith starts 11-09 at $62,000. SIN and TD1 are still to follow."),
                                   roster=ROSTER, period_start=START)
    assert a["type"] == "new_hire" and a["employee_name"] == "Lisa Smith"
    assert a["effective_date"] == "2026-11-09" and a["sin_provided"] is False and a["td1_provided"] is False


def test_output_has_every_schema_field():
    [a] = RulesExtractor().extract(mail("Bonus of $1,500 for Anthony."), roster=ROSTER, period_start=START)
    assert set(FIELDS) <= set(a)
    assert set(ADJUSTMENT_SCHEMA["properties"]["adjustments"]["items"]["required"]) == set(FIELDS) | {"evidence"}


def test_claude_extractor_replays_recorded_fixture(tmp_path, monkeypatch):
    """No fixture is committed (no API key exists); this checks the plumbing with a temp one."""
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    with pytest.raises(llm.FixtureMissing):
        ClaudeExtractor(tmp_path).extract(mail("Bonus of $1,500 for Anthony."), roster=ROSTER, period_start=START)


def test_default_extractor_selection(monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    assert default_extractor().name == "rules"
    assert default_extractor("claude").name == "claude"
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    assert default_extractor().name == "claude"
    assert default_extractor("rules").name == "rules"


def test_eval_runs_and_reports_real_numbers():
    path = Path(__file__).resolve().parent.parent / "evals" / "eval_payroll.py"
    spec = importlib.util.spec_from_file_location("eval_payroll", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.score(RulesExtractor())
    assert r["emails"] >= 15 and r["labelled_adjustments"] >= 15
    assert 0 <= r["email_exact_match"] <= 1 and 0 <= r["adjustment_f1"] <= 1
