"""Unit tests for A2A remote-subagent support (Phase 2).

Covers the four surfaces the POC added to the public core:

* ``A2AProvider.stream`` SSE -> provider-event mapping (mocked stream, no net).
* ``a2a_agents`` registry parse + collision refusal at config load.
* ``_validate_agent`` unioning A2A names into the known set.
* ``_should_use_session_sharing`` excluding A2A registry members.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    PROVIDER_LABEL_A2A,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import A2aAgentConfig, A2aAuthConfig
from kiro_crew.providers.a2a import A2AProvider, A2AStreamError
from kiro_crew.providers.base import LLMEvent
from kiro_crew.subagent_persistence import create_agent_folder

# Two tests drive SubagentManager.spawn; unpinned, a memory-pressured runner
# refuses the spawn and the test fails on the following line.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# ── Test doubles for a mocked aiohttp SSE stream ─────────────────────────────


class _FakeContent:
    """Async byte-line iterator over pre-baked SSE lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        self._it = iter(self._lines)
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.content = _FakeContent(lines)

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePostSession:
    """A fake aiohttp ClientSession whose .post() returns baked SSE lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.posted: list[dict] = []

    def post(
        self, url, *, json=None, headers=None, timeout=None, allow_redirects=True
    ):  # noqa: A002
        self.posted.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._lines)


def _sse(obj_json: str) -> bytes:
    return ("data: " + obj_json).encode("utf-8")


# The exact wire shape observed from the live shim (A2A v1.0).
_STREAM_LINES = [
    _sse(
        '{"result": {"task": {"id": "t-1", "contextId": "ctx-1",'
        ' "status": {"state": "TASK_STATE_SUBMITTED"}}}, "id": "1", "jsonrpc": "2.0"}'
    ),
    b"",
    _sse(
        '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
        ' "status": {"state": "TASK_STATE_WORKING",'
        ' "message": {"role": "ROLE_AGENT", "parts": [{"text": "Hello "}]}}}},'
        ' "id": "1", "jsonrpc": "2.0"}'
    ),
    b"",
    _sse(
        '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
        ' "status": {"state": "TASK_STATE_WORKING",'
        ' "message": {"role": "ROLE_AGENT", "parts": [{"text": "world"}]}}}},'
        ' "id": "1", "jsonrpc": "2.0"}'
    ),
    b"",
    _sse(
        '{"result": {"artifactUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
        ' "artifact": {"artifactId": "a-1", "name": "response",'
        ' "parts": [{"text": "Hello world"}]}, "lastChunk": true}},'
        ' "id": "1", "jsonrpc": "2.0"}'
    ),
    b"",
    _sse(
        '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
        ' "status": {"state": "TASK_STATE_COMPLETED"}}}, "id": "1", "jsonrpc": "2.0"}'
    ),
    b"",
]


async def _drain(agen):
    out = []
    async for e in agen:
        out.append(e)
    return out


class TestA2AProviderStream:
    @pytest.mark.asyncio
    async def test_truncated_stream_raises_not_completes(self):
        """A stream ending WITHOUT a terminal task state must raise, never
        yield EVENT_COMPLETE — run.py reads the completion event only for
        billing, so an error reported there becomes a silent empty success
        (live incident: shim killed mid-turn -> ✅ '_No response._')."""
        from kiro_crew.providers.a2a import A2AStreamError

        # Task started + one WORKING delta, then the stream just ends.
        truncated = [
            ln
            for ln in _STREAM_LINES
            if b"TASK_STATE_COMPLETED" not in (ln if isinstance(ln, bytes) else ln.encode())
            and b"artifactUpdate" not in (ln if isinstance(ln, bytes) else ln.encode())
        ]
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(truncated)

        with pytest.raises(A2AStreamError) as ei:
            await _drain(p.stream("hi"))
        assert "terminal" in str(ei.value)
        # Structural non-transient verdict: the retry ladder must not re-send
        # the task to a server that died mid-turn.
        assert ei.value.transient is False

    @pytest.mark.asyncio
    async def test_connection_error_mid_stream_raises_non_transient(self):
        from kiro_crew.providers.a2a import A2AStreamError

        class _ExplodingSession(_FakePostSession):
            def post(self, *a, **k):
                raise ConnectionResetError("connection lost")

        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _ExplodingSession([])

        with pytest.raises(A2AStreamError) as ei:
            await _drain(p.stream("hi"))
        assert ei.value.transient is False

    @pytest.mark.asyncio
    async def test_sse_maps_to_text_chunks_and_complete(self):
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True  # skip start()/card fetch
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(_STREAM_LINES)

        events = await _drain(p.stream("hi"))

        chunks = [e for e in events if e.kind == EVENT_TEXT_CHUNK]
        completes = [e for e in events if e.kind == EVENT_COMPLETE]
        assert "".join(c.text for c in chunks) == "Hello world"
        assert len(completes) == 1
        # Artifact text is the authoritative final answer.
        assert completes[0].text == "Hello world"
        assert completes[0].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_context_id_adopted_from_first_response(self):
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(_STREAM_LINES)
        assert p.context_id is None
        await _drain(p.stream("hi"))
        assert p.context_id == "ctx-1"
        assert p.current_task_id == "t-1"
        # session_id reuses the contextId for persistence.
        assert p.session_id == "ctx-1"

    @pytest.mark.asyncio
    async def test_failed_terminal_surfaces_error(self):
        lines = [
            _sse(
                '{"result": {"task": {"id": "t-9", "contextId": "ctx-9",'
                ' "status": {"state": "TASK_STATE_SUBMITTED"}}}, "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
            _sse(
                '{"result": {"statusUpdate": {"taskId": "t-9",'
                ' "status": {"state": "TASK_STATE_FAILED",'
                ' "message": {"role": "ROLE_AGENT", "parts": [{"text": "boom"}]}}}},'
                ' "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
        ]
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)

        # FAILED is a failed run, never a completion: the runner would otherwise
        # record_success. The remote's reason rides in the exception, and the
        # status-message delta was already streamed before the raise.
        seen: list[str] = []
        with pytest.raises(A2AStreamError) as excinfo:
            async for ev in p.stream("hi"):
                if ev.kind == EVENT_TEXT_CHUNK:
                    seen.append(ev.text or "")
        assert "task_state_failed" in str(excinfo.value)
        assert "boom" in str(excinfo.value)
        assert "".join(seen) == "boom"
        assert excinfo.value.transient is False

    @pytest.mark.asyncio
    async def test_reference_task_ids_sent_on_second_turn(self):
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        sess = _FakePostSession(_STREAM_LINES)
        p._session = sess
        await _drain(p.stream("first"))
        # Second turn reuses contextId and references the prior task.
        p._session = _FakePostSession(_STREAM_LINES)
        await _drain(p.stream("second"))
        msg = p._session.posted[0]["json"]["params"]["message"]
        assert msg["contextId"] == "ctx-1"
        assert msg["referenceTaskIds"] == ["t-1"]

    def test_provider_label_is_a2a(self):
        p = A2AProvider(name="x", agent_card_url="http://h/.well-known/agent-card.json")
        assert p.provider_label == PROVIDER_LABEL_A2A == "a2a"
        assert p.context_usage_pct() == 0.0


class TestA2ARegistryConfig:
    def test_accessors(self):
        cfg = KiroCrewConfig()
        cfg.a2a_agents = [A2aAgentConfig(name="remote", agent_card_url="http://h/card.json")]
        assert cfg.a2a_agent_names() == frozenset({"remote"})
        assert cfg.a2a_agent_by_name("remote").agent_card_url == "http://h/card.json"
        assert cfg.a2a_agent_by_name("missing") is None

    def test_to_dict_round_trips_a2a_agents(self):
        cfg = KiroCrewConfig()
        cfg.a2a_agents = [
            A2aAgentConfig(
                name="remote",
                agent_card_url="http://h/card.json",
                auth=A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_TOKEN"),
            )
        ]
        d = cfg.to_dict()
        assert d["a2a_agents"] == [
            {
                "name": "remote",
                "agent_card_url": "http://h/card.json",
                "auth": {"scheme": "bearer", "token_env": "KIROCREW_A2A_REMOTE_TOKEN"},
            }
        ]
        # And back: the loader reads the object shape (and the legacy bare string).
        from kiro_crew.config.sections import _a2a_auth_from

        assert _a2a_auth_from(d["a2a_agents"][0]["auth"]) == A2aAuthConfig(
            "bearer", "KIROCREW_A2A_REMOTE_TOKEN"
        )
        assert _a2a_auth_from("bearer") == A2aAuthConfig()  # only the object shape is a spelling
        assert _a2a_auth_from(None) == A2aAuthConfig()


class TestValidateAgentUnion:
    def test_a2a_name_accepted_by_validate_agent(self):
        from kiro_crew import subagent

        with patch.object(subagent, "list_agents", return_value=[]):
            with patch.object(subagent, "_a2a_agent_names", return_value=frozenset({"local-kiro"})):
                name, err, code = subagent._validate_agent("local-kiro")
        assert name == "local-kiro"
        assert err == ""
        assert code == ""

    def test_unknown_name_still_refused(self):
        from kiro_crew import subagent

        with patch.object(subagent, "list_agents", return_value=[]):
            with patch.object(subagent, "_a2a_agent_names", return_value=frozenset()):
                name, err, code = subagent._validate_agent("ghost")
        assert name == ""
        assert err != ""

    def test_collision_is_refused_from_the_roster_in_hand(self):
        # A name that is BOTH a local agent and (per the admitted decision) a
        # registry entry is refused with the typed code -- decided from the
        # roster and the decision alone, with no config re-read that could fail
        # or see a different registry and let the name through.
        from types import SimpleNamespace

        from kiro_crew import subagent

        with patch.object(subagent, "list_agents", return_value=[SimpleNamespace(name="dup")]):
            with patch.object(subagent, "_a2a_agent_names", return_value=frozenset({"dup"})):
                name, err, code = subagent._validate_agent("dup")
        assert (name, code) == ("", subagent.AGENT_NAME_COLLISION_CODE) and "collides" in err
        with patch.object(subagent, "list_agents", return_value=[SimpleNamespace(name="dup")]):
            name, err, code = subagent._validate_agent("dup", remote=True)
        assert code == subagent.AGENT_NAME_COLLISION_CODE

    def test_admission_decision_overrides_registry_read(self):
        # The collision refusal and the roster union follow the decision the
        # caller (admission) passes in, not a fresh registry read: with
        # remote=False a registry name that is not a local agent is unknown,
        # and with remote=True a name absent from the registry is accepted.
        from kiro_crew import subagent

        with patch.object(subagent, "list_agents", return_value=[]):
            with patch.object(subagent, "_a2a_agent_names", return_value=frozenset({"local-kiro"})):
                _, err, code = subagent._validate_agent("local-kiro", remote=False)
                assert code == subagent.AGENT_NOT_FOUND_CODE and err
                name, err, _ = subagent._validate_agent("ghost", remote=True)
                assert (name, err) == ("ghost", "")


class TestAdmittedRoutingIsSingleResolution:
    """Local-vs-remote is classified ONCE at admission and travels on the record.

    Governance vet, collision refusal and the run-path branch all consume that
    one resolution. Three independent ``KiroCrewConfig.load()`` reads were a
    time-of-check/time-of-use hole: an ``a2a_agents`` entry written while a spawn
    waited for approval re-routed a locally-vetted spawn off-box.
    """

    def test_admission_stashes_the_entry_and_threads_the_decision(self):
        import inspect

        from kiro_crew.subagent_manager import admission

        src = inspect.getsource(admission.SpawnAdmissionCoordinator.spawn_impl)
        assert "a2a_entry = _a2a_agent_entry(agent)" in src
        assert "remote=is_remote, remote_origin=remote_origin" in src
        assert "remote_origin = card_origin(a2a_entry.agent_card_url)" in src
        assert "_validate_agent(agent, effective_cwd, remote=is_remote)" in src
        assert 'setattr(info, "_a2a_entry", a2a_entry)' in src

    def test_run_path_branches_on_the_stash_not_the_registry(self):
        import inspect

        from kiro_crew.subagent_manager import run

        src = inspect.getsource(run.RunEventCoordinator._run_inner_impl)
        assert 'getattr(info, "_a2a_entry", None)' in src
        assert "_a2a_agent_entry(" not in src

    def test_vetted_entry_record_round_trips_and_fails_closed(self):
        """The classification travels between the gated pass and a re-entry as
        plain data (queue rows are JSON). It round-trips; anything malformed
        reads as LOCAL, never as a remote with a guessed destination."""
        from kiro_crew import subagent

        entry = A2aAgentConfig(
            name="remote-demo",
            agent_card_url="https://h/.well-known/agent-card.json",
            auth=A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_DEMO"),
        )
        rec = subagent._a2a_entry_record(entry)
        assert rec == {
            "name": "remote-demo",
            "agent_card_url": "https://h/.well-known/agent-card.json",
            "auth": {"scheme": "bearer", "token_env": "KIROCREW_A2A_DEMO"},
        }
        back = subagent._a2a_entry_from_record(rec)
        assert back is not None and back == entry
        assert subagent._a2a_entry_record(None) is None
        for garbage in (None, "", "remote-demo", 7, [], {}, {"name": "x"}, {"agent_card_url": ""}):
            assert subagent._a2a_entry_from_record(garbage) is None, garbage

    @pytest.mark.asyncio
    async def test_reentry_consumes_the_vetted_classification_not_the_registry(self):
        """The governance vet runs only on the gated pass. A re-entry that
        resolved the registry afresh would stash whatever config says NOW, past
        the vet: an ``a2a_agents`` entry written while the row waited would
        route an admitted LOCAL spawn off-box. The re-entry must therefore not
        read the registry at all -- it consumes what the gated pass vetted."""
        import inspect
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from kiro_crew import subagent
        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_manager import admission

        src = inspect.getsource(admission.SpawnAdmissionCoordinator.spawn_impl)
        assert "a2a_entry = _a2a_entry_from_record(_a2a_vetted)" in src
        assert '"_a2a_vetted": _a2a_entry_record(a2a_entry)' in src

        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=AssertionError("no run in this test"))
        sessions.get_approval_policy = MagicMock(return_value="")
        # The gate derives a parentless template from the parent's selection.
        sessions.get_agent_selection = MagicMock(return_value=("template", "kirocrew"))
        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=3)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock()),
            patch("kiro_crew.subagent.update_state"),
            patch.object(subagent, "_a2a_agent_entry", return_value=None) as registry,
            patch.object(subagent, "list_agents", return_value=[SimpleNamespace(name="worker")]),
            patch.object(manager, "_run", AsyncMock()),  # admission is the subject, not the run
        ):
            manager._running_count = manager.max_concurrent
            queued = manager.spawn(
                "queued local task", agent="worker", parent_session_key="dashboard:d"
            )
            assert queued is not None and queued.queued and not queued.error, (
                queued.error,
                queued.error_code,
            )
            assert registry.call_count == 1  # the gated pass classified: LOCAL
            params = manager._queue.pop(0)
            assert params["_a2a_vetted"] is None
            # The registry changes while the row waits. The re-entry must not see it.
            # A drained row re-run by the pump enters with the slot already CLAIMED
            # (``_claimed``): the policy gates, the governance vet among them, are
            # not re-run on that entry -- so neither may the classification be.
            registry.reset_mock()
            registry.side_effect = AssertionError("re-entry read the agent-writable registry")
            manager._running_count = 0
            child = manager.spawn(
                **params, _from_queue=True, _claimed=(0, True, ""), _child_registration=False
            )
            assert child is not None and not child.error, child.error
            if child.id in manager._tasks:
                await manager._tasks[child.id]
        assert getattr(child, "_a2a_entry", None) is None
        registry.assert_not_called()


class TestSessionSharingExclusion:
    def test_a2a_agent_excluded_from_session_sharing(self):
        from kiro_crew import subagent

        # A minimal info-like object with the fields _should_use_session_sharing reads.
        info = MagicMock()
        info.model = ""
        info.allowed_tools = None
        info.bare = False
        info.parent_session_key = "parent"
        info.memory_store = None
        info.agent = "local-kiro"

        fake_cfg = MagicMock()
        fake_cfg.agent.session_sharing = True

        # Build a manager whose only wired bits are what the impl touches.
        mgr = MagicMock()
        mgr._sessions.is_session_sharing_eligible.return_value = True

        # The impl is bound to the subagent module namespace; call via the class.
        with patch.object(subagent, "KiroCrewConfig") as cfg_cls:
            cfg_cls.load.return_value = fake_cfg
            with patch.object(subagent, "_is_a2a_agent", return_value=True):
                # Reach the impl through a stand-in coordinator.
                rc = _make_run_coordinator(mgr)
                assert rc._should_use_session_sharing_impl(info) is False

    def test_local_agent_uses_session_sharing(self):
        from kiro_crew import subagent

        info = MagicMock()
        # A member spawn is excluded from sharing upstream (its capability is
        # prepared at launch); a MagicMock's member_id is a truthy mock, so pin
        # this as the ordinary template spawn the test is about.
        info.execution_context.member_id = None
        info.model = ""
        info.allowed_tools = None
        info.bare = False
        info.parent_session_key = "parent"
        info.memory_store = None
        info.agent = "local-agent"

        fake_cfg = MagicMock()
        fake_cfg.agent.session_sharing = True

        mgr = MagicMock()
        mgr._sessions.is_session_sharing_eligible.return_value = True

        with patch.object(subagent, "KiroCrewConfig") as cfg_cls:
            cfg_cls.load.return_value = fake_cfg
            with patch.object(subagent, "_is_a2a_agent", return_value=False):
                rc = _make_run_coordinator(mgr)
                assert rc._should_use_session_sharing_impl(info) is True


def _make_run_coordinator(manager):
    """Construct a RunEventCoordinator bound to *manager*."""
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    return RunEventCoordinator(manager)


class TestArtifactsAndCancelSpelling:
    """Two contract points a real A2A server exposed that the shim never did."""

    @pytest.mark.asyncio
    async def test_artifact_only_stream_is_streamed_as_chunks(self):
        """A server that puts its result in Artifacts (the protocol's rule) with
        no status-message text must still produce EVENT_TEXT_CHUNKs: run.py builds
        the run's result from chunks alone, and EVENT_COMPLETE.text is billing-only.
        """
        lines = [
            _sse(
                '{"result": {"task": {"id": "t-1", "contextId": "ctx-1",'
                ' "status": {"state": "TASK_STATE_WORKING"}}}, "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
            _sse(
                '{"result": {"artifactUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
                ' "append": true, "artifact": {"artifactId": "a", "parts": [{"text": "one "}]}}},'
                ' "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
            _sse(
                '{"result": {"artifactUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
                ' "append": true, "artifact": {"artifactId": "a", "parts": [{"text": "two"}]}}},'
                ' "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
            _sse(
                '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
                ' "status": {"state": "TASK_STATE_COMPLETED"}}}, "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
        ]
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        events = await _drain(p.stream("go"))
        chunks = [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
        assert chunks == ["one ", "two"]
        assert events[-1].kind == EVENT_COMPLETE and events[-1].text == "one two"

    @pytest.mark.asyncio
    async def test_recap_artifact_after_streamed_deltas_is_not_doubled(self):
        """The reference-SDK pattern: progress as status-message deltas, then ONE
        artifact repeating the full text. Shown once, recorded once."""
        p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(_STREAM_LINES)
        events = await _drain(p.stream("go"))
        assert "".join(e.text for e in events if e.kind == EVENT_TEXT_CHUNK) == "Hello world"
        assert events[-1].text == "Hello world"

    @staticmethod
    def _art(aid: str, text: str, *, append: bool | None) -> bytes:
        flag = "" if append is None else f' "append": {"true" if append else "false"},'
        return _sse(
            '{"result": {"artifactUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            + flag
            + ' "artifact": {"artifactId": "'
            + aid
            + '", "parts": [{"text": "'
            + text
            + '"}]}}}, "id": "1", "jsonrpc": "2.0"}'
        )

    @pytest.mark.asyncio
    async def test_replacement_artifact_supersedes_the_appended_draft(self):
        """Semantics as the reference SDK's TaskManager applies them: the first
        update for an id (``append`` false) CREATES the artifact and streams live,
        ``append: true`` extends it and streams live, and ``append`` false on an id
        that already has content REPLACES it. The replacement cannot retract what
        streamed, so it is held and delivered once, last; the completion text (the
        record) is the artifact's FINAL content, never draft + final."""
        done = _sse(
            '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            ' "status": {"state": "TASK_STATE_COMPLETED"}}}, "id": "1", "jsonrpc": "2.0"}'
        )
        lines = [
            _STREAM_LINES[0],
            b"",
            self._art("out", "draft one ", append=False),  # create
            b"",
            self._art("out", "draft two", append=True),  # extend
            b"",
            self._art("out", "FINAL", append=False),  # replace
            b"",
            done,
            b"",
        ]
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        events = await _drain(p.stream("go"))
        chunks = [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
        # Transcript: everything the user watched, in order, replacement last.
        assert chunks == ["draft one ", "draft two", "FINAL"]
        # Record: the artifact's final content alone.
        assert events[-1].kind == EVENT_COMPLETE and events[-1].text == "FINAL"

    @pytest.mark.asyncio
    async def test_artifacts_are_tracked_per_id(self):
        # Two artifacts: "a" is created and left alone; "b" is created then
        # replaced. The record is each artifact's final content, in first-seen order.
        done = _sse(
            '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            ' "status": {"state": "TASK_STATE_COMPLETED"}}}, "id": "1", "jsonrpc": "2.0"}'
        )
        lines = [
            _STREAM_LINES[0],
            b"",
            self._art("a", "alpha", append=None),
            b"",
            self._art("b", "beta-draft", append=None),
            b"",
            self._art("b", "beta", append=False),
            b"",
            done,
            b"",
        ]
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        events = await _drain(p.stream("go"))
        assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == [
            "alpha",
            "beta-draft",
            "beta",
        ]
        assert events[-1].text == "alphabeta"

    @pytest.mark.asyncio
    async def test_replacement_is_flushed_before_a_failed_turn_is_reported(self):
        # A held replacement is the remote's last-known result: it reaches the
        # transcript even when the turn then ends FAILED.
        failed = _sse(
            '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            ' "status": {"state": "TASK_STATE_FAILED"}}}, "id": "1", "jsonrpc": "2.0"}'
        )
        lines = [
            _STREAM_LINES[0],
            b"",
            self._art("out", "draft", append=False),
            b"",
            self._art("out", "partial", append=False),
            b"",
            failed,
            b"",
        ]
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        chunks: list[str] = []
        with pytest.raises(A2AStreamError, match="failed"):
            async for ev in p.stream("go"):
                if ev.kind == EVENT_TEXT_CHUNK:
                    chunks.append(ev.text or "")
        assert chunks == ["draft", "partial"]

    @staticmethod
    def _remote_manager(provider):
        """A manager whose remote build step hands back *provider* directly."""
        from unittest.mock import AsyncMock

        from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(
            side_effect=AssertionError("remote runs never touch the ACP path")
        )
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.release_subagent_runtime = AsyncMock()
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        ctx = MagicMock()
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=3)
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        info = SubagentInfo(
            id="rr01",
            task="write it",
            agent="remote-demo",
            parent_session_key="dashboard:default",
            # The run path refuses without a captured execution context (memory
            # owner + mode); a remote run carries one like any other spawn.
            execution_context=ExecutionContext(None, MemoryStoreRef("default"), "template", ""),
        )
        setattr(info, "_raw_task", "write it")
        setattr(
            info, "_a2a_entry", A2aAgentConfig(name="remote-demo", agent_card_url="https://h/c")
        )
        manager._agents["rr01"] = info
        # The run path reads its own record back (execution owner + memory
        # mode), so the record is written for real under the suite's isolated
        # KIROCREW_HOME rather than mocked away.
        create_agent_folder(info.id, task=info.task, execution_context=info.execution_context)
        return manager, info

    @staticmethod
    def _run_patches(provider):
        """The run path calls the component impl directly, and components are
        slotted, so the build step is patched on the class; plus the usual
        persistence and stats doubles."""
        from unittest.mock import AsyncMock

        from kiro_crew.subagent_manager import run

        return (
            patch.object(
                run.RunEventCoordinator,
                "_build_a2a_provider_impl",
                AsyncMock(return_value=provider),
            ),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        )

    @staticmethod
    def _stub_provider(events, *, context_id="ctx-1", started=True):
        from unittest.mock import AsyncMock

        provider = MagicMock()

        async def _stream(*_a, **_kw):
            for ev in events:
                yield ev

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_id = context_id
        provider.session_id = context_id
        provider.started = started
        provider.supports_steer = False
        provider.provider_label = "a2a"
        return provider

    @pytest.mark.asyncio
    async def test_the_record_is_the_final_artifact_content_not_the_streamed_draft(self):
        """The runner builds the transcript from what streamed, but a remote run's
        RECORD (``info.result``, what the parent reads) is the completion text --
        the artifacts' final content -- so a draft that streamed and was then
        replaced never reaches the parent."""
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="DRAFT "),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="FINAL"),
            LLMEvent(kind=EVENT_COMPLETE, text="FINAL", stop_reason="end_turn"),
        ]
        provider = self._stub_provider(events)
        manager, info = self._remote_manager(provider)
        with contextlib.ExitStack() as stack:
            for p in self._run_patches(provider):
                stack.enter_context(p)
            await manager._run_inner(info, "subagent:rr01")
        assert info.result == "FINAL"
        assert not info.error

    @pytest.mark.asyncio
    async def test_a_continuation_whose_provider_never_started_is_resume_failed(self):
        """A retained contextId alone must not report a live resume: the build
        step swallows a failed card fetch on a continuation so THIS guard, not a
        blank fresh conversation, is the outcome."""
        events = [LLMEvent(kind=EVENT_COMPLETE, text="", stop_reason="end_turn")]
        provider = self._stub_provider(events, context_id="ctx-1", started=False)
        manager, info = self._remote_manager(provider)
        info.conversation_key = "subagent:rr00"
        # A continuation checks its memory owner against what the ORIGINAL run
        # published for the conversation's session; publish that first, as the
        # first run would have (the record is the one _remote_manager wrote).
        from kiro_crew.execution_context import bind_session_execution

        create_agent_folder("rr00", task="write it", execution_context=info.execution_context)
        bind_session_execution("subagent:rr00", info.execution_context, replace_existing=True)
        with contextlib.ExitStack() as stack:
            for p in self._run_patches(provider):
                stack.enter_context(p)
            # The continuation guard restores the original run's memory mode
            # from its state.json; not what this test is about.
            stack.enter_context(
                patch(
                    "kiro_crew.subagent_persistence.tighten_run_memory_mode", lambda _id, mode: mode
                )
            )
            with pytest.raises(RuntimeError, match="resume_failed"):
                await manager._run_inner(info, "subagent:rr00")
        provider.stream.assert_not_called()

        # Same retained contextId, provider that DID start: the resume is live.
        live = self._stub_provider(events, context_id="ctx-1", started=True)
        manager2, info2 = self._remote_manager(live)
        info2.conversation_key = "subagent:rr00"
        with contextlib.ExitStack() as stack:
            for p in self._run_patches(live):
                stack.enter_context(p)
            stack.enter_context(
                patch(
                    "kiro_crew.subagent_persistence.tighten_run_memory_mode", lambda _id, mode: mode
                )
            )
            await manager2._run_inner(info2, "subagent:rr00")
        live.stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_l_canceled_is_terminal(self):
        """v1.0 proto spelling is TASK_STATE_CANCELED; the 0.3 layer says CANCELLED.
        Either is terminal (never read as a lost connection) and ends the turn as a
        cancelled FAILURE: cancelled work must not be recorded as completed."""
        for spelling in ("TASK_STATE_CANCELED", "TASK_STATE_CANCELLED"):
            lines = [
                _sse(
                    '{"result": {"task": {"id": "t-1", "contextId": "ctx-1",'
                    ' "status": {"state": "TASK_STATE_WORKING"}}}, "id": "1", "jsonrpc": "2.0"}'
                ),
                b"",
                _sse(
                    '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
                    ' "status": {"state": "' + spelling + '"}}}, "id": "1", "jsonrpc": "2.0"}'
                ),
                b"",
            ]
            p = A2AProvider(
                name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json"
            )
            p._started = True
            p._message_endpoint = "http://h/"
            p._session = _FakePostSession(lines)
            with pytest.raises(A2AStreamError, match="cancelled") as ei:
                await _drain(p.stream("go"))
            # Terminal: the cancel is named, not read as a lost connection.
            assert "without a terminal task state" not in str(ei.value), spelling


def _task_frame(state: str, artifacts: str = "", message: str = "") -> bytes:
    """A ``Task`` snapshot frame; ``artifacts`` is the raw JSON list body."""
    status = '{"state": "' + state + '"'
    if message:
        status += ', "message": {"role": "ROLE_AGENT", "parts": [{"text": "' + message + '"}]}'
    status += "}"
    arts = (', "artifacts": [' + artifacts + "]") if artifacts else ""
    return _sse(
        '{"result": {"task": {"id": "t-1", "contextId": "ctx-1", "status": '
        + status
        + arts
        + '}}, "id": "1", "jsonrpc": "2.0"}'
    )


def _snapshot_provider(lines: list[bytes]) -> A2AProvider:
    p = A2AProvider(name="local-kiro", agent_card_url="http://h/.well-known/agent-card.json")
    p._started = True
    p._message_endpoint = "http://h/"
    p._session = _FakePostSession(lines)
    return p


class TestTerminalTaskSnapshotEndsTheTurn:
    """A ``Task`` frame in a terminal state is a final event, not a dropped stream.

    The spec lets a server finish a turn either with a terminal
    ``TaskStatusUpdateEvent`` or with a ``Task`` whose status is terminal; the
    reference SDK's ``completed_task()`` helper (and its sample agents) emit the
    latter with the result inline as ``task.artifacts``. Reading a Task frame for
    its ids alone made every such server look like a lost connection: the turn
    was recorded as failed and its output discarded.
    """

    @pytest.mark.asyncio
    async def test_completed_task_with_inline_artifacts_completes_with_that_text(self):
        lines = [
            _task_frame("TASK_STATE_WORKING"),
            b"",
            _task_frame(
                "TASK_STATE_COMPLETED",
                artifacts='{"artifactId": "out", "parts": [{"text": "the answer"}]}',
            ),
            b"",
        ]
        events = await _drain(_snapshot_provider(lines).stream("go"))
        assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["the answer"]
        assert events[-1].kind == EVENT_COMPLETE and events[-1].text == "the answer"

    @pytest.mark.asyncio
    async def test_snapshot_is_authoritative_over_streamed_deltas(self):
        """Artifacts streamed live and then restated whole by the terminal
        snapshot are not doubled; an artifact the snapshot states DIFFERENTLY
        is replaced and delivered once, and the record is the snapshot's form."""
        art = TestArtifactsAndCancelSpelling._art
        lines = [
            _task_frame("TASK_STATE_WORKING"),
            b"",
            art("a", "one ", append=False),
            b"",
            art("a", "two", append=True),
            b"",
            art("b", "draft", append=False),
            b"",
            _task_frame(
                "TASK_STATE_COMPLETED",
                artifacts=(
                    '{"artifactId": "a", "parts": [{"text": "one two"}]},'
                    ' {"artifactId": "b", "parts": [{"text": "final"}]}'
                ),
            ),
            b"",
        ]
        events = await _drain(_snapshot_provider(lines).stream("go"))
        chunks = [e.text for e in events if e.kind == EVENT_TEXT_CHUNK]
        # "a" streamed live and matches the snapshot verbatim: not repeated.
        # "b" was restated: its draft streamed live, the final form once, last.
        assert chunks == ["one ", "two", "draft", "final"]
        assert events[-1].text == "one twofinal"

    @pytest.mark.asyncio
    async def test_failed_task_snapshot_is_a_failed_turn_with_its_reason(self):
        lines = [
            _task_frame("TASK_STATE_WORKING"),
            b"",
            _task_frame("TASK_STATE_FAILED", message="no can do"),
            b"",
        ]
        with pytest.raises(A2AStreamError) as ei:
            await _drain(_snapshot_provider(lines).stream("go"))
        assert "task_state_failed" in str(ei.value) and "no can do" in str(ei.value)
        assert "without a terminal task state" not in str(ei.value)

    @pytest.mark.asyncio
    async def test_non_terminal_snapshot_still_only_adopts_ids(self):
        """The opening WORKING snapshot carries no result; a stream that ends
        after it is still a truncated turn."""
        lines = [_task_frame("TASK_STATE_WORKING"), b""]
        with pytest.raises(A2AStreamError, match="without a terminal task state"):
            await _drain(_snapshot_provider(lines).stream("go"))


class TestSdkLabelPin:
    def test_driver_label_equals_acp_label(self):
        """``agent_sdk.drivers.a2a.A2A_PROVIDER_LABEL`` is a literal, not an import
        (application code must not add ACP-layer import edges), so it is pinned
        equal here: a divergence fails loudly instead of silently mis-detecting
        persisted A2A runs in ``subagent.py``."""
        from kiro_crew.agent_sdk.drivers.a2a import A2A_PROVIDER_LABEL

        assert A2A_PROVIDER_LABEL == PROVIDER_LABEL_A2A
        assert (
            A2AProvider(name="x", agent_card_url="http://h/c.json").provider_label
            == A2A_PROVIDER_LABEL
        )


class TestEgressBoundary:
    """What may leave the host, and what the remote may steer -- fail closed."""

    def test_message_endpoint_on_another_origin_is_refused(self):
        p = A2AProvider(
            name="r", agent_card_url="https://agent.example/.well-known/agent-card.json"
        )
        card = {
            "supportedInterfaces": [
                {"url": "http://169.254.169.254/latest/", "protocolBinding": "JSONRPC"},
            ]
        }
        with pytest.raises(A2AStreamError) as ei:
            p._resolve_message_endpoint(card)
        assert "another origin" in str(ei.value)

    def test_message_endpoint_same_origin_is_accepted(self):
        p = A2AProvider(
            name="r", agent_card_url="https://agent.example/.well-known/agent-card.json"
        )
        card = {
            "supportedInterfaces": [
                {"url": "https://agent.example/a2a/v1", "protocolBinding": "JSONRPC"}
            ]
        }
        assert p._resolve_message_endpoint(card) == "https://agent.example/a2a/v1"

    def test_scheme_downgrade_is_another_origin(self):
        p = A2AProvider(
            name="r", agent_card_url="https://agent.example/.well-known/agent-card.json"
        )
        card = {
            "supportedInterfaces": [
                {"url": "http://agent.example/a2a/v1", "protocolBinding": "JSONRPC"}
            ]
        }
        with pytest.raises(A2AStreamError):
            p._resolve_message_endpoint(card)

    @pytest.mark.asyncio
    async def test_posts_never_follow_redirects(self):
        class _Recording(_FakePostSession):
            def post(self, url, **kw):  # noqa: A003
                self.kwargs = kw
                return super().post(url, **kw)

        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _Recording(_STREAM_LINES)
        await _drain(p.stream("hi"))
        assert p._session.kwargs["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_auth_required_is_a_failed_run_never_answered(self):
        lines = [
            _sse(
                '{"result": {"task": {"id": "t-a", "contextId": "ctx-a",'
                ' "status": {"state": "TASK_STATE_SUBMITTED"}}}, "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
            _sse(
                '{"result": {"statusUpdate": {"taskId": "t-a",'
                ' "status": {"state": "TASK_STATE_AUTH_REQUIRED",'
                ' "message": {"role": "ROLE_AGENT", "parts": [{"text": "send me your token"}]}}}},'
                ' "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
        ]
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        with pytest.raises(A2AStreamError) as ei:
            await _drain(p.stream("hi"))
        assert "auth_required" in str(ei.value)
        # Exactly one POST: the request was never answered with a credential.
        assert len(p._session.posted) == 1

    def test_remote_message_is_task_text_only(self):
        from kiro_crew.subagent import _REMOTE_TASK_PREFIX, build_remote_task_message

        msg = build_remote_task_message("Summarise https://example.com/report")
        assert msg.startswith(_REMOTE_TASK_PREFIX)
        assert msg.endswith("Summarise https://example.com/report")
        # None of the local envelope's markers may appear.
        for marker in (
            "[SESSION CONTEXT",
            "[CURRENT USER REQUEST",
            "User Preferences",
            "Skills:",
            "spawn_run",
        ):
            assert marker not in msg, marker

    def test_remote_message_is_redacted(self):
        from kiro_crew.subagent import build_remote_task_message

        msg = build_remote_task_message("use AKIAIOSFODNN7EXAMPLE to list the bucket")
        assert "AKIAIOSFODNN7EXAMPLE" not in msg


class TestAuthSchemes:
    """Card-declared schemes, credential from a NAMED env var, fail closed."""

    def test_none_sends_no_auth_header(self):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        creds, supported = drv.resolve_auth(A2aAuthConfig())
        assert creds is None and supported == frozenset()

    def test_bearer_reads_env_at_request_time(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED", "first")
        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED_ORIGIN", "https://Agents.Example:8443")
        cred, supported = drv.resolve_auth(
            A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_CRED")
        )
        assert supported == frozenset({"bearer"})
        assert cred.headers() == {"Authorization": "Bearer first"}
        assert cred.origin == "https://agents.example:8443"
        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED", "rotated")
        assert cred.headers() == {"Authorization": "Bearer rotated"}

    def test_bearer_without_env_name_value_or_origin_is_refused(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        with pytest.raises(ValueError):
            drv.resolve_auth(A2aAuthConfig(scheme="bearer"))
        monkeypatch.delenv("KIROCREW_A2A_REMOTE_CRED", raising=False)
        with pytest.raises(ValueError):
            drv.resolve_auth(A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_CRED"))
        # Value set, but no pinned origin beside it: refused, with the fix named.
        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED", "t")
        monkeypatch.delenv("KIROCREW_A2A_REMOTE_CRED_ORIGIN", raising=False)
        with pytest.raises(ValueError, match="_ORIGIN"):
            drv.resolve_auth(A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_CRED"))
        # An origin that is not scheme://host[:port] is not an origin.
        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED_ORIGIN", "agents.example/path")
        with pytest.raises(ValueError, match="pinned origin"):
            drv.resolve_auth(A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_CRED"))

    def test_config_cannot_redirect_a_credential(self, monkeypatch):
        # The exfiltration shape: a valid credential kept, agent_card_url moved to
        # another (TLS) host by a config write. Refused at construction, before
        # any request -- the origin pin lives in the environment, not in config.
        from kiro_crew.agent_sdk.drivers import a2a as drv

        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED", "t")
        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED_ORIGIN", "https://agents.example")
        auth = A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_CRED")
        with pytest.raises(ValueError, match="pinned to"):
            drv.create_a2a_provider(
                A2aAgentConfig(
                    name="r",
                    agent_card_url="https://evil.example/.well-known/agent-card.json",
                    auth=auth,
                ),
                context_id=None,
            )
        p = drv.create_a2a_provider(
            A2aAgentConfig(
                name="r",
                agent_card_url="https://agents.example/.well-known/agent-card.json",
                auth=auth,
            ),
            context_id=None,
        )
        assert p._credential_origin == "https://agents.example"

    def test_unknown_scheme_is_refused(self):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        with pytest.raises(ValueError) as ei:
            drv.resolve_auth(A2aAuthConfig(scheme="sigv4"))
        assert "not supported" in str(ei.value)

    def test_edition_can_register_a_scheme(self):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        drv.register_auth_scheme(
            drv.A2aAuthScheme(
                "probe",
                frozenset({"apiKey"}),
                lambda a: drv.ResolvedCredential(lambda: {"X-Probe": "1"}, "https://h"),
            )
        )
        try:
            cred, supported = drv.resolve_auth(A2aAuthConfig(scheme="probe"))
            assert cred.headers() == {"X-Probe": "1"} and supported == frozenset({"apiKey"})
            assert cred.origin == "https://h"
        finally:
            drv._SCHEMES.pop("probe", None)

    def test_card_requiring_bearer_without_credentials_refuses_start(self):
        p = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        card = {
            "securitySchemes": {"idp": {"openIdConnectSecurityScheme": {"openIdConnectUrl": "x"}}},
            "securityRequirements": [{"schemes": {"idp": {"list": ["openid"]}}}],
        }
        with pytest.raises(A2AStreamError) as ei:
            p._check_security_requirements(card)
        assert "requires authentication" in str(ei.value)

    def test_card_requiring_bearer_with_bearer_configured_starts(self):
        p = A2AProvider(
            name="r",
            agent_card_url="https://h/.well-known/agent-card.json",
            credentials=lambda: {"Authorization": "Bearer t"},
            supported_schemes=frozenset({"bearer"}),
        )
        for card in (
            {
                "securitySchemes": {"idp": {"openIdConnectSecurityScheme": {}}},
                "securityRequirements": [{"schemes": {"idp": {}}}],
            },
            {
                "securitySchemes": {"b": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
                "securityRequirements": [{"schemes": {"b": {}}}],
            },
            {
                "securitySchemes": {"b": {"type": "http", "scheme": "bearer"}},
                "security": [{"b": []}],
            },
            {},
        ):
            p._check_security_requirements(card)  # no raise

    def test_card_with_an_anonymous_alternative_starts_without_credentials(self):
        p = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        card = {
            "securitySchemes": {"b": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
            "securityRequirements": [{"schemes": {"b": {}}}, {}],
        }
        p._check_security_requirements(card)

    @pytest.mark.asyncio
    async def test_bearer_header_is_sent_on_every_post(self):
        p = A2AProvider(
            name="r",
            agent_card_url="http://h/.well-known/agent-card.json",
            credentials=lambda: {"Authorization": "Bearer t-1"},
            supported_schemes=frozenset({"bearer"}),
        )
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(_STREAM_LINES)
        await _drain(p.stream("hi"))
        assert p._session.posted[0]["headers"]["Authorization"] == "Bearer t-1"


class TestCredentialTransport:
    """Everything this client sends travels only over TLS, or over plain HTTP to a
    loopback origin the gateway's ENVIRONMENT names; and a start() that fails
    releases the client session it opened -- nothing else can, because a
    provider that never started is never stashed on the run record."""

    def test_predicate_refuses_plaintext_by_default(self, monkeypatch):
        from kiro_crew.providers.a2a import A2A_PLAINTEXT_ORIGINS_ENV
        from kiro_crew.providers.a2a import _credentials_may_travel as ok

        monkeypatch.delenv(A2A_PLAINTEXT_ORIGINS_ENV, raising=False)
        assert ok("https://agents.example/.well-known/agent-card.json")
        # Loopback is not exempt: an agent-written config entry at
        # http://127.0.0.1:<port> would reach whatever listens on that port.
        assert not ok("http://127.0.0.1:8123/.well-known/agent-card.json")
        assert not ok("http://localhost/.well-known/agent-card.json")
        assert not ok("http://[::1]:9/.well-known/agent-card.json")
        assert not ok("http://agents.example/.well-known/agent-card.json")
        assert not ok("http://10.0.0.5/.well-known/agent-card.json")
        assert not ok("ftp://agents.example/card")
        assert not ok("")

    def test_environment_admits_exact_loopback_origins_only(self, monkeypatch):
        from kiro_crew.providers.a2a import A2A_PLAINTEXT_ORIGINS_ENV
        from kiro_crew.providers.a2a import _credentials_may_travel as ok

        monkeypatch.setenv(
            A2A_PLAINTEXT_ORIGINS_ENV,
            # An exact loopback origin, an IPv6 one, and two entries that must be
            # ignored: a non-loopback host and a TLS origin (which needs no entry).
            "http://127.0.0.1:8123, http://[::1]:9,http://10.0.0.5:8123,https://agents.example",
        )
        assert ok("http://127.0.0.1:8123/.well-known/agent-card.json")
        assert ok("HTTP://127.0.0.1:8123/other/path")  # origin, not URL, is compared
        assert ok("http://[::1]:9/.well-known/agent-card.json")
        assert not ok("http://127.0.0.1:8124/.well-known/agent-card.json")  # other port
        assert not ok("http://127.0.0.2:8123/.well-known/agent-card.json")  # other address
        assert not ok("http://localhost:8123/.well-known/agent-card.json")  # other name
        assert not ok("http://10.0.0.5:8123/.well-known/agent-card.json")  # never plaintext
        assert not ok("http://agents.example/.well-known/agent-card.json")

    @pytest.mark.asyncio
    async def test_unadmitted_loopback_plaintext_never_starts(self, monkeypatch):
        import kiro_crew.providers.a2a as mod

        monkeypatch.delenv(mod.A2A_PLAINTEXT_ORIGINS_ENV, raising=False)
        opened: list[object] = []
        monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: opened.append(object()))
        p = A2AProvider(
            name="r", agent_card_url="http://127.0.0.1:8123/.well-known/agent-card.json"
        )
        with pytest.raises(A2AStreamError, match=mod.A2A_PLAINTEXT_ORIGINS_ENV):
            await p.start()
        assert opened == [] and p._session is None

    @pytest.mark.asyncio
    async def test_unauthenticated_plaintext_to_a_remote_host_never_starts(self, monkeypatch):
        # Task text is session-derived data: the TLS rule holds for `none` too.
        import kiro_crew.providers.a2a as mod

        opened: list[object] = []
        monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: opened.append(object()))
        p = A2AProvider(
            name="r", agent_card_url="http://agents.example/.well-known/agent-card.json"
        )
        with pytest.raises(A2AStreamError, match="not https"):
            await p.start()
        assert opened == [] and p._session is None

    @pytest.mark.asyncio
    async def test_bearer_over_plaintext_to_a_remote_host_never_starts(self, monkeypatch):
        import kiro_crew.providers.a2a as mod

        opened: list[object] = []
        monkeypatch.setattr(
            mod.aiohttp, "ClientSession", lambda *a, **k: opened.append(object())  # never called
        )
        p = A2AProvider(
            name="r",
            agent_card_url="http://agents.example/.well-known/agent-card.json",
            credentials=lambda: {"Authorization": "Bearer t"},
            supported_schemes=frozenset({"bearer"}),
        )
        with pytest.raises(A2AStreamError, match="not https"):
            await p.start()
        assert opened == []  # refused before any socket was opened
        assert p._session is None and not p._started

    @pytest.mark.asyncio
    async def test_failed_card_fetch_closes_the_session(self, monkeypatch):
        import kiro_crew.providers.a2a as mod

        class _Boom:
            closed = False

            def get(self, *a, **k):
                raise ConnectionError("unreachable")

            async def close(self):
                self.closed = True

        boom = _Boom()
        monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: boom)
        p = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        with pytest.raises(ConnectionError):
            await p.start()
        assert boom.closed
        assert p._session is None and not p._started


class TestContextIdSurvivesFailedTurns:
    """The remote conversation handle is persisted by the provider RELEASE, which
    every terminal path reaches -- not only by the post-turn write, which a
    raising stream (the ordinary dropped/failed-task path) skips. Without this a
    conversation the remote still holds was recorded as ``conversation_gone``."""

    @staticmethod
    def _manager():
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        mgr = SubagentManager(sessions=sessions, ctx_builder=None)  # type: ignore[arg-type]
        writes: list[dict] = []

        async def _record(info, what, **fields):
            writes.append(dict(fields))
            return True

        mgr._write_state_off_loop = _record  # type: ignore[method-assign]
        return mgr, writes

    @staticmethod
    def _provider(ctx: str) -> A2AProvider:
        p = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        p._context_id = ctx
        p._session = _FakePostSession([])
        return p

    @pytest.mark.asyncio
    async def test_release_persists_an_adopted_context_id(self):
        from kiro_crew.subagent import SubagentInfo

        mgr, writes = self._manager()
        info = SubagentInfo(id="r1", task="t", agent="r")
        setattr(info, "_session_id", "")  # the acquisition-time record: no contextId yet
        setattr(info, "_direct_provider", self._provider("ctx-77"))
        await mgr._release_direct_provider(info, cancel=False)
        assert writes == [{"session_id": "ctx-77"}]
        assert getattr(info, "_session_id") == "ctx-77"
        assert getattr(info, "_direct_provider") is None

    @pytest.mark.asyncio
    async def test_release_is_a_no_op_when_already_recorded(self):
        from kiro_crew.subagent import SubagentInfo

        mgr, writes = self._manager()
        info = SubagentInfo(id="r2", task="t", agent="r")
        setattr(info, "_session_id", "ctx-77")  # the post-turn write already ran
        setattr(info, "_direct_provider", self._provider("ctx-77"))
        await mgr._release_direct_provider(info, cancel=False)
        assert writes == []

    @pytest.mark.asyncio
    async def test_teardown_cancels_the_remote_when_the_run_ended_with_an_error(self):
        """The normal teardown (not stop/reap) issues CancelTask when the run did
        not end cleanly -- the wall-clock timeout above all, where the remote is
        still working on a task nobody will read -- and skips it on a clean end."""
        from unittest.mock import AsyncMock

        from kiro_crew.subagent import SubagentInfo

        mgr, _writes = self._manager()
        mgr._sessions.release = MagicMock()
        mgr._sessions.reset = AsyncMock()

        async def run(error: str) -> bool:
            info = SubagentInfo(id="r3", task="t", agent="r")
            info.error = error
            setattr(info, "_session_id", "ctx-77")
            p = self._provider("ctx-77")
            p.cancel = AsyncMock()  # type: ignore[method-assign]
            p.shutdown = AsyncMock()  # type: ignore[method-assign]
            setattr(info, "_direct_provider", p)
            await mgr._teardown_run_session(info, "subagent:r3")
            assert p.shutdown.await_count == 1
            return p.cancel.await_count == 1

        assert await run("Timed out after 30 minutes [...]") is True
        assert await run("") is False


class TestBoundsOnRemoteInput:
    """A remote endpoint is operator configuration, but config.json is agent-writable
    and the memory guard cannot measure a remote run -- so what a config entry may
    select, and what a remote may make this process hold, are both bounded."""

    def test_token_env_must_be_in_the_a2a_namespace(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        # A gateway secret is set and would be a valid bearer value -- config
        # still cannot select it.
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-for-remotes")
        with pytest.raises(ValueError, match="namespace"):
            drv.resolve_auth(A2aAuthConfig(scheme="bearer", token_env="SLACK_BOT_TOKEN"))
        with pytest.raises(ValueError, match="namespace"):
            drv.resolve_auth(A2aAuthConfig(scheme="bearer", token_env=drv.A2A_TOKEN_ENV_PREFIX))
        monkeypatch.setenv("KIROCREW_A2A_OK", "t")
        monkeypatch.setenv("KIROCREW_A2A_OK_ORIGIN", "https://h")
        cred, _ = drv.resolve_auth(A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_OK"))
        assert cred is not None and cred.headers() == {"Authorization": "Bearer t"}

    @pytest.mark.asyncio
    async def test_oversized_agent_card_refuses_start(self, monkeypatch):
        import kiro_crew.providers.a2a as mod

        class _Body:
            async def read(self, n):
                return b"{" + b" " * n  # one byte past the cap, never parsed

        class _Resp:
            status = 200
            content = _Body()

            def raise_for_status(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            closed = False

            def get(self, *a, **k):
                return _Resp()

            async def close(self):
                self.closed = True

        sess = _Session()
        monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: sess)
        p = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        with pytest.raises(A2AStreamError, match="exceeds"):
            await p.start()
        assert sess.closed

    @pytest.mark.asyncio
    async def test_turn_text_over_cap_is_a_failed_turn(self, monkeypatch):
        import kiro_crew.providers.a2a as mod

        monkeypatch.setattr(mod, "_MAX_TURN_TEXT_CHARS", 20)
        chunk = _sse(
            '{"result": {"statusUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            ' "status": {"state": "TASK_STATE_WORKING",'
            ' "message": {"role": "ROLE_AGENT", "parts": [{"text": "0123456789"}]}}}},'
            ' "id": "1", "jsonrpc": "2.0"}'
        )
        lines = [_STREAM_LINES[0], b""] + [chunk, b""] * 5  # 50 chars, cap is 20
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        with pytest.raises(A2AStreamError, match="output cap"):
            await _drain(p.stream("hi"))

    @staticmethod
    def _empty_artifact(aid: str) -> bytes:
        return _sse(
            '{"result": {"artifactUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            ' "artifact": {"artifactId": "' + aid + '", "parts": [{"text": ""}]}}},'
            ' "id": "1", "jsonrpc": "2.0"}'
        )

    @pytest.mark.asyncio
    async def test_many_empty_artifacts_under_fresh_ids_hit_the_artifact_cap(self, monkeypatch):
        """The text cap measures joined artifact TEXT; a server emitting empty
        artifacts under a new id per frame grew the per-artifact table without
        bound while that measure stayed at zero. The table is capped by count."""
        import kiro_crew.providers.a2a as mod

        monkeypatch.setattr(mod, "_MAX_TURN_ARTIFACTS", 4)
        lines = [_STREAM_LINES[0], b""]
        for i in range(6):  # 6 distinct ids, cap is 4
            lines += [self._empty_artifact(f"a-{i}"), b""]
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        with pytest.raises(A2AStreamError, match="distinct artifacts"):
            await _drain(p.stream("hi"))

    @pytest.mark.asyncio
    async def test_artifact_id_length_counts_toward_the_text_cap(self, monkeypatch):
        """A count cap alone leaves one very long id uncounted; the id's length is
        part of the footprint the text cap bounds."""
        import kiro_crew.providers.a2a as mod

        monkeypatch.setattr(mod, "_MAX_TURN_TEXT_CHARS", 20)
        lines = [_STREAM_LINES[0], b"", self._empty_artifact("x" * 50), b""]
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        with pytest.raises(A2AStreamError, match="output cap"):
            await _drain(p.stream("hi"))

    @pytest.mark.asyncio
    async def test_distinct_artifact_inside_status_text_is_not_dropped(self):
        # Status text "Hello world" precedes an artifact "world": the old
        # substring test dropped it; only an EXACT full recap is skipped.
        lines = list(_STREAM_LINES)
        lines[6] = _sse(
            '{"result": {"artifactUpdate": {"taskId": "t-1", "contextId": "ctx-1",'
            ' "artifact": {"artifactId": "a-1", "name": "response",'
            ' "parts": [{"text": "world"}]}, "lastChunk": true}},'
            ' "id": "1", "jsonrpc": "2.0"}'
        )
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(lines)
        texts = [e.text for e in await _drain(p.stream("hi")) if e.kind == EVENT_TEXT_CHUNK]
        assert texts == ["Hello ", "world", "world"]
        # ...while the exact recap in the canonical stream is still deduplicated.
        p._session = _FakePostSession(_STREAM_LINES)
        texts = [e.text for e in await _drain(p.stream("hi")) if e.kind == EVENT_TEXT_CHUNK]
        assert texts == ["Hello ", "world"]

    def test_read_timeout_is_idle_not_total(self):
        p = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        t = p._make_timeout()
        assert t.total is None and t.sock_read and t.sock_read > 0


class TestReapOwnsRecordBeforeRemoteRelease:
    def test_release_follows_ownership_claim_and_local_cancel(self):
        import inspect

        from kiro_crew.subagent_manager import terminal

        src = inspect.getsource(terminal.TerminalCoordinator._force_reap_impl)
        release = src.index("_release_direct_provider(info, cancel=True)")
        cancel = src.index("_cancel_task_intentionally(task, info")
        # The LAST `info.reaped = True` (the unconditional one after the cancel
        # block) must precede the release; so must the intentional cancel.
        reaped = src.rindex("info.reaped = True")
        assert cancel < release and reaped < release
        assert src.count("_release_direct_provider(info, cancel=True)") == 1


class TestContinuationRefusesAnotherConversation:
    @pytest.mark.asyncio
    async def test_different_context_id_on_a_retained_conversation_fails_the_turn(self):
        # Retained ctx-1; the server answers under ctx-9. Adopting it silently
        # would record unrelated output as this conversation's.
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._context_id = "ctx-1"
        p._started = True
        p._message_endpoint = "http://h/"
        lines = [
            _sse(
                '{"result": {"task": {"id": "t-2", "contextId": "ctx-9",'
                ' "status": {"state": "TASK_STATE_SUBMITTED"}}}, "id": "1", "jsonrpc": "2.0"}'
            ),
            b"",
        ] + _STREAM_LINES[2:]
        p._session = _FakePostSession(lines)
        with pytest.raises(A2AStreamError, match="contextId mismatch"):
            await _drain(p.stream("more"))
        assert p.context_id == "ctx-1"  # the retained handle is untouched

    @pytest.mark.asyncio
    async def test_same_context_id_continues(self):
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._context_id = "ctx-1"
        p._started = True
        p._message_endpoint = "http://h/"
        p._session = _FakePostSession(_STREAM_LINES)  # all frames carry ctx-1
        events = await _drain(p.stream("more"))
        assert events[-1].kind == EVENT_COMPLETE


class TestTurnIsOneTask:
    """One ``message/stream`` call is one task: the first frame naming a task locks
    the turn, and a frame for any other task fails it. Otherwise a misbehaving
    server could have another task's text recorded as this run's output, or move
    the target a stop cancels onto a task this run never started."""

    def _provider(self) -> A2AProvider:
        p = A2AProvider(name="r", agent_card_url="http://h/.well-known/agent-card.json")
        p._started = True
        p._message_endpoint = "http://h/"
        return p

    @pytest.mark.asyncio
    async def test_status_frame_for_another_task_fails_the_turn(self):
        p = self._provider()
        foreign = _sse(
            '{"result": {"statusUpdate": {"taskId": "t-9", "contextId": "ctx-1",'
            ' "status": {"state": "TASK_STATE_WORKING", "message": {"parts":'
            ' [{"text": "not yours"}]}}}}, "id": "1", "jsonrpc": "2.0"}'
        )
        # Task frame for t-1 (locks), then a WORKING frame for t-9, then t-1's end.
        lines = [_STREAM_LINES[0], b"", foreign, b""] + _STREAM_LINES[2:]
        p._session = _FakePostSession(lines)
        with pytest.raises(A2AStreamError, match="taskId mismatch"):
            await _drain(p.stream("go"))
        assert p._current_task_id == "t-1"  # the stop target stays on the locked task

    @pytest.mark.asyncio
    async def test_artifact_frame_for_another_task_is_refused(self):
        p = self._provider()
        foreign = _sse(
            '{"result": {"artifactUpdate": {"taskId": "t-9", "contextId": "ctx-1",'
            ' "artifact": {"artifactId": "a", "parts": [{"text": "smuggled"}]}}},'
            ' "id": "1", "jsonrpc": "2.0"}'
        )
        lines = [_STREAM_LINES[0], b"", foreign, b""] + _STREAM_LINES[2:]
        p._session = _FakePostSession(lines)
        chunks: list[str] = []
        with pytest.raises(A2AStreamError, match="taskId mismatch"):
            async for ev in p.stream("go"):
                if ev.kind == EVENT_TEXT_CHUNK:
                    chunks.append(ev.text or "")
        assert "smuggled" not in "".join(chunks)

    @pytest.mark.asyncio
    async def test_each_stream_call_locks_afresh(self):
        # Turn 1 is task t-1; turn 2 on the same provider is a new task and is
        # anchored to t-1 through referenceTaskIds, not refused as a mismatch.
        p = self._provider()
        p._session = _FakePostSession(_STREAM_LINES)
        assert (await _drain(p.stream("one")))[-1].kind == EVENT_COMPLETE
        turn2 = [ln.replace(b'"t-1"', b'"t-2"') for ln in _STREAM_LINES]
        p._session = _FakePostSession(turn2)
        assert (await _drain(p.stream("two")))[-1].kind == EVENT_COMPLETE
        assert p._current_task_id == "t-2"
        sent = p._session.posted[-1]["json"]["params"]["message"]
        assert sent["referenceTaskIds"] == ["t-1"]


class TestContinuationStaysWithTheRecordedAgent:
    """A remote conversation belongs to the agent that minted its contextId (A2A has
    no notion of handing a context to another agent). The facts come from ONE
    ``state.json`` read -- ``recorded_a2a`` -- resolved off the event loop by the
    async caller and handed to the synchronous ``continue_conversation``, which
    reads nothing itself (the same contract as ``cwd``)."""

    @staticmethod
    def _manager(monkeypatch):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentInfo, SubagentManager

        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)  # type: ignore[arg-type]
        mgr._agents["conv1"] = SubagentInfo(id="conv1", task="t")
        monkeypatch.setattr(mgr, "_conversation_busy", lambda _k: None)
        monkeypatch.setattr(mgr._sessions, "resumable_sid", lambda _k: "ctx-1")
        monkeypatch.setattr(mgr, "_promote_conversation", lambda *_a: None)
        monkeypatch.setattr(mgr, "_inherited_memory_store", lambda _id: "")
        monkeypatch.setattr(mgr, "_inherited_context_groups", lambda _id: (True, True, True))
        promoted: list[object] = []
        monkeypatch.setattr(mgr, "_promote_conversation", lambda *a: promoted.append(a))
        captured: dict[str, object] = {}
        monkeypatch.setattr(mgr, "spawn", lambda *_a, **kw: captured.update(kw))
        return mgr, captured, promoted

    def test_recorded_a2a_is_the_one_read(self, monkeypatch):
        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_manager import RecordedA2A

        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)  # type: ignore[arg-type]
        monkeypatch.setattr(
            "kiro_crew.subagent.read_state",
            lambda _id: {"provider": "a2a", "agent": "remote-demo", "session_id": "ctx-1"},
        )
        assert mgr.recorded_a2a("conv1") == RecordedA2A(agent="remote-demo", context_id="ctx-1")
        # A local run has no record; an A2A run that never adopted a contextId
        # has an empty handle (the rebuilt provider then fails resume_failed).
        monkeypatch.setattr("kiro_crew.subagent.read_state", lambda _id: {"provider": "kiro"})
        assert mgr.recorded_a2a("conv1") is None
        monkeypatch.setattr(
            "kiro_crew.subagent.read_state", lambda _id: {"provider": "a2a", "agent": "r"}
        )
        assert mgr.recorded_a2a("conv1") == RecordedA2A(agent="r", context_id="")

    def test_empty_agent_inherits_the_recorded_one(self, monkeypatch):
        from kiro_crew.subagent_manager import RecordedA2A

        mgr, captured, _ = self._manager(monkeypatch)
        rec = RecordedA2A(agent="remote-demo", context_id="ctx-1")
        mgr.continue_conversation("conv1", "more", a2a_record=rec)
        assert captured["agent"] == "remote-demo"
        # Naming the same agent is fine too.
        captured.clear()
        mgr.continue_conversation("conv1", "more", agent="remote-demo", a2a_record=rec)
        assert captured["agent"] == "remote-demo"

    def test_a_different_agent_is_refused_before_any_side_effect(self, monkeypatch):
        from kiro_crew.subagent_manager import RecordedA2A

        mgr, captured, promoted = self._manager(monkeypatch)
        rec = RecordedA2A(agent="remote-demo", context_id="ctx-1")
        info = mgr.continue_conversation("conv1", "more", agent="other-remote", a2a_record=rec)
        assert info is not None and info.done
        assert info.error.startswith("agent_mismatch")
        assert "remote-demo" in info.error and "other-remote" in info.error
        assert captured == {} and promoted == []  # nothing spawned, nothing promoted

    def test_a_local_conversation_is_untouched(self, monkeypatch):
        mgr, captured, _ = self._manager(monkeypatch)
        mgr.continue_conversation("conv1", "more", agent="reviewer", a2a_record=None)
        assert captured["agent"] == "reviewer"
        captured.clear()
        mgr.continue_conversation("conv1", "more", a2a_record=None)
        assert captured["agent"] == ""  # spawn applies its own default

    def test_the_sync_method_reads_no_state_for_the_agent(self, monkeypatch):
        # The record arrives resolved; a state.json read here would be on-loop.
        mgr, captured, _ = self._manager(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.subagent.read_state",
            lambda _id: (_ for _ in ()).throw(AssertionError("read_state on the event loop")),
        )
        monkeypatch.setattr(mgr._sessions, "resumable_sid", lambda _k: "ctx-1")  # no seed read
        mgr.continue_conversation("conv1", "more", a2a_record=None)
        assert "agent" in captured

    @pytest.mark.asyncio
    async def test_the_run_uses_the_admitted_context_and_never_rereads_state(self, monkeypatch):
        """The contextId the continue caller resolved is carried on the run record
        (``_a2a_context``) and the build step uses it as-is. A second read of
        ``state.json`` at build time could disagree with what was admitted, so
        there is none: ``read_state`` is made to raise and the build succeeds.
        Exercised on the REAL rebound manager, where only ``subagent.py``'s
        namespace counts."""
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None)  # type: ignore[arg-type]

        def _no_read(_id):
            raise AssertionError("the run path must not read state.json for the contextId")

        monkeypatch.setattr("kiro_crew.subagent.read_state", _no_read)
        built: dict[str, object] = {}

        def fake_create(entry, context_id=None):
            built["context_id"] = context_id
            p = MagicMock()

            async def _start():
                return None

            p.start = _start
            return p

        monkeypatch.setattr("kiro_crew.agent_sdk.drivers.a2a.create_a2a_provider", fake_create)
        info = SubagentInfo(id="run2", task="more", conversation_key="subagent:conv1")
        setattr(info, "_a2a_context", "ctx-7")
        await mgr._build_a2a_provider(
            info, A2aAgentConfig(name="remote-demo", agent_card_url="https://h/c")
        )
        assert built["context_id"] == "ctx-7"
        # A fresh spawn (no conversation) starts with no contextId, even if a
        # stale value were somehow attached.
        fresh = SubagentInfo(id="run3", task="t")
        setattr(fresh, "_a2a_context", "ctx-stale")
        await mgr._build_a2a_provider(
            fresh, A2aAgentConfig(name="remote-demo", agent_card_url="https://h/c")
        )
        assert built["context_id"] is None

    def test_continue_threads_the_admitted_context_onto_the_run_record(self, monkeypatch):
        """``continue_conversation`` hands the admitted record's contextId to spawn,
        and admission stashes it on the run record beside the routing decision."""
        from kiro_crew.subagent_manager.continuation import RecordedA2A

        mgr, captured, _promoted = self._manager(monkeypatch)
        mgr.continue_conversation(
            "conv1", "more", a2a_record=RecordedA2A(agent="remote-demo", context_id="ctx-9")
        )
        assert captured["_a2a_context"] == "ctx-9"
        # A local conversation (no record) carries an empty value.
        captured.clear()
        mgr.continue_conversation("conv1", "more", a2a_record=None)
        assert captured["_a2a_context"] == ""


class TestOriginCanonicalization:
    """Origins are built from the parsed hostname and port, never the raw netloc, and
    a URL carrying userinfo is refused: ``https://allowed.example:x@evil.example/``
    must not pass an origin allowlist or a pinned-origin comparison for
    ``allowed.example`` while the request goes to ``evil.example``."""

    def test_userinfo_is_refused_everywhere(self):
        from kiro_crew.agent_sdk.drivers import a2a as drv
        from kiro_crew.providers.a2a import _origin

        evil = "https://agents.example:pw@evil.example/.well-known/agent-card.json"
        assert drv.card_origin(evil) == ""
        assert _origin(evil) == ""
        assert drv._normalized_origin("https://agents.example:pw@evil.example") == ""
        # Without userinfo the canonical form is host[:port], lowercased.
        assert drv.card_origin("HTTPS://Agents.Example:8443/x") == "https://agents.example:8443"
        assert _origin("HTTPS://Agents.Example:8443/x") == "https://agents.example:8443"
        assert (
            drv.card_origin("http://[::1]:8790/.well-known/agent-card.json") == "http://[::1]:8790"
        )

    def test_pinned_origin_cannot_be_satisfied_with_userinfo(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import a2a as drv

        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED", "t")
        monkeypatch.setenv("KIROCREW_A2A_REMOTE_CRED_ORIGIN", "https://agents.example")
        with pytest.raises(ValueError, match="pinned to"):
            drv.create_a2a_provider(
                A2aAgentConfig(
                    name="r",
                    agent_card_url="https://agents.example:x@evil.example/.well-known/agent-card.json",
                    auth=A2aAuthConfig(scheme="bearer", token_env="KIROCREW_A2A_REMOTE_CRED"),
                ),
                context_id=None,
            )


class TestPrevalidatedAppSpawnNeverRoutesRemote:
    def test_admission_refuses_prevalidated_name_that_is_also_remote(self):
        import inspect

        from kiro_crew.subagent_manager import admission

        src = inspect.getsource(admission.SpawnAdmissionCoordinator.spawn_impl)
        # The refusal sits between the single resolution and the governance vet,
        # and is typed as a collision so callers act on it like any other.
        assert "if _agent_prevalidated and is_remote:" in src
        assert "error_code=AGENT_NAME_COLLISION_CODE" in src
        assert src.index("if _agent_prevalidated and is_remote:") < src.index("gov_spawn_err = (")
        # The refusal itself is NOT behind the re-entry gate: a stored row that
        # re-enters and classifies as remote must still never route off-host.
        refusal = src[src.index("if _agent_prevalidated and is_remote:") :]
        assert "_gate" not in refusal[: refusal.index("gov_spawn_err = (")]


class TestCredentialNeverFailsOpen:
    def test_empty_credential_at_request_time_refuses(self):
        p = A2AProvider(
            name="r",
            agent_card_url="https://h/.well-known/agent-card.json",
            credentials=lambda: None,  # e.g. the env var vanished after construction
            supported_schemes=frozenset({"bearer"}),
            credential_origin="https://h",
        )
        with pytest.raises(A2AStreamError, match="unauthenticated"):
            p._headers({"A2A-Version": "1.0"})
        # No credentials configured: the base headers go out as-is.
        q = A2AProvider(name="r", agent_card_url="https://h/.well-known/agent-card.json")
        assert q._headers({"A2A-Version": "1.0"}) == {"A2A-Version": "1.0"}
