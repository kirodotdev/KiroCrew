"""kiro-cli refusing its v3 engine on this host is a host verdict, not a process death.

On an Amazon Linux 2 host with kiro-cli signed in, kiro-cli lists ``--agent-engine
v3`` in ``acp --help`` and then answers ``kiro-cli acp --agent-engine v3`` with::

    error: V3 is currently not supported on this system.

and exit 1, before ``initialize``. Every KAS start died with the generic "process
exited (rc=1)" card, the next turn spawned the same refused relay, and a stored
Kiro CLI pick was moved onto KAS on the help text alone. The refusal is the host's,
so a respawn only reproduces it: the runtime that sees it marks KAS unservable,
withdraws the Kiro-CLI-offers-v3 evidence and reloads the live config so new
sessions take the Kiro CLI launch, whoever spawned it, and a session start raises a
non-retryable error that names it. The verdict is kept in the data home per kiro-cli
binary identity, so the v3 start check runs once per install and a restarted
gateway falls back before its first chat.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import kiro_cli
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.session_handle import AcpRuntimeDead
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.agent_sdk import backends as registry
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.testing.ids import unallocatable_pids

KAS = registry.ACP_BACKEND_KAS
KIRO = registry.ACP_BACKEND_KIRO

#: Fake pids for the runtime doubles, above every pid_max, so no cleanup path
#: that reads one can signal a live process.
_SPAWN_PID, _RESPAWN_PID = unallocatable_pids(2)

#: kiro-cli's own stderr on the reporter's host, verbatim.
REFUSAL_LINE = "error: V3 is currently not supported on this system."

#: The module under test, imported by the ``refusal`` fixture rather than at the
#: top: a tree without it then fails these tests one by one at setup, and the
#: other test files collected beside this one still run.
REFUSAL_MODULE = "kiro_crew.acp.kas_engine_refusal"


@pytest.fixture
def refusal():
    """``kiro_crew.acp.kas_engine_refusal``, imported when a test asks for it."""
    return importlib.import_module(REFUSAL_MODULE)


@pytest.fixture
def kas_selectable(monkeypatch, refusal):
    """A fresh process: KAS selectable, no host verdict, no v3 evidence yet."""
    monkeypatch.setattr(registry, "_baseline", {KIRO, KAS})
    monkeypatch.setattr(registry, "_selectable", {KIRO, KAS})
    monkeypatch.setattr(registry, "_host_unservable", {})
    monkeypatch.setattr(registry, "_kiro_cli_offers_kas", False)
    monkeypatch.setattr(registry, "_kiro_cli_kas_probed", False)
    monkeypatch.setattr(refusal, "_recorded", False)


@pytest.fixture
def woken(monkeypatch):
    """Record the config-watcher wakes and probe evictions the verdict asks for."""
    from kiro_crew.agent_sdk import backend_install
    from kiro_crew.config import live

    calls: list[str] = []
    watcher = MagicMock()
    watcher.notify_written = MagicMock(side_effect=lambda: calls.append("wake"))
    monkeypatch.setattr(live, "watch", lambda: watcher)
    monkeypatch.setattr(
        backend_install, "forget_probe", lambda backend: calls.append(f"forget:{backend}")
    )
    return calls


# -- The signature --


@pytest.mark.parametrize(
    "text",
    [
        REFUSAL_LINE,
        "AcpRuntime dead: process exited (rc=1)\n" + REFUSAL_LINE,
        "V3 IS CURRENTLY NOT SUPPORTED ON THIS SYSTEM",
    ],
    ids=["reported", "in-a-tail", "case"],
)
def test_the_refusal_is_recognised(refusal, text):
    assert refusal.is_kas_engine_refusal_output(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "error: failed to launch kiro-cli-chat",
        "Access denied: The bearer token included in the request is invalid.",
        "Cannot open directory /x/ssl/certs to load OpenSSL certificates.",
        "process exited (rc=1)",
        "",
    ],
    ids=["delegate", "auth", "healthy-v3-noise", "bare-death", "empty"],
)
def test_other_stderr_is_not_the_refusal(refusal, text):
    assert refusal.is_kas_engine_refusal_output(text) is False


# -- The latch, written by the drain --


class _FakeStream:
    def __init__(self, lines):
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


def _runtime(lines, *, started=False, backend=KAS, runtime_cls=None):
    from kiro_crew.acp.runtime import AcpRuntime

    cls = runtime_cls or AcpRuntime
    runtime = cls.__new__(cls)
    runtime._acp_backend = backend
    runtime.recording_allowed = True
    runtime._stderr_lines = []
    runtime._saw_auth_failure = False
    runtime._saw_sandbox_init_failure = False
    runtime._saw_delegate_launch_failure = False
    runtime._saw_kas_engine_refusal = False
    runtime._initialized = False
    runtime._first_session_ready = started
    runtime._process = MagicMock(stderr=_FakeStream(lines))
    return runtime


@pytest.fixture
def fresh_host(kas_selectable, woken, pinned):
    """A fresh process on a pinned kiro-cli whose data home holds no verdict yet."""
    return woken


@pytest.mark.asyncio
async def test_the_runtime_latches_the_refusal_past_the_ring_buffer(fresh_host):
    runtime = _runtime([REFUSAL_LINE] + [f"noise {i}" for i in range(40)])
    await runtime._drain_stderr()
    assert not any("V3 is" in line for line in runtime._stderr_lines), "precondition: evicted"
    assert runtime.saw_kas_engine_refusal() is True


@pytest.mark.asyncio
async def test_a_restricted_runtime_still_latches(fresh_host):
    runtime = _runtime([REFUSAL_LINE])
    runtime.recording_allowed = False
    await runtime._drain_stderr()
    assert runtime._stderr_lines == []
    assert runtime.saw_kas_engine_refusal() is True


@pytest.mark.asyncio
async def test_a_line_after_startup_does_not_latch(fresh_host):
    runtime = _runtime([REFUSAL_LINE], started=True)
    await runtime._drain_stderr()
    assert runtime.saw_kas_engine_refusal() is False
    assert KAS in registry.selectable_backends()


@pytest.mark.asyncio
async def test_a_healthy_start_never_latches(fresh_host):
    runtime = _runtime(["Cannot open directory /x/ssl/certs", "error: failed to launch x"])
    await runtime._drain_stderr()
    assert runtime.saw_kas_engine_refusal() is False
    assert fresh_host == []


#: The server path in kiro-cli's own line for an incomplete unpacked engine
#: (``acp.kas_extract_repair``), the other startup verdict a KAS stderr carries.
_BROKEN_EXTRACT_PATH = (
    "/h/.local/share/kiro-cli/kas/0.1.0/node_modules/@kiro/agent/dist/server/acp-server.js"
)
_BROKEN_EXTRACT_LINE = f"error: failed to canonicalize KAS server path: {_BROKEN_EXTRACT_PATH}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "refused", "server_path"),
    [(REFUSAL_LINE, True, ""), (_BROKEN_EXTRACT_LINE, False, _BROKEN_EXTRACT_PATH)],
    ids=["refusal", "broken-extract"],
)
async def test_the_refusal_and_a_broken_extract_latch_apart(fresh_host, line, refused, server_path):
    """Each startup verdict latches its own line only, so neither start reads as the other."""
    runtime = _runtime([line])
    await runtime._drain_stderr()
    assert runtime.saw_kas_engine_refusal() is refused
    assert getattr(runtime, "_kas_extract_server_path", "") == server_path
    assert (KAS in registry.selectable_backends()) is not refused


# -- The runtime records the host verdict itself, whoever spawned it --


@pytest.mark.asyncio
async def test_the_runtime_records_the_verdict_at_its_own_start(refusal, fresh_host, pinned):
    """No session start reads the latch here: the drain alone records the verdict."""
    registry.confirm_kiro_cli_offers_kas()
    await _runtime([REFUSAL_LINE])._drain_stderr()

    assert KAS not in registry.selectable_backends()
    assert registry.host_unservable_reason(KAS) == refusal.KAS_ENGINE_REFUSED_REASON
    assert registry.kiro_cli_pick_backend() == KIRO
    assert fresh_host == [f"forget:{KAS}", "wake"]
    # And kept for the next process, against the pinned kiro-cli as it is now.
    assert refusal.recorded_start_verdict(pinned) is False


@pytest.mark.asyncio
async def test_a_kiro_cli_runtime_latches_but_records_nothing(refusal, fresh_host, pinned):
    runtime = _runtime([REFUSAL_LINE], backend=KIRO)
    await runtime._drain_stderr()
    assert runtime.saw_kas_engine_refusal() is True
    assert KAS in registry.selectable_backends()
    assert fresh_host == []
    assert refusal.recorded_start_verdict(pinned) is None


@pytest.mark.asyncio
async def test_a_refused_disk_hop_keeps_the_drain_reading(refusal, fresh_host, pinned, monkeypatch):
    """A loop or executor shutting down refuses ``to_thread``; the drain reads on."""
    import asyncio

    async def _shut(*_a, **_kw):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(asyncio, "to_thread", _shut)
    runtime = _runtime([REFUSAL_LINE, "a later line"])
    await runtime._drain_stderr()

    assert runtime._stderr_lines == [REFUSAL_LINE, "a later line"]
    assert KAS not in registry.selectable_backends()
    assert refusal.recorded_start_verdict(pinned) is None


def _refused_runtime_factory(spawned: list[str]):
    """``AcpRuntime(**kw)`` whose child prints kiro-cli's refusal through the REAL drain."""
    from kiro_crew.acp.runtime import AcpRuntime

    def build(**kwargs):
        runtime = _runtime(
            [REFUSAL_LINE], backend=kwargs.get("acp_backend", KAS), runtime_cls=AcpRuntime
        )

        async def spawn(**_kw):
            spawned.append(runtime.acp_backend)
            await runtime._drain_stderr()
            raise AcpRuntimeDead("process exited (rc=1)")

        runtime.spawn = spawn
        runtime.kill = AsyncMock()
        runtime.is_alive = lambda: False
        return runtime

    return build


@pytest.mark.asyncio
async def test_a_refused_background_start_records_the_verdict(refusal, fresh_host, pinned):
    """A tips / title / summary one-liner on a refused KAS records it like a chat."""
    from session_helpers import _mock_provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = KAS
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    spawned: list[str] = []
    try:
        with patch(
            "kiro_crew.acp.runtime.AcpRuntime", side_effect=_refused_runtime_factory(spawned)
        ):
            with pytest.raises(AcpRuntimeDead):
                await mgr.get_bg_session()
    finally:
        await mgr.close_all()

    assert spawned == [KAS]
    assert KAS not in registry.selectable_backends()
    assert registry.resolve_selected_backend(None) == KIRO
    assert fresh_host == [f"forget:{KAS}", "wake"]
    assert refusal.recorded_start_verdict(pinned) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [True, False], ids=["refused", "other-death"])
async def test_a_refused_companion_spawn_is_not_retried(fresh_host, monkeypatch, refused):
    """The companion runtime's spawn retry stops on the refusal and only on it."""
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    sm = SessionManager(KiroCrewConfig())
    sm._get_session_agent = lambda key: "kirocrew"  # type: ignore[assignment]
    spawns: list[int] = []

    class _DeadOnStart:
        def __init__(self, agent=None, **kwargs):
            pass

        async def spawn(self, **_kw):
            spawns.append(1)
            raise AcpRuntimeDead("process exited (rc=1)")

        def saw_kas_engine_refusal(self):
            return refused

    monkeypatch.setattr("kiro_crew.acp.runtime.AcpRuntime", _DeadOnStart)
    with pytest.raises(AcpRuntimeDead):
        await sm.get_subagent_runtime("dashboard:slot1")
    assert len(spawns) == (1 if refused else 2)


def test_a_test_double_is_never_read_as_refused():
    from kiro_crew.agent_sdk import kas_start_was_refused

    assert kas_start_was_refused(MagicMock()) is False
    assert kas_start_was_refused(object()) is False
    assert kas_start_was_refused(_latched(True)) is True


def test_the_companion_retry_reads_the_refusal_through_the_sdk():
    """The companion module is application code: the agent-SDK boundary gate holds it."""
    import importlib.util

    from kiro_crew.session_allocation_parts import companions

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "check_agent_sdk_boundary", root / "scripts" / "check_agent_sdk_boundary.py"
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    rel = "src/kiro_crew/session_allocation_parts/companions.py"
    source = Path(companions.__file__).read_text(encoding="utf-8")
    assert gate._violations_in_source(rel, source) == {}
    assert "kas_start_was_refused" in source


def test_every_start_resets_the_latch():
    from kiro_crew.acp import runtime_session_create, runtime_spawn_entry

    for module in (runtime_spawn_entry, runtime_session_create):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert source.count("self._saw_kas_engine_refusal = False") == source.count(
            "self._saw_delegate_launch_failure = False"
        ), module.__name__


# -- The translation records the host verdict --


def _latched(value=True):
    runtime = MagicMock()
    runtime.saw_kas_engine_refusal.return_value = value
    return runtime


def test_a_kas_refusal_becomes_a_non_retryable_error(refusal, kas_selectable, woken):
    from kiro_crew.llm_helpers import acp_error_is_transient
    from kiro_crew.providers.acp_runtime_start import _kas_engine_refusal

    exc = _kas_engine_refusal(_latched(), KAS)
    assert isinstance(exc, refusal.AcpKasEngineRefused)
    assert isinstance(exc, AcpError)
    # Not a process death: the chat surfaces route those into a respawn by type.
    assert not isinstance(exc, AcpProcessDied)
    assert exc.transient is False
    assert exc.user_worded is True
    assert acp_error_is_transient(exc) is False
    assert str(exc) == refusal.KAS_ENGINE_REFUSED_MESSAGE
    assert "V3 is currently not supported on this system" in str(exc)
    assert "Kiro CLI" in str(exc)


def test_the_refusal_moves_new_sessions_off_kas(refusal, kas_selectable, woken):
    from kiro_crew.providers.acp_runtime_start import _kas_engine_refusal

    # A gateway whose floor confirmed v3 from the help text alone.
    registry.confirm_kiro_cli_offers_kas()
    assert registry.kiro_cli_pick_backend() == KAS

    _kas_engine_refusal(_latched(), KAS)

    assert KAS not in registry.selectable_backends()
    assert registry.host_unservable_reason(KAS) == refusal.KAS_ENGINE_REFUSED_REASON
    # The default, an explicit KAS and a stored Kiro CLI pick all load as Kiro CLI.
    assert registry.resolve_selected_backend(None) == KIRO
    assert registry.resolve_selected_backend(KAS) == KIRO
    assert registry.kiro_cli_offers_kas() is False
    assert registry.kiro_cli_pick_backend() == KIRO
    # And the live config is reloaded once, with the dashboard's KAS row re-read.
    assert woken == [f"forget:{KAS}", "wake"]


def test_a_second_refusal_reloads_nothing_more(refusal, kas_selectable, woken, caplog):
    from kiro_crew.providers.acp_runtime_start import _kas_engine_refusal

    caplog.set_level("WARNING", logger=refusal.logger.name)
    assert isinstance(_kas_engine_refusal(_latched(), KAS), refusal.AcpKasEngineRefused)
    assert isinstance(_kas_engine_refusal(_latched(), KAS), refusal.AcpKasEngineRefused)
    assert woken == [f"forget:{KAS}", "wake"]
    assert caplog.text.count("KAS could not start") == 1


def test_where_kas_is_the_floor_nothing_claims_a_fallback(
    refusal, kas_selectable, woken, monkeypatch
):
    from kiro_crew import edition
    from kiro_crew.providers.acp_runtime_start import _kas_engine_refusal

    monkeypatch.setattr(edition, "is_lts", lambda: True)
    exc = _kas_engine_refusal(_latched(), KAS)
    assert str(exc) == refusal.KAS_ENGINE_REFUSED_NO_FALLBACK_MESSAGE
    assert "Kiro CLI" not in str(exc)
    # The floor is never marked; the v3 evidence for a Kiro CLI pick is still withdrawn.
    assert KAS in registry.selectable_backends()
    assert registry.kiro_cli_offers_kas() is False


@pytest.mark.parametrize(
    ("backend", "latched"), [(KIRO, True), (KAS, False)], ids=["kiro-start", "kas-unlatched"]
)
def test_only_a_latched_kas_start_is_translated(kas_selectable, woken, backend, latched):
    from kiro_crew.providers.acp_runtime_start import _kas_engine_refusal

    assert _kas_engine_refusal(_latched(latched), backend) is None
    assert KAS in registry.selectable_backends()
    assert woken == []


@pytest.mark.parametrize("verdict", ["refusal", "broken-extract"])
def test_the_start_classifier_names_each_verdict_as_itself(refusal, kas_selectable, woken, verdict):
    """A refused start is never shown as a broken extract, nor a broken extract as a refusal."""
    from kiro_crew.acp.kas_extract_repair import AcpKasExtractBroken
    from kiro_crew.providers.acp_runtime_start import _kas_engine_refusal, _kas_extract_failure

    runtime = _latched(verdict == "refusal")
    runtime.acp_backend = KAS
    runtime.kas_extract_broken_message.return_value = (
        "" if verdict == "refusal" else "move the kas directory aside"
    )
    extract = _kas_extract_failure(runtime)
    refused = _kas_engine_refusal(runtime, KAS)
    if verdict == "refusal":
        assert extract is None
        assert isinstance(refused, refusal.AcpKasEngineRefused)
    else:
        assert isinstance(extract, AcpKasExtractBroken)
        assert refused is None
        assert KAS in registry.selectable_backends()


def test_every_startup_site_consults_the_latch():
    """Three startup-death paths, all three translate the refusal."""
    import kiro_crew.providers.acp as provider_mod

    source = "\n".join(
        Path(sys.modules[name].__file__).read_text(encoding="utf-8")
        for name in (provider_mod.__name__, *provider_mod._ACP_PROVIDER_OWNERS)
    )
    sandbox_sites = len(re.findall(r"sandbox_init_failure_for_runtime\(runtime\)", source))
    refusal_sites = len(
        re.findall(r"_kas_engine_refusal\(runtime, self\._client\.backend\)", source)
    )
    assert sandbox_sites == 3
    assert refusal_sites == sandbox_sites


def _kas_provider():
    from kiro_crew.providers.acp import AcpProvider

    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=KAS)
    provider._client = MagicMock()
    provider._client.backend = KAS
    provider._client._work_dir = "/tmp/ws"
    provider._client._agent = "kirocrew"
    provider._client._sandbox_mode = "auto"
    provider._client._extra_env = {}
    provider._client._mcp_gateway_overlay = None
    provider._client._mcp_gateway_socket = None
    provider._client._resume_session_id = ""
    provider._client._model = "auto"
    return provider


@pytest.mark.asyncio
async def test_a_refused_kas_spawn_raises_the_refusal_not_the_death(refusal, kas_selectable, woken):
    from kiro_crew.acp.session_handle import AcpRuntimeDead

    runtime = MagicMock()
    runtime.pid = _SPAWN_PID
    runtime.spawn = AsyncMock(side_effect=AcpRuntimeDead("process exited (rc=1)"))
    runtime.settle_stderr = AsyncMock()
    runtime.kill = AsyncMock()
    runtime.saw_sandbox_init_failure = MagicMock(return_value=False)
    runtime.saw_delegate_launch_failure = MagicMock(return_value=False)
    runtime.saw_kas_engine_refusal = MagicMock(return_value=True)
    runtime.saw_not_logged_in = MagicMock(return_value=False)
    provider = _kas_provider()
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
        patch("kiro_crew.providers.acp.AcpSessionProvider"),
    ):
        with pytest.raises(refusal.AcpKasEngineRefused):
            await provider._start_kiro_runtime()
    runtime.create_session.assert_not_called()
    assert KAS not in registry.selectable_backends()


def _refused_start_runtime(**overrides):
    """A runtime double whose start died on the latched refusal, as the sites read it."""
    runtime = MagicMock()
    runtime.pid = _RESPAWN_PID
    runtime.work_scratch_dir = None
    runtime.spawn = AsyncMock()
    runtime.settle_stderr = AsyncMock()
    runtime.kill = AsyncMock()
    runtime.saw_sandbox_init_failure = MagicMock(return_value=False)
    runtime.saw_delegate_launch_failure = MagicMock(return_value=False)
    runtime.saw_kas_engine_refusal = MagicMock(return_value=True)
    runtime.saw_not_logged_in = MagicMock(return_value=False)
    for name, value in overrides.items():
        setattr(runtime, name, value)
    return runtime


@pytest.mark.asyncio
async def test_a_refused_resume_respawn_raises_the_refusal(refusal, kas_selectable, woken):
    """The runtime died during resume; its respawn meets the refusal."""
    dead = _refused_start_runtime(
        is_alive=MagicMock(return_value=False),
        load_session=AsyncMock(side_effect=RuntimeError("load failed")),
        saw_kas_engine_refusal=MagicMock(return_value=False),
    )
    fresh = _refused_start_runtime(spawn=AsyncMock(side_effect=AcpRuntimeDead("rc=1")))
    runtimes = iter([dead, fresh])
    provider = _kas_provider()
    provider._client._resume_session_id = "old-sess"
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", side_effect=lambda **kw: next(runtimes)),
        patch("kiro_crew.providers.acp.AcpSessionProvider"),
        patch("pathlib.Path.exists", return_value=True),
    ):
        with pytest.raises(refusal.AcpKasEngineRefused):
            await provider._start_kiro_runtime()
    fresh.spawn.assert_awaited_once()
    fresh.create_session.assert_not_called()
    assert KAS not in registry.selectable_backends()


@pytest.mark.asyncio
async def test_a_refused_session_create_raises_the_refusal(refusal, kas_selectable, woken):
    """The process started, then died at ``session/new`` on the refusal."""
    runtime = _refused_start_runtime(
        is_alive=MagicMock(return_value=True),
        create_session=AsyncMock(side_effect=AcpRuntimeDead("rc=1")),
    )
    provider = _kas_provider()
    with (
        patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
        patch("kiro_crew.providers.acp.AcpSessionProvider"),
    ):
        with pytest.raises(refusal.AcpKasEngineRefused):
            await provider._start_kiro_runtime()
    runtime.create_session.assert_awaited_once()
    assert KAS not in registry.selectable_backends()


@pytest.mark.asyncio
async def test_the_dashboard_turn_shows_the_refusal_without_a_respawn(refusal, tmp_path):
    from test_dashboard_chat import TestAcpProcessDiedRecovery as _suite

    state, slot, client, run_chat = _suite._make_state_and_slot(_suite(), tmp_path)
    _suite._make_stream_raise(_suite(), client, refusal.AcpKasEngineRefused())
    await run_chat(state, slot, "hello")

    errors = [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
    assert slot._acp_pipe_death_retries == 0
    assert not any("retrying" in e.lower() or "stuck" in e.lower() for e in errors), errors
    assert any("V3 is currently not supported on this system" in e for e in errors), errors


# -- The start check the KAS floor and doctor read --


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """The kiro-cli a session spawn runs is the pinned install."""
    binary = tmp_path / "kiro-cli"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: str(binary))
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
    return str(binary)


def _start_output(monkeypatch, output):
    calls: list[tuple[str, tuple[str, ...]]] = []

    def _fake(binary, argv_tail):
        calls.append((binary, tuple(argv_tail)))
        return output

    monkeypatch.setattr(kiro_cli, "kas_relay_start_output", _fake)
    return calls


def test_a_refused_start_is_a_gap(refusal, monkeypatch, pinned):
    calls = _start_output(monkeypatch, REFUSAL_LINE + "\n")
    assert acp_driver.kas_engine_start_verdict() == (False, refusal.KAS_ENGINE_REFUSED_REASON)
    assert calls == [(pinned, ("acp", "--agent-engine", "v3"))]


@pytest.mark.parametrize(
    ("output", "refused"),
    [(REFUSAL_LINE, True), ("", False)],
    ids=["refused", "starts"],
)
def test_the_start_runs_once_per_kiro_cli_install(refusal, monkeypatch, pinned, output, refused):
    """A later process reads the kept verdict; a changed kiro-cli is started again."""
    verdict = (False, refusal.KAS_ENGINE_REFUSED_REASON) if refused else (True, None)
    calls = _start_output(monkeypatch, output)
    assert acp_driver.kas_engine_start_verdict() == verdict
    # A later process asks again: the same binary and data home.
    assert acp_driver.kas_engine_start_verdict() == verdict
    assert acp_driver.kas_engine_start_verdict(start=False) == verdict
    assert len(calls) == 1
    # An update in place is a new identity, so it is checked again.
    stat = os.stat(pinned)
    os.utime(pinned, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert acp_driver.kas_engine_start_verdict(start=False) == (None, None)
    assert acp_driver.kas_engine_start_verdict() == verdict
    assert len(calls) == 2


def test_a_record_only_read_starts_nothing(refusal, monkeypatch, pinned):
    calls = _start_output(monkeypatch, REFUSAL_LINE)
    assert acp_driver.kas_engine_start_verdict(start=False) == (None, None)
    assert calls == []
    refusal.remember_start_verdict(pinned, False)
    assert acp_driver.kas_engine_start_verdict(start=False) == (
        False,
        refusal.KAS_ENGINE_REFUSED_REASON,
    )
    assert calls == []


def test_an_unknown_start_is_not_kept(refusal, monkeypatch, pinned):
    _start_output(monkeypatch, None)
    assert acp_driver.kas_engine_start_verdict() == (None, None)
    assert refusal.recorded_start_verdict(pinned) is None


def test_the_record_drops_verdicts_for_changed_binaries(refusal, tmp_path, pinned):
    other = tmp_path / "other-kiro-cli"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    refusal.remember_start_verdict(str(other), True)
    other.write_text("#!/bin/sh\n# updated\n", encoding="utf-8")
    refusal.remember_start_verdict(pinned, False)
    record = refusal._read_start_records()
    assert set(record) == {pinned}


def test_an_unreadable_record_is_no_verdict(refusal, pinned):
    refusal._start_record_path().write_text("not json", encoding="utf-8")
    assert refusal.recorded_start_verdict(pinned) is None
    refusal.remember_start_verdict(pinned, False)
    assert refusal.recorded_start_verdict(pinned) is False


_V3_HELP = "Usage: kiro-cli acp [OPTIONS]\n  --agent-engine <ENGINE>  [possible values: v2, v3]\n"


@pytest.mark.asyncio
async def test_a_refusal_one_process_met_moves_the_next_gateway_off_kas(
    refusal, fresh_host, pinned, monkeypatch
):
    """After a restart a defaulted KAS falls back at the floor, before any chat."""
    from kiro_crew.platform import bootstrap

    # Process one: a KAS runtime meets the refusal at its own start.
    await _runtime([REFUSAL_LINE])._drain_stderr()
    # Process two: nothing in memory, the same kiro-cli, KAS by default, help lists v3.
    monkeypatch.setattr(registry, "_selectable", {KIRO, KAS})
    monkeypatch.setattr(registry, "_host_unservable", {})
    monkeypatch.setattr(kiro_cli, "kas_relay_help", lambda binary: _V3_HELP)
    starts = _start_output(monkeypatch, "")
    cfg = SimpleNamespace(
        agent=SimpleNamespace(acp_backend=KAS, member_acp_backend="", kas_bundle=False)
    )
    notice = bootstrap.apply_kas_cli_floor(cfg, kas_defaulted=True)

    assert cfg.agent.acp_backend == KIRO
    assert KAS not in registry.selectable_backends()
    assert notice == registry.KAS_CLI_FLOOR_DEFAULT_NOTICE.format(
        reason=refusal.KAS_ENGINE_REFUSED_REASON
    )
    assert starts == [], "the floor read the record; it started nothing"


def _automatic_bundle_route(monkeypatch):
    """kiro-cli is signed out with no ``kiro-cli-chat`` beside it; Crew's sign-in holds one.

    So the next KAS spawn takes the bundle route automatically (``acp.kas_route``
    rule 4), which never runs kiro-cli.
    """
    from kiro_crew.acp import kas_route
    from kiro_crew.auth import bridge

    # The suite pins the automatic switch closed (conftest); no Kiro CLI pick is
    # stored here, so the real rule would open it.
    monkeypatch.setattr(kas_route, "automatic_switch_allowed", lambda pin="": True)
    monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **_kw: True)
    # No ``kiro-cli-chat`` beside the launcher. On Windows the one kiro-cli.exe is the
    # chat binary, whose relay Crew's sign-in stands in for, so pin the answer.
    monkeypatch.setattr(kas_route, "crew_entry_skips_launcher", lambda *_a, **_kw: False)
    monkeypatch.setattr(bridge, "vault_holds_identity", lambda **_kw: True)
    route = kas_route.resolve_kas_route()
    assert route.bundle and route.automatic, "precondition: the automatic bundle route"


def _floor_keeps_kas_on_the_bundle_route(starts):
    from kiro_crew.platform import bootstrap

    cfg = SimpleNamespace(
        agent=SimpleNamespace(acp_backend=KAS, member_acp_backend="", kas_bundle=False)
    )

    assert bootstrap.apply_kas_cli_floor(cfg) is None
    assert cfg.agent.acp_backend == KAS
    assert registry.host_unservable_reason(KAS) is None
    assert KAS in registry.selectable_backends()
    assert starts == []


def test_at_the_floor_a_refusal_on_record_leaves_the_automatic_bundle_route_on_kas(
    refusal, kas_selectable, pinned, monkeypatch
):
    """The relay's refusal is no gap where the next KAS spawn takes Crew's own engine.

    kiro-cli is signed out and has no ``kiro-cli-chat`` beside it while Kiro Crew's
    own sign-in holds an identity, so the spawn switches to the bundle route
    automatically (``acp.kas_route`` rule 4). Marking KAS unservable there would
    drop the usable engine for a kiro-cli that cannot sign in.
    """
    refusal.remember_start_verdict(pinned, False)
    monkeypatch.setattr(kiro_cli, "kas_relay_help", lambda binary: _V3_HELP)
    starts = _start_output(monkeypatch, "")
    _automatic_bundle_route(monkeypatch)

    _floor_keeps_kas_on_the_bundle_route(starts)


def test_at_the_floor_a_help_gap_leaves_the_automatic_bundle_route_on_kas(
    kas_selectable, pinned, monkeypatch
):
    """The same for a signed-out kiro-cli whose help names no v3 engine.

    The help gap shares the floor's one marking line with the refusal, and the
    bundle route the spawn takes runs no kiro-cli either way.
    """
    monkeypatch.setattr(kiro_cli, "kas_relay_help", lambda binary: "Usage: kiro-cli acp\n")
    starts = _start_output(monkeypatch, "")
    _automatic_bundle_route(monkeypatch)

    _floor_keeps_kas_on_the_bundle_route(starts)


def test_a_start_without_the_refusal_starts(monkeypatch, pinned):
    _start_output(monkeypatch, "Cannot open directory /x/ssl/certs\n")
    assert acp_driver.kas_engine_start_verdict() == (True, None)


def test_a_failed_start_probe_is_unknown(monkeypatch, pinned):
    _start_output(monkeypatch, None)
    assert acp_driver.kas_engine_start_verdict() == (None, None)


def test_no_trusted_cli_never_starts_the_engine(monkeypatch):
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: None)
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))
    calls = _start_output(monkeypatch, REFUSAL_LINE)
    assert acp_driver.kas_engine_start_verdict() == (None, None)
    assert calls == []


def test_a_path_only_cli_is_read_but_never_started(refusal, tmp_path, monkeypatch):
    """The start runs unsandboxed, so a binary reached only through PATH is never run.

    Its help verdict stands (``True``) until a KAS runtime that met the refusal keeps
    it for that binary, which is the record every later read honours.
    """
    binary = tmp_path / "opt-bin" / "kiro-cli"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin", lambda **_kw: str(binary))
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, True))
    calls = _start_output(monkeypatch, REFUSAL_LINE)
    assert acp_driver.kas_engine_start_verdict() == (True, None)
    assert acp_driver.kas_engine_start_verdict(start=False) == (None, None)
    refusal.remember_kas_engine_refusal()
    assert refusal.recorded_start_verdict(str(binary)) is False
    assert acp_driver.kas_engine_start_verdict() == (False, refusal.KAS_ENGINE_REFUSED_REASON)
    assert calls == []


def test_doctor_names_its_own_binary(refusal, monkeypatch):
    monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, False))
    calls = _start_output(monkeypatch, REFUSAL_LINE)
    assert acp_driver.kas_engine_start_verdict("/opt/kiro-cli") == (
        False,
        refusal.KAS_ENGINE_REFUSED_REASON,
    )
    assert calls == [("/opt/kiro-cli", ("acp", "--agent-engine", "v3"))]


def _executable(tmp_path: Path, body: str) -> str:
    """A script written and made executable beside its public path, then renamed in."""
    private = tmp_path / ".kiro-cli.tmp"
    private.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    private.chmod(0o755)
    public = tmp_path / "kiro-cli"
    os.replace(private, public)
    return str(public)


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell script stands in for kiro-cli")
def test_the_probe_reads_the_refusal_from_a_real_start(refusal, tmp_path, monkeypatch):
    monkeypatch.setattr(kiro_cli, "_kas_start_cache", {})
    count = tmp_path / "starts"
    where = tmp_path / "cwd"
    script = [
        f'echo started >> "{count}"',
        f'pwd > "{where}"',
        # stdin is closed: a healthy engine would read EOF here and exit.
        "cat >/dev/null",
        f'echo "{REFUSAL_LINE}" >&2',
        "exit 1",
    ]
    binary = _executable(tmp_path, "\n".join(script) + "\n")
    argv = ("acp", "--agent-engine", "v3")
    output = kiro_cli.kas_relay_start_output(binary, argv)
    assert output is not None and refusal.is_kas_engine_refusal_output(output)
    # Cached per binary identity: the second answer spawns nothing.
    assert kiro_cli.kas_relay_start_output(binary, argv) == output
    assert count.read_text(encoding="utf-8").splitlines() == ["started"]
    # It ran in a private directory of its own, removed once it was done.
    ran_in = Path(where.read_text(encoding="utf-8").strip())
    assert ran_in.name.startswith("kirocrew-kas-start-")
    assert ran_in != Path.cwd()
    assert not ran_in.exists()


def test_a_start_that_never_exits_is_killed_with_its_group_and_unknown(tmp_path, monkeypatch):
    from kiro_crew import platform_compat

    monkeypatch.setattr(kiro_cli, "_kas_start_cache", {})
    binary = tmp_path / "kiro-cli"
    binary.write_text("", encoding="utf-8")
    proc = MagicMock()
    proc.wait = MagicMock(side_effect=[subprocess.TimeoutExpired("kiro-cli", 30), 0])
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(kiro_cli.subprocess, "Popen", popen)
    killed: list[object] = []
    monkeypatch.setattr(platform_compat, "kill_popen_tree", killed.append)
    assert kiro_cli.kas_relay_start_output(str(binary), ("acp",)) is None
    # The engine the start launched leads no group of its own: the start's group
    # is what the kill reaches.
    assert killed == [proc]
    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is platform_compat.IS_POSIX
    assert kwargs["creationflags"] & platform_compat.CREATE_NEW_PROCESS_GROUP == (
        platform_compat.CREATE_NEW_PROCESS_GROUP
    )
    assert Path(kwargs["cwd"]).name.startswith("kirocrew-kas-start-")
    assert kiro_cli._kas_start_cache == {}


def test_a_missing_binary_is_unknown(tmp_path):
    assert kiro_cli.kas_relay_start_output(str(tmp_path / "absent"), ("acp",)) is None
