"""Pipeline orchestration — runs Agent 1 → Agent 2 in sequence through the bus.

Agents 3 (Investigator) and 4 (Case Reporter) are driven on demand by the
analyst in the product UI, but they read the same bus this pipeline populated.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .agents import detective, grapher
from .bus import MemoryBus

CTR_THRESHOLD = 10_000.0


def _baseline_comparison(graph: dict, rings: list[dict]) -> dict:
    """What a naive single-transaction threshold rule would have done — the
    'flag everything big' baseline RingFinder beats on precision."""
    ring_members = {m for r in rings for m in r["members"]}
    flagged_accounts = set()
    big_txns = 0
    for t in graph["transactions"]:
        if t["amount"] >= CTR_THRESHOLD:
            flagged_accounts.add(t["src"])
            big_txns += 1
    overlap = flagged_accounts & ring_members
    return {
        "ctr_threshold": CTR_THRESHOLD,
        "txns_over_threshold": big_txns,
        "accounts_threshold_would_flag": sorted(flagged_accounts),
        "ring_members_caught_by_threshold": sorted(overlap),
        "verdict": (
            f"A ${CTR_THRESHOLD:,.0f} threshold rule flags {len(flagged_accounts)} "
            f"accounts on {big_txns} large transactions — and catches "
            f"{len(overlap)} of the {len(ring_members)} ring members. "
            "RingFinder surfaced the ring from topology while ignoring those "
            "large-but-legitimate one-off purchases."
        ),
    }


def run_pipeline(bus: MemoryBus, csv_path: str | Path) -> dict:
    started = datetime.now()
    graph = grapher.run(bus, csv_path)
    rings = detective.run(bus)
    baseline = _baseline_comparison(graph, rings)
    bus.write("system", "baseline", baseline)
    summary = {
        "ran_at": started.isoformat(),
        "graph_stats": graph["stats"],
        "graph_reason": graph.get("reason"),
        "n_rings": len(rings),
        "top_ring": rings[0]["ring_id"] if rings else None,
        "baseline": baseline,
    }
    bus.write("system", "summary", summary)
    bus.log("system", "pipeline", f"completed: {len(rings)} ring(s), top={summary['top_ring']}")

    # Compose the Find→Rank→Act→Explain case queue the product page renders.
    from . import cases

    cases.build(bus)

    # Optional: mirror findings into a real Cognee graph (Anthropic LLM +
    # local fastembed). Best-effort and non-fatal — see cognee_sync.py.
    from . import cognee_sync

    if cognee_sync.enabled():
        cog = cognee_sync.build_graph(bus)
        summary["cognee"] = cog
        bus.write("system", "summary", summary)
    return summary
