"""RingFinder FastAPI server — the product surface.

One command launches this; the judge gets a ranked ring queue, a graph view
with the cycle/shared-device evidence, a plain-English ask box, an
escalate/clear/watch control, and a Download case pack button. Nothing scripted.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load .env so the Claude key + Cognee toggles apply under `uvicorn` too.
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents import investigator, reporter
from .bus import MemoryBus
from . import llm_agents
from .pipeline import run_pipeline

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_CSV = os.environ.get("RINGFINDER_DATA", str(ROOT / "track02_fraud_watch.csv"))

app = FastAPI(title="RingFinder", version="0.1.0")
bus = MemoryBus()


def _ensure_pipeline() -> None:
    """Run the Grapher → Detective pipeline once if the bus is empty."""
    if not bus.has("detective", "rings"):
        run_pipeline(bus, DEFAULT_CSV)


@app.on_event("startup")
def _startup() -> None:
    _ensure_pipeline()


# ---- API ---------------------------------------------------------------------
@app.get("/api/summary")
def summary():
    _ensure_pipeline()
    return {
        "summary": bus.read("system", "summary"),
        "trace": bus.trace(),
        "data_source": Path(DEFAULT_CSV).name,
    }


@app.get("/api/trace")
def trace():
    return bus.trace()


@app.get("/api/overview")
def overview():
    """Product-brief stats, transactions sample, Cognee ledger, interaction history."""
    _ensure_pipeline()
    return bus.read("system", "overview")


@app.get("/api/cases")
def cases():
    """Worst-first case queue (lightweight)."""
    _ensure_pipeline()
    cs = bus.read("system", "cases") or []
    return [
        {k: c[k] for k in ("case_id", "rank", "kind", "title", "short", "score", "status",
                            "exposure", "tx_count", "n_accounts", "blurb", "recommendation")}
        for c in cs
    ]


@app.get("/api/cases/{case_id}")
def case_detail(case_id: str):
    _ensure_pipeline()
    cs = bus.read("system", "cases") or []
    case = next((c for c in cs if c["case_id"] == case_id), None)
    if case is None:
        raise HTTPException(404, f"No such case {case_id}")
    members = case["members"]
    nodes = [{"id": m, "hub": m == case.get("hub")} for m in members]
    edges = [
        {"src": e["src"], "dst": e["dst"], "amount": e["amount"], "timestamp": e["timestamp"]}
        for e in case.get("edges", [])
    ]
    shared_links = (bus.read("grapher", "graph") or {}).get("shared_device_links", [])
    shared = [
        {"device_id": l["device_id"], "accounts": sorted(set(l["accounts"]) & set(members))}
        for l in shared_links
        if len(set(l["accounts"]) & set(members)) >= 2
    ]
    return {"case": case, "graph": {"nodes": nodes, "edges": edges, "shared_devices": shared}}


@app.post("/api/agent/{step}")
def agent_step(step: str):
    _ensure_pipeline()
    try:
        return llm_agents.run_step(bus, step)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/rings")
def rings():
    _ensure_pipeline()
    rs = bus.read("detective", "rings") or []
    return [
        {
            "ring_id": r["ring_id"],
            "rank": r["rank"],
            "risk_score": r["risk_score"],
            "headline": r["headline"],
            "n_members": r["n_members"],
            "aggregate_amount": r["aggregate_amount"],
            "hub": r["hub"],
            "fired_patterns": r["fired_patterns"],
            "decision": (bus.read("investigator", f"decision_{r['ring_id']}") or {}).get("decision"),
        }
        for r in rs
    ]


@app.get("/api/rings/{ring_id}")
def ring_detail(ring_id: str):
    _ensure_pipeline()
    rs = bus.read("detective", "rings") or []
    ring = next((r for r in rs if r["ring_id"] == ring_id), None)
    if ring is None:
        raise HTTPException(404, f"No such ring {ring_id}")
    # build a graph payload for the visualization
    members = ring["members"]
    shared_links = (bus.read("grapher", "graph") or {}).get("shared_device_links", [])
    shared_in_ring = [
        {"device_id": l["device_id"], "accounts": sorted(set(l["accounts"]) & set(members))}
        for l in shared_links
        if len(set(l["accounts"]) & set(members)) >= 2
    ]
    nodes = [
        {"id": m, "hub": m == ring["hub"]} for m in members
    ]
    edges = [
        {"src": e["src"], "dst": e["dst"], "amount": e["amount"], "timestamp": e["timestamp"]}
        for e in ring["edges"]
    ]
    return {
        "ring": ring,
        "graph": {"nodes": nodes, "edges": edges, "shared_devices": shared_in_ring},
        "qa": bus.read("investigator", f"qa_{ring_id}") or [],
        "decision": bus.read("investigator", f"decision_{ring_id}"),
        "casepack_ready": bus.has("reporter", f"casepack_{ring_id}"),
    }


class AskBody(BaseModel):
    question: str


@app.post("/api/rings/{ring_id}/ask")
def ask(ring_id: str, body: AskBody):
    try:
        return investigator.ask(bus, ring_id, body.question)
    except ValueError as e:
        raise HTTPException(404, str(e))


class DecideBody(BaseModel):
    decision: str
    notes: str = ""


@app.post("/api/rings/{ring_id}/decide")
def decide(ring_id: str, body: DecideBody):
    try:
        return investigator.decide(bus, ring_id, body.decision, body.notes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/rings/{ring_id}/casepack")
def make_casepack(ring_id: str):
    try:
        rec = reporter.run(bus, ring_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ring_id": ring_id, "ready": True, "files": [rec["filename_md"], rec["filename_html"]]}


@app.get("/api/rings/{ring_id}/casepack.md")
def casepack_md(ring_id: str):
    rec = reporter.get_casepack(bus, ring_id) or reporter.run(bus, ring_id)
    return Response(
        rec["markdown"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{rec["filename_md"]}"'},
    )


@app.get("/api/rings/{ring_id}/casepack.html")
def casepack_html(ring_id: str):
    rec = reporter.get_casepack(bus, ring_id) or reporter.run(bus, ring_id)
    return HTMLResponse(rec["html"])


@app.post("/api/reset")
def reset():
    bus.reset()
    run_pipeline(bus, DEFAULT_CSV)
    return {"ok": True}


# ---- static UI ---------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
