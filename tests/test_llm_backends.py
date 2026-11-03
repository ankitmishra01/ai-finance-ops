"""The two Claude backends, tested with a fake Anthropic client and a fake subprocess.

Nothing here calls the API or the real `claude` CLI. The fake replies are labelled test doubles;
they exercise the plumbing (schema checks, retries, record/replay, token log), not model quality.
"""

import json
import subprocess
from types import SimpleNamespace as NS

import pytest
import schema_mocks as M
import yaml

from shared import llm
from shared.schema import problems

SCHEMA = {"type": "object", "properties": {"vendor": {"type": ["string", "null"]},
                                           "total": {"type": "number", "minimum": 0}},
          "required": ["vendor", "total"], "additionalProperties": False}
GOOD = {"vendor": "Synthetic Paper Co.", "total": 113.0}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "TOKEN_LOG", tmp_path / "runs" / "token_log.jsonl")
    for k in ("AFO_MODEL", "AFO_LLM_BACKEND", "AFO_RUN_ID", "AFO_EFFORT", "AFO_CLAUDE_CLI_MODEL",
              "AFO_CLAUDE_CLI_SAFE_MODE"):
        monkeypatch.delenv(k, raising=False)
    yield
    llm.set_client(None)
    llm.set_cli_runner(None)


def call(tmp_path, **kw):
    return llm.extract(module="bills", task="classify_extract", system="rules", prompt="email text",
                       schema=SCHEMA, fixtures_dir=tmp_path / "fixtures", **kw)


def log_rows():
    return [json.loads(x) for x in llm.TOKEN_LOG.read_text().splitlines()]


# --- fake Anthropic client --------------------------------------------------------------------------

class FakeMessages:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def create(self, **kw):
        self.requests.append(kw)
        return self.replies.pop(0)


def reply(tool_input=None, *, stop="tool_use", model="claude-sonnet-5-5"):
    content = [NS(type="text", text="ok")]
    if tool_input is not None:
        content.append(NS(type="tool_use", name="submit", id="toolu_fake", input=tool_input))
    return NS(content=content, stop_reason=stop, model=model,
              usage=NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=2000,
                       cache_creation_input_tokens=0))


def fake_client(*replies):
    msgs = FakeMessages(replies)
    llm.set_client(NS(messages=msgs))
    return msgs


def test_pricing_config_cites_anthropic_and_defaults_to_sonnet_5_5():
    text = (llm.ROOT / "shared" / "pricing.yaml").read_text()
    cfg = yaml.safe_load(text)
    assert cfg["default_model"] == "claude-sonnet-5-5" == llm.current_model()
    assert "https://platform.claude.com/docs/en/about-claude/pricing" in text
    assert cfg["models"]["claude-sonnet-5-5"] == {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.50}


def test_model_is_configurable(monkeypatch):
    monkeypatch.setenv("AFO_MODEL", "claude-opus-5-5")
    assert llm.current_model() == "claude-opus-5-5"


def test_api_backend_uses_auto_tool_choice_logs_cost_and_records(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "record")
    monkeypatch.setenv("AFO_RUN_ID", "run-1")
    msgs = fake_client(reply(GOOD))
    assert call(tmp_path) == GOOD
    req = msgs.requests[0]
    assert req["model"] == "claude-sonnet-5-5" and req["tool_choice"] == {"type": "auto"}
    assert req["max_tokens"] >= llm.MIN_MAX_TOKENS
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "submit" in req["messages"][0]["content"]
    row = log_rows()[0]
    # 1000 in * $2 + 100 out * $10 + 2000 cache reads * $0.10, per million tokens
    assert row["cost_usd"] == pytest.approx(0.0032) and row["billing"] == "api" and row["run_id"] == "run-1"
    fixture = next((tmp_path / "fixtures").glob("classify_extract-*.json"))
    saved = json.loads(fixture.read_text())
    assert saved == {"model": "claude-sonnet-5-5", "backend": "api", "output": GOOD}
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    llm.set_client(NS(messages=FakeMessages([])))  # replay must not call the client
    assert call(tmp_path) == GOOD


def test_api_backend_retries_once_on_schema_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    msgs = fake_client(reply({"vendor": "x", "total": -1}), reply(GOOD))
    assert call(tmp_path) == GOOD
    retry = msgs.requests[1]["messages"]
    assert retry[-1]["content"][0]["type"] == "tool_result" and retry[-1]["content"][0]["is_error"] is True
    assert len(log_rows()) == 2


def test_api_backend_gives_up_after_retry_and_on_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    fake_client(reply(None), reply(None))
    with pytest.raises(llm.ModelOutputError):
        call(tmp_path)
    fake_client(reply(None, stop="refusal"))
    with pytest.raises(llm.ModelOutputError, match="refusal"):
        call(tmp_path)
    assert not (tmp_path / "fixtures").exists()


# --- fake claude CLI --------------------------------------------------------------------------------

class FakeRunner:
    def __init__(self, *outputs, returncode=0):
        self.outputs, self.cmds, self.returncode = list(outputs), [], returncode

    def __call__(self, cmd, **kw):
        assert kw["capture_output"] and kw["text"] and kw["timeout"] > 0
        self.cmds.append(list(cmd))
        out = self.outputs.pop(0)
        return subprocess.CompletedProcess(cmd, self.returncode, json.dumps(out) if isinstance(out, dict) else out,
                                           "boom" if self.returncode else "")


def test_cli_backend_command_parse_log_and_record_then_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "record")
    monkeypatch.setenv("AFO_LLM_BACKEND", "cli")
    runner = FakeRunner(M.cli_result("```json\n" + json.dumps(GOOD) + "\n```", model="claude-sonnet-5-5"))
    llm.set_cli_runner(runner)
    assert call(tmp_path) == GOOD
    cmd = runner.cmds[0]
    assert cmd[:2] == ["claude", "-p"] and "JSON Schema" in cmd[2] and json.dumps(SCHEMA) in cmd[2]
    for flag in (["--output-format", "json"], ["--system-prompt", "rules"], ["--tools", ""],
                 ["--model", "claude-sonnet-5-5"]):
        i = cmd.index(flag[0])
        assert cmd[i:i + 2] == flag
    assert "--no-session-persistence" in cmd and "--safe-mode" in cmd
    row = log_rows()[0]
    assert row["backend"] == "cli" and row["billing"] == llm.SUBSCRIPTION
    assert row["model"] == "claude-sonnet-5-5" and row["input_tokens"] == 900
    assert row["cost_usd"] == pytest.approx((900 * 2 + 60 * 10) / 1e6) and row["cli_reported_cost_usd"] == 0.0024
    saved = json.loads(next((tmp_path / "fixtures").glob("*.json")).read_text())
    assert saved["backend"] == "cli" and saved["output"] == GOOD
    # The same fixture replays with the CLI backend unset (as in CI) and no runner calls.
    monkeypatch.setenv("AFO_LLM_MODE", "replay")
    monkeypatch.delenv("AFO_LLM_BACKEND")
    llm.set_cli_runner(FakeRunner())
    assert call(tmp_path) == GOOD


def test_cli_and_api_fixtures_share_one_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "record")
    fake_client(reply(GOOD))
    call(tmp_path)
    api_files = sorted(p.name for p in (tmp_path / "fixtures").glob("*.json"))
    monkeypatch.setenv("AFO_LLM_BACKEND", "cli")
    llm.set_cli_runner(FakeRunner(M.cli_result(json.dumps(GOOD))))
    call(tmp_path)
    assert sorted(p.name for p in (tmp_path / "fixtures").glob("*.json")) == api_files


def test_cli_retries_once_on_invalid_json_and_records_subscription_without_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    monkeypatch.setenv("AFO_LLM_BACKEND", "cli")
    monkeypatch.setenv("AFO_CLAUDE_CLI_MODEL", "default")
    runner = FakeRunner(M.cli_result("Sure! Here it is: {not json", usage=False),
                        M.cli_result("Here you go: " + json.dumps(GOOD), usage=False))
    llm.set_cli_runner(runner)
    assert call(tmp_path) == GOOD
    assert "--model" not in runner.cmds[0]
    assert "could not be used" in runner.cmds[1][2]
    rows = log_rows()
    assert len(rows) == 2 and all(r["cost_usd"] is None and r["input_tokens"] is None for r in rows)
    assert all(r["billing"] == "subscription (no per-token billing)" for r in rows)
    rep = llm.token_report(llm.TOKEN_LOG)["bills"]
    assert rep["calls"] == 2 and rep["documents"] == 1 and rep["unpriced_calls"] == 2 and rep["cost_usd"] == 0.0


def test_cli_failures_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    monkeypatch.setenv("AFO_LLM_BACKEND", "cli")
    llm.set_cli_runner(FakeRunner("", returncode=1))
    with pytest.raises(llm.ModelOutputError, match="exited with 1"):
        call(tmp_path)
    llm.set_cli_runner(FakeRunner({**M.cli_result(""), "is_error": True, "result": "Not logged in"}))
    with pytest.raises(llm.ModelOutputError, match="Not logged in"):
        call(tmp_path)
    llm.set_cli_runner(FakeRunner(M.cli_result("[1, 2]"), M.cli_result('{"vendor": null}')))
    with pytest.raises(llm.ModelOutputError, match="missing required 'total'"):
        call(tmp_path)


def test_unknown_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AFO_LLM_MODE", "live")
    monkeypatch.setenv("AFO_LLM_BACKEND", "bedrock")
    with pytest.raises(ValueError):
        call(tmp_path)


def test_token_report_filters_by_run_and_counts_documents(tmp_path):
    rows = [{"run_id": "a", "module": "bills", "task": "t", "doc": "d1", "model": "m", "input_tokens": 10,
             "output_tokens": 1, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0, "cost_usd": 0.5},
            {"run_id": "a", "module": "bills", "task": "t", "doc": "d1", "model": "m", "input_tokens": 10,
             "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.25},
            {"run_id": "b", "module": "bills", "task": "t", "doc": "d2", "model": "m", "input_tokens": 99,
             "output_tokens": 9, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 9.0}]
    p = tmp_path / "log.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = llm.token_report(p, run_id="a")["bills"]
    assert (rep["calls"], rep["documents"], rep["input_tokens"], rep["output_tokens"], rep["cost_usd"]) == (2, 1, 25, 2, 0.75)
    assert llm.token_report(p)["bills"]["documents"] == 2


@pytest.mark.parametrize("value,ok", [
    ({"vendor": None, "total": 1}, True),
    ({"vendor": "x", "total": "1"}, False),
    ({"vendor": "x"}, False),
    ({"vendor": "x", "total": 1, "extra": 1}, False),
    ({"vendor": "x", "total": True}, False),
])
def test_schema_problems(value, ok):
    assert (problems(SCHEMA, value) == []) is ok


def test_schema_enum_pattern_items():
    s = {"type": "array", "items": {"type": "string", "enum": ["a", "b"], "pattern": "^[ab]$"}}
    assert problems(s, ["a", "b"]) == [] and problems(s, ["c"]) != []
