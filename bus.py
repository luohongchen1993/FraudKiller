"""Cognee-compatible memory bus — the handoff layer between the 4 agents.

This is RingFinder's "Cognee". Every agent both READS the prior agent's
namespace and WRITES its own, so "Agent N+1 used what Agent N found, via the
bus" is literally how the pipeline runs (MVP R02).

The default implementation is a local, file-backed store that runs cold with
zero API keys or network — ideal for the cold-operation judge demo. When
`RINGFINDER_USE_COGNEE=1` and an LLM_API_KEY is present, the pipeline also
mirrors its findings into a real Cognee dataset (see `cognee_sync.py`) and runs
`cognify()` so the graph can be inspected in Cognee's UI; detection still runs
on this local store for determinism.

The API intentionally mirrors Cognee's mental model:
    bus.write(namespace, key, value)   ~ cognee.add(...)   into a dataset
    bus.read(namespace, key)           ~ reading that dataset back
    bus.log(agent, action, detail)     ~ the visible handoff trace
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / ".ringfinder_bus"


class MemoryBus:
    """A namespaced, file-backed key/value store with an append-only handoff log.

    Namespaces map to the four agents:
        grapher, detective, investigator, reporter
    Plus `system` for the cross-agent handoff trace.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---- core key/value (the "memory") --------------------------------------
    def _path(self, namespace: str, key: str) -> Path:
        ns = self.root / namespace
        ns.mkdir(parents=True, exist_ok=True)
        safe = key.replace("/", "_")
        return ns / f"{safe}.json"

    def write(self, namespace: str, key: str, value: Any) -> None:
        """Persist a value under namespace/key (Cognee `add` analogue)."""
        with self._lock:
            payload = {"_written_at": time.time(), "value": value}
            self._path(namespace, key).write_text(json.dumps(payload, default=str))

    def read(self, namespace: str, key: str, default: Any = None) -> Any:
        """Read a value the prior agent wrote (the handoff)."""
        p = self._path(namespace, key)
        if not p.exists():
            return default
        with self._lock:
            return json.loads(p.read_text())["value"]

    def has(self, namespace: str, key: str) -> bool:
        return self._path(namespace, key).exists()

    def keys(self, namespace: str) -> list[str]:
        ns = self.root / namespace
        if not ns.exists():
            return []
        return sorted(p.stem for p in ns.glob("*.json"))

    # ---- handoff trace (the "provable collaboration", MVP criterion 2) ------
    def log(self, agent: str, action: str, detail: str) -> None:
        rec = {"t": time.time(), "agent": agent, "action": action, "detail": detail}
        with self._lock:
            with (self.root / "handoff_log.jsonl").open("a") as fh:
                fh.write(json.dumps(rec) + "\n")

    def trace(self) -> list[dict]:
        f = self.root / "handoff_log.jsonl"
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]

    def reset(self) -> None:
        """Clear the bus so a fresh pipeline run starts clean."""
        import shutil

        with self._lock:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
