"""Repairing kiro-cli's broken KAS extract at spawn.

The guard and the rename run on real directories under ``tmp_path``; the spawn
wrapper runs the real ``runtime_spawn_entry`` function over a stand-in runtime; and
one test drives the real kiro-cli binary through the whole break-and-repair cycle,
skipped where kiro-cli is not installed.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat_links
from kiro_crew.acp import kas_extract_repair, runtime_liveness, runtime_spawn_entry
from kiro_crew.acp.kas_extract_repair import AcpKasExtractBroken
from kiro_crew.acp.session_handle import AcpRuntimeDead
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.identity_stores import state_db_candidates
from kiro_crew.testing.links import make_dir_link

_VERSION = "3.1.0-191a57181648c1baf0df554635a918f5b63d30aaf16cd6389da726e3e2313fa9"


@pytest.fixture(autouse=True)
def _no_recorded_block(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(kas_extract_repair, "_BLOCKED", None)


def _data_dir(home: Path) -> Path:
    """kiro-cli's data directory under *home* on this platform, env vars ignored."""
    return state_db_candidates(sys.platform, home, {})[0].parent


def _server_path(version_dir: Path) -> Path:
    return version_dir.joinpath(*kas_extract_repair.SERVER_ENTRY_PARTS)


def _broken_extract(home: Path) -> Path:
    """A ``kas/<version>`` dir with its ``node_modules`` gone, as a home copy leaves it."""
    version_dir = _data_dir(home) / "kas" / _VERSION
    version_dir.mkdir(parents=True)
    (version_dir / "kept.txt").write_text("user bytes", encoding="utf-8")
    _age(version_dir)
    return version_dir


def _age(version_dir: Path) -> None:
    """Date *version_dir* and its direct entries past the in-progress window."""
    old = time.time() - kas_extract_repair.MIN_EXTRACT_AGE_SECS - 60
    for path in (*version_dir.iterdir(), version_dir):
        os.utime(path, (old, old))  # real files and dirs only; Windows has no lutime


def _repair(home: Path, server_path: Path, *, enabled: bool = True):
    return kas_extract_repair.repair(str(server_path), enabled=enabled, home=home, environ={})


def test_marker_is_read_from_kiro_cli_stderr() -> None:
    path = f"/home/u/.local/share/kiro-cli/kas/{_VERSION}/node_modules/@kiro/agent/dist/server/acp-server.js"
    stderr = (
        "Cannot open directory /x/ssl/certs to load OpenSSL certificates.\n"
        f"error: failed to canonicalize KAS server path: {path}\n"
    )
    assert kas_extract_repair.server_path_from_stderr(stderr) == path
    assert kas_extract_repair.server_path_from_stderr("error: process exited") is None
    padded = f"error: failed to canonicalize KAS server path:   {path}  \n"
    assert kas_extract_repair.server_path_from_stderr(padded) == path
    no_path = "error: failed to canonicalize KAS server path:   \n"
    assert kas_extract_repair.server_path_from_stderr(no_path) is None


@pytest.mark.parametrize(
    ("server_path", "expected"),
    [
        (
            f"/h/.local/share/kiro-cli/kas/{_VERSION}/node_modules/@kiro/agent/dist/server/acp-server.js",
            f"/h/.local/share/kiro-cli/kas/{_VERSION}",
        ),
        (f"h/kas/{_VERSION}/node_modules/@kiro/agent/dist/server/acp-server.js", None),
        (f"/h/kas/{_VERSION}/node_modules/@kiro/agent/dist/server/other.js", None),
        (f"/h/engines/{_VERSION}/node_modules/@kiro/agent/dist/server/acp-server.js", None),
        (
            "/h/.local/share/kiro-cli/kas/../node_modules/@kiro/agent/dist/server/acp-server.js",
            None,
        ),
    ],
    ids=["kiro-cli-layout", "relative", "other-entry", "not-under-kas", "dot-dot-version"],
)
def test_version_dir_is_parsed_from_the_layout_only(server_path: str, expected: str | None) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX path spellings")
    got = kas_extract_repair.version_dir_of(server_path)
    assert (str(got) if got else None) == expected


def test_broken_extract_is_renamed_never_deleted(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    verdict = _repair(tmp_path, _server_path(version_dir))
    assert verdict.retry and verdict.subject == str(version_dir)
    moved = Path(verdict.renamed_to)
    assert not version_dir.exists()
    assert moved.parent == _data_dir(tmp_path)  # beside kas/, which kiro-cli sweeps
    assert moved.name.startswith(_VERSION + kas_extract_repair.BROKEN_SUFFIX)
    assert (moved / "kept.txt").read_text(encoding="utf-8") == "user bytes"


def test_switch_off_leaves_the_extract_in_place(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    verdict = _repair(tmp_path, _server_path(version_dir), enabled=False)
    assert not verdict.retry and not verdict.renamed_to
    assert version_dir.is_dir()


def test_extract_outside_kiro_clis_data_dir_is_not_moved(tmp_path: Path) -> None:
    version_dir = tmp_path / "elsewhere" / "kas" / _VERSION
    version_dir.mkdir(parents=True)
    verdict = _repair(tmp_path, _server_path(version_dir))
    assert not verdict.retry
    assert version_dir.is_dir()


def test_symlinked_version_dir_is_not_moved(tmp_path: Path) -> None:
    target = tmp_path / "real-copy"
    target.mkdir()
    kas = _data_dir(tmp_path) / "kas"
    kas.mkdir(parents=True)
    link = kas / _VERSION
    make_dir_link(link, target)
    verdict = _repair(tmp_path, _server_path(link))
    assert not verdict.retry
    assert platform_compat_links.is_link_or_junction(link)


def test_an_extract_changed_in_the_last_minute_is_not_moved(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    (version_dir / "being-written").write_text("", encoding="utf-8")
    verdict = _repair(tmp_path, _server_path(version_dir))
    assert not verdict.retry and not verdict.renamed_to
    assert version_dir.is_dir()


def test_a_linked_kas_dir_is_not_moved(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    version_dir = elsewhere / _VERSION
    version_dir.mkdir(parents=True)
    _age(version_dir)
    data = _data_dir(tmp_path)
    data.mkdir(parents=True)
    make_dir_link(data / "kas", elsewhere)
    verdict = _repair(tmp_path, _server_path(data / "kas" / _VERSION))
    assert not verdict.retry
    assert version_dir.is_dir()


def test_extract_already_moved_or_restored_retries_without_renaming(tmp_path: Path) -> None:
    gone = _data_dir(tmp_path) / "kas" / _VERSION
    assert _repair(tmp_path, _server_path(gone)).retry  # a concurrent start moved it
    version_dir = _broken_extract(tmp_path)
    server = _server_path(version_dir)
    server.parent.mkdir(parents=True)
    server.write_text("//", encoding="utf-8")
    verdict = _repair(tmp_path, server)
    assert verdict.retry and not verdict.renamed_to
    assert server.is_file()


def test_fix_message_names_the_directory_and_the_command() -> None:
    message = kas_extract_repair.fix_message(
        kas_extract_repair.RepairVerdict(subject="/h/kas/v 1", retry=False)
    )
    assert "/h/kas/v 1" in message
    target = str(Path("/h/kas/v 1").parent.parent / "v 1.kas-broken-manual")  # host separators
    assert message.endswith(f"mv '/h/kas/v 1' {shlex.quote(target)}")
    unparsed = kas_extract_repair.fix_message(
        kas_extract_repair.RepairVerdict(subject="/odd/acp-server.js", retry=False)
    )
    assert "/odd/acp-server.js" in unparsed and "mv " not in unparsed


def test_warm_pool_is_held_until_the_directory_is_moved(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    server = _server_path(version_dir)
    kas_pool = SimpleNamespace(is_kas_backend=True)
    assert asyncio.run(kas_extract_repair.warm_pool_held(kas_pool)) is False
    verdict = kas_extract_repair.RepairVerdict(subject=str(version_dir), retry=False)
    kas_extract_repair.note_blocked(verdict, str(server))
    assert asyncio.run(kas_extract_repair.warm_pool_held(kas_pool)) is True
    # A pool on another backend never reads kiro-cli's extract, so it keeps filling.
    other_pool = SimpleNamespace(is_kas_backend=False)
    assert asyncio.run(kas_extract_repair.warm_pool_held(other_pool)) is False
    version_dir.rename(version_dir.with_name("moved-by-hand"))
    assert asyncio.run(kas_extract_repair.warm_pool_held(kas_pool)) is False
    assert kas_extract_repair._BLOCKED is None


class _StandInRuntime:
    """The fields and methods the spawn wrapper reads, with scripted start outcomes."""

    def __init__(self, outcomes: list[str], *, revives_ok: bool = True) -> None:
        self._outcomes = outcomes
        self._revives_ok = revives_ok
        self._kas_extract_server_path = ""
        self._kas_extract_broken_message = ""
        self._remote_workspace = None
        self.starts = 0
        self.revives = 0

    async def _spawn_admitted_rederiving_once(self) -> None:
        self.starts += 1
        outcome = self._outcomes.pop(0)
        if outcome == "ok":
            return
        self._kas_extract_server_path = kas_extract_repair.server_path_from_stderr(outcome) or ""
        raise AcpRuntimeDead("process exited (rc=None)")

    async def _revive_after_failed_start(self) -> bool:
        self.revives += 1
        if not self._revives_ok:
            return False
        self._kas_extract_server_path = ""
        return True

    _broken_kas_server_path = runtime_spawn_entry._broken_kas_server_path
    spawn_repairing = runtime_spawn_entry._spawn_admitted_repairing_kas_extract


def _marker(server: Path) -> str:
    return f"error: failed to canonicalize KAS server path: {server}"


@pytest.fixture
def _repair_under(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = kas_extract_repair.repair
    monkeypatch.setattr(
        kas_extract_repair,
        "repair",
        functools.partial(real, enabled=True, home=tmp_path, environ={}),
    )


@pytest.mark.usefixtures("_repair_under")
def test_spawn_repairs_once_and_starts(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    runtime = _StandInRuntime([_marker(_server_path(version_dir)), "ok"])
    asyncio.run(runtime.spawn_repairing())
    assert (runtime.starts, runtime.revives) == (2, 1)
    assert runtime._kas_extract_broken_message == ""
    assert not version_dir.exists()
    assert kas_extract_repair._BLOCKED is None


@pytest.mark.usefixtures("_repair_under")
def test_spawn_failing_again_raises_once_with_the_fix_and_holds_the_pool(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    marker = _marker(_server_path(version_dir))
    runtime = _StandInRuntime([marker, marker])
    with pytest.raises(AcpRuntimeDead):
        asyncio.run(runtime.spawn_repairing())
    assert runtime.starts == 2
    assert str(version_dir) in runtime._kas_extract_broken_message
    assert "mv " in runtime._kas_extract_broken_message
    assert kas_extract_repair._BLOCKED is not None
    # The provider's translation is a non-retryable, person-worded error.
    error = AcpKasExtractBroken(runtime._kas_extract_broken_message)
    assert error.transient is False and error.user_worded is True


@pytest.mark.usefixtures("_repair_under")
@pytest.mark.usefixtures("_repair_under")
def test_a_refused_revive_after_the_move_raises_the_first_error_without_the_fix(
    tmp_path: Path,
) -> None:
    version_dir = _broken_extract(tmp_path)
    runtime = _StandInRuntime([_marker(_server_path(version_dir))], revives_ok=False)
    with pytest.raises(AcpRuntimeDead):
        asyncio.run(runtime.spawn_repairing())
    assert (runtime.starts, runtime.revives) == (1, 1)
    assert not version_dir.exists()  # moved: the next handle unpacks a fresh copy
    assert runtime._kas_extract_broken_message == ""
    assert kas_extract_repair._BLOCKED is None


@pytest.mark.usefixtures("_repair_under")
def test_a_remote_workspace_does_not_revive_after_the_move(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    runtime = _StandInRuntime([_marker(_server_path(version_dir))])
    runtime._remote_workspace = {"close": object()}
    with pytest.raises(AcpRuntimeDead):
        asyncio.run(runtime.spawn_repairing())
    assert not version_dir.exists()  # moved: the next handle unpacks a fresh copy
    assert (runtime.starts, runtime.revives) == (1, 0)
    assert runtime._kas_extract_broken_message == ""
    assert kas_extract_repair._BLOCKED is None


def test_the_provider_translates_the_message_into_one_error() -> None:
    from kiro_crew.providers.acp_runtime_start import _kas_extract_failure

    def runtime(backend: str, message: str) -> SimpleNamespace:
        return SimpleNamespace(acp_backend=backend, kas_extract_broken_message=lambda: message)

    error = _kas_extract_failure(runtime(ACP_BACKEND_KAS, "move it"))
    assert isinstance(error, AcpKasExtractBroken) and str(error) == "move it"
    assert _kas_extract_failure(runtime(ACP_BACKEND_KAS, "")) is None
    # Any other harness is never translated, whatever the runtime carries.
    assert _kas_extract_failure(runtime(ACP_BACKEND_KIRO, "move it")) is None


def test_spawn_without_the_marker_is_untouched(tmp_path: Path) -> None:
    version_dir = _broken_extract(tmp_path)
    other = _StandInRuntime(["error: something else"])
    with pytest.raises(AcpRuntimeDead):
        asyncio.run(other.spawn_repairing())
    assert (other.starts, other.revives) == (1, 0)
    assert other._kas_extract_broken_message == ""
    assert version_dir.is_dir()
    assert kas_extract_repair._BLOCKED is None


def test_marker_is_latched_when_the_session_keeps_no_stderr(tmp_path: Path) -> None:
    """A temporary or incognito session keeps no stderr ring; the latch still sees the line."""
    server = _server_path(_broken_extract(tmp_path))

    async def drain() -> SimpleNamespace:
        reader = asyncio.StreamReader()
        reader.feed_data(f"OpenSSL noise\n{_marker(server)}\n".encode())
        reader.feed_eof()
        runtime = SimpleNamespace(
            _process=SimpleNamespace(stderr=reader),
            recording_allowed=False,
            _acp_backend=ACP_BACKEND_KAS,
            _stderr_lines=[],
            _first_session_ready=False,
            _saw_auth_failure=False,
            _saw_sandbox_init_failure=False,
            _saw_delegate_launch_failure=False,
            _kas_extract_server_path="",
        )
        await runtime_liveness._drain_stderr(runtime)
        return runtime

    runtime = asyncio.run(drain())
    assert runtime._stderr_lines == []
    assert runtime._kas_extract_server_path == str(server)


def test_a_degraded_config_read_moves_nothing(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.config import loader

    cfg = loader.KiroCrewConfig()
    assert cfg.agent.kas_repair_kiro_cli_extract is True
    _floor_monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    assert kas_extract_repair._repair_enabled() is True
    _floor_monkeypatch.setattr(cfg, "_overlay_unreadable", True)
    assert kas_extract_repair._repair_enabled() is False


# ── The real kiro-cli ──


def _real_kiro_cli() -> str | None:
    """The kiro-cli binary named by ``KIROCREW_TEST_KIRO_CLI``; opt-in only, never PATH."""
    named = os.environ.get("KIROCREW_TEST_KIRO_CLI", "")
    return named if named and os.access(named, os.X_OK) else None


_INIT = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
)


def _relay_initialize(kiro_cli: str, home: Path) -> subprocess.CompletedProcess[str]:
    """One ``initialize`` over ``kiro-cli acp --agent-engine v3`` under *home*."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "KIRO_KAS_"))
    }
    isolated = {}
    for name in (
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    ):
        path = home.parent / name.lower()
        path.mkdir(mode=0o700, exist_ok=True)
        isolated[name] = str(path)
    # XDG_DATA_HOME stays unset so kiro-cli's data dir is HOME/.local/share/kiro-cli.
    env.update(isolated, HOME=str(home), KIRO_API_KEY="placeholder-not-a-credential")
    with subprocess.Popen(
        [kiro_cli, "acp", "--agent-engine", "v3", "--auth-method", "cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=home,
    ) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(_INIT + "\n")
        proc.stdin.flush()
        silent = threading.Timer(60, proc.kill)  # a child that never answers fails here
        silent.start()
        try:
            first = proc.stdout.readline()
        finally:
            silent.cancel()
        proc.stdin.close()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        assert proc.stderr is not None
        return subprocess.CompletedProcess(proc.args, proc.returncode, first, proc.stderr.read())


@pytest.mark.timeout(240)
def test_real_kiro_cli_broken_extract_is_repaired(tmp_path: Path) -> None:
    kiro_cli = _real_kiro_cli()
    if kiro_cli is None:
        pytest.skip("opt-in: set KIROCREW_TEST_KIRO_CLI to a kiro-cli binary to run this")
    if sys.platform != "linux":
        pytest.skip("measured on Linux; the data dir layout differs elsewhere")
    home = tmp_path / "home"
    home.mkdir()
    healthy = _relay_initialize(kiro_cli, home)
    kas = _data_dir(home) / "kas"
    extracts = [p for p in kas.iterdir() if p.is_dir()] if kas.is_dir() else []
    if '"result"' not in healthy.stdout or len(extracts) != 1:
        pytest.skip(f"this kiro-cli did not unpack a KAS engine: {healthy.stderr[-300:]!r}")
    version_dir = extracts[0]
    shutil.rmtree(version_dir / "node_modules")
    _age(version_dir)

    broken = _relay_initialize(kiro_cli, home)
    server = kas_extract_repair.server_path_from_stderr(broken.stderr)
    assert broken.returncode != 0 and server is not None, broken.stderr[-500:]
    assert kas_extract_repair.version_dir_of(server) == version_dir
    # kiro-cli does not repair it on its own.
    assert kas_extract_repair.server_path_from_stderr(_relay_initialize(kiro_cli, home).stderr)

    verdict = kas_extract_repair.repair(server, enabled=True, home=home, environ={})
    assert verdict.retry and Path(verdict.renamed_to).is_dir()
    repaired = _relay_initialize(kiro_cli, home)
    assert '"result"' in repaired.stdout, repaired.stderr[-500:]
    assert _server_path(version_dir).is_file()
    # The moved copy survives kiro-cli's sweep of the entries of kas/ it does not track.
    assert Path(verdict.renamed_to).is_dir()


@pytest.mark.parametrize(
    ("backend", "expected"), [(ACP_BACKEND_KIRO, "plain"), (ACP_BACKEND_KAS, "repairing")]
)
def test_only_a_kas_runtime_takes_the_repairing_start(
    _floor_monkeypatch: pytest.MonkeyPatch, backend: str, expected: str
) -> None:
    from kiro_crew.acp import runtime as runtime_mod

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._acp_backend = backend
    rt._process = None
    taken: list[str] = []

    async def plain() -> None:
        taken.append("plain")

    async def repairing() -> None:
        taken.append("repairing")

    _floor_monkeypatch.setattr(rt, "_spawn_admitted_rederiving_once", plain, raising=False)
    _floor_monkeypatch.setattr(
        rt, "_spawn_admitted_repairing_kas_extract", repairing, raising=False
    )
    asyncio.run(rt.spawn())
    assert taken == [expected]
