"""CodexHarness, and the three membership sets that decide what codex may do where.

Two halves, and they are separate on purpose.

The first half drives :class:`~kiro_crew.acp.harness.codex.CodexHarness` against a
FAKE ACP peer -- a scripted dict-in/dict-out stand-in for the adapter, no process and
no pipe. What it proves is that the harness's own seam answers, sent in the order a
session is built in, produce an exchange the adapter accepts: one handshake, two
sessions on the ONE peer with distinct ids, prompts routed to the right one, a model
switch that goes down the config-option channel, and a teardown that sends the verb
codex actually has. A real codex-acp cannot stand in for this here: ``session/new``
answers ``-32000 Authentication required`` without an OpenAI login, so a live adapter
can only be taken as far as ``initialize``.

The second half pins the TRANSPORT and the three sets that answer about this host.
codex is served by ``AcpRuntime``; it is in ``ACP_BACKENDS_SESSION_EVICTION`` because
its teardown is a ``session/close`` request that disposes the session (measured),
which is what admits it to the high-churn background path and warm pooled reuse; and
it is IN ``ACP_BACKENDS_SESSION_SHARING``, because the thread ``codex`` persists
outlives that close and a ``session/load`` restores it. Two of those three read the
same teardown verb and reach different conclusions from it, which is exactly why each
is its own table -- the eviction claim is about the live session leaving the process,
the sharing claim is about the record staying behind.

The seam-level contract every harness answers is in
``test_acp_harness_contract.py``; codex is parametrised into it. What is here is what
is true of codex and of nothing else.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import (
    CLIENT_NAME,
    CLIENT_VERSION,
    AcpError,
    AcpToolGateUnroutable,
)
from kiro_crew.acp.harness import codex as harness_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext, TeardownPolicy
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.runtime import AcpRuntime, _MirroredSessionMcp
from kiro_crew.acp.session_handle import (
    AcpSessionHandle,
    advertised_models_from_session,
    models_from_config_options,
    parse_advertised_models,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    METHOD_CANCEL,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_NEW,
    METHOD_SESSION_UPDATE,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_SESSION_EVICTION,
    effort_config_option_id,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.mirrors.registry import has_mirror

# ---------------------------------------------------------------------------
# The fake peer
# ---------------------------------------------------------------------------


class FakeCodexPeer:
    """A scripted stand-in for one codex-acp process hosting N sessions.

    Deliberately not a mock: it holds the two behaviours the harness's answers are
    judged against, and a mock would assert only that a call was made rather than
    that the peer could answer it.

    * ``sessions`` is a map, like the adapter's own ``this.sessions``, so "two
      session/new on one process" is observable as two entries rather than two calls.
    * ``mcpCapabilities`` advertises ``http`` only, which is what the real adapter
      advertises, and ``session/new`` ACCEPTS an ``sse`` element anyway -- recording
      it in the session verbatim. That is the measured behaviour and it is the
      reason the narrowing exists: the adapter raises nothing, so an unnarrowed
      element becomes a server that is silently never wired.

    Every frame is round-tripped through ``json.dumps``/``loads`` so a payload that
    is not JSON-serialisable fails here rather than at a real pipe.
    """

    MCP_CAPABILITIES = {"http": True, "sse": False, "acp": False}

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.config_options: list[tuple[str, str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.initialized = False
        self._next_id = 0
        #: Model values this peer refuses, answered as a bare ``-32602`` with no
        #: detail -- the shape a real codex session draws for a stale pin.
        self.rejected_models: set[str] = set()

    # ── wire ──

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = json.loads(json.dumps(params))
        self.requests.append((method, params))
        handler = {
            "initialize": self._initialize,
            "session/new": self._session_new,
            "session/prompt": self._session_prompt,
            "session/set_config_option": self._set_config_option,
            "session/close": self._session_close,
        }.get(method)
        if handler is None:
            raise AssertionError(f"codex-acp has no {method!r}: it would answer -32601")
        return handler(params)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        params = json.loads(json.dumps(params))
        if method == METHOD_SESSION_CLOSE:
            # Measured: the real adapter IGNORES a close sent as a notification --
            # the session stays addressable exactly as under ``cancel``. The peer
            # refuses it outright so the regression fails loudly here instead of
            # passing while leaking on the wire.
            raise AssertionError(
                "session/close sent as a NOTIFICATION: the adapter ignores it and the "
                "session stays resident -- it must be an awaited request"
            )
        if method != METHOD_CANCEL:
            raise AssertionError(f"unexpected notification {method!r}")
        self.notifications.append((method, params))

    # ── handlers ──

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        assert params["protocolVersion"] == 1, "codex-acp speaks numeric ACP v1"
        assert params["clientInfo"]["name"], "a flat clientName is not read"
        self.initialized = True
        return {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "mcpCapabilities": dict(self.MCP_CAPABILITIES),
            },
        }

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self.initialized, "session/new before initialize"
        assert params["cwd"], "codex-acp requires a cwd"
        assert "_meta" not in params, "codex-acp reads none of Crew's _meta"
        # No validation against the advertised transports, deliberately: the real
        # adapter accepts an element it declared unsupported and answers normally.
        # Modelling a refusal here would make the narrowing look like
        # error-avoidance and let a test pass by catching an error nothing raises.
        self._next_id += 1
        sid = f"thread-{self._next_id}"
        self.sessions[sid] = {"cwd": params["cwd"], "mcpServers": params.get("mcpServers") or []}
        return {"sessionId": sid, "modes": {"currentModeId": "agent"}}

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params["sessionId"]
        assert sid in self.sessions, f"prompt for unknown session {sid!r}"
        self.sessions[sid].setdefault("prompts", []).append(params["prompt"])
        return {"stopReason": "end_turn"}

    def _set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params["sessionId"]
        assert sid in self.sessions, f"config option for unknown session {sid!r}"
        option, value = params["optionId"], params["value"]
        if option == "model" and value in self.rejected_models:
            raise CodexRpcError(-32602, "Invalid params")
        self.config_options.append((sid, option, value))
        self.sessions[sid][option] = value
        return {}

    def _session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """What codex-acp does with ``session/close``, as measured live.

        Evicts: the id leaves the map, so the liveness oracle (set_config_option on
        the same id) fails afterwards. Idempotent: a second close on the same id, or
        a close on an id never opened, answers ``{}`` again rather than erroring.
        The Codex thread's own record is untouched (evict, not delete).
        """
        self.sessions.pop(params["sessionId"], None)
        return {}


class CodexRpcError(Exception):
    """A JSON-RPC error frame the peer answered with."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code


def _initialize_params(adapter: CodexHarness) -> dict[str, Any]:
    """The handshake, built from the harness's own seam answers.

    Assembled here rather than on the harness because the runtime owns
    ``clientInfo``: what a harness decides is the version and the capabilities.
    ``CLIENT_NAME``/``CLIENT_VERSION`` are imported rather than spelled out so
    this fixture keeps matching the real handshake; the version is the package's
    and moves every release.
    """
    return {
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        "protocolVersion": adapter.protocol_version,
        "clientCapabilities": adapter.client_capabilities,
    }


def _open_session(
    peer: FakeCodexPeer, *, cwd: str, mcp_servers: list[Any], adapter: CodexHarness | None = None
) -> str:
    """Build one session the way the harness's answers say to, and return its id."""
    adapter = adapter or CodexHarness()
    init = peer.request("initialize", _initialize_params(adapter))
    narrowed = adapter.session_mcp_servers(
        list(mcp_servers), agent_capabilities=init.get("agentCapabilities") or {}
    )
    resp = peer.request("session/new", {"cwd": cwd, "mcpServers": narrowed})
    return resp["sessionId"]


@pytest.fixture()
def adapter() -> CodexHarness:
    return CodexHarness()


def _ctx(**over: Any) -> SpawnContext:
    """A spawn context with every field the harness reads, overridable per test."""
    base: dict[str, Any] = {
        "agent": "kirocrew",
        "work_dir": "/w",
        "model": "gpt-5-codex",
        "environ": {},
        "home": Path("/h"),
        "sandbox_mode": "standard",
    }
    base.update(over)
    return SpawnContext(**base)


# ---------------------------------------------------------------------------
# Half one: the harness against the fake peer
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_harness_for_codex_returns_this_harness(self):
        resolved = harness_for(ACP_BACKEND_CODEX)
        assert isinstance(resolved, CodexHarness)
        assert resolved.backend == ACP_BACKEND_CODEX

    def test_registration_is_a_separate_question_from_routing(self):
        """Two different questions, and conflating them breaks both answers.

        The registry answers "can the shared-process runtime drive this host?";
        ``ACP_BACKENDS_ACP_RUNTIME`` answers "does a session take that path?". They
        agree for codex, and they are still separate: a harness is written and
        tested before it is routed, so a registry gated on the set would make one
        unreachable to its own tests.
        """
        assert isinstance(harness_for(ACP_BACKEND_CODEX), CodexHarness)
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME


class TestHandshake:
    def test_initialize_is_accepted_and_answers_the_transports(self, adapter):
        peer = FakeCodexPeer()
        init = peer.request("initialize", _initialize_params(adapter))
        assert peer.initialized
        assert init["agentCapabilities"]["mcpCapabilities"] == FakeCodexPeer.MCP_CAPABILITIES

    def test_the_protocol_version_is_this_harness_own_literal(self, adapter):
        """A handshake collapsed to what every harness accepts downgrades one of them.

        Pinned as a value rather than as an import equality, so folding codex's
        version into another harness's constant fails here even when the two integers
        happen to agree.
        """
        assert adapter.protocol_version == 1
        assert harness_mod.PROTOCOL_VERSION_CODEX == 1

    def test_the_version_is_an_integer_not_a_date_string(self, adapter):
        """The TYPE is part of the contract: kiro-cli's date spelling is rejected."""
        assert isinstance(adapter.protocol_version, int)
        assert not isinstance(adapter.protocol_version, str)

    def test_a_junk_handshake_narrows_nothing_rather_than_crashing(self, adapter):
        """A shape the adapter never promised must not cost the session its tools."""
        requested = [{"name": "a", "type": "sse"}, {"name": "b", "url": "http://b"}]
        for junk in (
            {},
            {"mcpCapabilities": None},
            {"mcpCapabilities": {}},
            {"mcpCapabilities": 1},
        ):
            assert adapter.session_mcp_servers(requested, agent_capabilities=junk) is requested


class TestTwoSessionsOnOneProcess:
    """The point of the harness: N sessions, one adapter."""

    def test_two_session_new_yield_distinct_ids_on_one_peer(self):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        assert first != second
        assert set(peer.sessions) == {first, second}
        assert peer.sessions[first]["cwd"] == "/w/one"
        assert peer.sessions[second]["cwd"] == "/w/two"

    def test_a_prompt_reaches_only_its_own_session(self):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        peer.request("session/prompt", {"sessionId": first, "prompt": [{"text": "one"}]})
        peer.request("session/prompt", {"sessionId": second, "prompt": [{"text": "two"}]})
        peer.request("session/prompt", {"sessionId": first, "prompt": [{"text": "three"}]})
        assert [p[0]["text"] for p in peer.sessions[first]["prompts"]] == ["one", "three"]
        assert [p[0]["text"] for p in peer.sessions[second]["prompts"]] == ["two"]

    def test_each_session_carries_its_own_mcp_array(self):
        """codex-acp reads no agent spec, so the array is the session's whole surface."""
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[{"name": "a", "url": "http://a"}])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        assert [s["name"] for s in peer.sessions[first]["mcpServers"]] == ["a"]
        assert peer.sessions[second]["mcpServers"] == []


class TestMcpArrayNarrowing:
    def test_an_sse_element_is_dropped_before_it_reaches_the_wire(self):
        peer = FakeCodexPeer()
        sid = _open_session(
            peer,
            cwd="/w",
            mcp_servers=[
                {"name": "keep", "url": "http://keep"},
                {"name": "drop", "type": "sse", "url": "http://drop"},
            ],
        )
        assert [s["name"] for s in peer.sessions[sid]["mcpServers"]] == ["keep"]

    def test_an_unnarrowed_sse_element_is_accepted_and_silently_unwired(self, adapter):
        """Why the narrowing is not cosmetic: nothing else catches this.

        The adapter advertises ``sse: false`` and then accepts an ``sse`` element,
        answering ``session/new`` normally. So the cost of skipping the narrowing is
        not a failed request anyone can see -- it is a session that runs with a
        server the adapter never wired, which no downstream check can detect.

        Sent deliberately unnarrowed, so this fails if the peer ever starts
        modelling a refusal and the test above begins passing for the wrong reason.
        """
        peer = FakeCodexPeer()
        peer.request("initialize", _initialize_params(adapter))
        result = peer.request(
            "session/new",
            {
                "cwd": "/w",
                "mcpServers": [
                    {"name": "keep", "url": "http://keep"},
                    {"name": "drop", "type": "sse", "url": "http://drop"},
                ],
            },
        )
        sid = result["sessionId"]
        assert [s["name"] for s in peer.sessions[sid]["mcpServers"]] == ["keep", "drop"]

    def test_a_narrowed_list_is_not_aliased(self, adapter):
        """A session must not be able to mutate the caller's list after the fact."""
        source: list[Any] = [{"name": "a", "url": "http://a"}]
        narrowed = adapter.session_mcp_servers(
            source, agent_capabilities={"mcpCapabilities": {"http": True}}
        )
        source.append({"name": "b", "url": "http://b"})
        assert [s["name"] for s in narrowed] == ["a"]


class TestSessionExtras:
    @pytest.mark.asyncio
    async def test_extras_are_empty_and_carry_no_custom_agents(self, adapter):
        """``custom_agents`` is a kiro-family field; codex has no such channel."""
        extras = await adapter.session_extras("kirocrew", work_dir="/w")
        assert extras.custom_agents is None

    @pytest.mark.asyncio
    async def test_extras_stay_empty_whatever_is_passed(self, adapter):
        for kwargs in (
            {"work_dir": None},
            {"work_dir": "/w", "member_dispatch": True},
            {"work_dir": "/w", "mcp_gateway_overlay": object()},
            {"work_dir": "/w", "session_key": "subagent:worker"},
        ):
            assert (await adapter.session_extras("a", **kwargs)).custom_agents is None

    def test_no_session_file_is_named_on_load(self, adapter):
        """The adapter locates the session from its id; a path it cannot read is worse."""
        assert adapter.wants_session_file_on_load is False


class TestModelAndEffortSwitch:
    def test_a_model_write_is_accepted_per_session(self):
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        peer.request(
            "session/set_config_option",
            {"sessionId": sid, "optionId": "model", "value": "gpt-5-codex"},
        )
        assert peer.config_options == [(sid, "model", "gpt-5-codex")]

    def test_there_is_no_session_set_model_to_send(self):
        """The request the CONFIG_OPTION answer keeps Crew from sending."""
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        with pytest.raises(AssertionError, match="session/set_model"):
            peer.request("session/set_model", {"sessionId": sid, "modelId": "gpt-5-codex"})

    def test_a_refused_value_is_a_bare_invalid_params(self):
        """The frame that must not be read as a protocol failure.

        Read as one, the session init failed and a stale model pin from another
        backend killed every codex session at startup.
        """
        peer = FakeCodexPeer()
        peer.rejected_models = {"kiro-default"}
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        with pytest.raises(CodexRpcError) as exc:
            peer.request(
                "session/set_config_option",
                {"sessionId": sid, "optionId": "model", "value": "kiro-default"},
            )
        assert exc.value.code == -32602
        assert str(exc.value).endswith("Invalid params")

    def test_effort_is_its_own_write(self):
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        peer.request(
            "session/set_config_option", {"sessionId": sid, "optionId": "effort", "value": "high"}
        )
        assert (sid, "effort", "high") in peer.config_options


class TestPermissionRouting:
    """The routing ANSWER lives in ``ACP_BACKEND_ROUTING``, which both drivers read.

    The harness declares no routing member, so what is left to prove here is the
    wire half: the option that table names is accepted on every session of one
    shared process, not just the first.
    """

    def test_the_routing_option_is_accepted_on_every_session_of_the_process(self):
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        option, value = ("mode", "read-only")
        for sid in (first, second):
            peer.request(
                "session/set_config_option",
                {"sessionId": sid, "optionId": option, "value": value},
            )
        assert peer.sessions[first]["mode"] == "read-only"
        assert peer.sessions[second]["mode"] == "read-only"


class TestTeardown:
    def test_the_verb_is_the_standard_session_close(self, adapter):
        assert adapter.teardown.method == METHOD_SESSION_CLOSE
        assert adapter.teardown.method == "session/close"

    def test_the_verb_is_not_cancel(self, adapter):
        """``cancel`` interrupts a turn and leaves the session resident (measured);
        a harness that sends it as its teardown leaks one session per teardown."""
        assert adapter.teardown.method != METHOD_CANCEL

    def test_no_kiro_family_delete_verb_is_sent(self, adapter):
        """Either kiro-family verb would draw -32601 and leave the session on the process."""
        assert adapter.teardown.method != "_kiro.dev/session/terminate"
        assert adapter.teardown.method != "_kiro/session/delete"

    def test_the_policy_carries_the_verb_and_its_delivery(self, adapter):
        """codex evicts but does not delete: the adapter drops the session and keeps
        the Codex thread's record, the same shape as kiro-cli's ``terminate``.

        The policy carries the DELIVERY beside the verb because the two are not
        separable facts, and here the wrong delivery is silent: measured live, a
        ``session/close`` sent as a notification is ignored and the session stays
        resident. ``notification=False`` is the load-bearing half.
        """
        assert adapter.teardown == TeardownPolicy(method=METHOD_SESSION_CLOSE, notification=False)

    def test_teardown_evicts_one_session_and_leaves_the_other_running(self, adapter):
        """The property the eviction set is built on, against the peer's model of
        the measured adapter: after close the id is gone, its sibling is untouched."""
        peer = FakeCodexPeer()
        first = _open_session(peer, cwd="/w/one", mcp_servers=[])
        second = _open_session(peer, cwd="/w/two", mcp_servers=[])
        assert peer.request(adapter.teardown.method, {"sessionId": first}) == {}
        assert first not in peer.sessions, "close must evict the session"
        with pytest.raises(AssertionError, match="unknown session"):
            peer.request(
                "session/set_config_option",
                {"sessionId": first, "optionId": "mode", "value": "read-only"},
            )
        peer.request("session/prompt", {"sessionId": second, "prompt": [{"text": "still here"}]})

    def test_teardown_is_idempotent(self, adapter):
        """A double-terminate (runtime cleanup racing a caller's own destroy) must
        not fault; measured: the adapter answers ``{}`` for an already-closed id."""
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w/one", mcp_servers=[])
        assert peer.request(adapter.teardown.method, {"sessionId": sid}) == {}
        assert peer.request(adapter.teardown.method, {"sessionId": sid}) == {}
        assert peer.request(adapter.teardown.method, {"sessionId": "never-opened"}) == {}


class TestTeardownAwaitsTheReplyTheHostGives:
    """The teardown DELIVERY, pinned against what the adapter actually does.

    ``session/close`` is a request codex-acp answers with ``{}``. Sent as a notification
    the adapter ignores it and the session stays resident -- silently, with nothing
    logged. So the runtime must AWAIT it, on the same bounded path the kiro family's
    verbs take, and a notification here is the leak this PR exists to close.
    """

    @pytest.mark.asyncio
    async def test_codex_teardown_is_an_awaited_request_not_a_notification(self):
        rt = _codex_runtime()
        rt._session_queues["sid-codex"] = asyncio.Queue()
        awaited: list[tuple[str, dict[str, Any]]] = []
        notified: list[tuple[str, dict[str, Any]]] = []

        async def _send_and_await(method, params, timeout=None):
            awaited.append((method, params))
            return {}

        async def _notify(method, params):
            notified.append((method, params))

        with ExitStack() as stack:
            stack.enter_context(patch.object(rt, "_send_and_await", _send_and_await))
            stack.enter_context(patch.object(rt, "send_notification", _notify))
            await rt.terminate_session("sid-codex")

        assert awaited == [(METHOD_SESSION_CLOSE, {"sessionId": "sid-codex"})]
        assert notified == [], "a notification-form close is ignored by the adapter"
        assert "sid-codex" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_the_await_is_bounded_by_the_teardown_budget(self):
        """The reply is awaited, but never past ``_TERMINATE_TIMEOUT``: a stalled
        adapter costs one budget, then the local unregister still runs."""
        from kiro_crew.acp.runtime import _TERMINATE_TIMEOUT

        rt = _codex_runtime()
        rt._session_queues["sid-codex"] = asyncio.Queue()
        seen_timeout: list[Any] = []

        async def _send_and_await(method, params, timeout=None):
            seen_timeout.append(timeout)
            raise AcpError(f"Request {method} timed out")

        with patch.object(rt, "_send_and_await", _send_and_await):
            await rt.terminate_session("sid-codex")  # must NOT raise

        assert seen_timeout == [_TERMINATE_TIMEOUT]
        assert "sid-codex" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_the_kiro_family_still_awaits_its_teardown(self):
        """The other half: kiro and KAS answer their verb, so the reply is still awaited.

        Without this the fix could have been "never await a teardown", which would
        drop the only signal that a kiro session actually left the shared process.
        """
        for backend, verb in (
            (ACP_BACKEND_KIRO, "_kiro.dev/session/terminate"),
            (ACP_BACKEND_KAS, "_kiro/session/delete"),
        ):
            rt = _runtime_for(backend, pid=4243)
            rt._session_queues["sid"] = asyncio.Queue()
            awaited: list[str] = []
            notified: list[str] = []

            async def _send_and_await(method, params, timeout=None, _a=awaited):
                _a.append(method)
                return {}

            async def _notify(method, params, _n=notified):
                _n.append(method)

            with ExitStack() as stack:
                stack.enter_context(patch.object(rt, "_send_and_await", _send_and_await))
                stack.enter_context(patch.object(rt, "send_notification", _notify))
                await rt.terminate_session("sid")

            assert awaited == [verb], f"{backend or 'kiro'} must still await its teardown"
            assert notified == []
            assert "sid" not in rt._session_queues

    def test_every_known_harness_declares_its_teardown_delivery(self):
        """A new harness cannot inherit the awaited shape by saying nothing.

        ``TeardownPolicy.notification`` has no default, so this asserts the field is
        answered rather than that it holds a particular value -- the value is the
        harness's own to state.
        """
        for backend in sorted(ACP_BACKENDS_ACP_RUNTIME | {ACP_BACKEND_CODEX}):
            policy = harness_for(backend).teardown
            assert isinstance(policy.notification, bool), backend
            assert policy.method


class TestWhatThisHarnessDoesNotHave:
    def test_no_inbound_request_is_claimed(self, adapter):
        assert adapter.host_answered_methods == ()

    @pytest.mark.asyncio
    async def test_answering_one_raises_rather_than_returning_an_empty_result(self, adapter):
        """An empty result would answer a frame this harness never claimed."""
        with pytest.raises(NotImplementedError, match="_kiro/auth/getAccessToken"):
            await adapter.answer_request("_kiro/auth/getAccessToken")

    def test_only_the_standard_session_update_spelling_is_accepted(self, adapter):
        aliases = adapter.notification_aliases
        assert aliases.session_update == (METHOD_SESSION_UPDATE,)
        assert aliases.subagent_list_update == ""
        assert aliases.mcp_init == ()

    def test_the_kiro_family_vocabulary_is_not_inherited(self, adapter):
        """KAS speaks ``_kiro.dev/*`` because it is reached THROUGH kiro-cli. codex is not."""
        from kiro_crew.acp.harness._common import KIRO_FAMILY_ALIASES

        assert adapter.notification_aliases != KIRO_FAMILY_ALIASES

    def test_the_peer_raises_nothing_at_crew_during_a_session(self, adapter):
        peer = FakeCodexPeer()
        sid = _open_session(peer, cwd="/w", mcp_servers=[])
        peer.request("session/prompt", {"sessionId": sid, "prompt": [{"text": "hi"}]})
        # Teardown is a request the adapter answers, delivered on the request seam.
        assert peer.request(adapter.teardown.method, {"sessionId": sid}) == {}
        assert [m for m, _ in peer.requests] == [
            "initialize",
            "session/new",
            "session/prompt",
            "session/close",
        ]
        assert peer.notifications == []


@pytest.fixture()
def mask_resolved(monkeypatch):
    """Stub the credential mask so an argv test does not touch the real filesystem.

    The mask's own behaviour is asserted in ``TestSpawnMasks``; here it only has to
    not run a sandbox probe.
    """

    async def _preflight(_fn, _backend, _mode):
        return ("/h/.aws",)

    from kiro_crew.acp import client as client_mod

    monkeypatch.setattr(client_mod, "_run_preflight_bounded", _preflight)
    monkeypatch.setattr(
        harness_mod.acp_tool_gate, "adapter_expose_files", lambda b, h: ("/h/.aws/config",)
    )


class TestSpawn:
    @pytest.mark.asyncio
    async def test_the_resolved_entry_is_the_whole_argv(self, adapter, mask_resolved):
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/index.js"], "/s")
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert plan.argv == ["/n/node", "/p/index.js"]

    @pytest.mark.asyncio
    async def test_no_agent_and_no_model_flag_is_appended(self, adapter, mask_resolved):
        """codex takes no argv of its own: any invocation blocks on stdin."""
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/index.js"], "/s")
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert "--agent" not in plan.argv
        assert "--model" not in plan.argv
        assert "kirocrew" not in plan.argv
        assert "gpt-5-codex" not in plan.argv

    @pytest.mark.asyncio
    async def test_crew_never_owns_this_host_credential(self, adapter, mask_resolved):
        """codex raises nothing at Crew, so a process expecting a callback would wait."""
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/index.js"], "/s")
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert plan.host_auth is False

    @pytest.mark.asyncio
    async def test_a_missing_adapter_aborts_the_spawn_with_the_searched_path(self, adapter):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        with patch.object(client_mod, "_resolve_codex_acp_bin", return_value=(None, "/one:/two")):
            with pytest.raises(AcpRuntimeError) as exc:
                await adapter.resolve_spawn(_ctx())
        message = str(exc.value)
        assert "codex-acp" in message
        assert "@agentclientprotocol/codex-acp" in message
        assert "CODEX_ACP_BIN" in message
        assert "/one" in message and "/two" in message

    @pytest.mark.asyncio
    async def test_the_resolver_is_delegated_not_duplicated(self, adapter, mask_resolved):
        """One resolution order, so "why did it pick that one?" has one answer."""
        from kiro_crew.acp import client as client_mod

        with patch.object(
            client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
        ) as resolver:
            await adapter.resolve_spawn(_ctx())
        assert resolver.call_count == 1

    def test_the_kiro_api_key_is_taken_out_of_the_child_environment(self, adapter):
        """A foreign adapter must never receive it. Same as the AcpClient path does."""
        env = {"KIRO_API_KEY": "secret", "PATH": "/usr/bin"}
        adapter.apply_spawn_env(env)
        assert "KIRO_API_KEY" not in env
        assert env["PATH"] == "/usr/bin"

    def test_codex_path_is_left_exactly_as_the_operator_set_it(self, adapter):
        env = {"CODEX_PATH": "/opt/my-codex"}
        adapter.apply_spawn_env(env)
        assert env["CODEX_PATH"] == "/opt/my-codex"

    def test_crew_own_sandbox_is_the_only_confinement(self, adapter):
        """A Node adapter carries no OS sandbox for Crew's to nest inside or defer to."""
        assert adapter.internal_sandbox is False

    def test_no_pod_home_remap(self, adapter):
        assert adapter.pod_home_remap is False

    def test_nothing_was_selected_at_spawn_for_a_later_check_to_confirm(self, adapter):
        assert adapter.verifies_agent_activation is False


class TestSpawnMasks:
    """The refusal that must happen BEFORE the process exists."""

    @pytest.mark.asyncio
    async def test_the_mask_is_resolved_and_the_carve_out_is_projected_over_it(self):
        from kiro_crew.acp import client as client_mod

        async def _preflight(_fn, backend, mode):
            assert (backend, mode) == (ACP_BACKEND_CODEX, "standard")
            return ("/h/.aws",)

        with (
            patch.object(client_mod, "_run_preflight_bounded", new=_preflight),
            patch.object(
                harness_mod.acp_tool_gate, "adapter_expose_files", return_value=("/h/.aws/config",)
            ) as expose,
        ):
            hidden, exposed = await harness_mod.resolve_spawn_masks("standard")
        assert hidden == ("/h/.aws",)
        assert exposed == ("/h/.aws/config",)
        # Projected over the mask just resolved, never a re-derived one -- re-deriving
        # puts a filesystem read back on the event loop.
        assert expose.call_args.args == (ACP_BACKEND_CODEX, ("/h/.aws",))

    @pytest.mark.asyncio
    async def test_a_refused_preflight_stops_the_spawn_rather_than_returning_empty(self):
        """An enforced adapter started with its mask dropped has no control at all."""
        from kiro_crew.acp import client as client_mod

        async def _refuse(*_args, **_kwargs):
            raise RuntimeError("sandbox floor refused")

        with patch.object(client_mod, "_run_preflight_bounded", new=_refuse):
            with pytest.raises(RuntimeError, match="sandbox floor refused"):
                await harness_mod.resolve_spawn_masks("off")

    @pytest.mark.asyncio
    async def test_the_spawn_refuses_a_tier_that_would_drop_the_mask(self):
        """The refusal reaches the SPAWN, not just the helper.

        An enforced host that spawned on `off` would run a third-party binary with
        the operator's credential homes readable and nothing compensating for it, so
        the whole spawn has to fail rather than return an empty mask.
        """
        from kiro_crew.acp import client as client_mod

        async def _refuse(*_args, **_kwargs):
            raise RuntimeError("sandbox floor refused")

        with (
            patch.object(
                client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
            ),
            patch.object(client_mod, "_run_preflight_bounded", new=_refuse),
        ):
            with pytest.raises(RuntimeError, match="sandbox floor refused"):
                await CodexHarness().resolve_spawn(_ctx(sandbox_mode="off"))

    @pytest.mark.asyncio
    async def test_the_spawn_hands_the_configured_tier_to_the_refusal(self):
        """The tier comes off the context, never re-read from config.

        Re-reading it would resolve a different tier than the one the argv was built
        for, which is the mismatch carrying the mask on the plan exists to prevent.
        """
        from kiro_crew.acp import client as client_mod

        seen: list[str] = []

        async def _preflight(_fn, backend, mode):
            seen.append(mode)
            return ("/h/.aws",)

        with (
            patch.object(
                client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
            ),
            patch.object(client_mod, "_run_preflight_bounded", new=_preflight),
            patch.object(
                harness_mod.acp_tool_gate, "adapter_expose_files", return_value=("/h/.aws/config",)
            ),
        ):
            plan = await CodexHarness().resolve_spawn(_ctx(sandbox_mode="strict"))
        assert seen == ["strict"]
        assert plan.extra_hidden_dirs == ("/h/.aws",)

    def test_resolve_spawn_resolves_the_mask_with_the_tier(self):
        """No mask-without-a-tier path is left behind for a caller to reach for."""
        import inspect

        source = inspect.getsource(CodexHarness.resolve_spawn)
        assert "resolve_spawn_masks(ctx.sandbox_mode)" in source
        assert "extra_hidden_dirs=" in source
        assert not hasattr(harness_mod, "resolve_mask_only")


class TestReclaimIsScopedToTheCore:
    """The ceiling and the scope are one decision, so they are pinned together."""

    def test_the_ceiling_is_the_measured_core_number_not_the_operators(self, adapter):
        """A subtree budget cannot be applied to two processes.

        ``rss_depth`` changes what is being counted, so the incoming ``max_rss_mb``
        describes a quantity this policy does not measure. Pinned as a literal so
        raising or lowering the ceiling is a visible edit.
        """
        policy = adapter.reclaim_policy(max_age_secs=3600.0, max_rss_mb=500.0)
        assert policy.max_rss_mb == 1024.0

    @pytest.mark.asyncio
    async def test_the_spawn_scope_includes_a_resident_launcher(self, adapter):
        """The pid-relative depth reaches app-server through a resident launcher."""
        from kiro_crew.acp import client as client_mod

        with (
            patch.object(
                client_mod, "_resolve_codex_acp_bin", return_value=(["/n/node", "/p/i.js"], "/s")
            ),
            patch.object(harness_mod, "resolve_spawn_masks", new=AsyncMock(return_value=((), ()))),
            patch.object(harness_mod, "_sandbox_wrapper_generations", return_value=1),
        ):
            plan = await adapter.resolve_spawn(_ctx())
        assert plan.rss_depth == 2

    def test_age_still_passes_straight_through(self, adapter):
        """Nothing about age changes with the measurement scope."""
        policy = adapter.reclaim_policy(max_age_secs=1234.0, max_rss_mb=500.0)
        assert policy.max_age_secs == 1234.0

    def test_the_ceiling_clears_the_measured_core_with_headroom(self, adapter):
        """522 MB is the core at eight sessions in the worse of the two runs.

        A ceiling at or under it would recycle a process that is merely busy, which
        is the failure the default 500 produces. Asserted as an inequality against
        the measurement rather than a second copy of the constant, so the two cannot
        drift into agreeing with each other and disagreeing with the probe.
        """
        policy = adapter.reclaim_policy(max_age_secs=3600.0, max_rss_mb=500.0)
        assert policy.max_rss_mb > 522.0


# ---------------------------------------------------------------------------
# Half two: the transport, and the three sets that read one fact differently
# ---------------------------------------------------------------------------


def _build_provider(backend: str) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


class TestCodexIsServedByTheSharedRuntime:
    def test_the_provider_reads_as_a_runtime_backend(self):
        assert _build_provider(ACP_BACKEND_CODEX).is_acp_runtime_backend is True

    def test_the_shipped_set_names_it(self):
        """Pinned as a literal set, so gaining or losing a member is a visible edit."""
        assert ACP_BACKENDS_ACP_RUNTIME == frozenset(
            {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX}
        )

    def test_the_other_harnesses_keep_their_answers(self):
        assert _build_provider(ACP_BACKEND_KIRO).is_acp_runtime_backend is True
        assert _build_provider(ACP_BACKEND_KAS).is_acp_runtime_backend is True
        assert _build_provider(ACP_BACKEND_CLAUDE).is_acp_runtime_backend is False

    def test_membership_is_read_from_the_set_and_not_inferred(self):
        """The property must not be spelled as "everything that is not claude".

        With three members that inference happens to agree, which is exactly why it
        is worth pinning: a harness added later would inherit the shared-runtime
        path without anyone deciding it should.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(AcpProvider.is_acp_runtime_backend.fget))
        )
        # The docstring names the spelling it rejects, so strip it: a substring scan
        # over the whole source passes or fails on the prose rather than the code.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.body and isinstance(node.body[0], ast.Expr):
                    if isinstance(node.body[0].value, ast.Constant):
                        node.body = node.body[1:]
        body = ast.unparse(tree)
        assert "ACP_BACKENDS_ACP_RUNTIME" in body
        assert "is_claude_backend" not in body

    def test_running_here_does_not_hand_it_the_kiro_family_conventions(self):
        """The transport set is not the cli.json set, and codex proves it.

        codex reads no ``cli.json`` overlay -- it takes effort over
        ``session/set_config_option`` -- so a site that keyed overlay work off the
        transport would write a file this host ignores and seed its effort map from
        another harness's copy.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_SLASH_COMMANDS


class TestOneFactReadThreeWays:
    """The teardown Crew sends codex disposes the session. Three sets act on that."""

    def test_it_shares_sessions_on_the_persisted_thread_not_a_resident_one(self, adapter):
        """In, and pinned so the REASON cannot be misread as a resident session.

        Three facts together. One adapter process serves N sessions, so a subagent
        can have one on its parent's runtime. The teardown still EVICTS -- a shared
        subagent session is closed like any other, because one left resident would
        hold its own MCP fleet on a runtime nobody is using. And the thread codex
        persisted survives that close, so ``spawn_continue`` reaches the conversation
        with a ``session/load`` rather than by finding the session still there --
        which is why membership in ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS`` is the
        companion claim, asserted here so a reader cannot conclude that sharing means
        keeping.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_SHARING
        assert _build_provider(ACP_BACKEND_CODEX).is_session_sharing_eligible is True
        # Membership does NOT soften the teardown: the verb is the disposing one.
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_EVICTION
        assert adapter.teardown == TeardownPolicy(method=METHOD_SESSION_CLOSE, notification=False)
        # And the restore is resolved from the sessionId alone, with no Crew-side
        # transcript -- the property that makes a closed session continuable.
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_HARNESS_OWNED_SESSIONS

    def test_running_on_the_shared_runtime_does_not_by_itself_grant_sharing(self):
        """The superset relation must not be read as an implication.

        codex belongs to both sets, so KAS is the member that separates them, and it
        is asserted here rather than left to the sharing set's comment: a harness runs
        on the runtime and is still refused sharing when its teardown removes the
        record a continuation would load.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_SHARING
        assert ACP_BACKEND_KAS in ACP_BACKENDS_ACP_RUNTIME
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_SESSION_SHARING
        assert ACP_BACKENDS_SESSION_SHARING < ACP_BACKENDS_ACP_RUNTIME

    def test_it_is_in_the_eviction_set_because_its_teardown_evicts(self):
        """Eviction is the harness's own property, and it is unchanged by sharing.

        ``session/close`` evicts -- measured -- so codex is a member, and joining the
        sharing set did not make the verb gentler. A reader who assumes a shared
        subagent must stay resident would drop codex from here; the two memberships
        standing together is what says otherwise.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_EVICTION
        assert ACP_BACKENDS_SESSION_EVICTION == frozenset(
            {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX}
        )

    def test_the_background_path_is_now_reachable(self):
        """Background handles are the high-churn ones -- title generation,
        suggestions, folders, nav, each taking an ephemeral sessionId many times per
        conversation. They are safe on this host exactly because every one of them
        is evicted on teardown; the eviction term in ``_bg_runtime_backends`` is what
        admits it, and would exclude it again the day the verb stopped evicting."""
        from kiro_crew.session import _bg_runtime_backends

        assert ACP_BACKEND_CODEX in _bg_runtime_backends()

    def test_the_background_path_admits_it_by_eviction_not_by_name(self):
        """Membership in the eviction set must be what lets it in, so the reason is
        visible and the next non-evicting harness is not admitted by a name check
        that happens to read the same from the outside."""
        import inspect

        from kiro_crew import session as session_mod

        source = inspect.getsource(session_mod._bg_runtime_backends)
        assert "ACP_BACKENDS_SESSION_EVICTION" in source
        assert "CODEX" not in source.upper()

    def test_the_teardown_verb_is_the_one_the_sets_are_reasoning_about(self):
        """Pinned here so the sets above cannot be justified by a verb that changed."""
        adapter = harness_for(ACP_BACKEND_CODEX)
        assert adapter.teardown.method == METHOD_SESSION_CLOSE
        assert adapter.teardown.notification is False


class TestResumeNeedsNoLocalTranscript:
    def test_a_codex_resume_is_attempted_without_a_local_file(self):
        """codex keeps its own session records, so there is no file to pre-check.

        A resume path that stats ``<kiro home>/sessions/cli/<sid>.json`` can never
        say yes for codex -- that file is written only for the kiro family -- so
        gating the load on it drops the conversation on every reopen and starts a
        fresh session instead.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_HARNESS_OWNED_SESSIONS
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_HARNESS_OWNED_SESSIONS


def _effort_provider(backend: str, model: str) -> AcpProvider:
    provider = _build_provider(backend)
    provider._client._model = model
    provider._client._work_dir = MagicMock()
    provider._client.set_config_option = AsyncMock()
    provider._client.supports_config_option = MagicMock(return_value=True)
    return provider


class TestTheEffortChannelIsReadFromItsOwnTable:
    """A tuning channel is decided by its own set, never by "runs on the runtime".

    ``_apply_initial_effort`` skips the live push for the kiro family because kiro
    reads effort from the spawn-time ``cli.json`` overlay instead. Spelling that
    skip as "is this backend on AcpRuntime?" makes the two facts one, and a codex
    session's configured effort is then dropped silently: codex is a member of
    ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` and ``session/set_config_option`` is
    the only channel it has, so nothing else would apply the level and only a
    manual ``change_effort`` recovers it. Harness-parity H6 is the rule these
    assertions hold the gate to.
    """

    @pytest.mark.asyncio
    async def test_on_the_runtime_codex_still_gets_its_configured_effort(self):
        provider = _effort_provider(ACP_BACKEND_CODEX, "gpt-5.6-codex")
        provider._effort_per_model = {"gpt-5.6-codex": "high"}
        # The premise of the assertion below: codex IS on the shared runtime, so a
        # runtime-membership gate would return here and apply nothing.
        assert provider.is_acp_runtime_backend is True
        await provider._apply_initial_effort()
        # The option ID is codex's own spelling, resolved through
        # ``effort_config_option_id``: the startup application of a persisted
        # level is one of the effort sites that reads it, and writing ``effort``
        # here draws "unknown config option", which the push reads as "no effort
        # selector" and skips.
        provider._client.set_config_option.assert_awaited_once_with(
            effort_config_option_id(ACP_BACKEND_CODEX), "high"
        )

    @pytest.mark.asyncio
    async def test_the_kiro_family_still_takes_effort_from_its_overlay(self):
        """The skip the runtime gate stands in for, held by the channel set instead."""
        for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS):
            provider = _effort_provider(backend, "claude-fable-5")
            provider._effort_per_model = {"claude-fable-5": "high"}
            await provider._apply_initial_effort()
            provider._client.set_config_option.assert_not_awaited()

    def test_codex_is_in_the_channel_set_and_the_kiro_family_is_not(self):
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
        assert not ({ACP_BACKEND_KIRO, ACP_BACKEND_KAS} & ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION)


def _handle_on(backend: str) -> AcpSessionHandle:
    """A handle whose runtime names *backend* -- the only thing these answers read."""
    rt = MagicMock()
    rt.acp_backend = backend
    return AcpSessionHandle("s1", asyncio.Queue(), rt)


class TestWhatTheHandleAdvertisesForCodex:
    """The handle's own capability answers come from the tables, not from "it is kiro".

    ``AcpSessionHandle`` was written when ``AcpRuntime`` served one host, so two of
    its answers were constants. The moment the switch puts a second host on that
    runtime a constant becomes a claim about a host that never made it -- and both
    of these are user-visible: an advertised steer the host answers ``-32601`` to,
    and a model picker with nothing in it.
    """

    def test_steer_is_not_advertised_for_a_host_outside_the_steer_set(self):
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_STEER
        assert _handle_on(ACP_BACKEND_CODEX).supports_steer is False

    def test_the_kiro_family_still_advertises_steer(self):
        for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS):
            assert _handle_on(backend).supports_steer is True

    def test_the_handle_and_the_client_answer_steer_from_the_same_table(self):
        """Two drivers, one host, one answer -- the drift this whole PR keeps finding."""
        for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX):
            client = MagicMock()
            client.backend = backend
            assert _handle_on(backend).supports_steer is (backend in ACP_BACKENDS_STEER)

    def test_a_model_select_populates_the_picker_when_no_models_object_is_sent(self):
        """codex advertises its models as a ``model`` select, not as ``models``."""
        handle = _handle_on(ACP_BACKEND_CODEX)
        handle.store_session_config(
            {
                "configOptions": [
                    {
                        "id": "model",
                        "type": "select",
                        "currentValue": "gpt-5.6-codex",
                        "options": [
                            {"value": "gpt-5.6-codex", "name": "GPT-5.6 Codex"},
                            {"value": "gpt-5.6", "name": "GPT-5.6"},
                        ],
                    }
                ]
            }
        )
        assert handle._advertised_model_ids() == ["gpt-5.6-codex", "gpt-5.6"]
        assert handle._resolved_model_id == "gpt-5.6-codex"

    def test_a_host_outside_the_advertised_selection_set_gets_no_synthesized_list(self):
        """The fold is only meaningful where the advertised list IS the vocabulary."""
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
        handle = _handle_on(ACP_BACKEND_KIRO)
        handle.store_session_config(
            {
                "configOptions": [
                    {
                        "id": "model",
                        "type": "select",
                        "options": [{"value": "claude-fable-5"}],
                    }
                ]
            }
        )
        assert handle._advertised_model_ids() == []

    def test_a_models_object_still_wins_when_the_host_sends_one(self):
        """The synthesis is a fallback, never an override of what was advertised."""
        handle = _handle_on(ACP_BACKEND_CODEX)
        handle.store_session_config(
            {
                "models": {
                    "availableModels": [{"modelId": "gpt-5.6", "name": "GPT-5.6"}],
                    "currentModelId": "gpt-5.6",
                },
                "configOptions": [
                    {
                        "id": "model",
                        "type": "select",
                        "options": [{"value": "should-not-be-read"}],
                    }
                ],
            }
        )
        assert handle._advertised_model_ids() == ["gpt-5.6"]

    def test_the_entitlement_probe_reads_the_select_too(self):
        """The probe exists to HEAL a degraded snapshot, so [] is its worst answer.

        ``probe_advertised_models`` re-asks the entitlement question on a throwaway
        session because a birth snapshot taken while a token refresh was in flight can
        name the free-tier set forever. It returns the normalized list, and an empty
        return is contractually "no evidence" -- ``refresh_available_models`` keeps the
        snapshot it holds. A probe that reads only the ``models`` object therefore
        answers [] for a host whose list is a ``configOptions`` select, which makes the
        one snapshot it exists to correct the one it can never correct.
        """
        resp = {
            "sessionId": "probe",
            "configOptions": [
                {
                    "id": "model",
                    "type": "select",
                    "currentValue": "gpt-5.6-codex",
                    "options": [
                        {"value": "gpt-5.6-codex", "name": "GPT-5.6 Codex"},
                        {"value": "gpt-5.6", "name": "GPT-5.6"},
                    ],
                }
            ],
        }
        assert parse_advertised_models(resp) == []
        assert [m["modelId"] for m in advertised_models_from_session(resp, ACP_BACKEND_CODEX)] == [
            "gpt-5.6-codex",
            "gpt-5.6",
        ]

    def test_both_model_readers_answer_from_one_fold(self):
        """The session-init capture and the probe must not know different shapes.

        They are the two readers of "where does this host's model list live", and the
        asymmetry between them is exactly what shipped once already: the capture was
        taught about the select and the probe was not. Both go through
        ``session_models_envelope`` now, so this asserts they agree rather than
        asserting each separately.
        """
        select_only = {
            "configOptions": [
                {"id": "model", "type": "select", "options": [{"value": "gpt-5.6-codex"}]}
            ]
        }
        object_only = {"models": {"availableModels": [{"modelId": "gpt-5.6-codex"}]}}
        for resp in (select_only, object_only):
            handle = _handle_on(ACP_BACKEND_CODEX)
            handle.store_session_config(resp)
            assert handle._advertised_model_ids() == [
                m["modelId"] for m in advertised_models_from_session(resp, ACP_BACKEND_CODEX)
            ]

    def test_one_authoring_serves_both_drivers(self):
        """``AcpClient`` reads the same helper, so the two captures cannot drift."""
        resp = {
            "configOptions": [
                {
                    "id": "model",
                    "type": "select",
                    "options": [{"value": "gpt-5.6-codex"}],
                }
            ]
        }
        envelope = models_from_config_options(resp, ACP_BACKEND_CODEX)
        assert envelope is not None
        assert envelope["availableModels"][0]["modelId"] == "gpt-5.6-codex"
        assert models_from_config_options(resp, ACP_BACKEND_KIRO) is None


# ---------------------------------------------------------------------------
# Half three: what AcpRuntime does with a codex session
# ---------------------------------------------------------------------------


class _ControlPlane:
    """Every control-plane request one ``create_session`` issues, in order.

    THREE seams are recorded because the runtime uses three: handshake requests it
    awaits itself (``_send_and_await`` — session/new, set_mode), requests a session
    HANDLE sends (``send_request`` — set_config_option), and NOTIFICATIONS
    (``send_notification`` — ``session/cancel``, which the host does not answer).
    codex's teardown ``session/close`` is a REQUEST and lands in ``methods``; a
    test asserting it in ``notifications`` would be asserting the leak.
    A test that watched only the first would read a missing permission write as a
    passing session; one that watched only the first two would make an awaited
    teardown look correct, which is the shape that hid a full-budget stall on every
    codex eviction.

    ``notifications`` is kept separate from ``methods`` on purpose. Folding them into
    one list would let a verb sent the wrong way still satisfy an assertion about
    which verbs went out.
    """

    def __init__(self, new_response: dict[str, Any], *, reject_config: bool = False) -> None:
        self._new_response = new_response
        self._reject_config = reject_config
        self.methods: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    def install(self, rt: AcpRuntime, stack: Any) -> None:
        async def _send_and_await(method, params, timeout=None):
            self.methods.append(method)
            self.params.append(params)
            if method == METHOD_SESSION_NEW:
                return self._new_response
            return {}

        async def _send_request(method, params):
            self.methods.append(method)
            self.params.append(params)
            return 999

        async def _send_notification(method, params):
            self.notifications.append((method, params))

        async def _wait_for_response(_self, req_id, timeout=None):
            if self._reject_config:
                # AcpError, not AcpRuntimeError: an error FRAME travels the shared
                # raise helper, and the rejected-write branch of
                # apply_session_permission_routing catches that class. A stand-in
                # raising the wrong one would exercise no branch at all.
                raise AcpError("-32602 Invalid params")
            return {}

        stack.enter_context(patch.object(rt, "_send_and_await", _send_and_await))
        stack.enter_context(patch.object(rt, "send_request", _send_request))
        stack.enter_context(patch.object(rt, "send_notification", _send_notification))
        stack.enter_context(
            patch.object(AcpSessionHandle, "_wait_for_response", _wait_for_response)
        )

    def params_for(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in zip(self.methods, self.params) if m == method]


def _codex_runtime() -> AcpRuntime:
    """An initialized codex runtime wired to a fake process.

    No pipe and no child: the control plane is recorded at the two seams above,
    so what is exercised is the SET and ORDER of requests a codex session start
    puts on the wire.
    """
    return _runtime_for(ACP_BACKEND_CODEX, pid=4242)


def _runtime_for(backend: str, *, pid: int) -> AcpRuntime:
    rt = AcpRuntime(work_dir="/tmp", acp_backend=backend, expect_mcp_reports=False)
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    proc.pid = pid
    rt._process = proc
    rt._pid = pid
    rt._initialized = True
    return rt


def _codex_session_new_response(sid: str = "sid-codex") -> dict[str, Any]:
    """What codex-acp answers ``session/new`` with: modes as PERMISSION TIERS.

    ``mode`` is a config option whose values are the tiers, and ``currentModeId``
    names the permissive one. Note what is NOT here: an ``availableModes`` list of
    agent ids, because codex has no agent spec to name.
    """
    return {
        "sessionId": sid,
        "modes": {"currentModeId": "agent"},
        "configOptions": [
            {
                "id": "mode",
                "options": [
                    {"value": "read-only"},
                    {"value": "agent"},
                    {"value": "full-access"},
                ],
            }
        ],
    }


class TestTheRuntimeSendsCodexNoAgentMode:
    @pytest.mark.asyncio
    async def test_no_set_mode_goes_out_for_a_codex_session(self):
        """codex's modes are permission tiers, so a Crew agent id resolves to none.

        The runtime asks ``ACP_BACKEND_ROUTING`` which hosts activate an agent by
        ``session/set_mode``; codex is not one, so the request is never built.
        Sent anyway it draws a fault, and the cleanup path then tears down a
        session that had started fine -- every codex session on the shared runtime
        failing at startup.
        """
        rt = _codex_runtime()
        plane = _ControlPlane(_codex_session_new_response())

        with ExitStack() as stack:
            plane.install(rt, stack)
            handle = await rt.create_session(cwd="/w", agent="kirocrew")

        assert handle.session_id == "sid-codex"
        assert METHOD_SET_MODE not in plane.methods
        # The session survived: codex's teardown verb never went out, on either seam.
        assert METHOD_SESSION_CLOSE not in plane.methods
        assert METHOD_SESSION_CLOSE not in [m for m, _ in plane.notifications]
        assert "sid-codex" in rt._session_queues

    @pytest.mark.asyncio
    async def test_the_kiro_family_still_sends_it(self):
        """The counterexample: the gate reads a table, so kiro is unaffected.

        A negative assertion alone would pass just as well if the gate refused
        every host, which is the failure this pairs against.
        """
        rt = _runtime_for(ACP_BACKEND_KIRO, pid=4243)
        plane = _ControlPlane({"sessionId": "sid-kiro"})

        with ExitStack() as stack:
            plane.install(rt, stack)
            await rt.create_session(cwd="/w", agent="kirocrew")

        assert METHOD_SET_MODE in plane.methods


class TestTheRuntimeArmsCodexPermissionRouting:
    @pytest.mark.asyncio
    async def test_the_mode_write_goes_out_before_the_handle_is_returned(self):
        """The boundary is armed on the way out, not on the first prompt.

        codex's default ``agent`` mode writes inside the workspace without asking,
        so a session handed back before this write is a session whose first turn
        runs ungoverned. ``session_config_issue`` reads the option list
        ``session/new`` just advertised, which is why the call sits after that
        response is stored.
        """
        rt = _codex_runtime()
        plane = _ControlPlane(_codex_session_new_response())

        with ExitStack() as stack:
            plane.install(rt, stack)
            await rt.create_session(cwd="/w", agent="kirocrew")

        writes = plane.params_for(METHOD_SET_CONFIG_OPTION)
        assert len(writes) == 1, plane.methods
        assert writes[0]["configId"] == "mode"
        assert writes[0]["value"] == "read-only"
        # After session/new, never before it: the option list it carries is what
        # decides whether the write is even applicable.
        assert plane.methods.index(METHOD_SESSION_NEW) < plane.methods.index(
            METHOD_SET_CONFIG_OPTION
        )

    @pytest.mark.asyncio
    async def test_a_rejected_write_terminates_the_session_and_refuses(self):
        """An enforced host that cannot be routed must not run, and must not leak.

        ``session/new`` already succeeded, so the session exists in the shared
        process: refusing without tearing it down would leave it there for the
        life of the adapter, holding a Codex thread nothing will reclaim.
        """
        rt = _codex_runtime()
        plane = _ControlPlane(_codex_session_new_response(), reject_config=True)

        with ExitStack() as stack:
            plane.install(rt, stack)
            with pytest.raises(AcpToolGateUnroutable):
                await rt.create_session(cwd="/w", agent="kirocrew")

        # As an awaited REQUEST: ``session/close`` is answered promptly, so the
        # refusal path pays one round-trip -- and the session actually leaves the
        # process, which a notification-form close (ignored by the adapter) would not
        # achieve. The teardown is the last control-plane write.
        assert plane.methods[-1] == METHOD_SESSION_CLOSE
        assert plane.params[-1] == {"sessionId": "sid-codex"}
        assert plane.notifications == []
        assert "sid-codex" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_an_unadvertised_option_refuses_rather_than_reading_as_routed(self):
        """ "Cannot tell" is not "armed".

        A response with no ``mode`` option leaves Crew unable to say what the
        adapter will do. The verdict is INDETERMINATE, and for an enforced host
        that refuses -- the alternative being a session that reports routed while
        running its own default tier.
        """
        rt = _codex_runtime()
        plane = _ControlPlane({"sessionId": "sid-codex", "modes": {"currentModeId": "agent"}})

        with ExitStack() as stack:
            plane.install(rt, stack)
            with pytest.raises(AcpToolGateUnroutable):
                await rt.create_session(cwd="/w", agent="kirocrew")

        assert METHOD_SET_CONFIG_OPTION not in plane.methods
        assert plane.methods[-1] == METHOD_SESSION_CLOSE
        assert plane.params[-1] == {"sessionId": "sid-codex"}
        assert plane.notifications == []

    @pytest.mark.asyncio
    async def test_the_kiro_family_sends_no_extra_write(self):
        """Self-gating, so the shipped hosts are untouched.

        kiro-cli routes through its agent spec, so the call is inert there. The
        byte-for-byte proof that neither shipped host's control plane moved is
        ``test_acp_harness_wire_parity.py``'s golden corpus, which this change does
        not re-record.
        """
        rt = _runtime_for(ACP_BACKEND_KIRO, pid=4244)
        plane = _ControlPlane({"sessionId": "sid-kiro"})

        with ExitStack() as stack:
            plane.install(rt, stack)
            await rt.create_session(cwd="/w", agent="kirocrew")

        assert METHOD_SET_CONFIG_OPTION not in plane.methods


class TestTheRuntimeCarriesTheProjectionOntoTheSession:
    """``create_session`` sends the projected array and hands the deny set to the handle.

    The projection itself is a double here: what these assert is that the runtime
    CONSUMES what it returned. Dropping either half is silent otherwise -- an array
    that was narrowed and then not sent looks identical to a narrowed session, and a
    deny set the handle never received is a restriction nothing enforces.
    """

    @staticmethod
    def _projection(servers: list[dict], denied: set[tuple[str, str]]):
        async def _mirrored(agent, *, work_dir, session_key, channel_id):
            return _MirroredSessionMcp(
                servers=list(servers),
                denied_tools=frozenset(denied),
                stub_token="tok-proj",
                derived_spec_snapshot=None,
            )

        return _mirrored

    @pytest.mark.asyncio
    async def test_the_projected_array_is_what_session_new_carries(self):
        rt = _codex_runtime()
        plane = _ControlPlane(_codex_session_new_response())
        projected = [{"name": "kirocrew-core", "type": "stdio", "command": "/x", "args": []}]
        with ExitStack() as stack:
            plane.install(rt, stack)
            stack.enter_context(
                patch.object(rt, "_mirrored_session_mcp", self._projection(projected, set()))
            )
            handle = await rt.create_session(cwd="/w", agent="kirocrew")

        sent = plane.params_for(METHOD_SESSION_NEW)[0]["mcpServers"]
        assert [e["name"] for e in sent] == ["kirocrew-core"]
        # And the token the projection minted rides the handle, so a later warm-pool
        # rekey claim names THIS session rather than every session on the runtime.
        assert handle.stub_session_token == "tok-proj"

    @pytest.mark.asyncio
    async def test_the_deny_set_reaches_the_handle_that_answers_the_prompts(self):
        rt = _codex_runtime()
        plane = _ControlPlane(_codex_session_new_response())
        with ExitStack() as stack:
            plane.install(rt, stack)
            stack.enter_context(
                patch.object(
                    rt,
                    "_mirrored_session_mcp",
                    self._projection([], {("kirocrew-core", "spawn_run")}),
                )
            )
            handle = await rt.create_session(cwd="/w", agent="kirocrew")

        assert handle.spec_denied_tools == frozenset({("kirocrew-core", "spawn_run")})

    @pytest.mark.asyncio
    async def test_a_host_with_no_mirror_gets_an_empty_deny_set(self):
        """kiro takes the pooled array and checks nothing at the approval request --
        it enforces the spec's per-tool restrictions itself."""
        rt = _runtime_for(ACP_BACKEND_KIRO, pid=4245)
        plane = _ControlPlane({"sessionId": "sid-kiro"})
        with ExitStack() as stack:
            plane.install(rt, stack)
            handle = await rt.create_session(cwd="/w", agent="kirocrew")

        assert handle.spec_denied_tools == frozenset()
        assert has_mirror(ACP_BACKEND_KIRO) is False
