"""KAS projection: an MCP server's withheld env reaches it through a local launcher.

On KAS the agent's servers travel in ``session/new`` and the engine stores that
batch in ``session.json``, so the projection keeps env values off the wire. These
tests pin how the values still reach the server: the projection writes an
owner-only launch snapshot and hands the engine Crew's launcher plus a reference,
and the launcher (``kiro_crew.kas_env_shim``) starts the real command with them only
when the snapshot matches that reference. The canary must reach the server's
process and must appear in nothing that crosses the wire.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from kas_agents_helpers import _spec

from kiro_crew import kas_env_shim
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent

CANARY = "https://canary-26256.invalid/v1/zq7Xk"
SESSION = "dashboard_chat-26256-1"

#: A stdio MCP server that answers ``initialize`` and one ``tools/call`` with the
#: canary variable it sees, then exits. Enough to prove what the process received.
_FAKE_SERVER = r"""
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "tools/call":
        text = os.environ.get("CANARY_26256_ENDPOINT", "<missing>")
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"text": text}}), flush=True)
        break
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _snapshot(home: Path, launched: dict) -> Path:
    """The snapshot file a projected launcher entry references."""
    names = kas_env_launch.batch_snapshot_names([{"mcpServers": {"s": launched}}])
    assert len(names) == 1
    return home / "kas-mcp-launch" / names[0]


def _server_spec(script: Path, **extra) -> dict:
    return {
        "command": sys.executable,
        "args": [str(script)],
        "env": {"CANARY_26256_ENDPOINT": CANARY, **extra},
    }


def _project(spec: dict, agent: str = "kirocrew", session_key: str = SESSION) -> dict:
    return to_client_custom_agent(agent, spec, "p", session_key=session_key)


def _wire_text(agent_entry: dict) -> str:
    """Everything the engine receives for the session, as the frame serializes it."""
    agents, array = hoist_servers_for_session([agent_entry], agent_entry["id"], [])
    return json.dumps(build_session_new_params("/w", mcp_servers=array, kas_custom_agents=agents))


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "fake_server.py"
    path.write_text(_FAKE_SERVER, encoding="utf-8")
    return path


def _run_entry(entry: dict, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Start *entry* the way the engine does: its command, args and env alone."""
    # PYTHONDONTWRITEBYTECODE: the child imports the launcher from the checkout,
    # and must not leave a __pycache__ behind in it.
    # The engine spawns stdio servers with the MCP SDK's default environment, which
    # on Windows carries SYSTEMROOT and friends; without them Winsock cannot load and
    # the child's ``import asyncio`` fails (WinError 10106) before the launcher runs.
    inherited = ("PATH",) + (
        ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
        if os.name == "nt"
        else ()
    )
    env = {k: os.environ[k] for k in inherited if k in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", **(entry.get("env") or {})})
    # A source checkout run (``PYTHONPATH=src``) must find the launcher module
    # the projection names; an installed package needs nothing.
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    requests = "\n".join(
        json.dumps(m)
        for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}},
        )
    )
    return subprocess.run(
        [entry["command"], *entry["args"]],
        input=requests + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=timeout,
        check=False,
    )


class TestProjection:
    def test_the_canary_reaches_the_server_and_never_the_wire(self, home, script):
        entry = _project(_spec(mcpServers={"canary": _server_spec(script)}))
        launched = entry["mcpServers"]["canary"]
        assert CANARY not in _wire_text(entry), "no env value crosses the wire"
        assert CANARY not in " ".join(launched["args"]), "nor rides on argv"
        assert launched["args"][-8:] == [
            "--session",
            SESSION,
            "--agent",
            "kirocrew",
            "--server",
            "canary",
            "--version",
            launched["args"][-1],
        ]
        result = _run_entry(launched)
        assert result.returncode == 0, result.stderr
        assert CANARY in result.stdout, "the launcher handed the server its env"
        assert CANARY not in result.stderr

    def test_the_snapshot_holds_only_the_launch_and_is_owner_only(self, home, script):
        entry = _project(_spec(mcpServers={"canary": _server_spec(script)}))
        snapshot = _snapshot(home, entry["mcpServers"]["canary"])
        doc = json.loads(snapshot.read_text(encoding="utf-8"))
        assert doc["env"] == {"CANARY_26256_ENDPOINT": CANARY}
        assert doc["command"] == sys.executable and doc["args"] == [str(script)]
        assert set(doc) == {
            kas_env_shim.SNAPSHOT_MARKER,
            "session",
            "agent",
            "server",
            "version",
            "command",
            "args",
            "env",
        }, "only the launch, never the rest of the agent"
        assert doc["version"] == entry["mcpServers"]["canary"]["args"][-1]
        if os.name == "posix":
            assert snapshot.stat().st_mode & 0o777 == 0o600
            assert (home / "kas-mcp-launch").stat().st_mode & 0o777 == 0o700

    def test_a_crewmate_worker_spec_takes_the_same_path(self, home, script):
        from kiro_crew.mate_worker import MATE_WORKER_AGENT, merge_worker_spec, worker_additions

        base = {"name": MATE_WORKER_AGENT, "prompt": "base", "tools": ["*"], "mcpServers": {}}
        mate = {"prompt": "", "tools": ["*"], "mcpServers": {"headstart": _server_spec(script)}}
        spec = merge_worker_spec(base, worker_additions(base, mate))
        entry = _project(spec, agent=MATE_WORKER_AGENT, session_key="worker-session")
        assert CANARY not in _wire_text(entry)
        result = _run_entry(entry["mcpServers"]["headstart"])
        assert result.returncode == 0, result.stderr
        assert CANARY in result.stdout

    def test_a_relayed_flag_stays_on_the_entry_and_the_snapshot_takes_the_rest(self, home, script):
        entry = _project(_spec(mcpServers={"c": _server_spec(script, FEATURE_ENABLED="true")}))
        launched = entry["mcpServers"]["c"]
        assert launched["env"]["FEATURE_ENABLED"] == "true"
        snapshot = _snapshot(home, launched)
        assert "FEATURE_ENABLED" not in json.loads(snapshot.read_text(encoding="utf-8"))["env"]

    def test_no_session_key_keeps_the_withholding_projection(self, home, script):
        entry = _project(_spec(mcpServers={"c": _server_spec(script)}), session_key="")
        assert entry["mcpServers"]["c"] == {"command": sys.executable, "args": [str(script)]}
        assert not (home / "kas-mcp-launch").exists()

    def test_a_server_with_nothing_withheld_is_left_alone(self, home, script):
        spec = _spec(mcpServers={"c": {"command": sys.executable, "args": [str(script)]}})
        assert _project(spec)["mcpServers"]["c"]["command"] == sys.executable
        assert not (home / "kas-mcp-launch").exists()

    def test_a_remote_server_is_not_launched_locally(self, home):
        spec = _spec(mcpServers={"r": {"url": "https://x.invalid/mcp", "env": {"K": CANARY}}})
        entry = _project(spec)
        assert entry["mcpServers"]["r"] == {"url": "https://x.invalid/mcp"}
        assert CANARY not in _wire_text(entry)

    def test_a_snapshot_that_cannot_be_written_falls_back_to_withholding(
        self, home, script, monkeypatch
    ):
        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(kas_env_launch, "atomic_write", boom)
        entry = _project(_spec(mcpServers={"c": _server_spec(script)}))
        launched = entry["mcpServers"]["c"]
        assert CANARY not in _wire_text(entry)
        assert kas_env_shim.SHIM_MODULE in launched["args"], "the launcher, never the bare command"
        assert str(script) not in launched["args"]
        result = _run_entry(launched)
        assert result.returncode == 78, result.stderr
        assert "missing" in result.stderr
        assert CANARY not in result.stdout + result.stderr

    def test_a_snapshot_directory_that_cannot_be_made_refuses_the_launch(self, home, script):
        # A plain file where the directory belongs: the real write path fails.
        (home / "kas-mcp-launch").write_text("not a directory", encoding="utf-8")
        entry = _project(_spec(mcpServers={"c": _server_spec(script)}))
        launched = entry["mcpServers"]["c"]
        assert kas_env_shim.SHIM_MODULE in launched["args"]
        assert str(script) not in launched["args"]
        result = _run_entry(launched)
        assert result.returncode == 78
        assert CANARY not in result.stdout + result.stderr


class TestLifecycle:
    def test_each_projection_writes_its_own_file(self, home, script):
        # Two providers of one session (a cold-start race) never share a file.
        spec = _spec(mcpServers={"c": _server_spec(script)})
        first = _project(spec)["mcpServers"]["c"]
        second = _project(spec)["mcpServers"]["c"]
        assert first["args"][-1] != second["args"][-1]
        assert _snapshot(home, first) != _snapshot(home, second)
        assert _snapshot(home, first).exists() and _snapshot(home, second).exists()

    def test_discarding_one_batch_leaves_the_other_running(self, home, script):
        spec = _spec(mcpServers={"c": _server_spec(script)})
        loser, winner = _project(spec), _project(spec)
        kas_env_launch.discard_batch([loser])
        assert not _snapshot(home, loser["mcpServers"]["c"]).exists()
        assert _snapshot(home, winner["mcpServers"]["c"]).exists()
        result = _run_entry(winner["mcpServers"]["c"])
        assert result.returncode == 0, result.stderr
        assert CANARY in result.stdout

    def test_a_batch_without_launcher_entries_removes_nothing(self, home, script):
        entry = _project(_spec(mcpServers={"c": _server_spec(script)}))
        kas_env_launch.discard_batch([{"mcpServers": {"x": {"command": "x"}}}, "junk", None])
        kas_env_launch.discard_batch(None)
        assert _snapshot(home, entry["mcpServers"]["c"]).exists()

    def test_another_sessions_projection_keeps_an_old_live_snapshot(self, home, script):
        # A channel session can stay resident past the age limit; another
        # session's projection must not delete what its launcher still reads.
        live = _project(_spec(mcpServers={"c": _server_spec(script)}))["mcpServers"]["c"]
        aged = _snapshot(home, live)
        eight_days_ago = time.time() - 8 * 24 * 3600
        os.utime(aged, (eight_days_ago, eight_days_ago))
        _project(_spec(mcpServers={"d": _server_spec(script)}), session_key="another-session")
        assert aged.exists()
        result = _run_entry(live)
        assert result.returncode == 0, result.stderr
        assert CANARY in result.stdout

    def test_the_boot_sweep_removes_only_old_snapshots(self, home, script):
        spec = _spec(mcpServers={"c": _server_spec(script)})
        old, fresh = _project(spec), _project(spec)
        stale = _snapshot(home, old["mcpServers"]["c"])
        eight_days_ago = time.time() - 8 * 24 * 3600
        os.utime(stale, (eight_days_ago, eight_days_ago))
        recent = _snapshot(home, fresh["mcpServers"]["c"])
        six_days_ago = time.time() - 6 * 24 * 3600
        os.utime(recent, (six_days_ago, six_days_ago))
        kas_env_launch.sweep_stale(time.time())
        assert not stale.exists()
        assert recent.exists()

    @pytest.mark.parametrize(
        ("owner", "swept"),
        [("other", False), ("self", True), ("none", True)],
    )
    def test_the_boot_sweep_waits_while_another_owner_holds_runtimes(
        self, home, script, owner, swept
    ):
        from kiro_crew.agent_sdk.agent_specs import sweep_kas_launch_snapshots

        # A `kirocrew chat` CLI (or a second gateway) on this data home records its
        # KAS runtime under its own pid; the parent process stands in for it here.
        rows = {"other": f"{os.getppid()}:424242:tok\n", "self": f"{os.getpid()}:424242:tok\n"}
        if owner in rows:
            (home / "kiro_session_pids.txt").write_text(rows[owner], encoding="utf-8")
        live = _snapshot(
            home, _project(_spec(mcpServers={"c": _server_spec(script)}))["mcpServers"]["c"]
        )
        eight_days_ago = time.time() - 8 * 24 * 3600
        os.utime(live, (eight_days_ago, eight_days_ago))
        assert sweep_kas_launch_snapshots(time.time()) is swept
        assert live.exists() is not swept

    def test_the_cutoff_is_the_boot_time_not_the_scan_time(self, home, script, monkeypatch):
        # A snapshot written after boot is never older than the cutoff, even when
        # the background scan only gets to it more than a week later.
        boot = time.time() - 8 * 24 * 3600
        spec = _spec(mcpServers={"c": _server_spec(script)})
        after_boot = _snapshot(home, _project(spec)["mcpServers"]["c"])
        written = boot + 12 * 3600
        os.utime(after_boot, (written, written))
        before_boot = _snapshot(home, _project(spec)["mcpServers"]["c"])
        old = boot - 8 * 24 * 3600
        os.utime(before_boot, (old, old))
        kas_env_launch.sweep_stale(boot)
        assert after_boot.exists(), "written after boot, so never swept"
        assert not before_boot.exists()

    def test_a_swept_snapshot_refuses_the_launch(self, home, script):
        launched = _project(_spec(mcpServers={"c": _server_spec(script)}))["mcpServers"]["c"]
        _snapshot(home, launched).unlink()
        result = _run_entry(launched)
        assert result.returncode == 78
        assert "missing" in result.stderr
        assert CANARY not in result.stdout + result.stderr

    def test_a_number_is_relayed_as_its_spelling_and_an_object_stays_withheld(
        self, home, script, caplog
    ):
        import logging

        spec = _spec(mcpServers={"c": _server_spec(script, PORT=8080, NESTED={"x": 1})})
        with caplog.at_level(logging.INFO, logger="kiro_crew.acp.kas_agents"):
            entry = _project(spec)
        env = json.loads(_snapshot(home, entry["mcpServers"]["c"]).read_text("utf-8"))["env"]
        assert env["PORT"] == "8080" and "NESTED" not in env
        assert "NESTED" in caplog.text, "the value nothing carries is still named"

    def test_provider_shutdown_discards_its_own_batch(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace

        from kiro_crew.acp.session_provider import AcpSessionProvider

        seen: list[object] = []
        batch = [{"id": "kirocrew", "mcpServers": {}}]
        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._handle = SimpleNamespace(kas_registered_agents=batch)

        async def fails():
            raise RuntimeError("runtime already gone")

        monkeypatch.setattr(provider, "_shutdown_session", fails, raising=False)
        monkeypatch.setattr(kas_env_launch, "discard_batch", lambda b, _w=(): seen.append(b))
        monkeypatch.setattr(kas_env_launch, "sweep_stale", lambda *_a: seen.append("sweep"))
        with pytest.raises(RuntimeError):
            asyncio.run(provider.shutdown())
        assert seen == [batch], "discarded even when teardown fails, and never swept"

    def test_provider_shutdown_keeps_another_sessions_old_snapshot(self, home, script, monkeypatch):
        import asyncio
        from types import SimpleNamespace

        from kiro_crew.acp.session_provider import AcpSessionProvider

        live = _project(_spec(mcpServers={"c": _server_spec(script)}))["mcpServers"]["c"]
        aged = _snapshot(home, live)
        eight_days_ago = time.time() - 8 * 24 * 3600
        os.utime(aged, (eight_days_ago, eight_days_ago))
        mine = _project(_spec(mcpServers={"d": _server_spec(script)}), session_key="mine")
        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._handle = SimpleNamespace(kas_registered_agents=[mine])

        async def done():
            return None

        monkeypatch.setattr(provider, "_shutdown_session", done, raising=False)
        asyncio.run(provider.shutdown())
        assert not _snapshot(home, mine["mcpServers"]["d"]).exists(), "its own batch goes"
        assert aged.exists(), "another live session's snapshot stays"

    @pytest.mark.asyncio
    async def test_gateway_boot_starts_the_sweep_without_waiting_for_it(self, monkeypatch):
        import asyncio
        import threading

        from kiro_crew import session as session_mod
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.metrics import sessions as metrics_sessions
        from kiro_crew.slack import gateway as gateway_mod
        from kiro_crew.slack.gateway import GatewayOrchestrator

        class _Stop(Exception):
            pass

        order: list[str] = []
        boot_times: list[float] = []
        release = threading.Event()

        async def _no_preflight(*_a, **_k):
            return None

        def _cleanup(**_k):
            order.append("cleanup")
            boot_times.append(time.time())

        def _slow_sweep(boot_time):
            # Holds until the boot reaches its next step; a boot that waited for
            # the sweep would reach that step only after this times out.
            order.append("sweep:start")
            boot_times.append(boot_time)
            release.wait(5)
            order.append("sweep:end")

        def _next_boot_step():
            # The step after the sweep's place: stops the boot here, before it
            # starts any real service.
            order.append("warm_backend")
            release.set()
            raise _Stop

        monkeypatch.setattr(gateway_mod, "warm_backend", _next_boot_step)
        monkeypatch.setattr(gateway_mod.platform_compat, "probe_file_persistence", lambda d: None)
        monkeypatch.setattr(gateway_mod, "_run_signing_key_preflight_bounded", _no_preflight)
        monkeypatch.setattr(session_mod, "cleanup_orphaned_sessions", _cleanup)
        monkeypatch.setattr(metrics_sessions, "backfill_crashed_sessions", lambda _cutoff: 0)
        monkeypatch.setattr(kas_env_launch, "sweep_stale", _slow_sweep)
        orchestrator = GatewayOrchestrator(KiroCrewConfig(), no_dashboard=True, no_crons=True)
        with pytest.raises(_Stop):
            await orchestrator.run()
        await asyncio.gather(*list(orchestrator._background_tasks), return_exceptions=True)
        assert order[0] == "cleanup"
        assert order.index("warm_backend") < order.index("sweep:end"), order
        cleaned_at, cutoff = boot_times
        assert cleaned_at <= cutoff <= time.time(), "the cutoff is taken after the cleanup"


class TestShim:
    @pytest.fixture
    def launched(self, home, script):
        entry = _project(_spec(mcpServers={"c": _server_spec(script)}))["mcpServers"]["c"]
        ref = entry["args"][entry["args"].index("--session") :]
        return ref, dict(entry["env"])

    def _refusal(self, ref, environ) -> str:
        with pytest.raises(SystemExit) as exc:
            kas_env_shim.resolve_launch(ref, environ)
        assert exc.value.code == 78
        return ""

    def test_a_matching_reference_yields_the_recorded_launch(self, launched, script):
        ref, environ = launched
        argv, env = kas_env_shim.resolve_launch(ref, environ)
        assert argv == [sys.executable, str(script)]
        assert env["CANARY_26256_ENDPOINT"] == CANARY
        assert "KIROCREW_HOME" not in env, "the data home is the launcher's, not the server's"

    @pytest.mark.parametrize("flag", ["--session", "--agent", "--server"])
    def test_another_session_agent_or_server_finds_no_snapshot(self, launched, flag, capsys):
        ref, environ = launched
        ref = list(ref)
        ref[ref.index(flag) + 1] = "forged"
        self._refusal(ref, environ)
        assert "missing" in capsys.readouterr().err

    def test_a_forged_version_finds_no_snapshot(self, launched, capsys):
        ref, environ = launched
        self._refusal([*ref[:-1], "0" * 32], environ)
        err = capsys.readouterr().err
        assert "missing" in err and CANARY not in err

    def test_a_snapshot_copied_under_another_reference_is_refused(self, launched, home, capsys):
        ref, environ = launched
        real = home / "kas-mcp-launch" / kas_env_shim.snapshot_name(*ref[1::2])
        forged = [*ref[:-1], "0" * 32]
        copy = home / "kas-mcp-launch" / kas_env_shim.snapshot_name(*forged[1::2])
        copy.write_bytes(real.read_bytes())
        copy.chmod(0o600)
        self._refusal(forged, environ)
        err = capsys.readouterr().err
        assert "different version" in err and CANARY not in err

    def test_no_data_home_is_refused(self, launched, capsys):
        ref, environ = launched
        environ.pop("KIROCREW_HOME")
        self._refusal(ref, environ)
        assert "data home" in capsys.readouterr().err

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_a_snapshot_others_can_read_is_refused(self, launched, home, capsys):
        ref, environ = launched
        snapshot = home / "kas-mcp-launch" / kas_env_shim.snapshot_name(*ref[1::2])
        snapshot.chmod(0o644)
        self._refusal(ref, environ)
        assert "owner-only" in capsys.readouterr().err

    @pytest.mark.skipif(os.name != "posix", reason="symlink creation")
    def test_a_linked_snapshot_is_refused(self, launched, home, tmp_path, capsys):
        ref, environ = launched
        snapshot = home / "kas-mcp-launch" / kas_env_shim.snapshot_name(*ref[1::2])
        planted = tmp_path / "planted.json"
        planted.write_bytes(snapshot.read_bytes())
        planted.chmod(0o600)
        snapshot.unlink()
        snapshot.symlink_to(planted)
        self._refusal(ref, environ)
        assert "cannot be opened" in capsys.readouterr().err

    def test_a_malformed_reference_is_refused(self, home, capsys):
        self._refusal(["--session", SESSION], {"KIROCREW_HOME": str(home)})
        assert "malformed" in capsys.readouterr().err


def test_the_snapshot_leaf_is_sealed_and_fenced_like_pi_gate():
    from kiro_crew import sandbox, security

    leaf = kas_env_shim.SNAPSHOT_LEAF
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert any(Path(p).name == leaf for p in security.sensitive_home_dirs())
