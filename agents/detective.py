"""Agent 2 · Ring Detective.

Reads:  Agent 1's graph from the bus.
Does:   computes 6 structural fraud patterns over the money-flow graph and
        blends them into a Ring Risk Score. Nothing is hardcoded — rings are
        computed from whatever topology Agent 1 built. RingFinder is never told
        where the rings are.
Writes: a ranked list of rings, each with per-pattern evidence and a score
        breakdown, back to the bus.

The 6 patterns (MVP §6):
  1. Circular flow / layering    — directed cycles in a time window
  2. Mule fan-in / fan-out       — in/out-degree + aggregate flow
  3. Coordinated cluster         — community detection over flow edges
  4. Shared-identity bridge      — same device across accounts
  5. Synchronized structuring    — sub-threshold txns + correlated timing
  6. Centrality                  — betweenness / PageRank hub

Every pattern emits a human-readable "why" (R07).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import pstdev

import networkx as nx

from ..bus import MemoryBus

NAMESPACE = "detective"

# AML reference thresholds (grounded by the Domain Expert's Geodo research,
# ingested as a typology reference — MVP R04/R07).
CTR_THRESHOLD = 10_000.0          # Currency Transaction Report line
STRUCTURING_BAND = (400.0, 9_999.0)  # "just under" territory; ring uses tight low band
OFF_HOURS = range(1, 6)           # 1am–5am: atypical for legitimate retail flow


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _build_flow_graph(flow_edges: list[dict]) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for e in flow_edges:
        g.add_edge(e["src"], e["dst"], **e)
    return g


def _components(g: nx.MultiDiGraph) -> list[set]:
    """Weakly-connected components of the flow graph = candidate rings."""
    return [c for c in nx.weakly_connected_components(g) if len(c) >= 2]


def _pattern_circular(sub: nx.MultiDiGraph) -> dict:
    simple = nx.DiGraph(sub)
    cycles = [c for c in nx.simple_cycles(simple) if len(c) >= 2]
    if not cycles:
        # chains that return value indirectly still matter; report longest path
        return {"fired": False, "cycles": [], "why": "no directed cycle found"}
    cycles.sort(key=len, reverse=True)
    longest = cycles[0]
    path = "→".join(a.split("-")[-1] for a in longest) + "→" + longest[0].split("-")[-1]
    return {
        "fired": True,
        "cycles": cycles[:5],
        "why": f"money returns to origin: {path} ({len(cycles)} loop(s) detected)",
    }


def _pattern_mule(sub: nx.MultiDiGraph, accounts: dict) -> dict:
    flagged = []
    for n in sub.nodes:
        indeg = sub.in_degree(n)
        outdeg = sub.out_degree(n)
        in_amt = sum(d["amount"] for _, _, d in sub.in_edges(n, data=True))
        out_amt = sum(d["amount"] for _, _, d in sub.out_edges(n, data=True))
        # fan-in then fan-out, low retained balance proxy: high in & out degree
        if (indeg >= 3 and outdeg >= 1) or (outdeg >= 3 and indeg >= 1):
            flagged.append(
                {
                    "account": n,
                    "in_degree": indeg,
                    "out_degree": outdeg,
                    "in_amount": round(in_amt, 2),
                    "out_amount": round(out_amt, 2),
                }
            )
    if not flagged:
        return {"fired": False, "accounts": [], "why": "no fan-in/fan-out mule pattern"}
    top = max(flagged, key=lambda f: f["in_degree"] + f["out_degree"])
    return {
        "fired": True,
        "accounts": flagged,
        "why": (
            f"{top['account']} funnels {top['in_degree']} in / {top['out_degree']} out "
            f"(${top['in_amount']:,.0f} in, ${top['out_amount']:,.0f} out)"
        ),
    }


def _pattern_cluster(members: set, sub: nx.MultiDiGraph) -> dict:
    n = len(members)
    e = sub.number_of_edges()
    # density of the flow subgraph relative to a sparse-bank baseline
    max_edges = n * (n - 1)
    density = (e / max_edges) if max_edges else 0.0
    fired = n >= 3
    return {
        "fired": fired,
        "size": n,
        "edges": e,
        "density": round(density, 3),
        "why": (
            f"{n} accounts form a closed flow cluster ({e} internal transfers) "
            f"the bank does not otherwise connect"
            if fired
            else f"only {n} accounts; below cluster threshold"
        ),
    }


def _pattern_shared_identity(members: set, shared_links: list[dict]) -> dict:
    hits = []
    for link in shared_links:
        overlap = sorted(set(link["accounts"]) & members)
        if len(overlap) >= 2:
            hits.append({"device_id": link["device_id"], "accounts": overlap})
    if not hits:
        return {"fired": False, "links": [], "why": "no shared device/identity across members"}
    h = hits[0]
    return {
        "fired": True,
        "links": hits,
        "why": f"accounts {', '.join(h['accounts'])} share device {h['device_id']}",
    }


def _pattern_structuring(edges: list[dict]) -> dict:
    amts = [e["amount"] for e in edges]
    if not amts:
        return {"fired": False, "why": "no internal transfers"}
    sub_thresh = [a for a in amts if STRUCTURING_BAND[0] <= a <= STRUCTURING_BAND[1]]
    hours = [e["hour"] for e in edges]
    off = [h for h in hours if h in OFF_HOURS]
    off_frac = len(off) / len(hours)
    band_frac = len(sub_thresh) / len(amts)
    lo, hi = min(amts), max(amts)
    # all transfers in a tight low band AND clustered off-hours = structuring
    fired = band_frac >= 0.8 and off_frac >= 0.5 and hi <= CTR_THRESHOLD
    return {
        "fired": fired,
        "off_hours_fraction": round(off_frac, 2),
        "band_fraction": round(band_frac, 2),
        "amount_range": [round(lo, 2), round(hi, 2)],
        "why": (
            f"{len(amts)} transfers all ${lo:,.0f}–${hi:,.0f} (under the "
            f"${CTR_THRESHOLD:,.0f} CTR line), {int(off_frac*100)}% between 1–5 AM"
            if fired
            else f"transfers ${lo:,.0f}–${hi:,.0f}; not a structuring signature"
        ),
    }


def _pattern_centrality(sub: nx.MultiDiGraph) -> dict:
    if sub.number_of_nodes() < 3 or sub.number_of_edges() == 0:
        return {"fired": False, "hub": None, "why": "too few nodes/flows for a hub"}
    # weighted total throughput per node = money routed in + out through it
    throughput: dict[str, float] = defaultdict(float)
    degree: dict[str, int] = defaultdict(int)
    for u, v, d in sub.edges(data=True):
        throughput[u] += d["amount"]
        throughput[v] += d["amount"]
        degree[u] += 1
        degree[v] += 1
    hub = max(throughput, key=throughput.get)
    ranked = {
        k: round(v, 2) for k, v in sorted(throughput.items(), key=lambda x: -x[1])[:5]
    }
    return {
        "fired": True,
        "hub": hub,
        "throughput": ranked,
        "why": (
            f"{hub} is the routing hub — highest money throughput "
            f"(${throughput[hub]:,.0f} across {degree[hub]} transfers) in the cluster"
        ),
    }


def _open_date_window(members: set, accounts: dict) -> dict:
    dates = sorted(
        accounts[m]["open_date"]
        for m in members
        if m in accounts and accounts[m].get("open_date")
    )
    if len(dates) < 2:
        return {"span_days": None, "first": dates[0] if dates else None, "last": None}
    d0 = datetime.fromisoformat(dates[0])
    d1 = datetime.fromisoformat(dates[-1])
    return {"span_days": (d1 - d0).days, "first": dates[0], "last": dates[-1]}


def _score(patterns: dict, sub_clusters: int, open_win: dict) -> dict:
    """Ring Risk Score — weighted blend of the structural signals. The
    breakdown is returned so the *ranking itself* is explainable (MVP §6).
    Each component is a normalized [0,1] signal times a weight; the weights
    sum to 1.0, so a ring lighting up every pattern approaches 1.00."""
    circular = patterns["circular"]
    cluster = patterns["cluster"]
    structuring = patterns["structuring"]
    mule = patterns["mule"]
    centrality = patterns["centrality"]
    shared = patterns["shared_identity"]

    comp = {
        # circular layering is the strongest single signal when present
        "circular_flow": 0.20 if circular["fired"] else 0.0,
        # cluster size, saturating at ~8 accounts
        "cluster_size": round(0.15 * min(cluster.get("size", 0) / 8.0, 1.0), 3),
        "sub_threshold_structuring": 0.18 if structuring["fired"] else 0.0,
        "synchronized_timing": round(0.15 * structuring.get("off_hours_fraction", 0.0), 3),
        # lockstep: multiple sub-clusters moving with one signature
        "lockstep_subclusters": round(0.12 * min((sub_clusters - 1) / 2.0, 1.0), 3)
        if sub_clusters > 1
        else 0.0,
        "mule_routing": 0.10 if mule["fired"] else 0.0,
        "shared_identity": 0.05 if shared["fired"] else 0.0,
        "coordinated_onboarding": 0.0,
    }
    span = open_win.get("span_days")
    if span is not None and span <= 14:
        comp["coordinated_onboarding"] = 0.05
    total = round(min(sum(comp.values()), 1.0), 3)
    return {"score": total, "breakdown": comp}


def _merge_components(components: list[set], accounts: dict, g: nx.MultiDiGraph,
                      shared_links: list[dict]) -> list[dict]:
    """Community detection (MVP pattern 3): link flow sub-clusters that share a
    behavioral fingerprint into one coordinated ring, even when no direct
    money-flow edge bridges them. Two sub-clusters are merged when BOTH show
    off-hours sub-threshold structuring AND their accounts were onboarded in the
    same tight window — or when they share a device fingerprint. This is what
    turns 6 separate loops into 'one ring of ~12 accounts'."""
    n = len(components)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    # precompute per-component signature
    sigs = []
    for members in components:
        sub = g.subgraph(members)
        edges = [d for _, _, d in sub.edges(data=True)]
        structuring = _pattern_structuring(edges)
        dates = [
            accounts[m]["open_date"]
            for m in members
            if m in accounts and accounts[m].get("open_date")
        ]
        devices = set()
        for m in members:
            devices.update(accounts.get(m, {}).get("devices", []))
        sigs.append(
            {
                "structuring": structuring["fired"],
                "dates": sorted(dates),
                "devices": devices,
            }
        )

    for i in range(n):
        for j in range(i + 1, n):
            si, sj = sigs[i], sigs[j]
            # shared device bridge
            if si["devices"] & sj["devices"]:
                union(i, j)
                continue
            # coordinated structuring + onboarding window
            if si["structuring"] and sj["structuring"] and si["dates"] and sj["dates"]:
                alld = sorted(si["dates"] + sj["dates"])
                span = (datetime.fromisoformat(alld[-1]) - datetime.fromisoformat(alld[0])).days
                if span <= 14:
                    union(i, j)

    groups: dict[int, set] = defaultdict(set)
    group_counts: dict[int, int] = defaultdict(int)
    for idx, members in enumerate(components):
        root = find(idx)
        groups[root] |= members
        group_counts[root] += 1
    return [
        {"members": m, "n_subclusters": group_counts[root]}
        for root, m in groups.items()
    ]


def run(bus: MemoryBus) -> list[dict]:
    graph = bus.read("grapher", "graph")
    if graph is None:
        raise RuntimeError("Agent 2 found no graph on the bus — run Agent 1 (Grapher) first.")
    bus.log("detective", "read", f"loaded graph from grapher ({graph['stats']})")

    accounts = {a["account_id"]: a for a in graph["accounts"]}
    g = _build_flow_graph(graph["flow_edges"])
    shared_links = graph["shared_device_links"]

    components = _components(g)
    merged = _merge_components(components, accounts, g, shared_links)

    rings = []
    for i, grp in enumerate(merged, start=1):
        members = grp["members"]
        sub = g.subgraph(members)
        edges = [{**d} for _, _, d in sub.edges(data=True)]
        circular = _pattern_circular(sub)
        mule = _pattern_mule(sub, accounts)
        cluster = _pattern_cluster(members, sub)
        shared = _pattern_shared_identity(members, shared_links)
        structuring = _pattern_structuring(edges)
        centrality = _pattern_centrality(sub)
        open_win = _open_date_window(members, accounts)

        patterns = {
            "circular": circular,
            "mule": mule,
            "cluster": cluster,
            "shared_identity": shared,
            "structuring": structuring,
            "centrality": centrality,
        }
        scored = _score(patterns, grp["n_subclusters"], open_win)

        ts = [e["timestamp"] for e in edges]
        amts = [e["amount"] for e in edges]
        ring = {
            "ring_id": f"RING-{i:03d}",
            "members": sorted(members),
            "n_members": len(members),
            "n_subclusters": grp["n_subclusters"],
            "edges": edges,
            "n_transfers": len(edges),
            "aggregate_amount": round(sum(amts), 2),
            "time_window": {"first": min(ts), "last": max(ts)} if ts else None,
            "open_date_window": open_win,
            "patterns": patterns,
            "risk_score": scored["score"],
            "score_breakdown": scored["breakdown"],
            "hub": centrality.get("hub"),
            "headline": _headline(members, circular, structuring, amts, grp["n_subclusters"]),
            "fired_patterns": [name for name, p in patterns.items() if p.get("fired")],
        }
        rings.append(ring)

    rings.sort(key=lambda r: (r["risk_score"], r["n_members"]), reverse=True)
    for rank, r in enumerate(rings, 1):
        r["rank"] = rank

    bus.write(NAMESPACE, "rings", rings)
    fired = rings[0]["fired_patterns"] if rings else []
    bus.log(
        "detective",
        "write",
        f"ranked {len(rings)} candidate rings; top = "
        f"{rings[0]['ring_id'] if rings else 'none'} "
        f"(risk {rings[0]['risk_score'] if rings else 0}, patterns: {', '.join(fired)})",
    )
    return rings


def _headline(members, circular, structuring, amts, sub_clusters=1) -> str:
    n = len(members)
    agg = sum(amts)
    bits = [f"{n} accounts"]
    if sub_clusters > 1:
        bits.append(f"{sub_clusters} coordinated loops")
    elif circular["fired"]:
        bits.append("circular flow")
    if structuring["fired"]:
        bits.append("sub-threshold structuring")
    bits.append(f"${agg:,.0f} aggregate")
    return " · ".join(bits)
