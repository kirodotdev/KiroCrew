"""Session-domain tests: the batching dispatcher, noise filter, and lifecycle.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Every dispatch goes through the fake session manager from ``meetings_helpers`` —
no test spawns a process or opens a socket.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest import mock

import pytest
from meetings_helpers import (  # noqa: F401
    FakeSessionManager,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess


class TestNoiseFilter:
    """The filter enumerates recognizer fillers instead of judging word LENGTH.

    The old rule — "three or fewer words, all one or two characters" — was measured
    against the noise it was written for and not against real speech, so it also
    dropped meaningful short utterances: `"I do"`, `"we go"`, `"no it is"`. Those
    are the ANSWERS to questions, and losing them removes exactly the decision a
    meeting was held to reach, with nothing in the notes to show a turn was dropped.
    """

    @pytest.mark.parametrize(
        "text",
        ["I", "I I I", "I I I I", "OK I I", "uh", "um so uh", "Hmm.", "ok so"],
    )
    def test_noise_dropped(self, text):
        assert sess.is_noise(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "the deployment is done",
            "OK yes",
            "ship it now",
            # Short, and every one of them is real speech the old rule discarded.
            "I do",
            "we go",
            "no it is",
            "do it",
            "he is up",
            # Two single letters: could be initials or a name, so not ours to drop.
            "a b",
        ],
    )
    def test_content_passes(self, text):
        assert sess.is_noise(text) is False

    def test_a_long_filler_run_is_not_judged(self):
        """Beyond a handful of words a segment is far more likely to be speech."""
        assert sess.is_noise("uh um uh um uh um uh") is False

    def test_empty_is_not_noise_by_itself(self):
        # broadcast() handles the empty case separately; is_noise must not claim
        # an empty string is a noise FRAGMENT (all() over [] is vacuously true).
        assert sess.is_noise("") is False


class TestSlotKey:
    def test_shape(self):
        assert sess.slot_key("note-taker", "standup") == "meetings-note-taker-standup"


class TestAgentQueue:
    @pytest.mark.asyncio
    async def test_flush_joins_and_clears(self):
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", agent="a", sessions=manager)
        queue.queue = ["line one", "line two"]
        await queue.flush()
        assert manager.calls == [("k", "a", "line one\n\nline two")]
        assert queue.queue == []
        assert manager.released == ["k"]

    @pytest.mark.asyncio
    async def test_flush_skips_when_busy(self):
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", sessions=manager)
        queue.queue = ["text"]
        queue.busy = True
        await queue.flush()
        assert manager.calls == []
        assert queue.queue == ["text"]

    @pytest.mark.asyncio
    async def test_flush_noop_when_empty(self):
        manager = FakeSessionManager()
        await sess.AgentQueue(name="n", key="k", sessions=manager).flush()
        assert manager.calls == []

    @pytest.mark.asyncio
    async def test_batch_char_cap(self):
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", sessions=manager)
        queue.queue = ["x" * (k.MAX_BATCH_CHARS + 1000)]
        await queue.flush()
        assert len(manager.calls[0][2]) == k.MAX_BATCH_CHARS

    @pytest.mark.asyncio
    async def test_breaker_trips_after_max_failures(self):
        manager = FakeSessionManager(fail=True)
        queue = sess.AgentQueue(name="n", key="k", sessions=manager, batch_interval=0)
        for _ in range(k.MAX_DISPATCH_FAILURES):
            queue.queue = ["text"]
            await queue.flush()
        assert queue.fail_count == k.MAX_DISPATCH_FAILURES
        assert queue.paused is True
        # A paused queue keeps its content and stops dispatching.
        queue.queue = ["text"]
        await queue.flush()
        assert queue.queue == ["text"]

    @pytest.mark.asyncio
    async def test_backoff_grows_then_caps(self):
        manager = FakeSessionManager(fail=True)
        queue = sess.AgentQueue(name="n", key="k", sessions=manager, batch_interval=0)
        seen: list[float] = []
        for _ in range(k.MAX_DISPATCH_FAILURES):
            queue.queue = ["text"]
            await queue.flush()
            seen.append(queue._backoff)
        assert seen == sorted(seen)
        assert seen[-1] <= k.BACKOFF_CAP_SECS

    def test_resume_resets_breaker(self):
        queue = sess.AgentQueue(name="n", key="k")
        queue._fail_count = k.MAX_DISPATCH_FAILURES
        queue._backoff = 120.0
        queue.resume()
        assert queue.fail_count == 0
        assert queue.paused is False

    @pytest.mark.asyncio
    async def test_flush_now_forces_dispatch(self):
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", agent="a", sessions=manager)
        queue.queue = ["urgent"]
        await queue.flush_now()
        assert manager.calls == [("k", "a", "urgent")]

    @pytest.mark.asyncio
    async def test_no_session_manager_is_a_dispatch_failure(self):
        queue = sess.AgentQueue(name="n", key="k", sessions=None, batch_interval=0)
        queue.queue = ["text"]
        await queue.flush()
        assert queue.fail_count == 1
        assert queue.queue == ["text"]

    def test_enqueue_off_loop_does_not_raise(self):
        # A sync context has no running loop; enqueue must still record the line.
        queue = sess.AgentQueue(name="n", key="k")
        queue.enqueue("line")
        assert queue.queue == ["line"]

    @pytest.mark.asyncio
    async def test_an_oversized_queue_keeps_its_undispatched_tail(self):
        """A batch over the cap must not delete the lines it never sent.

        The regression: the joined batch was truncated to MAX_BATCH_CHARS but the
        WHOLE queue was then cleared, so a paused/backed-up meeting silently lost
        transcript — notes that skip the end of what was said, with no error.
        """
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", agent="a", sessions=manager)
        # Three lines, each just over half the cap: only the first can fit.
        line = "x" * (k.MAX_BATCH_CHARS // 2 + 10)
        queue.queue = [line, line, line]

        await queue.flush()

        assert len(manager.calls) == 1
        assert len(manager.calls[0][2]) <= k.MAX_BATCH_CHARS
        # Exactly the dispatched line is gone; the rest is still queued.
        assert queue.queue == [line, line]

    @pytest.mark.asyncio
    async def test_a_single_oversized_line_is_consumed_not_requeued(self):
        """Keeping an un-sendable line forever would wedge the queue."""
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", agent="a", sessions=manager)
        queue.queue = ["y" * (k.MAX_BATCH_CHARS + 500)]

        await queue.flush()

        assert len(manager.calls[0][2]) == k.MAX_BATCH_CHARS
        assert queue.queue == []

    @pytest.mark.asyncio
    async def test_flush_now_waits_for_an_in_flight_dispatch(self):
        """Ending a meeting must not cancel the turn that is already running.

        The regression: `flush_now` cancelled `_flush_task` unconditionally. If that
        task was inside `flush()` awaiting the agent, the cancel killed the live turn
        — and because `busy` was still set, the follow-up `flush()` no-opped. So
        stopping a meeting mid-dispatch lost the batch AND the finalization notice.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        delivered: list[str] = []

        async def slow_dispatch(sessions, key, text, agent="", *, hooks=None):
            started.set()
            await release.wait()
            delivered.append(text)

        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=FakeSessionManager(), batch_interval=0
        )
        queue.queue = ["spoken before the stop"]
        with mock.patch.object(sess, "dispatch_to_agent", slow_dispatch):
            queue._schedule_flush()
            await asyncio.wait_for(started.wait(), timeout=2)
            assert queue.busy is True  # mid-dispatch, not sleeping

            stopping = asyncio.create_task(queue.flush_now())
            await asyncio.sleep(0)  # let flush_now observe `busy`
            release.set()
            await asyncio.wait_for(stopping, timeout=2)

        assert delivered == ["spoken before the stop"]
        assert queue.queue == []

    @pytest.mark.asyncio
    async def test_flush_now_drains_every_queued_batch(self):
        """An over-cap queue needs several batches, and stop is the last chance.

        The regression: `flush_now` dispatched ONE batch and returned, so whatever
        still needed a second batch was discarded by teardown — the tail loss the
        `_take_batch` fix was supposed to have closed.
        """
        manager = FakeSessionManager()
        line = "x" * (k.MAX_BATCH_CHARS // 2 + 10)  # two lines cannot share a batch
        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=manager, batch_interval=0
        )
        queue.queue = [line, line, line]

        await asyncio.wait_for(queue.flush_now(), timeout=5)

        assert len(manager.calls) == 3
        assert queue.queue == []

    @pytest.mark.asyncio
    async def test_a_transient_failure_mid_drain_does_not_end_the_drain(self):
        """A failed dispatch must be RETRIED here, not read as "nothing left".

        `flush()` leaves the queue untouched on failure and still returns True
        (`more_queued = not self.paused`), so the old length-based progress check
        saw `len(queue) == before` and broke out — ending the drain with transcript
        still queued, which teardown then discards. Exactly the loss the drain
        exists to prevent, reached through the guard meant to bound it.
        """
        manager = FakeSessionManager()
        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=manager, batch_interval=0
        )
        queue.queue = ["first line"]

        # Fail the first dispatch, then recover — the transient case.
        original = manager.get_or_create
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("gateway hiccup")
            return await original(*args, **kwargs)

        manager.get_or_create = flaky  # type: ignore[method-assign]
        await asyncio.wait_for(queue.flush_now(), timeout=5)

        assert queue.queue == [], "the retry must drain the queue, not abandon it"
        assert len(manager.calls) == 1
        assert queue.fail_count == 0, "a successful retry resets the breaker"

    @pytest.mark.asyncio
    async def test_a_permanently_failing_dispatch_still_terminates(self):
        """The other half: retrying must not spin.

        Two independent bounds apply — `_MAX_DRAIN_BATCHES` caps the iterations and
        the circuit breaker pauses the queue after `MAX_DISPATCH_FAILURES`, after
        which `flush()` returns False. Without the breaker this test would hang,
        which is why it carries a timeout.
        """
        manager = FakeSessionManager(fail=True)
        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=manager, batch_interval=0
        )
        queue.queue = ["never lands"]

        await asyncio.wait_for(queue.flush_now(), timeout=5)

        assert queue.fail_count == k.MAX_DISPATCH_FAILURES
        assert queue.paused is True
        # The transcript is still queued and reported, not silently dropped.
        assert queue.queue == ["never lands"]

    @pytest.mark.asyncio
    async def test_the_batching_timer_chains_until_the_queue_is_empty(self):
        """The timer path needs the same drain, not just `flush_now`.

        `flush()` cannot reschedule itself: it runs as the body of `_flush_task`, so
        `_schedule_flush` sees a live task and takes its early return. The loop
        therefore lives in `_delayed_flush`.
        """
        manager = FakeSessionManager()
        line = "x" * (k.MAX_BATCH_CHARS // 2 + 10)
        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=manager, batch_interval=0
        )
        queue.queue = [line, line, line]

        queue._schedule_flush()
        for _ in range(50):
            if not queue.queue:
                break
            await asyncio.sleep(0.01)

        assert queue.queue == []
        assert len(manager.calls) == 3

    @pytest.mark.asyncio
    async def test_a_failing_dispatch_does_not_spin_the_drain(self):
        """A drain must terminate even when nothing can be delivered."""
        async def boom(sessions, key, text, agent="", *, hooks=None):
            raise RuntimeError("dispatch down")

        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=FakeSessionManager(), batch_interval=0
        )
        queue.queue = ["a", "b"]
        with mock.patch.object(sess, "dispatch_to_agent", boom):
            await asyncio.wait_for(queue.flush_now(), timeout=5)
        # Nothing delivered, nothing lost, and the breaker counted the failure.
        assert queue.queue == ["a", "b"]
        assert queue.fail_count >= 1

    @pytest.mark.asyncio
    async def test_flush_now_still_cancels_a_sleeping_timer(self):
        """The fast path must survive the fix: a pending TIMER is skipped, not awaited.

        Without this, `flush_now` would wait out the batch interval and "flush now"
        would mean "flush in 30 seconds".
        """
        manager = FakeSessionManager()
        queue = sess.AgentQueue(
            name="n", key="k", agent="a", sessions=manager, batch_interval=30
        )
        queue.enqueue("line")
        assert queue.busy is False  # sleeping on the timer
        await asyncio.wait_for(queue.flush_now(), timeout=2)
        assert manager.calls == [("k", "a", "line")]

    @pytest.mark.asyncio
    async def test_a_batch_under_the_cap_still_takes_everything(self):
        """The common path is unchanged: normal traffic flushes in one batch."""
        manager = FakeSessionManager()
        queue = sess.AgentQueue(name="n", key="k", agent="a", sessions=manager)
        queue.queue = ["one", "two", "three"]

        await queue.flush()

        assert manager.calls == [("k", "a", "one\n\ntwo\n\nthree")]
        assert queue.queue == []


class TestMeetingSession:
    def _session(self, root: Path, **kwargs) -> sess.MeetingSession:
        return sess.MeetingSession(
            meeting_id="m", config=store.read_config(root), **kwargs
        )

    def test_creates_a_queue_per_enabled_agent_plus_extractor(self, root: Path):
        session = self._session(root)
        assert set(session.agents) == {"note-taker", "sketch-artist", k.TASK_EXTRACTOR_ID}

    def test_agents_enabled_filter(self, root: Path):
        session = self._session(root, agents_enabled=["note-taker"])
        assert set(session.agents) == {"note-taker", k.TASK_EXTRACTOR_ID}

    def test_task_extractor_always_present(self, root: Path):
        session = self._session(root, agents_enabled=[])
        assert set(session.agents) == {k.TASK_EXTRACTOR_ID}

    def test_broadcast_enqueues_to_all_unmuted(self, root: Path):
        session = self._session(root)
        assert session.broadcast("the deployment is done") == 3
        assert session.agents["note-taker"].queue == ["the deployment is done"]

    def test_broadcast_skips_muted(self, root: Path):
        session = self._session(root)
        session.muted_agents.add("note-taker")
        assert session.broadcast("the deployment is done") == 2
        assert session.agents["note-taker"].queue == []
        assert session.agents["sketch-artist"].queue

    def test_broadcast_drops_noise(self, root: Path):
        session = self._session(root)
        assert session.broadcast("I I") == 0
        assert session.agents["note-taker"].queue == []

    def test_broadcast_drops_empty(self, root: Path):
        session = self._session(root)
        assert session.broadcast("   ") == 0

    def test_broadcast_applies_dictionary(self, root: Path):
        sess.shared_dictionary().load_terms(
            [{"correct": "DynamoDB", "aliases": ["dynamo db"]}]
        )
        session = self._session(root)
        session.broadcast("we switched to dynamo db")
        assert session.agents["note-taker"].queue == ["we switched to DynamoDB"]

    def test_broadcast_truncates_overlong_line(self, root: Path):
        session = self._session(root)
        session.broadcast("x" * (k.MAX_TRANSCRIPT_CHARS + 500))
        assert len(session.agents["note-taker"].queue[0]) == k.MAX_TRANSCRIPT_CHARS

    def test_expiry(self, root: Path):
        session = self._session(root)
        assert session.expired is False
        session.started_at = time.time() - (k.MAX_SESSION_DURATION + 1)
        assert session.expired is True

    def test_add_agent_is_idempotent_and_unmutes(self, root: Path):
        session = self._session(root, agents_enabled=["note-taker"])
        session.muted_agents.add("sketch-artist")
        first = session.add_agent("sketch-artist", "meetings/meetings-sketch-artist")
        second = session.add_agent("sketch-artist", "meetings/meetings-sketch-artist")
        assert first is second
        assert "sketch-artist" not in session.muted_agents

    def test_status_shape(self, root: Path):
        session = self._session(root)
        status = session.status()
        assert status["active_meeting"] == "m"
        assert status["agents_paused"] is False
        assert set(status["agents"]) == set(session.agents)

    def test_agents_paused_reflects_a_tripped_queue(self, root: Path):
        session = self._session(root)
        session.agents["note-taker"]._fail_count = k.MAX_DISPATCH_FAILURES
        assert session.agents_paused is True
        assert session.resume_all() == ["note-taker"]
        assert session.agents_paused is False

    @pytest.mark.asyncio
    async def test_flush_all_dispatches_every_queue(self, root: Path):
        manager = FakeSessionManager()
        session = sess.MeetingSession(
            meeting_id="m", sessions=manager, config=store.read_config(root)
        )
        session.broadcast("the deployment is done")
        await session.flush_all()
        assert len(manager.calls) == 3


class TestLifecycleMeta:
    def test_start_activates_and_seeds_outputs(self, root: Path):
        meta = sess.start_meeting_meta("m", None, "Sprint Standup", root)
        assert meta["status"] == k.STATUS_ACTIVE
        assert "started_at" in meta
        assert set(meta["outputs"]) == {"note-taker", "sketch-artist"}
        assert store.agent_output_path("m", "note-taker.md", root).is_file()

    def test_start_with_filter_narrows_outputs(self, root: Path):
        meta = sess.start_meeting_meta("m", ["note-taker"], "T", root)
        assert set(meta["outputs"]) == {"note-taker"}
        assert not store.agent_output_path("m", "sketch-artist.html", root).exists()

    def test_start_preserves_existing_title_when_none_given(self, root: Path):
        store.write_meeting_meta("m", store.new_meeting_meta("m", "Original"), root)
        assert sess.start_meeting_meta("m", None, "", root)["title"] == "Original"

    def test_end_marks_ended(self, root: Path):
        sess.start_meeting_meta("m", None, "T", root)
        meta = sess.end_meeting_meta("m", root)
        assert meta is not None
        assert meta["status"] == k.STATUS_ENDED
        assert "ended_at" in meta

    def test_end_on_unknown_meeting_returns_none(self, root: Path):
        assert sess.end_meeting_meta("absent", root) is None


class TestPrompts:
    def test_context_includes_attendees_and_attachments(self):
        context = sess.build_meeting_context(
            {
                "title": "Design Review",
                "description": "the new seam",
                "attendees": ["Alice", "Bob"],
                "attachments": [
                    {"type": "url", "url": "https://example.test/doc", "label": "Doc"},
                    {"type": "file", "path": "/tmp/spec.md", "label": "Spec"},
                ],
            }
        )
        assert "Design Review" in context
        assert "Alice, Bob" in context
        assert "https://example.test/doc" in context
        assert "/tmp/spec.md" in context

    def test_context_redacts_credentials(self):
        context = sess.build_meeting_context({"title": "AKIAIOSFODNN7EXAMPLE"})
        assert "AKIAIOSFODNN7EXAMPLE" not in context

    def test_context_skips_malformed_attachment(self):
        context = sess.build_meeting_context({"title": "T", "attachments": ["oops", None]})
        assert "oops" not in context

    def test_init_message_carries_output_path(self):
        message = sess.build_init_message(
            {"id": "note-taker", "name": "Note Taker"},
            {"title": "Standup"},
            "/data/meetings/m/note-taker.md",
            "cross ref block",
        )
        assert message.startswith("OUTPUT_FILE: /data/meetings/m/note-taker.md")
        assert "cross ref block" in message
        assert "Standup" in message

    def test_init_message_uses_custom_prompt(self):
        message = sess.build_init_message(
            {"id": "x", "name": "X", "prompt": "BESPOKE INSTRUCTIONS"}, {}, "/p", ""
        )
        assert "BESPOKE INSTRUCTIONS" in message

    def test_cross_reference_lists_every_output_and_tasks(self):
        block = sess.build_cross_reference(
            "/d/m",
            [
                {"id": "note-taker", "name": "Note Taker", "widget_type": "markdown"},
                {"id": "sketch-artist", "name": "Sketch", "widget_type": "html"},
                {"id": "chatty", "name": "Chatty", "widget_type": "chat"},
            ],
        )
        assert "/d/m/note-taker.md" in block
        assert "/d/m/sketch-artist.html" in block
        assert "chatty" not in block  # chat agents have no output file
        assert f"/d/m/{k.TASKS_FILE}" in block


class TestInitAgents:
    @pytest.mark.asyncio
    async def test_dispatches_one_prompt_per_agent(self, root: Path):
        manager = FakeSessionManager()
        meta = sess.start_meeting_meta("m", None, "Standup", root)
        session = sess.MeetingSession(
            meeting_id="m", sessions=manager, config=store.read_config(root)
        )
        await sess.init_agents(session, meta, root)
        keys = {key for key, _agent, _msg in manager.calls}
        assert keys == {
            "meetings-note-taker-m",
            "meetings-sketch-artist-m",
            f"meetings-{k.TASK_EXTRACTOR_ID}-m",
        }

    @pytest.mark.asyncio
    async def test_one_failing_agent_does_not_abort_the_rest(self, root: Path):
        manager = FakeSessionManager(fail=True)
        meta = sess.start_meeting_meta("m", None, "Standup", root)
        session = sess.MeetingSession(
            meeting_id="m", sessions=manager, config=store.read_config(root)
        )
        # Must not raise — a broken agent is logged and skipped.
        await sess.init_agents(session, meta, root)
        assert manager.calls == []

    @pytest.mark.asyncio
    async def test_broadcast_system_flushes_immediately(self, root: Path):
        manager = FakeSessionManager()
        session = sess.MeetingSession(
            meeting_id="m", sessions=manager, config=store.read_config(root)
        )
        await sess.broadcast_system(session, k.SYSTEM_MEETING_ENDED)
        assert len(manager.calls) == 3
        assert all(k.SYSTEM_MEETING_ENDED in msg for _k, _a, msg in manager.calls)


class TestConfigHelpers:
    def test_defaults_when_no_filter(self, root: Path):
        config = store.read_config(root)
        assert len(sess.get_enabled_agents(config)) == 2

    def test_explicit_filter(self, root: Path):
        config = store.read_config(root)
        enabled = sess.get_enabled_agents(config, ["sketch-artist"])
        assert [a["id"] for a in enabled] == ["sketch-artist"]

    def test_empty_filter_means_none(self, root: Path):
        assert sess.get_enabled_agents(store.read_config(root), []) == []

    def test_enabled_by_default_false_excluded(self):
        config = {"meeting_agents": [{"id": "a", "enabled_by_default": False}]}
        assert sess.get_enabled_agents(config) == []


class TestDispatchThreadsGovernanceIdentity:
    """The agent dispatch must tell the PreToolUse gate WHICH app it is.

    The gate resolves `ceiling ∩ profile`, and it can only look up a profile whose
    name it was given. With the identity arguments left at their empty defaults it
    applied the enterprise ceiling alone — so an operator profile narrowing this app
    (denying `filesystem.write`, say) was silently not enforced for tools this
    dispatch approved. The ceiling still held, which is why this failed quietly: the
    app-scoped half of the two-level model simply did not participate.
    """

    @pytest.mark.asyncio
    async def test_app_session_key_and_agent_reach_stream_and_collect(self):
        captured: dict[str, object] = {}

        async def fake_stream(provider, text, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return ""

        provider = mock.MagicMock()
        sessions = mock.MagicMock()
        sessions.get_or_create = mock.AsyncMock(return_value=(provider, False, False))
        sessions.release = mock.MagicMock()

        with mock.patch.object(sess, "stream_and_collect", fake_stream):
            await sess.dispatch_to_agent(
                sessions, "meetings:m1:note-taker", "a line", "meetings-note-taker",
                hooks=mock.MagicMock(),
            )

        # `app` is the load-bearing one — without it there is no profile to resolve.
        assert captured.get("app") == k.APP_NAME
        assert captured.get("session_key") == "meetings:m1:note-taker"
        assert captured.get("agent") == "meetings-note-taker"
        # And the gate is still the authority for this path.
        assert captured.get("approval_policy") is sess.ToolApprovalPolicy.HOOK_BASED

    def test_stream_and_collect_forwards_the_app_to_the_gate(self):
        """AST: the shared helper must pass `app` on to `hooks.on_tool_call`.

        Threading it into `stream_and_collect` accomplishes nothing if the helper
        drops it before the gate — which is exactly the shape the original bug had one
        level down.
        """
        import ast
        import inspect

        from kiro_crew import llm_helpers

        tree = ast.parse(inspect.getsource(llm_helpers))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "on_tool_call"
        ]
        assert calls, "llm_helpers no longer calls hooks.on_tool_call"
        for call in calls:
            names = {kw.arg for kw in call.keywords}
            assert "app" in names, "on_tool_call is invoked without `app`, so no profile resolves"
            assert "session_key" in names and "agent" in names
