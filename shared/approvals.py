"""Human-in-the-loop gate.

Agents never post, pay, file or send. They propose; a person approves. Each
proposal is a short human-readable summary plus the payload that would be
committed. In the CLI the reviewer types y/n per item; tests and demos pass
`auto=True`, and `--dry-run` (the default in every run.py) stops after proposals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Proposal:
    kind: str
    summary: str
    payload: dict
    flags: list[str] = field(default_factory=list)
    decision: str = "pending"


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
        path.write_text(json.dumps([p.__dict__ for p in self.items], indent=2, default=str))
