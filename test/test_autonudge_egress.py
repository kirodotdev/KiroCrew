"""Tests for the auto-nudge egress scrub, its wire flags, and store vetting.

A nudge loop's text fields reach two very different places. The model receives
``message`` whole on every cycle -- that is the guarantee the nudge exists to
provide, and it is never rewritten. Everything a READER sees, though, is an
egress surface: the REST projection, the WS broadcast, and the transcript row are
persisted and served to every connected dashboard client.

So the scrub belongs at the SINKS, and this suite pins that:

1. **Every text field is scrubbed unless it is named.** A denylist, not an
   allowlist, so the next free-text field added to ``NudgeLoop`` is covered by
   the same loop rather than silently missed. The addressing fields are the
   deliberate exemption, because a rewritten ``id`` or ``slot_key`` would leave a
   row the client cannot act on.
2. **A scrubbed projection must never be written back as truth.** The wire
   carries ``message_redacted`` so a client can say the text is masked, and
   ``message_ignored`` when the backend declined a write, and the echo guard
   refuses a PATCH that merely returns the masked copy.
3. **A store it cannot vet is refused, not guessed at.** A row whose addressing
   field is credential-shaped is held aside rather than armed, and an unreadable
   sidecar refuses writes and is moved aside so recovery is a restart.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import multiprocessing
import re
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_authz as authz
from kiro_crew.autonudge import (
    AutoNudgeService,
    AutoNudgeStaleBaseline,
    AutoNudgeStoreUnvetted,
    NudgeLoop,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import autonudge as autonudge_handlers
from kiro_crew.monitoring.models import MonitorState
from kiro_crew.security import redact
from kiro_crew.slack import gateway as gw


def _moved_aside_sidecar(base_dir: Path) -> Path:
    """The single ``.corrupt-<ts>`` copy an unreadable sidecar is renamed to.

    Design review asked for the move-aside so recovery is a restart rather than a hand
    repair; the bytes must still be there, which is what these tests assert on.
    """
    matches = sorted(base_dir.glob("autonudge.quarantine.json.corrupt-*"))
    assert len(matches) == 1, f"expected exactly one moved-aside copy, got {matches!r}"
    return matches[0]


def _held_aside_rows(base_dir) -> list:
    """Read held-aside rows from the quarantine sidecar, the single durable location."""
    path = Path(base_dir) / "autonudge.quarantine.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("quarantined", [])


#: A held row whose offending addressing field an operator has since repaired: well
#: formed, and claimed by neither slot nor id in the store, so nothing but the hold
#: contract itself keeps it out of the live map.
_REPAIRED_HELD_ROW = {
    "id": "repaired",
    "slot_key": "chat-9-9",
    "message": "held aside, then repaired by the operator",
    "idle_secs": 300,
}


# ── Fire-path harness (mirrors test_autonudge_dashboard_fire.py) ──


def _loop(**kw) -> NudgeLoop:
    base = dict(
        id="loop-abc",
        slot_key="chat-1-1785",
        message="the full multi-paragraph babysit instruction",
        idle_secs=300,
        max_cycles=24,
        cycle_count=3,
    )
    base.update(kw)
    return NudgeLoop(**base)  # type: ignore[arg-type]


def _slot(key: str = "chat-1-1785") -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = False
    slot._in_stage_execution = False
    return slot


def _orchestrator() -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = SimpleNamespace(
        get_slot=MagicMock(return_value=None),
        push_slots_update=MagicMock(),
        _background_tasks=set(),
        run_background_turn=MagicMock(side_effect=lambda _slot, coro: coro),
    )
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch._session_tasks = {}
    return orch


def _fake_spawn():
    def _spawn(state, slot, coro, **kwargs):
        coro.close()
        return MagicMock(name="turn-task")

    return _spawn


async def _fire(loop: NudgeLoop, *, ledger: str = "") -> tuple[str, str]:
    """Fire once; return ``(appended_row, prompt_passed_to_run_chat)``.

    Both halves come from the REAL ``_fire_dashboard_nudge``, so the two can be
    compared against each other rather than against a re-derivation of what the
    code is assumed to do.
    """
    appended, prompt, _meta = await _fire_full(loop, ledger=ledger)
    return appended, prompt


async def _fire_full(loop: NudgeLoop, *, ledger: str = "") -> tuple[str, str, dict]:
    """``_fire`` plus the appended row's ``meta`` block.

    Split out rather than changing ``_fire``'s shape so the row-vs-prompt tests
    that only care about the two strings keep reading exactly as before.
    """
    orch = _orchestrator()
    slot = _slot()
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    run_chat = AsyncMock()

    async def _compose(message, sentinel, slot_key):
        # Stand in for the ledger-snapshot composer: exercised with and without
        # a snapshot, because the composed body is what the prompt carries while
        # the prompt keeps it.
        body = message.replace("{{STOP_FILE}}", sentinel or "")
        return f"{ledger}\n\n{body}" if ledger else body

    with (
        patch.object(gw, "spawn_guarded_turn", _fake_spawn()),
        patch.object(gw, "compose_nudge_body", new=AsyncMock(side_effect=_compose)),
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
    ):
        assert await orch._fire_dashboard_nudge(loop) is True

    appended = slot.append.call_args.args[1]
    prompt = run_chat.call_args.args[2]
    meta = slot.append.call_args.kwargs["meta"]["nudge"]
    return appended, prompt, meta


def _mp_noop() -> None:
    pass


def _fork_probe_context():
    """A fork context whose children the parent can reap, or skip the probe.

    These lock probes must run OUT of process. A deadlock regression inside a thread
    leaves it blocked holding the sidecar lock with its descriptor open past teardown,
    and nothing in the parent can release either -- the fd belongs to the blocked frame.
    A child is terminated in ``finally`` and the OS drops its locks and descriptors.
    """
    probe = None
    try:
        ctx = multiprocessing.get_context("fork")
        probe = ctx.Process(target=_mp_noop)
        probe.start()
        probe.join(timeout=10)
    except (ValueError, PermissionError, OSError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"fork-based lock probes unavailable in this environment: {exc}")
    finally:
        # A join that TIMES OUT reaps nothing, and ``skip`` leaves by the same door, so the
        # reap belongs here; ``close`` only once dead, since it raises on a live process.
        if probe is not None:
            _reap(probe)
            if not probe.is_alive():
                probe.close()
    return ctx


def _reap(proc) -> None:
    """Terminate then kill, so no probe process outlives the test that started it."""
    for stop in (proc.terminate, proc.kill):
        if not proc.is_alive():
            return
        stop()
        proc.join(timeout=5)


def _appeared(path: Path, timeout: float) -> bool:
    """Wait for a child's sentinel file, the cross-process stand-in for an Event."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.02)
    return path.exists()


def _mp_nested_move_aside(base_dir: str, done_path: str) -> None:
    """Re-enter the sidecar lock and move an unreadable sidecar aside beneath it."""
    from kiro_crew.autonudge import AutoNudgeService

    svc = AutoNudgeService(base_dir=Path(base_dir))
    with svc._sidecar_transaction():
        svc._move_aside_unreadable_sidecar()
    Path(done_path).write_text("ok", encoding="utf-8")


def _mp_rival_acquire(lock_path: str, entered_path: str) -> None:
    """Acquire the sidecar lock from a SECOND process and record having got in."""
    with _an._locked_file(Path(lock_path), "a+"):
        Path(entered_path).write_text("in", encoding="utf-8")


class TestMalformedEntryWarningWithholdsTheRow:
    """The construction-failure sink must not dump the row it failed to build.

    F1, first-principles review. Every repair arm in ``_load`` withholds the value's
    VALUE and scrubs the id, on the stated ground that the rule belongs to the
    SINK rather than to one branch. The ``except`` arm wrapping those arms did
    not: it logged ``%r`` of the whole persisted dict, so a row that fails to
    construct put every field -- ``message`` and
    any credential inside either -- into the same log ring and ``/api/logs``
    stream the arms exist to keep it out of.

    The row is the ONE object in the function guaranteed to be attacker-shaped:
    construction failed precisely because it was not the shape we expected.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _write_malformed_store(self, tmp_path, extra) -> None:
        """A row that CANNOT construct: ``slot_key`` is REQUIRED and absent.

        The omission has to be a required dataclass field. A wrong-TYPE value
        does not work -- dataclasses do no type checking, so ``idle_secs={}``
        constructs happily and no exception is raised at all (measured: the first
        draft of this fixture used exactly that and the row loaded).
        """
        row = {
            "id": "abc123",
            "message": f"deploy with {self.SECRET}",
            "idle_secs": 300,
        }
        row.update(extra)
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_an_uncapped_loop_also_owes_the_interrupted_cycle(self, tmp_path) -> None:
        """A cap applied later is refused against ``cycle_count``, so it is never inert."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "uncapped1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 0,
            "cycle_count": 4,
            "inflight_cycle": 5,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["uncapped1"]
            assert (
                loaded.max_cycles == 0
            ), f"precondition: fixture is not uncapped, max_cycles={loaded.max_cycles!r}"
            assert loaded.cycle_count == 4, (
                "an uncapped loop COMMITTED a cycle whose delivery was never confirmed, so a "
                "cap applied later is measured against a nudge that never went out"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_capped_loop_still_owes_the_interrupted_cycle(self, tmp_path) -> None:
        """The other arm: a cap DOES bill the cycle, so there the recovery must survive."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "capped1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["capped1"]
            assert loaded.max_cycles == 9, "precondition: fixture is not capped"
            assert loaded.cycle_count == 4, (
                "a capped loop counted an undelivered cycle as spent, so the cap bills a nudge "
                "that never went out"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_held_claim_survives_the_rewrite_that_follows_the_restart(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): the held claim was in memory only, so a rewrite erased it.

        ``_serialized_loops`` re-emitted the marker from the delivering map alone, and the
        load-time hold does not populate that map -- so the next write dropped it and a
        second restart found nothing to hold.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "held1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._unreconciled_claim.get("held1") == 5, "precondition: nothing was held"

            await svc._persist_locked()

            disk = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert (
                disk.get("inflight_cycle") == 5
            ), "the rewrite dropped the held claim, so the next restart replays the turn: " + repr(
                disk.get("inflight_cycle")
            )
            assert disk.get("cycle_count") == 4, "the held cycle was spent by the rewrite"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_boolean_claim_value_is_not_read_as_cycle_one(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): ``isinstance(True, int)`` let a boolean read as cycle 1."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "boolclaim1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 1,
            "cycle_count": 0,
            "inflight_cycle": True,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["boolclaim1"]
            assert svc._unreconciled_claim == {}, (
                "a boolean was accepted as a cycle claim, so re-activation spends a phantom "
                "cycle: " + repr(svc._unreconciled_claim)
            )
            assert loaded.active is True, "a boolean claim stopped a loop that had nothing owed"
            assert loaded.cycle_count == 0, "a phantom cycle was billed against the cap"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_reactivation_write_keeps_the_unresolved_claim(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the rollback restored fields but not the popped claims.

        Reactivation pops both claim maps before persisting. The write-failure handler
        restored the dataclass fields and the two pending-wake sets, so a refused write
        left the claim gone from memory while the store still carried it -- and the next
        successful write then serialized without ``inflight_cycle``, erasing the durable
        marker and redelivering the interrupted turn.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "rollback1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._unreconciled_claim.get("rollback1") == 5, "precondition: nothing was held"
            # The load-time hold populates only the unreconciled map, so the delivering
            # entry is seeded here to reach the second claim this same block drops.
            delivering = (5, 1.0)
            svc._delivering_claim["rollback1"] = delivering

            def _refuse(payload):
                raise OSError("disk refused the reactivation write")

            svc._write_state = _refuse  # type: ignore[method-assign]

            with pytest.raises(OSError):
                await svc.update("rollback1", active=True)

            assert svc._unreconciled_claim.get("rollback1") == 5, (
                "the refused write erased the held claim, so the next write omits "
                "inflight_cycle and the interrupted turn is redelivered: "
                + repr(svc._unreconciled_claim)
            )
            assert svc._delivering_claim.get("rollback1") == delivering, (
                "the refused write erased the delivering claim, so the marker is re-emitted "
                "from neither map: " + repr(svc._delivering_claim)
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_settings_only_update_keeps_a_held_cycle_claim(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a settings-only edit on a held record dropped the claim.

        The pop ran before the active check, so editing an interval while reconciliation
        was still owed discarded the claim, and the write that followed serialized the
        row without ``inflight_cycle`` -- erasing the durable marker with no recovery.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "settings1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._loops["settings1"].active is False, "precondition: the claim was not held"
            assert svc._unreconciled_claim.get("settings1") == 5, "precondition: nothing was held"

            await svc.update("settings1", idle_secs=600)

            assert svc._unreconciled_claim.get("settings1") == 5, (
                "a settings-only edit spent the held claim, so the interrupted cycle is no "
                "longer owed: " + repr(svc._unreconciled_claim)
            )
            disk = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert disk.get("inflight_cycle") == 5, (
                "the settings write erased the durable marker, so a later restart replays the "
                "turn: " + repr(disk.get("inflight_cycle"))
            )
            assert disk.get("cycle_count") == 4, "a settings edit billed the held cycle"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_reconciling_a_spent_cycle_discharges_the_terminal_debt(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the reconcile settled the cycle but left the terminal debt.

        Accepting the interrupted cycle as spent says the final turn reached the reader,
        yet a surviving ``terminal_pending`` still records that turn as owed -- so the
        next fire delivers a terminal turn that was already delivered once.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "debt1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            held = svc._loops["debt1"]
            assert svc._unreconciled_claim.get("debt1") == 5, "precondition: nothing was held"
            held.monitor = MonitorState(
                kind="github_pull_request",
                target="owner/repo#123",
                objective="review_ready",
                created_ts=1_000.0,
            )
            # A GATED loop carries probe state while still delivering down the legacy
            # path, so it reaches the reconcile that a controller record is refused from.
            held.gate = True
            # The settlement write failed after the turn landed, so the debt outlived it
            # while ``terminal_delivered`` did not -- the load recovery needs both.
            held.monitor.terminal_pending = "success"

            await svc.update("debt1", active=True)

            assert held.monitor is not None, "the monitor was dropped by the update"
            assert held.monitor.terminal_pending == "", (
                "the reconcile spent the cycle but left the final turn owed, so it is "
                "delivered a second time: " + repr(held.monitor.terminal_pending)
            )
            assert svc._unreconciled_claim.get("debt1") is None, "the claim was not settled"
            assert held.cycle_count == 5, "the interrupted cycle was not accounted as spent"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_cancelled_persist_keeps_the_lock_until_the_write_lands(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): cancellation freed the lock while the write was in flight.

        The executor thread cannot be interrupted, so the snapshot lands regardless. A
        cancelled caller that frees ``_lock`` lets a later writer land behind it, and the
        claim it just committed then reads as spent on the next reactivation.
        """
        import asyncio
        import threading
        import time as _time

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            started = threading.Event()
            landed = threading.Event()

            def _slow_write(payload):
                started.set()
                _time.sleep(0.4)
                landed.set()

            svc._write_state = _slow_write  # type: ignore[method-assign]
            runner = asyncio.get_running_loop()

            task = asyncio.create_task(svc._persist_locked())
            assert await runner.run_in_executor(None, started.wait, 3.0), "the write never began"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert svc._lock.locked(), (
                "the cancelled caller released the lock while the write was still in "
                "flight, so a later writer can land behind the committed claim"
            )
            assert await runner.run_in_executor(None, landed.wait, 3.0), "the write never landed"

            for _ in range(100):
                if not svc._lock.locked():
                    break
                await asyncio.sleep(0.02)
            assert not svc._lock.locked(), "the supervised persist never released the lock"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unresolved_claim_is_held_inactive_instead_of_replaying_the_turn(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a restart between the claim and its turn re-delivered it.

        The claim is durable BEFORE the turn, so an unresolved one cannot say whether the
        reader already received that turn. Arming it anyway sent a second copy.
        """
        import asyncio

        store = tmp_path / "autonudge.json"
        row = {
            "id": "interrupted1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
            "next_due_ts": 1.0,
            "last_fire_ts": 1.0,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        fired: list[str] = []

        async def on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        try:
            await svc.start()
            await asyncio.sleep(0.4)

            assert (
                fired == []
            ), "the interrupted cycle was delivered a second time on restart: " + repr(fired)
            loaded = svc._loops["interrupted1"]
            assert (
                loaded.active is False
            ), "an unresolved claim was left armed, so the turn it claims runs again"
            assert loaded.cycle_count == 4, "the unresolved cycle was spent without a decision"

            await svc.update("interrupted1", active=True)

            assert (
                svc._loops["interrupted1"].cycle_count == 5
            ), "re-activation left the claim owed, so the next tick still repeats the turn"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_baseline_is_not_logged_as_a_detached_failure(
        self, tmp_path, caplog
    ) -> None:
        """Opus 4.8: the expected 409 read to an operator as an internal fault.

        ``AutoNudgeStaleBaseline`` is the concurrent-edit refusal the HTTP layer answers
        with 409, so a routine conflict must not emit a WARNING traceback. The second arm
        holds the other half: an exception that is NOT expected still logs as it did.
        """
        import asyncio

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="first goal", idle_secs=300)

            with caplog.at_level("WARNING"):
                with pytest.raises(AutoNudgeStaleBaseline):
                    await svc.update(
                        loop.id, message="second goal", expect_fingerprint="not-the-token"
                    )
                await asyncio.sleep(0)
            assert "detached update() failed" not in caplog.text, (
                "an ordinary concurrent-edit conflict logged a WARNING traceback, so a 409 "
                "the caller already surfaces reads to an operator as an internal failure"
            )

            caplog.clear()

            async def _boom(*_a, **_k):
                raise RuntimeError("genuinely unexpected")

            svc._update_unserialized = _boom
            with caplog.at_level("WARNING"):
                with pytest.raises(RuntimeError):
                    await svc.update(loop.id, message="third goal")
                await asyncio.sleep(0)
            assert "detached update() failed" in caplog.text, (
                "a real failure stopped logging, so the fix suppressed the whole sink rather "
                "than the one expected exception"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_normalisation_warning_above_the_guard_scrubs_its_own_values(
        self, tmp_path, caplog
    ) -> None:
        """GPT 5.6 (BLOCKING, fenced): a sink above the addressing guard logged a raw id.

        The guard quarantines credential- and newline-shaped addressing fields, but it runs
        AFTER the ``gate``/``self_armed`` normalisation. A row carrying both a credential-shaped
        ``id`` and a non-boolean flag therefore reached the log ring and ``/api/logs`` verbatim.
        Both arguments of both sinks are asserted, because the flag VALUE is agent-writable too.
        """
        row = {
            "id": f"loop-{self.SECRET}",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "self_armed": f"armed-{self.SECRET}",
            "gate": f"gated-{self.SECRET}",
        }
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                svc._load()
            # PRECONDITION: both normalisation sinks must actually have fired, or the test
            # asserts over an empty log and passes vacuously.
            assert (
                "non-boolean self_armed" in caplog.text
            ), "precondition: the self_armed normalisation never ran, so nothing was under test"
            assert (
                "non-boolean gate" in caplog.text
            ), "precondition: the gate normalisation never ran, so nothing was under test"
            assert self.SECRET not in caplog.text, (
                "a normalisation warning above the addressing guard emitted a store-sourced "
                "value verbatim, so a credential-shaped row reaches the log ring and /api/logs"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_malformed_row_is_not_echoed_into_the_log(self, tmp_path, caplog) -> None:
        """Fails on the unmodified tree, where ``%r`` of the row is logged."""
        self._write_malformed_store(tmp_path, {"banner": f"watching {self.SECRET}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert "abc123" not in svc._loops, "a malformed row was loaded anyway"
            assert self.SECRET not in caplog.text, "the warning echoed the credential"
            assert "watching" not in caplog.text, "the warning echoed the banner value"
            assert "deploy with" not in caplog.text, "the warning echoed the message value"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_warning_still_says_which_row_and_which_fields(
        self, tmp_path, caplog
    ) -> None:
        """Negative control: withholding the VALUES must not blank the warning.

        A fix that simply dropped the interpolation would pass the arm above
        while making a malformed row undiagnosable. The operator still needs the
        row's identity and the field NAMES present, which is what makes a
        hand-edited store fixable.
        """
        self._write_malformed_store(tmp_path, {"banner": "watching CI"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert "abc123" in caplog.text, "the warning names no row -- undiagnosable"
            assert "message" in caplog.text, "the warning names no field -- undiagnosable"
            assert "banner" in caplog.text, "the field NAMES are what make it fixable"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_credential_shaped_id_in_a_malformed_row_is_redacted(
        self, tmp_path, caplog
    ) -> None:
        """The id is named, so it gets the same scrub the repair arms give it.

        Naming the row cannot become a new leak: the id comes out of the same
        hand-editable store as every other field.
        """
        self._write_malformed_store(tmp_path, {"id": f"loop-{self.SECRET}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert self.SECRET not in caplog.text, "a credential-shaped id was echoed raw"
            assert "[REDACTED: credential]" in caplog.text, "the id was not scrubbed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row", [None, 42, "oops", [1, 2], True])
    async def test_a_non_dict_row_is_skipped_not_fatal(self, tmp_path, caplog, row) -> None:
        """F1, Opus + GPT (BLOCKING): a non-object row must SKIP, not kill startup.

        ``loops`` is hand-editable JSON, so an element need not be an object at
        all. Construction raises, this arm runs, and ``raw.get`` does not exist on
        a non-dict -- so the arm meant to SKIP the row instead raises
        ``AttributeError`` out of ``_load``, out of the unguarded
        ``run_in_executor(None, self._load)`` in ``start()``, and NO loop arms.
        The previous ``%r`` handler tolerated this; the id-scrubbing rewrite
        introduced the regression.

        Fails on the unmodified tree with AttributeError, not an assertion.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()  # must not raise
            assert svc._loops == {}, "a non-dict row produced a loop"
            assert "malformed" in caplog.text, "the row was dropped with no warning at all"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_dict_row_still_arms_normally(self, tmp_path) -> None:
        """Negative control for the guard: a WELL-FORMED row must still load.

        A guard written as "treat everything as non-dict" would pass the arm above
        while making the loader load nothing at all.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "good1",
                            "slot_key": "chat-9-1",
                            "message": "go",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "good1" in svc._loops, "a well-formed row failed to arm"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_credential_shaped_field_name_is_redacted(self, tmp_path, caplog) -> None:
        """F2, GPT (BLOCKING): the joined field NAMES are attacker-controlled too.

        ``raw`` is hand-editable JSON, so its KEYS are as untrusted as its values
        -- a key can itself be a credential. ``bad_id`` above is run through both
        redactors; the joined names were not, which is the asymmetry. Same sink,
        same log ring, same ``/api/logs`` stream.

        Fails on the unmodified tree, where the key is joined in verbatim.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [{"id": "abc123", f"tok_{self.SECRET}": 1}]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert self.SECRET not in caplog.text, "a credential-shaped field NAME was echoed"
        finally:
            svc.stop()


class TestPersistenceRoundTrip:
    """``_load`` filters unknown keys, which is what makes this additive.

    Both directions are pinned because only one of them is obvious. Old store /
    new code is the upgrade everyone will hit. New store / OLD code is the
    DOWNGRADE — a user reverting the release — and it is the direction that
    would justify bumping ``_STORE_VERSION`` if it raised. It does not, so the
    version stays at 1 rather than signalling a break that is not happening.
    """

    def _write_store(self, tmp_path, loops: list[dict]) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": loops}), encoding="utf-8"
        )


class TestRestSurface:
    def _app(self, monkeypatch, fake_svc):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import autonudge as _handler

        monkeypatch.setattr(_handler, "_autonudge_get", lambda: fake_svc)
        state = MagicMock()
        state._slots = {"chat-1-123": MagicMock(workspace="default")}
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/autonudge", _handler.api_autonudge_start)
        app.router.add_patch("/api/autonudge/{loop_id}", _handler.api_autonudge_update)
        return app

    def _svc(self, **loop_kw):
        svc = MagicMock()
        loop = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="go", **loop_kw)
        svc.add = AsyncMock(return_value=loop)
        svc.update = AsyncMock(return_value=loop)
        # Harness parity: the real ``AutoNudgeService`` exposes ``list_all``, and
        # the update authorizer uses it to resolve an opaque ``loop_id`` to its
        # slot key before deciding whether a banner is supported there. A bare
        # ``MagicMock`` would return a non-iterable mock and turn that lookup into
        # a 500 that says nothing about the code under test.
        svc.list_all = lambda: [loop]
        svc.get_by_id = lambda _id, _rows=[loop]: next(
            (r for r in _rows if getattr(r, "id", None) == _id), None
        )
        return svc


class TestTheListSerializerScrubsLoopText:
    """``GET /api/autonudge`` was the third sink, and it served ``message`` raw.

    ``_load`` repairs the store and the transcript row scrubs at the sink, but the
    REST serializer was a bare ``asdict``. Three producers reach ``svc.add``
    without the authorizer -- the goal loop (``dashboard/chat_runner.py``),
    auto-research, and issue-radar, whose message is composed from external issue
    text -- so this is reachable, not theoretical.
    """

    def test_a_credential_in_message_does_not_reach_the_client(self) -> None:
        """The item First Principles counted as the unfixed sibling."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(message=f"do the thing {secret}"))
        assert secret not in out["message"], "the REST surface serves an unredacted credential"
        assert "REDACTED" in out["message"]

    def test_a_clean_loop_round_trips_unchanged(self) -> None:
        """The scrub must not rewrite ordinary text.

        Its own arm because a scrub that replaced every value with a placeholder
        would satisfy the arm above while destroying the surface.
        """
        loop = _loop(message="run the next cycle")
        out = autonudge_handlers._serialize(loop)
        assert out["message"] == "run the next cycle"

    def test_the_addressing_fields_are_never_rewritten(self) -> None:
        """``id`` and ``slot_key`` must survive verbatim or the client cannot act.

        A rewritten ``id`` would break ``PATCH``/``DELETE`` targeting, turning a
        redaction into a functional regression.
        """
        loop = _loop(id="loop-abc", slot_key="chat-1-1785")
        out = autonudge_handlers._serialize(loop)
        assert out["id"] == "loop-abc"
        assert out["slot_key"] == "chat-1-1785"

    def test_a_field_the_scrub_does_not_name_is_still_covered(self) -> None:
        """The denylist shape, pinned.

        ``stopped_reason`` is agent-supplied free text (``autonudge_stop(reason=)``)
        and is named nowhere in the scrub. An allowlist would have missed it, which
        is exactly how a new free-text field comes to need a scrub of its own.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        out = autonudge_handlers._serialize(_loop(stopped_reason=f"gave up {secret}"))
        assert secret not in out["stopped_reason"]


class TestTheCountedLogHygieneSiblings:
    """First Principles: the log-hygiene rule this PR argues from left 2 siblings.

    The lane named both: ``cron.py`` logging a malformed entry's id, and
    the autonudge loader logging a malformed row. Both
    read from a store an agent can write directly, and both land in the log ring
    and the ``/api/logs`` stream.
    """


class TestScrubbedLogTextCannotForgeARecord:
    """GPT 5.6 (BLOCKING): the ``_load`` warnings returned control characters intact.

    The two redactors remove credential- and URL-shaped SUBSTRINGS. Neither is a
    ``str``-shape control: a newline survives both, so a store-supplied id or key
    carrying one arrives whole at the ``%s`` warning and splits one record into
    several in the log ring and the ``/api/logs`` stream. The forged tail is
    attacker-authored and indistinguishable from a real line, which makes the log
    unreliable exactly where it must diagnose a hand-edited store.

    The escape is supplied by ``repr`` -- ``redact(repr(value))``, the spelling
    ``cron.py`` uses for the identical malformed-entry warning and the base's own
    at ``session_storage.py``. That replaced a hand-rolled ``str.isprintable``
    comprehension: First Principles asked for one definition rather than two, and
    ``repr`` escapes the same set, because CPython's ``repr`` for ``str`` keys its
    own escaping on ``str.isprintable``. Measured across newline, CR, ESC, NUL,
    U+2028 and a lone surrogate: identical escaping, plus a literal backslash is
    now escaped too, which REMOVES an ambiguity the hand-rolled version accepted.

    Not a denylist on ``\\n``. Every non-printable character is escaped, so
    ``\\r`` (a bare CR rewrites a line in a terminal), ``\\x1b`` (an ANSI escape
    can erase the line above it), and U+2028/U+2029 (line separators several log
    viewers honour) cannot be substituted for it tomorrow.
    """

    # A tail that would read as a whole extra record if a newline survived.
    FORGED = "AutoNudge: all clear, nothing to see here"

    @staticmethod
    def _scrub(value: object) -> str:
        """The spelling the ``_load`` warnings use, applied here verbatim."""
        return redact(repr(value))

    def test_a_newline_is_escaped_not_returned_raw(self) -> None:
        out = self._scrub(f"loop-abc\n{self.FORGED}")
        assert "\n" not in out, f"a raw newline survived the sink: {out!r}"
        assert out.count("\\n") == 1, f"the newline was dropped rather than escaped: {out!r}"
        assert "loop-abc" in out, "the value was destroyed rather than escaped"
        assert self.FORGED in out, "the tail was dropped -- escape, do not truncate"

    @pytest.mark.parametrize(
        "raw,name",
        [
            ("\r", "carriage return"),
            ("\t", "tab"),
            ("\x1b", "ANSI escape"),
            ("\x0b", "vertical tab"),
            ("\x85", "NEL"),
            ("\u2028", "line separator"),
            ("\u2029", "paragraph separator"),
        ],
    )
    def test_every_control_character_is_escaped_not_just_newline(self, raw, name) -> None:
        """The denylist-on-newline fix would pass the arm above and fail here.

        Output encoding is transformed against what is ALLOWED (printable)
        rather than by enumerating what is forbidden. A denylist is always
        incomplete: escape only ``\\n`` and the next separator becomes the next
        bug.
        """
        out = self._scrub(f"loop-abc{raw}tail")
        assert raw not in out, f"a raw {name} survived the sink: {out!r}"
        assert out.isprintable(), f"the return is not a single printable line: {out!r}"

    def test_printable_text_survives_readably(self) -> None:
        """Negative control: the escape must not MANGLE ordinary values.

        ``repr`` quotes the value, which is the accepted cost of using the shared
        spelling rather than a second one -- so the assertion is that the text
        arrives intact and unescaped INSIDE the quotes, not that the return is
        byte-identical to the input. A fix that escaped indiscriminately would
        pass every arm above and fail here.
        """
        for value in ("loop-abc", "id with spaces", "banner, message", "café — ok"):
            out = self._scrub(value)
            assert value in out, f"a printable value was altered: {value!r} -> {out!r}"
            assert "\\" not in out, f"an ordinary value picked up an escape: {out!r}"

    def test_a_credential_is_still_redacted_after_the_escape(self) -> None:
        """Negative control: the escape must not displace the redaction."""
        out = self._scrub("loop-AKIAIOSFODNN7EXAMPLE\nx")
        assert "AKIAIOSFODNN7EXAMPLE" not in out, "the credential survived"
        assert "[REDACTED: credential]" in out, "the credential arm did not run"
        assert "\n" not in out, "a raw newline survived alongside the redaction"

    def test_the_load_warnings_use_the_shared_spelling(self) -> None:
        """First Principles: one definition for both loaders, not two.

        Pins that ``autonudge.py`` reaches for ``redact(repr(...))`` and never
        carries a hand-rolled escape of its own.

        Banning the token ``isprintable`` outright would over-reach: the
        subtraction it protects is the removal of a hand-rolled log ESCAPE, and the
        addressing guard now uses the same builtin for a different job -- deciding
        whether to REFUSE a persisted row at the trust boundary. Banning the token
        conflated the two, so the ban is replaced by the narrower property that was
        always the point: the only use is the refusal predicate, and nothing here
        escapes text for a log line by hand.
        """
        src = Path(_an.__file__).read_text(encoding="utf-8")
        assert "_scrub_for_log" not in src, "the second spelling is still defined"
        # ONE spelling, defined once, and now homed beside ``redact_via_context`` in
        # ``platform.context`` rather than in this service module: it is generic log
        # hygiene, and leaving it here made ``cron.py`` import the whole autonudge
        # service for a five-line helper. Same reasoning the PR applied to
        # ``MAX_BANNER_CHARS``. It still routes through the ACTIVE credential policy
        # instead of the bare ``security.redact``, which let a composed host's own
        # patterns be skipped.
        # ONE spelling, defined once, and homed with its ONLY consumer. The relocation
        # to ``platform.context`` was justified by ``cron.py`` importing this module for
        # a five-line helper; with the cron and ops_mission_control loader redactions
        # deferred to their own PR (Design + First Principles both asked for that split),
        # that justification is gone and the helper belongs beside the code that uses it.
        assert "def redact_store_value(" in src, "the one shared log-scrub spelling is not defined"
        assert (
            src.count("redact_store_value(") >= 5
        ), "the store-sourced log sinks do not share one spelling"
        assert (
            "redact_log_via_context(repr(value))" in src
        ), "the shared spelling no longer routes through the active credential policy"
        assert src.count("redact(repr(") == 0, "a bare-redactor log scrub survives"
        assert "redact_credentials(" not in src, "the redaction pair is still hand-rolled"
        # ``isprintable`` now has TWO uses, and the property being pinned is that
        # EVERY use is an addressing-field VALIDITY predicate -- never a hand-rolled
        # log escape, which is the subtraction this test protects. The second use is
        # the refused-row eviction deliberately re-applying the load-time guard's own
        # test, so it only ever drops a row whose key it could actually vet.
        assert src.count("isprintable") == 1, (
            "isprintable count moved -- the load-time validity check is the only "
            "user now that the refused-row eviction is gone"
        )
        assert (
            "not got.isprintable()" in src
        ), "the load-time addressing refusal predicate no longer uses it"

    @pytest.mark.asyncio
    async def test_a_newline_bearing_id_cannot_forge_a_log_record(self, tmp_path, caplog) -> None:
        """End to end through the real ``_load`` warning, not just the helper.

        The unit arms pin the sink; this one pins that the sink is what the
        warning actually uses. ``slot_key`` is omitted so construction fails and
        the malformed-entry arm runs -- the arm that names the id.
        """
        from kiro_crew.autonudge import AutoNudgeService as _Svc

        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [{"id": f"abc123\n{self.FORGED}", "idle_secs": 300}],
                }
            ),
            encoding="utf-8",
        )
        svc = _Svc(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            warnings = [r for r in caplog.records if "malformed loop entry" in r.getMessage()]
            assert warnings, "the malformed-entry arm did not run -- fixture is wrong"
            for rec in warnings:
                msg = rec.getMessage()
                assert "\n" not in msg, f"the record was split into several lines: {msg!r}"
                assert not msg.startswith(self.FORGED), "the forged tail became its own record"
        finally:
            svc.stop()


class TestNoMutationCommitsWhenTheResponseCannotBeSerialized:
    """GPT 5.6 (BLOCKING): the write landed and the caller was told it failed.

    The ordering was the defect. On a host whose credential policy cannot compose:

    * a MESSAGELESS request scrubs nothing during authorization --
      the message compare is
      gated on ``message is not None`` -- so nothing raises before the mutation;
    * ``svc.add`` / ``svc.update`` COMMITS (authz :408 / :692, after the critical
      audit);
    * only then does the handler serialize the response, and ``_serialize`` ->
      ``scrub_loop_text`` -> ``redact_via_context`` raises.

    Result: HTTP 500 with the mutation already persisted and audited as ``success``.
    The store and the caller's belief about the store disagree, permanently, and a
    retry would apply it twice.

    The fix probes the policy in BOTH authorizers before auditing or mutating, so an
    unusable policy is a clean audited 503 with nothing written. Pinned in both
    directions: refused-and-unwritten when the policy is broken, and completely
    unaffected when it works.
    """

    class _BrokenPolicy:
        """A host that declares a companion policy it cannot compose."""

        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    def test_the_serializer_really_does_raise_under_this_policy(self) -> None:
        """CONTROL FIRST: without this the arms below could pass vacuously.

        If ``_serialize`` did not raise under the broken policy there would be no
        500-after-commit to prevent, and a 503 from the authorizers would prove
        nothing about the ordering.
        """
        from kiro_crew.platform import PlatformCompositionError

        self._install(self._BrokenPolicy())
        loop = NudgeLoop(id="l1", slot_key="chat-1-123", message="keep going")
        with pytest.raises(PlatformCompositionError):
            autonudge_handlers._serialize(loop)

    @pytest.mark.asyncio
    async def test_the_probe_runs_before_the_critical_invoked_audit(self, audits) -> None:
        """The refusal must precede the ``invoked`` audit, not follow it.

        An ``invoked`` event records an ATTEMPTED mutation. Emitting one and then
        refusing would leave the audit trail claiming a write that never happened --
        the mirror of the bug being fixed, where the write happened and the caller
        was told it had not.
        """
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.get_by_id = lambda _id: SimpleNamespace(message="x")
        svc.update = AsyncMock(return_value=None)
        self._install(self._BrokenPolicy())

        await authz.authorize_and_update_nudge(
            svc=svc, loop_id="loop-1", idle_secs=600, source="dashboard"
        )
        assert not [
            a for a in audits if a.get("outcome") == "invoked"
        ], f"an invoked audit was written for a mutation that was refused: {audits!r}"

    @pytest.mark.asyncio
    async def test_a_working_policy_is_completely_unaffected(self, tmp_path, audits) -> None:
        """PRESERVED: the probe must not turn ordinary requests into refusals."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-5-555", message="keep going")
            loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc, loop_id=armed.id, idle_secs=600, source="dashboard"
            )
            assert status == 200, f"a healthy update was refused: {error}"
            assert loop.idle_secs == 600, "the update did not apply"
            # And the response really can be serialized, which is the property the
            # probe exists to guarantee.
            assert isinstance(autonudge_handlers._serialize(loop)["message"], str)
        finally:
            svc.stop()


class TestANumericStoredMessageIsCoercedNotServedRaw:
    """GPT 5.6 (BLOCKING): ``message: 42`` in the store crashed the goal popover.

    The store is hand-editable JSON and ``NudgeLoop`` is a plain dataclass, so
    ``{"message": 42}`` becomes ``loop.message = 42`` -- ``_load`` repairs the
    numeric timer fields, but nothing coerces ``message``. The REST
    projection then served it untouched, because ``scrub_loop_text`` returned every
    ``int``/``float``/``bool`` early.

    ``AutoNudgePopover.tsx`` reads ``loop?.message || DEFAULT_MSG``, and ``42`` is
    truthy, so the number reached ``message.trim()`` and threw -- the popover died
    rather than showing the row. (``0`` was survivable only by accident: it is
    falsy, so the default template took over.)

    The numeric pass-through is NOT simply wrong, which is why this is a
    field-aware fix rather than a blanket ``str()``: nine of the sixteen fields are
    declared numeric and clients do arithmetic on them, so coercing ``300`` to
    ``"300"`` would break the contract this projection exists to serve. The
    exemption therefore keys on the FIELD, not on the value's type.
    """

    @staticmethod
    def _serialized(**overrides):
        loop = NudgeLoop(
            id="loop-num",
            slot_key="chat-1-123",
            message=overrides.pop("message", "watch the build"),
            **overrides,
        )
        return autonudge_handlers._serialize(loop)

    def test_a_numeric_message_is_served_as_a_string(self) -> None:
        """The bug: a number reached the wire, where the client calls ``.trim()``."""
        out = self._serialized(message=42)
        assert isinstance(out["message"], str), (
            f"a numeric message was served as {type(out['message']).__name__}, which "
            "crashes message.trim() in the popover"
        )
        assert out["message"] == "42", f"the value was not preserved: {out['message']!r}"

    def test_a_numeric_message_survives_the_loader_uncoerced(self, tmp_path) -> None:
        """Establishes the premise rather than assuming it: ``_load`` does not coerce."""
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {"id": "l1", "slot_key": "chat-1-123", "message": 42, "idle_secs": 300}
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            armed = svc.get_by_id("l1")
            assert armed is not None, "the row was refused, so the premise does not hold"
            assert armed.message == 42, (
                f"_load coerced the message to {armed.message!r}; if this ever becomes "
                "the fix, this test is the place to record it"
            )
            out = autonudge_handlers._serialize(armed)
            assert isinstance(out["message"], str), "the projection served a raw number"
        finally:
            svc.stop()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("idle_secs", 300),
            ("max_cycles", 7),
            ("cycle_count", 3),
            ("max_runtime_secs", 900),
            ("active", True),
            ("approval_stalled", False),
            ("last_fire_ts", 1.5),
            ("created_ts", 2.5),
            ("next_due_ts", 3.5),
        ],
    )
    def test_every_declared_numeric_field_stays_numeric(self, field, value) -> None:
        """PRESERVED: the whole reason the early return existed.

        All nine declared numeric fields, named individually -- a fix that coerced
        any one of them to a string would break arithmetic and comparison on the
        client, which is what the docstring's ``300`` -> ``"300"`` warning is about.
        """
        out = self._serialized(**{field: value})
        assert out[field] == value and isinstance(out[field], type(value)), (
            f"{field} was coerced from {type(value).__name__} to "
            f"{type(out[field]).__name__}: {out[field]!r}"
        )

    def test_the_numeric_exemption_is_derived_and_finds_the_declared_fields(self) -> None:
        """The derivation must actually resolve, because empty FAILS OPEN into coercion.

        Replaces a set/dataclass drift test: there is now one definition, so drift is
        impossible. What IS possible is a derivation that silently resolves to nothing
        -- annotations are strings under ``from __future__ import annotations``, so a
        probe that stopped matching would exempt no field and coerce every number a
        client does arithmetic on.
        """
        derived = _an._numeric_loop_fields()
        assert derived, "the derivation resolved to nothing -- every number would coerce"
        assert {
            "idle_secs",
            "max_cycles",
            "cycle_count",
            "active",
        } <= derived, f"the derivation missed a known numeric field: {sorted(derived)}"
        assert (
            "message" not in derived and "id" not in derived
        ), f"a text field slipped into the exemption: {sorted(derived)}"

    def test_a_non_numeric_value_in_a_numeric_field_is_still_coerced(self) -> None:
        """A numeric FIELD does not license a non-numeric value onto the wire."""
        out = self._serialized(idle_secs="AKIAIOSFODNN7EXAMPLE")
        assert isinstance(out["idle_secs"], str)
        assert "AKIAIOSFODNN7EXAMPLE" not in out["idle_secs"], "the scrub was skipped"

    def test_none_is_not_stringified(self) -> None:
        """PRESERVED: ``None`` must not become the literal string ``"None"``.

        A blanket ``str()`` coercion would do exactly that -- corrupting an absent
        value into a four-character message -- so ``None`` keeps passing through.
        The popover's ``|| DEFAULT_MSG`` already handles it, since ``None`` is falsy.
        """
        out = self._serialized(message=None)
        assert out["message"] is None, f"None was stringified to {out['message']!r}"

    def test_a_string_message_still_scrubs(self) -> None:
        """PRESERVED: the ordinary path is untouched."""
        out = self._serialized(message="deploy with AKIAIOSFODNN7EXAMPLE now")
        assert isinstance(out["message"], str)
        assert "AKIAIOSFODNN7EXAMPLE" not in out["message"], "the scrub was lost"

    def test_an_empty_string_message_is_returned_as_is(self) -> None:
        """PRESERVED: the empty-string short-circuit the compare path depends on."""
        assert _an.scrub_loop_text("", field="message") == ""

    def test_the_broadcast_path_coerces_too(self) -> None:
        """The websocket sink shares the rule, so it must share the coercion.

        ``slack/gateway.py`` scrubs ``loop.message`` through the same function; if it
        passed no field the number would reach the browser by the other route and the
        fix would have moved the crash rather than closed it.
        """
        assert isinstance(_an.scrub_loop_text(42, field="message"), str)


class TestTheRedactedProjectionCannotOverwriteTheStoredMessage:
    """GPT 5.6 (BLOCKING): the popover's own Save destroyed the operator's prompt.

    The mechanism needs both halves of an asymmetry to line up:

    * ``svc.add`` stores a message WITHOUT the PATCH path's redaction pair -- the
      MCP arming tools and any direct service caller go in that way -- so the stored
      text keeps whatever it was armed with.
    * ``_serialize`` projects that field through ``scrub_loop_text`` ->
      ``redact_via_context``, which is a DIFFERENT and wider rule than the pair at
      ``authorize_and_update_nudge``'s message arm (a composed host contributes its
      own patterns).

    So the popover loads a projection that differs from the stored value, and its
    Save PATCHes that projection straight back. Nothing errored, nothing warned,
    and the operator's instruction was replaced by ``[REDACTED: ...]`` permanently.

    The remedy is the lane's own: a submitted message equal to the current scrubbed
    projection is treated as UNCHANGED. It is compared with the very same
    ``scrub_loop_text`` the projection uses -- not a second hand-rolled redaction --
    so the two cannot drift apart.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        """Capture SEL events rather than writing them (mirrors the authz suite)."""
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @pytest.fixture()
    def svc(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        yield service
        service.stop()

    async def _armed(self, svc):
        """Arm through ``svc.add``, the path that does NOT redact on the way in."""
        original = f"deploy using key {self.SECRET} and report back"
        loop = await svc.add(slot_key="chat-1-123", message=original, idle_secs=300)
        assert loop.message == original, "svc.add unexpectedly redacted on the way in"
        return original, loop.id

    @pytest.mark.asyncio
    async def test_saving_the_loaded_projection_leaves_the_original_intact(
        self, svc, audits
    ) -> None:
        """The bug: PATCH the exact projection back, as the popover's Save does."""
        original, loop_id = await self._armed(svc)

        projection = autonudge_handlers._serialize(svc.list_all()[0])
        assert projection["message"] != original, (
            "the projection did not differ from the stored value, so this test would "
            "not exercise the overwrite at all"
        )
        assert self.SECRET not in projection["message"]

        # The popover sends the whole form back: the untouched message field it was
        # served, alongside the field the operator actually changed.
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=projection["message"],
            idle_secs=600,
            source="test",
        )
        assert status == 200, f"the save itself failed: {error}"
        assert loop.idle_secs == 600, "the operator's real edit was lost"
        assert loop.message == original, (
            "the redacted projection overwrote the stored instruction: "
            f"{loop.message!r} replaced {original!r}"
        )

    @pytest.mark.asyncio
    async def test_the_audit_does_not_claim_a_message_change_that_was_dropped(
        self, svc, audits
    ) -> None:
        """The dropped field must be absent from the critical ``invoked`` audit.

        The ``fields`` list is what an auditor reads to learn which fields a caller
        mutated. Recording ``message`` on a save that deliberately applied no message
        would make that record disagree with the store.
        """
        _, loop_id = await self._armed(svc)
        projection = autonudge_handlers._serialize(svc.list_all()[0])

        _, _, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=projection["message"],
            idle_secs=600,
            source="test",
        )
        assert status == 200

        invoked = [c for c in audits if c.get("outcome") == "invoked"]
        assert invoked, "the critical invoked audit stopped being written"
        recorded = invoked[-1]["metadata"]["fields"]
        assert "idle_secs" in recorded, "the field that WAS applied is missing"
        assert (
            "message" not in recorded
        ), f"the audit claims a message change that was dropped: {recorded!r}"

    @pytest.mark.asyncio
    async def test_a_genuinely_different_message_still_replaces_and_is_redacted(
        self, svc, audits
    ) -> None:
        """Preserved: a real edit still lands, and inbound redaction still applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id=loop_id,
            message=f"completely new instruction {self.SECRET}",
            source="test",
        )
        assert status == 200, f"a genuine edit was refused: {error}"
        assert loop.message.startswith("completely new instruction")
        assert self.SECRET not in loop.message, "inbound redaction was lost"

    @pytest.mark.asyncio
    async def test_a_submitted_empty_string_still_clears_as_it_does_today(
        self, svc, audits
    ) -> None:
        """Preserved: '' is not the projection of a non-empty message, so it applies."""
        _, loop_id = await self._armed(svc)

        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=loop_id, message="", source="test"
        )
        assert status == 200, f"the empty-string update was refused: {error}"
        assert loop.message == "", "'' stopped being applied"

    @pytest.mark.asyncio
    async def test_a_message_with_nothing_to_scrub_is_still_updatable(self, svc, audits) -> None:
        """Preserved: when projection == stored, re-saving it is a genuine no-op.

        A benign message projects to itself, so the new predicate treats a re-save as
        unchanged -- which is correct, because applying it would store the identical
        value. Pinned so the predicate cannot be read as breaking benign saves.
        """
        benign = await svc.add(slot_key="chat-2-456", message="just do it")
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id=benign.id, message="just do it", idle_secs=900, source="test"
        )
        assert status == 200, f"a benign re-save was refused: {error}"
        assert loop.message == "just do it"
        assert loop.idle_secs == 900

    @pytest.mark.asyncio
    async def test_an_unknown_loop_still_produces_the_existing_404(self, svc, audits) -> None:
        """Preserved: the pre-read must not invent a second 404 path."""
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc, loop_id="no-such-loop", message="anything", source="test"
        )
        assert status == 404, f"the existing not-found path changed: {status} {error}"
        assert loop is None
        assert error == "loop not found"


class TestSentinelRepairWarningsAttachNoTraceback:
    """GPT 5.6 (BLOCKING): ``exc_info=True`` re-exposed what ``_scrub`` withheld.

    Both sentinel-repair arms interpolate ``_scrub(...)`` precisely because the
    value comes out of a hand-editable store and the log ring is served by
    ``/api/logs``. ``exc_info=True`` then attached the traceback, and a traceback
    ends with the exception's own ``str()`` -- which for the failures these arms
    exist to catch embeds the offending path verbatim (``OSError: [Errno 36] File
    name too long: '<path>'``). So the scrubbed argument was served next to an
    unscrubbed copy of the same value, on the same record.

    Both directions are pinned: no traceback text, AND the warning still fires with
    the scrubbed value while startup proceeds -- the ``# noqa: BLE001`` on each arm
    says a repair failure must never block startup, so turning either warning into
    a raise, a return or a silence would be a regression, not a fix.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _path(self) -> str:
        return f"/tmp/{'d' * 200}{self.SECRET}/STOP"

    @staticmethod
    def _assert_no_traceback(records, secret: str, where: str) -> None:
        """No record on this path may carry traceback text, formatted or not."""
        import logging

        fmt = logging.Formatter("%(message)s")
        for rec in records:
            assert rec.exc_info is None, (
                f"{where}: the record still attaches exc_info, so the traceback "
                f"(and the raw value in its exception text) reaches /api/logs"
            )
            assert rec.exc_text is None, f"{where}: the record carries cached traceback text"
            assert secret not in fmt.format(
                rec
            ), f"{where}: the credential is still in the emitted record: {fmt.format(rec)!r}"

    def test_the_rehome_arm_emits_scrubbed_and_without_a_traceback(
        self, caplog, monkeypatch
    ) -> None:
        """Site 1: the ``could not re-home sentinel`` arm."""
        bad = self._path()

        def _raise(_s):
            raise OSError(f"[Errno 36] File name too long: '{bad}'")

        # normpath runs INSIDE the arm's own try, which is what the real
        # filesystem failure this arm catches also does.
        monkeypatch.setattr(_an.os.path, "normpath", _raise)
        with caplog.at_level("WARNING"):
            # The property that matters here is that it RETURNED rather than raised:
            # this arm's ``# noqa: BLE001`` says a repair failure must never block
            # startup. The exact return value is not asserted, because patching
            # ``normpath`` also perturbs the sensitivity check further down, so
            # pinning it would measure the patch rather than the arm.
            out = _an.repair_sentinel_path(bad)
        assert isinstance(out, str), "the repair arm no longer returns a string"

        mine = [r for r in caplog.records if "could not re-home sentinel" in r.getMessage()]
        assert mine, "the repair warning stopped being emitted at all"
        self._assert_no_traceback(mine, self.SECRET, "re-home arm")

    def test_the_sensitivity_arm_emits_scrubbed_and_without_a_traceback(
        self, caplog, monkeypatch
    ) -> None:
        """Site 2: the ``sensitivity re-check failed`` arm, which also drops the sentinel."""
        bad = self._path()

        def _raise(_p):
            raise OSError(f"[Errno 36] File name too long: '{bad}'")

        monkeypatch.setattr(_an, "is_sensitive_path", _raise)
        with caplog.at_level("WARNING"):
            out = _an.repair_sentinel_path(bad)

        mine = [r for r in caplog.records if "sensitivity re-check failed" in r.getMessage()]
        assert mine, "the sensitivity warning stopped being emitted at all"
        assert out == "", "the fail-closed drop was lost -- an unvalidated sentinel survived"
        self._assert_no_traceback(mine, self.SECRET, "sensitivity arm")

    def test_a_benign_path_is_untouched(self, caplog) -> None:
        """Negative control: neither arm may fire on a path that repairs cleanly."""
        with caplog.at_level("WARNING"):
            out = _an.repair_sentinel_path("/tmp/kc-benign/STOP")
        assert out == "/tmp/kc-benign/STOP"
        assert not [
            r
            for r in caplog.records
            if "could not re-home" in r.getMessage() or "sensitivity re-check" in r.getMessage()
        ], "a repair arm fired on a benign path"


class TestTheMalformedEntryArmTracebackIsMeasured:
    """Measures, rather than assumes, whether the ``:950`` sibling leaks too.

    The malformed-entry arm scrubs ``bad_id`` and ``fields`` for the same reason
    and also passes ``exc_info=True``. Whether that is the identical bypass depends
    on one thing only: can an exception raised inside the per-row ``try`` carry
    store-sourced TEXT in its own message? A traceback lists source lines and frame
    locations, not local values, so the exception's ``str()`` is the whole exposure.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _write(tmp_path, rows) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            pytest.param(42, id="row-is-an-int"),
            pytest.param("AKIAIOSFODNN7EXAMPLE", id="row-is-a-credential-string"),
            pytest.param(["AKIAIOSFODNN7EXAMPLE"], id="row-is-a-list"),
            pytest.param({"AKIAIOSFODNN7EXAMPLE": 1}, id="credential-shaped-KEY"),
            pytest.param(
                {"id": "x", "slot_key": "s", "idle_secs": "AKIAIOSFODNN7EXAMPLE"},
                id="credential-in-a-numeric-field",
            ),
        ],
    )
    async def test_whether_a_malformed_row_puts_its_text_in_the_traceback(
        self, tmp_path, caplog, row
    ) -> None:
        """Records what the arm actually emits, and fails only on a real leak.

        If any of these shapes lands the credential in the emitted record, the
        ``:950`` site is the same defect and must lose its ``exc_info`` too.
        """
        import logging

        self._write(tmp_path, [row])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()  # must never raise -- the arm's whole job is to skip
            fmt = logging.Formatter("%(message)s")
            leaked = [
                r
                for r in caplog.records
                if self.SECRET in (fmt.format(r) + (r.exc_text or "") + str(r.exc_info or ""))
            ]
            assert not leaked, (
                "the malformed-entry arm leaked the credential: "
                f"{[fmt.format(r) for r in leaked]!r}"
            )
        finally:
            svc.stop()


class TestEveryPersistedRowBodyUsesTheActiveCredentialPolicy:
    """First Principles (CONCERNS): the root cause was closed for user rows only.

    "The bare redactor pair skips a composed host's own patterns" was fixed at the user-row
    persisters, leaving the siblings that write the OTHER halves -- assistant/system rows at
    the history write boundary, the Slack nudge reply, and the cron run/result rows -- still
    on the bare pair. Model output carries credentials too, so a companion-only shape landed
    verbatim in the transcript. Every persisted row BODY now routes through the shim.
    """

    TOKEN = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

    class _CompanionPolicy:
        token = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

        def redact(self, text: str) -> str:
            from kiro_crew import security

            return security.redact(text).replace(self.token, "[REDACTED: companion]")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    def test_the_bare_pair_alone_cannot_see_the_companion_shape(self) -> None:
        """POSITIVE CONTROL: without the shim the token survives, so the test can fail."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        bare, _ = redact_exfiltration_urls(f"key={self.TOKEN}")
        bare, _ = redact_credentials(bare)
        assert self.TOKEN in bare, (
            "precondition: the bare pair already redacts this shape, so neither arm of this "
            "test could distinguish the composed host from the baseline"
        )

    def test_a_scrubbed_row_is_rewritten_while_a_clean_row_is_not(self, tmp_path) -> None:
        """Design (F2): the write-time scrub is a one-way door that has to say so.

        The nudge wire already carries ``message_redacted``; a conversation row that the
        scrub rewrote carried no equivalent, so a reader could not tell a redacted row from
        one the model wrote verbatim. Same contract, mirrored: a boolean set only on change.
        """
        import json as _json

        from kiro_crew.history import ConversationLog

        # Vendor-shaped so the HOST redactor masks it with no companion policy installed;
        # a vendor-less placeholder is not masked at all and would not exercise the flag.
        key = "AKIAIOSFODNN7EXAMPLE"
        log = ConversationLog(base_dir=tmp_path)
        log.append("slot-flagged", "assistant", f"deploy with {key}")
        log.append("slot-flagged", "assistant", "nothing sensitive here")
        rows = [
            _json.loads(line)
            for line in (tmp_path / "slot-flagged.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and _json.loads(line).get("role") == "assistant"
        ]
        assert len(rows) == 2, f"precondition: expected two assistant rows, got {len(rows)}"
        altered, untouched = rows[0], rows[1]
        assert (
            key not in altered["content"]
        ), "precondition: the scrub did not rewrite this row, so nothing was under test"
        assert untouched["content"] == "nothing sensitive here", (
            "a row with nothing credential-shaped in it was rewritten anyway, so the "
            "boundary is not shape-based"
        )

    def test_a_non_user_history_row_is_scrubbed_by_the_composed_policy(self) -> None:
        from kiro_crew.history import _redact_at_write_boundary

        self._install(self._CompanionPolicy())
        try:
            stored = _redact_at_write_boundary("assistant", f"here it is: {self.TOKEN}")
        finally:
            from kiro_crew.platform import set_context

            set_context(None)
        assert self.TOKEN not in stored, (
            "an assistant row kept a companion-only credential verbatim, so the host's own "
            "patterns are still skipped for every non-user role"
        )
        assert "[REDACTED: companion]" in stored


class TestTheAddressingGuardUsesTheActiveCredentialPolicy:
    """GPT 5.6 (BLOCKING): ``_load``'s addressing guard used the bare redactor.

    ``if redact(got) != got`` is the credential-shape DETECTOR for the two fields
    ``_serialize`` deliberately serves unscrubbed. Asking the bare
    ``security.redact`` means a host that loads a companion credential policy --
    whose whole purpose is extra, host-specific patterns -- had those patterns
    skipped by the detector. A companion-only credential parked in an
    agent-writable loop ``id`` was therefore judged clean, the loop armed, and the
    value reached every dashboard client verbatim through ``GET /api/autonudge``
    and the transcript row's ``meta.nudge.loop_id``.

    Both directions are pinned, because a guard that refuses EVERYTHING is not a
    fix: the companion shape must be refused AND an ordinary id must still arm.
    """

    COMPANION_TOKEN = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

    class _CompanionPolicy:
        """Core redaction plus ONE host-specific pattern, as a companion supplies."""

        token = "COMPANION-SSO-COOKIE-9f3a2b4c7d1e"

        def redact(self, text: str) -> str:
            from kiro_crew import security

            return security.redact(text).replace(self.token, "[REDACTED: companion]")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    @staticmethod
    def _write(tmp_path, **over) -> None:
        row = {
            "id": "abc123",
            "slot_key": "chat-1-2",
            "message": "keep going",
            "idle_secs": 300,
        }
        row.update(over)
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    def test_core_redaction_leaves_the_companion_token_alone(self) -> None:
        """Control FIRST: without this the arms below could pass vacuously.

        If core redaction already stripped this shape, the bare detector and the
        policy-routed one would agree and nothing below could discriminate.
        """
        from kiro_crew.security import redact

        assert redact(self.COMPANION_TOKEN) == self.COMPANION_TOKEN, (
            "core redaction now strips this shape, so it can no longer distinguish "
            "the bare detector from the platform-routed one -- pick another token"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_companion_shaped_addressing_field_is_refused(
        self, tmp_path, caplog, field
    ) -> None:
        """The lane's path: agent-writable store row -> _load -> dashboard API."""
        self._install(self._CompanionPolicy())
        self._write(tmp_path, **{field: f"loop-{self.COMPANION_TOKEN}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()

            assert svc._loops == {}, (
                f"a companion-shaped {field} armed anyway -- the guard asked the bare "
                "redactor, so the active policy's patterns never ran"
            )
            assert "refusing loop" in caplog.text, "the refusal was silent"

            # and it must not be reachable through the REST projection either
            for loop in svc.list_all():
                assert self.COMPANION_TOKEN not in str(autonudge_handlers._serialize(loop))
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_ordinary_id_still_arms(self, tmp_path) -> None:
        """The other direction: a guard that refuses everything is not a fix."""
        self._install(self._CompanionPolicy())
        self._write(tmp_path, id="chat-1281-1785676802")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "chat-1281-1785676802" in svc._loops, "an ordinary id was refused"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_mis_composed_host_refuses_every_row_without_losing_one(
        self, tmp_path, caplog
    ) -> None:
        """``redact_via_context`` is FAIL-CLOSED, so establish what that does HERE.

        It re-raises ``PlatformCompositionError`` -- a host declaring a companion
        policy it could not compose. That exception is ``RuntimeError``-derived, so
        the per-row ``except Exception`` would swallow it once per row and the
        malformed-entry arm would log N confusing "skipping malformed loop entry"
        warnings; and the same call inside THAT arm would escape ``_load``
        altogether, escaping the unguarded ``run_in_executor`` in ``start()``.

        So the loader resolves the policy ONCE, up front. This pins the resulting
        contract: no loop arms (the security decision fails closed) and ``start()``
        does NOT raise.

        Non-destructiveness is pinned at the WRITE boundary now rather than by
        carrying rows through memory: ``_load_refused`` makes ``_write_state``
        refuse, so the file survives even though the payload is empty. That is
        cron's answer to the same state, and ``TestARefusalDoesNotDestroyTheStore``
        proves the file is byte-identical afterwards.
        """
        from kiro_crew.platform import PlatformCompositionError

        class _MisComposed:
            def redact(self, text: str) -> str:
                raise PlatformCompositionError("companion policy unreadable")

        self._install(_MisComposed())
        self._write(tmp_path, id="chat-1281-1785676802")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("ERROR"):
                await svc.start()  # must NOT raise

            assert svc._loops == {}, "a loop armed without a usable credential policy"
            assert svc._load_refused is True, "the write-refusal flag was not set"
            # The payload IS empty now -- and that is safe only because the write is
            # refused. Asserting both together is the point: either alone would pass
            # while the store was being destroyed.
            assert svc._serialize_state()["loops"] == []
            original = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
            # RAISES rather than returning quietly, so a mutation caller's existing
            # rollback handler fires instead of it confirming an undurable loop.
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (tmp_path / "autonudge.json").read_text(
                encoding="utf-8"
            ) == original, "the store was overwritten while unvettable"
        finally:
            svc.stop()


class TestCredentialShapedAddressingFieldsAreRefused:
    """GPT 5.6 (BLOCKING): the REST serializer exempts the addressing fields.

    ``id`` and ``slot_key`` pass through ``_serialize`` unscrubbed because the
    client addresses the row by them -- rewriting either leaves a row that renders
    but cannot be acted on. That exemption is only safe if an addressing field can
    never CARRY a credential, and the store is a file an agent writes directly, so
    nothing upstream guaranteed it: a credential placed in ``id`` reached every
    dashboard client verbatim through ``GET /api/autonudge`` and through the
    transcript row's ``meta.nudge.loop_id``.

    ``_load`` now REFUSES such a loop rather than scrubbing it, and refusing is
    also what matches the arm-time contract -- ``authorize_and_add_nudge`` never
    mints an id of this shape, so a store row carrying one did not come from the
    API.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _write(self, tmp_path, **over) -> None:
        row = {
            "id": "abc123",
            "slot_key": "chat-1-2",
            "message": "keep going",
            "idle_secs": 300,
        }
        row.update(over)
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_credential_shaped_addressing_field_is_refused(
        self, tmp_path, caplog, field
    ) -> None:
        """Fails on the unmodified tree, where the loop arms and is then served."""
        self._write(tmp_path, **{field: f"loop-{self.SECRET}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert svc._loops == {}, "a credential-shaped addressing field was armed anyway"
            assert "refusing loop" in caplog.text, "the refusal was silent"
            assert field in caplog.text, "the warning does not name the offending field"
            assert self.SECRET not in caplog.text, "the refusal echoed the credential"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_row_is_not_dropped_by_the_next_write(self, tmp_path, caplog) -> None:
        """GPT 5.6 (BLOCKING): the refused row was deleted by the next write.

        Declining the row kept it out of ``_loops``, so the next wholesale write --
        any ``add``/``update``/stop -- serialized the store WITHOUT it and the
        operator's row was permanently gone. The warning named a field to fix in a
        file that did not contain it.

        The fix arms ``_load_refused``, so every persist raises and the file on disk
        is left untouched until the entry is repaired and the process restarted. That
        is the same mechanism the whole-store arm already used, and it holds no row in
        memory, so the rollback gap that killed the earlier hold stays unreachable.

        Discriminates the two refusals by the WARNING, not by ``_loops``: both empty it
        now, so "the clean row still armed" cannot separate them. Only the row-level
        arm names the offending loop and field, so asserting that text proves the policy
        composed and one row was declined -- without it, a leaked broken policy would
        satisfy the raise for the wrong reason.
        """
        rows = [
            {"id": "abc123", "slot_key": "chat-1-2", "message": "keep going", "idle_secs": 300},
            {"id": self.SECRET, "slot_key": "chat-3-3", "message": "x", "idle_secs": 300},
        ]
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                svc._load()
            assert "refusing loop" in caplog.text, (
                "no row-level refusal was logged, so this is the WHOLE-STORE arm and the "
                "raise below would prove nothing about a single declined row"
            )
            svc._write_state(svc._serialize_state())
            after = (tmp_path / "autonudge.quarantine.json").read_text(encoding="utf-8")
            assert _held_aside_rows(tmp_path), "the declined row was dropped"
            assert self.SECRET in after, (
                "the refused row is gone from disk -- the operator was told to fix a "
                "field in an entry the next write had already deleted"
            )
        finally:
            svc.stop()

    @pytest.mark.parametrize(
        "payload",
        [
            "{not json at all",
            '["a", "list", "not", "an", "object"]',
            '{"quarantined": ["not-an-object"]}',
        ],
        ids=["unparseable", "wrong-shape", "non-object-member"],
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_sidecar_is_not_unlinked_by_the_next_write(
        self, tmp_path, caplog, payload
    ) -> None:
        """GPT 5.6 (BLOCKING): a corrupt sidecar was deleted by the next write.

        ``_read_quarantine_sidecar`` answered unparseable or wrongly-shaped content with
        ``[]``, which is indistinguishable from "nothing is held aside". The loader
        therefore armed normally, and the next successful write called
        ``_drop_quarantine_sidecar`` and UNLINKED the only surviving copy of rows the
        loader itself had refused -- so the operator lost exactly the data the warning
        told them to repair. A downgrade that writes an older sidecar format reaches this.

        The fix sets ``_load_refused`` on either failure, so every persist raises
        ``AutoNudgeStoreUnvetted`` and the file survives until it is repaired and the
        process restarted.

        Asserts the BYTES are still on disk rather than only that the call raised: a
        raise that happened after the unlink would satisfy a raises-only assertion while
        the data was already gone. Both arms are parametrized because they are separate
        ``return []`` sites, and covering one would leave the other free to regress.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(payload, encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                svc._load()
            assert (
                svc._load_refused is True
            ), "an unreadable sidecar left writes ENABLED, so the next one unlinks it"
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert not sidecar.exists(), (
                "the unreadable sidecar is still in place, so a restart hits the same "
                "refusal and the outage needs a hand repair"
            )
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == payload
            ), "the sidecar bytes were lost, so held-aside rows are unrecoverable"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_cycle_is_claimed_durably_before_delivery(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): fire-then-record repeats a delivered action after restart.

        The load-time guard only covers a store already refused at startup. When the store
        goes unwritable AFTER arming, the timer still fired, ``_on_fire`` delivered, and the
        bookkeeping write only LOGGED its failure -- so the cycle was never recorded and the
        restart delivered the same action again. The claim is now durable and precedes
        delivery, so an unwritable store refuses to deliver instead.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        fired: list[str] = []

        async def _on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            # The store becomes unwritable AFTER arming -- the case the load guard misses.
            def _refuse(_payload):
                raise OSError("store went read-only after arming")

            svc._write_state = _refuse  # type: ignore[method-assign]

            # The current path raises out of the re-arm persist AFTER delivering; the harm
            # under test is what was delivered and counted, not how the write failed.
            with suppress(Exception):
                await svc._run_fire_cycle(loop)

            assert (
                fired == []
            ), f"delivered with the cycle unrecordable, so a restart repeats it: {fired!r}"
            assert (
                loop.cycle_count == before
            ), f"a cycle was consumed with nothing delivered: {before} -> {loop.cycle_count}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_delivered_cycle_number_is_not_shifted_by_the_claim(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the pre-claim shifted every delivered cycle number.

        The fire callback renders the cycle as ``cycle_count + 1`` -- correct while
        ``cycle_count`` counts DELIVERED cycles. Claiming before delivery made it already
        include this turn, so every nudge announced a number one too high. The claim must be
        durable on DISK before the turn goes out while the in-memory count still reads the
        delivered total, which is the contract every consumer was written against.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        seen: list[tuple[int, int]] = []

        async def _on_fire(loop):
            on_disk = json.loads(store.read_text(encoding="utf-8"))["loops"][0].get(
                "inflight_cycle"
            )
            seen.append((loop.cycle_count, on_disk))
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            await svc._run_fire_cycle(loop)

            assert len(seen) == 1, "the turn was not delivered"
            in_memory, on_disk = seen[0]
            assert in_memory == before, (
                f"the callback saw {in_memory} instead of {before}, so it announced "
                f"cycle {in_memory + 1} for the {before + 1}th turn"
            )
            assert on_disk == before + 1, (
                f"the claim was not durable during delivery (disk={on_disk}), so a crash "
                "mid-turn would repeat it"
            )
            assert loop.cycle_count == before + 1, "the delivered cycle was not counted"
        finally:
            svc.stop()

    def test_a_readable_replacement_is_not_moved_aside_by_a_stale_loader(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the move-aside renamed on a detection made earlier.

        Detection happens during ``_load``, outside the sidecar lock, and the rename then
        took whatever sat at the canonical path. A peer that restarts and publishes a
        readable replacement in that window had it renamed away by the stale loader -- and
        the sidecar is those rows' only durable copy.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        sidecar = svc._quarantine_path
        try:
            # What the concurrent restart published: readable, with a held row in it.
            sidecar.write_text(
                json.dumps({"version": 1, "quarantined": [{"id": "held", "slot_key": "chat-9-9"}]}),
                encoding="utf-8",
            )
            assert svc._quarantine_rows_on_disk() is not None, "precondition: readable"

            svc._move_aside_unreadable_sidecar()

            moved = list(tmp_path.glob(f"{sidecar.name}.corrupt-*"))
            assert sidecar.exists(), "the readable replacement was renamed away"
            assert (
                svc._quarantine_rows_on_disk() is not None
            ), f"the held rows are no longer readable at the canonical path; moved={moved!r}"
            assert moved == [], f"a stale detection still moved a readable sidecar: {moved!r}"
        finally:
            svc.stop()

    def test_a_genuinely_unreadable_sidecar_is_still_moved_aside(self, tmp_path) -> None:
        """POSITIVE CONTROL for the guard above: it must not become a blanket no-op.

        A revalidation that refused every rename would satisfy the sibling test while
        leaving the service permanently unable to persist.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        sidecar = svc._quarantine_path
        try:
            sidecar.write_text("{ this is not json", encoding="utf-8")
            assert svc._quarantine_rows_on_disk() is None, "precondition: unreadable"

            svc._move_aside_unreadable_sidecar()

            moved = list(tmp_path.glob(f"{sidecar.name}.corrupt-*"))
            assert len(moved) == 1, f"the unreadable sidecar was not preserved aside: {moved!r}"
            assert not sidecar.exists(), "the unreadable sidecar is still blocking the path"
            assert "not json" in moved[0].read_text(encoding="utf-8"), "bytes were not preserved"
        finally:
            svc.stop()

    def test_the_move_aside_does_not_deadlock_when_the_lock_is_already_held(self, tmp_path) -> None:
        """The write path reaches the move-aside with the sidecar lock ALREADY held.

        ``flock`` is per-fd, so a second acquisition inside one process blocks on a lock
        that process already owns. The probe runs OUT of process, so a regression fails
        on the timed join and is still reaped in ``finally`` -- a thread would stay
        blocked holding the lock, descriptor open, past teardown.
        """
        ctx = _fork_probe_context()
        svc = AutoNudgeService(base_dir=tmp_path)
        done = tmp_path / "nested-move-aside-done"
        proc = None
        try:
            svc._quarantine_path.write_text("{ not json either", encoding="utf-8")
            proc = ctx.Process(
                target=_mp_nested_move_aside, args=(str(tmp_path), str(done)), daemon=True
            )
            proc.start()
            proc.join(timeout=10)
            assert not proc.is_alive(), (
                "the move-aside deadlocked and its probe is STILL RUNNING while holding "
                "the sidecar lock"
            )
            assert proc.exitcode == 0, f"the probe process failed: exitcode={proc.exitcode}"
            assert done.exists(), (
                "the move-aside blocked while the sidecar lock was already held by the "
                "same process, so the write path deadlocks instead of preserving the bytes"
            )
        finally:
            if proc is not None:
                _reap(proc)
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_crash_between_the_claim_and_delivery_does_not_spend_the_last_cycle(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING, UPHOLD-FENCED): the claim consumed an undelivered cycle.

        Claiming by INCREMENTING the spent counter makes the durable record say "spent" for the
        whole delivery, which the code itself notes can take minutes. Die in that window and a
        capped loop reloads with its budget gone and its final action never run -- silently, with
        no recovery path. The claim has to be recorded SEPARATELY from the delivered total.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _die_before_delivering(loop):
            # A process death, not a callback failure: BaseException is not caught by the
            # delivery guard, so nothing after the durable claim gets to run.
            raise KeyboardInterrupt("process exited mid-delivery")

        svc._on_fire = _die_before_delivering
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            loop.max_cycles = 1  # one cycle of budget: the case Opus fenced
            await svc._persist_locked()

            with pytest.raises(KeyboardInterrupt):
                await svc._run_fire_cycle(loop)
        finally:
            svc.stop()

        # POSITIVE CONTROL: the claim really was written before the death, so this is not a
        # vacuous pass over a cycle that never started.
        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert (
            row.get("last_fire_ts") or 0.0
        ) > 0.0, "precondition: the claim persist never happened, so nothing was under test"

        # RESTART: a fresh service reads the same store, exactly as the next boot would.
        revived = AutoNudgeService(base_dir=tmp_path)
        try:
            revived._load()
            reloaded = revived._loops[loop.id]
            assert reloaded.cycle_count == 0, (
                f"the restart counts {reloaded.cycle_count} of {reloaded.max_cycles} cycles as "
                "spent although none was ever delivered, so the capped loop's only scheduled "
                "action never runs"
            )
            assert not (
                reloaded.max_cycles and reloaded.cycle_count >= reloaded.max_cycles
            ), "the cap reads as reached on a loop that delivered nothing"
        finally:
            revived.stop()

    @pytest.mark.asyncio
    async def test_a_retained_claim_keeps_the_claimed_timestamp_not_the_stale_one(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): retaining the claim restored its count but not its clock.

        The release first rolls memory back to the PRE-claim pair, then re-applies only the
        COUNT when the release write fails -- and it has already popped the overlay, so a
        later write persists that memory verbatim: the incremented count beside a timestamp
        from before the turn. Nothing between the retain arm and the end of the cycle puts
        the claimed clock back.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before_count = loop.cycle_count
            before_ts = loop.last_fire_ts

            calls = {"n": 0}
            real_persist = svc._persist_locked

            async def _fail_the_release_only():
                calls["n"] += 1
                if calls["n"] == 1:  # the CLAIM itself must still land durably
                    await real_persist()
                    return
                raise OSError("store is unwritable")

            svc._persist_locked = _fail_the_release_only
            svc._arm_timer = lambda lp, delay=None: None

            await svc._run_fire_cycle(loop)

            # POSITIVE CONTROL: the claim really was written, so this is not a vacuous pass.
            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert calls["n"] >= 2, f"the release write was never attempted ({calls['n']} calls)"
            assert (
                row.get("inflight_cycle") == before_count + 1
            ), f"precondition: disk should carry the claim, got {row.get('inflight_cycle')!r}"
            assert (
                row.get("last_fire_ts") or 0.0
            ) > before_ts, "precondition: disk should carry the CLAIMED clock"

            # The claim is RETAINED rather than released: the marker, not an inflated count,
            # is what a restart reads, and `cycle_count` stays at the delivered total.
            assert (
                svc._delivering_claim.get(loop.id, (None, None))[0] == before_count + 1
            ), f"the claim was not retained: {svc._delivering_claim.get(loop.id)!r}"
            assert loop.cycle_count == before_count, (
                f"the undelivered cycle was counted as spent ({loop.cycle_count}), which is the "
                "loss the two-phase claim exists to prevent"
            )
            assert loop.last_fire_ts > before_ts, (
                f"the retained claim kept the stale clock {loop.last_fire_ts} (pre-claim was "
                f"{before_ts}), so the next write persists a claim its own timestamp cannot "
                "account for"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_turn_makes_its_rollback_durable_before_rearming(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a refused cycle could stay durably counted.

        The rollback went out through the DETACHED best-effort persist, whose docstring
        promises only "a fresh full countdown" -- reasoning written for a deadline
        assignment, not for a cycle count. Shutdown cancels that task, so the claim
        survives on disk: the cycle is charged against ``max_cycles`` having delivered
        nothing. Read with no intervening await, which is what a cancelled task leaves.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            await svc._run_fire_cycle(loop)

            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert row.get("inflight_cycle") is None, (
                f"the refused turn left claim marker {row.get('inflight_cycle')!r} on disk, so a "
                "restart re-runs a cycle the release had already settled"
            )
            assert row["cycle_count"] == before, (
                f"the refused turn left {row['cycle_count']} durably counted instead of {before}, "
                "so a shutdown here spends a cycle that never delivered"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unpersistable_rollback_keeps_the_claim_and_stops_retrying(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING), second half: on failure, retain the claim and stop.

        If the store will not take the rollback, re-arming spends the remaining budget one
        refused turn at a time while every write fails the same way. Keeping the claim
        durable is the safe direction -- a spent cycle, never a repeated action.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        armed: list[float] = []

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            calls = {"n": 0}
            real_persist = svc._persist_locked

            async def _fail_after_the_claim():
                calls["n"] += 1
                if calls["n"] == 1:  # the claim itself must still land
                    await real_persist()
                    return
                raise OSError("store is unwritable")

            svc._persist_locked = _fail_after_the_claim
            svc._arm_timer = lambda lp, delay=None: armed.append(delay or 0.0)

            await svc._run_fire_cycle(loop)

            assert armed == [], f"re-armed {armed!r} against a store that refuses the rollback"
            assert svc._delivering_claim.get(loop.id, (None, None))[0] == before + 1, (
                f"the claim was dropped ({svc._delivering_claim.get(loop.id)!r}) while the disk "
                "marker could not be cleared, so the turn is set up to repeat"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_write_during_delivery_keeps_claim_count_and_timestamp_together(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): the claim overlay carried the count but not its timestamp.

        The claimed ``last_fire_ts`` is taken at claim time and then discarded from memory for
        the delivery window, so a write landing mid-delivery persisted the claimed
        ``cycle_count`` beside the PRE-claim timestamp. A restart then reads a loop that has
        fired more cycles than its own clock accounts for.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        seen: list[tuple[int, float]] = []

        async def _on_fire(loop):
            await svc._persist_locked()
            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            seen.append((row.get("inflight_cycle"), row.get("last_fire_ts") or 0.0))
            return True

        svc._on_fire = _on_fire
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before_count = loop.cycle_count
            before_ts = loop.last_fire_ts

            await svc._run_fire_cycle(loop)

            count, ts = seen[0]
            assert count == before_count + 1, f"claimed count not persisted: {count}"
            assert ts > before_ts, (
                f"the claimed count {count} was persisted beside the pre-claim timestamp "
                f"{ts} (claim was after {before_ts}), so the two disagree on disk"
            )
        finally:
            svc.stop()

    def test_a_staged_monitor_snapshot_keeps_a_preserved_malformed_row(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a staged monitor write deleted rows the load preserved.

        ``_serialize_state`` carries ``_unparsed_rows``, but the monitor paths build their
        OWN whole-store replacement payload from ``_loops`` alone. A malformed row plus a
        structured-monitor transition therefore replaced the store with a snapshot that
        omitted the row -- permanent loss, and the operator was told it was held for repair.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "good",
                            "slot_key": "chat-1-1",
                            "message": "go",
                            "idle_secs": 300,
                            "goal_token": "0123456789abcdef0123456789abcdef",
                        },
                        {"id": "broken", "slot_key": "chat-2-2", "idle_secs": "not-an-int"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            live = svc._loops["good"]
            assert svc._unparsed_rows, "precondition: the malformed row was preserved"

            payload = svc._monitor_snapshot_with_replacement(live, live)

            ids = [r.get("id") for r in payload["loops"] if isinstance(r, dict)]
            assert (
                "broken" in ids
            ), f"the staged monitor snapshot dropped the preserved row; payload: {ids!r}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_removal_write_keeps_a_preserved_malformed_row_on_disk(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a whole-store write must not delete a row held for repair.

        The staged-snapshot sibling above pins the monitor builder. This pins the OTHER
        whole-store writer -- the removal path, which persists after taking a row out and
        is the one a failed deferred-trust replacement unwinds through. A payload built
        from ``_loops`` alone omits an unparseable row, and the write is a replace: the
        row is gone from disk with nothing left to re-read it from.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "good",
                            "slot_key": "chat-1-1",
                            "message": "go",
                            "idle_secs": 300,
                            "goal_token": "0123456789abcdef0123456789abcdef",
                        },
                        {"id": "broken", "slot_key": "chat-2-2", "idle_secs": "not-an-int"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._unparsed_rows, "precondition: the malformed row was preserved"

            await svc.remove("good")

            written = json.loads(store.read_text(encoding="utf-8"))
            ids = [r.get("id") for r in written["loops"] if isinstance(r, dict)]
            assert "broken" in ids, (
                "the removal write deleted the row held for repair; " f"store now holds: {ids!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_concurrent_write_during_delivery_keeps_the_durable_claim(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a mid-delivery write erased the durable cycle claim.

        The claim is persisted before the turn goes out, then rolled back IN MEMORY so the
        callback still renders the delivered number. Any other writer snapshotting during
        that window -- a ``monitor_update`` arriving on the nudge itself -- persisted the
        rolled-back count, so the restart repeated an action already delivered.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        during: list[int] = []

        async def _on_fire(loop):
            # Exactly what a concurrent update does: snapshot and write, mid-delivery. Read
            # back INSIDE the window -- a crash here is what strands the erased claim.
            await svc._persist_locked()
            during.append(
                json.loads(store.read_text(encoding="utf-8"))["loops"][0].get("inflight_cycle")
            )
            return True

        svc._on_fire = _on_fire
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            await svc._run_fire_cycle(loop)

            assert during == [before + 1], (
                f"a write during delivery persisted {during!r} instead of {[before + 1]!r}, "
                "so a crash in that window re-delivers the turn that already went out"
            )
        finally:
            svc.stop()

    def test_a_malformed_sibling_survives_a_legacy_token_migration(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the migration's own rewrite deleted rows it could not parse.

        ``_load`` skips a malformed entry and warns, which is the documented contract -- but
        skipping it leaves it out of ``_loops``, and the disk payload is built from ``_loops``
        alone. So ANY cause that flags the store dirty makes the next write delete it. The
        legacy-token migration is such a cause, so a store that merely needed a token minted
        silently lost the neighbour the operator was told to repair.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        # No goal_token: triggers the migration, which flags the store dirty.
                        {
                            "id": "legacy",
                            "slot_key": "chat-1-1",
                            "message": "go",
                            "idle_secs": 300,
                        },
                        {"id": "broken", "slot_key": "chat-2-2", "idle_secs": "not-an-int"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert "broken" not in svc._loops, "precondition: the row is unparseable"
            assert svc._store_dirty is True, "precondition: the migration flags a rewrite"

            svc._write_state(svc._serialize_state())

            on_disk = json.loads(store.read_text(encoding="utf-8"))["loops"]
            ids = [r.get("id") for r in on_disk if isinstance(r, dict)]
            assert (
                "broken" in ids
            ), f"the malformed sibling was deleted by the migration's write; on disk: {ids!r}"
            assert "legacy" in ids, "the migrated row must still be written"
            survivor = next(r for r in on_disk if r.get("id") == "broken")
            assert (
                survivor["idle_secs"] == "not-an-int"
            ), "the row must survive VERBATIM so the operator can repair it, not normalized"
        finally:
            svc.stop()

    def test_a_reload_does_not_carry_sidecar_ownership_into_the_next_pass(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): stale ownership let a reload delete a PEER's held row.

        ``_sidecar_seen`` is the licence to remove: compaction drops a row only if THIS
        instance enumerated it. ``_load`` reset ``_quarantined`` but never the seen set, so a
        key survived a pass in which the row did not -- and a peer that later re-wrote that
        row had it deleted by a process which did not hold it.
        """
        sidecar = tmp_path / "autonudge.quarantine.json"
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        row = {"id": "held", "slot_key": "chat-9-9"}
        sidecar.write_text(json.dumps({"quarantined": [row]}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._sidecar_seen, "precondition: the first load must enumerate the row"

            # The row is repaired away, so the next load holds nothing.
            sidecar.write_text(json.dumps({"quarantined": []}), encoding="utf-8")
            svc._load()
            assert svc._quarantined == [], "precondition: nothing is held after the reload"

            # A PEER re-quarantines the same row, after our load enumerated nothing.
            sidecar.write_text(json.dumps({"quarantined": [row]}), encoding="utf-8")
            svc._compact_quarantine_sidecar()

            assert sidecar.exists(), (
                "compaction unlinked the sidecar on a key this pass never enumerated, so "
                "the peer's only durable copy is gone"
            )
            surviving = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            assert [r.get("id") for r in surviving] == [
                "held"
            ], f"the peer's row was dropped on stale ownership; on disk: {surviving!r}"
        finally:
            svc.stop()

    def test_a_legacy_empty_token_re_mint_is_persisted(self, tmp_path) -> None:
        """Opus 4.8: the empty-token re-mint never reached disk.

        Only the non-empty branch flagged the store dirty, so a legacy row got a fresh token
        in memory that ``start()`` never wrote -- re-minting again on every load, and handing
        each restart a different baseline than the one a client was last served.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "ship it",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._loops["lp-1"].goal_token, "precondition: a token was minted"
            assert svc._store_dirty is True, (
                "the re-mint was not flagged for persistence, so start() drops it and the "
                "row is re-minted on every load"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_removal_refuses_rather_than_falsely_succeeding_when_unvetted(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): delete reported success while storage was refused.

        An unreadable store sets ``_load_refused`` and leaves the live map EMPTY, so every
        loop looks absent and the removal took the absent-loop early return -- a no-op the
        handler answered 200 to. The durable row is untouched, so the monitor re-arms on the
        next start and the operator was told it was gone.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            # The state the unreadable store produces: refused, and nothing in the map.
            svc._load_refused = True
            assert svc._loops == {}, "precondition: a refused load arms no loops"

            with pytest.raises(AutoNudgeStoreUnvetted):
                await svc.remove("lp-1")
        finally:
            svc.stop()

    def test_a_persisted_token_that_is_not_generator_shaped_is_re_minted(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING, security): the fingerprint routed around the egress scrub.

        ``goal_token`` is served verbatim as ``message_fingerprint``, and the load path only
        minted one when the field was EMPTY -- so a credential-shaped value hand-written or
        migrated into the store reached every REST and websocket client untouched, past the
        scrub this change exists to add. ADDRESSING_FIELDS are exempt, and this field was
        effectively a third one nobody declared.
        """
        planted = "AKIAIOSFODNN7EXAMPLE"
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "ship it",
                            "idle_secs": 300,
                            "goal_token": planted,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loop = svc._loops["lp-1"]
            assert loop.goal_token != planted, (
                "a non-generated token survived the load, so it is served verbatim as "
                "message_fingerprint and bypasses the scrub entirely"
            )
            assert re.fullmatch(
                r"[0-9a-f]{32}", loop.goal_token
            ), f"re-minted token is not generator-shaped: {loop.goal_token!r}"
        finally:
            svc.stop()

    def test_a_valid_token_is_rotated_on_every_load(self, tmp_path) -> None:
        """INVERTED: a valid token surviving the load unchanged is the defect, not the rule.

        GPT 5.6 blocked on that: while the process is down a human can hand-edit the goal
        in the store without touching its token, so a fingerprint issued BEFORE the restart
        still authorised overwriting the edit. The token is an authorisation, and nothing
        this service can read tells it the file was untouched, so it stops honouring one it
        did not issue this load. The cost is a 409 on the first baselined save after a
        restart, which is a refetch, not a lost write.

        Rotated IN MEMORY only: the comparison reads ``loop.goal_token``, so forcing a
        rewrite per boot would buy nothing and break the clean-load-no-rewrite invariant.
        """
        good = "0123456789abcdef0123456789abcdef"
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "ship it",
                            "idle_secs": 300,
                            "goal_token": good,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            token = svc._loops["lp-1"].goal_token
            assert token != good, (
                "the pre-restart token survived, so a fingerprint issued before this load "
                "still authorises a write against a goal that may have been hand-edited"
            )
            assert _an._is_generator_shaped_token(token), f"rotated to a bad shape: {token!r}"
            assert (
                svc._store_dirty is False
            ), "a clean load must not be rewritten just to persist the rotation"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_pre_restart_token_cannot_overwrite_a_hand_edited_goal(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the restart + hand-edit interleaving, end to end.

        A client reads the goal and holds its token. The service stops. A human edits the
        goal in the store file and leaves the token alone -- nothing else could, since the
        token is opaque. On restart the client's stale token must not authorise the
        write, or the human's edit is silently replaced.
        """
        store = tmp_path / "autonudge.json"
        issued = "0123456789abcdef0123456789abcdef"
        store.write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "lp-1",
                            "slot_key": "chat-1-1",
                            "message": "the goal the client read",
                            "idle_secs": 300,
                            "goal_token": issued,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        before = AutoNudgeService(base_dir=tmp_path)
        try:
            before._load()
        finally:
            before.stop()

        # The HUMAN edit, with the token deliberately left as it was.
        raw = json.loads(store.read_text(encoding="utf-8"))
        raw["loops"][0]["message"] = "the goal a human typed while we were down"
        raw["loops"][0]["goal_token"] = issued
        store.write_text(json.dumps(raw), encoding="utf-8")

        after = AutoNudgeService(base_dir=tmp_path)
        try:
            after._load()
            with pytest.raises(AutoNudgeStaleBaseline):
                await after.update(
                    "lp-1", message="what the client had typed", expect_fingerprint=issued
                )
            assert after._loops["lp-1"].message == (
                "the goal a human typed while we were down"
            ), "the hand-edited goal was overwritten by a pre-restart authorisation"
        finally:
            after.stop()

    def test_a_populated_sidecar_alone_does_not_mark_the_main_store_dirty(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING, fenced): a sidecar row triggered a destructive rewrite.

        Holding quarantined rows says nothing about the main store, so flagging it dirty
        bought a rewrite nobody asked for. The row that MOVED to quarantine this load flags
        the store itself, which is the only case that genuinely needs one.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [{"id": "held", "slot_key": "chat-9-9"}]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "precondition: the sidecar row must have been read"
            assert svc._store_dirty is False, (
                "a populated sidecar flagged the main store dirty, so startup rewrites a "
                "store the user never changed"
            )
        finally:
            svc.stop()

    def test_an_unparseable_row_survives_startup_when_a_sidecar_row_exists(self, tmp_path) -> None:
        """The harm the flag caused: the rewrite deletes what the parser could not read.

        A row that fails construction is skipped WITHOUT being quarantined, and the payload
        is rebuilt from the live map, so any flush writes that row out of existence -- no
        user action, no copy anywhere. Only the store's own bytes hold it.
        """
        store = tmp_path / "autonudge.json"
        unparseable = {"id": "hand-edited", "slot_key": "chat-1-1", "idle_secs": "not-a-number"}
        store.write_text(json.dumps({"version": 1, "loops": [unparseable]}), encoding="utf-8")
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [{"id": "held", "slot_key": "chat-9-9"}]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert (
                "hand-edited" not in svc._loops
            ), "precondition: this row must fail construction, or the test proves nothing"
            assert svc._quarantined, "precondition: the sidecar row must have been read"
            # Exactly what start() does with the flag after loading.
            if svc._store_dirty:
                svc._write_state(svc._serialize_state())

            surviving = json.loads(store.read_text(encoding="utf-8"))["loops"]
            assert [row.get("id") for row in surviving] == [
                "hand-edited"
            ], f"startup deleted the row it could not parse; store now holds {surviving!r}"
        finally:
            svc.stop()

    def test_a_peers_row_survives_compaction_by_a_stale_instance(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING, fenced): compaction deleted rows this process never saw.

        Two instances share a data home. One quarantines a row; the other, whose memory
        predates it, reaches compaction with an empty local set and unlinked the file --
        taking the peer's only durable copy.

        The cross-process lock does NOT cover this: it serializes writers, so the peer's row
        is already committed and merely absent from this instance's stale memory. The licence
        to remove is having ENUMERATED a row at load and not holding it.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        sidecar = tmp_path / "autonudge.quarantine.json"
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            # This instance loaded before the peer existed: nothing held, nothing enumerated.
            assert svc._quarantined == []
            assert svc._sidecar_seen == set()
            peer_row = {
                "id": "peer-held",
                "slot_key": "chat-9-9",
                "message": "AKIAIOSFODNN7EXAMPLE",
                "idle_secs": 300,
            }
            sidecar.write_text(json.dumps({"quarantined": [peer_row]}), encoding="utf-8")

            svc._compact_quarantine_sidecar()

            assert sidecar.exists(), (
                "the sidecar was unlinked by an instance that never enumerated the peer's "
                "row; that row had no other durable copy, so the loss is unrecoverable"
            )
            surviving = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            assert [row.get("id") for row in surviving] == [
                "peer-held"
            ], f"the peer's row did not survive compaction; on disk: {surviving!r}"
        finally:
            svc.stop()

    def test_compaction_still_drops_a_row_this_instance_repaired(self, tmp_path) -> None:
        """NEGATIVE CONTROL: preserving a peer's row must not make compaction a no-op.

        A row this instance DID enumerate and does not hold is repaired, so removing it is
        the whole purpose of the compaction pass. If this stopped working the sidecar would
        grow without bound and every repaired row would be re-read forever.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        sidecar = tmp_path / "autonudge.quarantine.json"
        repaired = {"id": "mine", "slot_key": "chat-1-1", "idle_secs": 300}
        sidecar.write_text(json.dumps({"quarantined": [repaired]}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            # Enumerated at load, then repaired out of the held set this pass.
            svc._sidecar_seen = {_an._quarantine_row_key(repaired)}
            svc._quarantined = []

            svc._compact_quarantine_sidecar()

            assert not sidecar.exists(), (
                "a row this instance enumerated and no longer holds was kept, so compaction "
                "can never shrink the file"
            )
        finally:
            svc.stop()

    def test_a_second_writer_is_excluded_from_the_sidecar_transaction(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING, fenced): the union read-then-replace could lose a row.

        ``_write_quarantine_sidecar`` reads the on-disk rows, unions them with memory, then
        replaces the whole file. Within one event loop that pair is synchronous, so the
        intra-process race cannot fire -- the harm needs a SECOND AutoNudge writing the same
        home, whose row lands after the read and is replaced away. The sidecar is that row's
        only durable copy, so the loss has no recovery path.

        The remedy EXCLUDES that writer rather than comparing a stat: a stat bracket cannot
        be made atomic under POSIX rename, which is why the earlier one was removed. So the
        property to assert is mutual exclusion -- a second holder must NOT get in while the
        transaction is open. Asserting that a mid-region foreign write survives would instead
        test merge-safe append, a different and unimplemented remedy. A thread suffices
        because the lock is advisory on the fd, so a second open is the rival's acquisition.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        ctx = _fork_probe_context()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            lock_path = svc._quarantine_path.with_name(svc._quarantine_path.name + ".lock")
            entered = tmp_path / "rival-entered"
            proc = ctx.Process(
                target=_mp_rival_acquire, args=(str(lock_path), str(entered)), daemon=True
            )
            with svc._sidecar_transaction():
                proc.start()
                got_in = _appeared(entered, 1.5)
            proc.join(timeout=5)
            assert not proc.is_alive(), (
                "the rival never returned after the transaction closed, so its probe is "
                "still waiting on the sidecar lock"
            )

            assert not got_in, (
                "a second writer acquired the sidecar lock while the transaction was open, "
                "so the read-modify-write is still losable by a concurrent AutoNudge"
            )
            assert _appeared(entered, 5), "the rival never acquired after release"
        finally:
            _reap(proc)
            svc.stop()

    def test_the_sidecar_lock_is_held_for_the_whole_read_modify_write(self, tmp_path) -> None:
        """The guard must WRAP the union, not merely exist beside it.

        A lock taken and released before the read, or after the replace, leaves exactly the
        window the finding names. This asserts the transaction is open at both ends.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantined = [{"id": "held", "slot_key": "chat-1-1", "idle_secs": 300}]
            held_during: list[str] = []
            real_read = svc._quarantine_rows_on_disk
            real_write = svc._write_quarantine_rows
            depth = {"n": 0}

            @contextmanager
            def counting_transaction():
                depth["n"] += 1
                try:
                    yield
                finally:
                    depth["n"] -= 1

            def note_read():
                held_during.append(f"read:{depth['n']}")
                return real_read()

            def note_write(rows):
                held_during.append(f"write:{depth['n']}")
                return real_write(rows)

            with (
                patch.object(svc, "_sidecar_transaction", counting_transaction),
                patch.object(svc, "_quarantine_rows_on_disk", note_read),
                patch.object(svc, "_write_quarantine_rows", note_write),
            ):
                svc._write_quarantine_sidecar()

            assert held_during == ["read:1", "write:1"], (
                "the lock must be open across BOTH the read and the replace; observed "
                f"{held_during!r}"
            )
        finally:
            svc.stop()

    def test_an_unlocked_hand_editor_is_still_last_write_wins(self, tmp_path) -> None:
        """The residual the lock does NOT cover, pinned so it is a decision not an oversight.

        ``_sidecar_transaction`` excludes another AutoNudge, because both take the same
        advisory lock on the sentinel. It cannot exclude a human editing the file in a text
        editor, which takes no lock at all -- so that write stays last-write-wins, exactly as
        it is on ``autonudge.json``, which ``_write_state`` replaces with no guard whatever.

        Closing this too would require the sidecar to stop being a second durable location,
        which is first-principles-review's store-only alternative -- recorded as a deliberate
        open decision in ``_serialize_state`` rather than taken here.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        sidecar = tmp_path / "autonudge.quarantine.json"
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            held = {
                "id": "held",
                "slot_key": "chat-4-4",
                "message": "AKIAIOSFODNN7EXAMPLE",
                "idle_secs": 300,
            }
            svc._quarantined = [dict(held)]
            latecomer = {"id": "latecomer", "slot_key": "chat-5-5", "idle_secs": 300}

            # The read happens first and sees no rows; the foreign write lands after it.
            with patch.object(svc, "_quarantine_rows_on_disk", return_value=[]):
                sidecar.write_text(json.dumps({"quarantined": [latecomer]}), encoding="utf-8")
                svc._write_quarantine_sidecar()

            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            ids = {row.get("id") for row in on_disk}
            assert ids == {"held"}, (
                "expected last-write-wins publication; a bracket that aborts here is the "
                f"check-then-mutate the subtraction removed. On disk: {ids!r}"
            )
        finally:
            svc.stop()

    def test_two_same_second_move_asides_both_keep_their_bytes(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a second corrupt sidecar overwrote the first's bytes.

        The ``.corrupt-<ts>`` stamp is second-granular and ``Path.replace`` CLOBBERS, so two
        instances moving a corrupt sidecar aside inside one second left only the later copy
        -- destroying exactly the rows the move-aside exists to preserve for the operator.

        The stamp is pinned rather than raced, because a real same-second collision is not
        reproducible on demand; pinning makes the clobber deterministic instead of rare.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(_an.time, "strftime", lambda *_a, **_kw: "20260906T220000Z")
            sidecar = tmp_path / "autonudge.quarantine.json"
            try:
                for bytes_ in ("first corrupt bytes", "second corrupt bytes"):
                    sidecar.write_text(bytes_, encoding="utf-8")
                    svc._move_aside_unreadable_sidecar()
            finally:
                monkeypatch.undo()

            moved = sorted(tmp_path.glob("autonudge.quarantine.json.corrupt-*"))
            survived = {path.read_text(encoding="utf-8") for path in moved}
            assert survived == {"first corrupt bytes", "second corrupt bytes"}, (
                "a same-second move-aside overwrote an earlier corrupt copy, so those "
                f"held-aside rows are unrecoverable; on disk: {survived!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_stale_sidecar_row_does_not_roll_back_the_repaired_loop(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a stale sidecar copy overwrote the authoritative row.

        The row loop is LAST-WINS on ``id`` (``self._loops[loop.id] = loop``), and the
        sidecar rows were concatenated AFTER the main store. So if an unlink ever fails
        and the operator then repairs the loop through the API, the next restart replays
        the held-aside copy last and silently rolls the configuration back -- then
        persists the rollback on the following write.

        The fix puts the sidecar rows FIRST, so a same-``id`` main-store row lands on top.

        Both rows carry a SAFE addressing field here: the stale copy has to be one that
        would otherwise arm, or the ordering it is meant to prove is never exercised.
        """
        loop_id = "abc123"
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps(
                {
                    "quarantined": [
                        {
                            "id": loop_id,
                            "slot_key": "chat-1-2",
                            "message": "STALE instruction",
                            "idle_secs": 999,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": loop_id,
                            "slot_key": "chat-1-2",
                            "message": "repaired instruction",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            armed = svc._loops.get(loop_id)
            assert armed is not None, "the repaired loop did not arm at all"
            assert armed.idle_secs == 300, (
                f"the stale sidecar row won: idle_secs={armed.idle_secs}, so a restart "
                "rolled the operator's repair back"
            )
            assert (
                armed.message == "repaired instruction"
            ), "the stale sidecar message overwrote the repaired one"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_held_row_does_not_arm_a_second_timer_on_a_claimed_slot(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a repaired held row armed a DUPLICATE loop on one slot.

        The earlier fix made the store win on duplicate ``id``. But an operator who
        replaces a refused loop through the API gets a NEW id on the same ``slot_key``, so
        the id-keyed last-wins collapse never fires: the held copy and the replacement both
        armed, and that slot then took two unattended turns per cycle.

        Both rows carry SAFE addressing fields here -- the held copy has to be one that
        would otherwise arm, or the de-duplication this pins is never exercised.

        Also asserts the held copy is still HELD. Dropping it to avoid the duplicate would
        trade this bug for the sibling one: the next write would unlink the only copy.
        """
        slot = "chat-7-7"
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps(
                {
                    "quarantined": [
                        {
                            "id": "held-copy",
                            "slot_key": slot,
                            "message": "the held instruction",
                            "idle_secs": 300,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "replacement",
                            "slot_key": slot,
                            "message": "the replacement instruction",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            on_slot = [loop for loop in svc._loops.values() if loop.slot_key == slot]
            assert len(on_slot) == 1, (
                f"{len(on_slot)} timers armed on one slot, so it takes duplicate "
                f"unattended turns: {sorted(loop.id for loop in on_slot)!r}"
            )
            assert on_slot[0].id == "replacement", (
                "the held copy won the slot instead of the authoritative store row: "
                f"armed={on_slot[0].id!r}"
            )
            assert [row.get("id") for row in svc._quarantined] == ["held-copy"], (
                "the held copy was dropped rather than held aside, so the next write "
                "unlinks the only surviving copy"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_gate_warning_scrubs_the_id_it_names(self, tmp_path, caplog) -> None:
        """GPT 5.6 (BLOCKING): the gate warning fired ABOVE the addressing guard.

        The guard's own comment claimed every sink was downstream of it, but this warning
        is not: a newline-bearing id plus a non-boolean ``gate`` logged the id raw, so a
        store an agent writes could forge a second log record in the ring and /api/logs.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "real-id\nERROR forged log record",
                            "slot_key": "chat-1-1",
                            "message": "hello",
                            "idle_secs": 300,
                            "gate": "false",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level(logging.WARNING):
                svc._load()
            gate_lines = [
                r.getMessage() for r in caplog.records if "non-boolean gate" in r.getMessage()
            ]
            assert gate_lines, "the gate warning did not fire, so this proves nothing"
            for line in gate_lines:
                assert "\n" not in line, (
                    "the raw newline reached the log line, so the store can forge a "
                    f"second record: {line!r}"
                )
                assert (
                    "forged log record" not in line or "\\n" in line
                ), f"the id was interpolated unescaped: {line!r}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_malformed_sidecar_row_is_kept_held_not_dropped(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a malformed HELD row was skipped, then compacted away.

        The insertion loop's skip arm treats every unusable row alike, but a store row
        still has the store as its durable copy while a sidecar row has only the
        sidecar -- so skipping the latter let compaction unlink the row held for repair.
        """
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [{"id": "held-malformed", "idle_secs": []}]}),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert not svc._loops, "the malformed row armed a loop instead of being skipped"
            assert [row.get("id") for row in svc._quarantined] == ["held-malformed"], (
                "the malformed sidecar row was dropped rather than kept held, so "
                f"compaction unlinks its only durable copy: {svc._quarantined!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_sidecar_only_row_is_held_not_armed(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING) + First Principles item 6: hold, never arm.

        Arming a sidecar row makes the sidecar the only durable copy of a LIVE loop,
        which puts compaction on the delete path. Withhold-and-warn is the contract
        the malformed-row sibling already keeps.
        """
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [_REPAIRED_HELD_ROW]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert "repaired" not in svc._loops, (
                "a sidecar-only row was armed, so the sidecar is the only durable copy "
                f"of a live loop: {sorted(svc._loops)}"
            )
            assert [row.get("id") for row in svc._quarantined] == ["repaired"], (
                "the row was not kept held for repair, so the next write unlinks its "
                f"only copy: {svc._quarantined!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_compaction_cannot_resurrect_a_deleted_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): repaired sidecar row + failed compaction + DELETE.

        Compaction is the ONLY path that removes a sidecar row, and its failure is
        deliberately non-fatal, so a delete commits the store alone and leaves the
        row on disk. The next load then re-arms a loop the operator deleted.
        """

        def _boom(self) -> None:
            raise OSError("sidecar compaction failed")

        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [_REPAIRED_HELD_ROW]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        monkeypatch.setattr(AutoNudgeService, "_compact_quarantine_sidecar", _boom)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            svc._loops.pop("repaired", None)
            svc._save()
        finally:
            svc.stop()

        reloaded = AutoNudgeService(base_dir=tmp_path)
        try:
            reloaded._load()
            assert "repaired" not in reloaded._loops, (
                "a deleted loop was resurrected from the sidecar after compaction "
                f"failed: {sorted(reloaded._loops)}"
            )
        finally:
            reloaded.stop()

    @pytest.mark.asyncio
    async def test_a_non_list_loops_container_refuses_instead_of_reading_empty(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a corrupt ``loops`` read as empty, then got DELETED.

        ``_rows_or_empty`` answers ``[]`` for any non-list, which is indistinguishable
        from an empty store -- so the rows are not armed AND the next write replaces the
        file with an empty one. Absent stays legal; present-but-not-a-list must refuse.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(
            json.dumps({"version": 1, "loops": {"chat-1-1": {"id": "a"}}}), encoding="utf-8"
        )
        original = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._load_refused, (
                "a non-list 'loops' loaded as an empty store, so the next write deletes "
                "the rows it still holds"
            )
            with pytest.raises(AutoNudgeStoreUnvetted):
                svc._write_state({"version": 1, "loops": []})
            assert store.read_text(encoding="utf-8") == original, (
                "the corrupt store was overwritten, destroying the rows an operator "
                "needs in order to repair it"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unreadable_sidecar_latches_the_refusal_not_just_moves_aside(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6: moving the file aside without the latch left a RETRY legal.

        ``_write_quarantine_sidecar`` refused this write, but without ``_load_refused``
        the next attempt in the same process reads the freshly-absent sidecar as
        authoritative and compacts around rows nothing ever enumerated.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            monkeypatch.setattr(svc, "_quarantine_rows_on_disk", lambda: None)
            with pytest.raises(AutoNudgeStoreUnvetted):
                svc._write_quarantine_sidecar()
            assert svc._load_refused, (
                "the refusal was not latched, so a retry in this process would treat "
                "the moved-aside sidecar's absence as 'nothing held'"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_held_row_whose_id_a_store_row_claims_stays_held(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a store row REUSING a held row's id silently deleted it.

        The two existing guards cover a claimed SLOT and a second HELD row on one id.
        Neither covers the case here: the held row's slot is free, so no store row wants
        it, and no other held row shares its id -- yet the store carries that id. The
        id-keyed last-wins insertion then lets the store row replace the held copy in
        memory, and compaction reads the id as stored and unlinks the sidecar, so the
        only durable copy of the held row is gone.
        """
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps(
                {
                    "quarantined": [
                        {
                            "id": "shared-id",
                            "slot_key": "chat-held-1",
                            "message": "the held instruction",
                            "idle_secs": 300,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "shared-id",
                            "slot_key": "chat-store-2",
                            "message": "the store instruction",
                            "idle_secs": 600,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [row.get("id") for row in svc._quarantined] == ["shared-id"], (
                "the held row was not held aside, so compaction will unlink its only "
                f"durable copy: quarantined={svc._quarantined!r}"
            )
            armed = svc._loops.get("shared-id")
            assert armed is not None and armed.slot_key == "chat-store-2", (
                "the authoritative store row did not win the id: "
                f"armed={armed and armed.slot_key!r}"
            )
            assert not any(
                loop.slot_key == "chat-held-1" for loop in svc._loops.values()
            ), "the held row armed a timer on its own slot instead of staying held"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_two_held_rows_sharing_an_id_both_survive_the_additive_write(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): keying on ``id`` alone COLLAPSED distinct held rows.

        Two sidecar rows can carry the same id while differing in content -- one repaired,
        one still held. An id-keyed de-duplication treated them as the same row and kept
        only the in-memory copy, so a failed main-store replacement lost the other for
        good. The key has to cover the whole serialized row.
        """
        held = {
            "id": "same-id",
            "slot_key": "chat-1-1",
            "message": "AKIAIOSFODNN7EXAMPLE",
            "idle_secs": 300,
        }
        repaired = {
            "id": "same-id",
            "slot_key": "chat-2-2",
            "message": "a different instruction entirely",
            "idle_secs": 600,
        }
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(json.dumps({"quarantined": [repaired]}), encoding="utf-8")
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantined = [dict(held)]
            real_replace = _an.replace_with_retry

            def only_the_store_fails(src, dst):
                if Path(dst) == svc._path:
                    raise OSError("store volume is full")
                return real_replace(src, dst)

            monkeypatch.setattr(_an, "replace_with_retry", only_the_store_fails)
            with pytest.raises(OSError):
                svc._write_state({"version": 1, "loops": []})

            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            assert len(on_disk) == 2, (
                "the two same-id rows collapsed to one, so the copy not held in memory is "
                f"lost now that the store replacement failed: on disk={on_disk!r}"
            )
            assert sorted(row["slot_key"] for row in on_disk) == ["chat-1-1", "chat-2-2"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unreadable_sidecar_refuses_the_write_and_is_moved_aside(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING) reconciled with Design's availability concern.

        GPT: returning here let the store land and the sidecar compact around rows this
        process never enumerated, so the write must RAISE instead.

        Design: making an unreadable sidecar refuse until a human edits JSON reintroduces
        the "one bad artifact disarms everything" cliff this PR removed for the main store.

        Both: refuse THIS write, and move the file aside so a restart recovers. The bytes
        survive either way, which is what an operator needs to re-inject the rows.
        """
        sidecar = tmp_path / "autonudge.quarantine.json"
        payload = "{ this is not json at all"
        sidecar.write_text(payload, encoding="utf-8")
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantined = [
                {
                    "id": "held",
                    "slot_key": "chat-3-3",
                    "message": "AKIAIOSFODNN7EXAMPLE",
                    "idle_secs": 300,
                }
            ]
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_quarantine_sidecar()

            assert store.read_text(encoding="utf-8") == before, (
                "the store was replaced around rows nothing enumerated, which is the "
                "overwrite this refusal exists to prevent"
            )
            assert not sidecar.exists(), (
                "the unreadable file is still in place, so every later write hits the "
                "same refusal and the outage needs a hand repair"
            )
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == payload
            ), "the moved-aside copy does not hold the original bytes"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_repaired_row_survives_a_store_replacement_that_fails(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the pre-replacement sidecar write could DELETE a row.

        The crash window: one row is repaired this pass (so it leaves ``_quarantined``)
        while a sibling stays held. Writing the in-memory set alone lands a REDUCED
        sidecar, and when the main-store replacement then fails, the repaired row is in
        neither file -- the store still holds old content that never had it.

        So the pre-replacement write must be ADDITIVE, with compaction waiting for the
        store to land. Asserts on the FILE, because that is all a restart can read.
        """
        repaired = {
            "id": "repaired",
            "slot_key": "chat-9-9",
            "message": "operator fixed this one",
            "idle_secs": 300,
        }
        still_held = {
            "id": "still-held",
            "slot_key": "chat-8-8",
            "message": "AKIAIOSFODNN7EXAMPLE",
            "idle_secs": 300,
        }
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(json.dumps({"quarantined": [repaired, still_held]}), encoding="utf-8")
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            # The repaired row armed and left the held set; the sibling stayed.
            svc._quarantined = [dict(still_held)]

            real_replace = _an.replace_with_retry

            def only_the_store_fails(src, dst):
                if Path(dst) == svc._path:
                    raise OSError("store volume is full")
                return real_replace(src, dst)

            monkeypatch.setattr(_an, "replace_with_retry", only_the_store_fails)
            with pytest.raises(OSError):
                svc._write_state({"version": 1, "loops": []})

            assert store.read_text(encoding="utf-8") == before, (
                "the store changed even though its replacement raised, so this test is "
                "not measuring the crash window it claims to"
            )
            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            ids = sorted(str(row.get("id")) for row in on_disk)
            assert ids == ["repaired", "still-held"], (
                "the repaired row is gone from the sidecar while the store still holds "
                f"its old content, so a restart loses it permanently: on disk={ids!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_sidecar_is_compacted_once_the_store_lands(self, tmp_path) -> None:
        """The additive write must not let the sidecar grow without bound.

        Additive is only correct BEFORE the replacement; once the store is durable the
        rows it superseded have to go, or every repaired row accumulates forever.

        The row must be one this instance ENUMERATED and then repaired, so the fixture
        goes through ``_load``. An earlier fixture wrote the sidecar and never loaded it,
        which made its row indistinguishable from one added out of band -- the shape a
        sibling test requires be PRESERVED -- so it pinned data loss, not bounded growth.
        """
        stale = {"id": "gone", "slot_key": "chat-1-1", "message": "x", "idle_secs": 300}
        sidecar = tmp_path / "autonudge.quarantine.json"
        sidecar.write_text(json.dumps({"quarantined": [stale]}), encoding="utf-8")
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [r.get("id") for r in svc._quarantined] == ["gone"], (
                "the fixture did not enumerate the sidecar row, so this would measure "
                "the out-of-band case instead of the repaired one"
            )
            # The repair: the operator moved it out, so it is not held.
            svc._quarantined = [
                {
                    "id": "held",
                    "slot_key": "chat-2-2",
                    "message": "AKIAIOSFODNN7EXAMPLE",
                    "idle_secs": 300,
                }
            ]
            svc._write_state({"version": 1, "loops": []})

            on_disk = json.loads(sidecar.read_text(encoding="utf-8"))["quarantined"]
            assert [row.get("id") for row in on_disk] == ["held"], (
                "the superseded row was still on disk after the store landed, so the "
                f"sidecar grows without bound: {[r.get('id') for r in on_disk]!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.parametrize(
        "held",
        ['{"id": "x"}', "null", '"a string"', "7"],
        ids=["object", "null", "string", "number"],
    )
    @pytest.mark.asyncio
    async def test_a_non_list_quarantined_refuses_rather_than_reading_as_empty(
        self, tmp_path, held
    ) -> None:
        """GPT 5.6 (BLOCKING): a non-list ``quarantined`` was silently deleted.

        ``_rows_or_empty`` answers anything that is not a list with ``[]``, which is
        indistinguishable from "nothing is held aside". The loader armed normally and the
        next persist called ``_drop_quarantine_sidecar``, unlinking the only copy of rows
        an operator still had to repair.

        ``null`` is parametrized alongside the object shape because it is equally
        "present but not a list" and equally reads as empty through that helper.
        """
        sidecar = tmp_path / "autonudge.quarantine.json"
        payload = '{"quarantined": ' + held + "}"
        sidecar.write_text(payload, encoding="utf-8")
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "loops": [
                        {
                            "id": "keep",
                            "slot_key": "chat-1-1",
                            "message": "fine",
                            "idle_secs": 300,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert (
                svc._load_refused is True
            ), "a non-list `quarantined` read as empty, so the next write unlinks it"
            assert not svc._loops, (
                "loops armed under a refused store; a delivered cycle cannot record "
                f"itself, so a restart repeats it. armed={sorted(svc._loops)!r}"
            )
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == payload
            ), "the sidecar bytes were lost, so the operator has nothing to repair"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_row_does_not_strand_the_loops_loaded_before_it(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a declined row stranded every loop already loaded.

        ``_loops[loop.id] = loop`` runs per row inside the load loop, and ``_load`` does
        not reset ``_loops`` on entry, so rows BEFORE the unusable one were already
        armed when the refusal latched. Each then fired once, its post-fire persist
        raised ``AutoNudgeStoreUnvetted``, and it never re-armed -- so a single bad
        addressing field silently froze healthy channel loops mid-cycle.

        Quarantining the offending row is what makes that unreachable: the row is held
        aside under ``quarantined`` and its SIBLINGS still arm, so ``_load_refused``
        stays False. The whole-store refusal is a different arm, for a host whose
        credential policy will not compose at all, and the file on disk is untouched
        either way.

        Order matters -- the clean row is FIRST, so it is in ``_loops`` by the time the
        second row is declined. A fixture with the bad row first would pass even
        unfixed.
        """
        rows = [
            {"id": "clean-1", "slot_key": "chat-1-1", "message": "one", "idle_secs": 300},
            {"id": self.SECRET, "slot_key": "chat-2-2", "message": "two", "idle_secs": 300},
            {"id": "clean-2", "slot_key": "chat-3-3", "message": "three", "idle_secs": 300},
        ]
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._load_refused is False, "one bad row disarmed the whole store"
            assert sorted(svc._loops) == [
                "clean-1",
                "clean-2",
            ], f"healthy loops were disarmed by one bad row: {sorted(svc._loops)!r}. Quarantine holds the offending row and leaves its siblings armed"
            svc._write_state(svc._serialize_state())
            assert _held_aside_rows(tmp_path), "the declined row was dropped"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_clean_loop_still_arms(self, tmp_path) -> None:
        """Negative control: the guard must not refuse ordinary rows.

        A predicate that rejected everything -- or that compared the wrong pair of
        values -- would pass every arm above while disarming the whole fleet.
        """
        self._write(tmp_path)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "abc123" in svc._loops, "a clean loop was refused"
            assert svc._loops["abc123"].slot_key == "chat-1-2"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_refusal_does_not_rewrite_the_addressing_value(self, tmp_path) -> None:
        """Refused, NOT scrubbed. Scrubbing would rewrite the identity the client
        resolves the row by, leaving a row that is displayed but unactionable --
        so the loop must be absent entirely rather than present under a mangled
        id."""
        self._write(tmp_path, id=f"loop-{self.SECRET}")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}
            assert not any("REDACTED" in k for k in svc._loops), "a scrubbed id was armed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_refusal_leaves_the_store_row_for_the_operator(self, tmp_path) -> None:
        """The warning says "fix the store entry", so the entry must still be there.

        Found by a failing test, not by reasoning: ``_load``'s banner repair sets
        ``_store_dirty``, and ``start()`` then persists ``self._loops`` -- which no
        longer holds the refused row. Without suppression the refusal DELETED the
        very entry the operator was told to fix, and the on-disk ``loops`` list came
        back empty. The banner here is a list so the repair arm fires and arms the
        dirty flag, which is the condition that made it destructive.
        """
        row = {
            "id": f"loop-{self.SECRET}",
            "slot_key": "chat-1-2",
            "message": "keep going",
            "idle_secs": 300,
            "banner": ["not-a-string"],
        }
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, "the loop was armed"
            held = _held_aside_rows(tmp_path)
            assert len(held) == 1, "the refusal deleted the operator's row"
            assert held[0]["id"] == row["id"], "the persisted identity changed"
        finally:
            svc.stop()

    def test_the_serializer_and_the_loader_share_one_field_set(self) -> None:
        """Two copies could drift so the serializer exempts a field the loader
        does not guard -- which is exactly the hole the exemption would open."""
        from kiro_crew.autonudge import ADDRESSING_FIELDS

        # The module-level alias was removed (one set, two names); what matters is that
        # the serializer reads the SERVICE's set, so assert the binding, not a rename.
        assert autonudge_handlers.ADDRESSING_FIELDS is ADDRESSING_FIELDS
        assert sorted(ADDRESSING_FIELDS) == ["id", "slot_key"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_non_printable_addressing_field_is_refused(
        self, tmp_path, caplog, field
    ) -> None:
        """GPT 5.6 (BLOCKING): ``redact`` is not a control-character guard.

        ``redact`` rewrites credential- and URL-shaped text, so a newline rides
        straight through ``redact(got) != got`` -- the row constructs cleanly, arms,
        and the id then reaches ~15 bare ``%s`` log calls, where one newline splits
        a record in two and the operator reads an attacker-authored second line as
        the gateway's own.

        This is a DIFFERENT path from
        ``TestScrubbedLogTextCannotForgeARecord`` above: that fixture omits
        ``slot_key`` so construction fails and the malformed-entry arm runs. Here
        every field is present and well-typed, so the row reaches the addressing
        guard -- which is the arm that would otherwise let it through.
        """
        forged = "2026-01-01 00:00:00 WARNING FORGED: gateway compromised"
        self._write(tmp_path, **{field: f"abc123\n{forged}"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("WARNING"):
                await svc.start()
            assert svc._loops == {}, "a newline-bearing addressing field was armed anyway"
            assert "refusing loop" in caplog.text, "the refusal was silent"
            assert field in caplog.text, "the warning does not name the offending field"
            refusals = [r for r in caplog.records if "refusing loop" in r.getMessage()]
            assert refusals, "no refusal record was emitted"
            for rec in refusals:
                msg = rec.getMessage()
                # The property is that the record cannot be SPLIT, not that the
                # text is absent. The warning renders the id through
                # ``redact(repr(...))``, and ``repr`` turns the newline into a
                # literal backslash-n, so the injected tail stays inert on one
                # line. Asserting absence instead would fail here for the right
                # reason and the wrong claim.
                assert "\n" not in msg, f"the refusal itself was split in two: {msg!r}"
                if forged in msg:
                    assert "\\n" in msg, "the newline reached the record unescaped"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("abc\rdef", id="carriage-return"),
            pytest.param("abc\tdef", id="tab"),
            pytest.param("abc\x1b[31mdef", id="ansi-escape"),
            pytest.param("abc\x00def", id="nul"),
        ],
    )
    async def test_other_non_printables_are_refused_too(self, tmp_path, bad) -> None:
        """The predicate is a class of characters, not one special-cased newline.

        Measured: none of these is caught by ``redact``, so before this guard every
        one of them armed and reached the log sinks.
        """
        self._write(tmp_path, id=bad)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, f"{bad!r} was armed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_printable_addressing_value_is_still_accepted(self, tmp_path) -> None:
        """NEGATIVE CONTROL for the new arm, and it must be able to fail.

        ``str.isprintable()`` is False for an EMPTY string's opposite reasons and
        True for the ASCII space, so a guard written as ``got.isascii()`` or as a
        whitespace ban would refuse ordinary keys. A real slot key carries hyphens
        and digits; refuse those and the whole fleet disarms while every arm above
        still passes.
        """
        self._write(tmp_path, id="chat-1281-1785676802", slot_key="chat-1281-1785676802")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "chat-1281-1785676802" in svc._loops, "an ordinary printable id was refused"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_non_printable_refusal_keeps_the_row_on_disk(self, tmp_path) -> None:
        """The refusal is unchanged; the row now SURVIVES the next write.

        Pinning the DROP here would pin the data-loss defect itself
        (GPT 5.6, BLOCKING): the operator was warned by name about a field in an entry
        that the next wholesale write had already deleted. The loop not arming is the
        security property and it is untouched -- only the row's fate on disk changed.
        """
        bad = "abc123\nFORGED"
        self._write(tmp_path, id=bad)
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, "the loop was armed"
            assert (
                svc._load_refused is False
            ), "one unusable row refused the whole store instead of being quarantined"
            await svc._persist_locked()
            assert _held_aside_rows(tmp_path), "the declined row was dropped"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_id, why",
        [
            pytest.param("loop-AKIAIOSFODNN7EXAMPLE", "credential", id="credential-shaped"),
            pytest.param("loop\nFORGED-LINE", "non-printable", id="non-printable"),
            pytest.param(["loop-1"], "non-string", id="non-string"),
        ],
    )
    async def test_validation_runs_BEFORE_the_monitor_warning_sink(
        self, tmp_path, caplog, bad_id, why
    ) -> None:
        """GPT 5.6 (BLOCKING): ordering IS the control here.

        ``_load`` must not validate the addressing fields only AFTER the
        quarantined-malformed-monitor warning, which interpolates a bare
        ``loop.id``. A row that is unsafe in ``id`` AND carries a monitor that will
        not parse therefore hit that ``%s`` first, putting the raw value into the
        log ring and ``/api/logs`` before anything refused it.

        This fixture is that exact row: an unsafe ``id`` plus ``monitor`` set to a
        value ``monitor_state_from_dict`` rejects. The assertions are about ORDER --
        the row must be refused and NO record may carry the raw value. A guard that
        still validates but does so too late passes an ordinary refusal test and
        fails this one.
        """
        self._write(tmp_path, id=bad_id, monitor={"not": "a valid monitor"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            with caplog.at_level("DEBUG"):
                await svc.start()

            # (a) refused
            assert svc._loops == {}, f"an id that is {why} was armed anyway"
            assert "refusing loop" in caplog.text, "the refusal was silent"

            # (b) the REFUSAL's own record may name the id -- that is its job, and it
            # renders it through ``redact(repr(...))`` so a credential is stripped and
            # a control character is escaped. What must never happen is any OTHER sink
            # carrying the value, and no record may be split by a real newline.
            #
            # Scoped to THIS module's logger on purpose. ``caplog`` at DEBUG also
            # captures unrelated records (config-deprecation notices, the sandbox
            # userns probe), some of which are legitimately multi-line -- asserting
            # over every captured record made the arm depend on what else happened to
            # log during the test, which failed 1 run in 4 for a reason that had
            # nothing to do with the code under test.
            needle = bad_id if isinstance(bad_id, str) else repr(bad_id)
            mine = [r for r in caplog.records if r.name == "kiro_crew.autonudge"]
            assert mine, "no autonudge record was captured -- the fixture never reached _load"
            for rec in mine:
                msg = rec.getMessage()
                assert "\n" not in msg, f"a record was split into several lines: {msg!r}"
                if "refusing loop" in msg:
                    # the intended disclosure: scrubbed and escaped
                    assert self.SECRET not in msg, "the refusal echoed the credential unredacted"
                    continue
                assert needle not in msg, f"a non-refusal record carried the id: {msg!r}"

            # (c) the leaking sink must not have run for this row at all --
            # reaching it is what the relocation prevents.
            assert not [
                r for r in mine if "quarantined malformed monitor" in r.getMessage()
            ], "the monitor sink ran on a row that should have been refused first"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_malformed_monitor_on_a_SAFE_row_still_quarantines(self, tmp_path) -> None:
        """Positive control for the arm above, so its silence is not vacuous.

        The ordering assertion checks that the monitor sink did NOT run. That is only
        meaningful if the sink runs at all on a row whose addressing fields are fine
        -- otherwise the fixture could be wrong about what triggers it and the
        assertion would pass for the wrong reason.
        """
        self._write(tmp_path, monitor={"not": "a valid monitor"})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            loop = svc._loops.get("abc123")
            assert loop is not None, "a safe row with a bad monitor was refused"
            assert loop.monitor is not None, "the monitor was not quarantined"
        finally:
            svc.stop()


class TestTheStopSentinelSurvivesARefusedArm:
    """GPT 5.6 (BLOCKING): the policy refusal ran AFTER deleting the stop sentinel.

    The auto-default path resolves a per-session sentinel and unlinks it, so a fresh loop
    does not inherit a stale stop signal. That unlink is unconditional
    (``unlink(missing_ok=True)``) and it sat BEFORE the 503 policy probe. So on a host
    whose credential policy cannot compose: the operator's live stop file for an
    ALREADY-RUNNING loop was deleted, and only then was the arm refused -- leaving the old
    unattended loop running with its stop signal gone. Deleting a control-plane file and
    then declining to do the work is the data loss.

    Both 503 sites test the same condition, but they are NOT interchangeable, which is why
    the second one had to move rather than be dropped: the earlier one only fires when
    ``normalize_banner`` actually scrubs, and it returns early on a BLANK banner. A
    bannerless arm -- the common case, and the only shape a channel key permits -- reaches
    the sentinel block with the policy still unprobed. So the probe is lifted above the
    unlink instead.

    ``test_the_unlink_still_happens_on_a_healthy_host`` is the negative control: it fails
    if the unlink is simply removed, which would break the documented reason it exists
    ("per-session sentinel so multiple loops don't clash").
    """

    SLOT = "chat-1281-1785676802"
    WORKSPACE = "default"

    class _BrokenPolicy:
        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    def _state(self):
        """A REAL dashboard slot: the auto-default branch needs one to resolve a path."""
        return SimpleNamespace(
            # ``memory_mode`` must be spelled out: the arm path refuses a
            # non-persistent slot with a 403 before it reaches the gate under test,
            # and a bare MagicMock attribute stringifies to a Mock repr.
            _slots={self.SLOT: MagicMock(workspace=self.WORKSPACE, memory_mode="persistent")},
            sessions=None,
            channel_transports={},
            push_slots_update=lambda: None,
        )

    def _sentinel(self) -> Path:
        """The exact path the auto-default branch resolves for this slot."""
        return Path(authz.resolve_stop_sentinel(self.SLOT, self.WORKSPACE))

    @pytest.mark.asyncio
    async def test_a_refused_arm_leaves_a_live_stop_file_alone(self, tmp_path) -> None:
        """THE finding: refuse first, so an existing stop signal is never destroyed."""
        sentinel = self._sentinel()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("STOP", encoding="utf-8")
        assert sentinel.exists(), "fixture failed to place the sentinel"

        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.add = AsyncMock()
        self._install(self._BrokenPolicy())
        try:
            loop, error, status = await authz.authorize_and_add_nudge(
                svc=svc,
                state=self._state(),
                slot_key=self.SLOT,
                message="keep going",
                source="dashboard",
            )
            assert status == 503, f"expected the policy refusal, got {status} {error!r}"
            assert loop is None
            svc.add.assert_not_awaited()
            assert sentinel.exists(), (
                "the arm deleted the operator's live stop file and THEN refused -- the "
                "already-running loop is now unattended with no way to stop it"
            )
            assert sentinel.read_text(encoding="utf-8") == "STOP", "the file was rewritten"
        finally:
            sentinel.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_the_unlink_still_happens_on_a_healthy_host(self, tmp_path) -> None:
        """NEGATIVE CONTROL: the fix must reorder the probe, not delete the unlink.

        A stale sentinel left in place would stop the NEW loop on its first cycle, which
        is exactly what the auto-default unlink exists to prevent. This arm fails if the
        unlink is removed rather than moved.
        """
        sentinel = self._sentinel()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("STOP", encoding="utf-8")

        armed = SimpleNamespace(id="loop-1", slot_key=self.SLOT, message="m", banner="")
        svc = MagicMock()
        svc.list_all = lambda: []
        svc.get_by_id = lambda _id: None
        svc.add = AsyncMock(return_value=armed)
        # A healthy policy: the probe passes, so the unlink must still run.
        self._install(SimpleNamespace(redact=lambda text: text))
        try:
            _loop, error, status = await authz.authorize_and_add_nudge(
                svc=svc,
                state=self._state(),
                slot_key=self.SLOT,
                message="keep going",
                source="dashboard",
            )
            assert status == 200, f"the healthy arm was refused: {status} {error!r}"
            assert not sentinel.exists(), (
                "the stale stop file survived a successful arm -- the new loop will stop "
                "on its first cycle; the probe must be REORDERED, not the unlink removed"
            )
        finally:
            sentinel.unlink(missing_ok=True)


class TestAClientCanDetectTheDestructiveEcho:
    """Design: the PATCH echo guard is a success-that-isn't.

    ``authorize_and_update_nudge`` compares an incoming ``message`` against
    ``scrub_loop_text(current.message)`` and, on a match, sets it to ``None`` and answers
    200. So a client that read the loop, changed an unrelated field and PATCHed the whole
    object back was told the write succeeded while its ``message`` was discarded. The
    popover's dirty check stops OUR client doing it and a log line records it, but neither
    reaches a third-party client -- the API contract itself said nothing.

    The contract now says it on BOTH surfaces, because a GET-only flag is not enough:

    * **GET** carries ``message_redacted`` -- true when the served projection differs from
      the stored value, i.e. echoing it back WOULD be destructive. A client that reads
      before writing can now see that in advance.
    * **PATCH** carries ``message_ignored`` -- true when the guard kept the stored goal.
      A client that never GETs first learns nothing from the GET flag, so the response to
      the lossy write has to say so itself. Without this arm the finding is only half
      answered.

    ``test_a_clean_message_is_not_flagged`` is the negative control: a message with nothing
    credential-shaped in it round-trips unchanged, so the flag must be false and
    ``message_ignored`` absent. A flag hardcoded true, or one keyed on "did we scrub at all"
    rather than "did the value change", passes the arms above and fails this one.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _loop(message: str):
        return NudgeLoop(id="loop-1", slot_key="chat-1-123", message=message)

    def test_the_GET_projection_flags_a_redacted_message(self) -> None:
        """A client reading first must be able to see that an echo would destroy data."""
        served = autonudge_handlers._serialize(self._loop(f"deploy with {self.SECRET}"))
        assert self.SECRET not in served["message"], "the projection did not scrub"
        assert served.get("message_redacted") is True, (
            "the projection gives a client no way to know its `message` differs from the "
            "stored value, so echoing it back silently destroys the original"
        )

    def test_a_clean_message_is_not_flagged(self) -> None:
        """NEGATIVE CONTROL: the flag must track CHANGE, not merely 'we ran a scrubber'."""
        served = autonudge_handlers._serialize(self._loop("just keep going"))
        assert served["message"] == "just keep going", "a clean message was altered"
        assert served.get("message_redacted") is False, (
            "a message that round-trips unchanged was flagged as redacted, so the flag "
            "cannot tell a client whether an echo is actually destructive"
        )

    @pytest.mark.asyncio
    async def test_the_PATCH_response_names_the_ignored_field(self, tmp_path) -> None:
        """A client that never GETs first must still learn the field was discarded."""
        stored = self._loop(f"deploy with {self.SECRET}")
        echoed = _an.scrub_loop_text(stored.message, field="message")
        assert echoed != stored.message, "fixture failed: the message was not redacted"

        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _s=stored: _s if _id == _s.id else None
        svc.update = AsyncMock(return_value=stored)
        loop, error, status = await authz.authorize_and_update_nudge(
            svc=svc,
            loop_id="loop-1",
            message=echoed,
            source="dashboard",
        )
        assert status == 200, f"the echo was refused rather than ignored: {status} {error!r}"
        assert "message" not in (svc.update.await_args.kwargs or {}) or (
            svc.update.await_args.kwargs.get("message") is None
        ), "the echoed message was written through"
        ignored = svc.update.await_args.kwargs.get("message", "sentinel") is None
        assert ignored is True, (
            f"the update path does not report the kept goal: {ignored!r}. A client that "
            "did not GET first is told 200 with no indication its message was discarded"
        )


class TestOneEchoDecisionGovernsBothTheWriteAndTheResponse:
    """Two projection reads let an echoed mask overwrite a newer goal.

    The handler read the stored row to decide the response flag and the authorizer read it
    again to decide the write, so an update landing between the two made them disagree:
    the handler matched the OLD stored text and reported the goal as ignored, while the
    authorizer compared against the NEWER text, found no echo, and persisted the mask
    over it. A 200 then claimed the field was discarded on the one interleaving where it
    was the value that survived.

    Two assertions, because either alone can pass while the defect is live. The
    AGREEMENT arm is the defect: whatever is persisted and whatever is reported must be
    the same decision. The READ-COUNT arm is the structure that makes it impossible by
    construction -- one read cannot race itself -- and it is what fails loudly if a
    future edit reintroduces a second read whose value happens to match.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_concurrent_update_between_the_reads_cannot_persist_the_mask(self):
        """Fail-first: a newer goal lands between the reads and the mask must not win."""
        older = NudgeLoop(id="loop-1", slot_key="chat-1-123", message=f"deploy with {self.SECRET}")
        masked = _an.scrub_loop_text(older.message, field="message")
        assert masked != older.message, "fixture failed: the message was not redacted"
        newer = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="the newer goal")

        reads: list[str] = []

        def _get_by_id(loop_id: str):
            # The concurrent update lands after the first read, so every later reader
            # sees `newer` -- which is exactly the window the two-read path opened.
            reads.append(loop_id)
            return older if len(reads) == 1 else newer

        svc = MagicMock()
        svc.list_all = lambda: [older]
        svc.get_by_id = _get_by_id
        svc.update = AsyncMock(return_value=newer)

        captured: dict = {}

        class _Resp:
            def __init__(self, body, status=200):
                captured["body"] = body
                captured["status"] = status

        request = SimpleNamespace(
            match_info={"loop_id": "loop-1"},
            remote="127.0.0.1",
            json=AsyncMock(return_value={"message": masked, "expect_fingerprint": "fp-test"}),
        )

        with (
            patch.object(autonudge_handlers, "_autonudge_get", lambda: svc),
            patch.object(autonudge_handlers.web, "json_response", _Resp),
        ):
            await autonudge_handlers.api_autonudge_update(request)

        assert captured["status"] == 200, f"the update was refused: {captured}"
        persisted = svc.update.await_args.kwargs.get("message", None)
        claimed_ignored = "message_ignored" in captured["body"]

        assert not (claimed_ignored and persisted is not None), (
            f"the response claimed message_ignored while persisting {persisted!r} over "
            f"{newer.message!r}. The write and the response came from two different "
            "reads, so the mask overwrote the newer goal on a 200 that called it kept"
        )
        assert not (persisted is None and not claimed_ignored), (
            "the goal was dropped but the response did not say so, so a client that "
            "never GETs first cannot tell its message had no effect"
        )
        assert len(reads) == 1, (
            f"the projection was read {len(reads)} times on one update path; a second "
            "read can observe a different value than the one the write decided on, "
            "which is the race this test exists to close"
        )


class TestTheTwoSkipArmsHoldNoRow:
    """GPT 5.6 (BLOCKING) + First Principles (Subtraction): the hold had to go.

    ``_load`` has TWO arms that decline a row, and they disagreed:

    * the malformed-entry ``except`` arm ``continue``s with no hold, so the row is
      dropped by the next wholesale write -- the contract ``cron.py`` documents
      ("dropped from the store on the next write");
    * the addressing-guard arm HELD the row and wrote it back, which then needed a
      retirement on slot close, a rollback for that retirement, and a rollback in
      ``_add_locked`` -- three hand-maintained paths for one invariant.

    The third path could not be completed. An aborted slot close rolls back through
    ``chat_handlers._restore_slot_nudge_loop(exc.loop, ...)`` where
    ``exc.loop = svc.get_by_slot(name)``, and ``get_by_slot`` searches ``_loops`` only --
    so for a refused row it is ``None`` and the caller has no token to restore. The
    retirement had already reached disk, so the row was permanently gone. That gap sits
    in a caller autonudge does not own, which is why no fourth rollback could close it.

    So the hold is gone, and NEITHER arm holds a row. This test pins that: it FAILS while
    one arm holds and the other drops, and passes once neither does.

    The arms are not identical at the WRITE, and deliberately so. A row declined by
    the addressing guard is QUARANTINED -- re-emitted under ``quarantined`` so the entry
    stays on disk to be repaired, because dropping it silently was a data-loss defect
    (GPT 5.6, BLOCKING). The malformed-entry arm still drops its row. What both share, and
    what this class covers, is that no row is HELD IN ``_loops``: with nothing in the live
    map there is no retirement, no retirement rollback, and nothing for an aborted close to
    restore. A quarantined row is invisible to ``get_by_slot``, so it cannot enter that path.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _write(tmp_path, rows) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_the_two_skip_arms_agree_and_differ_only_in_where_they_preserve(
        self, tmp_path
    ) -> None:
        """Both arms skip the row, keep loading, and PRESERVE it; only the place differs.

        INVERTED from pinning the malformed row as dropped. Dropping it was the defect GPT 5.6
        raised: skipping leaves the row out of ``_loops``, the disk payload is built from
        ``_loops``, so any cause that flagged the store dirty deleted a neighbour nobody chose
        to remove. A malformed row is now carried verbatim in the payload's own ``loops``; an
        unusable ADDRESSING field is still held in the sidecar instead, because addressing
        fields are served unscrubbed and ``loops`` is read back by the loader.

        Refusing the WHOLE store was the earlier answer to that data-loss defect, and it
        cost every healthy loop in the file (design-review: availability cliff). Neither
        arm holds a row in ``_loops``, which ``test_no_held_row_state_exists_to_be_lost``
        pins, so the rollback gap that killed the original hold stays unreachable.
        """
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        malformed = {"id": ["not", "a", "string"], "slot_key": "chat-2-2"}
        bad_addr = {"id": self.SECRET, "slot_key": "chat-3-3", "message": "x"}

        # Malformed alone: skipped but preserved, and the sibling arms.
        self._write(tmp_path, [good, malformed])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == ["keep"], "the good row did not arm"
            assert svc._load_refused is False, "a malformed row refused the whole store"
            payload_ids = [r.get("id") for r in svc._serialize_state()["loops"]]
            assert payload_ids == [
                "keep",
                ["not", "a", "string"],
            ], f"the malformed row was not preserved verbatim: {payload_ids!r}"
        finally:
            svc.stop()

        # Unusable addressing field: quarantined, and the sibling still arms.
        self._write(tmp_path, [good, bad_addr])
        svc2 = AutoNudgeService(base_dir=tmp_path)
        try:
            svc2._load()
            assert svc2._load_refused is False, "an unusable addressing field refused the store"
            assert [lp.id for lp in svc2.list_all()] == [
                "keep"
            ], f"a bad row disarmed its sibling: {sorted(svc2._loops)!r}"
            assert self.SECRET not in svc2._loops, "the credential-shaped row was armed"
            payload = svc2._serialize_state()
            assert [r.get("id") for r in payload["loops"]] == ["keep"]
            assert [r.get("id") for r in svc2._quarantined] == [
                self.SECRET
            ], "the declined row was not preserved for repair"
        finally:
            svc2.stop()

    def test_no_held_row_state_exists_to_be_lost(self) -> None:
        """Structural: the data-loss path is unreachable because nothing is held.

        A rollback gap can only lose state that exists. With no hold there is no
        retirement, no retirement rollback, and nothing for an aborted close to fail to
        restore -- which is what makes the hazard unreachable rather than merely guarded.
        """
        import inspect

        src = inspect.getsource(_an)
        for gone in (
            "_refused_raw_rows",
            "_retire_refused_rows_for_slot",
            "held_before_retire",
        ):
            assert gone not in src, f"{gone} still exists, so the loss path is still live"
        # The whole-store refusal is a DIFFERENT mechanism and must survive.
        assert "_load_refused" in src, "the whole-store refusal was removed too"

    @pytest.mark.asyncio
    async def test_an_aborted_close_cannot_lose_a_refused_row(self, tmp_path) -> None:
        """The finding's own scenario, made harmless.

        The row is not in memory and not in the payload from the moment it is refused, so
        a close that retires nothing and then aborts has nothing to delete.
        """
        self._write(tmp_path, [{"id": self.SECRET, "slot_key": "chat-1-1", "message": "x"}])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            before_ids = [r.get("id") for r in svc._serialize_state()["loops"]]
            await svc.remove_by_slot("chat-1-1")
            after_ids = [r.get("id") for r in svc._serialize_state()["loops"]]
            assert before_ids == after_ids == [], (
                f"a close changed refused-row state: {before_ids!r} -> {after_ids!r}; "
                "with no hold there is nothing for an aborted close to lose"
            )
        finally:
            svc.stop()


class TestANullStoreKeyDoesNotAbortStartup:
    """A store key present but null must not crash ``_load``.

    ``data.get(key, [])`` yields the default only when the key is ABSENT, so a
    hand-edited store carrying ``"quarantined": null`` returned ``None`` and the
    comprehension raised ``TypeError`` uncaught at gateway startup. The sibling
    ``loops`` key is unpacked at the same site and had the identical hazard.
    """

    @staticmethod
    def _write(tmp_path, payload) -> None:
        (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

    GOOD = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, "not-a-list", 7, {"id": "x"}])
    async def test_a_non_list_quarantined_value_still_arms_the_clean_loops(
        self, tmp_path, bad
    ) -> None:
        """``_load`` completes and the well-formed loop arms."""
        self._write(tmp_path, {"version": 1, "loops": [self.GOOD], "quarantined": bad})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == [
                "keep"
            ], f"a {type(bad).__name__} quarantined value disarmed the clean loop"
            assert svc._load_refused is False, "a malformed quarantined value refused the store"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [None, "not-a-list", 7])
    async def test_a_non_list_loops_value_does_not_crash_the_load(self, tmp_path, bad) -> None:
        """The sibling key is unpacked at the same site, so it needs the same guard."""
        self._write(tmp_path, {"version": 1, "loops": bad, "quarantined": []})
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc.list_all() == [], f"a {type(bad).__name__} loops value armed something"
        finally:
            svc.stop()


class TestIdCollidingHeldRowsStayQuarantined:
    """Two held rows on one ``id`` must not collapse under the last-wins insertion.

    ``_load`` applies held rows before store rows so the store wins a shared ``id``.
    That same last-wins insertion silently drops one of TWO held rows sharing an id,
    and compaction would then delete the loser as though the store carried it.
    """

    @staticmethod
    def _write(tmp_path, loops, quarantined) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": loops}),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"version": 1, "quarantined": quarantined}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_the_losing_row_is_kept_rather_than_dropped(self, tmp_path) -> None:
        """The second held row on a shared id stays quarantined, not overwritten.

        ``_loops`` is keyed by id, so its SIZE is 1 either way -- the row that proves
        the fix is the loser surviving in quarantine instead of being forgotten.
        """
        first = {"id": "dup", "slot_key": "chat-1-1", "message": "first", "idle_secs": 300}
        second = {"id": "dup", "slot_key": "chat-2-2", "message": "second", "idle_secs": 300}
        self._write(tmp_path, [], [first, second])

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        held = [row.get("slot_key") for row in svc._quarantined]
        assert "chat-2-2" in held, "the id-colliding held row was dropped, not quarantined"

    @pytest.mark.asyncio
    async def test_a_committed_write_survives_a_failing_compaction(self, tmp_path) -> None:
        """A post-commit compaction error must not report a landed write as failed."""
        row = {"id": "held", "slot_key": "chat-9-9", "message": "keep", "idle_secs": 300}
        self._write(tmp_path, [], [row])
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        def _boom() -> None:
            raise OSError("sidecar compaction failed")

        svc._compact_quarantine_sidecar = _boom  # type: ignore[method-assign]

        # No raise: the main store is already committed, so rolling the caller back
        # would leave live state disagreeing with the file on disk.
        svc._write_state(svc._serialize_state())

        assert json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))


class TestAQuarantinedRowStaysHeld:
    """A quarantined row is re-read on every load and kept HELD, never re-armed.

    An earlier round of this change re-armed a row whose addressing fields had been
    repaired, so an operator recovered it without hand-editing. That made the sidecar
    the only durable copy of a LIVE loop, which is what let a failed compaction
    resurrect a deleted one -- so recovery is now an explicit move into ``loops``.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _write(tmp_path, loops, quarantined) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": loops}),
            encoding="utf-8",
        )
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"version": 1, "quarantined": quarantined}),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_a_repaired_quarantined_row_stays_held(self, tmp_path) -> None:
        """Even a row whose addressing fields now pass is held, not armed."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        repaired = {"id": "fixed", "slot_key": "chat-2-2", "message": "back", "idle_secs": 300}
        self._write(tmp_path, [good], [repaired])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == [
                "keep"
            ], f"a held row was armed from the sidecar: {sorted(svc._loops)!r}"
            assert [r.get("id") for r in svc._quarantined] == [
                "fixed"
            ], f"the repaired row was not kept for repair: {svc._quarantined!r}"
            assert sorted(r.get("id") for r in svc._serialize_state()["loops"]) == ["keep"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_still_unsafe_quarantined_row_stays_quarantined(self, tmp_path) -> None:
        """Revalidation rebuilds quarantine from the rows that STILL fail."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        still_bad = {"id": self.SECRET, "slot_key": "chat-3-3", "message": "x"}
        self._write(tmp_path, [good], [still_bad])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert [lp.id for lp in svc.list_all()] == [
                "keep"
            ], f"an unsafe row was reactivated: {sorted(svc._loops)!r}"
            assert self.SECRET not in svc._loops, "the credential-shaped row was armed"
            assert [r.get("id") for r in svc._quarantined] == [
                self.SECRET
            ], "the still-unsafe row was not preserved for repair"
        finally:
            svc.stop()


class TestARefusalDoesNotDestroyTheStore:
    """A whole-store refusal must not overwrite the operator's file with nothing.

    Scoped to the WHOLE-STORE arm only, which is the one this class still covers: a host
    that cannot compose its credential policy, so no row's addressing fields can be
    vetted. ``_loops`` is then empty because nothing could be checked rather than because
    the store is empty, so persisting it would delete every row. Every write raises
    ``AutoNudgeStoreUnvetted`` instead.

    A SINGLE unusable row does NOT arm that refusal: it is QUARANTINED instead, because
    dropping it deleted the entry the operator was told to repair, and refusing the whole
    store would have disarmed its healthy siblings. That arm is covered by
    ``TestCredentialShapedAddressingFieldsAreRefused``, which also discriminates the two
    outcomes -- a clean row still arms there, which a whole-store refusal would prevent.

    The refusal itself keeps its own tests
    (``TestCredentialShapedAddressingFieldsAreRefused`` and
    ``TestTheAddressingGuardUsesTheActiveCredentialPolicy``).
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    class _BrokenPolicy:
        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    @staticmethod
    def _install(policy) -> None:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import build_default_context, set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=policy))

    def _write(self, tmp_path, rows) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": rows}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_an_unvettable_store_refuses_the_write_instead(self, tmp_path) -> None:
        """Arm two, and the one that matters: the file must survive untouched.

        This is the arm the old machinery existed for. Without the write refusal the
        subtraction WOULD have deleted the operator's store, because every row is
        refused here and the payload is therefore empty.
        """
        self._write(tmp_path, [{"id": "a", "slot_key": "chat-1-1", "message": "one"}])
        original = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
        self._install(self._BrokenPolicy())

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc.list_all() == [], "a loop armed on an unvettable store"
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            after = (tmp_path / "autonudge.json").read_text(encoding="utf-8")
            assert after == original, (
                "the store was overwritten while unvettable -- the write refusal is the "
                "only thing standing between this state and total data loss"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_healthy_store_still_persists(self, tmp_path) -> None:
        """PRESERVED: the refusal must not block ordinary writes."""
        self._write(tmp_path, [])
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            armed = await svc.add(slot_key="chat-9-9", message="keep going")
            svc._write_state(svc._serialize_state())
            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert [r["id"] for r in on_disk["loops"]] == [
                armed.id
            ], f"a healthy write did not land: {on_disk!r}"
        finally:
            svc.stop()


class TestTheObserverBroadcastIsScrubbed:
    """Opus 4.8 (FINDING): the ``autonudge_state`` WS broadcast leaked ``message``.

    ``_serialize`` closed the REST path, but the observer shipped ``loop.message``
    verbatim to every connected dashboard client, where ``AutoNudgePopover`` renders
    it raw. Three producers reach ``svc.add`` without the authorizer, so a
    credential could arrive and go straight out over the socket.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def test_the_observer_scrubs_the_message_before_broadcasting(self) -> None:
        """Source-level: the payload must not interpolate ``loop.message`` raw.

        A cheap structural guard that nobody re-inlines the raw value. The
        BEHAVIOUR is proved in ``TestNonStringMessageDoesNotDropTheObserverBroadcast``
        by driving the real observer; this arm only pins the call site, so it
        asserts the scrub CALL rather than the redactor pair -- the pair moved into
        ``scrub_loop_text`` when the socket and REST surfaces were made to share one
        definition.

        The two surfaces sharing ONE rule is the property this pair of tests exists to
        protect. Requiring this frame to spell the same scrub
        call as REST; the frame now DERIVES its payload from the REST projection itself,
        so sharing is structural rather than duplicated and there is no spelling left to
        diverge. What must not come back is a field-by-field rebuild, which is what the
        assertions below pin.
        """
        import inspect

        src = inspect.getsource(gw)
        # Anchor on the payload ASSEMBLY, not a byte window around the event name:
        # the structured-monitor work put the payload dict, the monitor block and the
        # owner-scoped broadcast selection between the scrub and the event name, so a
        # fixed-size window silently stopped covering the call site.
        start = src.find("loop_payload: dict[str, Any] = {")
        assert start != -1, "the broadcast payload moved -- this test is measuring nothing"
        end = src.find('"autonudge_state"', start)
        assert end != -1, "the broadcast site moved -- this test is measuring nothing"
        block = src[start:end]
        assert (
            "_serialize_loop_for_clients(loop)" in src[:start][-1200:]
        ), "the payload is no longer derived from the REST projection, so it can diverge"
        assert '"message": loop.message' not in block, "the raw message is still interpolated"
        assert "scrub_loop_text(" not in block, (
            "the frame hand-scrubs a field again instead of taking the projection's "
            "output, which is how one surface gets a new field and the other does not"
        )

    def test_the_broadcast_carries_the_redaction_flag(self) -> None:
        """GPT 5.6 (BLOCKING): scrubbing without the flag re-opens the overwrite.

        The socket frame REPLACES the REST-fetched loop wholesale --
        ``ChatPage`` does ``setAutoNudgeLoop(detail.loop ?? null)`` and hands that
        object to ``AutoNudgePopover`` as ``loop``. So a payload that scrubs
        ``message`` but omits ``message_redacted`` leaves the flag ``undefined``
        after any frame: the masked-credential notice stops rendering and
        ``editsRedactedGoal`` goes false, disarming the confirm gate. An edit then
        stores the mask over the original instruction with no warning -- the exact
        loss the REST flag exists to prevent.

        The truthiness must match the REST projection, which compares the SERVED
        value against the STORED one rather than asking whether a scrubber ran. This
        originally pinned a COPY of that comparison in this frame. It now pins the
        stronger property Design asked for: the frame is DERIVED from that projection,
        so the two surfaces cannot hold different formulas at all, and a newly added
        free-text field is scrubbed on both or neither.
        """
        import inspect

        src = inspect.getsource(gw)
        start = src.find("loop_payload: dict[str, Any] = {")
        assert start != -1, "the broadcast payload moved -- this test is measuring nothing"
        end = src.find('"autonudge_state"', start)
        assert end != -1, "the broadcast site moved -- this test is measuring nothing"
        block = src[start:end]
        assert (
            '"message_redacted"' in block
        ), "the socket payload omits message_redacted, so a frame disarms the overwrite guard"
        assert "projected[key]" in block, (
            "the socket payload is built field-by-field again rather than derived from "
            "the REST denylist projection, so the two surfaces can disagree"
        )
        derivation = src.find("_serialize_loop_for_clients(loop)")
        assert derivation != -1 and derivation < start, (
            "the payload no longer comes from the shared projection, so message_redacted "
            "is not keyed on served-differs-from-stored like REST"
        )

    @pytest.mark.asyncio
    async def test_a_settings_only_save_does_not_claim_the_goal_was_ignored(self) -> None:
        """Opus 4.8 + First Principles + a maintainer: a routine save warned falsely.

        Driven through the HANDLER, not the helper. The old arm read
        ``svc.update.await_args.kwargs``, so the handler's own construction of the
        response flag was never exercised -- which is why this shipped.

        The popover omits ``message`` on an interval-only save
        (``if (loop && message !== (loop.message ?? '')) patch.message = message``) and
        every stop sends ``{"active": false}`` alone. Deriving the flag from the request
        body then read key-absent as ``None`` and reported a drop, so the client ran
        ``setMessageIgnored(true); return`` and left the popover open on a save that
        fully succeeded.
        """
        stored = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="keep going")
        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _s=stored: _s if _id == _s.id else None
        svc.update = AsyncMock(return_value=stored)

        captured: dict = {}

        class _Resp:
            def __init__(self, body, status=200):
                captured["body"] = body
                captured["status"] = status

        request = SimpleNamespace(
            match_info={"loop_id": "loop-1"},
            remote="127.0.0.1",
            json=AsyncMock(return_value={"idle_secs": 120, "max_cycles": 0, "active": True}),
        )

        with (
            patch.object(autonudge_handlers, "_autonudge_get", lambda: svc),
            patch.object(autonudge_handlers.web, "json_response", _Resp),
        ):
            await autonudge_handlers.api_autonudge_update(request)

        assert captured["status"] == 200, f"the interval-only save was refused: {captured}"
        assert (
            "message_ignored" not in captured["body"]
        ), f"an interval-only save reported the goal as ignored: {captured['body']}"

    @pytest.mark.asyncio
    async def test_a_stop_does_not_claim_the_goal_was_ignored(self) -> None:
        """A maintainer: every stop submits ``{"active": false}`` and no ``message``.

        The sibling arm above covers the interval-only save. This covers the stop, which
        is the other body carrying no ``message`` key -- named separately because it is
        the one path a client takes on EVERY stop, so a false "goal ignored" there would
        surface constantly.

        Driven through the handler, so the response the client actually parses is what is
        asserted. Reading the flag off ``svc.update.await_args`` would pass even if the
        handler built the payload wrongly, which is exactly how the original shipped.
        """
        stored = NudgeLoop(id="loop-1", slot_key="chat-1-123", message="keep going")
        svc = MagicMock()
        svc.list_all = lambda: [stored]
        svc.get_by_id = lambda _id, _s=stored: _s if _id == _s.id else None
        svc.update = AsyncMock(return_value=stored)

        captured: dict = {}

        class _Resp:
            def __init__(self, body, status=200):
                captured["body"] = body
                captured["status"] = status

        request = SimpleNamespace(
            match_info={"loop_id": "loop-1"},
            remote="127.0.0.1",
            json=AsyncMock(return_value={"active": False}),
        )

        with (
            patch.object(autonudge_handlers, "_autonudge_get", lambda: svc),
            patch.object(autonudge_handlers.web, "json_response", _Resp),
        ):
            await autonudge_handlers.api_autonudge_update(request)

        assert captured["status"] == 200, f"the stop was refused: {captured}"
        assert "message_ignored" not in captured["body"], (
            f"a stop reported the goal as ignored: {captured['body']}. The client runs "
            "setMessageIgnored(true) and leaves the popover open on a stop that worked"
        )

    def test_the_rest_projection_still_keys_the_flag_the_same_way(self) -> None:
        """NEGATIVE CONTROL: the two surfaces must not drift apart again.

        Fails if the REST projection stops deriving the flag by comparing the served
        value to the stored one -- which is what would make the socket spelling above
        a different rule wearing the same name.
        """
        import inspect

        rest = inspect.getsource(autonudge_handlers)
        assert (
            'out["message_redacted"] = out.get("message") != getattr(loop, "message", None)' in rest
        ), "the REST projection no longer derives the flag from served-differs-from-stored"


class TestTheBroadcastCarriesThePatchBaseline:
    """A fire on the active slot must not break the next genuine goal edit.

    ``ChatPage`` replaces the popover's loop with the ``autonudge_state`` payload, and
    the popover echoes ``message_fingerprint`` back as its stale-baseline token. The
    broadcast enumerates its keys, so a field present in the REST projection is dropped
    unless named -- and a dropped token reads as an empty baseline, which the store
    refuses 409 even though the stored goal never moved.
    """

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    async def _real_observer(self):
        """The gateway's OWN ``_observer`` closure, not a re-implementation."""
        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
            orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        ds = MagicMock()
        ds.broadcast_ws = MagicMock()
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_svc:
                inst = MagicMock()
                inst.start = AsyncMock()
                inst.subscribe = MagicMock()
                inst.remove = AsyncMock()
                mock_svc.return_value = inst
                await orch._init_autonudge()
        return inst.subscribe.call_args.args[0], ds

    @pytest.mark.asyncio
    async def test_an_edit_after_a_broadcast_is_not_refused_as_stale(
        self, tmp_path, audits
    ) -> None:
        observer, ds = await self._real_observer()
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-1-1785", message="the original goal")
            svc.subscribe(observer)
            # A cycle fires on the active slot: the real update path emits to the observer.
            await svc.update(armed.id, idle_secs=600)

            assert ds.broadcast_ws.called, "no autonudge_state broadcast was emitted"
            _topic, payload = ds.broadcast_ws.call_args.args
            served = payload["loop"]
            # Exactly what the popover does with the replaced loop.
            echoed = served.get("message_fingerprint") or ""

            _loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="a genuine later edit",
                expect_fingerprint=echoed,
                source="dashboard",
            )
            assert status == 200, (
                f"a genuine goal edit was refused after a broadcast ({status}, {error}) -- "
                "the payload carried no baseline token"
            )
            assert svc.get_by_id(armed.id).message == "a genuine later edit"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_bogus_baseline_is_still_refused(self, tmp_path, audits) -> None:
        """NEGATIVE CONTROL: the guard must still bite, or the fix above is vacuous."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-1-1786", message="the original goal")
            _loop, _error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="a clobbering edit",
                expect_fingerprint="a token from a goal since replaced",
                source="dashboard",
            )
            assert status == 409, "the stale-baseline guard stopped biting"
            assert svc.get_by_id(armed.id).message == "the original goal"
        finally:
            svc.stop()


class TestNonStringMessageDoesNotDropTheObserverBroadcast:
    """The observer scrub must survive a non-string ``message``.

    Both redactors raise ``TypeError: expected string or bytes-like object`` on a
    list, dict, int or ``None`` -- measured directly, all four. So any scrub on
    this path that took ``message`` unguarded would raise for a STORED non-string
    on ANY update, even one that only changes ``idle_secs``.

    That failure mode would be DATA LOSS rather than an error the operator sees:
    ``AutoNudgeService._emit`` wraps every observer in ``except Exception`` and
    only logs, so nothing 500s -- the ``autonudge_state`` broadcast would simply
    be dropped and the dashboard would stop seeing that loop's updates. That is
    why the primary arm asserts the broadcast HAPPENED; a test that only exercised
    the coercion helper would pass while the socket stayed mute.

    A non-string ``message`` is reachable: ``_load`` refuses a non-string
    ADDRESSING field but coerces the text ones at the sink, and the dataclass
    annotation is not enforced on ``NudgeLoop(**raw)``, so a hand-edited store
    arms such a loop. The authorizer redacts a message supplied BY a PATCH, which
    is a different value -- it never touches one already on the loop.
    """

    SECRET = "AKIA" + "IOSFODNN7EXAMPLE"

    async def _real_observer(self):
        """The gateway's OWN ``_observer`` closure, not a re-implementation."""
        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
            orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
        ds = MagicMock()
        ds.broadcast_ws = MagicMock()
        orch.dashboard_state = ds
        with patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True):
            with patch("kiro_crew.slack.gateway.AutoNudgeService") as mock_svc:
                inst = MagicMock()
                inst.start = AsyncMock()
                inst.subscribe = MagicMock()
                inst.remove = AsyncMock()
                mock_svc.return_value = inst
                await orch._init_autonudge()
        return inst.subscribe.call_args.args[0], ds

    def _store_with(self, tmp_path, message) -> AutoNudgeService:
        """Arm a loop straight from the store, the way a hand edit does."""
        (tmp_path / "autonudge.json").write_text(
            json.dumps(
                {
                    "loops": [
                        {
                            "id": "loop-nonstr",
                            "slot_key": "chat-1-1785",
                            "message": message,
                            "idle_secs": 300,
                            # Inactive on purpose: the emit under test is
                            # unconditional, and an armed loop would schedule real
                            # timers this test has no use for.
                            "active": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()
        return svc

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(["AKIA" + "IOSFODNN7EXAMPLE"], id="list"),
            pytest.param({"k": "AKIA" + "IOSFODNN7EXAMPLE"}, id="dict"),
            pytest.param(None, id="none"),
            pytest.param(42, id="int"),
        ],
    )
    async def test_update_still_broadcasts_when_the_stored_message_is_not_a_string(
        self, tmp_path, stored
    ) -> None:
        """THE REPORTED PATH: stored non-string -> update -> broadcast survives."""
        observer, ds = await self._real_observer()
        svc = self._store_with(tmp_path, stored)
        assert svc._loops, "the loop did not arm -- this test would measure nothing"
        svc.subscribe(observer)

        # The real update path: _update_locked -> _update_unserialized ->
        # _emit("updated", loop) -> the observer above, through _emit's own
        # try/except. Only idle_secs changes; the non-string message is the value
        # ALREADY on the loop, which is what the finding describes.
        await svc.update("loop-nonstr", idle_secs=600)

        assert ds.broadcast_ws.called, (
            "the autonudge_state broadcast was DROPPED -- _emit swallowed an "
            "exception from the observer scrub"
        )
        topic, payload = ds.broadcast_ws.call_args.args
        assert topic == "autonudge_state"
        assert payload["event"] == "updated"
        assert self.SECRET not in str(payload), "the credential reached the socket payload"

    @pytest.mark.asyncio
    async def test_the_socket_message_matches_what_the_rest_surface_would_send(
        self, tmp_path
    ) -> None:
        """PARITY, the property the fix is built on: one function, two callers.

        Asserted against ``_serialize``'s output rather than a literal, so a future
        change to the rule cannot make the two surfaces disagree without failing
        here.
        """
        observer, ds = await self._real_observer()
        svc = self._store_with(tmp_path, [self.SECRET])
        svc.subscribe(observer)
        await svc.update("loop-nonstr", idle_secs=600)

        _topic, payload = ds.broadcast_ws.call_args.args
        rest = autonudge_handlers._serialize(svc._loops["loop-nonstr"])
        assert payload["loop"]["message"] == rest["message"]
        assert isinstance(payload["loop"]["message"], str)

    @pytest.mark.asyncio
    async def test_declared_scalars_are_not_coerced_on_the_socket_either(self, tmp_path) -> None:
        """NEGATIVE CONTROL: a fix that stringified everything would pass the above.

        Clients compare and do arithmetic on these, so ``600`` must not arrive as
        ``"600"``.
        """
        observer, ds = await self._real_observer()
        svc = self._store_with(tmp_path, [self.SECRET])
        svc.subscribe(observer)
        await svc.update("loop-nonstr", idle_secs=600)

        _topic, payload = ds.broadcast_ws.call_args.args
        assert payload["loop"]["idle_secs"] == 600
        assert isinstance(payload["loop"]["idle_secs"], int)
        assert isinstance(payload["loop"]["active"], bool)

    def test_emit_swallows_an_observer_exception(self) -> None:
        """Pins the mechanism the primary arm relies on.

        If ``_emit`` ever let an observer exception propagate, a raise would become
        a visible 500 rather than a dropped broadcast -- a different defect, and the
        primary arm's failure message would then be wrong about what went wrong.
        """
        svc = AutoNudgeService()
        boom = MagicMock(side_effect=TypeError("expected string or bytes-like object"))
        svc.subscribe(boom)
        svc._emit("updated", _loop())  # must not raise
        boom.assert_called_once()

    def test_both_redactors_reject_a_non_string(self) -> None:
        """Why the coercion branch is load-bearing rather than defensive."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        for probe in ([self.SECRET], {"k": self.SECRET}, 42, None):
            for fn in (redact_exfiltration_urls, redact_credentials):
                with pytest.raises(TypeError):
                    fn(probe)


class TestNonStringFieldsCannotBypassTheScrub:
    """GPT 5.6 (BLOCKING): ``not isinstance(value, str)`` was an EARLY-OUT.

    ``_serialize``'s loop skipped any non-string value, so an agent-written
    ``message: ["AKIA..."]`` rode straight through to ``GET /api/autonudge`` and
    the ``autonudge_state`` WS broadcast. Measured before the fix: the loop LOADED
    and the serialized payload carried ``message = ['AKIAIOSFODNN7EXAMPLE']``.

    Two halves, because the right answer differs per field:

    * ADDRESSING fields (``id``/``slot_key``) are exempt from scrubbing BY DESIGN,
      so a non-string there rides both the exemption and the early-out -- and a
      list ``id`` is unhashable, so ``self._loops[loop.id] = loop`` raises
      uncaught, escapes ``_load`` and ``start()``, and NO loop arms at all. Those
      are REFUSED at load, like a credential-shaped one.
    * Other ``str``-declared fields are REDACT-COERCED at the sink. Blanking would
      destroy the operator's ability to see what is wrong; coercing scrubs the
      credential and keeps the value inspectable, and the field is declared ``str``
      so a string is what the contract already promises.

    The nine int/float/bool fields must pass through UNTOUCHED -- coercing
    ``idle_secs`` to ``"300"`` would break every client that compares it.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _row(self, **over) -> dict:
        row = {"id": "n1", "slot_key": "chat-1-2", "message": "ok", "idle_secs": 300}
        row.update(over)
        return row

    def _write(self, tmp_path, row) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [row]}), encoding="utf-8"
        )

    @pytest.mark.asyncio
    async def test_a_non_string_message_is_not_served_raw(self, tmp_path) -> None:
        """Fails on the unmodified tree: the payload carries the list verbatim."""
        self._write(tmp_path, self._row(message=[self.SECRET]))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert "n1" in svc._loops, "fixture wrong -- the row did not load"
            payload = json.dumps(autonudge_handlers._serialize(svc._loops["n1"]))
            assert self.SECRET not in payload, "a non-string message leaked the credential"
            assert "[REDACTED: credential]" in payload, "the value was not redact-coerced"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["banner", "stopped_reason", "stop_sentinel_path"])
    async def test_every_other_str_field_is_redact_coerced(self, tmp_path, field) -> None:
        """One arm per field: one break cannot validate the whole denylist."""
        self._write(tmp_path, self._row(**{field: [self.SECRET]}))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            if "n1" not in svc._loops:
                pytest.skip(f"{field} is refused at load, not coerced -- covered elsewhere")
            payload = json.dumps(autonudge_handlers._serialize(svc._loops["n1"]))
            assert self.SECRET not in payload, f"a non-string {field} leaked the credential"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["id", "slot_key"])
    async def test_a_non_string_addressing_field_is_refused(self, tmp_path, field) -> None:
        """Half 1. For ``id`` this ALSO fixes an uncaught TypeError that armed no
        loops at all; ``start()`` must complete rather than raise."""
        self._write(tmp_path, self._row(**{field: [self.SECRET]}))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            assert svc._loops == {}, f"a non-string {field} was armed"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_numeric_and_bool_fields_keep_their_types(self, tmp_path) -> None:
        """Negative control, and the one that matters most.

        A fix that coerced every non-string would pass every arm above while
        turning ``idle_secs`` into ``"300"`` and ``active`` into ``"True"``,
        breaking every client that compares them. Pins all nine.
        """
        self._write(tmp_path, self._row())
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            out = autonudge_handlers._serialize(svc._loops["n1"])
            for name, want in (
                ("idle_secs", int),
                ("max_cycles", int),
                ("cycle_count", int),
                ("max_runtime_secs", int),
                ("active", bool),
                ("approval_stalled", bool),
                ("last_fire_ts", float),
                ("created_ts", float),
                ("next_due_ts", float),
            ):
                assert isinstance(out[name], want), f"{name} became {type(out[name]).__name__}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_clean_loop_round_trips_unchanged(self, tmp_path) -> None:
        """Negative control: the coercion must not rewrite ordinary values."""
        self._write(tmp_path, self._row(message="just keep going"))
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            out = autonudge_handlers._serialize(svc._loops["n1"])
            assert out["message"] == "just keep going"
            assert out["id"] == "n1" and out["slot_key"] == "chat-1-2"
        finally:
            svc.stop()


class TestAStagedMonitorWriteKeepsQuarantine:
    """A staged monitor snapshot is a WHOLESALE write, so it must carry quarantine.

    Two builders reach ``_write_state``. ``_serialize_state`` re-emits ``quarantined``;
    the monitor snapshot builder did not, so a staged transition on a HEALTHY loop
    deleted the row an operator was told to repair.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_staged_monitor_snapshot_still_carries_quarantined(self, tmp_path) -> None:
        """A monitor payload keeps quarantined rows the transition never touched."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [good, unusable]}),
            encoding="utf-8",
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"
            loop = svc._loops["keep"]
            svc._write_state(svc._monitor_snapshot_with_replacement(loop, loop))
            assert [r.get("id") for r in _held_aside_rows(tmp_path)] == [
                self.SECRET
            ], "a staged monitor write dropped the held-aside row from the sidecar"
        finally:
            svc.stop()


class TestADowngradeCannotDeleteQuarantinedRows:
    """A build predating ``quarantined`` must not be able to destroy a held-aside row.

    Such a build writes ``autonudge.json`` WHOLESALE and knows nothing of the key, so
    an embedded-only copy is deleted by its next write. The sidecar is what survives.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_quarantined_row_survives_an_older_builds_wholesale_write(
        self, tmp_path
    ) -> None:
        """A row held aside is recoverable after a downgrade drops the embedded copy."""
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": [good, unusable]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"
            # Persist once so the sidecar exists, exactly as a live write would.
            svc._write_state(svc._serialize_state())
            sidecar = tmp_path / "autonudge.quarantine.json"
            assert sidecar.exists(), "the write sink did not persist the quarantine sidecar"

            # THE DOWNGRADE: an older build re-emits the store with no `quarantined`
            # key and never touches the sidecar.
            store.write_text(json.dumps({"version": 1, "loops": [good]}), encoding="utf-8")
            assert "quarantined" not in json.loads(store.read_text(encoding="utf-8"))
        finally:
            svc.stop()

        after = AutoNudgeService(base_dir=tmp_path)
        try:
            after._load()
            assert [r.get("id") for r in after._quarantined] == [self.SECRET], (
                "a downgrade permanently deleted the quarantined row; "
                f"recovered={[r.get('id') for r in after._quarantined]!r}"
            )
        finally:
            after.stop()


class TestTheQuarantineSidecarIsWrittenSafely:
    """The sidecar must not be able to corrupt or crash the store it protects.

    Ordering: it is written BEFORE the main store, so a sidecar failure leaves the old
    consistent file. Shape: a non-object sidecar is ignored rather than fatal.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _store_with_one_unusable_row(self, tmp_path):
        good = {"id": "keep", "slot_key": "chat-1-1", "message": "fine", "idle_secs": 300}
        unusable = {"id": self.SECRET, "slot_key": "chat-9-9", "message": "x", "idle_secs": 300}
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": [good, unusable]}), encoding="utf-8")
        return store

    @pytest.mark.asyncio
    async def test_a_refused_store_does_not_record_a_delivered_cycle(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): a runtime sidecar refusal left delivered cycles undurable.

        ``_persist_soon`` only LOGS a failed persist, so once the sidecar refusal latch is
        set every write raises while the post-fire ``cycle_count`` bump keeps advancing in
        memory. Cycles are then spent against a count no restart will have seen, so
        ``max_cycles`` bounds nothing durable. Firing must stop instead.
        """
        import kiro_crew.autonudge as _an

        fired: list[object] = []

        async def on_fire(loop):
            fired.append(loop)
            return True

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._on_fire = on_fire
        try:
            await svc.start()
            # Real sleep here, so arming does NOT fire before the latch is set.
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
            svc._timers[loop.id].cancel()

            async def _nosleep(_secs):
                return None

            monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
            # The sidecar became unreadable while the service was live.
            svc._load_refused = True
            await svc._timer(loop)

            assert not fired, "a cycle was delivered while no write could be recorded"
            assert svc._loops[loop.id].cycle_count == 0, (
                "cycle_count advanced in memory while persistence was refused, so the "
                "budget a restart reads is already wrong"
            )
        finally:
            # The latch is the condition under test, not the teardown: leaving it set
            # makes a teardown persist raise and mask the assertions above.
            svc._load_refused = False
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_non_object_root_refuses_instead_of_crashing_boot(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a non-dict root bypassed the list guard and aborted boot.

        ``"loops" in []`` is False, so a hand-edited list or scalar root reached the row
        loop and raised out of the unguarded ``_load`` that ``start()`` calls. Refusing is
        the same fail-closed answer the not-a-list arm already gives.
        """
        (tmp_path / "autonudge.json").write_text("[]", encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert svc._load_refused is True, "a non-object root did not refuse writes"
        assert svc._loops == {}, "a non-object root armed something"

    @pytest.mark.asyncio
    async def test_one_coincidental_field_does_not_retire_an_unrelated_held_row(
        self, tmp_path
    ) -> None:
        """GPT 5.6: a held row with an unknown field plus one match read as 'repaired'.

        Unknown keys were skipped, so a row this code cannot compare was judged against
        whatever remained -- one coincidentally-equal field stood for the whole row and
        deleted its only durable copy. An unknown key must never match.
        """
        held = {
            "id": self.SECRET,
            "idle_secs": 300,
            "some_future_field": "carried by a newer writer",
        }
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-2",
            "message": "a different instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        ids = [row.get("id") for row in svc._quarantined]
        assert (
            self.SECRET in ids
        ), f"an unrelated held row was retired on a coincidental match: {ids!r}"

    @pytest.mark.asyncio
    async def test_a_repaired_id_keeps_its_held_aside_copy(self, tmp_path) -> None:
        """HOLD AND WARN, on the id path too: only the operator removes a held row.

        Deleting it here needed a fuzzy match on the fields a repair leaves alone, and
        two rows differing only in an unsafe id match the SAME repaired loop. The load
        warning tells the operator to MOVE the row, which empties the sidecar without
        anything guessing.
        """
        held = {
            "id": self.SECRET,
            "slot_key": "chat-9-9",
            "message": "the operator instruction",
            "idle_secs": 300,
        }
        repaired = {
            "id": "loop-clean-id",
            "slot_key": "chat-9-9",
            "message": "the operator instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [repaired]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert "loop-clean-id" in svc._loops, "the repaired row did not arm from the store"
        ids = [row.get("id") for row in svc._quarantined]
        assert (
            self.SECRET in ids
        ), f"the held copy was deleted -- only the operator may remove it: {ids!r}"

    @pytest.mark.asyncio
    async def test_a_repaired_row_keeps_its_held_aside_copy(self, tmp_path) -> None:
        """HOLD AND WARN: nothing auto-retires a held row, because matching is fuzzy.

        The auto-retire matcher was deleted: repair-by-move already empties the sidecar,
        so it earned nothing, while a false positive deleted the held row's only durable
        copy. The residual cost is a repeated load-time warning and a stale sidecar row --
        recoverable, unlike a deletion.
        """
        held = {
            "id": "loop-repairable",
            "slot_key": self.SECRET,
            "message": "x",
            "idle_secs": 300,
        }
        repaired = {
            "id": "loop-repairable",
            "slot_key": "chat-4-4",
            "message": "x",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [repaired]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert "loop-repairable" in svc._loops, "the repaired row did not arm from the store"
        keys = [row.get("slot_key") for row in svc._quarantined]
        assert (
            self.SECRET in keys
        ), f"the held copy was deleted -- only the operator may remove it: {keys!r}"

    @pytest.mark.asyncio
    async def test_a_row_in_both_files_is_quarantined_once_not_twice(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a failed replacement made the next load DUPLICATE a row.

        Once the sidecar write has landed and the main-store replacement then fails, the
        unsafe row is in BOTH files. The load loop reaches it twice and appended it twice,
        so each failed replacement accumulated another copy of the same quarantine record.
        """
        unusable = {
            "id": self.SECRET,
            "slot_key": "chat-9-9",
            "message": "x",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [unusable]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unusable]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            held = [row.get("id") for row in svc._quarantined]
            assert len(held) == 1, (
                "the row present in BOTH files was quarantined twice, so every failed "
                f"replacement accumulates another duplicate record: {len(held)} copies"
            )
            assert not svc._loops, "the unsafe row armed instead of being held aside"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_sidecar_write_leaves_the_store_file_untouched(self, tmp_path) -> None:
        """ORDERING: the main store is not replaced when the sidecar cannot be written."""
        store = self._store_with_one_unusable_row(tmp_path)
        before = store.read_text(encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            def _boom() -> None:
                raise OSError("sidecar volume is full")

            svc._write_quarantine_sidecar = _boom  # type: ignore[method-assign]
            with pytest.raises(OSError):
                svc._write_state(svc._serialize_state())
            assert store.read_text(encoding="utf-8") == before, (
                "the store was replaced before the sidecar was durable, so a sidecar "
                "failure left committed disk state inconsistent"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_non_object_sidecar_refuses_the_store_rather_than_arming(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): tolerating a bad sidecar armed loops that could not persist.

        The OPPOSITE assertion -- that a list-shaped sidecar is
        "ignored rather than fatal" and the store's own quarantined row survived in
        memory. That tolerance was the defect: writes were already refused, so a loop
        armed under it delivers a cycle it cannot record, and the next restart re-fires
        that cycle past its own cap.

        So the contract is now REFUSE, and the assertions below cover both halves of it:
        nothing arms, and the file survives for the operator to repair. `_load` must
        still not RAISE -- a startup crash would be a third failure mode.
        """
        self._store_with_one_unusable_row(tmp_path)
        sidecar = tmp_path / "autonudge.quarantine.json"
        # A list, not an object -- `raw.get` on this would raise AttributeError.
        sidecar.write_text("[]", encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()  # must not raise
            assert svc._load_refused is True, "a non-object sidecar left writes enabled"
            assert not svc._loops, (
                "loops armed under a refused store; a delivered cycle cannot record "
                f"itself, so a restart repeats it. armed={sorted(svc._loops)!r}"
            )
            assert not svc._quarantined, (
                "rows were held in memory under a refused store, which cannot be "
                "persisted and so is lost silently on restart"
            )
            with pytest.raises(_an.AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert (
                _moved_aside_sidecar(tmp_path).read_text(encoding="utf-8") == "[]"
            ), "the sidecar bytes were lost, so the operator has nothing to repair"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_sidecar_rename_is_durable_before_the_store_drops_the_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the sidecar's RENAME was never flushed.

        The bytes were fsynced and the rename was atomic, so the ordering test above
        passed -- but an atomic rename is only durable once the PARENT DIRECTORY is
        synced. Until then a power-off can return from the replacement and still come
        back to the old directory entry. The main store lands immediately afterwards
        and drops the quarantined rows, so those rows' only remaining copy is a name
        recorded nowhere.

        The assertion is on ORDER, not on the call's existence: syncing the directory
        after the store has already dropped the rows would close no window.
        """
        store = self._store_with_one_unusable_row(tmp_path)
        events: list[str] = []
        real_fsync_dir = _an.fsync_dir
        real_replace = _an.replace_with_retry

        def _record_fsync(path, **kwargs):
            events.append(f"fsync_dir:{Path(path).name}")
            return real_fsync_dir(path, **kwargs)

        def _record_replace(src, dst):
            events.append(f"replace:{Path(dst).name}")
            return real_replace(src, dst)

        monkeypatch.setattr(_an, "fsync_dir", _record_fsync)
        monkeypatch.setattr(_an, "replace_with_retry", _record_replace)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            svc._write_state(svc._serialize_state())

            assert (
                f"replace:{store.name}" in events
            ), f"the main store never landed, so this run proves nothing: {events!r}"
            sidecar_synced = [i for i, e in enumerate(events) if e.startswith("fsync_dir:")]
            assert sidecar_synced, (
                "the sidecar's parent directory was never synced, so its rename is not "
                f"durable when the store drops the quarantined rows: {events!r}"
            )
            assert sidecar_synced[0] < events.index(f"replace:{store.name}"), (
                "the directory sync happened AFTER the store replacement, so there is "
                f"still a window where the held rows exist nowhere durable: {events!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_defaulted_field_is_not_identity_evidence(self, tmp_path) -> None:
        """A hand-edited row matched an unrelated loop on ``active`` alone.

        The gate accepted ANY non-addressing field as corroboration, and ``active``
        defaults to True -- so a two-field row carrying an unsafe ``id`` plus a
        ``slot_key`` a live loop happens to share was retired and compacted away. Only a
        NO-DEFAULT field is identity evidence, and a row lacking one is retained.
        """
        sparse = {"id": self.SECRET, "slot_key": "chat-live-1", "active": True}
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-1",
            "message": "a wholly different instruction",
            "active": True,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [sparse]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert (
                svc._quarantined
            ), "a row with no no-default field was retired on a defaulted boolean"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_two_held_rows_matching_one_repair_are_both_kept(self, tmp_path) -> None:
        """An ambiguous repair match must retire NEITHER row, not both.

        ``_is_repair_of`` answered "some armed loop matches", so two held rows differing
        only in their unsafe ``id`` both matched the SAME repaired loop and both were
        retired -- deleting the only durable copy of the one that was never repaired.
        """
        first = {"id": self.SECRET, "slot_key": "chat-1", "message": "same", "idle_secs": 300}
        second = {
            "id": f"{self.SECRET}-other",
            "slot_key": "chat-1",
            "message": "same",
            "idle_secs": 300,
        }
        repaired = {"id": "repaired-1", "slot_key": "chat-1", "message": "same", "idle_secs": 300}
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [first, second]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [repaired]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            ids = [row.get("id") for row in svc._quarantined]
            assert (
                len(svc._quarantined) == 2
            ), f"an ambiguous match retired a row it could not have repaired: {ids!r}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_sparse_held_row_is_not_mistaken_for_a_repair(self, tmp_path) -> None:
        """Absence of contradicting fields must not read as evidence of a repair.

        The comparison iterated only the fields PRESENT in the held row, so a two-field
        hand-edited row -- an unsafe ``id`` plus a ``slot_key`` that happens to match a
        live entry -- had nothing left to contradict it and was retired, deleting its only
        durable copy. Sparse hand-edited rows are the expected input class here, so the
        match now needs positive evidence: a non-addressing field that actually agrees.
        """
        sparse = {"id": self.SECRET, "slot_key": "chat-live-1"}
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-1",
            "message": "a wholly different instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [sparse]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            keys = [row.get("slot_key") for row in svc._quarantined]
            assert "chat-live-1" in keys, (
                "a sparse row was retired as a repair of an unrelated loop sharing its "
                f"slot_key; still held: {svc._quarantined!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_post_commit_dir_sync_failure_does_not_report_a_rollback(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the rename is the commit point, so nothing past it may raise.

        ``fsync_dir`` sat INSIDE the try that follows the rename, so a directory-sync
        error propagated -- the caller rolled its in-memory loop back while DISK KEPT the
        change, and a restart resurrected a mutation this process reported as rejected.
        The compaction below already carried this exact reasoning in a comment.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()

            compacted: list[int] = []
            monkeypatch.setattr(
                _an, "fsync_dir", lambda _p: (_ for _ in ()).throw(OSError("no fsync"))
            )
            monkeypatch.setattr(
                type(svc),
                "_compact_quarantine_sidecar",
                lambda self: compacted.append(1),
            )

            payload = {"version": 1, "loops": [{"id": "committed-1", "idle_secs": 300}]}
            svc._write_state(payload)  # must NOT raise

            on_disk = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
            assert (
                on_disk["loops"][0]["id"] == "committed-1"
            ), "the rename committed but the write was reported as failed"
            assert compacted == [], (
                "compaction ran after an unsynced rename; it DELETES rows, so a crash "
                "could drop a repaired row from both files"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_two_unsafe_addressing_fields_do_not_retire_on_field_residue(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): both addressing fields unsafe exempted BOTH from matching.

        The docstring claimed a safe addressing field must match, but nothing enforced it:
        with ``id`` AND ``slot_key`` both credential-shaped -- producible by hand-edit --
        every addressing field is skipped, so a sparse held row matched an unrelated armed
        loop on ``idle_secs`` alone and its only durable copy was deleted.
        """
        held = {"id": self.SECRET, "slot_key": self.SECRET, "idle_secs": 300}
        unrelated = {
            "id": "loop-unrelated",
            "slot_key": "chat-live-2",
            "message": "a different instruction",
            "idle_secs": 300,
        }
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"quarantined": [held]}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": [unrelated]}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        keys = [row.get("slot_key") for row in svc._quarantined]
        assert (
            self.SECRET in keys
        ), f"an unrelated loop retired the held row on field residue alone: {keys!r}"

    @pytest.mark.asyncio
    async def test_a_sidecar_missing_its_key_refuses_rather_than_reading_empty(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a dict with no ``quarantined`` key read as no rows.

        ``raw.get`` answers None, ``_rows_or_empty`` turns that into ``[]``, and the loader
        then reports nothing held aside -- so the next persist unlinks a file whose shape
        this process never actually understood. Absent-key must refuse, unlike an explicit
        empty list, which is legitimately empty.
        """
        (tmp_path / "autonudge.quarantine.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )

        svc = AutoNudgeService(base_dir=tmp_path)
        svc._load()

        assert svc._load_refused is True, (
            "a sidecar with no `quarantined` key was read as empty, so the next write "
            "would unlink it"
        )

    @pytest.mark.asyncio
    async def test_the_store_rename_is_durable_before_compaction_deletes_a_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 5.6 (BLOCKING): the MAIN STORE's rename was never dir-synced.

        The sidecar's own rename is dir-synced, and compaction then DELETES a held row
        from it -- so the deletion was durable while the store write meant to carry the
        repaired row forward was not. A power-off in that window came back to the old
        store directory entry with the sidecar copy already gone: the row is lost from
        both files. Asserted on ORDER, since syncing after compaction closes no window.
        """
        store = self._store_with_one_unusable_row(tmp_path)
        events: list[str] = []
        real_fsync_dir = _an.fsync_dir
        real_replace = _an.replace_with_retry

        def _record_fsync(path, **kwargs):
            events.append(f"fsync_dir:{Path(path).name}")
            return real_fsync_dir(path, **kwargs)

        def _record_replace(src, dst):
            events.append(f"replace:{Path(dst).name}")
            return real_replace(src, dst)

        monkeypatch.setattr(_an, "fsync_dir", _record_fsync)
        monkeypatch.setattr(_an, "replace_with_retry", _record_replace)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._quarantined, "fixture did not quarantine the credential-shaped row"

            compacted: list[str] = []
            real_compact = svc._compact_quarantine_sidecar

            def _record_compact():
                compacted.append("compact")
                events.append("compact")
                return real_compact()

            monkeypatch.setattr(svc, "_compact_quarantine_sidecar", _record_compact)
            svc._write_state(svc._serialize_state())

            assert compacted, "compaction never ran, so this run proves nothing"
            store_replace = events.index(f"replace:{store.name}")
            synced_after_store = [
                i for i, e in enumerate(events) if e.startswith("fsync_dir:") and i > store_replace
            ]
            assert synced_after_store, (
                "the store's parent directory was never synced after its rename, so the "
                f"store write is not durable when compaction deletes a row: {events!r}"
            )
            assert synced_after_store[0] < events.index("compact"), (
                "the store rename was synced only AFTER compaction, so a crash still "
                f"loses the repaired row from both files: {events!r}"
            )
        finally:
            svc.stop()


class TestAConfirmedGoalIsNotOverwrittenByAStaleClient:
    """The PATCH must compare the caller's baseline INSIDE the store's mutation lock.

    The popover re-checks the stored goal synchronously and then awaits the PATCH, so a
    second client committing in that window had its goal silently overwritten -- the
    endpoint was last-write-wins with no baseline at all. A check anywhere outside the
    lock only narrows that window, so these pin it to the lock itself.
    """

    @pytest.fixture()
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        events: list[dict] = []
        monkeypatch.setattr(
            authz,
            "sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
        )
        return events

    @pytest.mark.asyncio
    async def test_the_served_identifier_does_not_distinguish_two_masked_goals(
        self, tmp_path
    ) -> None:
        """The identifier served beside a redaction must not be an oracle for it.

        A digest over the raw goal let an authenticated reader who knows the
        surrounding template brute-force a low-entropy masked span OFFLINE: serve
        ``sha256(raw)`` next to the redaction and the mask is decorative. A random
        per-write token carries the same stale-baseline signal with no such leak.
        """
        template = "deploy with {} now"
        # One key-shaped literal, and a distinct same-shape variant derived from it, so
        # both goals redact to the identical projection.
        key_a = "AKIAIOSFODNN7EXAMPLE"
        key_b = key_a[:-1] + "F"
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            a = await svc.add(slot_key="chat-8-1", message=template.format(key_a))
            b = await svc.add(slot_key="chat-8-2", message=template.format(key_b))
            served_a = autonudge_handlers._serialize(a)
            served_b = autonudge_handlers._serialize(b)

            # A reader guessing the secret can recompute a CONTENT-derived identifier
            # and compare. It must not match what was served.
            guess = hashlib.sha256(
                template.format("AKIAIOSFODNN7EXAMPLE").encode("utf-8")
            ).hexdigest()[:32]
            assert served_a["message_fingerprint"] != guess, (
                "the served identifier is a digest of the raw goal -- an offline oracle "
                "against the redacted span"
            )
            # And it must still be a usable identity: distinct per loop, non-empty.
            assert served_a["message_fingerprint"], "no identity was served at all"
            assert served_a["message_fingerprint"] != served_b["message_fingerprint"]
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_rotated_secret_variant_is_not_mistaken_for_the_goal_already_seen(
        self, tmp_path, audits
    ) -> None:
        """The two goals differ ONLY inside the span redaction masks.

        Normalising a projection baseline to the stored text let these two collapse: the
        client had seen the OLD goal, the store already held the rotated one, and both
        render the same scrubbed projection -- so the stale write authorised itself.
        """
        # One key-shaped literal, and a distinct same-shape variant derived from it, so
        # the two goals differ only inside the mask.
        key_seen = "AKIAIOSFODNN7EXAMPLE"
        seen = f"deploy with {key_seen} now"
        rotated = f"deploy with {key_seen[:-1] + 'F'} now"
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-996", message=seen)
            seen_token = armed.goal_token
            await svc.update(armed.id, message=rotated)
            assert (
                armed.goal_token != seen_token
            ), "CONTROL: the write must mint a new identity, or nothing is detectable"

            _loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="clobbering goal",
                expect_fingerprint=seen_token,
                source="dashboard",
            )
            assert status == 409, f"a masked baseline authorised a stale write ({status}, {error})"
            assert (
                svc.get_by_id(armed.id).message == rotated
            ), "the rotated goal was overwritten by a client that had only seen the old one"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_current_fingerprint_still_succeeds(self, tmp_path, audits) -> None:
        """POSITIVE CONTROL: the token must not refuse an ordinary save."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-995", message="deploy with AKIAIOSFODNN7EXAMPLE")
            _loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="an edited goal",
                expect_fingerprint=armed.goal_token,
                source="dashboard",
            )
            assert status == 200, f"an ordinary save was refused ({status}, {error})"
            assert svc.get_by_id(armed.id).message == "an edited goal"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_stale_token_is_refused_under_the_lock(self, tmp_path) -> None:
        """The refusal is raised by the STORE, under its own mutation lock."""
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-999", message="original goal")
            with pytest.raises(AutoNudgeStaleBaseline):
                await svc.update(
                    armed.id,
                    message="clobbering goal",
                    expect_fingerprint="a token from a goal since replaced",
                )
            assert (
                svc.get_by_id(armed.id).message == "original goal"
            ), "a stale-baseline write overwrote the goal it never saw"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_baseline_read_before_the_lock_cannot_authorise_a_stale_write(
        self, tmp_path, audits
    ) -> None:
        """The row the authorizer read is NOT what the decision may rest on.

        Reproduces the window itself: the handed-down row still carries the old token
        while the store already holds the new one. Moving the comparison up to that read
        would pass this and re-open the defect, so it fails for the intended reason.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            armed = await svc.add(slot_key="chat-9-997", message="original goal")
            seen_token = armed.goal_token
            stale_row = SimpleNamespace(**{**armed.__dict__, "goal_token": seen_token})
            await svc.update(armed.id, message="a peer's newer goal")

            _loop, error, status = await authz.authorize_and_update_nudge(
                svc=svc,
                loop_id=armed.id,
                message="clobbering goal",
                expect_fingerprint=seen_token,
                source="dashboard",
                row=stale_row,
            )
            assert status == 409, f"a stale baseline was authorised (status={status}, {error})"
            assert (
                svc.get_by_id(armed.id).message == "a peer's newer goal"
            ), "the peer's goal was overwritten by a client that never saw it"
        finally:
            svc.stop()


class TestEveryChannelPersisterScrubsTheStoredTurn:
    """The persisted transcript row is an egress on every channel, not just Slack.

    ``_fire_slack_nudge`` scrubs the row it writes itself, and the dashboard nudge
    scrubs its own. The other channels do not write their own row: they hand a
    synthetic inbound to the dispatcher and the channel's ``_persist_turn`` writes
    it, so the store-sourced nudge text reached ``conv_log`` with nothing applied.

    Scrubbed at the SINK rather than at the door, because that is the only place the
    persisted copy is separable from the PROMPT. The prompt has already been
    consumed by the turn when ``_persist_turn`` runs, so redacting here cannot
    rewrite the instruction the model received -- the property the nudge depends on.

    Parametrized over all three channels that own a persister: a fix applied to only
    the two a reviewer happened to name leaves the third carrying the same hole.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _dispatcher_class(channel: str):
        if channel == "discord":
            from kiro_crew.discord.transport_dispatch import DiscordDispatcher

            return DiscordDispatcher
        if channel == "webex":
            from kiro_crew.webex.transport_dispatch import WebexDispatcher

            return WebexDispatcher
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        return TelegramDispatcher

    @pytest.mark.parametrize("channel", ["discord", "webex", "telegram"])
    def test_the_persisted_user_row_carries_no_credential(self, channel: str) -> None:
        """Fail-first: the row written to ``conv_log`` must not echo the stored secret."""
        cls = self._dispatcher_class(channel)
        rows: list[tuple[str, str]] = []

        def _append(_key, role, text, **_kw):
            rows.append((role, text))

        fake_log = MagicMock()
        fake_log.append = _append
        fake_log.append_if_absent = _append
        holder = SimpleNamespace(conv_log=fake_log)

        raw = f"[auto-nudge cycle 1]\ndeploy with {self.SECRET}"
        assert self.SECRET in raw, "fixture failed: the secret is not in the input"
        cls._persist_turn(holder, "chat-1-123", raw, "", False)

        user_rows = [text for role, text in rows if role == "user"]
        assert user_rows, f"fixture failed: {channel} persisted no user row to read"
        assert self.SECRET not in user_rows[0], (
            f"{channel} persisted the raw store-sourced turn: {user_rows[0]!r}. That row "
            "is served to dashboard readers, so a credential the ingress scan never saw "
            "reaches them verbatim"
        )

    @pytest.mark.parametrize("channel", ["discord", "webex", "telegram"])
    def test_a_clean_turn_is_persisted_unchanged(self, channel: str) -> None:
        """NEGATIVE CONTROL: the scrub must track CHANGE, not rewrite every row."""
        cls = self._dispatcher_class(channel)
        rows: list[tuple[str, str]] = []

        def _append(_key, role, text, **_kw):
            rows.append((role, text))

        fake_log = MagicMock()
        fake_log.append = _append
        fake_log.append_if_absent = _append
        holder = SimpleNamespace(conv_log=fake_log)

        clean = "[auto-nudge cycle 2]\njust keep going"
        cls._persist_turn(holder, "chat-1-123", clean, "", False)

        user_rows = [text for role, text in rows if role == "user"]
        assert user_rows, f"fixture failed: {channel} persisted no user row to read"
        assert user_rows[0] == clean, (
            f"{channel} altered a turn with nothing credential-shaped in it: "
            f"{user_rows[0]!r} != {clean!r}"
        )


# Every module that persists a transcript row with role "user". Pinned rather than
# derived so a NEW persister has to be added here deliberately.
_USER_ROW_PERSISTERS = frozenset(
    {
        "discord/transport_dispatch.py",
        "eval/runner.py",
        "feishu/transport_dispatch.py",
        "imessage/transport_dispatch.py",
        "llm_helpers.py",
        "slack/transport_dispatch.py",
        "taskrunner.py",
        "teams/transport_dispatch.py",
        "telegram/transport_dispatch.py",
        "webex/transport_dispatch.py",
        "wecom/transport_dispatch.py",
        "weixin/transport_dispatch.py",
        "whatsapp/transport_dispatch.py",
    }
)


def _appends_a_user_row(call: ast.Call) -> bool:
    """Does this call persist a ``ConversationLog`` row whose role is ``user``?

    Two shapes reach the same method and BOTH must be seen. Directly, the role is the
    second positional argument (``conv_log.append(key, "user", text)``). Indirectly, the
    bound method is handed to ``asyncio.to_thread`` and every argument shifts right by
    one, which is how the Slack dispatcher persists. Matching only the direct shape read
    Slack as unscrubbed while it was covered.

    The dashboard's in-memory slot is deliberately NOT matched: its ``append`` takes the
    role FIRST, and it is a different surface that stores what the user typed verbatim.
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "append":
        args = call.args
        return len(args) >= 2 and isinstance(args[1], ast.Constant) and args[1].value == "user"
    if isinstance(func, ast.Attribute) and func.attr == "to_thread":
        args = call.args
        return (
            len(args) >= 3
            and isinstance(args[0], ast.Attribute)
            and args[0].attr == "append"
            and isinstance(args[2], ast.Constant)
            and args[2].value == "user"
        )
    return False


def _modules_persisting_user_rows() -> set[str]:
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _appends_a_user_row(node):
                found.add(path.relative_to(root).as_posix())
    return found


def _enclosing_scopes(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Map every node to the nearest enclosing function, or the module."""
    owner: dict[ast.AST, ast.AST] = {}
    stack: list[ast.AST] = [tree]

    def walk(node: ast.AST) -> None:
        scope = stack[-1]
        for child in ast.iter_child_nodes(node):
            owner[child] = scope
            opens = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if opens:
                stack.append(child)
            walk(child)
            if opens:
                stack.pop()

    walk(tree)
    return owner


_SCRUBBERS = {"redact_via_context", "redact_row_via_context"}


def _scrubs_within(scope: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id in _SCRUBBERS for n in ast.walk(scope))


def _unscrubbed_user_row_sites() -> list[str]:
    """Every role=user append whose own function never calls the redactor.

    Scoped to the ENCLOSING FUNCTION rather than the file: the channel dispatchers
    rebind ``user_text = redact_via_context(user_text)`` above the call instead of
    wrapping the argument, so an argument-shaped check reports them all unscrubbed,
    while a whole-file check passes a second unscrubbed append in a module whose
    other function does scrub.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        owner = _enclosing_scopes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _appends_a_user_row(node)):
                continue
            scope = owner.get(node, tree)
            if not _scrubs_within(scope):
                name = getattr(scope, "name", "<module>")
                offenders.append(f"{path.relative_to(root).as_posix()}:{name}")
    return offenders


class TestEveryUserRowPersisterScrubs:
    """Ratchet: the transcript scrub is a convention across ~13 call sites, so pin them.

    Design and First-Principles review both landed on the same gap: the rule lives in
    each caller rather than at the write boundary, which is the weaker shape. Moving it
    into ``ConversationLog.append`` is AVAILABLE, not impossible -- it is declined here
    at a named cost, and the cost is the reason, not the impossibility. Those stored rows
    are read back into model context by ``context.py``'s recall path, so scrubbing user
    rows there changes the prompt on resume; and on a host whose credential policy cannot
    compose, every dashboard user row would degrade to the withheld placeholder, which is
    then what the model resumes on. Until that trade is decided, enforcement has to be a
    guard that fails when the set moves, rather than a convention the next author has to
    remember.
    """

    def test_the_set_of_user_row_persisters_has_not_grown(self) -> None:
        """A fourteenth persister goes RED here instead of silently reopening the leak."""
        found = _modules_persisting_user_rows()
        assert found, "the scanner matched nothing, so it cannot detect a new persister"
        added = found - _USER_ROW_PERSISTERS
        assert not added, (
            "a module now persists a role=user transcript row without being registered "
            f"as scrubbing it: {sorted(added)}. Scrub the persisted copy (NOT the text "
            "handed to the model) and add the module to _USER_ROW_PERSISTERS."
        )
        assert not _USER_ROW_PERSISTERS - found, (
            "a registered persister no longer appends a user row; drop it from "
            f"_USER_ROW_PERSISTERS: {sorted(_USER_ROW_PERSISTERS - found)}"
        )

    def test_every_user_row_call_site_scrubs(self) -> None:
        """A new unscrubbed append goes red even in a module already registered."""
        offenders = _unscrubbed_user_row_sites()
        assert not offenders, (
            "these functions persist a role=user transcript row without calling "
            f"redact_via_context anywhere in the same function: {offenders}. Scrub the "
            "persisted copy, NOT the text handed to the model."
        )


class TestPersistedRowRedactionSurvivesAMiscomposedHost:
    """A composed-host failure must not kill ordinary inbound chat.

    ``redact_via_context`` re-raises ``PlatformCompositionError`` by design, which is
    right for a nudge or an API response: the caller answers it with an audited refusal.
    The channel persisters are different -- they sit on the dispatch path of ordinary
    conversation, so the same raise takes the message down with an unhandled exception
    on a host whose only fault is a companion it could not compose. Raised by the Design
    review. The containment must not become a downgrade: the raw text must never reach
    the transcript, which is the assertion that fails if the shim ever returns its input.
    """

    class _BrokenPolicy:
        def redact(self, text: str) -> str:
            from kiro_crew.platform import PlatformCompositionError

            raise PlatformCompositionError("companion credential policy unreadable")

    def _install(self, monkeypatch) -> None:
        from kiro_crew.platform import context as ctx_mod

        class _Ctx:
            credentials = TestPersistedRowRedactionSurvivesAMiscomposedHost._BrokenPolicy()

        # A context IS installed on such a host -- only its policy fails to compose. Stubbing
        # current_context alone left installed_context() None, the OTHER no-companion state.
        monkeypatch.setattr(ctx_mod, "installed_context", lambda: _Ctx())
        monkeypatch.setattr(ctx_mod, "current_context", lambda: _Ctx())

    def test_the_raising_spelling_still_raises_on_such_a_host(self, monkeypatch) -> None:
        from kiro_crew.platform import PlatformCompositionError
        from kiro_crew.platform.context import redact_via_context

        self._install(monkeypatch)
        with pytest.raises(PlatformCompositionError):
            redact_via_context("token AKIAIOSFODNN7EXAMPLE")

    def test_the_row_spelling_withholds_instead_of_raising(self, monkeypatch) -> None:
        from kiro_crew.platform.context import (
            LOG_WITHHELD_PLACEHOLDER,
            redact_row_via_context,
        )

        self._install(monkeypatch)
        secret = "token AKIAIOSFODNN7EXAMPLE"
        out = redact_row_via_context(secret)
        assert out == LOG_WITHHELD_PLACEHOLDER
        assert secret not in out

    def test_no_raising_redactor_feeds_the_replay_persister(self) -> None:
        """A turn that already went out must be persisted, so its scrub cannot raise."""
        path = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "slack" / "gateway.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders: list[str] = []
        reached = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            raised_names = {
                target.id
                for node in ast.walk(fn)
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "redact_via_context"
            }
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (
                    isinstance(func, ast.Name) and func.id == "save_conversation_turn_off_loop"
                ):
                    continue
                reached += 1
                for arg in call.args:
                    if isinstance(arg, ast.Name) and arg.id in raised_names:
                        offenders.append(f"{fn.name}:{arg.id}")
                    elif (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Name)
                        and arg.func.id == "redact_via_context"
                    ):
                        offenders.append(f"{fn.name}:inline")
        assert reached >= 2, f"probe found only {reached} persister call(s) -- it proves nothing"
        assert offenders == [], (
            "a completed turn is scrubbed by the RAISING spelling before being persisted, so a "
            "miscomposed host drops it from replay and restart history: " + "; ".join(offenders)
        )

    def test_every_channel_persister_uses_the_containment_spelling(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders: list[str] = []
        reached = 0
        for path in sorted(root.rglob("*/transport_dispatch.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                if "redact_row_via_context" in names:
                    reached += 1
                if "redact_via_context" in names:
                    offenders.append(f"{path.parent.name}:{fn.name}")
        assert reached >= 9, f"probe reached only {reached} persisters -- it proves nothing"
        assert offenders == [], (
            "a channel dispatch path still uses the raising spelling, so a miscomposed "
            "host kills the message instead of withholding the row: " + "; ".join(offenders)
        )


class TestOneScrubRuleServesEveryLoopReader:
    """Every projection of a loop must scrub the same fields as every other.

    ``_serialize`` runs each non-addressing field through ``scrub_loop_text``. A second
    assembly that skips it serves raw what the first scrubs, and ``stopped_reason`` is
    the field that exposes: agent-supplied free text (``autonudge_stop(reason=)``),
    neither withheld nor mapped out of the reduced row, on a list route with no owner
    gate.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _structured(self, reason: str) -> NudgeLoop:
        loop = _loop(stopped_reason=reason)
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            created_ts=1_000.0,
        )
        return loop

    def test_the_reduced_row_scrubs_every_field_the_plain_row_scrubs(self) -> None:
        reason = f"gave up after {self.SECRET}"
        loop = self._structured(reason)
        assert autonudge_handlers.is_structured_monitor_loop(
            loop
        ), "precondition: this must take the REDUCING arm, not _serialize"
        reduced = autonudge_handlers._serialize_for_legacy_reader(loop)
        assert "stopped_reason" in reduced, (
            "precondition: the field must be SERVED here, or its absence would pass "
            "this test vacuously"
        )
        plain = autonudge_handlers._serialize(_loop(stopped_reason=reason))
        assert reduced["stopped_reason"] == plain["stopped_reason"], (
            "the two arms disagree on what a loop's text projection is, so one of them "
            "serves what the other scrubs"
        )
        assert self.SECRET not in json.dumps(
            reduced
        ), "an agent-written stop reason reached the un-gated legacy list verbatim"

    def test_the_in_session_reading_scrubs_its_stop_reason_too(self) -> None:
        reading = autonudge_handlers._autonudge_loop_reading(
            _loop(stopped_reason=f"gave up after {self.SECRET}")
        )
        assert "stopped_reason" in reading, "precondition: the field must be served here"
        assert self.SECRET not in json.dumps(
            reading
        ), "the in-session reading assembles its own payload and skipped the scrub"


class TestTheWriteTokenIsNotAlsoServedRaw:
    """`goal_token` authorises a write, so one spelling on the wire is one too many.

    It is republished as `message_fingerprint`, which every client reads; the raw field had
    no reader in the dashboard, in the handlers, or in any test payload -- verified with a
    positive control on `message_fingerprint`, which the same search finds 14 times.
    """

    def test_the_rest_projection_drops_the_raw_token(self) -> None:
        served = autonudge_handlers._serialize(_loop(goal_token="0123456789abcdef0123456789abcdef"))
        assert "message_fingerprint" in served, "precondition: the republished name must ship"
        assert served["message_fingerprint"], "precondition: a token must exist to serve"
        assert "goal_token" not in served, (
            "the raw write-authorising token is still on the wire beside its own republished "
            "name, so a reader can lift a baseline it was never meant to hold"
        )

    def test_the_reduced_legacy_row_drops_it_too(self) -> None:
        loop = _loop(goal_token="0123456789abcdef0123456789abcdef")
        loop.monitor = MonitorState(
            kind="github_pull_request",
            target="owner/repo#1",
            objective="review_ready",
            created_ts=1_000.0,
        )
        reduced = autonudge_handlers._serialize_for_legacy_reader(loop)
        assert "goal_token" not in reduced, "the reduced row serves the raw write token"


class TestSidecarRecoveryCannotAbortStartup:
    """Recovery from an unreadable sidecar must not end the process.

    ``_refuse_writes_and_preserve_sidecar`` arms the write refusal and then moves the
    unreadable sidecar aside. That move takes the sidecar lock, and a DIRECTORY standing
    where the lock file belongs makes ``open`` raise ``IsADirectoryError`` -- which
    propagated out of ``_load`` and ended the gateway during startup. The rename and the
    name reservation were already guarded; acquiring the lock was not.
    """

    def test_an_unopenable_lock_keeps_writes_refused_and_retains_the_bytes(self, tmp_path):
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._quarantine_path.write_text("{ not json", encoding="utf-8")
            lock = svc._quarantine_path.with_name(svc._quarantine_path.name + ".lock")
            lock.mkdir()
            assert lock.is_dir(), "precondition: the lock path must be unopenable"

            svc._refuse_writes_and_preserve_sidecar()

            assert svc._load_refused is True, (
                "the write refusal was lost, so a later persist would compact around rows "
                "nothing enumerated"
            )
            assert svc._quarantine_path.exists(), "the unreadable bytes were not retained"
            assert svc._quarantine_path.read_text(encoding="utf-8") == "{ not json"
            assert not list(
                tmp_path.glob("*.corrupt-*")
            ), "a move-aside was recorded that cannot have happened"
        finally:
            svc.stop()


class TestTheWriteRefusalIsReachableWithRowsInMemory:
    """``_write_quarantine_sidecar_locked`` latches the persist refusal from inside a
    write, so the next persist meets the guard with rows still held in memory."""

    @pytest.mark.asyncio
    async def test_the_refusal_is_reached_after_a_clean_load_armed_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._load_refused is False, "precondition: the store must load cleanly"
            await svc.add("chat-1-1785", "keep checking the pull request", idle_secs=300)
            assert svc._loops, "precondition: a loop must be armed, or the map is empty anyway"

            # Unreadable only AFTER a clean load: the window no load-time setter describes.
            monkeypatch.setattr(svc, "_quarantine_rows_on_disk", lambda: None)
            with pytest.raises(AutoNudgeStoreUnvetted):
                svc._write_state(svc._serialize_state())
            assert svc._load_refused is True, "the mid-write refusal did not latch"
            assert (
                svc._loops
            ), "the map emptied, so this case would not differ from the load-time one"

            with pytest.raises(AutoNudgeStoreUnvetted) as caught:
                svc._write_state(svc._serialize_state())
            assert "could not vet the store" in str(caught.value), (
                "the second persist raised from the sidecar writer rather than from the "
                f"latched guard, so the guard never executed: {caught.value}"
            )
        finally:
            svc.stop()


def test_a_timed_out_probe_process_is_reaped(monkeypatch) -> None:
    """A probe that outlives its join must still be terminated and closed, not leaked."""
    events: list[str] = []

    class _StubProbe:
        def __init__(self) -> None:
            self._alive = True

        def start(self) -> None:
            events.append("start")

        def join(self, timeout=None) -> None:
            # Returns with the child STILL ALIVE: the timeout branch, which is the only
            # path where the join does not reap on its own.
            events.append(f"join:{timeout}")

        def is_alive(self) -> bool:
            return self._alive

        def terminate(self) -> None:
            events.append("terminate")
            self._alive = False

        def kill(self) -> None:
            events.append("kill")
            self._alive = False

        def close(self) -> None:
            events.append("close")

    stub = _StubProbe()

    class _StubCtx:
        def Process(self, *_a, **_k):
            return stub

    monkeypatch.setattr(multiprocessing, "get_context", lambda _kind: _StubCtx())
    _fork_probe_context()

    assert "join:10" in events, f"precondition: the probe was never joined ({events})"
    assert "terminate" in events, (
        "a probe that outlived its join was left running, so the test leaks a process "
        f"holding whatever the child held ({events})"
    )
    assert (
        "close" in events
    ), f"the probe handle was never closed, so its sentinel descriptor leaks ({events})"


class TestOrdinaryUserProseSurvivesTheRowScrub:
    """Design's false-positive guard: the at-rest rewrite is irreversible, so it must not
    fire on ordinary prose."""

    CORPUS = (
        "can you rebase this branch onto main and push it",
        "the build failed with exit code 1, no other output",
        "my key insight is that the cache never invalidates",
        "set the token budget to 4096 and retry",
        "password rotation is due next quarter, per the runbook",
        "see https://github.com/acme/widgets/pull/42 for context",
        "AWS costs went up 12% after we moved to us-east-1",
        "the secret to this one is that ordering matters",
        "id 0123456789 belongs to the staging tenant",
        "Authorization is handled by the gateway, not here",
        "run `aws s3 ls` and paste the bucket names",
        "the access key rotation ticket is still open",
    )

    def test_no_ordinary_message_is_rewritten(self) -> None:
        from kiro_crew.platform.context import redact_row_via_context

        rewritten = [text for text in self.CORPUS if redact_row_via_context(text) != text]
        assert not rewritten, (
            "the row scrub rewrote ordinary user prose, and the original is not retained, "
            f"so this destroys transcript text: {rewritten!r}"
        )

    def test_the_guard_can_detect_a_rewrite(self) -> None:
        """Positive control: a real credential MUST still be rewritten, or the corpus proves
        nothing."""
        from kiro_crew.platform.context import redact_row_via_context

        secret = "my key is AKIAIOSFODNN7EXAMPLE"
        assert redact_row_via_context(secret) != secret


class TestAnEchoedBannerDoesNotOverwriteTheStoredOne:
    """FP: the echoed-projection guard covered ``message`` only.

    ``GET`` serves a scrubbed banner, so a client that PATCHes back what it was served
    replaced the operator's stored banner with its own redaction -- irreversibly.
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    class _Svc:
        def __init__(self, row: object) -> None:
            self.row = row
            self.applied: dict[str, object] = {}

        def get_by_id(self, _loop_id: str) -> object:
            return self.row

        async def update(self, _loop_id: str, **fields: object) -> object:
            self.applied = fields
            return self.row

    def _row(self, banner: str) -> object:
        return SimpleNamespace(
            id="lp-1", slot_key="dashboard:chat-1", message="goal", banner=banner
        )

    @pytest.mark.asyncio
    async def test_the_served_projection_is_not_written_back(self) -> None:
        from kiro_crew.autonudge import scrub_loop_text
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        stored = "watching " + self.SECRET
        served = scrub_loop_text(stored, field="banner")
        assert served != stored, "the fixture banner was not scrubbed, so it proves nothing"
        svc = self._Svc(self._row(stored))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner=served, source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") is None, (
            "the echoed projection was written back, so the stored banner is now its own "
            "redaction: " + repr(svc.applied.get("banner"))
        )

    @pytest.mark.asyncio
    async def test_a_genuinely_edited_banner_is_still_stored(self) -> None:
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        svc = self._Svc(self._row("watching " + self.SECRET))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner="watching CI instead", source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") == "watching CI instead"

    @pytest.mark.asyncio
    async def test_re_sending_an_unscrubbed_banner_unchanged_still_writes_it(self) -> None:
        """A clean banner's projection IS the stored text, so an equal value is no echo."""
        from kiro_crew.autonudge_authz import authorize_and_update_nudge

        svc = self._Svc(self._row("cycle ran"))

        _loop, error, _status = await authorize_and_update_nudge(
            svc=svc, loop_id="lp-1", banner="cycle ran", source="dashboard"
        )

        assert error is None, error
        assert svc.applied.get("banner") == "cycle ran", (
            "an idempotent set was mistaken for a destructive echo, so PATCH can no longer "
            "quiet a running loop: " + repr(svc.applied.get("banner"))
        )
