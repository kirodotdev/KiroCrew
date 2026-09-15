"""Windows run publication and reclamation use durable identity, not polling history."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew.pod import _windows_run as runs
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import PodConfig


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
    cfg = PodConfig.load()
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    state = SimpleNamespace(events=[], active=1, job_missing=False, failure="", count=0)
    monkeypatch.setattr(runs.pc, "process_start_time", lambda pid: "100")
    monkeypatch.setattr(win, "process_start_time", lambda pid: "100")
    monkeypatch.setattr(win, "pid_exists", lambda pid: False)
    monkeypatch.setattr(win, "_live_children_of", lambda *_a: [])
    monkeypatch.setattr(win.run_marker, "read_pid_record_path", lambda *_a: None)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setattr(rt.time, "sleep", lambda *_a: None)

    class Job:
        name = "Global\\KiroCrew.Pod." + "a" * 32

        @classmethod
        def create(cls):
            state.events.append("create_job")
            return cls()

        @classmethod
        def open_existing(cls, name):
            assert name == cls.name
            state.events.append("open_job")
            if state.job_missing:
                raise OSError("job missing")
            return cls()

        def assign_suspended(self, handle):
            assert handle == 8001
            state.events.append("assign")
            if state.failure == "assign":
                raise OSError("assignment denied")

        def contains(self, handle):
            return state.failure != "membership"

        def terminate_and_wait(self, *, timeout):
            state.events.append("job_zero")
            if state.failure == "query":
                raise OSError("job accounting unavailable")
            state.active = 0

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            state.events.append("close_job")

    monkeypatch.setattr(win.jobs, "PodJob", Job)
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: 8100)
    monkeypatch.setattr(
        win.jobs, "close_identity", lambda handle: state.events.append("close_identity")
    )

    def retire(handle, **_kw):
        state.events.append("retire")
        if state.failure == "publisher":
            raise TimeoutError("publisher still running")

    monkeypatch.setattr(win.jobs, "retire_identity", retire)

    def task(*args):
        state.events.append(args[0])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(win, "schtasks", task)
    return cfg, state


def contained(cfg, name="demo"):
    runs.reserve(cfg, name)
    record = runs.claim(cfg, name)
    # Model a different supervisor, never the current pytest process.
    record["publisher"] = [777, "100"]
    runs.publish(cfg, name, record)
    return runs.ready(cfg, name, record, "Global\\KiroCrew.Pod." + "a" * 32, (4242, "100"))


def test_disappearing_successor_record_uses_job_not_empty_old_tree(model, monkeypatch):
    cfg, state = model
    record = contained(cfg)
    cfg.home_dir("demo").mkdir(parents=True)
    (cfg.home_dir("demo") / "state").write_text("owned", encoding="utf-8")
    win.write_task_script(cfg, "demo")
    win.record_supervised_pid(cfg, "demo", 4242)
    # A successful empty legacy scan must not become cleanup authority.
    monkeypatch.setattr(win.jobs.pc, "descendant_termination_handles", lambda *_a, **_kw: {})

    def task(*args):
        state.events.append(args[0])
        if args[0] == "/End":
            assert win._begin_handoff(cfg, "demo")
            win.record_supervised_pid(cfg, "demo", 4300)
            # A successor exits while its unobserved descendant stays in the Job.
            win._end_handoff(cfg, "demo")
            win.clear_supervised_pid(cfg, "demo")
            assert state.active == 1
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(win, "schtasks", task)
    real_cleanup = rt.cleanup_home

    def cleanup(config, name):
        assert state.active == 0, "Job must be empty before HOME reclamation"
        assert runs.read(cfg, name)["generation"] == record["generation"]
        assert runs.read(cfg, name)["state"] == "drained"
        state.count += 1
        return real_cleanup(config, name)

    monkeypatch.setattr(rt, "cleanup_home", cleanup)
    result = rt.stop_pod(cfg, "demo")
    assert result.returncode == 0, result.stderr
    assert (
        state.events.index("retire")
        < state.events.index("job_zero")
        < state.events.index("/Delete")
    )
    assert state.count == 7
    assert not cfg.home_dir("demo").exists()
    assert runs.read(cfg, "demo") is None
    assert "create_job" not in state.events


@pytest.mark.parametrize("failure", ["publisher", "query", "membership", "missing"])
def test_incomplete_barrier_or_job_proof_preserves_home(model, monkeypatch, failure):
    cfg, state = model
    contained(cfg)
    state.failure = failure
    state.job_missing = failure == "missing"
    monkeypatch.setattr(rt, "cleanup_home", lambda *_a: pytest.fail("no reclamation authority"))
    result = rt.stop_pod(cfg, "demo")
    assert result.returncode == 1
    assert "/Delete" not in state.events
    assert runs.read(cfg, "demo")["state"] == "ready"
    if failure == "publisher":
        assert "job_zero" not in state.events


@pytest.mark.parametrize("evidence", ["home", "record", "reserved", "preparing", "malformed"])
def test_legacy_and_incomplete_boots_fail_closed(model, monkeypatch, evidence):
    cfg, state = model
    if evidence == "home":
        cfg.home_dir("demo").mkdir(parents=True)
    elif evidence == "record":
        win.record_supervised_pid(cfg, "demo", 4242)
    elif evidence == "malformed":
        runs.path(cfg, "demo").write_text("{}", encoding="utf-8")
    else:
        runs.reserve(cfg, "demo")
        if evidence == "preparing":
            runs.claim(cfg, "demo")
    result = win.stop(cfg, "demo", timeout=0)
    assert result.returncode == 1
    assert "/End" not in state.events and "/Delete" not in state.events


def test_cleanup_failure_retains_receipt_for_retry_without_a_job(model, monkeypatch):
    cfg, state = model
    contained(cfg)
    cfg.home_dir("demo").mkdir(parents=True)
    monkeypatch.setattr(rt, "cleanup_home", lambda *_a: 1)
    first = rt.stop_pod(cfg, "demo")
    assert first.returncode == 1
    assert runs.read(cfg, "demo")["state"] == "drained"
    state.events.clear()
    state.job_missing = True
    cfg.home_dir("demo").rmdir()
    second = rt.stop_pod(cfg, "demo")
    assert second.returncode == 0, second.stderr
    assert "open_job" not in state.events
    assert "retire" in state.events
    assert runs.read(cfg, "demo") is None


def test_changed_plane_and_duplicate_boot_cannot_replace_evidence(model):
    cfg, _state = model
    record = contained(cfg)
    before = runs.path(cfg, "demo").read_bytes()
    with pytest.raises(OSError):
        runs.reserve(cfg, "demo")
    with pytest.raises(OSError):
        runs.claim(cfg, "demo")
    assert runs.path(cfg, "demo").read_bytes() == before
    record["plane"] = ["foreign"]
    runs.publish(cfg, "demo", record)
    with pytest.raises(OSError):
        runs.read(cfg, "demo")


@pytest.mark.parametrize("failure", ["", "assign", "identity", "publication", "resume"])
def test_boot_assigns_and_publishes_before_resume(model, monkeypatch, tmp_path, failure):
    cfg, state = model
    runs.reserve(cfg, "demo")
    generation = runs.read(cfg, "demo")["generation"]
    state.failure = failure

    class Proc:
        pid = 4242
        _handle = 8001
        exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout=None):
            self.exited = True
            return 0

        def kill(self):
            state.events.append("kill_original")
            self.exited = True

    proc = Proc()
    monkeypatch.setattr(win.subprocess, "Popen", lambda *_a, **_kw: proc)
    monkeypatch.setattr(
        win.jobs.pc,
        "_windows_process_handle_identity",
        lambda *_a: None if failure == "identity" else (4242, 100, None),
    )
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda *_a: False)
    if failure == "publication":
        monkeypatch.setattr(
            runs, "ready", lambda *_a: (_ for _ in ()).throw(OSError("write denied"))
        )

    def resume(pid):
        state.events.append("resume")
        assert "assign" in state.events
        current = runs.read(cfg, "demo")
        assert current["state"] == "ready"
        assert current["generation"] == generation
        return failure != "resume"

    monkeypatch.setattr(win, "resume_process_main_thread", resume)
    result = win.supervise_gateway(
        cfg,
        "demo",
        tmp_path / "gateway",
        ["gateway"],
        {},
        gateway_pid_record=tmp_path / "gateway.pid",
    )
    assert (result == 0) is (failure == "")
    if failure in {"assign", "identity", "publication"}:
        assert "resume" not in state.events
        assert "kill_original" in state.events
        assert runs.read(cfg, "demo")["state"] == "preparing"
    else:
        assert runs.read(cfg, "demo")["state"] == "drained"
    assert state.active == 0


def test_fresh_start_reserves_before_task_creation(model):
    cfg, state = model

    def task(*args):
        state.events.append(args[0])
        if args[0] == "/Query":
            return subprocess.CompletedProcess([], 1, "", "")
        assert runs.read(cfg, "demo")["state"] == "reserved"
        return subprocess.CompletedProcess([], 0, "", "")

    # This test controls every scheduler operation; it never registers a task.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(win, "schtasks", task)
        result = win.start(cfg, "demo")
    assert result.returncode == 0
    assert state.events == ["/Query", "/Create", "/Run"]
    before = runs.path(cfg, "demo").read_bytes()
    state.events.clear()
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == before
    assert state.events == []


@pytest.mark.parametrize("preexisting_home", [False, True])
def test_public_up_prepares_home_only_after_start_reservation(
    model, monkeypatch, tmp_path, preexisting_home
):
    import argparse

    from kiro_crew.pod import cli

    cfg, state = model
    checkout = tmp_path / "checkout"
    binary = checkout / "fake-gateway"
    (checkout / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
    binary.write_text("unused", encoding="utf-8")
    binary.chmod(0o700)
    if preexisting_home:
        rt.write_pod_config(cfg.home_dir("demo"), "")
    monkeypatch.setattr(cli, "_resolve_or_die", lambda *_a: checkout)
    monkeypatch.setattr(cli, "_audit", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli.prov, "has_venv", lambda *_a: True)
    monkeypatch.setattr(cli.prov, "has_dist", lambda *_a: True)
    monkeypatch.setattr(rt.prov, "venv_bin", lambda *_a: binary)
    monkeypatch.setattr(rt, "allocate_port", lambda *_a: (8611, None))
    monkeypatch.setattr(rt, "_seed_pod_os_home", lambda *_a: None)
    monkeypatch.setattr(rt, "_probe_pod_child_bootstrap", lambda *_a: None)
    monkeypatch.setattr(rt, "target_supports_flag", lambda *_a: False)
    monkeypatch.setattr(rt, "mint_token", lambda *_a: "test-token")
    monkeypatch.setattr(cli, "_wait_healthy", lambda *_a, **_kw: 200)
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda *_a: False)
    monkeypatch.setattr(
        win.jobs.pc, "_windows_process_handle_identity", lambda *_a: (4242, 100, None)
    )
    monkeypatch.setattr(win, "resume_process_main_thread", lambda *_a: True)

    class Proc:
        pid = 4242
        _handle = 8001

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def spawn(*_a, **_kw):
        assert cfg.home_dir("demo").is_dir()
        assert runs.read(cfg, "demo")["state"] == "preparing"
        return Proc()

    monkeypatch.setattr(win.subprocess, "Popen", spawn)

    def scheduler(*args):
        state.events.append(args[0])
        if args[0] == "/Query":
            return subprocess.CompletedProcess([], 1, "", "")
        if args[0] == "/Create":
            assert not cfg.home_dir("demo").exists()
            assert runs.read(cfg, "demo")["state"] == "reserved"
        if args[0] == "/Run":
            assert rt.boot(cfg, "demo") == 0
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(win, "schtasks", scheduler)
    args = argparse.Namespace(
        pod_action="up", name="demo", seed=None, ttl=1, json=True, provision=False
    )
    if preexisting_home:
        with pytest.raises(SystemExit):
            cli.dispatch(args)
        assert "/Create" not in state.events
        assert cfg.home_dir("demo").is_dir()
    else:
        cli.dispatch(args)
        assert "/Run" in state.events and "assign" in state.events
        assert runs.read(cfg, "demo")["state"] == "drained"


def test_disappearing_record_negative_control_detects_omitted_job_drain(model, monkeypatch):
    """Bypassing the replacement proof must trip the HOME-deletion oracle."""
    monkeypatch.setattr(win.jobs.PodJob, "terminate_and_wait", lambda *_a, **_kw: None)
    with pytest.raises(AssertionError, match="Job must be empty before HOME reclamation"):
        test_disappearing_successor_record_uses_job_not_empty_old_tree(model, monkeypatch)


@pytest.mark.parametrize("sidecar", ["handoff", "pid", "result"])
def test_sidecar_unlink_failure_retains_receipt_until_retry_and_next_start(
    model, monkeypatch, sidecar
):
    cfg, state = model
    record = contained(cfg)
    paths = {
        "handoff": win.handoff_marker_path(cfg, "demo"),
        "pid": win.pid_record_path(cfg, "demo"),
        "result": win.result_path(cfg, "demo"),
    }
    for path in paths.values():
        path.write_text("4242\n100\n", encoding="utf-8")
    target = paths[sidecar]
    original_bytes = target.read_bytes()
    wrapper = win.write_task_script(cfg, "demo")
    home = cfg.home_dir("demo")
    home.mkdir(parents=True)
    payload = home / "owned-state"
    payload.write_text("preserve until retirement cleanup succeeds", encoding="utf-8")
    scheduler_state = {"exists": True}

    def scheduler(*args):
        state.events.append(args[0])
        if args[0] == "/Query":
            return subprocess.CompletedProcess([], int(not scheduler_state["exists"]), "", "")
        if args[0] == "/Delete":
            existed = scheduler_state["exists"]
            scheduler_state["exists"] = False
            return subprocess.CompletedProcess([], int(not existed), "", "")
        if args[0] == "/Create":
            scheduler_state["exists"] = True
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(win, "schtasks", scheduler)
    original_unlink = type(target).unlink
    denied = []

    def unlink(path, *args, **kwargs):
        if path == target and not denied:
            denied.append(path)
            raise PermissionError(13, "one-time sidecar sharing violation", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "unlink", unlink)
    original_cleanup = rt.cleanup_home

    def cleanup(config, name):
        assert runs.read(config, name) == {**record, "state": "drained"}
        state.count += 1
        return original_cleanup(config, name)

    monkeypatch.setattr(rt, "cleanup_home", cleanup)
    first = rt.stop_pod(cfg, "demo")
    assert first.returncode == 1, "sidecar deletion failure must not report successful cleanup"
    assert str(target) in first.stderr
    assert denied == [target] and target.read_bytes() == original_bytes
    assert runs.read(cfg, "demo") == {**record, "state": "drained"}
    if sidecar != "result":
        assert payload.read_text(encoding="utf-8") == "preserve until retirement cleanup succeeds"
        assert state.count == 0
    else:
        assert not home.exists() and state.count == 7
    assert not scheduler_state["exists"]

    state.job_missing = True
    state.events.clear()
    second = rt.stop_pod(cfg, "demo")
    assert second.returncode == 0, second.stderr
    assert "open_job" not in state.events and "create_job" not in state.events
    assert state.count == (14 if sidecar == "result" else 7)
    assert runs.read(cfg, "demo") is None
    assert all(not path.exists() for path in paths.values())
    assert not wrapper.exists() and not home.exists()

    assert win.start(cfg, "demo").returncode == 0
    fresh = runs.read(cfg, "demo")
    assert fresh["state"] == "reserved" and fresh["generation"] != record["generation"]


@pytest.mark.parametrize("kind", ["handoff", "pid"])
def test_supervisor_sidecar_cleanup_remains_best_effort(model, monkeypatch, kind):
    cfg, _state = model
    path = (win.handoff_marker_path if kind == "handoff" else win.pid_record_path)(cfg, "demo")
    path.write_text("diagnostic evidence", encoding="utf-8")
    original_unlink = type(path).unlink

    def unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("sidecar locked")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(type(path), "unlink", unlink)
    (win._end_handoff if kind == "handoff" else win.clear_supervised_pid)(cfg, "demo")
    assert path.read_text(encoding="utf-8") == "diagnostic evidence"
