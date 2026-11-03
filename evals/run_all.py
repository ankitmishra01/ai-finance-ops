"""Score every module's eval (evals/eval_<module>.py exposing run() -> dict), rules versus Claude.

    uv run python evals/run_all.py                                   # rules baseline only (CI)
    uv run python evals/run_all.py --extractor claude --record       # + Claude through the API, saving fixtures
    uv run python evals/run_all.py --extractor claude-cli --record   # + Claude through the local `claude` CLI
    uv run python evals/run_all.py --extractor claude --model claude-opus-5-5

Writes evals/results/results.json (every score, the run's settings and its token costs) and
evals/results/comparison.md (the per-module rules-vs-Claude table and the cost per document). When
Claude is scored, the same table replaces the block between the eval-table markers in README.md
(--no-readme to skip; --readme to refresh it after a rules-only run).

Costs come from runs/token_log.jsonl rows tagged with this run's id, priced with
shared/pricing.yaml. Nothing here invents a score: a module whose Claude extractor did not run
says "not run".
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_DIR = ROOT / "evals" / "results"
README = ROOT / "README.md"
START, END = "<!-- eval-table:start -->", "<!-- eval-table:end -->"


def _get(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _pct(v):
    return None if v is None else f"{v * 100:.1f}%"


def _lines(d):
    return None if not isinstance(d, dict) else f"{d['lines_matching']} of {d['lines_total']}"


# Headline metrics per module: (label, getter on one extractor's score dict).
HEADLINES = {
    "invoicing": [
        ("Customer details: mean field accuracy", lambda s: _pct(_get(s, "mean_field_accuracy"))),
        ("Customer details: all fields correct", lambda s: _pct(_get(s, "all_fields_correct"))),
    ],
    "bills": [
        ("Classification, templated", lambda s: _pct(_get(s, "templated", "classification_accuracy"))),
        ("Field accuracy, templated", lambda s: _pct(_get(s, "templated", "field_accuracy_mean"))),
        ("Classification, held-out formats", lambda s: _pct(_get(s, "holdout", "classification_accuracy"))),
        ("Field accuracy, held-out formats", lambda s: _pct(_get(s, "holdout", "field_accuracy_mean"))),
        ("Flag recall", lambda s: _pct(_get(s, "flags", "recall"))),
    ],
    "payroll": [
        ("Change requests: precision", lambda s: _pct(_get(s, "adjustment_precision"))),
        ("Change requests: recall", lambda s: _pct(_get(s, "adjustment_recall"))),
        ("Change requests: field accuracy", lambda s: _pct(_get(s, "field_accuracy"))),
    ],
    "gst_hst": [
        ("Category, eval set", lambda s: _pct(_get(s, "eval_set", "category", "accuracy"))),
        ("ITC treatment, eval set", lambda s: _pct(_get(s, "eval_set", "itc_treatment_effective", "accuracy"))),
        ("GST34 lines matching", lambda s: _lines(_get(s, "gst34_vs_expected"))),
    ],
    "bookkeeping": [
        ("Receipts: all fields correct", lambda s: _pct(_get(s, "receipt_fields", "all_fields_correct"))),
        ("Matching precision", lambda s: _pct(_get(s, "matching", "precision"))),
        ("Matching recall", lambda s: _pct(_get(s, "matching", "recall"))),
        ("Categorisation accuracy", lambda s: _pct(_get(s, "categorisation", "accuracy"))),
    ],
}
# Where each eval puts its two score blocks.
KEYS = {"gst_hst": ("rules_extractor", "claude_extractor")}


def _cell(score, getter) -> str:
    if score is None:
        return "not run"
    if isinstance(score, str):
        return "not run" if score.startswith("not ") else score
    v = getter(score)
    return "n/a" if v is None else v


def comparison_table(results: dict, meta: dict) -> str:
    claude_label = "Claude"
    if meta.get("claude_ran"):
        via = "CLI, subscription" if meta["backend"] == "cli" else "API"
        claude_label = f"Claude ({', '.join(meta['models_reported']) or meta['model_requested']}, {via})"
    rows = [f"| Module | Metric | Rules baseline | {claude_label} |", "|---|---|---|---|"]
    for module, metrics in HEADLINES.items():
        res = results.get(module)
        if res is None:
            continue
        if "error" in res and len(res) == 1:
            rows.append(f"| {module} | eval failed | {res['error']} | |")
            continue
        rk, ck = KEYS.get(module, ("rules", "claude"))
        for label, getter in metrics:
            rows.append(f"| {module} | {label} | {_cell(res.get(rk), getter)} | {_cell(res.get(ck), getter)} |")
    return "\n".join(rows)


def cost_table(costs: dict) -> str:
    if not costs:
        return "No model calls in this run, so no token cost to report."
    rows = ["| Module | Documents | Calls | Tokens per document (in / out) | USD per document (est.) "
            "| USD total (est.) | Billing |", "|---|---|---|---|---|---|---|"]
    for module, c in sorted(costs.items()):
        n = c["documents"] or 1
        priced = c["calls"] - c["unpriced_calls"]
        if priced:
            usd_doc, usd_total = f"{c['cost_usd'] / n:.5f}", f"{c['cost_usd']:.4f}"
            tok = f"{c['input_tokens'] / n:,.0f} / {c['output_tokens'] / n:,.0f}"
        else:
            usd_doc = usd_total = "none reported"
            tok = "not reported"
        if c["unpriced_calls"] and priced:
            usd_total += f" ({c['unpriced_calls']} calls without usage)"
        rows.append(f"| {module} | {c['documents']} | {c['calls']} | {tok} | {usd_doc} | {usd_total} "
                    f"| {', '.join(c['billing'])} |")
    return "\n".join(rows)


def render(results: dict, meta: dict, costs: dict) -> str:
    lines = [
        f"_Generated by `evals/run_all.py` on {meta['generated']}; extractor `{meta['extractor']}`, "
        f"mode `{meta['mode']}`._",
        "",
        comparison_table(results, meta),
        "",
    ]
    if meta.get("claude_ran"):
        if meta["backend"] == "cli":
            lines.append(
                "Claude scores above are real model outputs produced through the local `claude` CLI under a "
                f"Claude subscription; model id as reported by the CLI: {', '.join(meta['models_reported']) or 'not reported'}. "
                "A subscription is not billed per token, so the USD column is an API-equivalent estimate at "
                "the rates in `shared/pricing.yaml`, shown only where the CLI reported token usage.")
        else:
            lines.append(
                "Claude scores above are real model outputs from the Anthropic API; model id as returned by "
                f"the API: {', '.join(meta['models_reported']) or meta['model_requested']}. USD is estimated from "
                "the token log at the rates in `shared/pricing.yaml`.")
        lines += ["", cost_table(costs)]
    else:
        lines.append("Claude has not been scored in this run. To score it: "
                     "`uv run python evals/run_all.py --extractor claude --record` (API key) or "
                     "`--extractor claude-cli --record` (local Claude Code subscription).")
    return "\n".join(lines) + "\n"


def update_readme(block: str, path: Path = README) -> bool:
    text = path.read_text()
    if START not in text or END not in text:
        return False
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    path.write_text(f"{head}{START}\n{block}{END}{tail}")
    return True


def configure(args) -> str:
    """Set the env the evals and shared/llm.py read. Returns the effective extractor."""
    extractor = args.extractor
    if extractor == "rules" and os.environ.get("AFO_LLM_MODE") in ("live", "record"):
        # Old style: AFO_LLM_MODE=record uv run python evals/run_all.py
        extractor = "claude-cli" if os.environ.get("AFO_LLM_BACKEND") == "cli" else "claude"
    if extractor == "rules":
        os.environ["AFO_LLM_MODE"] = "replay"
        return extractor
    os.environ["AFO_LLM_MODE"] = "record" if args.record else os.environ.get("AFO_LLM_MODE", "live")
    if os.environ["AFO_LLM_MODE"] not in ("live", "record"):
        os.environ["AFO_LLM_MODE"] = "live"
    os.environ["AFO_LLM_BACKEND"] = "cli" if extractor == "claude-cli" else "api"
    if args.model:
        os.environ["AFO_MODEL"] = args.model
    return extractor


def run_evals(eval_dir: Path = Path(__file__).parent) -> dict:
    results = {}
    for f in sorted(eval_dir.glob("eval_*.py")):
        name = f.stem.removeprefix("eval_")
        spec = importlib.util.spec_from_file_location(f.stem, f)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            results[name] = mod.run()
        except Exception as e:  # one module's failure (e.g. a model error) must not lose the others
            results[name] = {"error": f"{type(e).__name__}: {e}"}
    return results


def main(argv: list[str] | None = None, *, eval_dir: Path | None = None, results_dir: Path = RESULTS_DIR,
         readme: Path = README) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extractor", choices=["rules", "claude", "claude-cli"], default="rules",
                    help="rules: baseline only. claude: also score Claude via the API. "
                         "claude-cli: also score Claude via the local `claude` CLI.")
    ap.add_argument("--record", action="store_true", help="save each Claude response as a replayable fixture")
    ap.add_argument("--model", help="model id (default: AFO_MODEL, else default_model in shared/pricing.yaml)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--readme", dest="readme", action="store_true", default=None, help="update README.md")
    g.add_argument("--no-readme", dest="readme", action="store_false", help="leave README.md alone")
    args = ap.parse_args(argv)

    extractor = configure(args)
    if extractor == "claude-cli" and not shutil.which(os.environ.get("AFO_CLAUDE_BIN", "claude")):
        ap.error("the `claude` CLI is not on PATH (set AFO_CLAUDE_BIN to its location)")

    from shared import llm  # after configure(), so it sees AFO_MODEL

    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    os.environ["AFO_RUN_ID"] = run_id
    results = run_evals(eval_dir or Path(__file__).parent)
    costs = llm.token_report(run_id=run_id) if extractor != "rules" else {}
    reported = sorted({m for c in costs.values() for m in c["models"]})
    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "run_id": run_id,
        "extractor": extractor,
        "mode": os.environ["AFO_LLM_MODE"],
        "backend": os.environ.get("AFO_LLM_BACKEND", "api") if extractor != "rules" else None,
        "model_requested": llm.current_model() if extractor != "rules" else None,
        "models_reported": reported,
        "claude_ran": extractor != "rules" and any(
            isinstance(r.get(KEYS.get(m, ("rules", "claude"))[1]), dict) for m, r in results.items()),
    }

    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "results.json").write_text(
        json.dumps({"meta": meta, "cost_by_module": costs, "results": results}, indent=2) + "\n")
    block = render(results, meta, costs)
    (results_dir / "comparison.md").write_text("# Rules vs Claude\n\n" + block)

    want_readme = args.readme if args.readme is not None else extractor != "rules"
    if want_readme and not update_readme(block, readme):
        print(f"warning: {readme} has no {START} ... {END} block; README not updated", file=sys.stderr)

    print(json.dumps(results, indent=2))
    print("\n" + block)
    return {"meta": meta, "costs": costs, "results": results}


if __name__ == "__main__":
    main()
