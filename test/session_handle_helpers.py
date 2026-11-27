"""``bare_session_handle``: the one way a test builds an ``AcpSessionHandle`` without ``__init__``.

A unit test of one handle method skips the constructor and sets only the fields that
method reads. Every such handle comes from this factory, so when a handle method starts
reading a new runtime attribute or awaiting a new step, the default is answered here once
instead of in each test file. ``test_bare_session_handle_guard.py`` refuses a direct
``AcpSessionHandle.__new__`` or ``object.__new__(AcpSessionHandle)`` in a test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle

_REAL_RUNTIME: Any = object()


def bare_session_handle(
    session_id: str = "s-1", *, runtime: Any = _REAL_RUNTIME, **fields: Any
) -> AcpSessionHandle:
    """Return an ``AcpSessionHandle`` built without ``__init__``.

    ``runtime`` defaults to a real ``AcpRuntime`` that never spawns (Kiro backend,
    sandbox off), so every runtime attribute a handle method reads exists with its real
    default. Pass a double to steer the runtime, or ``None`` for a handle with none:
    a method that reads the runtime through ``getattr(..., default)`` then takes its
    no-runtime branch, which a real runtime would not.
    ``settle_kas_model_select`` is an ``AsyncMock``: the KAS start path awaits it, and
    the tests that drive that path do not exercise the late model-select read. A test
    of ``settle_kas_model_select`` itself builds the handle with its constructor.
    ``fields`` are set on the handle last, so they override any default above.
    """
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = session_id
    handle._runtime = AcpRuntime(sandbox_mode="off") if runtime is _REAL_RUNTIME else runtime
    handle.settle_kas_model_select = AsyncMock()  # type: ignore[method-assign]
    for name, value in fields.items():
        setattr(handle, name, value)
    return handle
