"""Real-Cognee integration for RingFinder.

When RINGFINDER_USE_COGNEE=1 and a Claude (Anthropic) key is configured, this
module mirrors the pipeline's handoff artifacts into a Cognee dataset and runs
`cognify()` so the agents' findings become an inspectable knowledge graph +
searchable memory — the literal "open the Cognee graph UI" demo (MVP §4).

Why this works on a Claude key alone:
  - LLM  → Anthropic (Cognee has a native anthropic adapter; instructor
           `anthropic_tools` mode). Configured via LLM_PROVIDER/LLM_MODEL/LLM_API_KEY.
  - Embeddings → fastembed, a LOCAL model (Anthropic has no embeddings API).
           Configured via EMBEDDING_PROVIDER=fastembed.

The local file bus remains the source of truth for detection determinism; this
is the optional, inspectable mirror. Everything here is best-effort: failures
are logged and swallowed so a missing/invalid key never breaks the product.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from .bus import MemoryBus

DATASET = "ringfinder"


def enabled() -> bool:
    return os.environ.get("RINGFINDER_USE_COGNEE") == "1" and bool(os.environ.get("LLM_API_KEY"))


def configure() -> bool:
    """Point Cognee at Anthropic (LLM) + fastembed (embeddings). Returns True if
    cognee imported and was configured."""
    try:
        import cognee
    except Exception:
        return False

    # LLM — default to Claude if the env didn't already pin a provider.
    os.environ.setdefault("LLM_PROVIDER", "anthropic")
    os.environ.setdefault("LLM_MODEL", "claude-opus-4-8")
    # Embeddings — local fastembed; Anthropic has no embeddings endpoint.
    os.environ.setdefault("EMBEDDING_PROVIDER", "fastembed")
    os.environ.setdefault("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")
    # Local single-user mode.
    os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")

    try:
        cognee.config.set_llm_config(
            {
                "llm_provider": os.environ.get("LLM_PROVIDER", "anthropic"),
                "llm_model": os.environ.get("LLM_MODEL", "claude-opus-4-8"),
                "llm_api_key": os.environ["LLM_API_KEY"],
            }
        )
    except Exception:
        # Env vars above are the authoritative path, so a config-helper
        # signature mismatch across cognee versions is non-fatal.
        pass
    try:
        cognee.config.set_embedding_config(
            {
                "embedding_provider": os.environ.get("EMBEDDING_PROVIDER", "fastembed"),
                "embedding_model": os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
                "embedding_dimensions": int(os.environ.get("EMBEDDING_DIMENSIONS", "384")),
            }
        )
    except Exception:
        pass
    return True


def _ring_document(bus: MemoryBus) -> str:
    """A compact, natural-language description of the pipeline's findings for
    Cognee to extract a graph from — accounts, flows, hubs, patterns."""
    graph = bus.read("grapher", "graph") or {}
    rings = bus.read("detective", "rings") or []
    lines = [
        "RingFinder fraud-investigation findings.",
        f"Source dataset: {graph.get('source', 'unknown')}. {graph.get('reason', '')}.",
        "",
    ]
    for r in rings:
        lines.append(
            f"{r['ring_id']} is a fraud ring with risk score {r['risk_score']}. "
            f"It contains {r['n_members']} accounts: {', '.join(r['members'])}. "
            f"The routing hub is {r['hub']}. Aggregate exposure ${r['aggregate_amount']:,.0f} "
            f"across {r['n_transfers']} transfers."
        )
        for name, pat in r["patterns"].items():
            if pat.get("fired"):
                lines.append(f"  - {name}: {pat.get('why')}")
        for e in r["edges"][:40]:
            lines.append(
                f"  flow: account {e['src']} sent ${e['amount']:.0f} to account {e['dst']} at {e['timestamp']}."
            )
        lines.append("")
    return "\n".join(lines)


async def _build_async(bus: MemoryBus) -> dict:
    import cognee

    doc = _ring_document(bus)
    await cognee.add(doc, dataset_name=DATASET)
    await cognee.cognify(datasets=[DATASET])
    return {"ok": True, "chars": len(doc)}


def build_graph(bus: MemoryBus) -> dict:
    """Mirror findings into Cognee and cognify. Safe to call when disabled —
    returns a status dict either way."""
    if not enabled():
        return {"ok": False, "reason": "RINGFINDER_USE_COGNEE!=1 or no LLM_API_KEY"}
    if not configure():
        return {"ok": False, "reason": "cognee not importable"}
    try:
        result = asyncio.run(_build_async(bus))
        bus.log("system", "cognee", f"mirrored findings into Cognee dataset '{DATASET}' and cognified")
        return result
    except Exception as e:  # never break the product on the optional mirror
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def search(query: str) -> Optional[list]:
    """GraphRAG search over the Cognee graph (used by the Investigator when the
    real graph is present). Returns None if Cognee is unavailable."""
    if not enabled() or not configure():
        return None
    try:
        import cognee

        async def _run():
            return await cognee.search(query_text=query, datasets=[DATASET])

        return asyncio.run(_run())
    except Exception:
        return None
