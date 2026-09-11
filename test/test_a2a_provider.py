"""Unit tests for A2A remote-subagent support (Phase 2).

Covers the four surfaces the POC added to the public core:

* ``A2AProvider.stream`` SSE -> provider-event mapping (mocked stream, no net).
* ``a2a_agents`` registry parse + collision refusal at config load.
* ``_validate_agent`` unioning A2A names into the known set.
* ``_should_use_session_sharing`` excluding A2A registry members.
"""

from __future__ import annotations

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
    def test_collision_with_local_agent_refused(self):
        cfg = KiroCrewConfig()
        cfg.a2a_agents = [
            A2aAgentConfig(name="dup", agent_card_url="http://h/.well-known/agent-card.json")
        ]
        with pytest.raises(ValueError, match="collide"):
            cfg.validate_a2a_collisions({"dup", "other-local"})

    def test_no_collision_passes(self):
        cfg = KiroCrewConfig()
        cfg.a2a_agents = [
            A2aAgentConfig(name="remote", agent_card_url="http://h/.well-known/agent-card.json")
        ]
        # Must not raise.
        cfg.validate_a2a_collisions({"local-a", "local-b"})

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
    """Credentials travel only over TLS (loopback excepted), and a start() that
    fails releases the client session it opened -- nothing else can, because a
    provider that never started is never stashed on the run record."""

    def test_predicate(self):
        from kiro_crew.providers.a2a import _credentials_may_travel as ok

        assert ok("https://agents.example/.well-known/agent-card.json")
        assert ok("http://127.0.0.1:8123/.well-known/agent-card.json")
        assert ok("http://localhost/.well-known/agent-card.json")
        assert ok("http://[::1]:9/.well-known/agent-card.json")
        assert not ok("http://agents.example/.well-known/agent-card.json")
        assert not ok("http://10.0.0.5/.well-known/agent-card.json")
        assert not ok("ftp://agents.example/card")
        assert not ok("")

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
        assert src.index("if _agent_prevalidated and is_remote:") < src.index(
            "gov_spawn_err = _vet_spawn_governance("
        )
