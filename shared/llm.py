"""Thin Claude wrapper used by every module.

Four things matter here:
1. Structured output only: every call asks for a single `submit` tool call (API) or a bare JSON
   object (CLI), and the answer is checked against the JSON schema in shared/schema.py before any
   module sees it, with one corrective retry. Downstream code never parses prose.
2. Record/replay: AFO_LLM_MODE=replay (the default) serves responses from fixtures on disk, so
   tests and CI run with no API key and no cost. AFO_LLM_MODE=live calls Claude;
   AFO_LLM_MODE=record calls it and saves the response. The fixture name depends only on the
   system prompt, user prompt and schema, so a response recorded through either backend replays
   the same way.
3. Two backends, chosen with AFO_LLM_BACKEND:
   - "api" (default): the Anthropic SDK, needs ANTHROPIC_API_KEY (or another SDK credential).
   - "cli": the locally installed `claude` CLI in headless mode (`claude -p ... --output-format
     json`), which runs under the signed-in Claude subscription instead of an API key.
4. Token analytics: every live call appends usage and estimated cost to runs/token_log.jsonl,
   tagged by run, module, task and document, so the cost of each automation can be set against
   the work it replaces. CLI calls under a subscription are not billed per token; their rows say
   so, and carry an API-equivalent estimate only when the CLI reports token usage.

Model: AFO_MODEL, else `default_model` in shared/pricing.yaml (claude-sonnet-5-5).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from shared.schema import problems

ROOT = Path(__file__).resolve().parent.parent
PRICING = yaml.safe_load((ROOT / "shared" / "pricing.yaml").read_text())
DEFAULT_MODEL = PRICING["default_model"]
TOKEN_LOG = ROOT / "runs" / "token_log.jsonl"
SUBSCRIPTION = "subscription (no per-token billing)"

# Thinking is on by default on current models and its tokens count against max_tokens, so a small
# per-task max_tokens could end the turn before the submit call. Requests use at least this much.
MIN_MAX_TOKENS = 8192

SUBMIT_INSTRUCTION = "\n\nAnswer by calling the `submit` tool exactly once. Do not answer in prose."
CLI_INSTRUCTION = ("\n\nReply with only a JSON object that validates against the JSON Schema below. "
                   "No prose, no code fences.\n\nJSON Schema:\n")


def current_model() -> str:
    return os.environ.get("AFO_MODEL") or DEFAULT_MODEL


MODEL = current_model()  # import-time value, kept for callers that read it


class FixtureMissing(RuntimeError):
    pass


class ModelOutputError(RuntimeError):
    """Claude refused, or twice returned something that does not fit the schema."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def cost_usd(self, model: str) -> float:
        p = PRICING["models"].get(model) or PRICING["models"]["default"]
        return (
            self.input_tokens * p["input"]
            + self.output_tokens * p["output"]
            + self.cache_read_input_tokens * p["cache_read"]
            + self.cache_creation_input_tokens * p["cache_write"]
        ) / 1_000_000


# --- injection points (tests only) -------------------------------------------------------------

_client = None
_cli_runner: Callable = subprocess.run


def set_client(client) -> None:
    """Use `client` instead of anthropic.Anthropic(). Tests pass a fake; None restores the default."""
    global _client
    _client = client


def set_cli_runner(runner: Callable | None) -> None:
    """Use `runner` instead of subprocess.run for the CLI backend. None restores the default."""
    global _cli_runner
    _cli_runner = runner or subprocess.run


def backend() -> str:
    b = os.environ.get("AFO_LLM_BACKEND", "api")
    if b not in ("api", "cli"):
        raise ValueError(f"AFO_LLM_BACKEND must be 'api' or 'cli', not {b!r}")
    return b


# --- logging ------------------------------------------------------------------------------------

def _key(system: str, prompt: str, schema: dict) -> str:
    blob = json.dumps({"s": system, "p": prompt, "j": schema}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _log(module: str, task: str, model: str, usage: Usage | None, latency_s: float, *,
         doc: str, via: str, billing: str = "api", extra: dict | None = None) -> None:
    TOKEN_LOG.parent.mkdir(exist_ok=True)
    tokens = usage.__dict__ if usage else {k: None for k in Usage().__dict__}
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": os.environ.get("AFO_RUN_ID"),
        "module": module,
        "task": task,
        "doc": doc,
        "backend": via,
        "model": model,
        **tokens,
        # An estimate at first-party API rates (shared/pricing.yaml). None when no usage was reported.
        "cost_usd": round(usage.cost_usd(model), 6) if usage else None,
        "billing": billing,
        "latency_s": round(latency_s, 2),
        **(extra or {}),
    }
    with TOKEN_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


# --- API backend --------------------------------------------------------------------------------

def _api_client():
    if _client is not None:
        return _client
    import anthropic

    return anthropic.Anthropic()


def _extract_api(module, task, system, prompt, schema, max_tokens, doc) -> tuple[dict, str]:
    client = _api_client()
    model = current_model()
    tools = [{"name": "submit", "description": "Return the result.", "input_schema": schema}]
    messages: list[dict] = [{"role": "user", "content": prompt + SUBMIT_INSTRUCTION}]
    extra = {}
    if os.environ.get("AFO_EFFORT"):
        extra["output_config"] = {"effort": os.environ["AFO_EFFORT"]}
    errs: list[str] = []
    for _ in range(2):
        t0 = time.time()
        resp = client.messages.create(
            model=model,
            max_tokens=max(max_tokens, MIN_MAX_TOKENS),
            # The system prompt holds the rules (GST/HST tables, chart of accounts), which repeat
            # across every document in a run, so it is cached.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            # Current Sonnet and Opus models reject a forced tool_choice, so the call is requested
            # in the prompt and the answer is checked against the schema here instead.
            tool_choice={"type": "auto"},
            messages=messages,
            **extra,
        )
        u = resp.usage
        usage = Usage(u.input_tokens, u.output_tokens,
                      getattr(u, "cache_read_input_tokens", 0) or 0,
                      getattr(u, "cache_creation_input_tokens", 0) or 0)
        served_by = getattr(resp, "model", None) or model
        _log(module, task, served_by, usage, time.time() - t0, doc=doc, via="api")
        if resp.stop_reason == "refusal":
            raise ModelOutputError(f"{module}/{task}: Claude declined (stop_reason=refusal)")
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == "submit"), None)
        errs = problems(schema, block.input) if block else ["no submit tool call in the reply"]
        if not errs:
            return block.input, served_by
        fix = f"That answer cannot be used: {'; '.join(errs[:5])}. Call `submit` again with a corrected answer."
        reply = ([{"type": "tool_result", "tool_use_id": block.id, "is_error": True, "content": fix}]
                 if block else fix)
        messages = messages + [{"role": "assistant", "content": resp.content}, {"role": "user", "content": reply}]
    raise ModelOutputError(f"{module}/{task}: no schema-valid answer after a retry: {'; '.join(errs[:5])}")


# --- CLI backend --------------------------------------------------------------------------------

def _parse_json_object(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    try:
        out = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("reply contains no JSON object") from None
        try:
            out = json.loads(t[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"reply is not valid JSON ({e.msg})") from None
    if not isinstance(out, dict):
        raise ValueError("reply is JSON but not an object")
    return out


def _cli_model(envelope: dict) -> str | None:
    """The model id the CLI says it used: the modelUsage entry with the most output tokens."""
    mu = envelope.get("modelUsage")
    if isinstance(mu, dict) and mu:
        return max(mu, key=lambda m: (mu[m] or {}).get("outputTokens", 0) or 0)
    return envelope.get("model")


def _cli_usage(envelope: dict) -> Usage | None:
    u = envelope.get("usage")
    if not isinstance(u, dict) or "input_tokens" not in u or "output_tokens" not in u:
        return None
    return Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                 int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))


def cli_command(system: str, prompt: str, schema: dict) -> list[str]:
    cmd = [os.environ.get("AFO_CLAUDE_BIN", "claude"), "-p", prompt + CLI_INSTRUCTION + json.dumps(schema),
           "--output-format", "json", "--system-prompt", system,
           "--tools", "",                 # extraction only: no file, shell or web tools
           "--no-session-persistence"]
    if os.environ.get("AFO_CLAUDE_CLI_SAFE_MODE", "1") != "0":
        cmd.append("--safe-mode")      # no CLAUDE.md, skills, hooks or MCP servers leaking into the eval
    if os.environ.get("AFO_CLAUDE_CLI_MODEL", "") != "default":
        cmd += ["--model", current_model()]
    return cmd


def _extract_cli(module, task, system, prompt, schema, doc) -> tuple[dict, str]:
    cmd = cli_command(system, prompt, schema)
    base_prompt = cmd[2]
    timeout = float(os.environ.get("AFO_CLAUDE_CLI_TIMEOUT", "300"))
    errs: list[str] = []
    for _ in range(2):
        t0 = time.time()
        proc = _cli_runner(cmd, capture_output=True, text=True, timeout=timeout)
        latency = time.time() - t0
        if proc.returncode != 0:
            raise ModelOutputError(f"claude CLI exited with {proc.returncode}: {(proc.stderr or '')[-500:]}")
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise ModelOutputError("claude CLI did not return JSON; is --output-format json supported?") from None
        if envelope.get("is_error"):
            raise ModelOutputError(f"claude CLI reported an error: {str(envelope.get('result'))[:500]}")
        served_by = _cli_model(envelope) or current_model()
        extra = {}
        if envelope.get("total_cost_usd") is not None:
            extra["cli_reported_cost_usd"] = envelope["total_cost_usd"]
        _log(module, task, served_by, _cli_usage(envelope), latency, doc=doc, via="cli",
             billing=SUBSCRIPTION, extra=extra)
        try:
            out = _parse_json_object(str(envelope.get("result", "")))
            errs = problems(schema, out)
        except ValueError as e:
            errs = [str(e)]
        if not errs:
            return out, served_by
        cmd[2] = (base_prompt + "\n\nYour previous reply could not be used: " + "; ".join(errs[:5])
                  + ". Reply again with only the corrected JSON object.")
    raise ModelOutputError(f"{module}/{task}: no schema-valid JSON from the claude CLI after a retry: "
                           f"{'; '.join(errs[:5])}")


# --- entry point --------------------------------------------------------------------------------

def extract(
    *,
    module: str,
    task: str,
    system: str,
    prompt: str,
    schema: dict,
    fixtures_dir: Path,
    max_tokens: int = 2048,
) -> dict:
    """Return a dict matching `schema`, from a fixture or from Claude."""
    mode = os.environ.get("AFO_LLM_MODE", "replay")
    key = _key(system, prompt, schema)
    fixture = Path(fixtures_dir) / f"{task}-{key}.json"

    if mode == "replay":
        if not fixture.exists():
            raise FixtureMissing(
                f"No recorded response at {fixture}. Run with AFO_LLM_MODE=record "
                "and ANTHROPIC_API_KEY set (or AFO_LLM_BACKEND=cli) to create it."
            )
        return json.loads(fixture.read_text())["output"]

    via = backend()
    if via == "cli":
        output, served_by = _extract_cli(module, task, system, prompt, schema, key)
    else:
        output, served_by = _extract_api(module, task, system, prompt, schema, max_tokens, key)

    if mode == "record":
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps({"model": served_by, "backend": via, "output": output}, indent=2))
    return output


def token_report(path: Path | None = None, run_id: str | None = None) -> dict:
    """Aggregate the token log by module: calls, documents, tokens, estimated cost.

    `documents` counts distinct inputs (a retry is a second call on the same document), so
    per-document figures include the cost of retries. `cost_usd` sums only rows that carry an
    estimate; `unpriced_calls` counts the rest (CLI calls that reported no usage).
    """
    path = Path(path) if path is not None else TOKEN_LOG
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    docs: dict[str, set] = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if run_id is not None and r.get("run_id") != run_id:
            continue
        m = out.setdefault(r["module"], {"calls": 0, "documents": 0, "input_tokens": 0, "output_tokens": 0,
                                         "cost_usd": 0.0, "unpriced_calls": 0, "models": [], "billing": []})
        m["calls"] += 1
        docs.setdefault(r["module"], set()).add((r.get("task"), r.get("doc") or f"call-{m['calls']}"))
        m["input_tokens"] += sum(r.get(k) or 0 for k in
                                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        m["output_tokens"] += r.get("output_tokens") or 0
        if r.get("cost_usd") is None:
            m["unpriced_calls"] += 1
        else:
            m["cost_usd"] = round(m["cost_usd"] + r["cost_usd"], 6)
        for field, value in (("models", r.get("model")), ("billing", r.get("billing", "api"))):
            if value and value not in m[field]:
                m[field].append(value)
    for mod, m in out.items():
        m["documents"] = len(docs[mod])
    return out
