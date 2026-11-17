"""One transient read error on the ``agent_state`` sidecar, for the strict-read tests.

A test module is not importable from another one, so the fault lives in this helpers
module, the repo's convention (see ``agent_helpers`` / ``mcp_merge_helpers``).
"""

from __future__ import annotations

import pytest

from kiro_crew import agent_state


def fail_one_sidecar_read(monkeypatch: pytest.MonkeyPatch, func_name: str, agent: str) -> None:
    """Fail the first sidecar read inside a call of ``agent_state.<func_name>`` naming *agent*.

    The read fails the way a file another process holds without read sharing does on
    Windows (``PermissionError`` 13): a strict read raises it and a lenient one folds it
    to ``{}``, exactly as ``agent_state._read`` treats an unreadable file. Every later
    read, and every call for another agent, sees the real sidecar.
    """
    real_read = agent_state._read
    real_func = getattr(agent_state, func_name)
    armed: list[bool] = []

    def read(*, strict: bool = False) -> dict:
        if armed:
            armed.clear()
            if strict:
                raise PermissionError(13, "The process cannot access the file")
            return {}
        return real_read(strict=strict)

    def func(*args, **kwargs):
        if agent in args or kwargs.get("name") == agent:
            armed.append(True)
        try:
            return real_func(*args, **kwargs)
        finally:
            armed.clear()

    monkeypatch.setattr(agent_state, "_read", read)
    monkeypatch.setattr(agent_state, func_name, func)
