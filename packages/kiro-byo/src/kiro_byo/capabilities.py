"""Runtime facts exposed by the Kiro BYO management App.

KiroCrew owns its ten-row backend card.  Kiro BYO deliberately does not copy
that matrix or score locally: duplicate declarations drift as soon as either
side changes.  This module reports only facts Kiro BYO can prove about itself.
"""
from __future__ import annotations

from typing import Any

from .native_tools import advertised_native_tool_names
from .protocol import runtime_capability_report


def capability_report() -> dict[str, Any]:
    return runtime_capability_report(
        native_tools=advertised_native_tool_names(),
        mcp_transports=["stdio", "http"],
    )
