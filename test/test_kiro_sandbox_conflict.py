"""kiro-cli's own sandbox stops Kiro Crew's MCP servers from starting.

With ``"sandbox": true`` in ``~/.kiro/settings/amazon-internal.json`` on macOS,
kiro-cli confines itself and the MCP servers it starts. ``kirocrew-core`` fails to
start there, so every session ends with kiro-cli's own words::

    JSON-RPC error: {'code': -32603, 'message': 'Internal error', 'data':
    {'details': 'mcp-client(kirocrew-core): initial connection or tool
    synchronization failed'}}

and ``kirocrew doctor`` was all green, because its MCP probe starts the server
outside that sandbox. These tests pin both surfaces naming the setting and the fix:
the session's start error, and a doctor row that is fatal only when a selected
harness actually runs kiro-cli.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_runtime_helpers import (
    _await_pending,
    _feed,
    _make_runtime,
    _start_reader,
    _stop_reader,
)

from kiro_crew import cli_doctor, kiro_sandbox_conflict, sandbox
from kiro_crew.acp import kas_route
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_NEW, METHOD_SET_MODEL
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KIRO,
    STORED_BACKEND_KIRO,
)
from kiro_crew.doctor_checks import confinement
from kiro_crew.testing.wait import await_until

#: Rule 2 as shipped, captured at import, before conftest's per-test floor closes it.
_REAL_AUTOMATIC_SWITCH_ALLOWED = kas_route.automatic_switch_allowed

#: The reporter's error ``data``, verbatim from the issue.
_REPORTED = (
    "{'code': -32603, 'message': 'Internal error', 'data': {'details': "
    "'mcp-client(kirocrew-core): initial connection or tool synchronization failed'}}"
)


def _host(monkeypatch: pytest.MonkeyPatch, *, platform: str, enabled: bool, tier="auto") -> None:
    # The module's own ``sys`` binding, not the global ``sys.platform``: a global
    # patch reaches every import a teardown runs on this host's real platform.
    monkeypatch.setattr(
        kiro_sandbox_conflict, "sys", SimpleNamespace(platform=platform), raising=True
    )
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: enabled)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: tier)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _kas_route_on_host(
    monkeypatch: pytest.MonkeyPatch, *, saved: bool | None, build_prefers_bundle: bool
) -> None:
    """Pin every input of ``resolve_kas_route`` but the saved setting, the build's
    preference and the Bot pin: installation backend KAS, Crew signed in, and a
    usable kiro-cli relay (found and signed in)."""
    from kiro_crew.acp import client as acp_client
    from kiro_crew.acp import kas_build_default
    from kiro_crew.auth import bridge
    from kiro_crew.config import loader_document
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.kas_bundle = bool(saved)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
    monkeypatch.setattr(loader_document, "kas_bundle_declared", lambda: saved is not None)
    monkeypatch.setattr(
        kas_build_default, "kas_bundle_preferred_by_build", lambda: build_prefers_bundle
    )
    monkeypatch.setattr(kas_build_default, "first_switch_cooling_down", lambda: False)
    monkeypatch.setattr(kas_build_default, "engine_has_run", lambda: False)
    monkeypatch.setattr(kas_route, "sharing_preview_selects_bundle", lambda **_: False)
    # conftest closes the switch for every test; reopen it with rule 2 as written,
    # for an installation whose stored backend is KAS (not a Kiro CLI pick).
    monkeypatch.setattr(kas_route, "_stored_as_kiro_pick", lambda: False)
    monkeypatch.setattr(
        kas_route, "automatic_switch_allowed", _REAL_AUTOMATIC_SWITCH_ALLOWED, raising=True
    )
    monkeypatch.setattr(kas_route, "_crew_signed_in", lambda: True)
    monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda **_: "/opt/kiro-cli")
    monkeypatch.setattr(bridge, "vault_holds_identity", lambda: True)
    monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *_: False)
    monkeypatch.setattr(kas_route, "relay_unusable_reason", lambda **_: "")


class TestSessionStartHint:
    def test_names_the_setting_and_the_fix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)

        hint = kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KIRO)

        assert hint is not None
        settings_path, key = sandbox.kiro_internal_sandbox_switch()
        assert settings_path in hint
        assert f'"{key}": true' in hint
        assert f'Set "{key}" to false' in hint
        assert "start a new session" in hint
        assert "kirocrew-core" in hint

    def test_silent_when_the_setting_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same error has other causes; without the setting, claim none."""
        _host(monkeypatch, platform="darwin", enabled=False)

        assert kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KIRO) is None

    def test_silent_off_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, platform="linux", enabled=True)

        assert kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KIRO) is None

    def test_silent_for_a_third_party_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)
        other = _REPORTED.replace("kirocrew-core", "github")

        assert kiro_sandbox_conflict.session_start_hint(other, ACP_BACKEND_KIRO) is None

    def test_names_the_crew_server_that_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)
        sibling = _REPORTED.replace("kirocrew-core", "kirocrew-cron")

        hint = kiro_sandbox_conflict.session_start_hint(sibling, ACP_BACKEND_KIRO)

        assert hint is not None and "mcp-client(kirocrew-cron)" in hint

    def test_tier_off_never_gets_the_bare_advice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With agent.sandbox off, the plain advice leaves no confinement at all."""
        _host(monkeypatch, platform="darwin", enabled=True, tier="off")

        hint = _flat(kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KIRO) or "")

        assert 'Set agent.sandbox to "auto" first' in hint
        assert "no OS confinement at all" in hint
        assert 'Do NOT just set "sandbox" to false' in hint

    def test_unreadable_tier_gets_the_cautious_wording(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)

        def _boom(mode: str) -> str:
            raise RuntimeError("governance profile could not be composed")

        monkeypatch.setattr(sandbox, "effective_sandbox_mode", _boom)

        hint = _flat(kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KIRO) or "")

        assert 'check that agent.sandbox is not "off"' in hint
        assert "could not be read" in hint
        assert 'Set "sandbox" to false to hand isolation' not in hint

    def test_silent_for_a_harness_kiro_cli_does_not_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A harness that launches on its own can fail to start a Crew server too."""
        _host(monkeypatch, platform="darwin", enabled=True)

        for backend in (ACP_BACKEND_DEEPSEEK, ACP_BACKEND_CLAUDE):
            assert kiro_sandbox_conflict.session_start_hint(_REPORTED, backend) is None

    @pytest.mark.parametrize(
        ("saved", "build_prefers_bundle", "route_reason", "hinted"),
        [
            # Explicit true: the configured engine, never kiro-cli.
            (True, False, kas_route.REASON_CONFIGURED, False),
            # Unset on a build that prefers the engine: the bundle, no kiro-cli.
            (None, True, kas_route.REASON_BUILD_DEFAULT, False),
            # Unset on a build that does not: kiro-cli's relay.
            (None, False, "", True),
            # Explicit false wins over the build's preference: kiro-cli's relay.
            (False, True, "", True),
        ],
    )
    def test_kas_is_hinted_only_on_the_route_the_spawn_takes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        saved: bool | None,
        build_prefers_bundle: bool,
        route_reason: str,
        hinted: bool,
    ) -> None:
        """Relay KAS runs inside kiro-cli; Kiro Crew's own KAS bundle does not.

        The route is the spawn's own (``resolve_kas_route``), not the
        ``agent.kas_bundle`` setting alone: an insider or stable build runs the
        bundle with the setting unset.
        """
        _host(monkeypatch, platform="darwin", enabled=True)
        _kas_route_on_host(monkeypatch, saved=saved, build_prefers_bundle=build_prefers_bundle)
        assert kas_route.resolve_kas_route().reason == route_reason

        hint = kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KAS)

        assert (hint is not None) is hinted

    def test_kas_bot_pinned_to_kiro_cli_is_hinted_on_a_bundle_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Bot pinned to the Kiro CLI keeps the relay where the installation
        takes the bundle, so its failed start still gets the hint."""
        _host(monkeypatch, platform="darwin", enabled=True)
        _kas_route_on_host(monkeypatch, saved=None, build_prefers_bundle=True)
        assert kas_route.resolve_kas_route().bundle
        assert not kas_route.resolve_kas_route(pin=STORED_BACKEND_KIRO).bundle

        pinned = kiro_sandbox_conflict.session_start_hint(
            _REPORTED, ACP_BACKEND_KAS, STORED_BACKEND_KIRO
        )
        unpinned = kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KAS)

        assert pinned is not None
        assert unpinned is None

    def test_kas_unresolvable_route_gives_no_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A route that cannot be resolved is no evidence KAS runs inside kiro-cli."""
        _host(monkeypatch, platform="darwin", enabled=True)

        def _boom(**_: object) -> kas_route.KasRoute:
            raise RuntimeError("route could not be resolved")

        monkeypatch.setattr(kas_route, "resolve_kas_route", _boom)

        assert kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KAS) is None

    def test_unreadable_setting_gives_no_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)

        def _boom() -> bool:
            raise RuntimeError("home directory could not be resolved")

        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", _boom)

        assert kiro_sandbox_conflict.session_start_hint(_REPORTED, ACP_BACKEND_KIRO) is None


def _error_frame(req_id: int) -> MagicMock:
    msg = MagicMock()
    msg.is_response_for.return_value = True
    msg.error = {
        "code": -32603,
        "message": "Internal error",
        "data": {
            "details": (
                "mcp-client(kirocrew-core): initial connection or tool synchronization failed"
            )
        },
    }
    msg.result = None
    msg.method = None
    msg.id = req_id
    return msg


class TestSessionStartErrorCarriesTheHint:
    """The error the user sees at session start names the cause."""

    @pytest.mark.asyncio
    async def test_session_new_error_names_the_setting(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)
        client = AcpClient(work_dir=tmp_path)
        frame = _error_frame(7)

        async def _one(timeout=0.0):
            return frame

        client._read_message = _one  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(7, timeout=1.0, method=METHOD_SESSION_NEW)

        emitted = _flat(str(excinfo.value))
        settings_path, key = sandbox.kiro_internal_sandbox_switch()
        assert emitted.startswith("JSON-RPC error: ")
        assert "mcp-client(kirocrew-core)" in emitted
        assert f'{settings_path} sets "{key}": true' in emitted
        assert f'Set "{key}" to false' in emitted

    @pytest.mark.asyncio
    async def test_not_on_a_non_start_request(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only session start reports this cause; a later request is unchanged."""
        _host(monkeypatch, platform="darwin", enabled=True)
        client = AcpClient(work_dir=tmp_path)
        frame = _error_frame(8)

        async def _one(timeout=0.0):
            return frame

        client._read_message = _one  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(8, timeout=1.0, method=METHOD_SET_MODEL)

        assert "Likely cause" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_not_on_a_harness_kiro_cli_does_not_host(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The client passes its own backend: a DeepSeek session gets no hint."""
        _host(monkeypatch, platform="darwin", enabled=True)
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)
        frame = _error_frame(10)

        async def _one(timeout=0.0):
            return frame

        client._read_message = _one  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(10, timeout=1.0, method=METHOD_SESSION_NEW)

        assert "Likely cause" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_unchanged_when_the_setting_is_off(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _host(monkeypatch, platform="darwin", enabled=False)
        client = AcpClient(work_dir=tmp_path)
        frame = _error_frame(9)

        async def _one(timeout=0.0):
            return frame

        client._read_message = _one  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(9, timeout=1.0, method=METHOD_SESSION_NEW)

        assert "Likely cause" not in str(excinfo.value)


_RUNTIME_ERROR = {
    "code": -32603,
    "message": "Internal error",
    "data": {
        "details": "mcp-client(kirocrew-core): initial connection or tool synchronization failed"
    },
}


async def _runtime_session_error(*, backend: str = "", pin: str = "") -> AcpRuntimeError:
    """Start a session on the shared runtime and answer ``session/new`` with the error."""
    rt, reader, _ = _make_runtime()
    if backend:
        rt._acp_backend = backend
    rt._harness_pin = pin
    reader_task = await _start_reader(rt)
    create_task = asyncio.create_task(rt.create_session(cwd="/w"))
    try:
        request_id = await _await_pending(rt)
        _feed(reader, {"id": request_id, "error": _RUNTIME_ERROR})
        with pytest.raises(AcpRuntimeError) as excinfo:
            await await_until(create_task, what="the failed session start settling")
        return excinfo.value
    finally:
        if not create_task.done():
            create_task.cancel()
        await _stop_reader(reader_task)


class TestRuntimeSessionStartErrorCarriesTheHint:
    """The dashboard's kiro and relay-KAS sessions start on the shared runtime."""

    @pytest.mark.asyncio
    async def test_session_new_error_names_the_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)

        exc = await _runtime_session_error()

        emitted = _flat(str(exc))
        settings_path, key = sandbox.kiro_internal_sandbox_switch()
        assert type(exc) is AcpRuntimeError
        assert emitted.startswith("RPC error: ")
        assert f'{settings_path} sets "{key}": true' in emitted
        assert f'Set "{key}" to false' in emitted

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("pin", "hinted"), [(STORED_BACKEND_KIRO, True), ("", False)])
    async def test_kas_runtime_judges_the_route_by_its_own_bot_pin(
        self, monkeypatch: pytest.MonkeyPatch, pin: str, hinted: bool
    ) -> None:
        """The runtime passes its Bot pin: a Kiro CLI pin keeps the relay on a host
        whose installation takes the bundle, so only that start gets the hint."""
        _host(monkeypatch, platform="darwin", enabled=True)
        _kas_route_on_host(monkeypatch, saved=None, build_prefers_bundle=True)

        exc = await _runtime_session_error(backend=ACP_BACKEND_KAS, pin=pin)

        assert ("Likely cause" in str(exc)) is hinted

    @pytest.mark.asyncio
    async def test_unchanged_when_the_setting_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, platform="darwin", enabled=False)

        exc = await _runtime_session_error()

        assert "Likely cause" not in str(exc)
        assert "mcp-client(kirocrew-core)" in str(exc)


class TestDoctorRow:
    def test_fatal_when_a_selected_harness_runs_kiro_cli(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)
        issues: list[str] = []

        confinement._doctor_kiro_sandbox_blocks_crew_mcp(issues, required=True)

        out = _flat(capsys.readouterr().out)
        settings_path, key = sandbox.kiro_internal_sandbox_switch()
        assert "kiro-cli sandbox: ❌" in out
        assert f'{settings_path} sets "{key}": true' in out
        assert f'Set "{key}" to false' in out
        assert len(issues) == 1 and settings_path in issues[0]

    def test_advisory_when_no_selected_harness_runs_kiro_cli(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _host(monkeypatch, platform="darwin", enabled=True)
        issues: list[str] = []

        confinement._doctor_kiro_sandbox_blocks_crew_mcp(issues, required=False)

        out = _flat(capsys.readouterr().out)
        assert "kiro-cli sandbox: ⚠️" in out
        assert "not used by the selected backends" in out
        assert issues == []

    @pytest.mark.parametrize(("platform", "enabled"), [("darwin", False), ("linux", True)])
    def test_silent_without_the_conflict(
        self, monkeypatch: pytest.MonkeyPatch, capsys, platform: str, enabled: bool
    ) -> None:
        _host(monkeypatch, platform=platform, enabled=enabled)
        issues: list[str] = []

        confinement._doctor_kiro_sandbox_blocks_crew_mcp(issues, required=True)

        assert "kiro-cli sandbox" not in capsys.readouterr().out
        assert issues == []


def _cfg(main: str, member: str, *, kas_bundle: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(acp_backend=main, member_acp_backend=member, kas_bundle=kas_bundle)
    )


class TestWhichRolesRunKiroCli:
    """The gate for fatality is a positive test on the selected harnesses."""

    @pytest.mark.parametrize(
        ("cfg", "expected"),
        [
            (_cfg(ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE), True),
            (_cfg(ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO), True),
            (_cfg(ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE), True),
            (_cfg(ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE, kas_bundle=True), False),
            (_cfg(ACP_BACKEND_CLAUDE, ACP_BACKEND_CLAUDE), False),
        ],
    )
    def test_required(self, cfg: SimpleNamespace, expected: bool) -> None:
        assert any(cli_doctor._selected_roles_run_kiro_cli(cfg)) is expected
