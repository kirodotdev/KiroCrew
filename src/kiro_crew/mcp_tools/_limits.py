"""Numeric limits shared by a tool's descriptor and its handler.

A leaf module on purpose: it imports nothing, so both ``mcp_core`` and the
descriptor modules can read a limit at import time without a cycle. A limit
quoted in a tool description MUST come from here rather than a literal, so
the advertised default cannot drift from the enforced one.
"""

from __future__ import annotations

# Backstop cycle cap for a monitor loop the model arms without naming one.
# A loop that reaches it ran out of rope rather than finishing, so the value
# is a runaway guard, not a target.
_MONITOR_DEFAULT_MAX_CYCLES = 24
_MONITOR_DEFAULT_MAX_RUNTIME_SECS = 14_400
