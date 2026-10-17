"""Overlapping session starts never activate a skill view older than the newest one.

Every start on a shared runtime re-prepares the native skill projection right
before ``session/set_mode`` and sends the alias that preparation published. Two
starts can overlap: one whose preparation read the agent's spec BEFORE an edit
revoked a grant, and one whose preparation read it after. The runtime has to keep
the newer view authoritative, so the older start may neither become the
runtime's projection nor send its alias once the newer view is adopted -- not on
its first attempt, not on a retry, and not by having the host answer its request
after the newer view was adopted.

These drive the REAL ``_activate_mode_bracketed``, reader and ``_send_and_await``
against a fake kiro-cli. Only ``prepare_native_skill_projection`` is scripted: it
"reads the spec" (a shared variable the test edits to model the revocation) and
can be parked between that read and its return, the gap in which the real
function waits on the cross-process alias lock.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.skill_projection import NativeSkillProjection
from kiro_crew.acp.types import METHOD_SET_MODE

SPAWN_ALIAS = "kirocrew-skill-view-" + "0" * 24
# The view prepared from the spec before the edit that revoked a grant.
OLD_ALIAS = "kirocrew-skill-view-" + "a" * 24
# The view prepared from the spec after it.
NEW_ALIAS = "kirocrew-skill-view-" + "b" * 24


class _FakeKiro:
    """Answers ``set_mode`` from the aliases it has loaded; can hold one answer."""

    def __init__(self, reader: asyncio.StreamReader, loaded: set[str]) -> None:
        self.reader = reader
        self.loaded = loaded
        self.set_modes: list[str] = []
        # When set, the next set_mode naming this alias is not answered until
        # ``release`` is called: the host is still working on it.
        self.hold: str | None = None
        self.held: dict[str, Any] | None = None

    def write(self, data: bytes) -> None:
        frame = json.loads(data)
        if "id" not in frame:
            return
        if frame.get("method") != METHOD_SET_MODE:
            self._answer(frame["id"], result={})
            return
        mode = frame["params"]["modeId"]
        self.set_modes.append(mode)
        if self.hold is not None and mode == self.hold and self.held is None:
            self.hold = None
            self.held = frame
            return
        self._answer_set_mode(frame)

    def release(self) -> None:
        """Answer the held request from what the host has loaded NOW."""
        frame, self.held = self.held, None
        assert frame is not None
        self._answer_set_mode(frame)

    def _answer_set_mode(self, frame: dict[str, Any]) -> None:
        mode = frame["params"]["modeId"]
        if mode in self.loaded:
            self._answer(frame["id"], result={})
        else:
            self._answer(
                frame["id"],
                error={
                    "code": -32603,
                    "message": "Internal error",
                    "data": f"Mode '{mode}' not found",
                },
            )

    def _answer(self, req_id: int, **body: object) -> None:
        self.reader.feed_data(
            (json.dumps({"jsonrpc": "2.0", "id": req_id, **body}) + "\n").encode()
        )


class _ScriptedPrepare:
    """``prepare_native_skill_projection`` reading a spec the test can edit.

    The read happens on entry. A call listed in *parked* then waits on its own
    gate before returning -- the real function waits on the alias lock exactly
    there, after its spec read and before it publishes and returns.
    """

    def __init__(self, spec: dict[str, str], parked: dict[int, threading.Event]) -> None:
        self.spec = spec
        self.parked = parked
        self.read: dict[int, threading.Event] = {i: threading.Event() for i in parked}
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, work_dir: Path, **_kw: Any) -> NativeSkillProjection:
        with self._lock:
            call = self.calls
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            alias = self.spec["ops"]
            if call in self.parked:
                self.read[call].set()
                assert self.parked[call].wait(10), "parked preparation never released"
            return NativeSkillProjection({"ops": alias})
        finally:
            with self._lock:
                self.active -= 1


def _runtime(loaded: set[str]) -> tuple[AcpRuntime, _FakeKiro]:
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    kiro = _FakeKiro(reader, loaded)
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock(side_effect=kiro.write)
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    rt._native_skill_projection = NativeSkillProjection({"ops": SPAWN_ALIAS})
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    return rt, kiro


@pytest.fixture(autouse=True)
def rescans(monkeypatch):
    """Record the in-place rescan nudges instead of touching an agents directory."""
    from kiro_crew.acp import skill_projection

    seen: list[str] = []
    monkeypatch.setattr(skill_projection, "announce_alias", seen.append)
    return seen


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(runtime_mod, "_PROJECTED_MODE_RETRY_DELAYS_SECS", (0.0, 0.0))


async def _until(condition: Callable[[], bool], what: str) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _waiting_on_projection_lock(rt: AcpRuntime) -> bool:
    lock = getattr(rt, "_skill_projection_lock_obj", None)
    return bool(lock is not None and lock.locked() and getattr(lock, "_waiters", None))


def _start(rt: AcpRuntime, session_id: str) -> "asyncio.Task[None]":
    return asyncio.ensure_future(
        rt._activate_mode_bracketed(
            session_id, "ops", budget=5.0, payload_snapshot=None, wire_registered=True
        )
    )


async def _run(rt: AcpRuntime, prepare: _ScriptedPrepare, body: Callable[[], Any]) -> None:
    reader = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        with (
            patch(
                "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
                side_effect=prepare,
            ),
            patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None),
        ):
            await asyncio.wait_for(body(), timeout=10)
    finally:
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass


def _never_after(sent: list[str], newer: str, older: str) -> bool:
    return newer in sent and older not in sent[sent.index(newer) :]


@pytest.mark.asyncio
async def test_a_preparation_that_read_before_a_revocation_is_not_adopted_after_a_newer_one():
    """The finding's shape: start A reads the spec, then waits (the alias lock);
    the spec is edited to revoke a grant; start B reads the edit. A's preparation
    returning last must not become the runtime's projection, and its alias must
    not be sent once B's view is adopted."""
    rt, kiro = _runtime({OLD_ALIAS, NEW_ALIAS})
    spec = {"ops": OLD_ALIAS}
    gate_a = threading.Event()
    prepare = _ScriptedPrepare(spec, parked={0: gate_a})
    outcomes: dict[str, BaseException | None] = {}

    async def body() -> None:
        task_a = _start(rt, "sA")
        assert await asyncio.to_thread(prepare.read[0].wait, 5), "start A never read the spec"
        spec["ops"] = NEW_ALIAS  # the revocation lands between the two reads
        task_b = _start(rt, "sB")
        # B either runs to completion past the parked A, or waits its turn.
        await _until(lambda: task_b.done() or _waiting_on_projection_lock(rt), "start B to settle")
        gate_a.set()
        for name, task in (("sA", task_a), ("sB", task_b)):
            try:
                await task
                outcomes[name] = None
            except AcpRuntimeError as exc:
                outcomes[name] = exc

    try:
        await _run(rt, prepare, body)
    finally:
        gate_a.set()

    assert rt._native_skill_projection.agent("ops") == NEW_ALIAS
    assert _never_after(kiro.set_modes, NEW_ALIAS, OLD_ALIAS), kiro.set_modes
    assert outcomes["sB"] is None
    # One preparation at a time per runtime: the two never interleave their reads.
    assert prepare.max_active == 1


@pytest.mark.asyncio
async def test_a_retry_after_a_newer_view_was_adopted_never_resends_the_older_alias(
    monkeypatch,
):
    """A's alias is not loaded yet, so A retries after a forced rescan. B adopts
    the post-revocation view meanwhile. A's retry must not name its own, older
    alias -- the rescan makes the host load it, and the retry would activate it."""
    from kiro_crew.acp import skill_projection

    rt, kiro = _runtime({NEW_ALIAS})
    monkeypatch.setattr(skill_projection, "announce_alias", kiro.loaded.add)
    counted: list[str] = []
    monkeypatch.setattr(
        runtime_mod, "emit_counter", lambda _name, attrs: counted.append(attrs["outcome"])
    )
    kiro.hold = OLD_ALIAS
    spec = {"ops": OLD_ALIAS}
    prepare = _ScriptedPrepare(spec, parked={})

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        spec["ops"] = NEW_ALIAS
        # The projection lock is not held across set_mode: B completes while A's
        # request is in flight.
        await _start(rt, "sB")
        kiro.release()  # A's first attempt misses; its rescan then loads OLD
        await task_a

    await _run(rt, prepare, body)

    # A's retry lands on the newer alias first time: B's send, then A's.
    assert kiro.set_modes == [OLD_ALIAS, NEW_ALIAS, NEW_ALIAS]
    # The miss belonged to the replaced alias, so no "loaded after a reload".
    assert counted == []
    rt.terminate_session.assert_not_awaited()
    assert rt._native_skill_projection.agent("ops") == NEW_ALIAS


@pytest.mark.asyncio
async def test_a_start_answered_after_a_newer_view_was_adopted_is_refused(monkeypatch):
    """A's set_mode is in flight when B adopts a view that changed A's agent. The
    host may have activated A's older alias after B's read, so A is not started
    on it: the session is terminated and the refusal names the remedy."""
    rt, kiro = _runtime({OLD_ALIAS, NEW_ALIAS})
    kiro.hold = OLD_ALIAS
    spec = {"ops": OLD_ALIAS}
    prepare = _ScriptedPrepare(spec, parked={})
    raised: list[BaseException] = []
    counted: list[str] = []
    monkeypatch.setattr(
        runtime_mod, "emit_counter", lambda _name, attrs: counted.append(attrs["outcome"])
    )

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        spec["ops"] = NEW_ALIAS
        await _start(rt, "sB")
        kiro.release()  # the host answers A: it activated the OLD alias
        try:
            await task_a
        except AcpRuntimeError as exc:
            raised.append(exc)

    await _run(rt, prepare, body)

    assert raised, "start A succeeded on a view a newer preparation had replaced"
    assert "restart the gateway" in str(raised[0])
    rt.terminate_session.assert_awaited_once_with("sA")
    assert counted == ["refused_superseded"]


@pytest.mark.asyncio
async def test_overlapping_starts_on_an_unchanged_view_both_succeed():
    """No edit, no refusal: the generation check compares views, not counters,
    so a concurrent start whose agent did not change is not penalised."""
    rt, kiro = _runtime({OLD_ALIAS})
    kiro.hold = OLD_ALIAS
    spec = {"ops": OLD_ALIAS}
    prepare = _ScriptedPrepare(spec, parked={})

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        await _start(rt, "sB")
        kiro.release()
        await task_a

    await _run(rt, prepare, body)

    assert kiro.set_modes == [OLD_ALIAS, OLD_ALIAS]
    rt.terminate_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_retry_whose_agent_the_newer_view_refuses_fails_without_the_older_alias(
    monkeypatch,
):
    """The edit made the agent's view unpreparable (B's projection refuses it).
    A's retry has no fresh alias to send and must not fall back to its own."""
    from kiro_crew.acp import skill_projection

    rt, kiro = _runtime(set())
    monkeypatch.setattr(skill_projection, "announce_alias", kiro.loaded.add)
    kiro.hold = OLD_ALIAS
    prepared = iter(
        [
            NativeSkillProjection({"ops": OLD_ALIAS}),
            NativeSkillProjection({}, errors={"ops": "skill_search is disabled"}),
        ]
    )
    raised: list[BaseException] = []

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        with pytest.raises(ValueError, match="skill_search is disabled"):
            await _start(rt, "sB")  # B's own start refuses the agent
        kiro.release()  # A's first attempt misses; its rescan then loads OLD
        try:
            await task_a
        except AcpRuntimeError as exc:
            raised.append(exc)

    reader = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        with (
            patch(
                "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
                side_effect=lambda *_a, **_k: next(prepared),
            ),
            patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None),
        ):
            await asyncio.wait_for(body(), timeout=10)
    finally:
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass

    assert kiro.set_modes == [OLD_ALIAS]
    assert raised and "skill_search is disabled" in str(raised[0])
    assert "restart the gateway" in str(raised[0])
    rt.terminate_session.assert_any_await("sA")


def test_adoption_refuses_a_preparation_older_than_the_adopted_one():
    """Generations decide adoption, not arrival order: an older preparation that
    returns after a newer one changes nothing, and neither does a replay of the
    adopted generation."""
    rt, _kiro = _runtime(set())
    older = NativeSkillProjection({"ops": OLD_ALIAS})
    newer = NativeSkillProjection({"ops": NEW_ALIAS})
    first, second = rt._issue_skill_projection_generation(), rt._issue_skill_projection_generation()

    assert rt._adopt_skill_projection(newer, second) is True
    assert rt._adopt_skill_projection(older, first) is False
    assert rt._adopt_skill_projection(older, second) is False
    assert rt._native_skill_projection is newer
    assert rt._adopted_skill_projection_generation() == second


@pytest.mark.asyncio
async def test_an_answer_that_arrives_while_a_newer_preparation_runs_waits_for_it():
    """B has read the revocation and is still preparing (parked) when the host
    answers A's older alias. Judging A against the adopted generation at that
    instant would pass; A must wait for B's adoption and then be refused."""
    rt, kiro = _runtime({OLD_ALIAS, NEW_ALIAS})
    kiro.hold = OLD_ALIAS
    spec = {"ops": OLD_ALIAS}
    gate_b = threading.Event()
    prepare = _ScriptedPrepare(spec, parked={1: gate_b})
    outcomes: dict[str, BaseException | None] = {}

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        spec["ops"] = NEW_ALIAS
        task_b = _start(rt, "sB")
        assert await asyncio.to_thread(prepare.read[1].wait, 5), "start B never read the spec"
        kiro.release()  # the host answers A while B is still preparing
        await _until(lambda: task_a.done() or _waiting_on_projection_lock(rt), "start A to settle")
        gate_b.set()
        for name, task in (("sA", task_a), ("sB", task_b)):
            try:
                await task
                outcomes[name] = None
            except AcpRuntimeError as exc:
                outcomes[name] = exc

    try:
        await _run(rt, prepare, body)
    finally:
        gate_b.set()

    assert outcomes["sB"] is None
    assert outcomes["sA"] is not None, "start A kept a view B's preparation had replaced"
    assert "restart the gateway" in str(outcomes["sA"])
    rt.terminate_session.assert_awaited_once_with("sA")


@pytest.mark.asyncio
async def test_a_cancelled_newer_start_still_adopts_the_view_it_read():
    """B reads the revocation and is cancelled before its preparation returns.
    The lock must not open for A's check until B's worker finishes and its view
    is adopted, so A -- answered for the older alias meanwhile -- is refused."""
    rt, kiro = _runtime({OLD_ALIAS, NEW_ALIAS})
    kiro.hold = OLD_ALIAS
    spec = {"ops": OLD_ALIAS}
    gate_b = threading.Event()
    prepare = _ScriptedPrepare(spec, parked={1: gate_b})
    raised: list[BaseException] = []

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        spec["ops"] = NEW_ALIAS
        task_b = _start(rt, "sB")
        assert await asyncio.to_thread(prepare.read[1].wait, 5), "start B never read the spec"
        task_b.cancel()
        await asyncio.sleep(0.01)
        kiro.release()  # the host answers A while B's worker is still running
        await _until(lambda: task_a.done() or _waiting_on_projection_lock(rt), "start A to settle")
        gate_b.set()
        with pytest.raises(asyncio.CancelledError):
            await task_b
        try:
            await task_a
        except AcpRuntimeError as exc:
            raised.append(exc)

    try:
        await _run(rt, prepare, body)
    finally:
        gate_b.set()

    assert rt._native_skill_projection.agent("ops") == NEW_ALIAS
    assert raised, "start A kept a view a cancelled start's preparation had replaced"
    # Refused because B's view was adopted, not merely because B did not finish.
    assert "changed while this session was starting" in str(raised[0])
    assert "restart the gateway" in str(raised[0])
    assert kiro.set_modes == [OLD_ALIAS]  # B was cancelled before its send


@pytest.mark.asyncio
async def test_a_newer_preparation_that_yields_no_view_leaves_a_pending_start_unverified():
    """B reads the specs but its preparation returns no view (the alias lock is
    busy elsewhere). B fails, and A, answered while B ran, cannot prove the view
    the host activated is current, so it is refused too. A later adopted view
    clears that state for the next start."""
    rt, kiro = _runtime({OLD_ALIAS})
    kiro.hold = OLD_ALIAS
    results = iter(
        [
            NativeSkillProjection({"ops": OLD_ALIAS}),  # A
            None,  # B: no view could be prepared
            NativeSkillProjection({"ops": OLD_ALIAS}),  # C, after both
        ]
    )
    b_running = threading.Event()
    gate_b = threading.Event()

    def prepare(*_a: Any, **_k: Any) -> NativeSkillProjection | None:
        result = next(results)
        if result is None:
            b_running.set()
            assert gate_b.wait(10)
        return result

    outcomes: dict[str, BaseException | None] = {}

    async def body() -> None:
        task_a = _start(rt, "sA")
        await _until(lambda: kiro.held is not None, "A's set_mode to reach the host")
        task_b = _start(rt, "sB")
        assert await asyncio.to_thread(b_running.wait, 5), "start B never prepared"
        kiro.release()
        await _until(lambda: task_a.done() or _waiting_on_projection_lock(rt), "start A to settle")
        gate_b.set()
        for name, task in (("sA", task_a), ("sB", task_b)):
            try:
                await task
                outcomes[name] = None
            except AcpRuntimeError as exc:
                outcomes[name] = exc
        await _start(rt, "sC")
        outcomes["sC"] = None

    reader = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        with (
            patch(
                "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
                side_effect=prepare,
            ),
            patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None),
        ):
            await asyncio.wait_for(body(), timeout=10)
    finally:
        gate_b.set()
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass

    assert "could not be prepared" in str(outcomes["sB"])
    assert "did not complete" in str(outcomes["sA"])
    assert outcomes["sC"] is None
    assert kiro.set_modes == [OLD_ALIAS, OLD_ALIAS]  # A's, then C's


@pytest.mark.asyncio
async def test_a_cancelled_preparation_whose_worker_fails_stays_a_cancellation():
    """The worker is waited for through the cancellation; if it then raises, the
    caller still sees the cancellation it asked for, not the worker's error."""
    gate = threading.Event()

    def failing() -> None:
        assert gate.wait(10)
        raise OSError("disk went away")

    task = asyncio.ensure_future(runtime_mod._prepare_projection_to_completion(failing))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done(), "the cancelled preparation stopped waiting for its worker"
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
