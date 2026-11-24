"""A Bot's conducting worker gets the session-control mount the Bot's thread has
(rfc-bot-workers-conduct.md, section 5.3; Phase 1 exit criterion 3).

One predicate, ``members.mounts_session_control``, keys the mount for both populations:
the provider's ``_member_session_key`` on both runtime paths (``session/new`` and
``session/load``), the per-chat client's array composer, and the MCP re-inject load.
The composer's later compositions and the re-inject keep a worker that started plain
plain, so the kit arrives only at a start. What rides the mount still follows the
population: the KAS projection adds the
member grant set for the thread's key alone, a conducting worker's conductor-mode set
(``agent._CONDUCTOR_DASHBOARD_GRANTS``) rides its own spec, where its Bot's limits
strip it (``test_mate_worker_conducts``), and the crew panel stays the thread's alone.
A worker a worker created, a worker whose Bot's switch is off and every other session
are keyed on neither path, so their arrays are byte for byte what they are today.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew import members
from kiro_crew.acp.client import AcpClient, ClaudeAcpClient
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
)
from kiro_crew.agent import _CONDUCTOR_DASHBOARD_GRANTS, _MEMBER_DASHBOARD_GRANTS
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.members import MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.wait import await_until, default_timeout

THREAD = "dashboard_member-pixel"
WORKER = "dashboard_chat-7-1785396512"
GRANDCHILD = "dashboard_chat-8-1785396600"
PLAIN = "dashboard_chat-9-1785396700"


@pytest.fixture
def bot(monkeypatch):
    """pixel's switch on; ``WORKER`` thread-born and known, ``GRANDCHILD`` known but
    opened by the worker, ``PLAIN`` an ordinary chat. Returns pixel's record."""
    record = KiroCrewAgentConfig(kiro_agent="pixel", member_id="pixel-id", worker_conducts=True)
    cfg = KiroCrewConfig()
    cfg.agents["pixel"] = record
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    members.note_crewmate_worker_session(WORKER, "pixel-id")
    members.note_crewmate_worker_session(GRANDCHILD, "pixel-id")
    members.note_worker_lineage(WORKER, "member-pixel")
    members.note_worker_lineage(GRANDCHILD, "chat-7-1785396512")
    return record


def _provider(session_key: str, backend: str = ACP_BACKEND_KAS) -> SimpleNamespace:
    return SimpleNamespace(_client=SimpleNamespace(_session_key=session_key, backend=backend))


class TestTheProviderKeysBothPopulations:
    def test_a_conducting_worker_is_keyed_like_the_thread(self, bot):
        assert AcpProvider._member_session_key(_provider(THREAD)) == THREAD
        assert AcpProvider._member_session_key(_provider(WORKER)) == WORKER

    @pytest.mark.parametrize("key", [GRANDCHILD, PLAIN, ""])
    def test_every_other_session_is_keyed_on_neither_path(self, bot, key):
        assert AcpProvider._member_session_key(_provider(key)) == ""

    def test_the_switch_off_leaves_the_worker_unkeyed(self, bot):
        bot.worker_conducts = False
        assert AcpProvider._member_session_key(_provider(WORKER)) == ""
        assert AcpProvider._member_session_key(_provider(THREAD)) == THREAD

    def test_a_dispatch_incapable_backend_keys_no_worker(self, bot):
        assert AcpProvider._member_session_key(_provider(WORKER, ACP_BACKEND_KIRO)) == ""

    @pytest.mark.parametrize("on", [True, False])
    def test_the_key_reads_the_switch_where_the_spec_reads_it(self, bot, monkeypatch, on):
        """A start the owner's save brings at once (the Bot's worker reload) can run
        before the config watcher adopts that save, so the key itself reads the switch
        from the cached load the worker's spec reads, never from the watcher's copy.
        That the start hands it no other config is ``TestTheStartReadsTheKeyOffTheLoop``;
        together they keep the mount and the spec agreeing."""
        from kiro_crew.config import live

        stale = KiroCrewConfig()
        stale.agents["pixel"] = KiroCrewAgentConfig(
            kiro_agent="pixel", member_id="pixel-id", worker_conducts=not on
        )
        monkeypatch.setattr(live, "snapshot", lambda: stale)
        bot.worker_conducts = on
        assert AcpProvider._member_session_key(_provider(WORKER)) == (WORKER if on else "")
        # What the worker's spec reads (``mate_worker.is_conducting_worker_execution``).
        assert members.conducting_worker_session(WORKER) is on
        assert AcpProvider._member_session_key(_provider(THREAD)) == THREAD


class TestTheStartReadsTheKeyOffTheLoop:
    """The key may load the config for a worker (the switch), so a session on a backend
    that can mount session control reads it in a worker thread and hands that answer
    to whichever path it runs: ``session/load`` for a resume, ``session/new``
    otherwise, and the session provider's own ``session/new`` for a new conversation.
    Every other backend (kiro) passes an empty key with no thread hop and never calls
    the read, as on main (harness-parity H13)."""

    BACKENDS = pytest.mark.parametrize(
        ("backend", "reads"),
        [(ACP_BACKEND_KAS, True), (ACP_BACKEND_KIRO, False)],
        ids=["kas", "kiro"],
    )

    @staticmethod
    def _record(monkeypatch) -> tuple[int, list[int], list[str]]:
        """The loop's thread, where each key read ran, and every ``to_thread`` target."""
        import asyncio
        import threading

        read_in: list[int] = []
        hopped: list[str] = []
        real_to_thread = asyncio.to_thread

        def key(self):
            read_in.append(threading.get_ident())
            return WORKER

        async def to_thread(func, /, *args, **kwargs):
            hopped.append(getattr(func, "__name__", ""))
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(AcpProvider, "_member_session_key", key)
        monkeypatch.setattr(asyncio, "to_thread", to_thread)
        return threading.get_ident(), read_in, hopped

    @staticmethod
    def _check(sent: str, reads: bool, loop_thread: int, read_in: list, hopped: list) -> None:
        if reads:
            assert sent == WORKER
            assert read_in and loop_thread not in read_in
            assert "key" in hopped
        else:
            # No hop, and no call at all: the key read never runs off the kiro path.
            assert "key" not in hopped
            assert read_in == []
            assert sent == ""

    @pytest.mark.asyncio
    @BACKENDS
    @pytest.mark.parametrize("resume", [False, True], ids=["start", "resume"])
    async def test_the_session_path_gets_the_key_read_off_the_loop(
        self, monkeypatch, backend, reads, resume
    ):
        from unittest.mock import AsyncMock, MagicMock, patch

        from test_acp_provider import _build_provider

        from kiro_crew.testing.ids import UNALLOCATABLE_PID

        loop_thread, read_in, hopped = self._record(monkeypatch)
        provider = _build_provider(backend=backend)
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew-mate-worker"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_socket = None
        provider._client._model = "auto"
        provider._client._resume_session_id = "abc-123" if resume else ""
        handle = MagicMock(session_id="sid-1", inherited_model="landed")
        handle.set_model = AsyncMock()
        runtime = MagicMock(pid=UNALLOCATABLE_PID)
        runtime.spawn = AsyncMock()
        runtime.create_session = AsyncMock(return_value=handle)
        runtime.load_session = AsyncMock(return_value=handle)
        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, rt, **kw: MagicMock(_handle=h, _runtime=rt, resumed=False),
            ),
            patch("pathlib.Path.exists", return_value=True),
        ):
            await provider._start_kiro_runtime()
        path = runtime.load_session if resume else runtime.create_session
        sent = path.await_args.kwargs["member_session_key"]
        self._check(sent, reads, loop_thread, read_in, hopped)

    @pytest.mark.asyncio
    @BACKENDS
    async def test_a_new_conversation_gets_the_key_read_off_the_loop(
        self, monkeypatch, backend, reads
    ):
        from unittest.mock import AsyncMock, MagicMock

        from test_acp_provider import _build_provider

        from kiro_crew.acp.session_provider import AcpSessionProvider

        loop_thread, read_in, hopped = self._record(monkeypatch)
        provider = _build_provider(backend=backend)
        client = MagicMock(spec=AcpSessionProvider)
        client.backend = backend
        client._session_key = WORKER
        client.new_conversation = AsyncMock()
        provider._client = client
        monkeypatch.setattr(provider, "_apply_initial_effort", AsyncMock())
        await provider.new_conversation()
        sent = client.new_conversation.await_args.kwargs["member_session_key"]
        self._check(sent, reads, loop_thread, read_in, hopped)


class TestTheRuntimePaths:
    """``session/new`` and ``session/load`` carry the dispatch entry for a conducting
    worker and never the panel; the KAS flag follows the mount."""

    @staticmethod
    def _wire(monkeypatch, rt):
        sent: list[tuple[str, dict]] = []
        asked: list[str] = []
        kas: list[dict] = []
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.harness.base import SessionExtras

        def _disabled(name, _agent, *, work_dir=None, session_key=""):
            asked.append(name)
            return False

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _extras(
            _agent, *, member_dispatch=False, crew_panel=False, session_key="", **_kw
        ):
            kas.append({"member_dispatch": member_dispatch, "crew_panel": crew_panel})
            return SessionExtras(custom_agents=None, derived_spec_snapshot=None)

        monkeypatch.setattr(runtime_mod, "session_mcp_server_is_disabled", _disabled)
        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _extras)
        return sent, asked, kas

    @staticmethod
    def _mounted(sent, method) -> dict[str, dict]:
        params = next(p for m, p in sent if m == method)
        return {e["name"]: e for e in params["mcpServers"]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_a_conducting_worker_gets_the_dispatch_entry_and_no_panel(
        self, bot, monkeypatch, path
    ):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent, asked, kas = self._wire(monkeypatch, rt)
        if path == "create":
            handle = await rt.create_session(
                cwd="/work", agent="kirocrew-mate-worker", member_session_key=WORKER
            )
            mounted = self._mounted(sent, METHOD_SESSION_NEW)
        else:
            handle = await rt.load_session(
                "/home/u/.kiro/sessions/cli/sid-123.json",
                "sid-123",
                cwd="/work",
                agent="kirocrew-mate-worker",
                member_session_key=WORKER,
            )
            mounted = self._mounted(sent, METHOD_SESSION_LOAD)
        assert MEMBER_DISPATCH_SERVER in mounted, sorted(mounted)
        env = {p["name"]: p["value"] for p in mounted[MEMBER_DISPATCH_SERVER]["env"]}
        assert env["KIROCREW_SESSION_KEY"] == WORKER
        assert MEMBER_PANEL_SERVER not in mounted
        # The panel's switches are not even asked for a worker: it is the thread's.
        assert asked == [MEMBER_DISPATCH_SERVER], asked
        # The KAS grant follows the mount on the create path; the load path builds
        # its payload on KAS alone and is pinned on its source below.
        if path == "create":
            assert kas == [{"member_dispatch": True, "crew_panel": False}]
        assert handle.member_dispatch_mounted is True

    def test_the_load_path_hands_the_projection_the_same_flags(self):
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        source = inspect.getsource(AcpRuntime.load_session)
        assert "member_dispatch=bool(member_session_key) and not member_withheld" in source
        assert "crew_panel=panel_mounted" in source

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    @pytest.mark.parametrize("withheld", [False, True])
    async def test_a_mirrored_hosts_projection_gets_the_starts_mount(
        self, bot, monkeypatch, path, withheld
    ):
        """A mirrored host (codex) builds the worker's spec in its projection, before
        the member entry is appended, so both paths ask the dashboard server's
        switch-off first, keyed by the session so its Bot's file counts, and hand the
        projection what this start mounts."""
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.types import ACP_BACKEND_CODEX

        rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CODEX)
        rt._initialized = True
        rt._can_load_session = True
        sent, asked, _kas = self._wire(monkeypatch, rt)
        keys: list[str] = []

        def _disabled(name, _agent, *, work_dir=None, session_key=""):
            asked.append(name)
            keys.append(session_key)
            return withheld

        monkeypatch.setattr(runtime_mod, "session_mcp_server_is_disabled", _disabled)
        handed: list = []

        class _Projected(Exception):
            """Stops the start once the projection has its answer."""

        async def _mirrored(_agent, **kwargs):
            handed.append((list(asked), kwargs.get("session_control_mounted")))
            raise _Projected

        monkeypatch.setattr(rt, "_mirrored_session_mcp", _mirrored)
        with pytest.raises(_Projected):
            if path == "create":
                await rt.create_session(
                    cwd="/work",
                    agent="kirocrew-mate-worker",
                    member_session_key=WORKER,
                    session_key=WORKER,
                )
            else:
                await rt.load_session(
                    "/home/u/.kiro/sessions/cli/sid-123.json",
                    "sid-123",
                    cwd="/work",
                    agent="kirocrew-mate-worker",
                    member_session_key=WORKER,
                    session_key=WORKER,
                )
        # Asked once, before the projection ran, for this session, and the projection
        # got its answer.
        assert handed == [([MEMBER_DISPATCH_SERVER], not withheld)]
        assert keys == [WORKER]

    @pytest.mark.asyncio
    async def test_the_thread_still_gets_both(self, bot, monkeypatch):
        rt, _, _ = _make_runtime()
        sent, asked, kas = self._wire(monkeypatch, rt)
        await rt.create_session(cwd="/work", agent="kirocrew", member_session_key=THREAD)
        assert set(asked) == {MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER}
        assert kas[0]["member_dispatch"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_an_unkeyed_session_gets_todays_array(self, bot, monkeypatch, path):
        """The provider keys a grandchild, a plain chat and a switched-off Bot's
        worker on neither path; this is what each composer then does with ``""``."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent, asked, kas = self._wire(monkeypatch, rt)
        if path == "create":
            await rt.create_session(
                cwd="/work", agent="kirocrew-mate-worker", member_session_key=""
            )
            mounted = self._mounted(sent, METHOD_SESSION_NEW)
        else:
            await rt.load_session(
                "/home/u/.kiro/sessions/cli/sid-123.json",
                "sid-123",
                cwd="/work",
                agent="kirocrew-mate-worker",
                member_session_key="",
            )
            mounted = self._mounted(sent, METHOD_SESSION_LOAD)
        assert MEMBER_DISPATCH_SERVER not in mounted
        assert MEMBER_PANEL_SERVER not in mounted
        assert asked == []
        if path == "create":
            assert kas == [{"member_dispatch": False, "crew_panel": False}]


class TestTheKasGrantSetFollowsThePopulation:
    SPEC = {
        "name": "kirocrew-mate-worker",
        "prompt": "p",
        "tools": ["fs_read"],
        "allowedTools": ["fs_read"],
        "mcpServers": {},
    }

    @staticmethod
    def _dashboard_allows(out: dict) -> set[str]:
        allowed: set[str] = set()
        for rule in (out.get("permissions") or {}).get("rules") or []:
            if rule.get("effect") == "allow" and rule.get("capability") == "mcp":
                allowed.update(
                    m for m in rule.get("match") or [] if str(m).startswith("kirocrew-dashboard/")
                )
        return allowed

    @staticmethod
    def _bare(refs) -> set[str]:
        return {ref.removeprefix("@") for ref in refs}

    def _kit_spec(self) -> dict:
        """The spec a conducting worker's start projects: the conducting kit, which
        carries the conductor-mode verbs, on its governed merge."""
        from kiro_crew.mate_worker_conducts import conducting_worker_kit

        return conducting_worker_kit(copy.deepcopy(self.SPEC))

    def test_a_conducting_worker_gets_exactly_the_conductor_mode_set(self, bot):
        out = to_client_custom_agent(
            "kirocrew-mate-worker", self._kit_spec(), "p", member_dispatch=True, session_key=WORKER
        )
        assert "@kirocrew-dashboard" in out["tools"]
        assert self._dashboard_allows(out) == self._bare(_CONDUCTOR_DASHBOARD_GRANTS)
        assert "kirocrew-dashboard/session_send" not in self._dashboard_allows(out)
        assert "kirocrew-dashboard/session_stop" not in self._dashboard_allows(out)

    def test_the_thread_takes_the_member_set(self, bot):
        out = to_client_custom_agent(
            "kirocrew-mate-conductor",
            dict(self.SPEC),
            "p",
            member_dispatch=True,
            session_key=THREAD,
        )
        assert self._dashboard_allows(out) == self._bare(_MEMBER_DASHBOARD_GRANTS)

    @pytest.mark.parametrize("key", ["", WORKER, GRANDCHILD, PLAIN])
    def test_any_other_mounted_key_takes_no_verb_from_the_projection(self, bot, key):
        """Only a member DM key takes the member set, so a mounted key the projection
        cannot place, no key included, fails closed: the server is named, so its
        calls reach the gate and ask."""
        out = to_client_custom_agent(
            "kirocrew-mate-worker", dict(self.SPEC), "p", member_dispatch=True, session_key=key
        )
        assert "@kirocrew-dashboard" in out["tools"]
        assert self._dashboard_allows(out) == set()

    def test_a_save_after_the_mount_never_widens_a_workers_set(self, bot):
        """The projection does not read the switch: a save that turns it off between
        the start's spec and this projection leaves the worker the conductor-mode set
        its spec carries, never the thread's wider member set."""
        spec = self._kit_spec()
        bot.worker_conducts = False
        out = to_client_custom_agent(
            "kirocrew-mate-worker", spec, "p", member_dispatch=True, session_key=WORKER
        )
        assert self._dashboard_allows(out) == self._bare(_CONDUCTOR_DASHBOARD_GRANTS)

    def test_no_flag_projects_no_dashboard_verb_for_a_conducting_worker(self, bot):
        out = to_client_custom_agent(
            "kirocrew-mate-worker", dict(self.SPEC), "p", session_key=WORKER
        )
        assert "@kirocrew-dashboard" not in out["tools"]
        assert self._dashboard_allows(out) == set()


class _ClientStub:
    """The attributes ``_append_member_dispatch_server`` reads (see
    ``test_member_dispatch_mount._ClientStub``)."""

    backend = ACP_BACKEND_CLAUDE
    _session_key = WORKER
    _claude_settings_authored = True
    _stub_session_token = "e" * 64
    _withhold_is_the_only_deny_channel = AcpClient._withhold_is_the_only_deny_channel
    _member_mount_withheld = AcpClient._member_mount_withheld
    _claude_settings_shared = False
    _permission_surface_share_validated = False

    @property
    def _permission_surface_governed(self):
        return ClaudeAcpClient._permission_surface_governed.fget(self)


def _base_servers() -> list[dict]:
    return [{"name": "kirocrew-core", "command": "x", "args": [], "env": [], "type": "stdio"}]


class TestTheClientComposer:
    """The per-chat client's array (claude, goose, ...) reads the same predicate, and
    a worker the client composed plain stays plain when it composes again, as the
    KAS re-inject keeps it (``TestTheReinjectLoadKeysTheWorkerToo``)."""

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_GOOSE])
    def test_a_conducting_worker_gains_the_entry(self, bot, backend):
        stub = _ClientStub()
        stub.backend = backend
        stub._claude_settings_authored = backend == ACP_BACKEND_CLAUDE
        out = AcpClient._append_member_dispatch_server(stub, _base_servers())
        assert [e["name"] for e in out] == ["kirocrew-core", MEMBER_DISPATCH_SERVER]
        env = {p["name"]: p["value"] for p in out[-1]["env"]}
        assert env["KIROCREW_SESSION_KEY"] == WORKER
        assert stub._member_dispatch_mounted is True

    @pytest.mark.parametrize("key", [GRANDCHILD, PLAIN, ""])
    def test_every_other_session_is_untouched(self, bot, key):
        stub = _ClientStub()
        stub._session_key = key
        assert AcpClient._append_member_dispatch_server(stub, _base_servers()) == _base_servers()
        assert stub._member_dispatch_mounted is False

    def test_the_switch_off_leaves_the_worker_untouched(self, bot):
        bot.worker_conducts = False
        stub = _ClientStub()
        assert AcpClient._append_member_dispatch_server(stub, _base_servers()) == _base_servers()

    @pytest.mark.parametrize(
        ("first_on", "then_on", "mounted"),
        [
            # A worker this client composed plain, its Bot's switch turned on since:
            # a respawn or relaunch inside its turn, after admission checked its
            # approval mode, keeps it plain.
            pytest.param(False, True, False, id="composed-plain-then-switched-on"),
            pytest.param(True, True, True, id="conducting-worker-respawns"),
            pytest.param(True, False, False, id="switched-off-before-the-respawn"),
        ],
    )
    def test_a_recomposition_keeps_what_the_clients_start_mounted(
        self, bot, first_on, then_on, mounted
    ):
        stub = _ClientStub()
        bot.worker_conducts = first_on
        AcpClient._append_member_dispatch_server(stub, _base_servers())
        assert stub._member_dispatch_mounted is first_on
        bot.worker_conducts = then_on
        out = AcpClient._append_member_dispatch_server(stub, _base_servers())
        assert stub._member_dispatch_mounted is mounted
        assert (out[-1]["name"] == MEMBER_DISPATCH_SERVER) is mounted

    def test_the_thread_mounts_whatever_its_last_composition_was(self, bot):
        stub = _ClientStub()
        stub._session_key = THREAD
        stub._member_dispatch_mounted = False  # e.g. a withhold since lifted
        out = AcpClient._append_member_dispatch_server(stub, _base_servers())
        assert stub._member_dispatch_mounted is True
        assert out[-1]["name"] == MEMBER_DISPATCH_SERVER

    def test_the_respawn_reset_keeps_the_composition_record(self):
        """What the composer reads survives ``_reset_state``, the reset a respawn of a
        dead or stalled child runs (``client_start.ensure_ready``) before it composes
        again."""
        client = AcpClient()
        client._process = None
        client._child_pids = {}
        client._pid = None
        client._member_dispatch_mounted = False
        client._reset_state()
        assert client.member_dispatch_mounted is False

    def test_the_panel_append_stays_the_threads(self, bot):
        stub = _ClientStub()
        assert AcpClient._append_member_panel_server(stub, _base_servers()) == _base_servers()


class TestTheReinjectLoadKeysTheWorkerToo:
    """``session_provider_mcp_reinject`` re-derives the key with the same rule, so a
    re-sent projection keeps a conducting worker's tools mid-conversation, and with
    one more test for a worker: its start must have taken the mount, so a re-inject
    never mounts the kit on a worker that started plain (its approval mode is
    checked against the Bot's Profile only at a real start). The key it hands goes
    into ``load_session``, the path ``TestTheRuntimePaths`` pins: the dispatch entry,
    no panel, so the re-inject mounts no panel either."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "mounted", "on", "expected"),
        [
            pytest.param(THREAD, False, True, THREAD, id="thread"),
            pytest.param(WORKER, True, True, WORKER, id="conducting-worker"),
            # A worker opened while the switch was off, the switch turned on since,
            # and its reload still owed: the re-inject leaves it plain.
            pytest.param(WORKER, False, True, "", id="worker-started-plain"),
            pytest.param(WORKER, True, False, "", id="switch-turned-off"),
            pytest.param(PLAIN, False, True, "", id="plain-chat"),
        ],
    )
    async def test_the_key_handed_to_load_session(
        self, bot, monkeypatch, key, mounted, on, expected
    ):
        from contextlib import nullcontext
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp import session_provider_mcp_reinject as reinject
        from kiro_crew.acp.client import AcpError

        runtime = MagicMock()
        runtime.acp_backend = ACP_BACKEND_KAS
        runtime.is_alive.return_value = True
        runtime.evict_session = AsyncMock()
        # The key is decided before the load; the load itself is stopped here, so
        # the swap that follows it (and reads the whole handle) never runs.
        runtime.load_session = AsyncMock(side_effect=RuntimeError("stop at the load"))
        runtime.keep_record_on_teardown = lambda sid: nullcontext()
        old = MagicMock(session_id="sid-1", active_agent="kirocrew-mate-worker", _bound_cwd="/w")
        old._crew_agent = None
        old.kas_folder_hooks_projected = False
        # What the session's start recorded (``runtime_session_load``/``create``).
        old.member_dispatch_mounted = mounted
        bot.worker_conducts = on
        provider = SimpleNamespace(
            _mcp_reinject_pending=True,
            _handle=old,
            _runtime=runtime,
            _session_key=key,
            _channel_id="",
            kas_memory_mode=None,
            _harness_options={},
        )
        monkeypatch.setattr(reinject, "_mcp_independent", lambda self: False)
        with pytest.raises(AcpError):
            await reinject.apply_pending_mcp_reinjection(provider)
        assert runtime.load_session.await_args.kwargs["member_session_key"] == expected

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_keys_read_keeps_the_change_pending(self, bot, monkeypatch):
        """The worker's key is read off the loop after the mark is consumed. A cancel
        landing during that read (a Stop cancels the turn task) restores the mark, so
        the MCP change is applied at the next boundary instead of being lost."""
        import asyncio
        import threading
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp import session_provider_mcp_reinject as reinject

        loop = asyncio.get_running_loop()
        reading, release = asyncio.Event(), threading.Event()

        def held_read(session_key):
            loop.call_soon_threadsafe(reading.set)
            release.wait(default_timeout())
            return True

        monkeypatch.setattr(members, "mounts_session_control", held_read)
        runtime = MagicMock()
        runtime.acp_backend = ACP_BACKEND_KAS
        runtime.is_alive.return_value = True
        runtime.evict_session = AsyncMock()
        runtime.load_session = AsyncMock()
        old = MagicMock(session_id="sid-1", active_agent="kirocrew-mate-worker", _bound_cwd="/w")
        old.kas_folder_hooks_projected = False
        # A conducting worker's handle: only one whose start took the mount reads
        # the switch again.
        old.member_dispatch_mounted = True
        provider = SimpleNamespace(
            _mcp_reinject_pending=True,
            _handle=old,
            _runtime=runtime,
            _session_key=WORKER,
            _channel_id="",
            kas_memory_mode=None,
            _harness_options={},
        )
        monkeypatch.setattr(reinject, "_mcp_independent", lambda self: False)
        apply = asyncio.ensure_future(reinject.apply_pending_mcp_reinjection(provider))
        try:
            await await_until(reading, what="the worker's key read to start")
            assert provider._mcp_reinject_pending is False  # consumed before the read
            apply.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(apply, what="the cancelled re-inject to end")
        assert provider._mcp_reinject_pending is True
        runtime.evict_session.assert_not_awaited()
        runtime.load_session.assert_not_awaited()


class TestTheSwitchTurnedOffMidTask:
    """rfc-bot-workers-conduct.md section 5.2: a conducting worker that is running when
    the owner turns the switch off keeps what its session was started with until that
    session next starts (a resume included), and is a plain worker from then on.

    The lineage mark stays. The parts read per call (the hand-off, the own-child
    exemption, the bound of 4) key on the kit the running handle took, through
    ``mate_worker_conducts.holds_conducting_kit``, so they keep it conducting until
    that start (``test_mate_worker_dispatch``, ``test_conductor_own_child_approval``,
    ``test_worker_conducts_cap``); the owner's save brings the start by reloading the
    Bot's workers (``test_crew_worker_conducts_setting``). The next start's spec and
    KAS grant set are today's (``TestOffIsTodaysSpecByteForByte``,
    ``test_no_flag_projects_no_dashboard_verb_for_a_conducting_worker``).
    """

    @pytest.mark.asyncio
    async def test_the_running_mount_stays_and_the_resume_is_plain(self, bot, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent, _asked, _kas = TestTheRuntimePaths._wire(monkeypatch, rt)
        provider = _provider(WORKER)
        running = await rt.create_session(
            cwd="/work",
            agent="kirocrew-mate-worker",
            member_session_key=AcpProvider._member_session_key(provider),
        )
        assert MEMBER_DISPATCH_SERVER in TestTheRuntimePaths._mounted(sent, METHOD_SESSION_NEW)
        assert running.member_dispatch_mounted is True

        bot.worker_conducts = False  # the owner turns the switch off mid-task
        assert members.is_thread_born_worker_session(WORKER)
        assert not members.conducting_worker_session(WORKER)
        # Nothing re-composes the running session: it keeps what its start mounted,
        # and that handle is what the parts read per call key on.
        assert running.member_dispatch_mounted is True
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.mate_worker_conducts import holds_conducting_kit

        slot = SimpleNamespace(key=WORKER.removeprefix("dashboard_"))
        live = {effective_session_key(slot): running}
        state = SimpleNamespace(sessions=SimpleNamespace(get_provider=live.get))
        assert holds_conducting_kit(state, slot)

        sent.clear()
        resumed = await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-123.json",
            "sid-123",
            cwd="/work",
            agent="kirocrew-mate-worker",
            member_session_key=AcpProvider._member_session_key(provider),
        )
        mounted = TestTheRuntimePaths._mounted(sent, METHOD_SESSION_LOAD)
        assert MEMBER_DISPATCH_SERVER not in mounted
        assert MEMBER_PANEL_SERVER not in mounted
        assert resumed.member_dispatch_mounted is False
        live[effective_session_key(slot)] = resumed
        assert not holds_conducting_kit(state, slot)
