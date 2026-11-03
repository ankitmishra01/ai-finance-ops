"""evals/run_all.py plumbing, end to end, with a stand-in eval module and fake Claude backends.

The stand-in eval below is a test double: its "rules" numbers are constants and its "Claude"
numbers are whatever the fake client returns. Nothing here is a real score.
"""

import importlib.util
import json
import subprocess
from types import SimpleNamespace as NS

import pytest
import schema_mocks as M

from shared import llm

ROOT = llm.ROOT
spec = importlib.util.spec_from_file_location("run_all", ROOT / "evals" / "run_all.py")
run_all = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_all)

STAND_IN_EVAL = '''
import os
from pathlib import Path
from shared import llm

def run():
    out = {"task": "stand-in", "rules": {"mean_field_accuracy": 0.5, "all_fields_correct": 0.25}}
    if os.environ.get("AFO_LLM_MODE") in ("live", "record"):
        scores = []
        for doc in ("doc one", "doc two"):
            o = llm.extract(module="invoicing", task="customer_details", system="s", prompt=doc,
                            schema={"type": "object", "properties": {"acc": {"type": "number"}}, "required": ["acc"]},
                            fixtures_dir=Path(os.environ["STAND_IN_FIXTURES"]))
            scores.append(o["acc"])
        out["claude"] = {"mean_field_accuracy": sum(scores) / 2, "all_fields_correct": min(scores)}
    else:
        out["claude"] = "not run (stand-in)"
    return out
'''

README = "# x\n\nintro\n\n<!-- eval-table:start -->\nold table\n<!-- eval-table:end -->\n\nfooter\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("AFO_LLM_MODE", "AFO_LLM_BACKEND", "AFO_MODEL", "AFO_RUN_ID", "AFO_CLAUDE_BIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STAND_IN_FIXTURES", str(tmp_path / "fixtures"))
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "token_log.jsonl")
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "eval_invoicing.py").write_text(STAND_IN_EVAL)
    (tmp_path / "README.md").write_text(README)
    yield tmp_path
    llm.set_client(None)
    llm.set_cli_runner(None)


def run(tmp, *argv):
    return run_all.main(list(argv), eval_dir=tmp / "evals", results_dir=tmp / "results", readme=tmp / "README.md")


def test_rules_only_writes_results_and_leaves_readme(env):
    out = run(env)
    assert out["meta"]["claude_ran"] is False and out["costs"] == {}
    table = (env / "results" / "comparison.md").read_text()
    assert "| invoicing | Customer details: mean field accuracy | 50.0% | not run |" in table
    assert "Claude has not been scored" in table
    assert (env / "README.md").read_text() == README
    saved = json.loads((env / "results" / "results.json").read_text())
    assert saved["results"]["invoicing"]["rules"]["mean_field_accuracy"] == 0.5


def test_claude_api_one_command(env):
    replies = [NS(content=[NS(type="tool_use", name="submit", id="t", input={"acc": a})], stop_reason="tool_use",
                  model="claude-sonnet-5-5",
                  usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)) for a in (0.75, 1.0)]
    reqs = []
    llm.set_client(NS(messages=NS(create=lambda **kw: reqs.append(kw) or replies.pop(0))))
    out = run(env, "--extractor", "claude", "--record")
    assert out["meta"]["mode"] == "record" and out["meta"]["backend"] == "api"
    assert out["meta"]["models_reported"] == ["claude-sonnet-5-5"] and reqs[0]["model"] == "claude-sonnet-5-5"
    assert len(list((env / "fixtures").glob("customer_details-*.json"))) == 2
    c = out["costs"]["invoicing"]
    assert (c["documents"], c["calls"]) == (2, 2) and c["cost_usd"] == pytest.approx(2 * (1000 * 2 + 200 * 10) / 1e6)
    readme = (env / "README.md").read_text()
    assert "old table" not in readme and readme.startswith("# x\n\nintro\n\n<!-- eval-table:start -->\n")
    assert readme.endswith("<!-- eval-table:end -->\n\nfooter\n")
    assert "| Rules baseline | Claude (claude-sonnet-5-5, API) |" in readme
    assert "| invoicing | Customer details: mean field accuracy | 50.0% | 87.5% |" in readme
    assert "| invoicing | 2 | 2 | 1,000 / 200 | 0.00400 | 0.0080 | api |" in readme


def test_claude_cli_one_command_with_model_override(env, monkeypatch):
    monkeypatch.setattr(run_all.shutil, "which", lambda name: "/usr/local/bin/claude")
    outs = [M.cli_result(json.dumps({"acc": 1.0}), usage=False, model="claude-opus-5-5"),
            M.cli_result(json.dumps({"acc": 0.5}), usage=False, model="claude-opus-5-5")]
    cmds = []

    def runner(cmd, **kw):
        cmds.append(cmd)
        out = outs.pop(0)
        out["modelUsage"] = {"claude-opus-5-5": {"outputTokens": 10}}  # the CLI names the model it used
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    llm.set_cli_runner(runner)
    out = run(env, "--extractor", "claude-cli", "--model", "claude-opus-5-5")
    assert out["meta"]["mode"] == "live" and out["meta"]["backend"] == "cli"
    assert all(c[c.index("--model") + 1] == "claude-opus-5-5" for c in cmds)
    readme = (env / "README.md").read_text()
    assert "Claude (claude-opus-5-5, CLI, subscription)" in readme
    assert "under a Claude subscription; model id as reported by the CLI: claude-opus-5-5" in readme
    assert "| invoicing | 2 | 2 | not reported | none reported | none reported | subscription (no per-token billing) |" in readme
    assert not (env / "fixtures").exists()  # live, not record


def test_cli_missing_binary_is_a_clear_error(env, monkeypatch):
    monkeypatch.setattr(run_all.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        run(env, "--extractor", "claude-cli")


def test_a_failing_module_does_not_lose_the_others(env):
    (env / "evals" / "eval_payroll.py").write_text("def run():\n    raise RuntimeError('model said no')\n")
    out = run(env, "--readme")
    assert out["results"]["payroll"] == {"error": "RuntimeError: model said no"}
    assert "| payroll | eval failed | RuntimeError: model said no | |" in (env / "README.md").read_text()


def test_repo_readme_has_the_markers():
    text = (ROOT / "README.md").read_text()
    assert run_all.START in text and run_all.END in text
