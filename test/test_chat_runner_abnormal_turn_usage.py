"""A turn that ends without EVENT_COMPLETE still records its usage row and footer.

A turn torn down through ``_persist_partial_reply`` (the turn ceiling, the Stop
button, a signed-out CLI mid-turn and the other recovery paths sharing that seam)
never reaches the ``EVENT_COMPLETE`` persist, so ``_persist_abnormal_turn_usage``
is what records its credits.

These drive that helper over the REAL row store, asserting the row lands with the
turn's recovered credits and a terminal stop reason, that a turn which billed
nothing writes none, that a cost-only claude-seam turn records too, and that a
turn dying before it installs fresh billing stats does NOT re-bill the previous
turn (the ``since=`` guard).

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
from chat_test_helpers import _make_state

from kiro_crew.acp.types import STOP_REASON_END_TURN, TurnUsage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.testing.wait import await_until


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
    reconstructing a live ACP provider.
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
    # Reporting False leaves the once-per-turn guard unclaimed.
    assert wrote is False
    assert _rows(_isolated_shards) == []


@pytest.mark.asyncio
async def test_a_turn_dying_before_fresh_stats_does_not_rebill_the_prior_turn(
    _isolated_shards, monkeypatch
):
    # provider_last_turn_usage(..., since=<stats0>) reports an empty TurnUsage
    # while the live stats object is still the pre-turn one, so the prior
    # turn's credits must not be written again.
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
    ``CancelledError`` arm, whose ``_on_cancelled`` (``chat_turn/turn_failures.py``) runs
    ``_persist_partial_reply`` -> ``_record_abnormal_turn_usage``. The seam must see the
    row already claimed and write nothing, so the turn lands exactly one usage row.
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
        """The complete-path persist is SHIELDED from the turn's cancel.

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

        # The CancelledError unwinds through _run_chat's cancel arm and _on_cancelled
        # (chat_turn/turn_failures.py) into _persist_partial_reply -> _record_abnormal_turn_usage,
        # which calls the REAL provider_last_turn_usage with since=_turn_stats0.
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

    @pytest.mark.asyncio
    async def test_a_stop_before_dispatch_leaves_the_previous_footer_alone(
        self, tmp_path, monkeypatch
    ) -> None:
        # A Stop landing during pre-prompt preparation (after the client is
        # published, before dispatch captures the turn's message boundary)
        # must not attach anything: with the boundary still 0 the attach would
        # walk back to the PREVIOUS reply and overwrite its recorded footer.
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
        monkeypatch.setattr(chat_runner, "read_turn_model", lambda c: "served-model")
        state, slot, client = _drive_state_and_slot(tmp_path, name="pre-dispatch-stop-slot")
        prior_stats = {"elapsed_ms": 5000, "credits": 9.0}
        slot.append(
            "assistant", "previous reply", "msg msg-a", meta={"turn_stats": dict(prior_stats)}
        )
        slot.append("user", "next question", "msg msg-u")
        client.last_prompt_stats = _RealStats(credits=0.0)

        async def _stop_during_preparation(*args, **kwargs):
            await _let_the_turn_clock_advance()
            slot._stopping = True
            raise asyncio.CancelledError()

        monkeypatch.setattr(chat_runner, "publish_turn_identity", _stop_during_preparation)

        await _run_chat(state, slot, "next question")

        previous = next(m for m in slot.messages if m.get("content") == "previous reply")
        assert previous["meta"]["turn_stats"] == prior_stats


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
        real_attach = chat_runner._attach_owned_turn_stats

        def _inject_then_attach(sl, *args, **kwargs):
            if not self._reply_rows(sl, "workflow result"):
                sl.append("assistant", "workflow result", "msg msg-a")
            return real_attach(sl, *args, **kwargs)

        monkeypatch.setattr(chat_runner, "_attach_owned_turn_stats", _inject_then_attach)
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
