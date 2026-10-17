"""JSON-RPC framing for signal-cli's ``jsonRpc`` stdio mode.

signal-cli's ``jsonRpc`` command speaks exactly the framing the iMessage bridge
does -- newline-delimited JSON-RPC 2.0 on the child's stdin/stdout, diagnostics
on stderr, a clean exit when stdin closes -- so the peer is reused rather than
copied: one framing implementation, one set of oversized/unparseable-line
rules.

The one addition is :meth:`SignalRpcPeer.settle_stderr`. signal-cli reports
the commonest startup failure ("User +1... is not registered.") on stderr and
exits immediately, and the reader sees stdout EOF before the stderr drain has
necessarily read that line. Waiting for the drain to finish (bounded) before
reading :attr:`last_stderr` is what turns "bridge exited" into a reason an
operator can act on.
"""

from __future__ import annotations

import asyncio

from kiro_crew.imessage.rpc import (  # noqa: F401  (re-exported)
    DEFAULT_CALL_TIMEOUT_S,
    JsonRpcPeer,
    RpcError,
    RpcTransportError,
)

#: Upper bound on waiting for the stderr pipe to reach EOF after the child died.
STDERR_SETTLE_S = 2.0


class SignalRpcPeer(JsonRpcPeer):
    """:class:`JsonRpcPeer` plus a bounded wait for the child's last stderr line."""

    async def settle_stderr(self, timeout: float = STDERR_SETTLE_S) -> str:
        """Wait (bounded) for stderr to drain, then return its last line.

        Safe to call on a live child: it returns the tail seen so far once the
        bound expires, without cancelling the drain.
        """
        task = self._stderr_task
        if task is not None and not task.done():
            # ``asyncio.wait`` neither raises the task's outcome nor cancels it
            # on expiry, so a drain still running keeps running.
            await asyncio.wait({task}, timeout=timeout)
        return self.last_stderr
