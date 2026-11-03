"""Human-in-the-loop gate.

Agents never post, pay, file or send. They propose; a person approves. Each
proposal is a short human-readable summary plus the payload that would be
committed. In the CLI the reviewer types y/n per item; tests and demos pass
`auto=True`, and `--dry-run` (the default in every run.py) stops after proposals.

Every run.py writes its queue to a JSON file under out/ (see QUEUE_FILES). Those
files are the shared store: `decide()` records a person's decision and reason in
place, and both the stored-queue CLI below and the web UI (`python -m webui`) go
through it, so the two always show the same state.

  uv run python -m shared.approvals                     # counts per module
  uv run python -m shared.approvals review 02_bills     # decide pending items at the prompt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent

# Where each module's run.py writes its queue (dry run or not). Paths are relative to the out/ folder.
QUEUE_FILES = {
    "01_invoicing": "invoicing/proposals.json",
    "02_bills": "bills/proposals.json",
    "03_payroll": "payroll/approvals.json",
    "04_gst_hst": "gst_hst/approvals.json",
    "05_bookkeeping": "bookkeeping/proposals.json",
}
DECISIONS = ("approved", "rejected")
OPEN = ("pending", "held")


@dataclass
class Proposal:
    kind: str
    summary: str
    payload: dict
    flags: list[str] = field(default_factory=list)
    decision: str = "pending"
    reason: str = ""
    decided_by: str = ""
    decided_at: str = ""


class ApprovalQueue:
    def __init__(self) -> None:
        self.items: list[Proposal] = []

    def propose(self, kind: str, summary: str, payload: dict, flags: list[str] | None = None) -> Proposal:
        p = Proposal(kind, summary, payload, flags or [])
        self.items.append(p)
        return p

    def review(self, *, auto: bool = False, ask: Callable[[str], str] = input) -> list[Proposal]:
        for p in self.items:
            if p.decision != "pending":
                continue
            if auto:
                # Auto mode still refuses anything an agent flagged for a human.
                p.decision = "held" if p.flags else "approved"
                continue
            flag_txt = f"  FLAGS: {'; '.join(p.flags)}" if p.flags else ""
            ans = ask(f"[{p.kind}] {p.summary}{flag_txt}\nApprove? [y/N] ").strip().lower()
            p.decision = "approved" if ans == "y" else "rejected"
        return [p for p in self.items if p.decision == "approved"]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps([p.__dict__ for p in self.items], indent=2, default=str))

    @classmethod
    def load(cls, path: Path) -> "ApprovalQueue":
        q = cls()
        known = {f.name for f in fields(Proposal)}
        for d in json.loads(Path(path).read_text()):
            q.items.append(Proposal(**{k: v for k, v in d.items() if k in known}))
        return q


# ---------------------------------------------------------------- stored queues

class DecisionError(ValueError):
    """A decision the gate will not record (unknown item, already decided, missing reason)."""


def out_dir() -> Path:
    """The out/ folder the run.py scripts write to. AFO_OUT overrides it (tests, the static site build)."""
    return Path(os.environ.get("AFO_OUT", ROOT / "out"))


def queue_path(module: str, base: Path | None = None) -> Path:
    if module not in QUEUE_FILES:
        raise KeyError(f"unknown module {module!r}; expected one of {', '.join(QUEUE_FILES)}")
    return (base or out_dir()) / QUEUE_FILES[module]


def load_queue(module: str, base: Path | None = None) -> ApprovalQueue | None:
    """The module's stored queue, or None if its run.py has not been run yet."""
    p = queue_path(module, base)
    return ApprovalQueue.load(p) if p.exists() else None


def decide(module: str, index: int, decision: str, reason: str = "", *, by: str = "cli",
           base: Path | None = None) -> Proposal:
    """Record a person's decision on item `index` (0-based) of a stored queue.

    Only pending or held items can be decided, so two reviewers cannot overwrite each other.
    A rejection needs a reason, and so does approving an item the agent flagged: the reason
    is the reviewer's note on why the flag does not stop it.
    """
    if decision not in DECISIONS:
        raise DecisionError(f"decision must be one of {DECISIONS}, not {decision!r}")
    reason = (reason or "").strip()
    path = queue_path(module, base)
    if not path.exists():
        raise DecisionError(f"no queue for {module}; run {module}/run.py first")
    with _locked(path):
        q = ApprovalQueue.load(path)
        if not 0 <= index < len(q.items):
            raise DecisionError(f"{module} has no item #{index + 1}")
        p = q.items[index]
        if p.decision not in OPEN:
            raise DecisionError(f"item #{index + 1} is already {p.decision}")
        if decision == "rejected" and not reason:
            raise DecisionError("a rejection needs a reason")
        if decision == "approved" and p.flags and not reason:
            raise DecisionError("this item is flagged; say why it is safe to approve")
        p.decision, p.reason, p.decided_by = decision, reason, by
        p.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        q.write(path)
    return p


def counts(q: ApprovalQueue) -> dict[str, int]:
    c = {"total": len(q.items), "pending": 0, "held": 0, "approved": 0, "rejected": 0,
         "flagged": sum(bool(p.flags) for p in q.items)}
    for p in q.items:
        c[p.decision] = c.get(p.decision, 0) + 1
    return c


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # Windows: atomic replace only
        yield
        return
    with open(path.with_name(f".{path.name}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# ---------------------------------------------------------------- CLI

def _cli(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    ap = argparse.ArgumentParser(prog="python -m shared.approvals", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="counts per module (default)")
    r = sub.add_parser("review", help="decide the open items of one module's stored queue")
    r.add_argument("module", choices=list(QUEUE_FILES))
    r.add_argument("--flagged", action="store_true", help="only items the agent flagged")
    args = ap.parse_args(argv)

    if args.cmd != "review":
        print(f"{'module':<16} {'total':>5} {'pending':>7} {'held':>5} {'approved':>8} {'rejected':>8} {'flagged':>7}")
        for m in QUEUE_FILES:
            q = load_queue(m)
            if q is None:
                print(f"{m:<16} not run yet ({m}/run.py)")
                continue
            c = counts(q)
            print(f"{m:<16} {c['total']:>5} {c['pending']:>7} {c['held']:>5} {c['approved']:>8} "
                  f"{c['rejected']:>8} {c['flagged']:>7}")
        return 0

    q = load_queue(args.module)
    if q is None:
        print(f"No queue yet. Run: uv run python {args.module}/run.py", file=sys.stderr)
        return 2
    for i, p in enumerate(q.items):
        if p.decision not in OPEN or (args.flagged and not p.flags):
            continue
        flag_txt = f"\n  FLAGS: {'; '.join(p.flags)}" if p.flags else ""
        ans = ask(f"#{i + 1} [{p.kind}] {p.summary}{flag_txt}\nApprove, reject or skip? [a/r/S] ").strip().lower()
        if ans not in {"a", "r"}:
            continue
        decision = "approved" if ans == "a" else "rejected"
        reason = ask("Reason: " if decision == "rejected" or p.flags else "Note (optional): ")
        try:
            decide(args.module, i, decision, reason, by="cli")
            print(f"  -> {decision}")
        except DecisionError as e:
            print(f"  not recorded: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
