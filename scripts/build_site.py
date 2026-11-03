"""Build the static demo site: dry-run all five agents on the synthetic data and write site/.

    uv run python scripts/build_site.py                 # writes site/
    uv run python scripts/build_site.py --out /tmp/s    # somewhere else
    uv run python scripts/build_site.py --evals evals/results.json   # reuse a saved eval run

Nothing is approved, posted, paid or filed: each run.py runs in its default dry-run mode into a
scratch folder, and the eval figures come from the output of evals/run_all.py, never from this file.
The pages use relative links only, so the site works from GitHub Pages' /ai-finance-ops/ path.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from markupsafe import Markup  # noqa: E402

from shared import approvals  # noqa: E402
from webui import evidence  # noqa: E402
from webui.render import STATIC, render  # noqa: E402

REPO_URL = "https://github.com/ankitmishra01/ai-finance-ops"
SKIP_INPUT_PARTS = {"eval", "__pycache__"}
SKIP_OUTPUT_SUFFIXES = {".db", ".lock", ".tmp"}


# ---------------------------------------------------------------- running things

def run_agent(module: evidence.Module, out_base: Path, env: dict) -> str:
    """Dry-run one agent into out_base/<its out folder>; returns its console output."""
    sub = Path(approvals.QUEUE_FILES[module.id]).parent
    cmd = [sys.executable, str(ROOT / module.id / "run.py"), "--out", str(out_base / sub)]
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{module.id}/run.py failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r.stdout


def run_evals(env: dict) -> dict:
    r = subprocess.run([sys.executable, str(ROOT / "evals" / "run_all.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"evals/run_all.py failed ({r.returncode}):\n{r.stderr}")
    return json.loads(r.stdout)


# ---------------------------------------------------------------- eval rendering

RATE_KEYS = re.compile(r"(accuracy|precision|recall|f1|exact_match_rate|share|_mean)$")


def fmt_value(key: str, v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and 0 <= v <= 1 and key not in {"similarity", "confidence"}:
        return f"{v * 100:.1f}%"
    if isinstance(v, float):
        return f"{v:,.4g}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _is_matrix(d: dict) -> bool:
    return bool(d) and all(isinstance(v, dict) and v and all(isinstance(x, int) for x in v.values())
                           for v in d.values())


def eval_sections(d: dict, path: tuple[str, ...] = ()) -> list[dict]:
    """Flatten a module's eval result into titled sections of rows, matrices and lists."""
    rows, out = [], []
    for k, v in d.items():
        if isinstance(v, dict) and _is_matrix(v):
            labels = sorted({c for r in v.values() for c in r} | set(v))
            out.append({"title": " / ".join((*path, k)).replace("_", " "), "matrix": {
                "labels": labels, "rows": [[r] + [v.get(r, {}).get(c, 0) for c in labels] for r in v]}})
        elif isinstance(v, dict):
            out.extend(eval_sections(v, (*path, k)))
        elif isinstance(v, list):
            rows.append({"key": k.replace("_", " "), "value": f"{len(v)} item{'s' if len(v) != 1 else ''}",
                         "entries": [json.dumps(x) if not isinstance(x, str) else x for x in v[:12]],
                         "more": max(0, len(v) - 12)})
        else:
            rows.append({"key": k.replace("_", " "), "value": fmt_value(k, v)})
    if rows:
        out.insert(0, {"title": " / ".join(path).replace("_", " ") or "summary", "rows": rows})
    return out


def headline(d: dict, limit: int = 4) -> list[dict]:
    """The first few rate-type figures in a module's eval result, with where they came from."""
    found = []

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in {"classification_confusion", "field_accuracy"}:
                    continue
                walk(v, (*path, k))
        elif isinstance(x, (int, float)) and not isinstance(x, bool) and RATE_KEYS.search(path[-1]):
            found.append({"value": fmt_value(path[-1], x),
                          "label": " · ".join(p.replace("_", " ") for p in path if p not in {"rules", "rules_extractor"})})

    walk(d, ())
    return found[:limit]


def not_run_notes(d: dict) -> list[str]:
    return [f"{k}: {v}" for k, v in d.items() if isinstance(v, str) and ("not run" in v or "not scored" in v)]


# ---------------------------------------------------------------- inputs and outputs

def list_inputs(slug: str) -> list[dict]:
    base = ROOT / "data" / "synthetic" / slug
    groups: dict[str, list[Path]] = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if p.is_dir() or SKIP_INPUT_PARTS & set(rel.parts) or p.name.startswith("."):
            continue
        groups.setdefault(str(rel.parent) if rel.parent != Path(".") else "", []).append(p)
    out = []
    for folder, files in groups.items():
        if folder:
            exts = Counter(f.suffix or "(none)" for f in files)
            out.append({"name": f"{folder}/", "detail": ", ".join(f"{n} {e}" for e, n in exts.most_common()),
                        "path": f"data/synthetic/{slug}/{folder}/"})
        else:
            for f in files:
                note = "gold labels, used only by the evals" if f.stem.startswith(("labels", "expected")) else ""
                out.append({"name": f.name, "detail": note or _size(f), "path": f"data/synthetic/{slug}/{f.name}"})
    return out


def sample_email(slug: str) -> dict | None:
    base = ROOT / "data" / "synthetic" / slug
    for folder in ("inbox", "mailbox"):
        emls = sorted((base / folder).glob("*.eml"))
        if emls:
            m = evidence._read_eml(emls[0])
            body = m["body"].strip()
            return {"name": f"{folder}/{emls[0].name}", "subject": m["subject"], "from": m["from"],
                    "date": m["date"], "body": body[:1200] + ("\n..." if len(body) > 1200 else ""),
                    "attachments": m["attachments"]}
    return None


def _size(p: Path) -> str:
    n = p.stat().st_size
    return f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} B"


def copy_outputs(src: Path, dest: Path) -> list[dict]:
    files = []
    for p in sorted(src.rglob("*")):
        if p.is_dir() or p.suffix in SKIP_OUTPUT_SUFFIXES or p.name.startswith("."):
            continue
        rel = p.relative_to(src)
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / rel)
        files.append({"rel": str(rel), "size": _size(p)})
    return files


def md_inline(s: str) -> Markup:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return Markup(s)


def architecture_pieces() -> list[list[Markup]]:
    """The 'The pieces' table from docs/architecture.md, so the site never drifts from the doc."""
    text = (ROOT / "docs" / "architecture.md").read_text()
    m = re.search(r"## The pieces\n+(.*?)(?:\n\n|\Z)", text, re.S)
    if not m:
        return []
    rows = [r for r in m.group(1).splitlines() if r.startswith("|")]
    return [[md_inline(c.strip()) for c in r.strip("|").split(" | ")] for r in rows[2:]]


# ---------------------------------------------------------------- build

def build(out: Path, evals_file: Path | None = None) -> dict:
    env = {**os.environ, "AFO_LLM_MODE": os.environ.get("AFO_LLM_MODE", "replay"), "PYTHONPATH": str(ROOT)}
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    shutil.copy2(STATIC / "style.css", out / "assets" / "style.css")
    (out / ".nojekyll").write_text("")

    evals = json.loads(evals_file.read_text()) if evals_file else run_evals(env)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pages = []
    with tempfile.TemporaryDirectory(prefix="afo-site-") as tmp:
        runs = Path(tmp)
        for m in evidence.MODULES:
            log = run_agent(m, runs, env)
            q = approvals.load_queue(m.id, runs)
            sub = Path(approvals.QUEUE_FILES[m.id]).parent
            page_dir = out / "modules" / m.slug
            files = copy_outputs(runs / sub, page_dir / "files")
            flagged = []
            for i, p in enumerate(q.items, 1):
                if not p.flags:
                    continue
                ev = evidence.build(m.id, i - 1, p, q, runs)
                pdf_url = None
                data = evidence.source_pdf(m.id, p, runs) if ev["pdf"] else None
                if data:
                    (page_dir / "evidence").mkdir(parents=True, exist_ok=True)
                    (page_dir / "evidence" / f"{i}.pdf").write_bytes(data)
                    pdf_url = f"evidence/{i}.pdf"
                if ev.get("package"):
                    ev["package"] = {k: Path(v).name for k, v in ev["package"].items()}
                flagged.append({"n": i, "p": p, "ev": ev, "pdf_url": pdf_url})
            result = evals.get(m.slug, {})
            page = {"m": m, "log": log.rstrip(), "files": files, "flagged": flagged, "queue": q,
                    "counts": approvals.counts(q), "kinds": Counter(p.kind for p in q.items).most_common(),
                    "inputs": list_inputs(m.slug), "sample": sample_email(m.slug),
                    "evals": eval_sections(result) if result else [], "headline": headline(result),
                    "not_run": not_run_notes(result), "intro": m.intro()}
            pages.append(page)
            (page_dir / "index.html").write_text(render(
                "site_module.html", root="../../", css="../../assets/style.css", page=page, pages=pages,
                built=built, repo=REPO_URL))

    totals = {"proposals": sum(p["counts"]["total"] for p in pages),
              "flagged": sum(p["counts"]["flagged"] for p in pages),
              "modules": len(pages), "inputs": sum(1 for _ in (ROOT / "data" / "synthetic").rglob("*.*"))}
    (out / "index.html").write_text(render(
        "site_index.html", root="", css="assets/style.css", pages=pages, totals=totals,
        pieces=architecture_pieces(), built=built, repo=REPO_URL))
    return {"pages": len(pages) + 1, **totals}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--evals", type=Path, help="use this saved evals/run_all.py output instead of re-running it")
    args = ap.parse_args(argv)
    info = build(args.out, args.evals)
    print(f"Wrote {info['pages']} pages to {args.out}/ ({info['proposals']} proposals, {info['flagged']} flagged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
