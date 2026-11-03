"""Start the local approval screen.

  uv run python -m webui                # http://127.0.0.1:8700, reads out/ (run ./scripts/demo.sh first)
  uv run python -m webui --refresh      # dry-run all five agents first, then serve
  uv run python -m webui --port 9000
"""

from __future__ import annotations

import argparse
import subprocess
import sys

import uvicorn

from shared.approvals import ROOT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m webui", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="keep it local: there is no login")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--refresh", action="store_true", help="dry-run all five agents before serving")
    args = ap.parse_args(argv)
    if args.refresh:
        subprocess.run(["bash", str(ROOT / "scripts" / "demo.sh")], check=True)
    print(f"Approval queue: http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    uvicorn.run("webui.app:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
