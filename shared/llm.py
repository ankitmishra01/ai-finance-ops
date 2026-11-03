"""Thin Claude wrapper used by every module.

Three things matter here:
1. Structured output only: every call forces a single tool call whose input is
   validated against a JSON schema, so downstream code never parses prose.
2. Record/replay: AFO_LLM_MODE=replay (the default) serves responses from
   fixtures on disk, so tests and CI run with no API key and no cost.
   AFO_LLM_MODE=live calls the API; AFO_LLM_MODE=record calls it and saves.
3. Token analytics: every live call appends usage and estimated cost to
   runs/token_log.jsonl, tagged by module and task, so the cost of each
   automation can be set against the work it replaces.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("AFO_MODEL", "claude-sonnet-5-5")
PRICING = yaml.safe_load((ROOT / "shared" / "pricing.yaml").read_text())
TOKEN_LOG = ROOT / "runs" / "token_log.jsonl"


class FixtureMissing(RuntimeError):
    pass


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


def _key(system: str, prompt: str, schema: dict) -> str:
    blob = json.dumps({"s": system, "p": prompt, "j": schema}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _log(module: str, task: str, usage: Usage, latency_s: float) -> None:
    TOKEN_LOG.parent.mkdir(exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "module": module,
        "task": task,
        "model": MODEL,
        **usage.__dict__,
        "cost_usd": round(usage.cost_usd(MODEL), 6),
        "latency_s": round(latency_s, 2),
    }
    with TOKEN_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


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
    fixture = Path(fixtures_dir) / f"{task}-{_key(system, prompt, schema)}.json"

    if mode == "replay":
        if not fixture.exists():
            raise FixtureMissing(
                f"No recorded response at {fixture}. Run with AFO_LLM_MODE=record "
                "and ANTHROPIC_API_KEY set to create it."
            )
        return json.loads(fixture.read_text())["output"]

    import anthropic

    client = anthropic.Anthropic()
    t0 = time.time()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        # The system prompt holds the rules (VAT/GST tables, chart of accounts),
        # which repeat across every transaction in a run, so it is cached.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[{"name": "submit", "description": "Return the result.", "input_schema": schema}],
        tool_choice={"type": "tool", "name": "submit"},
        messages=[{"role": "user", "content": prompt}],
    )
    latency = time.time() - t0
    u = resp.usage
    usage = Usage(
        u.input_tokens,
        u.output_tokens,
        getattr(u, "cache_read_input_tokens", 0) or 0,
        getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    _log(module, task, usage, latency)
    output = next(b.input for b in resp.content if b.type == "tool_use")

    if mode == "record":
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps({"model": MODEL, "output": output}, indent=2))
    return output


def token_report(path: Path = TOKEN_LOG) -> dict:
    """Aggregate the token log by module: calls, tokens, cost."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        r = json.loads(line)
        m = out.setdefault(r["module"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        m["calls"] += 1
        m["input_tokens"] += r["input_tokens"] + r["cache_read_input_tokens"] + r["cache_creation_input_tokens"]
        m["output_tokens"] += r["output_tokens"]
        m["cost_usd"] = round(m["cost_usd"] + r["cost_usd"], 6)
    return out
