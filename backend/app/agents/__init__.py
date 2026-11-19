"""LangGraph agents that run inside one Temporal activity each.

Rules for every graph here:
- It runs start to finish in one call: no checkpointer, no interrupts, no waiting for
  people.
- Every model call goes through ModelGateway.
- Its only I/O is the read-only tools in tools.py.
"""
