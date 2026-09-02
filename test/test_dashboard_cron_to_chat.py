"""Tests for cron dashboard chat threading (inject_cron_result_to_dashboard)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard, run_marker
from kiro_crew.session_surface import set_dashboard_surfaced


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """inject_cron_result_to_dashboard publishes to the process-global
    dashboard-surface registry; reset it so keys from these mock states
    never leak into other tests."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _make_state(history_messages=None):
    """Create a mock DashboardState with conversation_log."""
    state = MagicMock()
    slots = {}
    # Real dict: inject_cron_result_to_dashboard publishes the surface registry
    # via _sync_dashboard_slots, which iterates state._slots.values().
    state._slots = slots

    def get_or_create_slot(name=None, agent="", origin=""):
        # ``origin`` is recorded, not just tolerated: the cron paths must
        # declare SlotOrigin.CRON, and a fake that swallowed the kwarg
        # would let that regress silently (a cron slot relabelled USER is
        # readable by any app holding `slots:user`).
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot._origin = origin
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True, meta=None):
                # Mirror the real ``_ChatSlot.append`` contract: preserve a
                # supplied ``meta.mid``, mint one otherwise, and hand the
                # appended row back — the injector reads the id off the return
                # to stamp the durable transcript copy.
                supplied = meta.get("mid") if isinstance(meta, dict) else None
                msg = {
                    "role": role,
                    "content": content,
                    "cls": cls,
                    "meta": {
                        **(meta if isinstance(meta, dict) else {}),
                        "mid": supplied or f"m-test-{len(slot.messages)}",
                    },
                }
                slot.messages.append(msg)
                return msg

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    return state


def _make_job(
    job_id="abc123",
    name="test-cron",
    last_result="Hello world",
    message="do the thing",
    last_result_ts=0.0,
    timezone="UTC",
):
    # ``message``, ``last_result_ts``, ``last_result_stamp`` and ``timezone``
    # are real values, not Mock attributes: the injector writes the run's prompt
    # as a paired ``user`` row and reads the rendered stamp, so a MagicMock here
    # would reach the redactors as a non-string.
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.last_result = last_result
    job.message = message
    job.last_result_ts = last_result_ts
    job.timezone = timezone
    # Rendered by the PRODUCTION renderer rather than hand-written, so the fake
    # cannot drift from the spelling the executor actually persists.
    job.last_result_stamp = CronJob._render_run_stamp(job, last_result_ts)
    job.agent_id = ""
    return job


def _inject(state, job, result_text, **kw):
    """The injection, with the transcript read its async callers now prefetch.

    ``history`` is a required parameter in production so that no async caller can
    leave the whole-transcript parse on the event loop (issue #7408). These tests
    drive the function synchronously, where a blocking read is the caller's own
    cost, so the read that used to live inside the injection lives here instead.
    """
    kw.setdefault(
        "history",
        state.conversation_log.read_messages(f"cron:{job.id}") if state.conversation_log else [],
    )
    inject_cron_result_to_dashboard(state, job, result_text, **kw)


class TestInjectCronResultToDashboard:
    def test_history_is_required_so_the_read_cannot_land_on_the_loop(self):
        """Omitting the prefetch is a TypeError at the call, not a production stall.

        Four of the five defects issue #7408 fixed were async callers that simply
        did not pass ``history=``, leaving this synchronous function to parse the
        whole transcript on the event loop. The parameter has no default so that
        omission cannot compile, rather than being caught by a convention in a
        spec file.
        """
        with pytest.raises(TypeError, match="history"):
            inject_cron_result_to_dashboard(_make_state(), _make_job(), "result")

    def test_slot_is_tagged_cron_not_user(self):
        """A cron result is the job's output, not something the person typed.

        The slot used to be created untagged and then labelled USER by
        get_or_create_slot's default, which put private cron content inside the
        ``slots:user`` WS scope -- so any app holding that scope received it.
        """
        from kiro_crew.dashboard.state import SlotOrigin

        state = _make_state()
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot._origin == SlotOrigin.CRON
        assert slot._origin != SlotOrigin.USER

    def test_sets_linked_session_key(self):
        state = _make_state()
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"

    def test_sets_title_from_job_name(self):
        state = _make_state()
        job = _make_job(name="daily-standup")
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert "daily-standup" in slot.title

    def test_hydrates_history_on_first_link(self):
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
        ]
        state = _make_state(history_messages=history)
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # History (2) + the run's prompt/result pair (2) = 4 messages
        assert len(slot.messages) == 4
        assert slot.messages[0]["content"] == "msg1"
        assert slot.messages[1]["content"] == "msg2"

    def test_hydrates_max_50_messages(self):
        history = [{"role": "assistant", "content": f"msg{i}"} for i in range(100)]
        state = _make_state(history_messages=history)
        job = _make_job()
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # 50 from history + the run's prompt/result pair (2) = 52
        assert len(slot.messages) == 52

    def test_does_not_rehydrate_on_second_call(self):
        history = [{"role": "assistant", "content": "old"}]
        state = _make_state(history_messages=history)
        job = _make_job()
        _inject(state, job, "result1")
        _inject(state, job, "result2")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # history(1) + run1 pair(2) + run2's result(1) = 4 (no re-hydration).
        # Both runs carry the same unstamped prompt row, so the second run's
        # prompt dedups against the first while its differing result is kept.
        assert len(slot.messages) == 4

    def test_dedup_prevents_duplicate_result(self):
        """One run injected twice stays one pair.

        This is what makes ``/to-chat`` idempotent against the executor's own
        auto-inject: both read the stamp off the job, so they render the same
        bytes and the second call adds nothing.
        """
        state = _make_state()
        job = _make_job(last_result_ts=1_756_000_000.0)
        _inject(state, job, "same result")
        _inject(state, job, "same result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert len(slot.messages) == 2
        assert [m["role"] for m in slot.messages] == ["user", "assistant"]

    def test_two_runs_with_identical_text_stay_two_rows(self):
        """Different runs must not collapse into one undated row.

        A daily job whose output repeats used to dedup the second day away, so
        the tab showed one row for N runs and a follow-up turn could not tell
        which run it was answering. The per-run stamp keeps them distinct.
        """
        state = _make_state()
        monday = _make_job(last_result_ts=1_756_000_000.0)
        tuesday = _make_job(last_result_ts=1_756_086_400.0)
        _inject(state, monday, "same result")
        _inject(state, tuesday, "same result")
        slot = state.get_or_create_slot(name=f"cron-{monday.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 2, "two runs collapsed into one row"
        assert results[0] != results[1]
        assert "2025-08-24" in results[0] and "2025-08-25" in results[1]

    def test_two_runs_in_the_same_minute_stay_two_rows(self):
        """Stamp resolution bounds which distinct runs can collapse.

        The stamp is part of the compared content, so a minute-resolution stamp
        silently merged two runs that finished in the same minute with identical
        output -- reachable for a fast script cron and for two manual runs.
        Seconds keep them apart.
        """
        state = _make_state()
        first = _make_job(last_result_ts=1_756_000_000.0)
        second = _make_job(last_result_ts=1_756_000_020.0)
        _inject(state, first, "same result")
        _inject(state, second, "same result")
        slot = state.get_or_create_slot(name=f"cron-{first.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 2, "same-minute runs collapsed into one row"
        assert results[0] != results[1]

    def test_two_runs_in_the_same_second_stay_two_rows(self):
        """Identity must not inherit the DISPLAY stamp's resolution.

        Both dedup layers compare row content, so while identity rode on the
        human-readable stamp its resolution was the bound: at minutes two runs in
        one minute collapsed, and at seconds two runs in one second still did.
        The invisible run marker carries the timestamp at full precision, so the
        bound is gone rather than moved.
        """
        state = _make_state()
        first = _make_job(last_result_ts=1_756_000_000.100000)
        second = _make_job(last_result_ts=1_756_000_000.900000)
        _inject(state, first, "same result")
        _inject(state, second, "same result")
        slot = state.get_or_create_slot(name=f"cron-{first.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 2, "same-second runs collapsed into one row"
        assert results[0] != results[1]
        # The DISPLAYED stamp is identical for both -- the marker is what
        # separates them, which is the whole point of splitting the two.
        assert first.last_result_stamp == second.last_result_stamp

    def test_the_run_marker_does_not_render_and_survives_a_reload(self):
        """The marker is identity, not content the person is meant to read.

        An HTML comment so the tab shows only the stamp, and fixed 6dp so a job
        reloaded from the JSON store renders the byte-identical marker the
        executor wrote -- otherwise a reloaded job would re-append every row.
        """
        job = _make_job(last_result_ts=1_756_000_000.5)
        marker = run_marker(job)
        assert marker.strip().startswith("<!--") and marker.strip().endswith("-->")
        assert f"{job.id}:1756000000.500000" in marker
        # Survives the store round-trip the same way _job_from_record restores it.
        reloaded = _make_job(last_result_ts=float(f"{1_756_000_000.5:.6f}"))
        assert run_marker(reloaded) == marker

    def test_a_legacy_unstamped_run_carries_no_marker(self):
        """A row already on disk must keep dedup-ing against its own spelling.

        A store written before this shipped has no timestamp, so adding a marker
        to its rows would make every one of them look new and re-append the lot.
        """
        job = _make_job(last_result_ts=0.0)
        assert run_marker(job) == ""
        state = _make_state()
        _inject(state, job, "legacy output")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert "<!-- cron-run:" not in results[0]

    def test_editing_the_timezone_after_a_run_does_not_respell_its_row(self):
        """A re-render under edited config would duplicate a row already on disk.

        ``append_if_absent`` judges "already persisted" by ``(role, content)``
        and the stamp is inside that content, so re-rendering it at injection
        time meant an edited ``timezone`` spelled the SAME run differently --
        appending a second copy instead of collapsing onto the first. The stamp
        is rendered once, when the result is recorded, so a later edit cannot
        reach it.
        """
        state = _make_state()
        job = _make_job(last_result_ts=1_756_000_000.0)
        _inject(state, job, "the result")
        # The user moves the job to another zone AFTER the run.
        job.timezone = "Asia/Tokyo"
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        results = [m["content"] for m in slot.messages if m["role"] == "assistant"]
        assert len(results) == 1, "a timezone edit duplicated an existing run row"

    def test_re_surfacing_does_not_write_a_prompt_row(self):
        """Only the run that produced a result knows the prompt behind it.

        ``/to-chat`` re-surfaces a STORED result, and ``job.message`` is live
        configuration a user can edit afterwards -- so pairing the two there
        attributes the output to an instruction that never ran. The re-surfacing
        caller writes the result alone.
        """
        state = _make_state()
        job = _make_job(message="the message as edited later")
        _inject(state, job, "output from an earlier run", include_prompt=False)
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["assistant"]
        assert "the message as edited later" not in slot.messages[0]["content"]

    def test_the_pair_is_persisted_as_one_grouped_write(self):
        """Both rows reach the durable log in a single ordered call.

        Two separate off-loop dispatches could interleave or half-fail, so the
        replay a follow-up turn reads could carry a reversed or partial run.
        """
        state = _make_state()
        job = _make_job(message="do the thing", last_result_ts=1_756_000_000.0)
        with patch("kiro_crew.dashboard.cron_inject.append_rows_if_absent_off_loop") as durable:
            _inject(state, job, "the result")
        assert durable.call_count == 1, "the pair must be ONE grouped write"
        rows = durable.call_args.args[2]
        assert [r[0] for r in rows] == ["user", "assistant"], "prompt row first"
        assert "do the thing" in rows[0][1]
        assert "the result" in rows[1][1]
        # Each row carries the window's own mid so the bounded read's identity
        # walk can match it rather than re-appending the injection.
        assert all(r[3] for r in rows)

    def test_nothing_is_persisted_when_the_run_produced_no_result(self):
        """No result means no run boundary, so the durable write never fires."""
        state = _make_state()
        job = _make_job()
        with patch("kiro_crew.dashboard.cron_inject.append_rows_if_absent_off_loop") as durable:
            _inject(state, job, "")
        durable.assert_not_called()

    def test_the_prompt_is_persisted_verbatim(self):
        """The replay must record the prompt the run was GIVEN.

        The executor sends ``job.message`` as written, so trimming it on the way
        to the transcript records a different instruction than the one that ran.
        Leading indentation is not decoration in a message carrying a fenced
        block or an indented snippet.
        """
        state = _make_state()
        message = "    indented line\n\n```\ncode\n```\n"
        job = _make_job(message=message, last_result_ts=1_756_000_000.0)
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        prompt_row = next(m["content"] for m in slot.messages if m["role"] == "user")
        assert message in prompt_row, "the prompt was altered on the way to the row"

    def test_a_whitespace_only_message_writes_no_prompt_row(self):
        """Verbatim storage must not turn an empty message into a blank boundary.

        The emptiness test still runs on the stripped value, so a job whose
        message is only whitespace has no prompt to record -- writing the row
        anyway would put a run header in the tab above nothing.
        """
        state = _make_state()
        job = _make_job(message="   \n\t\n ")
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["assistant"]

    def test_the_marker_precedes_the_untrusted_body(self):
        """An unclosed fence in the body must not render the marker as code.

        A prompt or result is untrusted text; everything after an unclosed ```
        fence renders as code, so a marker placed at the end of the row would be
        printed verbatim in the tab instead of hidden. It goes ahead of the body.
        """
        state = _make_state()
        job = _make_job(message="```\nunclosed prompt fence", last_result_ts=1_756_000_000.0)
        _inject(state, job, "```\nunclosed result fence")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        for msg in slot.messages:
            body = msg["content"]
            assert "<!-- cron-run:" in body
            assert body.index("<!-- cron-run:") < body.index(
                "```"
            ), "the marker must sit before the fence that would swallow it"

    def test_prompt_row_is_written_beside_the_result(self):
        """A follow-up turn needs to see what the run was asked, not just its output.

        The executor streams the prompt straight to the provider and never
        persists it, so the replay a follow-up turn opens with carried results
        alone -- nothing said which instruction produced any of them.
        """
        state = _make_state()
        job = _make_job(message="summarize yesterday")
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert [m["role"] for m in slot.messages] == ["user", "assistant"]
        assert "summarize yesterday" in slot.messages[0]["content"]

    def test_no_prompt_row_without_a_result(self):
        """A boundary row must never appear with nothing behind it.

        ``/to-chat`` on a job that has never produced a result reaches the
        injection with an empty ``result_text``; writing the prompt there would
        put a run header in the tab for a run that never happened.
        """
        state = _make_state()
        job = _make_job()
        _inject(state, job, "")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.messages == []

    def test_empty_result_creates_slot_without_message(self):
        state = _make_state()
        job = _make_job()
        _inject(state, job, "")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"
        assert len(slot.messages) == 0

    def test_pushes_slots_update(self):
        state = _make_state()
        job = _make_job()
        _inject(state, job, "result")
        state.push_slots_update.assert_called_once()

    def test_publishes_the_tab_to_the_surface_registry(self):
        """Regression: the cron tab must be surfaced the moment it is created.

        Every gate that asks "does this session have a tab?" — sub-agent event
        routing, completion injection, widget/question delivery — reads the
        surface registry via has_dashboard_surface. A created-but-unpublished
        slot fails those gates until some unrelated slot change republishes,
        so the first cron run's sub-agents stayed invisible and their results
        were never injected."""
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key
        from kiro_crew.session_surface import (
            has_dashboard_surface,
            set_dashboard_surfaced,
        )

        set_dashboard_surfaced(())
        try:
            state = _make_state()
            job = _make_job(job_id="188f71e5")
            _inject(state, job, "result")
            assert has_dashboard_surface("cron:188f71e5") is True
            assert dashboard_slot_key("cron:188f71e5") == "cron-188f71e5"
        finally:
            set_dashboard_surfaced(())


class TestPersistsResultToConversationLog:
    """the result must be written to the canonical ConversationLog
    under the linked key cron:{id} so a dashboard follow-up turn
    (chat_runner.build_session_replay) has it as context."""

    def test_appends_result_to_conversation_log_under_linked_key(self):
        state = _make_state()
        job = _make_job(job_id="job1", name="my-cron")
        _inject(state, job, "the result")
        # Persistence now goes through the atomic append_if_absent (the dup
        # check runs UNDER the session lock, not as a separate unlocked probe).
        # Two rows per run: the prompt, then the result.
        assert state.conversation_log.append_if_absent.call_count == 2
        prompt_args, _ = state.conversation_log.append_if_absent.call_args_list[0]
        assert prompt_args[0] == "cron:job1"
        assert prompt_args[1] == "user"
        assert prompt_args[2].startswith("# Cron Run: my-cron")
        assert "do the thing" in prompt_args[2]
        args, kwargs = state.conversation_log.append_if_absent.call_args
        assert args[0] == "cron:job1"
        assert args[1] == "assistant"
        assert "the result" in args[2]
        assert args[2].startswith("# Cron Job Result: my-cron")
        # The old unlocked append() persist path is gone (dup check is now
        # atomic inside append_if_absent). NB: read_messages is still called
        # once to hydrate the fresh slot — that is not the persistence probe.
        state.conversation_log.append.assert_not_called()

    def test_delegates_log_dedup_to_append_if_absent(self):
        # The log-level duplicate check is now performed ATOMICALLY inside
        # append_if_absent (under the per-session lock), not as a separate
        # unlocked read_messages probe at the inject layer. The inject path must
        # delegate to append_if_absent and no longer do its own log-persist.
        # (append_if_absent's own skip-on-duplicate behavior is covered by
        # test_history_locking_remediation::TestAppendIfAbsent.)
        state = _make_state()
        job = _make_job(job_id="job2", name="my-cron")
        _inject(state, job, "the result")
        assert state.conversation_log.append_if_absent.call_count == 2
        state.conversation_log.append.assert_not_called()

    def test_durable_copy_carries_the_window_rows_id(self):
        # The durable transcript copy must ride with the SAME ``meta.mid`` the
        # window copy was minted; a re-minted or absent id leaves a bounded
        # slot-detail read unable to reconcile the two copies as one message.
        state = _make_state()
        job = _make_job(job_id="job5", name="my-cron")
        _inject(state, job, "the result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        window_mid = slot.messages[-1]["meta"]["mid"]
        kwargs = state.conversation_log.append_if_absent.call_args.kwargs
        assert kwargs["mid"] == window_mid, "the durable copy did not carry the window row's id"

    def test_empty_result_does_not_persist(self):
        state = _make_state()
        job = _make_job(job_id="job3")
        _inject(state, job, "")
        state.conversation_log.append_if_absent.assert_not_called()
        state.conversation_log.append.assert_not_called()

    def test_no_conversation_log_does_not_crash(self):
        state = _make_state()
        state.conversation_log = None
        job = _make_job(job_id="job4")
        # Must not raise when conversation_log is unavailable.
        _inject(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert len(slot.messages) == 2


class TestHydrateSlotFromHistory:
    """Tests for hydrate_slot_from_history (accepts pre-loaded messages)."""

    def test_hydrates_messages_into_slot(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 2
        assert slot.messages[0]["content"] == "hello"
        assert slot.messages[1]["content"] == "world"

    def test_empty_history_produces_no_messages(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        state = _make_state(history_messages=[])
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, [])
        assert len(slot.messages) == 0

    def test_skips_messages_with_empty_content(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "real message"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == "real message"

    def test_assigns_user_role_class(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "user msg"},
            {"role": "assistant", "content": "assistant msg"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert slot.messages[0]["cls"] == "msg msg-u"
        assert slot.messages[1]["cls"] == "msg msg-a"

    def test_preserves_persisted_row_ids(self):
        # A hydrated row must keep the ``meta.mid`` its disk copy carries.
        # Minting fresh ids here leaves the window and the disk holding
        # disjoint id sets for the same rows while the durable injection
        # copies make the region read all-id — the identity walk then marks
        # every hydrated row owed and a bounded read serves the history twice.
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "hello", "meta": {"mid": "m-disk-1"}},
            {"role": "assistant", "content": "world", "meta": {"mid": "m-disk-2"}},
            {"role": "assistant", "content": "pre-id row"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert [m["meta"]["mid"] for m in slot.messages[:2]] == [
            "m-disk-1",
            "m-disk-2",
        ], "hydration re-minted ids the disk rows already carry"


class TestHasSlot:
    """Tests for DashboardState.has_slot method."""

    def test_returns_true_when_slot_exists(self):
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {"cron-abc": MagicMock()}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("cron-abc") is True

    def test_returns_false_when_slot_missing(self):
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("nonexistent") is False
