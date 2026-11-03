"""Web review screen, the stored-queue gate it shares with the CLI, and the static site build."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared import approvals
from webui.app import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> Path:
    """Real dry runs of bills, payroll and GST/HST into a scratch out/ folder."""
    out = tmp_path_factory.mktemp("out")
    for module in ("02_bills", "03_payroll", "04_gst_hst"):
        sub = Path(approvals.QUEUE_FILES[module]).parent
        subprocess.run([sys.executable, str(ROOT / module / "run.py"), "--out", str(out / sub)],
                       cwd=ROOT, check=True, capture_output=True, env={**os.environ, "AFO_LLM_MODE": "replay"})
    return out


@pytest.fixture
def out(runs, tmp_path) -> Path:
    """A fresh copy per test, so decisions in one test do not leak into another."""
    import shutil
    dest = tmp_path / "out"
    shutil.copytree(runs, dest)
    return dest


@pytest.fixture
def client(out) -> TestClient:
    return TestClient(create_app(out))


def _first(out: Path, module: str, flagged: bool) -> int:
    q = approvals.load_queue(module, out)
    return next(i for i, p in enumerate(q.items, 1) if bool(p.flags) == flagged)


# ---------------------------------------------------------------- shared gate

def test_queue_file_roundtrip_keeps_old_files_readable(tmp_path):
    path = tmp_path / "bills" / "proposals.json"
    path.parent.mkdir()
    # A queue written before reason/decided_* existed still loads.
    path.write_text(json.dumps([{"kind": "bill", "summary": "s", "payload": {}, "flags": [], "decision": "pending"}]))
    q = approvals.ApprovalQueue.load(path)
    assert q.items[0].reason == "" and q.items[0].decision == "pending"


def test_decide_rules(tmp_path):
    q = approvals.ApprovalQueue()
    q.propose("bill", "clean", {})
    q.propose("bill", "odd", {}, flags=["total mismatch"])
    q.write(approvals.queue_path("02_bills", tmp_path))

    with pytest.raises(approvals.DecisionError, match="needs a reason"):
        approvals.decide("02_bills", 0, "rejected", "", base=tmp_path)
    with pytest.raises(approvals.DecisionError, match="flagged"):
        approvals.decide("02_bills", 1, "approved", "  ", base=tmp_path)
    with pytest.raises(approvals.DecisionError, match="no item"):
        approvals.decide("02_bills", 5, "approved", base=tmp_path)
    with pytest.raises(approvals.DecisionError, match="must be one of"):
        approvals.decide("02_bills", 0, "held", base=tmp_path)

    p = approvals.decide("02_bills", 0, "approved", base=tmp_path)
    assert p.decision == "approved" and p.decided_by == "cli" and p.decided_at
    with pytest.raises(approvals.DecisionError, match="already approved"):
        approvals.decide("02_bills", 0, "rejected", "changed my mind", base=tmp_path)
    approvals.decide("02_bills", 1, "approved", "phoned vendor, total corrected", by="web", base=tmp_path)
    c = approvals.counts(approvals.load_queue("02_bills", tmp_path))
    assert (c["approved"], c["pending"], c["flagged"]) == (2, 0, 1)


def test_cli_review_records_to_the_same_store(out, monkeypatch):
    monkeypatch.setenv("AFO_OUT", str(out))
    n = _first(out, "02_bills", flagged=True)
    q = approvals.load_queue("02_bills", out)
    open_before = sum(p.decision == "pending" for p in q.items)
    answers = iter(["s"] * (n - 1) + ["r", "vendor GST/HST number not on the registry"] + ["s"] * 100)
    assert approvals._cli(["review", "02_bills"], ask=lambda _: next(answers)) == 0
    p = approvals.load_queue("02_bills", out).items[n - 1]
    assert (p.decision, p.decided_by) == ("rejected", "cli")
    assert sum(x.decision == "pending" for x in approvals.load_queue("02_bills", out).items) == open_before - 1


# ---------------------------------------------------------------- web UI

def test_queue_page_lists_every_module(client, out):
    r = client.get("/")
    assert r.status_code == 200
    for title in ("Invoicing", "Bills", "Payroll", "GST/HST", "Bookkeeping"):
        assert title in r.text
    assert "Not run yet" in r.text            # invoicing and bookkeeping were not run in this fixture
    q = approvals.load_queue("02_bills", out)
    assert q.items[0].summary.split(":")[0] in r.text
    flagged = client.get("/?module=02_bills&flagged=1").text
    assert all(p.summary[:30] in flagged for p in q.items if p.flags)
    assert not any(p.summary[:40] in flagged for p in q.items if not p.flags)


def test_empty_out_folder_explains_what_to_run(tmp_path):
    r = TestClient(create_app(tmp_path)).get("/")
    assert r.status_code == 200 and "demo.sh" in r.text


def test_bill_item_shows_extracted_fields_flags_and_pdf(client, out):
    n = _first(out, "02_bills", flagged=True)
    p = approvals.load_queue("02_bills", out).items[n - 1]
    r = client.get(f"/item/02_bills/{n}")
    assert r.status_code == 200
    assert "Extracted fields" in r.text and p.payload["vendor"] in r.text
    assert p.flags[0].split("'")[0] in r.text.replace("&#39;", "'")
    if f"/evidence/02_bills/{n}.pdf" in r.text:
        pdf = client.get(f"/evidence/02_bills/{n}.pdf")
        assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")


def test_payroll_item_shows_run_summary(client, out):
    r = client.get(f"/item/03_payroll/{_first(out, '03_payroll', flagged=True)}")
    assert r.status_code == 200
    assert "Pay run summary" in r.text and "PD7A remittance" in r.text and "Net pay" in r.text


def test_gst_return_item_shows_gst34_lines(client, out):
    q = approvals.load_queue("04_gst_hst", out)
    n = next(i for i, p in enumerate(q.items, 1) if p.kind == "gst_hst_return")
    r = client.get(f"/item/04_gst_hst/{n}")
    assert r.status_code == 200 and "GST34 lines" in r.text and "Net tax" in r.text


def test_approve_and_reject_from_the_web_are_seen_by_the_cli(client, out):
    clean = _first(out, "02_bills", flagged=False)
    flagged = _first(out, "02_bills", flagged=True)

    r = client.post(f"/item/02_bills/{clean}/decide", data={"action": "approve", "reason": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/item/02_bills/{flagged}/decide", data={"action": "reject", "reason": "duplicate of 05.eml"})
    assert r.status_code == 200 and "rejected" in r.text

    q = approvals.load_queue("02_bills", out)
    assert (q.items[clean - 1].decision, q.items[clean - 1].decided_by) == ("approved", "web")
    assert (q.items[flagged - 1].decision, q.items[flagged - 1].reason) == ("rejected", "duplicate of 05.eml")
    page = client.get(f"/item/02_bills/{flagged}").text
    assert "duplicate of 05.eml" in page and "Your decision" not in page


def test_web_refuses_rejection_without_reason_and_flagged_approval_without_note(client, out):
    flagged = _first(out, "02_bills", flagged=True)
    for action in ("reject", "approve"):
        r = client.post(f"/item/02_bills/{flagged}/decide", data={"action": action, "reason": " "})
        assert r.status_code == 200 and 'role="alert"' in r.text
    assert approvals.load_queue("02_bills", out).items[flagged - 1].decision == "pending"


def test_unknown_module_and_item_are_404(client):
    assert client.get("/item/99_nope/1").status_code == 404
    assert client.get("/item/02_bills/999").status_code == 404
    assert client.get("/item/01_invoicing/1").status_code == 404   # not run in this fixture
    assert client.post("/item/02_bills/999/decide", data={"action": "approve"}).status_code == 404


def test_api_queue(client):
    data = client.get("/api/queue").json()
    assert data["01_invoicing"]["items"] is None
    assert data["02_bills"]["counts"]["total"] == len(data["02_bills"]["items"]) > 0


# ---------------------------------------------------------------- static site

def test_build_site(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site

    results = json.loads((ROOT / "evals" / "results.json").read_text())
    info = build_site.build(tmp_path / "site", ROOT / "evals" / "results.json")
    site = tmp_path / "site"
    assert info["pages"] == 6 and (site / "assets" / "style.css").exists() and (site / ".nojekyll").exists()
    index = (site / "index.html").read_text()
    assert "Architecture" in index and "shared/approvals.py" in index
    for slug in ("invoicing", "bills", "payroll", "gst_hst", "bookkeeping"):
        page = (site / "modules" / slug / "index.html").read_text()
        assert "Inputs" in page and "Outputs" in page and "Flags raised" in page and "Evals" in page
        assert 'href="/' not in page and 'src="/' not in page     # relative links only (GitHub Pages subpath)
    # Eval figures come from the eval output, not the page template.
    bills = (site / "modules" / "bills" / "index.html").read_text()
    assert f"{results['bills']['rules']['holdout']['field_accuracy_mean'] * 100:.1f}%" in bills
    assert any((site / "modules" / "invoicing" / "files" / "drafts").glob("*.pdf"))


def test_eval_sections_formats_rates_and_matrices():
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_site

    secs = build_site.eval_sections({"rules": {"accuracy": 0.5, "n": 3, "misses": ["a", "b"],
                                               "confusion": {"bill": {"bill": 2, "other": 1}}}})
    rows = {r["key"]: r["value"] for r in secs[0]["rows"]}
    assert rows == {"accuracy": "50.0%", "n": "3", "misses": "2 items"}
    assert secs[1]["matrix"]["labels"] == ["bill", "other"]
