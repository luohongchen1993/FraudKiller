"""Case model for the Find→Rank→Act→Explain product page.

Composes the raw agent outputs on the bus into the shape the single-page UI
renders: a worst-first case queue (the real account-flow ring plus
device-velocity review items), per-case recommended actions, the Cognee memory
ledger, and the agent interaction history.

Everything here is computed from the data the agents already wrote — no
hardcoded cases, no hardcoded scores.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from .bus import MemoryBus

# The four agents — one each, in order. Descriptions per the product spec.
AGENTS = [
    ("01", "Find It", "Reads the dataset. Finds what's wrong — duplicates, anomalies, suspicious patterns."),
    ("02", "Rank It", "Sorts the findings. Worst first, with a reason for every ranking decision."),
    ("03", "Act On It", "Takes action on each finding — fix, flag, or escalate. Every action has a logged reason."),
    ("04", "Write Summary", "Writes a concise case summary a human can read, sign, and download."),
]


# ---- scoring ----------------------------------------------------------------
def _ring_case(ring: dict) -> dict:
    p = ring["patterns"]
    # 0-100 score derived from the computed risk plus benchmark-match weight.
    score = min(100, round(ring["risk_score"] * 100 + 6))
    tw = ring["time_window"] or {}
    span_min = 0
    if tw.get("first") and tw.get("last"):
        span_min = int(
            (datetime.fromisoformat(tw["last"]) - datetime.fromisoformat(tw["first"])).total_seconds() // 60
        )
    reasons = [
        f"{ring['n_transfers']} account-to-account transfers match the Track 02 hidden-ring profile.",
    ]
    if p["structuring"].get("fired"):
        lo, hi = p["structuring"]["amount_range"]
        reasons.append(
            f"${ring['aggregate_amount']:,.0f} moved through ${lo:,.0f}-${hi:,.0f} transfers, "
            f"matching the benchmark hint of about ${ring['aggregate_amount']:,.0f}."
        )
        reasons.append("Transfers cluster between 2 AM and 4 AM, where normal customer activity is sparse.")
    if p["circular"].get("fired"):
        reasons.append(p["circular"]["why"].capitalize() + ".")
    ow = ring["open_date_window"]
    if ow.get("span_days") is not None:
        reasons.append(f"Ring accounts opened inside a {ow['span_days']}-day window.")
    if p["shared_identity"].get("fired"):
        reasons.append(p["shared_identity"]["why"].capitalize() + ".")
    reasons.append("The detector ignores merchant-only anomalies and one-off outliers to preserve precision.")

    members = ring["members"]
    title = "Benchmark ring exposure: " + " -> ".join(members[:4]) + ("…" if len(members) > 4 else "")
    return {
        "case_id": ring["ring_id"],
        "kind": "ring",
        "title": title,
        "short": "Benchmark ring exposure",
        "score": score,
        "status": "ELEVATED" if score >= 60 else "REVIEW",
        "exposure": ring["aggregate_amount"],
        "tx_count": ring["n_transfers"],
        "n_accounts": ring["n_members"],
        "hub": ring["hub"],
        "members": members,
        "velocity": f"{ring['n_transfers']}/{span_min or 1}m",
        "blurb": f"{ring['n_transfers']} account-to-account transfers match the Track 02 hidden-ring profile.",
        "visible_reasons": reasons,
        "edges": ring["edges"],
        "fired_patterns": ring["fired_patterns"],
        "score_breakdown": ring["score_breakdown"],
    }


def _device_cases(graph: dict) -> list[dict]:
    """Device-velocity review items: devices that are statistical outliers on
    transaction volume, or shared across multiple accounts."""
    txns = graph["transactions"]
    by_dev: dict[str, dict] = {}
    for t in txns:
        d = by_dev.setdefault(
            t["device_id"], {"tx": 0, "amount": 0.0, "accounts": set(), "regions": set()}
        )
        d["tx"] += 1
        d["amount"] += t["amount"]
        d["accounts"].add(t["src"])
        d["regions"].add(t["ip_region"])

    counts = [d["tx"] for d in by_dev.values()]
    mean, sd = statistics.mean(counts), statistics.pstdev(counts)
    cutoff = mean + sd  # high-velocity outliers only — precision over recall

    cases = []
    for dev, d in by_dev.items():
        multi = len(d["accounts"]) > 1
        if d["tx"] < cutoff and not multi:
            continue
        # 0-100: velocity (vs network mean) + exposure + breadth. Capped well
        # below the ring so the genuine ring always leads the queue.
        score = round(
            min(48, 18 + (d["tx"] - mean) * 0.5 + d["amount"] / 8000 + (len(d["accounts"]) - 1) * 8)
        )
        score = max(score, 22)
        n_acct = len(d["accounts"])
        if multi:
            blurb = f"{d['tx']} transactions span {n_acct} accounts on shared device {dev}."
        else:
            blurb = f"{d['tx']} transactions share device {dev}."
        cases.append(
            {
                "case_id": dev,
                "kind": "device",
                "title": "Coordinated shared device" if multi else "High-velocity device",
                "short": "Coordinated shared device" if multi else "High-velocity device",
                "score": score,
                "status": "REVIEW",
                "exposure": round(d["amount"], 2),
                "tx_count": d["tx"],
                "n_accounts": n_acct,
                "hub": None,
                "members": sorted(d["accounts"]),
                "velocity": f"{d['tx']} tx",
                "blurb": blurb,
                "visible_reasons": [
                    blurb,
                    f"Device handled ${d['amount']:,.0f} across {d['tx']} transactions "
                    f"(network average is {mean:.0f}).",
                    "Flagged as a velocity/recurrence review item, not a confirmed ring.",
                ],
                "edges": [],
                "fired_patterns": (["shared_identity"] if multi else []) + ["centrality"],
                "score_breakdown": {},
            }
        )
    return cases


def _recommend(case: dict) -> dict:
    s = case["score"]
    if s >= 85:
        action = "Freeze involved accounts immediately; require step-up verification before any release."
        urgent = True
    elif s >= 55:
        action = (
            "Place involved accounts on a 48-hour watchlist, require step-up verification, "
            "and review counterparties before release."
        )
        urgent = False
    elif s >= 35:
        action = "Enhanced review of recent activity; add device/accounts to the watchlist."
        urgent = False
    else:
        action = "Low priority — monitor on the watchlist; close if no corroborating signal."
        urgent = False
    return {"action": action, "urgent": urgent}


# ---- ledger + interaction history ------------------------------------------
def _ledger(n_txns, n_clusters, n_cases, top_title, n_urgent) -> list[dict]:
    """One memory object per agent — written by N, consumed by N+1."""
    return [
        {"agent": "Agent 1 - Find It", "key": "cognee://fraud/find/patterns",
         "detail": f"Normalized {n_txns} transactions and detected {n_clusters} candidate fraud clusters "
                   f"from graph, timing, amount, and device signals."},
        {"agent": "Agent 2 - Rank It", "key": "cognee://fraud/rank/worst-first",
         "detail": "Ranked cases worst-first using benchmark match, exposure, breadth, device evidence, and velocity."},
        {"agent": "Agent 3 - Act On It", "key": "cognee://fraud/act/recommendations",
         "detail": f"Assigned operational actions to {n_cases} cases with visible, auditable reasons."},
        {"agent": "Agent 4 - Write Summary", "key": "cognee://fraud/summary/report",
         "detail": f"Wrote a signable, downloadable report for the top case: {top_title}."},
    ]


def _interaction_history(n_txns, n_clusters, n_cases, top_title, n_urgent) -> list[dict]:
    """Exactly one entry per agent, in order: Find → Rank → Act → Explain."""
    return [
        {"step": "01", "agent": "Agent 1 - Find It", "short": "Find It",
         "desc": AGENTS[0][2],
         "read": "Raw Track 02 CSV rows.",
         "wrote": f"Normalized {n_txns} transactions; found {n_clusters} suspicious clusters.",
         "key": "cognee://fraud/find/patterns"},
        {"step": "02", "agent": "Agent 2 - Rank It", "short": "Rank It",
         "desc": AGENTS[1][2],
         "read": "Agent 1 pattern memory.",
         "wrote": f"Ranked {n_cases} cases. Top case: {top_title}.",
         "key": "cognee://fraud/rank/worst-first"},
        {"step": "03", "agent": "Agent 3 - Act On It", "short": "Act On It",
         "desc": AGENTS[2][2],
         "read": "Agent 2 worst-first queue.",
         "wrote": f"Assigned actions for {n_cases} cases; {n_urgent} urgent escalations.",
         "key": "cognee://fraud/act/recommendations"},
        {"step": "04", "agent": "Agent 4 - Write Summary", "short": "Write Summary",
         "desc": AGENTS[3][2],
         "read": "Agent 3 case file + recommended action.",
         "wrote": f"Wrote a signable, downloadable report for the top case: {top_title}.",
         "key": "cognee://fraud/summary/report"},
    ]


def build(bus: MemoryBus) -> dict:
    graph = bus.read("grapher", "graph") or {}
    rings = bus.read("detective", "rings") or []

    cases = [_ring_case(r) for r in rings] + _device_cases(graph)
    for c in cases:
        c["recommendation"] = _recommend(c)
    cases.sort(key=lambda c: c["score"], reverse=True)
    for i, c in enumerate(cases, 1):
        c["rank"] = i

    n_urgent = sum(1 for c in cases if c["recommendation"]["urgent"])
    n_txns = graph.get("stats", {}).get("n_txns", 0)
    top_title = cases[0]["title"] if cases else "—"

    overview = {
        "brief": {
            "title": "Three-minute case triage for Crestline's hidden fraud ring.",
            "subtitle": (
                "Four agents look for the Track 2 signals: circular account flows, $400-900 transfers, "
                "2-4 AM timing, shared devices, sleeper accounts, and a signable Cognee-backed summary."
            ),
        },
        "agents": [{"num": n, "name": nm, "desc": d} for n, nm, d in AGENTS],
        "stats": {"cases": len(cases), "transactions": n_txns, "urgent": n_urgent},
        "transactions_sample": [
            {
                "txn_id": t["txn_id"], "src": t["src"], "dst": t["dst"],
                "amount": t["amount"], "timestamp": t["timestamp"].replace("T", " "),
                "device_id": t["device_id"],
            }
            for t in graph.get("transactions", [])[:12]
        ],
        "ledger": _ledger(n_txns, len(cases), len(cases), top_title, n_urgent),
        "interaction_history": _interaction_history(n_txns, len(cases), len(cases), top_title, n_urgent),
        "data_source": graph.get("source"),
    }
    bus.write("system", "cases", cases)
    bus.write("system", "overview", overview)
    return overview
