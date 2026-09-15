from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat
from kiro_crew.pod import runtime, windows
from kiro_crew.pod.config import PodConfig

# Relative, not `test.e2e.scenarios`: the interpreter's own stdlib `test`
# package shadows that absolute name wherever it is installed (the CI
# hostedtoolcache Python ships it), while pytest imports this file as
# `e2e.scenarios.test_conftest`, so the sibling conftest is `.conftest`.
from . import conftest as scenarios_conftest


@pytest.fixture
def plane_driver(tmp_path, monkeypatch):
    """Drive the real fixture generator; no CLI, service or process is real."""
    root = tmp_path / "checkout"
    (root / "src/kiro_crew/static/dist").mkdir(parents=True)
    cli = root / "kirocrew.exe"
    cli.write_text("fixture executable", encoding="utf-8")
    cli.chmod(0o755)
    scratch = tmp_path / "plane"
    home = scratch / "h" / root.name
    service = tmp_path / "service-definition"
    state = SimpleNamespace(
        root=root,
        scratch=scratch,
        home=home,
        service=service,
        calls=[],
        mutations=[],
        gateway_alive=False,
        gateway_argv=f"{cli} gateway",
        failure="down",
        listing="[]",
        pid_record=True,
        handoff=True,
        keep_home=False,
    )
    rmtree = scenarios_conftest.shutil.rmtree

    def record_rmtree(path, **kwargs):
        state.mutations.append(("rmtree", path))
        rmtree(path, **kwargs)

    def remove_template():
        state.mutations.append(("template", service))
        service.unlink(missing_ok=True)

    def run_cli(_cli, argv, env, **_kwargs):
        state.calls.append(argv)
        verb = argv[1]
        if verb == "ls":
            if len(state.calls) == 1 and state.failure == "probe":
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 0, state.listing, "")
        if verb == "install":
            service.write_text("service definition", encoding="utf-8")
            if state.failure == "install":
                return subprocess.CompletedProcess(argv, 1, "", "install failed")
        if verb == "up":
            home.mkdir(parents=True)
            (home / "state.json").write_text("authoritative state", encoding="utf-8")
            cfg = PodConfig(
                pod_root=scratch / "h",
                pods_dir=scratch / "e",
                artifacts_dir=scratch / "a",
                base_port=7410,
                live_port=5476,
                unit_prefix=scenarios_conftest.PLANE_PREFIX,
                gateway_path="isolated-path",
                repo_hint=None,
                worktrees_root=None,
            )
            state.sidecars = [
                windows.task_script_path(cfg, root.name),
                windows.result_path(cfg, root.name),
            ]
            if state.handoff:
                state.sidecars.append(windows.handoff_marker_path(cfg, root.name))
            if state.pid_record:
                state.sidecars.append(windows.pid_record_path(cfg, root.name))
            for path in state.sidecars:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("keep evidence", encoding="utf-8")
            state.gateway_alive = True
            if state.failure == "up":
                return subprocess.CompletedProcess(argv, 1, "", "up failed after handoff")
            return subprocess.CompletedProcess(
                argv, 0, '{"base_url": "http://127.0.0.1:7411", "port": 7411}', ""
            )
        if verb == "down":
            if state.failure in {"down", "up"}:
                return subprocess.CompletedProcess(argv, 1, "", "stop unproven")
            if state.failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            if state.failure == "oserror":
                raise OSError("stop unavailable")
            state.gateway_alive = False
            if not state.keep_home and home.exists():
                rmtree(home)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def service_command(*args, **kwargs):
        # A service wrapper can exit while the checkout-launched gateway stays
        # alive. Even a successful /End (or bootout/stop) is not writer proof.
        state.mutations.append(("service-command", args))
        rc = 1 if args and args[0] == "/Query" else 0
        return subprocess.CompletedProcess(args, rc, "", "")

    def unexpected_kill(*args, **kwargs):
        pytest.fail(f"fixture attempted fallback process kill: {args!r}")

    monkeypatch.setenv("KIROCREW_E2E_SCENARIOS", "1")
    monkeypatch.setenv("KIROCREW_E2E_REQUIRE", "1")
    monkeypatch.setattr(runtime, "require_backend", lambda: None)
    monkeypatch.setattr(scenarios_conftest, "_repo_root", lambda: root)
    monkeypatch.setattr(scenarios_conftest.prov, "venv_bin", lambda _root: cli)
    monkeypatch.setattr(scenarios_conftest, "_plane_root", lambda *_args: scratch)
    monkeypatch.setattr(scenarios_conftest, "_resolve_backend", lambda _scratch: None)
    monkeypatch.setattr(scenarios_conftest, "_run_cli", run_cli)
    monkeypatch.setattr(scenarios_conftest, "_remove_plane_unit_template", remove_template)
    monkeypatch.setattr(scenarios_conftest.shutil, "rmtree", record_rmtree)
    monkeypatch.setattr(scenarios_conftest.PodClient, "health", lambda _self: 200)
    monkeypatch.setattr(scenarios_conftest.PodClient, "api", lambda *_a, **_kw: {"ok": True})
    monkeypatch.setattr(scenarios_conftest.subprocess, "run", service_command)
    monkeypatch.setattr(windows, "schtasks", service_command)
    monkeypatch.setattr(platform_compat, "live_thread_group_leaders", lambda: {12345})
    monkeypatch.setattr(platform_compat, "process_command_line", lambda _pid: state.gateway_argv)
    monkeypatch.setattr(platform_compat, "process_start_time", lambda _pid: "pinned-start")
    monkeypatch.setattr(platform_compat, "kill_pid_pinned", unexpected_kill)
    factory = SimpleNamespace(getbasetemp=lambda: tmp_path)
    return scenarios_conftest.pod.__wrapped__(factory), state


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("failure", ["down", "timeout", "oserror", "up"])
@pytest.mark.parametrize("record", ["pid-record", "handoff-only", "no-record"])
def test_failed_stop_preserves_gateway_without_argv_marker(
    plane_driver, monkeypatch, platform, failure, record
):
    generator, state = plane_driver
    # Replace only this module's sys reference, not the interpreter's platform.
    monkeypatch.setattr(scenarios_conftest, "sys", SimpleNamespace(platform=platform))
    state.failure = failure
    state.pid_record = record == "pid-record"
    state.handoff = record != "no-record"
    if failure == "up":
        with pytest.raises(pytest.fail.Exception, match="up.*failed"):
            next(generator)
    else:
        next(generator)
        with pytest.raises(AssertionError) as stop_error:
            next(generator)

    assert str(state.scratch) not in state.gateway_argv
    assert state.gateway_alive
    assert state.mutations == []
    assert state.service.read_text(encoding="utf-8") == "service definition"
    assert (state.home / "state.json").read_text(encoding="utf-8") == "authoritative state"
    assert all(path.read_text(encoding="utf-8") == "keep evidence" for path in state.sidecars)
    assert state.calls[-2:] == [["pod", "down", "checkout"], ["pod", "ls", "--json"]]
    if failure != "up":
        assert "Kept plane=" in str(stop_error.value)
        assert "no-writer shutdown was not proven" in str(stop_error.value)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_confirmed_stop_cleans_plane(plane_driver, monkeypatch, platform):
    generator, state = plane_driver
    monkeypatch.setattr(scenarios_conftest, "sys", SimpleNamespace(platform=platform))
    state.failure = ""
    next(generator)
    with pytest.raises(StopIteration):
        next(generator)
    assert not state.gateway_alive
    assert not state.scratch.exists()
    assert not state.service.exists()
    assert state.mutations == [("rmtree", state.scratch), ("template", state.service)]


@pytest.mark.parametrize("listing", ["", "not-json", "null", "{}", '[{"name": "checkout"}]'])
def test_unconfirmed_listing_preserves_plane(plane_driver, listing):
    generator, state = plane_driver
    state.failure = ""
    next(generator)
    state.listing = listing
    with pytest.raises(AssertionError, match="Kept plane="):
        next(generator)
    assert state.mutations == []
    assert state.scratch.exists()
    assert state.service.exists()


def test_successful_down_with_home_residue_preserves_evidence(plane_driver):
    generator, state = plane_driver
    state.failure = ""
    state.keep_home = True
    next(generator)
    with pytest.raises(AssertionError, match="left the isolated home behind"):
        next(generator)
    assert state.mutations == []
    assert state.home.exists()
    assert state.service.exists()


@pytest.mark.parametrize("failure", ["probe", "install"])
def test_pre_boot_failure_cleans_unstarted_plane(plane_driver, failure):
    generator, state = plane_driver
    state.failure = failure
    with pytest.raises((pytest.fail.Exception, AssertionError)):
        next(generator)
    assert not state.gateway_alive
    assert not state.scratch.exists()
    assert not state.service.exists()


def test_new_plane_does_not_sweep_dead_pytest_owner(tmp_path, monkeypatch):
    stale = tmp_path / "kce2e-99999999999"
    stale.mkdir()
    (stale / scenarios_conftest._PLANE_MARKER).write_text("99999999999", encoding="ascii")
    state_file = stale / "state.json"
    state_file.write_text("service may still be writing", encoding="utf-8")
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(scenarios_conftest.tempfile, "gettempdir", lambda: str(tmp_path))
    # Exercise the fallback selection without a real AF_UNIX-length constraint.
    monkeypatch.setattr(scenarios_conftest, "_AF_UNIX_MAX", 10000)
    selected = scenarios_conftest._plane_root("checkout", None)
    assert selected.parent == tmp_path
    assert selected != stale
    assert state_file.read_text(encoding="utf-8") == "service may still be writing"


def test_preexisting_plane_is_not_adopted_or_cleaned(plane_driver):
    generator, state = plane_driver
    state.scratch.mkdir()
    evidence = state.scratch / "state.json"
    evidence.write_text("previous gateway may still be writing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        next(generator)
    assert state.calls == []
    assert state.mutations == []
    assert evidence.read_text(encoding="utf-8") == "previous gateway may still be writing"
