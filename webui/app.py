"""Local review screen for the approval queues of all five agents.

It reads and writes the same queue files as the CLI (shared/approvals.py), so a decision made
here shows up in `python -m shared.approvals` and the other way round. It never posts, pays or
files anything itself: it records a person's decision and reason against the proposal.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from shared import approvals
from webui import evidence
from webui.render import STATIC, render

STATUSES = ("open", "pending", "held", "approved", "rejected", "all")


def create_app(out: Path | None = None) -> FastAPI:
    """`out` is the folder the run.py scripts wrote to (default: out/, or AFO_OUT)."""
    app = FastAPI(title="ai-finance-ops approvals", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def base() -> Path:
        return out or approvals.out_dir()

    def queue_or_404(module: str):
        if module not in approvals.QUEUE_FILES:
            raise HTTPException(404, f"unknown module {module}")
        q = approvals.load_queue(module, base())
        if q is None:
            raise HTTPException(404, f"no queue for {module}; run {module}/run.py first")
        return q

    def item_or_404(module: str, n: int):
        q = queue_or_404(module)
        if not 1 <= n <= len(q.items):
            raise HTTPException(404, f"{module} has no item #{n}")
        return q, q.items[n - 1]

    def overview() -> list[dict]:
        rows = []
        for m in evidence.MODULES:
            q = approvals.load_queue(m.id, base())
            rows.append({"module": m, "queue": q, "counts": approvals.counts(q) if q else None})
        return rows

    @app.get("/", response_class=HTMLResponse)
    def index(module: str = "", status: str = "open", flagged: bool = False):
        if status not in STATUSES:
            status = "open"
        mods = overview()
        items = []
        for row in mods:
            m, q = row["module"], row["queue"]
            if q is None or (module and m.id != module):
                continue
            for i, p in enumerate(q.items, 1):
                if status == "open" and p.decision not in approvals.OPEN:
                    continue
                if status not in {"open", "all"} and p.decision != status:
                    continue
                if flagged and not p.flags:
                    continue
                items.append({"module": m, "n": i, "p": p})

        def link(**kw) -> str:
            q = {"module": module, "status": status, "flagged": "1" if flagged else ""}
            q.update(kw)
            return "/?" + urlencode({k: v for k, v in q.items() if v})

        missing = [r["module"] for r in mods if r["queue"] is None]
        return render("ui_queue.html", root="/", mods=mods, items=items, module=module, status=status,
                      flagged=flagged, statuses=STATUSES, link=link, missing=missing, out=base())

    @app.get("/item/{module}/{n}", response_class=HTMLResponse)
    def item(module: str, n: int, error: str = ""):
        q, p = item_or_404(module, n)
        ev = evidence.build(module, n - 1, p, q, base())
        open_ns = [i for i, x in enumerate(q.items, 1) if x.decision in approvals.OPEN]
        nxt = next((i for i in open_ns if i > n), None) or next((i for i in open_ns if i != n), None)
        return render("ui_item.html", root="/", m=evidence.BY_ID[module], n=n, p=p, ev=ev, total=len(q.items),
                      error=error, next_open=nxt, open_count=len(open_ns), is_open=p.decision in approvals.OPEN,
                      pdf_url=f"/evidence/{module}/{n}.pdf" if ev["pdf"] else None)

    @app.post("/item/{module}/{n}/decide")
    async def decide(module: str, n: int, request: Request):
        item_or_404(module, n)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        decision = {"approve": "approved", "reject": "rejected"}.get(form.get("action", ""), "")
        try:
            approvals.decide(module, n - 1, decision, form.get("reason", ""), by="web", base=base())
        except approvals.DecisionError as e:
            return RedirectResponse(f"/item/{module}/{n}?{urlencode({'error': str(e)})}", status_code=303)
        nxt = form.get("next", "")
        target = f"/item/{module}/{int(nxt)}" if nxt.isdigit() else f"/item/{module}/{n}"
        return RedirectResponse(target, status_code=303)

    @app.get("/evidence/{module}/{n}.pdf")
    def pdf(module: str, n: int):
        _, p = item_or_404(module, n)
        data = evidence.source_pdf(module, p, base())
        if data is None:
            raise HTTPException(404, "no PDF for this item")
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{module}-{n}.pdf"'})

    @app.get("/api/queue")
    def api_queue():
        return JSONResponse({r["module"].id: {"counts": r["counts"],
                                              "items": [p.__dict__ for p in r["queue"].items] if r["queue"] else None}
                             for r in overview()})

    return app


app = create_app()
