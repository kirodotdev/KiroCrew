"""Model provenance is persisted ONCE, at the crash-safe pre-spawn point.

``SubagentInfo.requested_model`` / ``resolved_model`` are written to disk
before the ``subagent_spawn`` event fires, so a gateway restart in the window
between the event and any later state write cannot lose them — orphan recovery
rebuilds the record from disk (GPT review on #3582). The later ``session_id``
state write in ``_run`` used to re-write the same two fields; that second write
was pure redundant I/O on the spawn hot path and was dropped (#5394). These
tests pin both halves: exactly one provenance write, ordered before the spawn
event, and a session_id write that no longer carries the provenance fields.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# ``SubagentManager.spawn`` refuses while the host looks short of memory, which
# is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _mock_sessions(served_model: str) -> MagicMock:
    """A mock SessionManager whose provider serves *served_model* and streams
    nothing (zero turns) — enough to drive ``_run_inner`` end to end."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    # Public accessor read by _resolved_model_of at spawn time. Plain string
    # attribute: an auto-created AsyncMock child would stringify to a mock repr
    # and masquerade as a served model id.
    provider.served_model = served_model

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


@pytest.mark.asyncio
async def test_provenance_written_once_before_the_spawn_event() -> None:
    """One write carries requested_model/resolved_model, and it lands BEFORE
    the ``subagent_spawn`` event — the crash-safe ordering orphan recovery
    depends on. The later session_id write must NOT re-write those fields."""
    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    # Per-spawn pin: becomes the requested side of the downgrade comparison.
    info = SubagentInfo(id="prov01", task="provenance task", model="model-req")
    manager._agents[info.id] = info

    # Ordered trace of every update_state call and every fired event, so the
    # pre-spawn ordering is asserted on one timeline. update_state runs via
    # asyncio.to_thread for the provenance write, but that thread is awaited
    # before the event fires, so the trace order is deterministic.
    trace: list[tuple[str, dict[str, Any]]] = []

    def _spy_update(agent_id: str, **kwargs: Any) -> bool:
        trace.append(("update_state", dict(kwargs)))
        return True

    orig_fire = manager._fire_event

    async def _spy_fire(kind: str, *args: Any, **kwargs: Any) -> None:
        trace.append(("event", {"kind": kind}))
        await orig_fire(kind, *args, **kwargs)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_spy_update),
        patch.object(manager, "_fire_event", _spy_fire),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    writes = [kw for tag, kw in trace if tag == "update_state"]
    prov_writes = [kw for kw in writes if "requested_model" in kw or "resolved_model" in kw]
    # Exactly one provenance write on this path (the empty stream never reaches
    # the CC first-chunk refinement, which only fills a still-empty value).
    assert len(prov_writes) == 1, f"expected one provenance write, got {prov_writes}"
    assert prov_writes[0]["requested_model"] == "model-req"
    assert prov_writes[0]["resolved_model"] == "model-served"

    # The session_id bookkeeping write no longer re-writes provenance (#5394).
    sid_writes = [kw for kw in writes if "session_id" in kw]
    assert sid_writes, "expected the session_id state write to still happen"
    for kw in sid_writes:
        assert (
            "requested_model" not in kw and "resolved_model" not in kw
        ), f"session_id write re-persists provenance: {kw}"

    # Crash-safe ordering: the provenance write precedes the spawn event.
    prov_idx = next(
        i for i, (tag, kw) in enumerate(trace) if tag == "update_state" and "requested_model" in kw
    )
    spawn_idx = next(
        i
        for i, (tag, kw) in enumerate(trace)
        if tag == "event" and kw.get("kind") == "subagent_spawn"
    )
    assert prov_idx < spawn_idx, "provenance must persist before subagent_spawn"


@pytest.mark.asyncio
async def test_provenance_write_retries_once_on_transient_failure() -> None:
    """The pre-spawn write is the SINGLE owner of the provenance fields, so a
    transient failure gets its second chance from that write's own bounded
    retry -- not from a second writer downstream (the dropped session_id
    re-write). The retry must still land before the spawn event, and a
    persistence failure must never block the spawn."""
    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="prov02", task="provenance retry task", model="model-req")
    manager._agents[info.id] = info

    trace: list[tuple[str, dict[str, Any]]] = []
    provenance_attempts = {"n": 0}

    def _flaky_update(agent_id: str, **kwargs: Any) -> bool:
        if "requested_model" in kwargs:
            provenance_attempts["n"] += 1
            if provenance_attempts["n"] == 1:
                raise OSError("transient fs hiccup")
        trace.append(("update_state", dict(kwargs)))
        return True

    orig_fire = manager._fire_event

    async def _spy_fire(kind: str, *args: Any, **kwargs: Any) -> None:
        trace.append(("event", {"kind": kind}))
        await orig_fire(kind, *args, **kwargs)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_flaky_update),
        patch.object(manager, "_fire_event", _spy_fire),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    # The failure was retried exactly once and the retry landed the write.
    assert provenance_attempts["n"] == 2
    landed = [
        (i, kw)
        for i, (tag, kw) in enumerate(trace)
        if tag == "update_state" and "requested_model" in kw
    ]
    assert len(landed) == 1, f"expected the retry to land one write, got {landed}"
    assert landed[0][1]["requested_model"] == "model-req"
    spawn_idx = next(
        i
        for i, (tag, kw) in enumerate(trace)
        if tag == "event" and kw.get("kind") == "subagent_spawn"
    )
    assert landed[0][0] < spawn_idx, "retried write must still precede subagent_spawn"
    # The spawn itself completed despite the transient failure.
    assert info.error == ""


@pytest.mark.asyncio
async def test_provenance_write_retries_on_silently_skipped_merge() -> None:
    """``update_state`` SKIPS the merge (returns False) when the current state
    cannot be read, without raising. The retry loop must treat that reported
    skip as a failure -- only a reported successful write ends the loop
    (GPT review round 2 on #5824: a silent no-op must not pass for success)."""
    sessions = _mock_sessions(served_model="model-served")
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        is_yolo=lambda: True,
    )
    info = SubagentInfo(id="prov03", task="provenance skip task", model="model-req")
    manager._agents[info.id] = info

    provenance_attempts = {"n": 0}
    landed: list[dict[str, Any]] = []

    def _skippy_update(agent_id: str, **kwargs: Any) -> bool:
        if "requested_model" in kwargs:
            provenance_attempts["n"] += 1
            if provenance_attempts["n"] == 1:
                return False  # the silent skip: no exception, nothing written
            landed.append(dict(kwargs))
        return True

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state", side_effect=_skippy_update),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")

    assert provenance_attempts["n"] == 2, "a reported skip must trigger the retry"
    assert len(landed) == 1 and landed[0]["requested_model"] == "model-req"
    assert info.error == ""


def test_update_state_reports_write_vs_skip(tmp_path: object) -> None:
    """The return contract the retry depends on: True when the merge was
    written, False when it was skipped because state.json is unreadable."""
    from kiro_crew.subagent_persistence import (
        create_agent_folder,
        read_state,
        update_state,
    )

    create_agent_folder("prov-rc1", task="task")
    assert update_state("prov-rc1", requested_model="model-req") is True
    state = read_state("prov-rc1")
    assert state is not None and state["requested_model"] == "model-req"
    # No folder / no state.json: the merge is skipped and reported as such.
    assert update_state("prov-rc-missing", requested_model="model-req") is False
