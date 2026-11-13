"""A re-open a person waits on parks the stored session in the start agent first.

KAS resumes a session in the mode it stored, its own agent, and queues that
agent's MCP servers ahead of Kiro Crew's. ``load_session`` therefore precedes the
load of a re-open a person is waiting on with ``_park_in_start_agent``, and the
load then runs as a new session's start does. Pinned here: who gets that step
(``mcp_start_wait.person_waits``), the three requests it sends, what each of its
failures does, and that every other resume, host and older engine sends the one
load it always sent. The engine's side was measured, not modelled:
``session/load`` takes the re-sent definition, ignores a ``modeId``, and resumes
in the stored mode (kiro-cli 3.2.0, KAS 0.66.95).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp import runtime as runtime_facade
from kiro_crew.acp import runtime_session_load
from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.kas_agents import KAS_MAX_CUSTOM_AGENTS
from kiro_crew.acp.kas_start_agent import START_AGENT_ID
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.mcp_start_wait import (
    first_prompt_brief_wait,
    left_to_first_turn,
    person_waits,
)
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import (
    AcpFrameTooLarge,
    AcpRequestTimeout,
    AcpRuntimeError,
    AcpSessionHandle,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_KAS_MCP_STATUS,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_LOAD,
    METHOD_SET_MODE,
    JsonRpcMessage,
)
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, finished

FOREGROUND, BACKGROUND = StartPriority.FOREGROUND, StartPriority.BACKGROUND

_SID = "sess-reopen"
_AGENT = "kirocrew"
_USER_SERVERS = {"github": {"command": "gh-mcp"}, "jira": {"command": "jira-mcp"}}
#: What an engine that has the release verb advertises at ``initialize``.
_CAN_RELEASE = {"sessionCapabilities": {"close": {}}}
#: The engine's built-in mode, where it resumes a session whose stored mode the
#: load's batch does not carry (measured: kiro-cli 3.2.0 and 3.2.1).
_ENGINE_DEFAULT = "vibe"
#: Modes the engine defines itself and advertises on every load, whatever the
#: batch carries (two of those kiro-cli 3.2.1 advertises).
_ENGINE_BUILTINS = (_ENGINE_DEFAULT, "spec")
#: Another agent registered beside the session's own, which a session can be
#: switched to while it runs.
_OTHER_AGENT = "reviewer"
_TARGET = {"prompt": "p", "tools": []}
_OTHER = {**_TARGET, "id": _OTHER_AGENT}
#: A load as sent: Kiro Crew's own server and a Connection's bridge on the session,
#: the user's two servers in the agent's own block.
_WAIT_PARAMS = {
    "sessionId": _SID,
    "mcpServers": [{"name": "kirocrew-core"}, {"name": "bridge"}],
    "_meta": {"kiro": {"customAgents": [{"id": _AGENT, "mcpServers": dict(_USER_SERVERS)}]}},
}


@pytest.fixture(autouse=True)
def _no_pooled_stub_read(_floor_monkeypatch):
    """Skip the pooled-stub hop: it reads the agent's spec from disk, which builds the
    default agent and its memory store on every fresh home. No stub is under test here."""
    _floor_monkeypatch.setattr(
        runtime_facade,
        "_pooled_session_servers_and_ref_spec",
        lambda *_a, **_k: ([], None, ()),
    )


class TestPersonWaits:
    """The one predicate: who is waiting, not which queue the start waited in."""

    @pytest.mark.parametrize(
        ("priority", "focus_resume", "expected"),
        [
            (FOREGROUND, False, True),
            (FOREGROUND, True, True),
            (BACKGROUND, True, True),
            (BACKGROUND, False, False),
        ],
        ids=["typed", "typed-after-focus", "focus-resume-prefetch", "nobody"],
    )
    def test_a_claim_or_the_owners_return_is_a_person_waiting(
        self, priority, focus_resume, expected
    ):
        assert person_waits(priority, focus_resume=focus_resume) is expected

    def test_the_default_is_nobody(self):
        assert person_waits(BACKGROUND) is False

    def test_the_wait_rule_reads_the_same_predicate(self):
        """The owner's return keeps its queue place and is not held for the user's servers."""
        params = {
            "mcpServers": [{"name": "kirocrew-core"}, {"name": "bridge"}],
            "_meta": {
                "kiro": {"customAgents": [{"id": _AGENT, "mcpServers": dict(_USER_SERVERS)}]}
            },
        }
        required = ("kirocrew-core",)
        typed = first_prompt_brief_wait(params, _AGENT, required, FOREGROUND)
        returned = first_prompt_brief_wait(params, _AGENT, required, BACKGROUND, focus_resume=True)
        nobody = first_prompt_brief_wait(params, _AGENT, required, BACKGROUND)
        assert returned == typed == ("bridge",)
        assert nobody == ("bridge", "github", "jira")

    @pytest.mark.parametrize(
        ("priority", "focus_resume"),
        [(FOREGROUND, False), (BACKGROUND, True), (FOREGROUND, True), (BACKGROUND, False)],
        ids=["typed", "owners-return", "typed-after-focus", "nobody"],
    )
    def test_every_own_server_is_waited_for_by_the_start_or_left_to_the_first_turn(
        self, priority, focus_resume
    ):
        """The owner's return skips the user's servers at the start, so its first turn
        must be told of them or hold for them, exactly as a typed start's is."""
        required = ("kirocrew-core",)
        waited = first_prompt_brief_wait(
            _WAIT_PARAMS, _AGENT, required, priority, focus_resume=focus_resume
        )
        left, more = left_to_first_turn(
            _WAIT_PARAMS, _AGENT, required, priority, focus_resume=focus_resume
        )

        assert set(_USER_SERVERS) <= {*waited, *left}
        assert not set(waited) & set(left)
        assert bool(left) is person_waits(priority, focus_resume=focus_resume)
        assert more == 0


class TestTheFirstTurnAfterAReopen:
    """The readiness wait itself, on a re-open: what it leaves to the first turn."""

    def _handle(self) -> AcpSessionHandle:
        """A loaded session whose own server and bridge are up; the user's two still connect."""
        handle = AcpSessionHandle.__new__(AcpSessionHandle)
        handle._session_id = _SID
        handle._mcp_report = McpSessionReport(folded_on_arrival=True)
        handle._mcp_report.begin_session([])
        handle._queue = asyncio.Queue()
        servers = []
        for name in ("kirocrew-core", "bridge", *_USER_SERVERS):
            entry: dict[str, Any] = {
                "name": name,
                "status": "connecting" if name in _USER_SERVERS else "connected",
                "_meta": {"kiro": {"resource": {"source": {"origin": "client"}}}},
            }
            if entry["status"] == "connected":
                entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
            servers.append(entry)
        frame = JsonRpcMessage(
            method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": servers}
        )
        handle._mcp_report.fold_frame(frame)
        handle._queue.put_nowait(frame)
        handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
        return handle

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("priority", "focus_resume", "left"),
        [
            (FOREGROUND, False, tuple(_USER_SERVERS)),
            (BACKGROUND, True, tuple(_USER_SERVERS)),
        ],
        ids=["typed", "owners-return"],
    )
    async def test_a_reopen_a_person_waits_on_leaves_the_users_servers_to_its_first_turn(
        self, tmp_path, monkeypatch, priority, focus_resume, left
    ):
        """Not waited for by the load, so the first prompt is told of them or holds."""
        monkeypatch.setattr(runtime_session_load, "_declared_governed", lambda: False)
        rt = _runtime(tmp_path)
        handle = self._handle()

        await finished(
            rt._wait_managed_mcp(
                handle,
                _WAIT_PARAMS,
                _AGENT,
                FALLBACK_TIMEOUT_SECS,
                0,
                priority,
                focus_resume=focus_resume,
            ),
            "the re-open's readiness wait",
        )

        assert handle._mcp_report.left_to_turn == left
        assert handle._mcp_report.not_ready() == {"connecting": left}


def _runtime(tmp_path, *, backend: str = ACP_BACKEND_KAS, capabilities: dict | None = None):
    """An initialized runtime on a fake process, with the wire recorded instead of sent."""
    rt = AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    rt._process = proc
    rt._pid = None
    rt._initialized = True
    rt._can_load_session = True
    rt._agent_capabilities = dict(_CAN_RELEASE if capabilities is None else capabilities)
    return rt


class _Wire:
    """Answers the runtime's requests as the engine does, and records them in order.

    ``stored`` is the mode the engine holds for the session: a ``session/load``
    answers it as the current mode when the batch carries it or it is one of the
    engine's own built-ins (``_ENGINE_BUILTINS``, always advertised), and the
    engine's default (``_ENGINE_DEFAULT``) otherwise, and a
    ``session/set_mode`` replaces it. ``fail`` maps a method to the exception
    its next request raises.
    """

    def __init__(
        self, *, stored: str = _AGENT, advertise_start: bool = True, advertise_own: bool = True
    ) -> None:
        self.stored = stored
        self.advertise_start = advertise_start
        self.advertise_own = advertise_own
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.resumed_in: list[str] = []
        self.fail: dict[str, Exception] = {}

    async def __call__(self, method: str, params: dict[str, Any], timeout=None, **_kw) -> dict:
        self.sent.append((method, params))
        if method in self.fail:
            raise self.fail.pop(method)
        if method == METHOD_SESSION_LOAD:
            batch = params.get("_meta", {}).get("kiro", {}).get("customAgents", [])
            # A host that took its agent at spawn time advertises it with no batch.
            ids = [entry["id"] for entry in batch] or [_AGENT]
            if not self.advertise_start and START_AGENT_ID in ids:
                ids.remove(START_AGENT_ID)
            if not self.advertise_own and _AGENT in ids:
                ids.remove(_AGENT)
            current = self.stored if self.stored in {*ids, *_ENGINE_BUILTINS} else _ENGINE_DEFAULT
            self.resumed_in.append(current)
            return {
                "modes": {
                    "currentModeId": current,
                    "availableModes": [
                        {"id": mode_id} for mode_id in {*ids, *_ENGINE_BUILTINS, current}
                    ],
                },
                "models": [],
            }
        if method == METHOD_SET_MODE:
            self.stored = params["modeId"]
        return {}

    def methods(self) -> list[str]:
        return [method for method, _params in self.sent]


def _parking(sid: str = _SID, *, others: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {
        "sessionId": sid,
        "cwd": "/w",
        "mcpServers": [],
        "_meta": {
            "kiro": {
                "customAgents": [
                    {"id": _AGENT, "prompt": "p", "includeMcpJson": False},
                    *others,
                    {"id": START_AGENT_ID, "prompt": "start", "tools": []},
                ]
            }
        },
    }


class TestParkInStartAgent:
    """The step itself: load with nothing to start, set the stored mode, free the copy."""

    @pytest.mark.asyncio
    async def test_it_loads_sets_the_start_mode_and_frees_the_copy(self, tmp_path, monkeypatch):
        rt = _runtime(tmp_path)
        wire = _Wire()
        monkeypatch.setattr(rt, "_send_and_await", wire)
        params = _parking()

        await rt._park_in_start_agent(_SID, params, 30.0)

        assert wire.sent == [
            (METHOD_SESSION_LOAD, params),
            (METHOD_SET_MODE, {"sessionId": _SID, "modeId": START_AGENT_ID}),
            (METHOD_SESSION_CLOSE, {"sessionId": _SID}),
        ]
        assert wire.stored == START_AGENT_ID
        assert rt._parked_copies == set()

    @pytest.mark.asyncio
    async def test_a_session_already_parked_is_only_freed(self, tmp_path, monkeypatch):
        """A re-open cut off after this step left the start agent as the stored mode."""
        rt = _runtime(tmp_path)
        wire = _Wire(stored=START_AGENT_ID)
        monkeypatch.setattr(rt, "_send_and_await", wire)

        await rt._park_in_start_agent(_SID, _parking(), 30.0)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SESSION_CLOSE]

    @pytest.mark.asyncio
    async def test_a_session_switched_to_another_agent_is_left_in_it(self, tmp_path, monkeypatch):
        """Parking it would end with its own agent in place of the one it runs."""
        rt = _runtime(tmp_path)
        wire = _Wire(stored=_OTHER_AGENT)
        monkeypatch.setattr(rt, "_send_and_await", wire)

        await rt._park_in_start_agent(_SID, _parking(others=(_OTHER,)), 30.0)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SESSION_CLOSE]
        assert wire.stored == _OTHER_AGENT
        assert rt._parked_copies == set()

    @pytest.mark.asyncio
    async def test_an_engine_that_did_not_register_the_start_agent_is_left_as_stored(
        self, tmp_path, monkeypatch
    ):
        rt = _runtime(tmp_path)
        wire = _Wire(advertise_start=False)
        monkeypatch.setattr(rt, "_send_and_await", wire)

        await rt._park_in_start_agent(_SID, _parking(), 30.0)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SESSION_CLOSE]
        assert wire.stored == _AGENT

    @pytest.mark.asyncio
    async def test_a_refused_set_mode_still_frees_the_copy_and_does_not_fail_the_reopen(
        self, tmp_path, monkeypatch
    ):
        rt = _runtime(tmp_path)
        wire = _Wire()
        wire.fail[METHOD_SET_MODE] = AcpRuntimeError("Mode not found")
        monkeypatch.setattr(rt, "_send_and_await", wire)

        await rt._park_in_start_agent(_SID, _parking(), 30.0)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SET_MODE, METHOD_SESSION_CLOSE]
        assert wire.stored == _AGENT
        assert rt._parked_copies == set()

    @pytest.mark.asyncio
    async def test_a_load_the_engine_refused_raises_its_own_error_and_holds_nothing(
        self, tmp_path, monkeypatch
    ):
        """The provider's retry reads the load's message, e.g. a stale session lock,
        and loads the id again: an error answer means the engine loaded nothing."""
        rt = _runtime(tmp_path)
        wire = _Wire()
        wire.fail[METHOD_SESSION_LOAD] = AcpRuntimeError("session is active in another process")
        # An engine that answers a close of an id it does not hold with an error.
        wire.fail[METHOD_SESSION_CLOSE] = AcpRuntimeError("no such session")
        monkeypatch.setattr(rt, "_send_and_await", wire)

        with pytest.raises(AcpRuntimeError, match="active in another process"):
            await rt._park_in_start_agent(_SID, _parking(), 30.0)

        assert wire.methods() == [METHOD_SESSION_LOAD]
        assert rt._parked_copies == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unanswered",
        [AcpRequestTimeout("session/load timed out"), AcpFrameTooLarge("frame too large")],
        ids=["no-answer", "answer-too-large"],
    )
    async def test_a_load_with_no_readable_answer_may_still_be_held(
        self, tmp_path, monkeypatch, unanswered
    ):
        rt = _runtime(tmp_path)
        wire = _Wire()
        wire.fail[METHOD_SESSION_LOAD] = unanswered
        wire.fail[METHOD_SESSION_CLOSE] = AcpRuntimeError("busy")
        monkeypatch.setattr(rt, "_send_and_await", wire)

        with pytest.raises(type(unanswered)):
            await rt._park_in_start_agent(_SID, _parking(), 30.0)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SESSION_CLOSE]
        assert rt._parked_copies == {_SID}

    @pytest.mark.asyncio
    async def test_a_copy_the_engine_did_not_confirm_freeing_fails_the_reopen(
        self, tmp_path, monkeypatch
    ):
        """The engine ignores the batch of a load for a session it still holds."""
        rt = _runtime(tmp_path)
        wire = _Wire()
        wire.fail[METHOD_SESSION_CLOSE] = AcpRuntimeError("close failed")
        monkeypatch.setattr(rt, "_send_and_await", wire)

        with pytest.raises(AcpRuntimeError, match="could not be freed"):
            await rt._park_in_start_agent(_SID, _parking(), 30.0)

        assert rt._parked_copies == {_SID}


def _resumable(
    rt: AcpRuntime,
    monkeypatch,
    wire: _Wire,
    *,
    servers: dict | None = None,
    tools: tuple[str, ...] = ("fs_read",),
    targets: tuple[dict[str, Any], ...] = (),
):
    """Let ``load_session`` run to its end on *rt*, recording the readiness wait's inputs.

    The batch is the session's own agent (*servers*: the user's two unless given,
    *tools*) followed by *targets*, the other agents registered with it.
    """
    waits: list[dict[str, Any]] = []
    declared = dict(_USER_SERVERS) if servers is None else servers

    async def _agents(agent, **_kw):
        own = {"id": agent, "prompt": "p", "tools": list(tools)}
        if declared:
            own["mcpServers"] = dict(declared)
        return SessionExtras(custom_agents=[own, *(dict(target) for target in targets)])

    async def _wait(
        handle, params, agent, timeout, stale, priority=BACKGROUND, *, focus_resume=False
    ):
        waits.append(
            {
                "stale_report_frames": stale,
                "start_priority": priority,
                "focus_resume": focus_resume,
                "batch": [entry["id"] for entry in params["_meta"]["kiro"]["customAgents"]],
            }
        )

    monkeypatch.setattr(rt, "_send_and_await", wire)
    monkeypatch.setattr(rt, "_kas_custom_agents", _agents)
    monkeypatch.setattr(rt, "_wait_managed_mcp", _wait)
    return waits


def _own_servers(params: dict[str, Any]) -> dict | None:
    return params["_meta"]["kiro"]["customAgents"][0].get("mcpServers")


class TestAReopenAPersonWaitsOn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("priority", "focus_resume"),
        [(FOREGROUND, False), (BACKGROUND, True)],
        ids=["typed-with-no-focus", "focus-then-type"],
    )
    async def test_it_parks_first_then_loads_and_switches_to_its_own_agent(
        self, tmp_path, monkeypatch, priority, focus_resume
    ):
        rt = _runtime(tmp_path)
        wire = _Wire()
        waits = _resumable(rt, monkeypatch, wire)

        handle = await rt.load_session(
            "",
            _SID,
            cwd=str(tmp_path),
            agent=_AGENT,
            start_priority=priority,
            focus_resume=focus_resume,
        )

        assert wire.methods() == [
            METHOD_SESSION_LOAD,
            METHOD_SET_MODE,
            METHOD_SESSION_CLOSE,
            METHOD_SESSION_LOAD,
            METHOD_SET_MODE,
        ]
        parked, real = wire.sent[0][1], wire.sent[3][1]
        # Nothing can start while the session is parked.
        assert parked["mcpServers"] == []
        assert _own_servers(parked) is None
        assert wire.sent[1][1] == {"sessionId": _SID, "modeId": START_AGENT_ID}
        # The load proper is the full one, the agent's own servers in its block.
        assert _own_servers(real) == _USER_SERVERS
        assert [e["id"] for e in real["_meta"]["kiro"]["customAgents"]] == [_AGENT, START_AGENT_ID]
        assert wire.sent[4][1]["modeId"] == _AGENT
        # The session ends in its own agent, which is the stored mode again.
        assert handle.active_agent == _AGENT
        assert wire.stored == _AGENT
        # Reports staged in the start agent name session-level servers: they count.
        assert waits == [
            {
                "stale_report_frames": 0,
                "start_priority": priority,
                "focus_resume": focus_resume,
                "batch": [_AGENT, START_AGENT_ID],
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("priority", "focus_resume"),
        [(FOREGROUND, False), (BACKGROUND, True)],
        ids=["typed-with-no-focus", "focus-then-type"],
    )
    async def test_a_session_switched_to_another_agent_stays_in_it(
        self, tmp_path, monkeypatch, priority, focus_resume
    ):
        """Nobody named an agent, so the re-open keeps the one the session runs:
        it is not parked, and the load is the one every resume sends."""
        rt = _runtime(tmp_path)
        rt._agent = _AGENT
        wire = _Wire(stored=_OTHER_AGENT)
        _resumable(rt, monkeypatch, wire, targets=(_OTHER,))

        await rt.load_session(
            "",
            _SID,
            cwd=str(tmp_path),
            agent=None,
            start_priority=priority,
            focus_resume=focus_resume,
        )

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SESSION_CLOSE, METHOD_SESSION_LOAD]
        assert wire.resumed_in == [_OTHER_AGENT, _OTHER_AGENT]
        assert wire.stored == _OTHER_AGENT

    @pytest.mark.asyncio
    async def test_a_parked_copy_that_was_not_freed_refuses_the_next_load_on_this_process(
        self, tmp_path, monkeypatch
    ):
        rt = _runtime(tmp_path)
        wire = _Wire()
        _resumable(rt, monkeypatch, wire)
        wire.fail[METHOD_SESSION_CLOSE] = AcpRuntimeError("close failed")

        with pytest.raises(AcpRuntimeError, match="could not be freed"):
            await rt.load_session(
                "", _SID, cwd=str(tmp_path), agent=_AGENT, start_priority=FOREGROUND
            )
        sent = len(wire.sent)
        with pytest.raises(AcpRuntimeError, match="still holds the copy"):
            await rt.load_session("", _SID, cwd=str(tmp_path), agent=_AGENT)

        # The refusal sends nothing: a load now would run the server-less copy.
        assert len(wire.sent) == sent
        assert METHOD_SESSION_LOAD not in wire.methods()[1:]


class TestEveryOtherResume:
    """A resume nobody waits on sends the one load it always sent."""

    @pytest.mark.asyncio
    async def test_a_background_resume_is_not_parked(self, tmp_path, monkeypatch):
        """Bulk Resume, the session_reload verb, a cron run, a sub-agent child."""
        rt = _runtime(tmp_path)
        wire = _Wire()
        waits = _resumable(rt, monkeypatch, wire)

        await rt.load_session("", _SID, cwd=str(tmp_path), agent=_AGENT)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SET_MODE]
        load = wire.sent[0][1]
        assert _own_servers(load) == _USER_SERVERS
        assert waits[0]["start_priority"] is BACKGROUND
        assert waits[0]["focus_resume"] is False

    @pytest.mark.asyncio
    async def test_an_engine_without_the_release_verb_is_not_parked(self, tmp_path, monkeypatch):
        rt = _runtime(tmp_path, capabilities={})
        wire = _Wire()
        _resumable(rt, monkeypatch, wire)

        await rt.load_session("", _SID, cwd=str(tmp_path), agent=_AGENT, start_priority=FOREGROUND)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SET_MODE]

    @pytest.mark.asyncio
    async def test_an_agent_with_no_servers_of_its_own_is_not_parked(self, tmp_path, monkeypatch):
        rt = _runtime(tmp_path)
        wire = _Wire()
        _resumable(rt, monkeypatch, wire, servers={})

        await rt.load_session("", _SID, cwd=str(tmp_path), agent=_AGENT, start_priority=FOREGROUND)

        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SET_MODE]
        assert [e["id"] for e in wire.sent[0][1]["_meta"]["kiro"]["customAgents"]] == [_AGENT]

    @pytest.mark.asyncio
    async def test_another_host_is_not_parked(self, tmp_path, monkeypatch):
        rt, _reader, _proc = _make_runtime()
        assert rt.acp_backend == ACP_BACKEND_KIRO
        rt._can_load_session = True
        rt._agent_capabilities = dict(_CAN_RELEASE)
        wire = _Wire()
        monkeypatch.setattr(rt, "_send_and_await", wire)

        await rt.load_session(
            "", _SID, cwd=str(tmp_path), agent=_AGENT, start_priority=FOREGROUND, focus_resume=True
        )

        assert wire.methods().count(METHOD_SESSION_LOAD) == 1
        assert METHOD_SESSION_CLOSE not in wire.methods()
        assert "kiro" not in wire.sent[0][1].get("_meta", {})


@dataclass(frozen=True)
class _WayBack:
    """One state a cut-off re-open can leave, as the next load finds it.

    The batch that load sends (*servers*, *tools*, *targets*: see ``_resumable``)
    decides whether it registers the start agent, and so which mode the engine
    brings the session back in (*resumes_in*).
    """

    name: str
    resumes_in: str
    stored: str = START_AGENT_ID
    servers: dict | None = None
    tools: tuple[str, ...] = ("fs_read",)
    targets: tuple[dict[str, Any], ...] = ()
    advertise_own: bool = True
    refused: bool = False
    #: A caller that names no agent leaves the session in the mode it is stored in.
    unnamed_keeps: bool = False


_WAYS_BACK = [
    _WayBack("the start agent is registered again", START_AGENT_ID),
    _WayBack("its agent lost its servers", _ENGINE_DEFAULT, servers={}),
    _WayBack(
        "its agent gained the sub-agent tool", _ENGINE_DEFAULT, tools=("fs_read", "@subagent")
    ),
    _WayBack(
        "its batch is full",
        _ENGINE_DEFAULT,
        targets=tuple(
            {**_TARGET, "id": f"target-{n:02d}"} for n in range(KAS_MAX_CUSTOM_AGENTS - 1)
        ),
    ),
    _WayBack(
        "another agent now carries the start agent's id",
        START_AGENT_ID,
        targets=({**_TARGET, "id": START_AGENT_ID},),
    ),
    _WayBack(
        "the start agent is registered, its own agent is not advertised",
        START_AGENT_ID,
        advertise_own=False,
        refused=True,
    ),
    _WayBack(
        "no start agent is registered, its own agent is not advertised",
        _ENGINE_DEFAULT,
        servers={},
        advertise_own=False,
        refused=True,
    ),
    _WayBack("it was never parked", _AGENT, stored=_AGENT, unnamed_keeps=True),
    _WayBack(
        "it runs another agent the batch carries",
        _OTHER_AGENT,
        stored=_OTHER_AGENT,
        targets=(_OTHER,),
        unnamed_keeps=True,
    ),
    # The engine answers this load as it answers one whose start agent fell back
    # to its default, so it is put back in its own agent too. Kiro Crew offers no
    # way into such a mode (test_no_kiro_crew_entry_point_switches_a_running_session).
    _WayBack("the engine or another client left it in a built-in", "spec", stored="spec"),
]


class TestASessionLeftParked:
    """A re-open cut off between the parking step and its load leaves the start agent stored.

    The table is each mode the engine can bring such a session back in, as the
    next load's batch decides, beside the modes a session stored elsewhere comes
    back in. It does not model the engine's held copies; a parked copy this
    process could not free is refused before any request
    (``TestAReopenAPersonWaitsOn``).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent", [_AGENT, None], ids=["named-agent", "runtime-default"])
    async def test_an_unattended_resume_ends_in_its_own_agent_with_its_servers(
        self, tmp_path, monkeypatch, agent
    ):
        rt = _runtime(tmp_path)
        rt._agent = _AGENT
        wire = _Wire(stored=START_AGENT_ID)
        waits = _resumable(rt, monkeypatch, wire)

        handle = await rt.load_session("", _SID, cwd=str(tmp_path), agent=agent)

        # One load, the full one, and the switch out of the start agent.
        assert wire.methods() == [METHOD_SESSION_LOAD, METHOD_SET_MODE]
        assert _own_servers(wire.sent[0][1]) == _USER_SERVERS
        assert wire.sent[1][1]["modeId"] == _AGENT
        assert handle.active_agent == _AGENT
        assert wire.stored == _AGENT
        # Today's wait for a start nobody waits on, with the start agent's reports counted.
        assert waits == [
            {
                "stale_report_frames": 0,
                "start_priority": BACKGROUND,
                "focus_resume": False,
                "batch": [_AGENT, START_AGENT_ID],
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent", [_AGENT, None], ids=["named-agent", "runtime-default"])
    @pytest.mark.parametrize("way", _WAYS_BACK, ids=lambda way: way.name)
    async def test_every_way_back_ends_in_its_own_agent_or_is_refused(
        self, tmp_path, monkeypatch, way, agent
    ):
        """Whatever the batch of the next load carries, and whoever sends it: a
        session left in the start agent, or brought back in a mode the load sent
        no definition for, ends in its own agent, or the load is refused and its
        copy freed. One stored in its own agent or in another agent of the batch
        is left there by a caller that names no agent."""
        rt = _runtime(tmp_path)
        rt._agent = _AGENT
        wire = _Wire(stored=way.stored, advertise_own=way.advertise_own)
        _resumable(rt, monkeypatch, wire, servers=way.servers, tools=way.tools, targets=way.targets)
        freed: list[str] = []

        async def _terminate(session_id: str) -> None:
            freed.append(session_id)

        monkeypatch.setattr(rt, "terminate_session", _terminate)

        if way.refused:
            with pytest.raises(AcpRuntimeError, match=f"Agent mode '{_AGENT}' is not available"):
                await rt.load_session("", _SID, cwd=str(tmp_path), agent=agent)
            assert wire.resumed_in == [way.resumes_in]
            assert METHOD_SET_MODE not in wire.methods()
            assert freed == [_SID]
            return

        handle = await rt.load_session("", _SID, cwd=str(tmp_path), agent=agent)

        assert wire.resumed_in == [way.resumes_in]
        assert freed == []
        # A caller that names no agent keeps a session the engine brought back in
        # its own agent, or in another the batch carries, as the one load it always
        # was; every other row is switched to the session's own agent.
        kept = way.unnamed_keeps and agent is None
        assert wire.stored == (way.stored if kept else _AGENT)
        assert wire.methods() == [METHOD_SESSION_LOAD, *([] if kept else [METHOD_SET_MODE])]
        if not kept:
            assert wire.sent[1][1]["modeId"] == _AGENT
            assert handle.active_agent == _AGENT


def test_no_kiro_crew_entry_point_switches_a_running_session():
    """Why a no-override re-open may put a session found in an engine built-in back in
    its own agent: nothing in Kiro Crew puts it there. The runtime switches a mode in
    two places only, the activation helper and the parking step
    (``test_worker_agent_load_bracket``), at a session's start. A running session's
    own switch (``AcpSessionHandle.set_mode``) is reached only through
    ``AcpProvider.set_mode``, and nothing calls that: Kiro Crew's agent picker starts
    a new session instead. A source pin, read from every module of the package (an
    app's own tests aside): each call of a ``set_mode`` attribute and each
    ``getattr(..., "set_mode")``. It does not follow a callable passed around under
    another name."""
    import ast
    from pathlib import Path

    package = Path(runtime_session_load.__file__).resolve().parents[1]
    calls = []
    for path in sorted(package.rglob("*.py")):
        if "tests" in path.relative_to(package).parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "set_mode" not in text:
            continue
        where = path.relative_to(package).as_posix()
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "set_mode":
                calls.append((where, ast.unparse(node.func.value)))
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and any(isinstance(a, ast.Constant) and a.value == "set_mode" for a in node.args)
            ):
                calls.append((where, ast.unparse(node)))

    assert calls == [("acp/session_provider.py", "self._handle")], calls


class TestTheOwnersReturnReachesTheLoad:
    """From the provider the session layer marked to the runtime's load."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("focus_resume", [True, False])
    async def test_the_resume_passes_it_with_the_priority(self, focus_resume):
        from unittest.mock import AsyncMock

        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider.__new__(AcpProvider)
        runtime = MagicMock()
        runtime.load_session = AsyncMock(return_value=MagicMock())

        await provider._load_session_with_retry(
            runtime, "", _SID, None, _AGENT, start_priority=BACKGROUND, focus_resume=focus_resume
        )

        kwargs = runtime.load_session.await_args.kwargs
        assert kwargs["focus_resume"] is focus_resume
        assert kwargs["start_priority"] is BACKGROUND

    def test_a_provider_nobody_marked_is_not_the_owners_return(self):
        from kiro_crew.providers.base import LLMProvider

        assert LLMProvider.focus_resume is False

    def test_only_the_owners_focus_frame_says_so(self):
        """Every other resume keeps today's path because it never passes the mark.

        A bulk Resume, the ``session_reload`` verb, a Bot's spec or harness change, the
        image repair, an app token, a cron run and a sub-agent all reach ``get_or_create``
        through callers that name no ``focus_resume``; a second literal ``True`` anywhere in
        the package would make one of them a start a person waits on.
        """
        import ast
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).parent
        sites: list[tuple[str, str]] = []
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "focus_resume" not in source:
                continue
            for func in ast.walk(ast.parse(source)):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.keyword) or node.arg != "focus_resume":
                        continue
                    if isinstance(node.value, ast.Constant) and node.value.value is True:
                        sites.append((path.relative_to(root).as_posix(), func.name))
        assert sites == [("dashboard/ws.py", "_handle_slot_focused")]
