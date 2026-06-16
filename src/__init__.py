"""RingFinder — a 4-agent fraud-ring detection pipeline that hands findings
through a Cognee-compatible memory bus.

Agents:
  1. Grapher        — ingests transactions, builds the knowledge graph
  2. Ring Detective — computes 6 structural patterns + Ring Risk Score
  3. Investigator   — answers analyst questions, grounded in the graph
  4. Case Reporter  — assembles a downloadable, SAR-ready case pack

Every agent reads the prior agent's writes from the bus and writes its own
results back. The bus is the handoff architecture.
"""

__version__ = "0.1.0"
