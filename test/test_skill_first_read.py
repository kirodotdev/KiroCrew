"""A gateway's first session-start skill read, made once before any session asks for it.

The first skill read of a process walks the packaged skill trees and reads each packaged
skill's ``app:`` line, and both results are kept for the life of the process. The gateway
makes that read once, past readiness and on a worker thread, so the first session's
prompt build does not wait for it after the person's message has arrived.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from typing import Any

import pytest
from context_composition_contract_helpers import (  # noqa: F401 -- the fixture is requested by name
    _Rig,
    rig_fixture,
)
from skills_helpers import _create_skill, _settled

from kiro_crew import skills as skills_mod
from kiro_crew.skill_runtime import catalog as catalog_mod
from kiro_crew.testing.wait import await_until, finished

pytestmark = [pytest.mark.usefixtures("ample_host_resources", "close_skills_loaders")]


@pytest.fixture(name="first_read_mod")
def first_read_mod_fixture() -> Any:
    from kiro_crew import skill_first_read

    return skill_first_read


def _session_start(rig: _Rig) -> str:
    """A default-agent dashboard session start with no project, as a first chat makes it."""
    return rig.builder.build_session_context(
        session_key="dashboard:first", runtime_source="dashboard", provider_type="acp"
    )


class _WatchedFuture(asyncio.Future):  # type: ignore[type-arg]
    """A stand-in for the builtin sync task that says when something waits on it."""

    def __init__(self) -> None:
        super().__init__()
        self.watched = asyncio.Event()

    def add_done_callback(self, fn: Any, *, context: Any = None) -> None:
        super().add_done_callback(fn, context=context)
        self.watched.set()


def test_the_read_ahead_is_the_read_a_session_start_makes(
    rig: _Rig, first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real = rig.skills.get_context

    def recording(*args: Any, **kwargs: Any) -> str:
        # The out-list is the caller's own and is filled by the call: compare what went in.
        seen = {k: list(v) if k == "required_parts_out" else v for k, v in kwargs.items()}
        calls.append((args, seen))
        return real(*args, **kwargs)

    monkeypatch.setattr(rig.skills, "get_context", recording)

    first_read_mod.first_read(rig.builder)
    assert len(calls) == 1
    ahead = calls.pop()

    _session_start(rig)
    assert calls == [ahead]


def test_a_session_start_after_it_reads_no_packaged_skill(
    rig: _Rig, first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = {"claims": 0, "names": 0}

    @functools.lru_cache(maxsize=None)
    def claims(packaged_root: str) -> dict[str, str]:
        reads["claims"] += 1
        return {}

    @functools.lru_cache(maxsize=1)
    def names() -> frozenset[str]:
        reads["names"] += 1
        return frozenset()

    # The two results a process keeps for its lifetime, as counters of their one read.
    monkeypatch.setattr(catalog_mod, "_packaged_app_claims", claims)
    monkeypatch.setattr(skills_mod, "_packaged_skill_names", names)
    _create_skill(rig.tmp / "skills", "mine", "---\nname: mine\ndescription: d\n---\nbody\n")
    _settled(rig.skills)

    first_read_mod.first_read(rig.builder)
    assert reads == {"claims": 1, "names": 1}
    assert "mine" in _session_start(rig).partition("[Skills:]")[2]
    assert reads == {"claims": 1, "names": 1}

    # Without the read ahead, the session start is what makes both reads.
    claims.cache_clear()
    names.cache_clear()
    _session_start(rig)
    assert reads == {"claims": 2, "names": 2}


@pytest.mark.asyncio
async def test_the_read_waits_for_the_builtin_sync_and_runs_on_a_worker_thread(
    first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = object()
    ran: list[tuple[object, int]] = []
    monkeypatch.setattr(
        first_read_mod, "first_read", lambda b: ran.append((b, threading.get_ident()))
    )
    synced = _WatchedFuture()

    task = asyncio.ensure_future(first_read_mod.first_read_after_ready(builder, synced))
    await await_until(synced.watched, what="the read never waited for the builtin sync")
    assert ran == []

    synced.set_result([])
    await await_until(task, what="the read did not end after the sync did")
    assert [b for b, _ in ran] == [builder]
    assert ran[0][1] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_gateway_with_no_sync_task_reads_at_once(
    first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(first_read_mod, "first_read", ran.append)

    await finished(first_read_mod.first_read_after_ready("builder", None), "the read ahead")
    assert ran == ["builder"]


@pytest.mark.asyncio
async def test_a_gateway_with_no_context_builder_reads_nothing(
    first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(first_read_mod, "first_read", ran.append)

    await finished(first_read_mod.first_read_after_ready(None, None), "the read ahead")
    assert ran == []


@pytest.mark.asyncio
async def test_a_failed_read_is_dropped(
    first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(builder: object) -> None:
        raise RuntimeError("the skills tree could not be read")

    monkeypatch.setattr(first_read_mod, "first_read", failing)

    assert (
        await finished(first_read_mod.first_read_after_ready(object(), None), "the read ahead")
        is None
    )


@pytest.mark.asyncio
async def test_cancelling_the_read_leaves_the_builtin_sync_running(
    first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(first_read_mod, "first_read", ran.append)
    synced = _WatchedFuture()

    task = asyncio.ensure_future(first_read_mod.first_read_after_ready(object(), synced))
    await await_until(synced.watched, what="the read never waited for the builtin sync")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled read did not end")

    assert not synced.done()
    assert ran == []
    synced.set_result([])


@pytest.mark.asyncio
async def test_a_cancelled_sync_reads_nothing(
    first_read_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(first_read_mod, "first_read", ran.append)
    synced: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    synced.cancel()

    await finished(first_read_mod.first_read_after_ready(object(), synced), "the read ahead")
    assert ran == []


def test_the_gateway_starts_the_read_past_the_ready_line_and_does_not_wait_for_it() -> None:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    source = inspect.getsource(GatewayOrchestrator.run)
    started = "first_read_after_ready(self.ctx_builder, self._skills_sync_task)"
    assert source.count(started) == 1
    assert source.index('print(f"KIROCREW_READY:') < source.index(started)
    assert "await first_read_after_ready" not in source
