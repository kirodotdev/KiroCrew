"""A native child card's Stop, through a shared-runtime session's provider.

``dashboard.native_child_stop`` asks the slot's client, which on the shared runtime is
this provider, to stop one harness-native child and names the requests that child left
waiting. Both answers are the handle's (``session_handle_claude_children``).

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields written: none.

Instance fields read: ``_handle``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider


async def cancel_native_child(self: AcpSessionProvider, child_sid: str) -> bool:
    """See ``AcpSessionHandle.cancel_native_child``; False on a handle without it."""
    cancel = getattr(self._handle, "cancel_native_child", None)
    return bool(await cancel(child_sid)) if callable(cancel) else False


def native_child_requests(self: AcpSessionProvider, child_sid: str) -> tuple[str, ...]:
    """See ``AcpSessionHandle.native_child_requests``; none on a handle without it."""
    requests = getattr(self._handle, "native_child_requests", None)
    return tuple(requests(child_sid)) if callable(requests) else ()
