"""ACP package — Agent Client Protocol for kiro-cli.

Importing the package loads none of its modules: each public name is imported from its
owner on first access (PEP 562), so importing one small ``kiro_crew.acp`` module does not
also load the client and everything under it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.acp.client import (
        AcpClient,
        AcpConversationBindingMismatch,
        AcpError,
        AcpPermissionNeeded,
        AcpProcessDied,
        AcpRegistrationRateLimited,
        AcpTimeoutError,
    )
    from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
    from kiro_crew.acp.types import AcpEvent, AcpPromptStats, JsonRpcMessage, JsonRpcRequest

__all__ = [
    "AcpClient",
    "AcpConversationBindingMismatch",
    "AcpError",
    "AcpPermissionNeeded",
    "AcpProcessDied",
    "AcpRegistrationRateLimited",
    "AcpTimeoutError",
    "AcpRuntime",
    "AcpSessionHandle",
    "AcpEvent",
    "AcpPromptStats",
    "JsonRpcMessage",
    "JsonRpcRequest",
]

#: The module each public name is read from. Nothing is cached here, so a patch of the
#: owner's name is what the package answers while it lasts.
_OWNERS = {
    "AcpClient": "kiro_crew.acp.client",
    "AcpConversationBindingMismatch": "kiro_crew.acp.client",
    "AcpError": "kiro_crew.acp.client",
    "AcpPermissionNeeded": "kiro_crew.acp.client",
    "AcpProcessDied": "kiro_crew.acp.client",
    "AcpRegistrationRateLimited": "kiro_crew.acp.client",
    "AcpTimeoutError": "kiro_crew.acp.client",
    "AcpRuntime": "kiro_crew.acp.runtime",
    "AcpSessionHandle": "kiro_crew.acp.runtime",
    "AcpEvent": "kiro_crew.acp.types",
    "AcpPromptStats": "kiro_crew.acp.types",
    "JsonRpcMessage": "kiro_crew.acp.types",
    "JsonRpcRequest": "kiro_crew.acp.types",
}


# Hidden from type checkers, as in ``acp/client.py``: mypy types every unknown attribute
# of a module that defines ``__getattr__`` as ``Any``. It sees the names through the
# ``TYPE_CHECKING`` imports above instead.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        if name not in _OWNERS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(importlib.import_module(_OWNERS[name]), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
