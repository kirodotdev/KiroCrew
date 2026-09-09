"""Advisor: opt-in, asynchronous, cross-model session reviewer.

Disabled by default. When enabled for a session, the advisor observes the
primary agent's work at host-owned checkpoints (tool-result groups, finalized
text segments, turn completion), reviews it on an isolated reviewer session,
and returns severity-aware advice without impersonating the primary agent.

Subsystem contract: docs/system-specs/modules/advisor.md.
"""
