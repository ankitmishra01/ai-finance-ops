"""Runs every module's eval (evals/<module>.py exposing run() -> dict) and prints a summary table."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

results = {}
for f in sorted(Path(__file__).parent.glob("eval_*.py")):
    spec = importlib.util.spec_from_file_location(f.stem, f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    results[f.stem.removeprefix("eval_")] = mod.run()

print(json.dumps(results, indent=2))
(ROOT / "evals" / "results.json").write_text(json.dumps(results, indent=2) + "\n")
