"""Agent 1 · Grapher.

Reads:  the ingest manifest from the bus (to skip re-ingesting unchanged data).
Does:   loads the transaction CSV and builds the knowledge graph — accounts,
        transactions, devices, IP regions as nodes; money-flow and
        shared-attribute edges.
Writes: the serialized graph + an ingest manifest back to the bus.

Visible reason (R07): "ingested N txns, M accounts; built X flow edges,
Y shared-device links."
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime
from pathlib import Path

from ..bus import MemoryBus

NAMESPACE = "grapher"


def _parse_ts(s: str) -> str:
    # Stored as ISO; kept as string for JSON-safety, parsed on demand downstream.
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").isoformat()


def run(bus: MemoryBus, csv_path: str | Path) -> dict:
    csv_path = Path(csv_path)
    raw = csv_path.read_bytes()
    fingerprint = hashlib.sha256(raw).hexdigest()[:16]

    manifest = bus.read(NAMESPACE, "manifest")
    bus.log(
        "grapher",
        "read",
        f"checked ingest manifest — "
        + ("found prior ingest, comparing fingerprints" if manifest else "no prior ingest, fresh build"),
    )
    if manifest and manifest.get("fingerprint") == fingerprint and bus.has(NAMESPACE, "graph"):
        bus.log("grapher", "skip", f"dataset unchanged ({fingerprint}); reusing graph")
        return bus.read(NAMESPACE, "graph")

    transactions: list[dict] = []
    accounts: dict[str, dict] = {}
    devices: dict[str, set] = {}

    with csv_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            amt = float(row["amount"])
            ts = _parse_ts(row["timestamp"])
            txn = {
                "txn_id": row["txn_id"],
                "src": row["account_id"],
                "dst": row["counterparty_id"],
                "amount": amt,
                "timestamp": ts,
                "hour": datetime.fromisoformat(ts).hour,
                "merchant_category": row["merchant_category"],
                "device_id": row["device_id"],
                "ip_region": row["ip_region"],
            }
            transactions.append(txn)

            acc = accounts.setdefault(
                row["account_id"],
                {
                    "account_id": row["account_id"],
                    "open_date": row["account_open_date"],
                    "devices": set(),
                    "ip_regions": set(),
                    "out_count": 0,
                    "out_amount": 0.0,
                },
            )
            acc["devices"].add(row["device_id"])
            acc["ip_regions"].add(row["ip_region"])
            acc["out_count"] += 1
            acc["out_amount"] += amt

            devices.setdefault(row["device_id"], set()).add(row["account_id"])

    # account-to-account money-flow edges. A counterparty is an account iff its
    # id carries the account prefix — receive-only accounts (which never
    # originate a row) must still be graph nodes, or the ring collapses.
    def _is_account(node_id: str) -> bool:
        return node_id.startswith("AC-")

    flow_edges = [
        {
            "txn_id": t["txn_id"],
            "src": t["src"],
            "dst": t["dst"],
            "amount": t["amount"],
            "timestamp": t["timestamp"],
            "hour": t["hour"],
        }
        for t in transactions
        if _is_account(t["dst"])
    ]

    # register receive-only accounts as nodes (no rows of their own)
    for e in flow_edges:
        if e["dst"] not in accounts:
            accounts[e["dst"]] = {
                "account_id": e["dst"],
                "open_date": None,
                "devices": set(),
                "ip_regions": set(),
                "out_count": 0,
                "out_amount": 0.0,
                "receive_only": True,
            }

    # shared-device links: a device used by >1 account is an identity bridge
    shared_device_links = [
        {"device_id": d, "accounts": sorted(accs)}
        for d, accs in devices.items()
        if len(accs) > 1
    ]

    # finalize account records (sets -> sorted lists for JSON)
    acc_list = []
    for a in accounts.values():
        acc_list.append(
            {
                "account_id": a["account_id"],
                "open_date": a["open_date"],
                "devices": sorted(a["devices"]),
                "ip_regions": sorted(a["ip_regions"]),
                "out_count": a["out_count"],
                "out_amount": round(a["out_amount"], 2),
                "receive_only": a.get("receive_only", False),
            }
        )

    graph = {
        "source": str(csv_path.name),
        "fingerprint": fingerprint,
        "transactions": transactions,
        "accounts": acc_list,
        "flow_edges": flow_edges,
        "shared_device_links": shared_device_links,
        "stats": {
            "n_txns": len(transactions),
            "n_accounts": len(accounts),
            "n_flow_edges": len(flow_edges),
            "n_shared_devices": len(shared_device_links),
        },
    }

    bus.write(NAMESPACE, "graph", graph)
    bus.write(NAMESPACE, "manifest", {"fingerprint": fingerprint, "source": csv_path.name})

    s = graph["stats"]
    reason = (
        f"ingested {s['n_txns']} txns, {s['n_accounts']} accounts; built "
        f"{s['n_flow_edges']} account→account flow edges, "
        f"{s['n_shared_devices']} shared-device links"
    )
    graph["reason"] = reason
    bus.write(NAMESPACE, "graph", graph)
    bus.log("grapher", "write", reason)
    return graph
