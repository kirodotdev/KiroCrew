"""Custom ACP is an explicit experiment, never a tested harness in disguise."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.agent_sdk import backends, tool_gate
from kiro_crew.config.loader import KiroCrewConfig

CUSTOM = "custom"


class TestCustomConfiguration:
    def test_defaults_keep_kiro_and_no_custom_command(self):
        cfg = KiroCrewConfig()
        assert cfg.agent.acp_backend == backends.ACP_BACKEND_KIRO
        assert cfg.agent.custom_acp == {"command": "", "args": []}

    def test_command_and_arguments_round_trip_together(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        command = {"command": "my-agent", "args": ["--acp", "two words", "$(literal)"]}
        (tmp_path / "config.json").write_text(
            json.dumps({"agent": {"acp_backend": CUSTOM, "custom_acp": command}}),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert cfg.agent.acp_backend == CUSTOM
        assert cfg.agent.custom_acp == command

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            [],
            {"command": 5, "args": []},
            {"command": "my-agent", "args": "--acp"},
            {"command": "my-agent", "args": [False]},
            {"command": "my-agent\nother", "args": []},
            {"command": "my-agent", "args": ["bad\0arg"]},
            {"command": "my-agent", "args": [], "permission_mode": "auto"},
        ],
    )
    def test_invalid_configuration_is_rejected_as_a_whole(self, raw):
        from kiro_crew.agent_sdk.custom_acp import validate_custom_acp

        with pytest.raises(ValueError):
            validate_custom_acp(raw)

    def test_argument_bound_is_enforced(self):
        from kiro_crew.agent_sdk.custom_acp import MAX_ARGUMENTS, validate_custom_acp

        with pytest.raises(ValueError):
            validate_custom_acp({"command": "my-agent", "args": ["x"] * (MAX_ARGUMENTS + 1)})

    def test_an_empty_argument_and_spaces_are_not_shell_parsed(self):
        from kiro_crew.agent_sdk.custom_acp import validate_custom_acp

        config = {"command": "my-agent", "args": ["", " a b ", ";", "'quoted'"]}
        assert validate_custom_acp(config) == config


class TestCustomAdmission:
    def test_custom_is_selectable_but_never_verified(self):
        assert CUSTOM == backends.ACP_BACKEND_CUSTOM
        assert CUSTOM in backends.BASELINE_SELECTABLE_BACKENDS
        assert backends.routing_for(CUSTOM) is backends.Routing.UNVERIFIED
        assert tool_gate.routing_verdict(CUSTOM)[0] is tool_gate.Verdict.INDETERMINATE
        assert not tool_gate.is_enforced(CUSTOM)

    def test_custom_does_not_inherit_native_privileges(self):
        assert CUSTOM not in backends.ACP_BACKENDS_INTERNAL_SANDBOX
        assert CUSTOM not in backends.ACP_BACKENDS_HOST_AUTH_CALLBACK
        assert CUSTOM not in backends.ACP_BACKENDS_SIDE_READONLY
        assert CUSTOM not in backends.ACP_BACKENDS_MEMBER_CAPABILITIES
        assert CUSTOM not in backends.ACP_BACKENDS_ACP_RUNTIME
        assert backends.model_registry_namespace(CUSTOM) == CUSTOM
        assert backends.POLICY_ID_BY_BACKEND[CUSTOM] == CUSTOM

    def test_other_unverified_backends_still_cannot_register(self, monkeypatch):
        monkeypatch.setitem(
            backends.ACP_BACKEND_ROUTING, backends.ACP_BACKEND_CLAUDE, backends.Routing.UNVERIFIED
        )
        with pytest.raises(ValueError, match="unverified"):
            backends.register_selectable_backend(backends.ACP_BACKEND_CLAUDE)

    def test_custom_receives_the_existing_credential_mask_without_carveouts(self, monkeypatch):
        from kiro_crew import security

        seen = []

        def targets(excluded):
            seen.extend(excluded)
            return ("/synthetic/home/.ssh", "/synthetic/home/.aws")

        monkeypatch.setattr(security, "sandbox_credential_targets", targets)
        hidden = tool_gate.adapter_hidden_credential_dirs(CUSTOM)
        assert hidden == ("/synthetic/home/.ssh", "/synthetic/home/.aws")
        assert not any(leaf in seen for leaf in (".ssh", ".aws", ".codex/auth.json"))
        assert tool_gate.adapter_expose_files(CUSTOM, hidden) == ()

    @pytest.mark.parametrize("mode", ["auto", "off"])
    def test_custom_refuses_when_credential_isolation_cannot_apply(self, monkeypatch, mode):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
        with pytest.raises(tool_gate.ToolGateUnroutable, match="Custom ACP"):
            tool_gate.enforce_sandbox_floor(CUSTOM, mode)

    def test_working_credential_isolation_allows_custom(self, monkeypatch):
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)
        tool_gate.enforce_sandbox_floor(CUSTOM, "auto")

    @pytest.mark.asyncio
    async def test_denied_custom_never_resolves_or_starts_a_command(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import AcpClient, AcpError
        from kiro_crew.agent_sdk import custom_acp

        monkeypatch.setattr(backends, "_selectable", {backends.ACP_BACKEND_KIRO})
        resolve = AsyncMock(side_effect=AssertionError("denied command must not be resolved"))
        monkeypatch.setattr(custom_acp, "resolve_custom_acp", resolve)
        spawn = AsyncMock(side_effect=AssertionError("denied command must not start"))
        monkeypatch.setattr(client_mod, "create_subprocess_limited", spawn)
        client = AcpClient(work_dir=tmp_path, acp_backend=CUSTOM)
        with pytest.raises(AcpError, match="not allowed"):
            await client._spawn()
        resolve.assert_not_called()
        spawn.assert_not_called()


class TestCustomLaunchResolution:
    def test_resolves_argv_without_executing_the_command(self, tmp_path, monkeypatch):
        from kiro_crew.agent_sdk import custom_acp

        cfg = KiroCrewConfig()
        cfg.agent.custom_acp = {"command": "my-agent", "args": ["--acp", " a b ", ";"]}
        monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
        monkeypatch.setattr(custom_acp.shutil, "which", lambda command, path: "/opt/bin/my-agent")
        assert custom_acp.resolve_custom_acp() == ["/opt/bin/my-agent", "--acp", " a b ", ";"]

    def test_missing_executable_has_an_actionable_error(self, monkeypatch):
        from kiro_crew.agent_sdk import custom_acp

        cfg = KiroCrewConfig()
        cfg.agent.custom_acp = {"command": "missing-agent", "args": []}
        monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
        monkeypatch.setattr(custom_acp.shutil, "which", lambda command, path: None)
        with pytest.raises(ValueError, match="executable.*not found"):
            custom_acp.resolve_custom_acp()

    def test_no_command_is_not_a_native_kiro_fallback(self, monkeypatch):
        from kiro_crew.agent_sdk import custom_acp

        monkeypatch.setattr(KiroCrewConfig, "load", lambda: KiroCrewConfig())
        with pytest.raises(ValueError, match="Configure"):
            custom_acp.resolve_custom_acp()


class TestCustomSpawnBehavior:
    """Drive the real spawn arm while recording, never executing, its argv."""

    def _capture_client(self, tmp_path, argv=None):
        import os
        from contextlib import ExitStack
        from unittest.mock import patch

        import acp_launch_capture as capture_mod

        from kiro_crew.acp import client as client_mod
        from kiro_crew.agent_sdk import custom_acp

        real_bundle_spawn = client_mod.apply_pod_bundle_spawn
        rec = capture_mod._Recorder()
        saved = capture_mod.snapshot_bin_caches()
        capture_mod._reset_bin_caches()
        patches = [patch.dict(os.environ, capture_mod.fixed_parent_env(), clear=True)]
        capture_mod._stub_common(patches, rec, tmp_path, CUSTOM)
        try:
            with ExitStack() as stack:
                for context in patches:
                    stack.enter_context(context)
                # Keep the identity decision real; only the OS wrapper is a double.
                stack.enter_context(
                    patch.object(client_mod, "apply_pod_bundle_spawn", real_bundle_spawn)
                )
                wrap = stack.enter_context(
                    patch.object(client_mod, "wrap_argv_async", wraps=client_mod.wrap_argv_async)
                )
                if argv is not None:
                    stack.enter_context(
                        patch.object(custom_acp, "resolve_custom_acp", return_value=argv)
                    )
                client = client_mod.AcpClient(
                    work_dir=tmp_path / "workspace",
                    session_key="custom-session",
                    acp_backend=CUSTOM,
                    model="auto",
                )
                client._mcp_gateway_overlay = object()
                client._resume_session_id = "resume-me"
                client._model = "some-pinned-model"
                asyncio.run(client._spawn())
                return client, rec, wrap.call_args.kwargs
        finally:
            capture_mod.restore_bin_caches(saved)

    def test_spawn_hands_the_factory_the_resolved_argv(self, tmp_path):
        import acp_launch_capture as capture_mod

        _client, rec, _wrapper = self._capture_client(tmp_path)
        assert rec.argv == list(capture_mod._CUSTOM_ACP_ARGV)
        assert rec.spawn_label == rec.stderr_label == "Custom ACP"

    def test_spawn_disables_the_unverified_integrations(self, tmp_path):
        client, _rec, _wrapper = self._capture_client(tmp_path)
        assert client._mcp_gateway_overlay is None
        assert client._resume_session_id is None
        assert client._model == "auto"

    def test_custom_never_delegates_the_internal_sandbox(self, tmp_path):
        argv = ["/opt/bin/kiro-cli", "acp", "", "two words"]
        client, rec, wrapper = self._capture_client(tmp_path, argv)
        assert client.backend == CUSTOM
        assert rec.argv == argv
        assert wrapper["is_kiro_cli"] is False
        assert wrapper["extra_expose_files"] == ()
        assert "--agent" not in rec.argv


class TestCustomModelsAndProbe:
    """Two read paths that must never touch the custom command or Kiro's account."""

    @pytest.mark.asyncio
    async def test_api_models_returns_empty_and_never_resolves_kiro(self, monkeypatch):
        """GET /api/models for custom is ``[]`` with no Kiro spawn and no command run.

        Custom uses the harness's own model configuration, so opening the picker must
        neither execute the configured command nor query Kiro's account. The Kiro
        binary resolver is stubbed to fail the test if it is reached.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.dashboard.handlers import agents as agents_mod

        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = CUSTOM
        monkeypatch.setattr(agents_mod.KiroCrewConfig, "load", lambda: cfg)

        async def _forbidden(*args, **kwargs):
            raise AssertionError("custom api_models must not resolve the Kiro binary")

        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _forbidden)

        response = await agents_mod.api_models(_StubRequest())
        assert response.status == 200
        assert json.loads(response.text) == []

    def test_probe_resolves_the_filesystem_and_never_runs_the_command(self, monkeypatch):
        """Install-probing custom is a PATH resolution, never a subprocess.

        ``_probe_custom`` decides INSTALLED vs MISSING solely from
        ``resolve_custom_acp`` -- a ``shutil.which`` lookup that never executes the
        configured command (proven by ``TestCustomLaunchResolution``). This pins that
        the probe's verdict is exactly that resolver's outcome and that the probe adds
        no execution of its own: the resolver is replaced with a recording stub, so a
        probe that shelled out would have to call something other than it.
        """
        from kiro_crew.agent_sdk import backend_install, custom_acp

        calls = []

        def _resolve_ok():
            calls.append("resolve")
            return ["/opt/bin/my-agent"]

        monkeypatch.setattr(custom_acp, "resolve_custom_acp", _resolve_ok)
        installed = backend_install._probe_custom()
        assert installed.installed == backend_install.INSTALLED
        assert installed.backend == CUSTOM
        assert calls == ["resolve"], "the probe reached the command another way"

        def _resolve_missing():
            raise ValueError("not configured")

        monkeypatch.setattr(custom_acp, "resolve_custom_acp", _resolve_missing)
        missing = backend_install._probe_custom()
        assert missing.installed == backend_install.MISSING


class _StubRequest:
    """Minimal stand-in for the aiohttp request ``api_models`` reads.

    ``api_models`` reaches the empty-list return for custom before it touches the
    request at all, so nothing on it needs to be real. It exists only so the call
    signature is satisfied without standing up a dashboard app.
    """

    app: dict = {}


# ── The inline config PATCH handler ──────────────────────────────────────────


class TestCustomAcpConfigPatch:
    """The real ``PATCH /api/config/kirocrew`` path for ``agent.custom_acp``.

    Driven through the same aiohttp handler ``test_config_patch.py`` uses, so the
    pair is validated by the shipped ``custom_acp`` spec branch rather than by a
    stand-in: the atomic command+args write, the governance-denied 403, the
    whole-pair refusal, and the SEL line that must never carry the arguments.
    """

    _SEED = {
        "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
        "default_agent": "kirocrew",
        "agent": {"acp_backend": "", "custom_acp": {"command": "", "args": []}},
    }

    def _app(self):
        from aiohttp import web

        from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
        return app

    @pytest.fixture
    def cfg_file(self, tmp_path, monkeypatch):
        from unittest.mock import patch as _patch

        path = tmp_path / "config.json"
        path.write_text(json.dumps(self._SEED), encoding="utf-8")
        with _patch("kiro_crew.config.loader.config_path", return_value=path):
            yield path

    @staticmethod
    async def _patch(client, value):
        return await client.patch(
            "/api/config/kirocrew", json={"path": "agent.custom_acp", "value": value}
        )

    @staticmethod
    def _written(cfg_file) -> dict:
        return json.loads(cfg_file.read_text(encoding="utf-8"))["agent"]["custom_acp"]

    @pytest.mark.asyncio
    async def test_command_and_args_are_written_as_one_atomic_pair(self, cfg_file):
        """Spaces, an empty argument and a literal ``$(...)`` all survive verbatim.

        The pair is stored as one value, never shell-parsed, so an argument that
        would be word-split or command-substituted by a shell is kept as its own
        string.
        """
        from aiohttp.test_utils import TestClient, TestServer

        value = {"command": "my-agent", "args": ["--acp", "two words", "", "$(literal)", ";"]}
        async with TestClient(TestServer(self._app())) as client:
            resp = await self._patch(client, value)
            assert resp.status == 200
        assert self._written(cfg_file) == value

    @pytest.mark.asyncio
    async def test_a_denied_custom_is_a_403_and_never_writes_config(self, cfg_file, monkeypatch):
        """A deployment that forbids Custom refuses the write before touching disk."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.agent_sdk import backends

        monkeypatch.setattr(backends, "_selectable", {backends.ACP_BACKEND_KIRO})
        before = cfg_file.read_bytes()
        async with TestClient(TestServer(self._app())) as client:
            resp = await self._patch(client, {"command": "my-agent", "args": []})
            assert resp.status == 403
            assert "not allowed" in (await resp.json())["error"]
        # The refusal precedes the read-modify-write entirely.
        assert cfg_file.read_bytes() == before

    @pytest.mark.asyncio
    async def test_an_invalid_pair_is_a_400_and_writes_nothing(self, cfg_file):
        """A pair validation rejects is refused whole; no partial field lands.

        ``args`` present with an empty ``command`` is the whole-pair rejection: a
        writer that saved ``command`` first would leave a half-written record.
        """
        from aiohttp.test_utils import TestClient, TestServer

        before = cfg_file.read_bytes()
        async with TestClient(TestServer(self._app())) as client:
            resp = await self._patch(client, {"command": "", "args": ["--acp"]})
            assert resp.status == 400
        assert cfg_file.read_bytes() == before
        # And a shape that is not the pair at all is refused the same way.
        async with TestClient(TestServer(self._app())) as client:
            resp = await self._patch(client, {"command": "my-agent"})
            assert resp.status == 400
        assert cfg_file.read_bytes() == before

    @pytest.mark.asyncio
    async def test_the_audit_line_never_carries_the_arguments(self, cfg_file, monkeypatch):
        """Custom's SEL line logs the path key alone, since an argument can be private.

        Every other editable field logs ``<path>=<value>``; the custom_acp branch
        logs the bare key so a submitted argument (a token, a URL) never reaches the
        audit sink. Asserted on the recorded ``resources`` rather than on wording.
        """
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard.handlers import core as core_mod

        recorded: list[str] = []

        class _Sel:
            def log_api_access(self, *, resources, **_kw):
                recorded.append(resources)

        monkeypatch.setattr(core_mod, "_sel", lambda: _Sel())
        secret_arg = "--token=SUPER-SECRET-VALUE"
        async with TestClient(TestServer(self._app())) as client:
            resp = await self._patch(client, {"command": "my-agent", "args": [secret_arg]})
            assert resp.status == 200
        assert recorded, "the write emitted no SEL line"
        assert "agent.custom_acp" in recorded
        assert not any("SUPER-SECRET-VALUE" in line for line in recorded)


# ── A real AcpClient over a fake JSON-RPC transport ───────────────────────────


class _FakeAcpTransport:
    """A scripted JSON-RPC peer for a real ``AcpClient``, with no subprocess.

    The client reads a line at a time off ``stdout.readline`` and writes requests
    to ``stdin.write``; this records every request and answers ``initialize`` and
    ``session/new`` from a fixed script, so ``_initialize_session`` completes
    against a numeric protocol version with no kiro-cli, no adapter, and no cloud
    model. A prompt's stream and a voluntary permission request are then fed as
    the client's own reader would see them.
    """

    def __init__(self, session_id: str = "sess-custom-1") -> None:
        self.requests: list[dict] = []
        self._session_id = session_id
        self._outbound: "deque[bytes]" = deque()

    # -- stdin side: the client writes framed JSON-RPC here --
    def write(self, data: bytes) -> None:
        msg = json.loads(data.decode())
        self.requests.append(msg)
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            self._reply(req_id, {"protocolVersion": msg["params"]["protocolVersion"]})
        elif method == "session/new":
            self._reply(req_id, {"sessionId": self._session_id})
        elif method == "session/prompt":
            self.feed(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": self._session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "hi"},
                        },
                    },
                }
            )
            self._reply(req_id, {"stopReason": "end_turn"})

    async def drain(self) -> None:
        return None

    # -- stdout side: the client reads framed JSON-RPC from here --
    async def readline(self) -> bytes:
        if self._outbound:
            return self._outbound.popleft()
        return b""

    def _reply(self, req_id, result) -> None:
        self._outbound.append(
            (json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n").encode()
        )

    def feed(self, message: dict) -> None:
        self._outbound.append((json.dumps(message) + "\n").encode())


def _custom_client_over_fake(tmp_path):
    """A real custom ``AcpClient`` wired to a fake transport, ready to drive."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path, acp_backend=CUSTOM, session_key="k")
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 99_999_999_999
    transport = _FakeAcpTransport()
    proc.stdin = transport
    proc.stdout = transport
    proc.stderr = None
    client._process = proc
    return client, transport


class TestCustomOverFakeTransport:
    """The wire path for a custom session: handshake, prompt, cancel, permission.

    A real ``AcpClient`` over a scripted transport, so the numeric protocol
    version, the session lifecycle and the VOLUNTARY permission dispatch through
    the existing gate are exercised without invoking a real executable or a cloud
    model.
    """

    @pytest.mark.asyncio
    async def test_initialize_sends_the_numeric_protocol_version(self, tmp_path):
        """Custom speaks standard ACP v1: ``protocolVersion`` is the integer 1."""
        from kiro_crew.acp.client import _PROTOCOL_VERSION_BY_BACKEND

        client, transport = _custom_client_over_fake(tmp_path)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert client._session_id == "sess-custom-1"
        init = next(r for r in transport.requests if r.get("method") == "initialize")
        assert init["params"]["protocolVersion"] == 1
        assert _PROTOCOL_VERSION_BY_BACKEND[CUSTOM] == 1
        # A custom session establishes its own agent, so no set_mode/set_model is
        # sent -- the model stays the harness's own default.
        assert not any(
            r.get("method") in ("session/set_mode", "session/set_model") for r in transport.requests
        )

    @pytest.mark.asyncio
    async def test_a_prompt_streams_text_then_completes(self, tmp_path):
        """session/prompt over the fake transport yields the streamed text.

        ``ensure_ready`` and ``_send_prompt`` are stubbed so the loop runs against a
        scripted stream; the point is that a custom session's ordinary turn drives
        the same dispatch every backend uses.
        """
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        client, transport = _custom_client_over_fake(tmp_path)
        client._drain_notifications = AsyncMock()
        await asyncio.wait_for(client._initialize_session(), timeout=5)

        async def collect():
            return [event async for event in client.stream_events("do it")]

        events = await asyncio.wait_for(collect(), timeout=5)
        prompts = [r for r in transport.requests if r.get("method") == "session/prompt"]
        assert len(prompts) == 1
        assert prompts[0]["params"]["sessionId"] == "sess-custom-1"
        kinds = [e.kind for e in events]
        assert EVENT_TEXT_CHUNK in kinds
        assert kinds[-1] == EVENT_COMPLETE
        assert events[0].text == "hi"

    @pytest.mark.asyncio
    async def test_cancel_writes_a_session_cancel_notification(self, tmp_path):
        """cancel_session sends ``session/cancel`` for the live session id."""
        client, transport = _custom_client_over_fake(tmp_path)
        client._session_id = "sess-custom-1"

        await client.cancel_session()

        assert client._cancelled is True
        cancels = [r for r in transport.requests if r.get("method") == "session/cancel"]
        assert cancels and cancels[0]["params"]["sessionId"] == "sess-custom-1"

    @pytest.mark.asyncio
    async def test_a_voluntary_permission_request_dispatches_through_the_gate(self, tmp_path):
        """A tool call the harness DOES route reaches the existing permission event.

        Custom's routing is unverified, so nothing forces a permission request --
        but when the harness voluntarily sends one, it must flow through the same
        ``session/request_permission`` dispatch as every other backend and yield a
        permission event the gate can answer.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
        )

        client, transport = _custom_client_over_fake(tmp_path)
        client._session_id = "sess-custom-1"
        transport.feed(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sess-custom-1",
                    "toolCall": {"title": "shell", "toolCallId": "tc1", "kind": "execute"},
                    "options": [
                        {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            }
        )
        transport._reply(1, {"stopReason": "end_turn"})

        async def collect():
            events = []
            async for event in client._dispatch_events(req_id=1, timeout=5.0):
                events.append(event)
                if event.kind == EVENT_PERMISSION_REQUEST:
                    await client.reject_tool(event.request_id)
            return events

        events = await asyncio.wait_for(collect(), timeout=5)
        kinds = [e.kind for e in events]
        assert EVENT_PERMISSION_REQUEST in kinds
        assert EVENT_COMPLETE in kinds
        perm_event = next(e for e in events if e.kind == EVENT_PERMISSION_REQUEST)
        assert perm_event.request_id == 7
        assert perm_event.title == "shell"
        reply = next(r for r in transport.requests if r.get("id") == 7)
        assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "reject"}


# ── The spawn arm's refusal ordering ─────────────────────────────────────────


class TestCustomSpawnRefusalOrdering:
    """The isolation floor is checked BEFORE the command is resolved or started.

    An arbitrary executable must never be resolved -- let alone started -- on a
    host where the credential mask cannot be applied, so ``_sandbox_preflight``
    runs ahead of ``resolve_custom_acp`` and the process factory in the custom arm.
    """

    def _custom_arm(self) -> str:
        import inspect

        from kiro_crew.acp.client import AcpClient

        body = inspect.getsource(AcpClient._spawn)
        arm = body.split("elif self.backend == ACP_BACKEND_CUSTOM:", 1)[1]
        return arm.split("        else:", 1)[0]

    def test_the_preflight_precedes_the_resolver_in_the_arm(self):
        arm = self._custom_arm()
        preflight_at = arm.find("_sandbox_preflight")
        # The resolver is IMPORTED at the top of the arm; the ordering that matters
        # is the CALL site, which resolves the command only after the floor passed.
        resolve_at = arm.find("asyncio.to_thread(resolve_custom_acp)")
        assert preflight_at != -1 and resolve_at != -1
        assert preflight_at < resolve_at

    @pytest.mark.asyncio
    async def test_unavailable_isolation_refuses_before_resolving_the_command(
        self, tmp_path, monkeypatch
    ):
        """A refused sandbox floor stops the arm before the resolver runs.

        ``enforce_sandbox_floor`` is made to refuse; ``resolve_custom_acp`` and the
        process factory are booby-trapped, so reaching either fails the test rather
        than passing quietly.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
        from kiro_crew.agent_sdk import custom_acp, tool_gate

        def _refuse(backend, mode):
            raise tool_gate.ToolGateUnroutable("Custom ACP cannot be isolated here")

        monkeypatch.setattr(tool_gate, "enforce_sandbox_floor", _refuse)
        monkeypatch.setattr(
            custom_acp,
            "resolve_custom_acp",
            MagicMock(side_effect=AssertionError("resolver ran despite a refused floor")),
        )
        monkeypatch.setattr(
            client_mod,
            "create_subprocess_limited",
            AsyncMock(side_effect=AssertionError("a process was started despite a refused floor")),
        )
        client = AcpClient(work_dir=tmp_path, acp_backend=CUSTOM, session_key="k")
        with pytest.raises(AcpToolGateUnroutable):
            await client._spawn()


def test_relative_path_resolution_is_anchored_before_session_cwd(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import custom_acp

    gateway_cwd = tmp_path / "gateway"
    gateway_cwd.mkdir()
    monkeypatch.chdir(gateway_cwd)
    cfg = KiroCrewConfig()
    cfg.agent.custom_acp = {"command": "my-agent", "args": ["--acp", ""]}
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(custom_acp.shutil, "which", lambda command, path: "./my-agent")

    argv = custom_acp.resolve_custom_acp()
    assert argv == [str(gateway_cwd / "my-agent"), "--acp", ""]
