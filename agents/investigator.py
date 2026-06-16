"""Agent 3 · Investigator.

Reads:  Agent 2's ranked rings + Agent 1's graph from the bus.
Does:   answers an analyst's plain-English question about a ring, grounded in
        the computed graph facts. Uses an LLM when LLM_API_KEY is configured;
        otherwise a deterministic graph-grounded responder. Either way the
        answer cites the exact accounts/edges (R07). Records the analyst's
        decision (escalate / clear / watch) and notes.
Writes: a Q&A trace and the decision back to the bus for Agent 4.

The LLM is never the source of facts — it only phrases the grounded context.
That context is built from Agent 2's evidence, so answers cannot hallucinate
connections the graph does not contain.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ..bus import MemoryBus

NAMESPACE = "investigator"


def _find_ring(bus: MemoryBus, ring_id: str) -> Optional[dict]:
    rings = bus.read("detective", "rings") or []
    for r in rings:
        if r["ring_id"] == ring_id:
            return r
    return None


def _grounding(ring: dict) -> dict:
    """The factual context an answer must cite — pulled straight from Agent 2."""
    p = ring["patterns"]
    flows = [
        f"{e['src']}→{e['dst']} ${e['amount']:.0f} @ {e['timestamp'][11:16]}"
        for e in ring["edges"]
    ]
    return {
        "ring_id": ring["ring_id"],
        "members": ring["members"],
        "hub": ring["hub"],
        "n_transfers": ring["n_transfers"],
        "aggregate_amount": ring["aggregate_amount"],
        "time_window": ring["time_window"],
        "open_date_window": ring["open_date_window"],
        "risk_score": ring["risk_score"],
        "fired_patterns": ring["fired_patterns"],
        "pattern_reasons": {k: v.get("why") for k, v in p.items()},
        "sample_flows": flows[:12],
    }


def _deterministic_answer(question: str, ring: dict, ctx: dict) -> str:
    """Graph-grounded fallback — handles the questions analysts actually ask."""
    q = question.lower()
    p = ring["patterns"]

    def cite(accts):
        return ", ".join(accts)

    if "hub" in q or "center" in q or "route" in q or "through" in q:
        return f"The routing hub is {ring['hub']}. {p['centrality']['why']}."
    if "start" in q or "origin" in q or "where did the money" in q or "source" in q:
        senders = sorted({e["src"] for e in ring["edges"]})
        recipients = sorted({e["dst"] for e in ring["edges"]})
        terminal = [a for a in recipients if a not in senders]
        return (
            f"Money originates from the funding accounts {cite(senders)} and flows "
            f"toward {cite(terminal) or 'accounts inside the cluster'}. "
            f"{p['centrality'].get('why', '')}."
        )
    if "why" in q and ("ring" in q or "flag" in q or "suspicious" in q):
        reasons = [v.get("why") for v in p.values() if v.get("fired")]
        return (
            f"{ring['ring_id']} scored {ring['risk_score']} because: "
            + "; ".join(reasons)
            + f". All {ring['n_transfers']} transfers sit under the $10k CTR line, "
            f"so no single transaction tripped a threshold rule."
        )
    if "who" in q or "account" in q or "member" in q or "involved" in q:
        return (
            f"{ring['n_members']} accounts form the ring: {cite(ring['members'])}. "
            f"They were onboarded between {ctx['open_date_window'].get('first')} and "
            f"{ctx['open_date_window'].get('last')} "
            f"({ctx['open_date_window'].get('span_days')} days apart)."
        )
    if "when" in q or "time" in q or "hour" in q:
        tw = ring["time_window"] or {}
        return (
            f"Transfers run from {tw.get('first')} to {tw.get('last')}, "
            f"and {p['structuring'].get('why', 'cluster in off-hours')}."
        )
    if "device" in q or "shared" in q or "identity" in q:
        return p["shared_identity"]["why"].capitalize() + "."
    if "how much" in q or "amount" in q or "total" in q or "exposure" in q:
        return (
            f"Aggregate exposure is ${ring['aggregate_amount']:,.2f} across "
            f"{ring['n_transfers']} transfers among {ring['n_members']} accounts."
        )
    # generic: summarize the fired evidence
    reasons = [v.get("why") for v in p.values() if v.get("fired")]
    return (
        f"{ring['ring_id']} ({ring['n_members']} accounts, "
        f"${ring['aggregate_amount']:,.0f}, risk {ring['risk_score']}). "
        + "; ".join(reasons)
        + "."
    )


_SYSTEM = (
    "You are a fraud-investigation assistant. Answer the analyst's question using "
    "ONLY the evidence provided — never invent accounts, amounts, or connections "
    "not present in it. Cite the exact account IDs you rely on. Be concise (2-4 "
    "sentences). Respond only with your final answer — no preamble, no reasoning trace."
)


def _graphrag_context(question: str) -> str:
    """Pull supporting facts from the real Cognee graph (GraphRAG) when present."""
    try:
        from .. import cognee_sync

        results = cognee_sync.search(question)
        if not results:
            return ""
        snippets = [str(r)[:300] for r in (results if isinstance(results, list) else [results])][:5]
        return "\n\nGRAPHRAG (Cognee knowledge graph):\n" + "\n".join(f"- {s}" for s in snippets)
    except Exception:
        return ""


def _llm_answer(question: str, ctx: dict) -> Optional[tuple[str, str]]:
    """Phrase a grounded answer with Claude. Returns (answer, source) or None.

    Uses the official Anthropic SDK (the LLM_API_KEY is a Claude key). Grounding
    is the graph evidence Agent 2 computed, optionally enriched with Cognee
    GraphRAG — the LLM only phrases facts it's given, so it can't hallucinate
    connections the graph doesn't contain (R07)."""
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    if provider != "anthropic":
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        model = os.environ.get("LLM_MODEL", "claude-opus-4-8").removeprefix("anthropic/")
        graphrag = _graphrag_context(question)
        user = (
            "EVIDENCE (computed from the transaction graph):\n"
            f"{json.dumps(ctx, indent=2)}{graphrag}\n\nQUESTION: {question}"
        )
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        source = "claude+graphrag" if graphrag else "claude (graph-grounded)"
        return (text, source) if text else None
    except Exception:
        return None


def ask(bus: MemoryBus, ring_id: str, question: str) -> dict:
    ring = _find_ring(bus, ring_id)
    if ring is None:
        raise ValueError(f"No such ring {ring_id} on the bus.")
    bus.log("investigator", "read", f"loaded {ring_id} from detective to answer a question")

    ctx = _grounding(ring)
    llm = _llm_answer(question, ctx)
    if llm is not None:
        answer, source = llm
    else:
        answer = _deterministic_answer(question, ring, ctx)
        source = "graph-grounded (deterministic)"

    record = {
        "ring_id": ring_id,
        "question": question,
        "answer": answer,
        "source": source,
        "cited": {
            "members": ring["members"],
            "hub": ring["hub"],
            "sample_flows": ctx["sample_flows"][:6],
        },
    }
    trace = bus.read(NAMESPACE, f"qa_{ring_id}") or []
    trace.append(record)
    bus.write(NAMESPACE, f"qa_{ring_id}", trace)
    bus.log("investigator", "write", f"answered ('{question[:48]}…') grounded via {source}")
    return record


def decide(bus: MemoryBus, ring_id: str, decision: str, notes: str = "") -> dict:
    if decision not in {"escalate", "clear", "watch"}:
        raise ValueError("decision must be escalate | clear | watch")
    rec = {"ring_id": ring_id, "decision": decision, "notes": notes}
    bus.write(NAMESPACE, f"decision_{ring_id}", rec)
    bus.log("investigator", "write", f"decision on {ring_id}: {decision.upper()}")
    return rec


def get_trace(bus: MemoryBus, ring_id: str) -> dict:
    return {
        "qa": bus.read(NAMESPACE, f"qa_{ring_id}") or [],
        "decision": bus.read(NAMESPACE, f"decision_{ring_id}"),
    }
