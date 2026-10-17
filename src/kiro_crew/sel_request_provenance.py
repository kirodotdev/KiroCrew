"""Request-bound forwarded provenance for SEL rows a handler writes itself.

The gateway's auth and audit layers hold the request and pass
``log_api_access(forwarded=...)`` from ``dashboard.origin.is_proxied_request``.
A route handler's own ``log_api_access`` call does not, and threading the
argument through every handler is the kind of rule a new call site silently
misses. So the deny-audit boundary, the outermost audit layer of both gateway
chains, binds that same predicate's result here for the whole request, and
``log_api_access`` reads it when its caller passed no ``forwarded``.

The value is the gateway's own verdict on the request it is serving, computed
once at the boundary. Nothing a handler, an app or a request body supplies can
set it: the only writer is :func:`bind_request_forwarded`, and an explicit
``forwarded=`` from a caller still wins, exactly as before.

Lifetime is the request, not the context. ``asyncio`` tasks and
``asyncio.to_thread`` copy the context they were started from, so a plain
``ContextVar[bool]`` would let a background task spawned by a request keep
filing rows under that request's provenance long after the response went out.
The var therefore holds a binding whose ``live`` flag the boundary clears when
the request ends; a copied context shares the binding object, so its later rows
read ``None`` and stay keyless. Work awaited inside the request (a
``to_thread`` call, a task the handler awaits) still records it. A plain
``threading.Thread`` copies no context, and a row written outside any request
finds no binding: both stay keyless, which means "provenance not recorded",
never "made directly".
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class _Binding:
    __slots__ = ("forwarded", "live")

    def __init__(self, forwarded: bool) -> None:
        self.forwarded = forwarded
        self.live = True


_CURRENT: ContextVar[_Binding | None] = ContextVar("sel_request_forwarded", default=None)


@contextmanager
def bind_request_forwarded(forwarded: bool) -> Iterator[None]:
    """Bind *forwarded* as the provenance of every row written while the request runs."""
    binding = _Binding(bool(forwarded))
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        binding.live = False
        _CURRENT.reset(token)


def current_request_forwarded() -> bool | None:
    """The live request's ``forwarded`` verdict, or ``None`` outside one."""
    binding = _CURRENT.get()
    if binding is None or not binding.live:
        return None
    return binding.forwarded
