"""A turn that ends without EVENT_COMPLETE still records its usage row.

The dashboard's only ``persist_token_record_async`` call lives in the
``EVENT_COMPLETE`` branch, so a turn torn down through ``_persist_partial_reply``
-- the 6-hour turn ceiling, the Stop button, a signed-out CLI mid-turn, and the
other recovery paths that share that seam -- wrote no usage row and no
``meta.turn_stats``, leaving its credits unrecorded however much it billed (the
ceiling path recorded zero credits for a six-hour, 91-tool-call turn).

These drive the extracted ``_persist_abnormal_turn_usage`` seam helper over the
REAL row store, asserting the row lands with the turn's recovered credits and a
terminal stop reason, that a turn which billed nothing writes none, that a
cost-only claude-seam turn records too, and that a turn dying before it installs
fresh billing stats does NOT re-bill the previous turn (the ``since=`` guard).

The riskiest invariant is the claim/seam guard itself: ``EVENT_COMPLETE`` claims
the turn's single row BEFORE its awaited (thread-offloaded) persist, so a
cancellation landing on that await unwinds into the abnormal-end seam, which must
see the row already claimed and write nothing. ``TestCancelDuringCompletePersist``
drives the real ``_run_chat`` to ``EVENT_COMPLETE``, cancels inside the persist
after one row is written, and asserts exactly one row -- with the REAL
``provider_last_turn_usage`` live and billing available, so a regression that
lets the seam re-read and re-bill produces a second row and reddens the test.
"""

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.acp.types import STOP_REASON_END_TURN, TurnUsage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.testing.wait import async_wait_until, await_until


class _FakeSlot:
    def __init__(self, key="chat-1-abc"):
        self.key = key
        self.model = "pinned-model"
        self.agent = "default"
        self._app = ""


@pytest.fixture
def _isolated_shards(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    return tmp_path


def _rows(shard_dir):
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    path = shard_dir / f"{day}.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _stub_accessors(monkeypatch, *, usage):
    """Make the seam helper's provider reads deterministic.

    The helper reads the turn's billing and attribution through named imports on
    the chat_runner module; stubbing them there exercises the helper's own logic
    (billing gate, argument wiring, the ``since=`` identity guard) without
    reconstructing a live ACP provider. ``provider_last_turn_usage`` accepts the
    ``since`` keyword the helper now threads through for the prior-turn guard.
    """
    monkeypatch.setattr(chat_runner, "provider_last_turn_usage", lambda c, *, since=None: usage)
    monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
    monkeypatch.setattr(chat_runner, "read_context_tokens", lambda c: (1234, 200000))
    monkeypatch.setattr(chat_runner, "read_effective_agent", lambda c: "kirocrew")
    monkeypatch.setattr(chat_runner, "telemetry_channel_of", lambda sk: "dashboard")

    class _Caps:
        provider_seam = "acp"

    monkeypatch.setattr(chat_runner, "capabilities_of", lambda c: _Caps())


@pytest.mark.asyncio
async def test_records_credits_for_a_turn_cut_before_event_complete(_isolated_shards, monkeypatch):
    _stub_accessors(monkeypatch, usage=TurnUsage(credits=78.9))
    wrote = await chat_runner._persist_abnormal_turn_usage(
        _FakeSlot(),
        object(),
        "chat-1-abc",
        elapsed_ms=21_600_300,
        stop_reason=chat_runner.STOP_REASON_CANCELLED,
    )
    assert wrote is True
    rows = _rows(_isolated_shards)
    assert len(rows) == 1
    (row,) = rows
    assert row["credits"] == pytest.approx(78.9)
    assert row["duration_ms"] == 21_600_300
    assert row["model"] == "served-model"
    assert row["provider"] == "acp"
    assert row["agent"] == "kirocrew"
    assert row["stop_reason"] == chat_runner.STOP_REASON_CANCELLED


@pytest.mark.asyncio
async def test_a_turn_that_billed_nothing_writes_no_row(_isolated_shards, monkeypatch):
    _stub_accessors(monkeypatch, usage=TurnUsage())
    wrote = await chat_runner._persist_abnormal_turn_usage(
        _FakeSlot(),
        object(),
        "chat-1-abc",
        elapsed_ms=5000,
        stop_reason=chat_runner.STOP_REASON_CANCELLED,
    )
    # Scope is the credits defect alone: a turn that billed nothing writes no
    # row (and the seam adds no histogram emit — that is the complete path's
    # concern), so the helper reports it did not record and leaves the
    # once-per-turn guard unclaimed.
    assert wrote is False
    assert _rows(_isolated_shards) == []


@pytest.mark.asyncio
async def test_a_turn_dying_before_fresh_stats_does_not_rebill_the_prior_turn(
    _isolated_shards, monkeypatch
):
    # The real guard: provider_last_turn_usage(..., since=<stats0>) reports an
    # empty TurnUsage when the live stats object is still the one observed
    # before the turn — a dispatch that died before installing fresh stats. The
    # helper must then write NO row, even though the prior turn's object still
    # carries credits. Modelled by a stub that returns empty when since is the
    # same sentinel the helper was handed.
    _prior_stats = object()

    def _guarded(c, *, since=None):
        return TurnUsage() if since is _prior_stats else TurnUsage(credits=50.0)

    _stub_accessors(monkeypatch, usage=TurnUsage(credits=50.0))
    monkeypatch.setattr(chat_runner, "provider_last_turn_usage", _guarded)
    wrote = await chat_runner._persist_abnormal_turn_usage(
        _FakeSlot(),
        object(),
        "chat-1-abc",
        elapsed_ms=1000,
        stop_reason="error: process died",
        since=_prior_stats,
    )
    assert wrote is False
    assert _rows(_isolated_shards) == []


@pytest.mark.asyncio
async def test_claude_seam_cost_only_turn_is_still_recorded(_isolated_shards, monkeypatch):
    # usage_has_billing counts cost_usd, not only credits: a cost-bearing turn
    # cut on the claude seam must record just as a credit-bearing acp turn does.
    _stub_accessors(monkeypatch, usage=TurnUsage(cost_usd=0.42, input_tokens=1000))
    wrote = await chat_runner._persist_abnormal_turn_usage(
        _FakeSlot(),
        object(),
        "chat-1-abc",
        elapsed_ms=1000,
        stop_reason="timeout: turn ceiling",
    )
    assert wrote is True
    (row,) = _rows(_isolated_shards)
    assert row["cost"] == pytest.approx(0.42)
    assert row["stop_reason"] == "timeout: turn ceiling"


class _RealStats:
    """A minimal real ``last_prompt_stats`` double resolved by the REAL
    ``provider_last_turn_usage`` walk.

    ``resolve_billing_stats`` reads the holder's ``last_prompt_stats`` and, when
    the object offers ``to_turn_usage``, prefers it. This carries actual billing
    so the seam's own read path (``_billing_stats`` -> ``to_turn_usage`` ->
    ``usage_has_billing``) runs for real -- not a stub -- which is what lets the
    double-bill assertion discriminate: if the claim guard regressed, the seam
    would read THIS object and write a second row.
    """

    def __init__(self, credits: float) -> None:
        self.credits = credits

    def to_turn_usage(self) -> TurnUsage:
        return TurnUsage(credits=self.credits)


def _drive_state_and_slot(tmp_path: Path, name: str = "cancel-persist-slot"):
    """The ``test_active_turn_session_key`` harness: a real ``_run_chat`` turn."""
    state = _make_state(tmp_path)
    client = MagicMock()
    # The seam gates on slot._acp_client (the ref the finally drops) being live;
    # _run_chat sets it from client.client, so a non-None value keeps the seam's
    # liveness check satisfied and leaves the _turn_usage_persisted guard as the
    # only thing that can stop a second write.
    client.client = client
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot(name)
    slot.append("user", "hello", "msg msg-u")
    client.shutdown = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=1.0)
    return state, slot, client


class TestCancelDuringCompletePersist:
    """The claim/seam guard, driven for real rather than stubbed.

    ``EVENT_COMPLETE`` sets ``_turn_usage_persisted = True`` BEFORE its awaited,
    thread-offloaded ``persist_token_record_async``. A cancellation landing on
    that await -- after the row is appended -- unwinds into ``_run_chat``'s
    ``CancelledError`` arm, which runs ``_persist_partial_reply`` ->
    ``_record_abnormal_turn_usage``. The seam must see the row already claimed and
    write nothing, so the turn lands exactly one usage row.
    """

    @pytest.mark.asyncio
    async def test_a_cancel_on_the_complete_persist_leaves_exactly_one_row(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        state, slot, client = _drive_state_and_slot(tmp_path)

        # Fresh billing is live when the seam would read it: _run_chat captures
        # _turn_stats0 from client.last_prompt_stats at turn start, so install a
        # DIFFERENT object before the stream runs. The real since= identity check
        # then sees a changed stats object -- a regressed guard WOULD bill it.
        client.last_prompt_stats = _RealStats(credits=0.0)

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _complete(msg):
            # Mid-turn the runtime installs fresh per-turn stats; model that so
            # the seam's since= guard is exercised against a changed object.
            client.last_prompt_stats = _RealStats(credits=12.5)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                usage=TurnUsage(credits=12.5),
            )

        client.stream = _complete
        client.stream_command = _complete

        # The real complete-path persist, replaced by one that writes a single
        # row through the REAL store and then is cancelled -- exactly a shutdown
        # landing on the thread-offloaded write after the claim is set.
        real_persist = usage_mod.persist_token_record

        async def _write_then_cancel(slot_key, model, event, **kwargs):
            # Write the row the complete path would have written, synchronously,
            # through the real store so the shard file gets one genuine record,
            # then be cancelled -- the sync variant takes no emit_metric.
            real_persist(
                slot_key,
                model,
                event,
                kwargs.get("provider", ""),
                surface=kwargs.get("surface", ""),
                agent=kwargs.get("agent", ""),
                context_used=kwargs.get("context_used", 0),
                context_window=kwargs.get("context_window", 0),
                elapsed_ms=kwargs.get("elapsed_ms", 0),
                app=kwargs.get("app", ""),
                model_source=kwargs.get("model_source"),
            )
            raise asyncio.CancelledError()

        monkeypatch.setattr(chat_runner, "persist_token_record_async", _write_then_cancel)

        # The CancelledError arm records the partial turn and returns -- an
        # involuntary cancel on the complete-path persist is caught, not
        # propagated -- so the turn ends normally and the assertion is on the
        # row store, not a raise.
        await _run_chat(state, slot, "test message")

        rows = _rows(tmp_path)
        assert len(rows) == 1, (
            "the complete-path claim did not stop the abnormal-end seam from "
            "billing the turn a second time"
        )
        (row,) = rows
        assert row["credits"] == pytest.approx(12.5)

    @pytest.mark.asyncio
    async def test_a_cancel_before_the_executor_runs_still_lands_the_row(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 6.1 F1: the complete-path persist is SHIELDED from the turn's cancel.

        ``persist_token_record_async`` offloads the append with
        ``asyncio.to_thread``. A cancellation delivered to the turn BEFORE the
        executor thread picks up the write cancels that future, so without the
        shield the append never runs -- and the claim set just above already told
        the abnormal-end seam the row was written, so the seam writes nothing and
        the turn's only usage row is lost.

        This drives the real ``_run_chat`` to ``EVENT_COMPLETE``, replaces the
        persist with one whose write is a genuine thread offload gated so it
        cannot finish until AFTER the turn task is cancelled, cancels the task in
        exactly that window, and asserts the row still lands. The write runs on a
        real worker thread; the shield is the only thing that keeps the turn's
        cancel from tearing the offloaded future down before it completes --
        remove ``asyncio.shield`` at the call site and this test writes zero rows.
        """
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        state, slot, client = _drive_state_and_slot(tmp_path, name="shield-persist-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _complete(msg):
            client.last_prompt_stats = _RealStats(credits=7.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                usage=TurnUsage(credits=7.0),
            )

        client.stream = _complete
        client.stream_command = _complete

        real_persist = usage_mod.persist_token_record
        # Set once the persist await is reached: lets the test cancel the turn
        # knowing the thread offload is in flight.
        write_scheduled = asyncio.Event()
        # Released by the test only AFTER it has cancelled the turn task, so the
        # worker thread's append happens in the post-cancel window the shield
        # must protect.
        release_write = threading.Event()
        wrote = threading.Event()

        async def _offloaded_persist(slot_key, model, event, **kwargs):
            def _blocking_write():
                # Do not append until the test has cancelled the turn: this is
                # the "cancel landed before the executor ran the write" window.
                release_write.wait(timeout=5)
                real_persist(
                    slot_key,
                    model,
                    event,
                    kwargs.get("provider", ""),
                    surface=kwargs.get("surface", ""),
                    agent=kwargs.get("agent", ""),
                    context_used=kwargs.get("context_used", 0),
                    context_window=kwargs.get("context_window", 0),
                    elapsed_ms=kwargs.get("elapsed_ms", 0),
                    app=kwargs.get("app", ""),
                    model_source=kwargs.get("model_source"),
                )
                wrote.set()

            # Signal the test from the event loop BEFORE awaiting the offload, so
            # the cancel is delivered while the to_thread future is pending.
            write_scheduled.set()
            await asyncio.to_thread(_blocking_write)

        monkeypatch.setattr(chat_runner, "persist_token_record_async", _offloaded_persist)

        task = asyncio.ensure_future(_run_chat(state, slot, "test message"))
        # Wait until the turn has reached the shielded persist await.
        await await_until(write_scheduled, what="the turn reaching the shielded persist")
        # Cancel the turn in the window before the executor thread runs the
        # append: the shield must keep the offloaded write alive.
        task.cancel()
        # Now let the worker thread complete its append. If the write were not
        # shielded, the to_thread future would already have been cancelled and
        # this release would land on a torn-down future -- no row.
        release_write.set()
        # The task's cancel resolves (its CancelledError arm catches and returns,
        # or the cancel propagates); either way we only care that the row landed.
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert wrote.wait(timeout=5), "the shielded offloaded write never completed"

        rows = _rows(tmp_path)
        assert len(rows) == 1, (
            "the complete-path persist was not shielded: a cancel before the "
            "executor ran dropped the turn's usage row"
        )
        (row,) = rows
        assert row["credits"] == pytest.approx(7.0)

    @pytest.mark.asyncio
    async def test_a_turn_dying_before_fresh_stats_writes_no_row_through_run_chat(
        self, tmp_path, monkeypatch
    ) -> None:
        """The ``since=_turn_stats0`` wiring, driven for real rather than stubbed.

        The direct-helper prior-stats test hands the helper its own ``since=``,
        so it cannot catch the call site dropping ``since=_turn_stats0`` -- the
        helper's default would then be ``_NO_PRIOR_STATS`` and the live read
        would bill the previous turn again. This drives the real ``_run_chat``
        with the REAL ``provider_last_turn_usage`` and models the exact failure
        mode: the dispatch dies BEFORE the runtime installs fresh per-turn stats,
        so ``client.last_prompt_stats`` is still the turn-start object that
        already carries the previous turn's credits.

        ``_run_chat`` captures ``_turn_stats0`` from that object at turn start and
        hands it to the abnormal seam as ``since=``. Because the object never
        changes, the real ``since`` identity check (``stats is since``) reports an
        empty ``TurnUsage`` and the seam writes NO row -- even though the object
        carries credits. Delete ``since=_turn_stats0`` at the call site and the
        seam reads those stale credits and writes a row; this test then fails,
        which is what makes the wiring, not just the helper default, load-bearing.
        """
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        state, slot, client = _drive_state_and_slot(tmp_path, name="prior-stats-slot")

        # The PREVIOUS turn's stats object: carries credits already recorded, and
        # -- the whole point -- stays installed, because this turn dies before the
        # runtime would replace it with a fresh zero-credit object.
        prior = _RealStats(credits=9.0)
        client.last_prompt_stats = prior

        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        async def _dies_before_complete(msg):
            # Fresh stats are NOT installed (the dispatch dies first); the prior
            # object stays in place carrying its already-recorded credits.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            raise asyncio.CancelledError()

        client.stream = _dies_before_complete
        client.stream_command = _dies_before_complete

        # The CancelledError unwinds through _run_chat's cancel arm into
        # _persist_partial_reply -> _record_abnormal_turn_usage, which calls the
        # REAL provider_last_turn_usage with since=_turn_stats0.
        await _run_chat(state, slot, "test message")

        assert _rows(tmp_path) == [], (
            "a turn that died before fresh stats re-billed the prior turn: the "
            "abnormal seam's since=_turn_stats0 guard is not wired through "
            "_run_chat"
        )
        # The prior object was never consumed/mutated by the guarded read.
        assert client.last_prompt_stats is prior


async def _let_the_turn_clock_advance() -> None:
    """Wait until the turn's monotonic clock has moved a few milliseconds.

    A turn the fake stream ends in under a millisecond measures 0 ms, which
    ``turn_stats_meta`` reads as nothing to show, so the footer assertions wait
    on the clock they depend on rather than sleeping a fixed amount.
    """
    import time

    from kiro_crew.testing.wait import async_wait_until

    start = time.monotonic()
    await async_wait_until(lambda: time.monotonic() - start >= 0.005)


class TestStoppedTurnFooter:
    """A Stop press gives the partial reply the completed-turn footer.

    A completed turn's last reply row carries ``meta.turn_stats`` (elapsed,
    credits/cost, model) and the dashboard renders it as the usage line under
    the reply. A Stop ends the turn through the CancelledError arm instead,
    which never reaches EVENT_COMPLETE, so the abnormal-end seam is what writes
    the stopped reply's footer. These drive the real ``_run_chat`` with the REAL
    ``provider_last_turn_usage`` and assert the footer on the partial row, and
    that the footer and the usage row come from the same single billing read.
    """

    @staticmethod
    def _stop_mid_stream(slot, client, credits, *, accumulated=None):
        from kiro_crew import llm_helpers
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(msg):
            client.last_prompt_stats = stats = _RealStats(credits=credits)
            if accumulated is not None:
                # A multi-attempt turn: the retry loop published the turn's
                # TOTAL beside the last attempt's live stats. The read consumes
                # it, so only a single shared read gives the footer and the row
                # the same total.
                setattr(client, llm_helpers._TURN_BILLED_ATTR, (stats, accumulated))
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            # The dashboard Stop handler marks the slot, then the turn's task
            # is cancelled -- the shape _stop_pressed() reads.
            slot._stopping = True
            raise asyncio.CancelledError()

        client.stream = _stream
        client.stream_command = _stream

    @staticmethod
    def _partial_row(slot):
        rows = [m for m in slot.messages if m.get("role") == "assistant"]
        assert rows, "the stopped turn persisted no partial reply row"
        return rows[-1]

    @pytest.mark.asyncio
    async def test_a_stop_press_puts_the_turn_footer_on_the_partial_reply(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="stop-footer-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        # Last attempt billed 1.0; the turn's accumulated total is 3.25.
        self._stop_mid_stream(slot, client, credits=1.0, accumulated=TurnUsage(credits=3.25))
        await _run_chat(state, slot, "test message")

        row = self._partial_row(slot)
        assert row["content"] == "partial answer"
        stats = (row.get("meta") or {}).get("turn_stats")
        assert stats is not None, "a stopped turn's partial reply carries no footer"
        assert stats["credits"] == pytest.approx(3.25)
        assert stats["elapsed_ms"] > 0
        assert stats["model"] == "served-model"
        # The in-place meta write must reach disk: the cancel path has no
        # explicit save, so the slot must end the turn dirty (the finally's
        # file-change flush marks it on every exit).
        assert slot._dirty is True
        # One billing read feeds both: the usage row carries the same total.
        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["credits"] == pytest.approx(3.25)
        assert rows[0]["stop_reason"] == chat_runner.STOP_REASON_CANCELLED

    @pytest.mark.asyncio
    async def test_a_stop_before_any_billing_still_shows_elapsed_only(
        self, tmp_path, monkeypatch
    ) -> None:
        # Parity with a completed turn: an unreported figure is omitted, never
        # rendered as zero, and no usage row is written for an unbilled turn.
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "")
        state, slot, client = _drive_state_and_slot(tmp_path, name="stop-unbilled-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        self._stop_mid_stream(slot, client, credits=0.0)

        await _run_chat(state, slot, "test message")

        stats = (self._partial_row(slot).get("meta") or {}).get("turn_stats")
        assert stats is not None
        assert set(stats) == {"elapsed_ms"}
        assert _rows(tmp_path) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_pressed", [True, False], ids=["stop", "involuntary-cancel"])
    async def test_an_end_before_dispatch_leaves_the_previous_footer_alone(
        self, tmp_path, monkeypatch, stop_pressed
    ) -> None:
        # A turn ending during pre-prompt preparation (after the client is
        # published, before dispatch captures the turn's message boundary)
        # must not attach anything: with the boundary still 0 the attach would
        # walk back to the PREVIOUS reply and overwrite its recorded footer.
        # Whatever ended it: the gate is the boundary, not the reason.
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="pre-dispatch-stop-slot")
        prior_stats = {"elapsed_ms": 5000, "credits": 9.0}
        slot.append(
            "assistant", "previous reply", "msg msg-a", meta={"turn_stats": dict(prior_stats)}
        )
        slot.append("user", "next question", "msg msg-u")
        client.last_prompt_stats = _RealStats(credits=0.0)

        async def _end_during_preparation(*args, **kwargs):
            await _let_the_turn_clock_advance()
            if stop_pressed:
                slot._stopping = True
            raise asyncio.CancelledError()

        monkeypatch.setattr(chat_runner, "publish_turn_identity", _end_during_preparation)

        await _run_chat(state, slot, "next question")

        previous = next(m for m in slot.messages if m.get("content") == "previous reply")
        assert previous["meta"]["turn_stats"] == prior_stats

    @pytest.mark.asyncio
    async def test_an_involuntary_cancel_gets_the_footer_too(self, tmp_path, monkeypatch) -> None:
        # A tab close / idle sweep / shutdown / turn-ceiling cancel ("error:
        # cancelled") leaves a reply the user read, just like a Stop press, so
        # it gets the same footer beside its usage row.
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="sweep-cancel-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            raise asyncio.CancelledError()

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        stats = (self._partial_row(slot).get("meta") or {}).get("turn_stats")
        assert stats is not None, "an involuntary cancel left the partial reply bare"
        assert stats["credits"] == pytest.approx(2.0)
        assert stats["model"] == "served-model"
        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["credits"] == pytest.approx(2.0)


class TestErrorEndTurnFooter:
    """What the abnormal-end seam guarantees around the footer it stamps.

    The stamp itself -- every abnormal end gives its reply the completed-turn
    footer -- is pinned by ``TestStoppedTurnFooter`` and ``TestAbnormalEndFooter``
    below. These pin the seam's ordering and its edges: the footer is on the
    reply before a shutdown's last save, the slot is marked dirty for it, it
    reaches open clients before a queued recovery runs, it lands on the reply
    rather than a system notice, it is found in a window at its cap, it is put on
    once however many ends the turn's teardown reaches, the ceiling's acknowledged
    stop keeps the completion's figures (first-output latency included), a
    completion that reported no billing is billed by the arm that cut it, and a
    failure inside the seam costs its caller nothing. Driven through the real
    ``_run_chat`` with the REAL ``provider_last_turn_usage``.
    """

    @staticmethod
    def _assistant_row(slot, content):
        rows = [
            m for m in slot.messages if m.get("role") == "assistant" and m["content"] == content
        ]
        assert rows, f"no assistant row {content!r} in {[m.get('role') for m in slot.messages]}"
        return rows[-1]

    @staticmethod
    def _assert_cut_reply_footer(slot, credits):
        partial = TestErrorEndTurnFooter._assistant_row(slot, "partial answer")
        stats = (partial.get("meta") or {}).get("turn_stats")
        assert stats is not None, "the reply the turn's end cut short carries no footer"
        assert stats["credits"] == pytest.approx(credits)
        assert stats["model"] == "served-model"
        assert stats["elapsed_ms"] > 0
        # What streamed is a saved reply, not chunk rows a save drops, and every
        # card the end appended follows the reply it cut short.
        assert not [m for m in slot.messages if m.get("role") == "chunk"]
        at = slot.messages.index(partial)
        assert all(i > at for i, m in enumerate(slot.messages) if m.get("role") == "error")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("native_stop", ["unacknowledged", "acknowledged"])
    async def test_the_turn_ceiling_puts_the_footer_on_the_partial_reply(
        self, tmp_path, monkeypatch, native_stop
    ) -> None:
        # At the ceiling the native agent is sent a stop first, and the task is
        # cancelled after. An agent that does not answer leaves that cancel to
        # land on the stream. One that does answer ends the prompt as
        # ``cancelled``: the completion bills the turn, and the cancel lands on an
        # await after that and before the completion's own footer attach.
        from kiro_crew.acp.types import STOP_REASON_CANCELLED
        from kiro_crew.dashboard import turn_dispatch as td
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        monkeypatch.setattr(
            td, "resolve_turn_limit", lambda slot=None: (60.0, td.TURN_LIMIT_GLOBAL)
        )
        # A hang bound only: the acknowledgement below arrives on a signal, and a
        # short bound would let a slow host turn the acknowledged stop into an
        # unanswered one.
        monkeypatch.setattr(td, "native_cancel_ack_timeout_secs", lambda: 30.0)
        # The turn's first-output clock reads a fixed 250 ms, so the footer's
        # ``ttft_ms`` is a value to compare, not a host timing.
        monkeypatch.setattr(
            chat_runner,
            "_turn_clock",
            lambda slot, t0, **_: chat_runner._FirstVisibleClock(t0, clock=lambda: t0 + 0.25),
        )
        # Hold the ceiling's timer and fire it once the reply has streamed: a
        # wall-clock ceiling short enough for a test races the turn's own setup on
        # a loaded host and can cut the turn before it streams anything.
        loop = asyncio.get_running_loop()
        real_call_later = loop.call_later
        deadline = []

        def _hold_the_ceiling(delay, callback, *args, **kwargs):
            if getattr(callback, "__name__", "") == "_on_deadline":
                deadline.append(callback)
                return real_call_later(3600, lambda: None)
            return real_call_later(delay, callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_later", _hold_the_ceiling)
        state, slot, client = _drive_state_and_slot(tmp_path, name=f"ceiling-{native_stop}-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        stop_sent = asyncio.Event()
        # The completion's usage write is shielded from the task cancel and lands
        # on its own afterwards; the row count waits for it, not for a sleep.
        real_persist = chat_runner.persist_token_record_async
        persist_started = asyncio.Event()
        writes_done = []

        async def _tracked_persist(*args, **kwargs):
            persist_started.set()
            try:
                return await real_persist(*args, **kwargs)
            finally:
                writes_done.append(None)

        monkeypatch.setattr(chat_runner, "persist_token_record_async", _tracked_persist)

        async def _cancel_owned_turn(key, owner, *, wait_ack_timeout):
            stop_sent.set()
            if native_stop == "unacknowledged":
                return "timeout"
            # The prompt's response resolves both the stream's completion and
            # this acknowledgement: the acknowledgement arrives once the
            # completion has claimed the turn's row and started writing it.
            await asyncio.wait_for(persist_started.wait(), timeout=30)
            return "acked"

        state.sessions.cancel_owned_turn = AsyncMock(side_effect=_cancel_owned_turn)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=4.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            (fire_the_ceiling,) = deadline
            fire_the_ceiling()
            await asyncio.wait_for(stop_sent.wait(), timeout=30)
            if native_stop == "unacknowledged":
                # The agent never answers the stop: the task cancel is what ends
                # this wait. The bound only names a hang if that cancel never comes.
                await asyncio.wait_for(asyncio.Event().wait(), timeout=30)
            # The live stats move on after the completion reports; the footer
            # must show what the completion billed, not a second read.
            client.last_prompt_stats = _RealStats(credits=9.0)
            yield LLMEvent(
                kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED, usage=TurnUsage(credits=4.0)
            )

        client.stream = _stream
        client.stream_command = _stream

        with pytest.raises(td.TurnTimeoutError) as raised:
            await td.bounded_chat_turn(
                _run_chat(state, slot, "test message"), state=state, slot=slot
            )

        state.sessions.cancel_owned_turn.assert_awaited_once()
        expected_outcome = "acked" if native_stop == "acknowledged" else "timeout"
        assert (
            raised.value.native_cancel_outcome == expected_outcome
        ), "the native stop was not answered the way this case sets up"
        self._assert_cut_reply_footer(slot, 4.0)
        # An acknowledged stop keeps the footer the completion would have written,
        # its first-output latency included; a turn the seam bills itself records
        # none, as a Stop press does (``test_a_stop_before_any_billing...``).
        stats = self._assistant_row(slot, "partial answer")["meta"]["turn_stats"]
        if native_stop == "acknowledged":
            assert (
                stats.get("ttft_ms") == 250
            ), "the cut reply's footer lost the completion's ttft_ms"
        else:
            assert "ttft_ms" not in stats
        assert slot.messages[-1]["meta"]["kind"] == td.TURN_TIMEOUT_KIND
        await async_wait_until(lambda: writes_done)
        # An acknowledged stop is billed by the completion it produced; an
        # unanswered one by the seam, under the involuntary-cancel label.
        billed_by = "cancelled" if native_stop == "acknowledged" else "error: cancelled"
        assert [r["stop_reason"] for r in _rows(tmp_path)] == [billed_by]

    @pytest.mark.asyncio
    async def test_a_footer_put_on_after_a_flush_marks_the_slot_dirty(
        self, tmp_path, monkeypatch
    ) -> None:
        # The periodic flush writes only a dirty slot, and it can run after the
        # reply row was saved and before the footer goes on it in place: the
        # footer marks the slot dirty itself, or it stays off disk.
        from kiro_crew.acp.session_handle import AcpRequestTimeout
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="flushed-footer-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        real_pick = chat_runner._last_reply_row
        real_send = chat_runner._broadcast_turn_stats
        dirty_after_stamp = []

        def _flush_ran_first(slot_, *args, **kwargs):
            slot_._dirty = False
            return real_pick(slot_, *args, **kwargs)

        def _record_dirty(state_, slot_, row, stats):
            dirty_after_stamp.append(slot_._dirty)
            return real_send(state_, slot_, row, stats)

        monkeypatch.setattr(chat_runner, "_last_reply_row", _flush_ran_first)
        monkeypatch.setattr(chat_runner, "_broadcast_turn_stats", _record_dirty)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.5)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            raise AcpRequestTimeout("session/prompt timed out")

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        self._assert_cut_reply_footer(slot, 2.5)
        assert dirty_after_stamp == [True], "the footer went on without marking the slot dirty"

    @pytest.mark.asyncio
    async def test_a_cut_reply_gets_its_footer_in_a_window_at_its_cap(
        self, tmp_path, monkeypatch
    ) -> None:
        # At the window's cap every append front-trims a row, so the index the
        # turn started at stops naming its first row: the footer finds the reply
        # by the row the turn started after.
        from kiro_crew.acp.session_handle import AcpRequestTimeout
        from kiro_crew.dashboard import state as state_mod
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="capped-window-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        cap = 12
        monkeypatch.setattr(state_mod, "_MAX_SLOT_MESSAGES", cap)
        while len(slot.messages) < cap:
            slot.append("assistant", "an earlier reply", "msg msg-a", broadcast=False)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.5)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            raise AcpRequestTimeout("session/prompt timed out")

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        assert len(slot.messages) <= cap
        self._assert_cut_reply_footer(slot, 2.5)

    @pytest.mark.asyncio
    async def test_a_shutdown_saves_the_partial_reply_with_its_footer(
        self, tmp_path, monkeypatch
    ) -> None:
        # While the gateway shuts down, the turn's teardown skips its own save, so
        # the helper's save is the last write the reply gets: a footer put on
        # after it is gone when the gateway starts again.
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="shutdown-footer-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        state.sessions.final_drain_started = True
        saved_footers = []
        real_save = chat_runner.save_slot_off_loop

        async def _recording_save(state_, slot_, *args, **kwargs):
            saved_footers.extend(
                (m.get("meta") or {}).get("turn_stats")
                for m in slot_.messages
                if m.get("role") == "assistant" and m.get("content") == "partial answer"
            )
            return await real_save(state_, slot_, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _recording_save)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            raise asyncio.CancelledError()

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        assert saved_footers, "the shutdown never saved the partial reply"
        assert saved_footers[-1] is not None, "the reply was saved at shutdown without its footer"
        assert saved_footers[-1]["credits"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_a_shutdown_saves_a_footer_put_on_a_flushed_segment(
        self, tmp_path, monkeypatch
    ) -> None:
        # The reply's text was already a row (flushed at the tool call), so the
        # cut adds no text: the footer alone is new, and it must still be saved.
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="shutdown-segment-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        state.sessions.final_drain_started = True
        saved_footers = []
        real_save = chat_runner.save_slot_off_loop

        async def _recording_save(state_, slot_, *args, **kwargs):
            saved_footers.extend(
                (m.get("meta") or {}).get("turn_stats")
                for m in slot_.messages
                if m.get("role") == "assistant" and m.get("content") == "partial answer"
            )
            return await real_save(state_, slot_, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _recording_save)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            yield LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read")
            await _let_the_turn_clock_advance()
            raise asyncio.CancelledError()

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        assert saved_footers, "the footer put on at shutdown was never saved"
        assert saved_footers[-1] is not None, "the reply was saved at shutdown without its footer"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("billed", [True, False], ids=["billed", "unbilled"])
    @pytest.mark.parametrize("late_end", ["error", "cancel"])
    async def test_an_end_after_the_reply_was_saved_does_not_save_it_twice(
        self, tmp_path, monkeypatch, late_end, billed
    ) -> None:
        # The end-of-turn flush saved the reply and gave it its footer, and the
        # text it saved stays in the turn's buffer for the legs after it. An
        # error or a cancel landing after that reaches the seam with the text
        # still there, and with the completion's figures captured whether or not
        # it billed.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name=f"late-{late_end}-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        late = (
            RuntimeError("context read failed") if late_end == "error" else asyncio.CancelledError()
        )
        state.sessions.check_context_usage = MagicMock(side_effect=late)
        patches = []

        def _record(event, payload, *args, **kwargs):
            if event == "chat_message_update" and "turn_stats" in (payload.get("meta") or {}):
                patches.append(payload)

        state.broadcast_ws = MagicMock(side_effect=_record)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer")
            await _let_the_turn_clock_advance()
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_END_TURN,
                usage=TurnUsage(credits=2.0) if billed else TurnUsage(),
            )

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        replies = [m for m in slot.messages if m.get("role") == "assistant"]
        assert [m["content"] for m in replies] == ["the answer"], "the reply was saved twice"
        assert (replies[0].get("meta") or {}).get("turn_stats") is not None
        # The footer the completion put on is the reply's: the late end neither
        # stamps it again nor sends a patch for it.
        assert patches == [], "the late end stamped the completed reply's footer again"
        # A completion that billed wrote the row; otherwise the late end bills
        # the turn once, under its own label.
        if billed:
            expected = STOP_REASON_END_TURN
        else:
            expected = "error: terminal" if late_end == "error" else "error: cancelled"
        assert [r["stop_reason"] for r in _rows(tmp_path)] == [expected]

    @pytest.mark.asyncio
    async def test_a_failing_reply_save_still_queues_the_recovery(
        self, tmp_path, monkeypatch
    ) -> None:
        # The helper is best-effort: a recovery arm that calls it still owes its
        # recovery, so a failure inside it must not turn a stall into an error.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        monkeypatch.setattr(chat_runner, "_recovery_delay", AsyncMock())
        state, slot, client = _drive_state_and_slot(tmp_path, name="failing-save-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        real_redact = chat_runner._redact_segment
        redactions = 0

        def _first_save_fails(slot_, body):
            nonlocal redactions
            redactions += 1
            if redactions == 1:
                raise RuntimeError("redaction failed")
            return real_redact(slot_, body)

        monkeypatch.setattr(chat_runner, "_redact_segment", _first_save_fails)
        attempts = 0

        async def _stream(msg):
            nonlocal attempts
            attempts += 1
            client.last_prompt_stats = _RealStats(credits=1.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"answer {attempts}")
            await _let_the_turn_clock_advance()
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason="stale_recover" if attempts == 1 else STOP_REASON_END_TURN,
                usage=TurnUsage(credits=1.0),
            )

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")
        await drain_background_tasks(state)

        assert attempts == 2, "a failure saving the partial reply cancelled the recovery"
        assert not [m for m in slot.messages if m.get("content") == "redaction failed"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stop_reason",
        [
            "stale_recover",
            "error: tool stall",
            "error: compaction failed",
            "error: connection reset",
        ],
        ids=["stale-recover", "tool-stall", "compaction-failed", "connection-lost"],
    )
    async def test_a_recovery_arm_bills_a_turn_its_completion_did_not(
        self, tmp_path, monkeypatch, stop_reason
    ) -> None:
        # The completion reported no billing, so it wrote no row; the provider
        # still holds the turn's credits. The seam bills the turn once, under the
        # arm's own stop reason, and the footer shows the figure that row records.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        monkeypatch.setattr(chat_runner, "_recovery_delay", AsyncMock())
        state, slot, client = _drive_state_and_slot(tmp_path, name="unbilled-arm-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        attempts = 0

        async def _stream(msg):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                client.last_prompt_stats = _RealStats(credits=4.0)
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                await _let_the_turn_clock_advance()
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason, usage=TurnUsage())
                return
            client.last_prompt_stats = _RealStats(credits=1.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered answer")
            await _let_the_turn_clock_advance()
            yield LLMEvent(
                kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN, usage=TurnUsage(credits=1.0)
            )

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")
        self._assert_cut_reply_footer(slot, 4.0)
        await drain_background_tasks(state)

        cut_rows = [r for r in _rows(tmp_path) if r["stop_reason"] == stop_reason]
        assert [r["credits"] for r in cut_rows] == [pytest.approx(4.0)]
        recovers = stop_reason != "error: compaction failed"
        assert len(_rows(tmp_path)) == (2 if recovers else 1)

    @pytest.mark.asyncio
    async def test_a_cancel_on_the_shutdown_save_still_leaves_the_arm_its_recovery(
        self, tmp_path, monkeypatch
    ) -> None:
        # The gateway is shutting down and a cancel lands on the helper's save:
        # the stall arm that called it still posts its card and queues its
        # recovery, and the turn's usage row is still written.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="cancelled-save-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        state.sessions.final_drain_started = True
        real_save = chat_runner.save_slot_off_loop
        cancelled_saves = 0

        async def _cancel_the_reply_save(state_, slot_, *args, **kwargs):
            # The helper's save is the one that runs once the reply row exists.
            nonlocal cancelled_saves
            if any(m.get("content") == "partial answer" for m in slot_.messages):
                cancelled_saves += 1
                raise asyncio.CancelledError()
            return await real_save(state_, slot_, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _cancel_the_reply_save)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            await _let_the_turn_clock_advance()
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="stale_recover", usage=TurnUsage())

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        assert cancelled_saves == 1, "the cancel never reached the helper's save"
        self._assert_cut_reply_footer(slot, 2.0)
        cards = [m for m in slot.messages if m.get("role") == "error"]
        assert cards, "the cancelled save cost the stall arm its card"
        assert cards[-1]["content"] == "⟳ Recovering a stalled turn…"
        assert slot._stale_recovery_retries == 1, "the cancelled save cost the arm its recovery"
        rows = _rows(tmp_path)
        assert [r["stop_reason"] for r in rows] == ["stale_recover"]
        assert rows[0]["credits"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_a_stall_sends_the_cut_reply_its_footer_before_the_recovery_runs(
        self, tmp_path, monkeypatch
    ) -> None:
        # The recovery is queued and takes the slot over without a ``chat_done``,
        # so no refresh reaches the client until it finishes: the footer goes out
        # on its own patch, while the recovery has not yet started.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        monkeypatch.setattr(chat_runner, "_recovery_delay", AsyncMock())
        state, slot, client = _drive_state_and_slot(tmp_path, name="live-footer-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)
        attempts = 0
        patches = []

        def _record(event, payload, *args, **kwargs):
            if event == "chat_message_update" and "turn_stats" in (payload.get("meta") or {}):
                patches.append((attempts, payload))

        state.broadcast_ws = MagicMock(side_effect=_record)

        async def _stream(msg):
            nonlocal attempts
            attempts += 1
            client.last_prompt_stats = _RealStats(credits=3.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"answer {attempts}")
            await _let_the_turn_clock_advance()
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason="stale_recover" if attempts == 1 else STOP_REASON_END_TURN,
                usage=TurnUsage(credits=3.0),
            )

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")
        await drain_background_tasks(state)

        assert attempts == 2
        cut = self._assistant_row(slot, "answer 1")
        assert patches, "the cut reply's footer was never sent to open clients"
        sent_at, payload = patches[0]
        assert sent_at == 1, "the footer went out only after the recovery started"
        assert payload["slot"] == slot.key
        assert payload["mid"] == cut["meta"]["mid"]
        assert payload["meta"]["turn_stats"] == cut["meta"]["turn_stats"]

    @pytest.mark.asyncio
    async def test_a_failed_compaction_puts_the_footer_on_the_reply_not_its_notice(
        self, tmp_path, monkeypatch
    ) -> None:
        # The text was saved at the tool call, and the failed compaction's notice
        # is the turn's last assistant-role row: the footer belongs on the text.
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="compaction-notice-slot")
        client.last_prompt_stats = _RealStats(credits=0.0)

        async def _stream(msg):
            client.last_prompt_stats = _RealStats(credits=2.0)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
            yield LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read")
            await _let_the_turn_clock_advance()
            yield LLMEvent(kind=EVENT_COMPACTION_STATUS, text="failed")
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason="error: compaction failed",
                usage=TurnUsage(credits=2.0),
            )

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        notices = [m for m in slot.messages if (m.get("meta") or {}).get("kind") == "compaction"]
        assert notices, "the failed compaction appended no notice"
        assert all("turn_stats" not in (m.get("meta") or {}) for m in notices)
        self._assert_cut_reply_footer(slot, 2.0)


def test_the_last_reply_row_passes_over_a_system_notice() -> None:
    from kiro_crew.dashboard.chat_turn.file_changes import _note_reply_row
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("last-reply-row")
    slot.append("assistant", "earlier turn", "msg msg-a", broadcast=False)
    boundary = len(slot.messages)
    _note_reply_row(slot, slot.append("assistant", "the reply", "msg msg-a", broadcast=False))
    slot.append("tool", "ran a tool", "msg msg-tool", broadcast=False)
    # Recorded as well, so only the notice rule can pass over it.
    notice = slot.append(
        "assistant", "notice", "msg msg-a", meta={"kind": "compaction"}, broadcast=False
    )
    _note_reply_row(slot, notice)

    assert chat_runner._last_reply_row(slot, boundary, None)["content"] == "the reply"
    assert chat_runner._last_reply_row(slot, len(slot.messages), None) is None


def test_the_last_reply_row_is_one_the_runner_recorded() -> None:
    # A workflow completion lands in the window as an assistant row another
    # writer appended mid-turn: it is newer than the reply, and not the reply.
    from kiro_crew.dashboard.chat_turn.file_changes import _note_reply_row
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("recorded-reply-row")
    slot.append("user", "hello", "msg msg-u", broadcast=False)
    start = chat_runner.row_mid(slot.messages[-1])
    _note_reply_row(slot, slot.append("assistant", "the reply", "msg msg-a", broadcast=False))
    slot.append("assistant", "[Workflow completion event]", "msg msg-a", broadcast=False)

    assert chat_runner._last_reply_row(slot, 0, start)["content"] == "the reply"


class TestAbnormalEndFooter:
    """Every reply a turn leaves keeps its footer, however the turn ended.

    A completed turn's last reply row carries ``meta.turn_stats``. A turn that
    ends on an error card, a mid-reply 5xx, a stall recovery, a prompt timeout
    or a cancel landing late must leave the text it streamed as ONE saved reply
    row, with that footer, above whatever card follows.
    """

    @staticmethod
    def _reply_rows(slot, text):
        return [m for m in slot.messages if m.get("role") == "assistant" and text in m["content"]]

    @staticmethod
    def _footer(row):
        return (row.get("meta") or {}).get("turn_stats")

    def _wire(self, tmp_path, monkeypatch, name, stream):
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name=name)
        client.last_prompt_stats = _RealStats(credits=0.0)
        client.stream = stream(client)
        client.stream_command = client.stream
        return state, slot, client

    @pytest.mark.asyncio
    async def test_a_terminal_error_after_a_chunk_keeps_the_footer(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.client import AcpError
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                client.last_prompt_stats = _RealStats(credits=4.0)
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                await _let_the_turn_clock_advance()
                raise AcpError("Prompt error: the request was malformed")

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "terminal-error-slot", _stream)
        await _run_chat(state, slot, "test message")

        (row,) = self._reply_rows(slot, "partial answer")
        stats = self._footer(row)
        assert stats is not None, "a reply cut by an error card carries no footer"
        assert stats["credits"] == pytest.approx(4.0)
        assert stats["model"] == "served-model"

    @pytest.mark.asyncio
    async def test_a_post_token_transient_keeps_the_footer_on_the_partial(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.client import AcpError
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        calls = 0

        def _stream(client):
            async def _s(msg):
                nonlocal calls
                calls += 1
                if calls == 1:
                    client.last_prompt_stats = _RealStats(credits=1.5)
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                    await _let_the_turn_clock_advance()
                    raise AcpError(
                        "Prompt error: {'message': 'Internal error: API Error: "
                        "Internal server error'}"
                    )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="continued answer")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "transient-slot", _stream)
        # The recovery's own backoff seam, so the test never waits on it.
        monkeypatch.setattr(chat_runner, "_recovery_delay", AsyncMock())
        await _run_chat(state, slot, "test message")

        (row,) = self._reply_rows(slot, "partial answer")
        stats = self._footer(row)
        assert stats is not None, "the partial above a 5xx recovery carries no footer"
        assert stats["credits"] == pytest.approx(1.5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stop_reason",
        [chat_runner.STOP_REASON_STALE_RECOVER, chat_runner.STOP_REASON_TOOL_STALL],
    )
    async def test_a_stall_recovery_saves_the_streamed_text_with_its_footer(
        self, tmp_path, monkeypatch, stop_reason
    ) -> None:
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        calls = 0

        def _stream(client):
            async def _s(msg):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                    await _let_the_turn_clock_advance()
                    yield LLMEvent(
                        kind=EVENT_COMPLETE,
                        stop_reason=stop_reason,
                        usage=TurnUsage(credits=3.0),
                    )
                    return
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "stall-slot", _stream)
        await _run_chat(state, slot, "test message")

        rows = self._reply_rows(slot, "partial answer")
        assert len(rows) == 1, "the text streamed before the recovery card was not saved"
        stats = self._footer(rows[0])
        assert stats is not None
        assert stats["credits"] == pytest.approx(3.0)
        assert not any(m.get("role") == "chunk" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_a_prompt_timeout_saves_the_streamed_text_with_its_footer(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.session_handle import AcpRequestTimeout
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                client.last_prompt_stats = _RealStats(credits=2.5)
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                await _let_the_turn_clock_advance()
                raise AcpRequestTimeout("session/prompt timed out")

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "prompt-timeout-slot", _stream)
        await _run_chat(state, slot, "test message")

        rows = self._reply_rows(slot, "partial answer")
        assert len(rows) == 1, "the text streamed before a prompt timeout was not saved"
        stats = self._footer(rows[0])
        assert stats is not None
        assert stats["credits"] == pytest.approx(2.5)
        # The usage row the arm never wrote before.
        (usage_row,) = _rows(tmp_path)
        assert usage_row["credits"] == pytest.approx(2.5)

    @pytest.mark.asyncio
    async def test_a_cancel_after_completion_keeps_the_completion_footer(
        self, tmp_path, monkeypatch
    ) -> None:
        # The turn ceiling's shape: the agent acknowledges the stop, so
        # EVENT_COMPLETE writes the usage row, then the task cancel lands on
        # an await before the end-of-turn attach.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                client.last_prompt_stats = _RealStats(credits=6.0)
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                await _let_the_turn_clock_advance()
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=chat_runner.STOP_REASON_CANCELLED,
                    usage=TurnUsage(credits=6.0),
                )

            return _s

        async def _cancel_after_write(*args, **kwargs):
            raise asyncio.CancelledError()

        state, slot, _ = self._wire(tmp_path, monkeypatch, "ceiling-slot", _stream)
        monkeypatch.setattr(chat_runner, "persist_token_record_async", _cancel_after_write)
        await _run_chat(state, slot, "test message")

        (row,) = self._reply_rows(slot, "partial answer")
        stats = self._footer(row)
        assert stats is not None, "a cancel after completion left the reply bare"
        assert stats["credits"] == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_a_cancel_after_the_end_of_turn_flush_saves_the_reply_once(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="final answer")
                await _let_the_turn_clock_advance()
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_END_TURN,
                    usage=TurnUsage(credits=1.0),
                )

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "late-cancel-slot", _stream)
        real_save = chat_runner.save_slot_off_loop
        cancelled = False
        footer_before_cancel: dict = {}

        async def _cancel_on_the_end_of_turn_save(st, sl, *args, **kwargs):
            # The end-of-turn save is the first one after the reply row exists.
            nonlocal cancelled, footer_before_cancel
            replies = [m for m in sl.messages if m.get("role") == "assistant"]
            if not cancelled and replies:
                cancelled = True
                footer_before_cancel = dict(self._footer(replies[-1]) or {})
                raise asyncio.CancelledError()
            return await real_save(st, sl, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _cancel_on_the_end_of_turn_save)
        await _run_chat(state, slot, "test message")

        assert cancelled, "the end-of-turn save never ran"
        (row,) = self._reply_rows(slot, "final answer")
        # The footer the end-of-turn attach wrote survives the late cancel whole,
        # first-token time included.
        assert self._footer(row) == footer_before_cancel

    @pytest.mark.asyncio
    async def test_a_late_cancel_leaves_a_row_another_writer_appended_alone(
        self, tmp_path, monkeypatch
    ) -> None:
        # A workflow result lands in the window after the reply, then the turn
        # is cancelled: the footer stays on this turn's reply, never on that row.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="final answer")
                await _let_the_turn_clock_advance()
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_END_TURN,
                    usage=TurnUsage(credits=1.0),
                )

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "foreign-row-slot", _stream)
        real_save = chat_runner.save_slot_off_loop
        cancelled = False

        async def _inject_then_cancel(st, sl, *args, **kwargs):
            nonlocal cancelled
            if not cancelled and any(m.get("role") == "assistant" for m in sl.messages):
                cancelled = True
                sl.append("assistant", "workflow result", "msg msg-a")
                raise asyncio.CancelledError()
            return await real_save(st, sl, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _inject_then_cancel)
        await _run_chat(state, slot, "test message")

        assert cancelled
        (foreign,) = self._reply_rows(slot, "workflow result")
        assert self._footer(foreign) is None, "the turn's footer landed on another writer's row"
        (row,) = self._reply_rows(slot, "final answer")
        assert self._footer(row)["credits"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_a_completed_turn_leaves_a_row_another_writer_appended_alone(
        self, tmp_path, monkeypatch
    ) -> None:
        # The end-of-turn attach uses the same narrowed scan: a workflow result
        # that lands just before it does not take the turn's footer.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="final answer")
                await _let_the_turn_clock_advance()
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_END_TURN,
                    usage=TurnUsage(credits=2.0),
                )

            return _s

        state, slot, _ = self._wire(tmp_path, monkeypatch, "foreign-complete-slot", _stream)
        real_attach = chat_runner._attach_turn_stats

        def _inject_then_attach(sl, *args, **kwargs):
            if not self._reply_rows(sl, "workflow result"):
                sl.append("assistant", "workflow result", "msg msg-a")
            return real_attach(sl, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "_attach_turn_stats", _inject_then_attach)
        await _run_chat(state, slot, "test message")

        (foreign,) = self._reply_rows(slot, "workflow result")
        assert self._footer(foreign) is None, "the turn's footer landed on another writer's row"
        (row,) = self._reply_rows(slot, "final answer")
        assert self._footer(row)["credits"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_a_recovery_arm_that_then_fails_saves_the_reply_once(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        def _stream(client):
            async def _s(msg):
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")
                yield LLMEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=chat_runner.STOP_REASON_STALE_RECOVER,
                    usage=TurnUsage(credits=1.0),
                )

            return _s

        def _fails(*args, **kwargs):
            raise RuntimeError("recovery prompt unavailable")

        state, slot, _ = self._wire(tmp_path, monkeypatch, "recover-then-fail-slot", _stream)
        # Fails after the arm saved the reply, so the generic error arm runs next.
        monkeypatch.setattr(chat_runner, "build_stale_recovery_prompt", _fails)
        await _run_chat(state, slot, "test message")

        assert len(self._reply_rows(slot, "partial answer")) == 1, "the reply was saved twice"
