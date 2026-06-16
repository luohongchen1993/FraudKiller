"""Claude-backed step agents for the demo UI.

The graph detector remains the source of truth. Each UI step sends compact,
computed evidence to Claude and requires strict JSON back so the page can render
without depending on free-form prose.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .bus import MemoryBus


STEP_TO_INDEX = {"find": 0, "rank": 1, "act": 2, "summary": 3}


SYSTEM = """You are a fraud operations agent in a four-step investigation pipeline.
Use only the supplied evidence. Do not invent accounts, amounts, rankings, or actions.
Return valid JSON only. No markdown fences. No commentary outside JSON.
Keep every string concise. Do not include literal newline characters inside strings."""


def _case_public(c: dict) -> dict:
    return {
        "case_id": c["case_id"],
        "rank": c["rank"],
        "kind": c["kind"],
        "short": c["short"],
        "title": c["title"],
        "score": c["score"],
        "status": c["status"],
        "exposure": c["exposure"],
        "tx_count": c["tx_count"],
        "n_accounts": c["n_accounts"],
        "blurb": c["blurb"],
        "recommendation": c["recommendation"],
        "visible_reasons": c.get("visible_reasons", [])[:5],
        "members": c.get("members", [])[:12],
        "hub": c.get("hub"),
    }


def _ring_public(r: dict | None) -> dict | None:
    if not r:
        return None
    patterns = r.get("patterns", {})
    return {
        "ring_id": r.get("ring_id"),
        "rank": r.get("rank"),
        "members": r.get("members", [])[:12],
        "n_members": r.get("n_members"),
        "n_transfers": r.get("n_transfers"),
        "aggregate_amount": r.get("aggregate_amount"),
        "hub": r.get("hub"),
        "risk_score": r.get("risk_score"),
        "headline": r.get("headline"),
        "fired_patterns": r.get("fired_patterns", []),
        "time_window": r.get("time_window"),
        "open_date_window": r.get("open_date_window"),
        "pattern_reasons": {name: pat.get("why") for name, pat in patterns.items()},
        "sample_edges": r.get("edges", [])[:12],
    }


def _evidence(bus: MemoryBus) -> dict:
    overview = bus.read("system", "overview") or {}
    cases = bus.read("system", "cases") or []
    graph = bus.read("grapher", "graph") or {}
    rings = bus.read("detective", "rings") or []
    return {
        "stats": overview.get("stats", {}),
        "data_source": overview.get("data_source"),
        "graph_stats": graph.get("stats", {}),
        "top_cases": [_case_public(c) for c in cases[:6]],
        "top_ring": _ring_public(rings[0] if rings else None),
        "transactions_sample": overview.get("transactions_sample", [])[:8],
    }


def _schema(step: str) -> dict:
    common = {
        "stage_title": "short title",
        "tab_title": "short tab title",
        "headline": "one-sentence result headline",
        "cards": [
            {
                "title": "short card title",
                "meta": "score/exposure/accounts summary",
                "body": "one or two sentences",
                "badge": "FOUND|RANKED|ACTION|SUMMARY",
            }
        ],
        "ledger_detail": "one concise memory write description",
        "history_wrote": "one concise handoff write description",
    }
    if step == "act":
        common["cards"][0]["action"] = "specific operational action"
    if step == "summary":
        common["summary"] = {
            "top_case": "case title",
            "finding": "what was found",
            "action": "recommended action",
            "signable_summary": "3-5 sentence analyst-ready summary",
        }
    return common


def _prompt(step: str, evidence: dict) -> str:
    instructions = {
        "find": "Agent 1 FIND: identify suspicious clusters from the evidence. Emphasize what was found, not ranking or actions.",
        "rank": "Agent 2 RANK: explain the worst-first order. Emphasize why the top case is ranked first.",
        "act": "Agent 3 ACT: assign operational actions for the ranked cases. Emphasize watch/freeze/review decisions.",
        "summary": "Agent 4 SUMMARY: write a concise signable analyst summary for the top case using prior step outputs.",
    }[step]
    return (
        f"{instructions}\n\n"
        "Return one JSON object matching this shape exactly.\n"
        "Hard requirements:\n"
        "- cards must contain 4 to 6 items for find/rank/act and 3 to 4 items for summary.\n"
        "- Each card body must be under 28 words.\n"
        "- headline, ledger_detail, and history_wrote must each be one short sentence.\n"
        "- For summary.signable_summary, write 65 to 90 words, no bullets.\n"
        "- Do not add keys outside the shape.\n"
        f"SHAPE:\n{json.dumps(_schema(step), indent=2)}\n\n"
        f"EVIDENCE:\n{json.dumps(evidence, indent=2, default=str)}"
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        depth = 0
        in_string = False
        escaped = False
        for i, ch in enumerate(text[start:], start):
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_string:
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _coerce(step: str, raw: dict, fallback: dict) -> dict:
    out = {
        "step": step,
        "source": raw.get("source", "claude (structured-json)"),
        "stage_title": str(raw.get("stage_title") or fallback["stage_title"]),
        "tab_title": str(raw.get("tab_title") or fallback["tab_title"]),
        "headline": str(raw.get("headline") or fallback["headline"]),
        "cards": raw.get("cards") if isinstance(raw.get("cards"), list) else fallback["cards"],
        "ledger_detail": str(raw.get("ledger_detail") or fallback["ledger_detail"]),
        "history_wrote": str(raw.get("history_wrote") or fallback["history_wrote"]),
    }
    clean_cards = []
    for card in out["cards"][:6]:
        if not isinstance(card, dict):
            continue
        clean_cards.append(
            {
                "title": str(card.get("title", "Case")),
                "meta": str(card.get("meta", "")),
                "body": str(card.get("body", "")),
                "badge": str(card.get("badge", fallback["cards"][0]["badge"])),
                "action": str(card.get("action", "")),
            }
        )
    out["cards"] = clean_cards or fallback["cards"]
    if step == "summary":
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else fallback.get("summary", {})
        out["summary"] = {
            "top_case": str(summary.get("top_case", "")),
            "finding": str(summary.get("finding", "")),
            "action": str(summary.get("action", "")),
            "signable_summary": str(summary.get("signable_summary", "")),
        }
    return out


def _fallback(step: str, evidence: dict) -> dict:
    cases = evidence.get("top_cases") or []
    top = cases[0] if cases else {}
    cards = []
    for c in cases[:6]:
        cards.append(
            {
                "title": c.get("short", "Case"),
                "meta": f"Score {c.get('score')} · ${c.get('exposure'):,.0f} · {c.get('tx_count')} tx",
                "body": c.get("blurb", ""),
                "badge": {"find": "FOUND", "rank": "RANKED", "act": "ACTION", "summary": "SUMMARY"}[step],
                "action": c.get("recommendation", {}).get("action", ""),
            }
        )
    base = {
        "step": step,
        "source": "deterministic fallback",
        "stage_title": {
            "find": "Agent 1 found suspicious patterns",
            "rank": "Agent 2 ranked the cases",
            "act": "Agent 3 recommended actions",
            "summary": "Agent 4 wrote the summary",
        }[step],
        "tab_title": {
            "find": "Agent 1 Output: Findings",
            "rank": "Agent 2 Output: Worst-First Ranking",
            "act": "Agent 3 Output: Actions",
            "summary": "Agent 4 Output: Summary",
        }[step],
        "headline": {
            "find": "Suspicious clusters were found from graph, timing, amount, and device evidence.",
            "rank": "Cases were ranked worst-first using exposure, breadth, velocity, and graph evidence.",
            "act": "Operational actions were assigned for each ranked case.",
            "summary": "A signable analyst summary was prepared for the top case.",
        }[step],
        "cards": cards,
        "ledger_detail": f"Agent {STEP_TO_INDEX[step] + 1} wrote structured {step} output.",
        "history_wrote": f"Completed {step} step with structured output.",
    }
    if step == "summary":
        base["summary"] = {
            "top_case": top.get("title", ""),
            "finding": top.get("blurb", ""),
            "action": top.get("recommendation", {}).get("action", ""),
            "signable_summary": (
                f"{top.get('title', 'The top case')} was surfaced from graph evidence and ranked first. "
                f"{top.get('blurb', '')} Recommended action: "
                f"{top.get('recommendation', {}).get('action', '')}"
            ),
        }
    return base


def run_step(bus: MemoryBus, step: str) -> dict:
    if step not in STEP_TO_INDEX:
        raise ValueError(f"Unknown step: {step}")
    evidence = _evidence(bus)
    fallback = _fallback(step, evidence)
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return fallback
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        model = os.environ.get("LLM_MODEL", "claude-opus-4-8").removeprefix("anthropic/")
        resp = client.messages.create(
            model=model,
            max_tokens=2200,
            system=SYSTEM,
            messages=[{"role": "user", "content": _prompt(step, evidence)}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        raw = _extract_json(text)
        raw["source"] = "claude (structured-json)"
        return _coerce(step, raw, fallback)
    except Exception as exc:
        fallback["source"] = f"deterministic fallback ({type(exc).__name__})"
        return fallback
