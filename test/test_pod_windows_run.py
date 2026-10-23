"""Windows run publication and reclamation use durable identity, not polling history."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.pod import _windows_run as runs
from kiro_crew.pod import cli as pod_cli
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
    monkeypatch.setattr(rt, "IS_LINUX", False)
    # Task Scheduler probing has its own tests; this fixture only simulates win32 dispatch.
    monkeypatch.setattr(rt, "require_backend", lambda: None)
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
    # A `finished` producer is given the stop budget to exit on its own before
    # it is ended; the model's producer outlives that budget unless a test says
    # it exited. The budget each wait was given is recorded for the ordering test.
    state.exited = False
    state.waits = []

    def wait(handle, *, timeout):
        state.events.append("wait")
        state.waits.append(timeout)
        return state.exited

    monkeypatch.setattr(win.jobs, "wait_identity", wait)

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
    runs.claim(cfg, "demo")  # The boot claims first; the supervisor checks the claim.
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
    expected_error = PermissionError(13, "one-time sidecar sharing violation", str(target))
    assert str(expected_error) in first.stderr
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


@pytest.fixture
def startup(model, monkeypatch):
    cfg, state = model
    state.task_exists = False
    state.generations = []
    reserve = runs.reserve

    def reserve_run(*args):
        record = reserve(*args)
        state.generations.append(record["generation"])
        return record

    def scheduler(*args):
        state.events.append(args[0])
        if args[0] == "/Query":
            return subprocess.CompletedProcess([], int(not state.task_exists), "", "")
        if args[0] == "/Create":
            state.task_exists = True
        elif args[0] == "/Delete":
            state.task_exists = False
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(runs, "reserve", reserve_run)
    monkeypatch.setattr(win, "schtasks", scheduler)
    return cfg, state


@pytest.mark.parametrize(
    "failure",
    ["render", "encoding", "mkdir", "write", "stale", "create_rc", "create_os", "create_timeout"],
)
def test_preclaim_failure_rolls_back_and_next_start_retries(startup, monkeypatch, failure):
    cfg, state = startup
    wrapper = win.task_script_path(cfg, "demo")
    result = win.result_path(cfg, "demo")
    with monkeypatch.context() as patch:
        if failure == "render":
            patch.setattr(win, "render_task_script", lambda *_a: win._cmd_literal('bad"path'))
        elif failure == "encoding":
            patch.setattr(win, "render_task_script", lambda *_a: "\u2603")
            patch.setattr(win, "_script_encoding", lambda: "ascii")
        elif failure == "mkdir":
            mkdir = type(wrapper).mkdir

            def fail_mkdir(path, *args, **kwargs):
                if path == win.log_paths(cfg, "demo")[0].parent:
                    raise PermissionError("log directory denied")
                return mkdir(path, *args, **kwargs)

            patch.setattr(type(wrapper), "mkdir", fail_mkdir)
        elif failure == "write":
            write = type(wrapper).write_bytes

            def partial_write(path, data):
                if path == wrapper:
                    write(path, data[:8])
                    raise OSError("partial wrapper write")
                return write(path, data)

            patch.setattr(type(wrapper), "write_bytes", partial_write)
        elif failure == "stale":
            unlink = type(result).unlink
            attempts = []

            def fail_unlink(path, *args, **kwargs):
                if path == result and not attempts:
                    attempts.append(path)
                    path.write_text("70", encoding="utf-8")
                    raise PermissionError("stale result locked")
                return unlink(path, *args, **kwargs)

            patch.setattr(type(result), "unlink", fail_unlink)
        else:
            scheduler = win.schtasks

            def fail_create(*args):
                response = scheduler(*args)
                if args[0] == "/Create":
                    # A failed command can still leave a partial registration.
                    if failure == "create_os":
                        raise OSError("create unavailable")
                    if failure == "create_timeout":
                        raise subprocess.TimeoutExpired("schtasks", 30)
                    return subprocess.CompletedProcess([], 5, "", "create denied")
                return response

            patch.setattr(win, "schtasks", fail_create)
        first = win.start(cfg, "demo")
    assert first.returncode != 0 and first.stderr
    assert "/Run" not in state.events
    assert runs.read(cfg, "demo") is None, "failed preclaim start must release its reservation"
    assert not wrapper.exists() and not result.exists() and not state.task_exists
    assert not cfg.home_dir("demo").exists()
    assert win.start(cfg, "demo").returncode == 0
    assert len(set(state.generations)) == 2


@pytest.mark.parametrize(
    "failure", ["query", "delete_rc", "delete_os", "wrapper", "result", "record"]
)
def test_cancelled_cleanup_is_honest_and_retryable(startup, monkeypatch, failure):
    cfg, state = startup
    scheduler = win.schtasks
    with monkeypatch.context() as patch:

        def fail_scheduler(*args):
            if args[0] == "/Query" and state.task_exists and failure == "query":
                raise OSError("query unavailable")
            if args[0] == "/Delete":
                with pytest.raises(OSError):
                    runs.claim(cfg, "demo")
                if failure == "delete_rc":
                    return subprocess.CompletedProcess([], 5, "", "delete denied")
                if failure == "delete_os":
                    raise OSError("delete unavailable")
            response = scheduler(*args)
            if args[0] == "/Create":
                win.result_path(cfg, "demo").write_text("70", encoding="utf-8")
                return subprocess.CompletedProcess([], 5, "", "create denied")
            return response

        patch.setattr(win, "schtasks", fail_scheduler)
        targets = {
            "wrapper": win.task_script_path(cfg, "demo"),
            "result": win.result_path(cfg, "demo"),
            "record": runs.path(cfg, "demo"),
        }
        if failure in targets:
            target = targets[failure]
            unlink = type(target).unlink

            def fail_unlink(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("cleanup locked")
                return unlink(path, *args, **kwargs)

            patch.setattr(type(target), "unlink", fail_unlink)
        first = win.start(cfg, "demo")
    assert first.returncode == 1 and "rollback incomplete" in first.stderr
    record = runs.read(cfg, "demo")
    assert record["state"] == "cancelled"
    with pytest.raises(OSError):
        runs.claim(cfg, "demo")
    assert win.start(cfg, "demo").returncode == 0
    assert runs.read(cfg, "demo")["generation"] != record["generation"]


@pytest.mark.parametrize("changed", ["preparing", "ready", "drained", "generation", "malformed"])
def test_start_rollback_preserves_claimed_or_changed_run(startup, monkeypatch, changed):
    cfg, state = startup
    expected = []

    def fail_write(*_args):
        if changed == "malformed":
            runs.path(cfg, "demo").write_text("{}", encoding="utf-8")
        elif changed == "generation":
            record = runs.read(cfg, "demo")
            record["generation"] = "b" * 32
            runs.publish(cfg, "demo", record)
        else:
            record = runs.claim(cfg, "demo")
            if changed in {"ready", "drained"}:
                record = runs.ready(
                    cfg, "demo", record, "Global\\KiroCrew.Pod." + "a" * 32, (4242, "100")
                )
            if changed == "drained":
                runs.drained(cfg, "demo", record)
        win.task_script_path(cfg, "demo").write_text("keep evidence", encoding="utf-8")
        expected.append(runs.path(cfg, "demo").read_bytes())
        raise OSError("write failed after competing publication")

    monkeypatch.setattr(win, "write_task_script", fail_write)
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == expected[0]
    assert win.task_script_path(cfg, "demo").read_text(encoding="utf-8") == "keep evidence"
    assert state.events == ["/Query"]


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("raised", [False, True])
def test_uncertain_run_failure_never_rolls_back(startup, monkeypatch, claimed, raised):
    cfg, state = startup
    scheduler = win.schtasks
    expected = []

    def fail_run(*args):
        response = scheduler(*args)
        if args[0] == "/Run":
            if claimed:
                runs.claim(cfg, "demo")
            expected.append(runs.path(cfg, "demo").read_bytes())
            if raised:
                raise subprocess.TimeoutExpired("schtasks", 30)
            return subprocess.CompletedProcess([], 5, "", "run may have started")
        return response

    monkeypatch.setattr(win, "schtasks", fail_run)
    assert win.start(cfg, "demo").returncode != 0
    assert runs.path(cfg, "demo").read_bytes() == expected[0]
    assert win.task_script_path(cfg, "demo").exists() and state.task_exists
    assert "/Delete" not in state.events
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == expected[0]


def test_start_rollback_negative_control_detects_leaked_reservation(startup, monkeypatch):
    monkeypatch.setattr(win, "_rollback_start", lambda *_a: None)
    with pytest.raises(AssertionError, match="failed preclaim start must release"):
        test_preclaim_failure_rolls_back_and_next_start_retries(startup, monkeypatch, "write")


@pytest.mark.parametrize("evidence", ["home", "pid", "handoff"])
def test_cancelled_start_never_discards_ambiguous_runtime_evidence(startup, monkeypatch, evidence):
    cfg, state = startup
    target = {
        "home": cfg.home_dir("demo"),
        "pid": win.pid_record_path(cfg, "demo"),
        "handoff": win.handoff_marker_path(cfg, "demo"),
    }[evidence]

    def fail_write(*_a):
        if evidence == "home":
            target.mkdir(parents=True)
        else:
            target.write_text("unknown writer", encoding="utf-8")
        raise OSError("unexpected runtime evidence")

    monkeypatch.setattr(win, "write_task_script", fail_write)
    assert win.start(cfg, "demo").returncode == 1
    before = runs.path(cfg, "demo").read_bytes()
    assert win.start(cfg, "demo").returncode == 1
    assert target.exists() and runs.path(cfg, "demo").read_bytes() == before
    assert state.events == ["/Query"]


def test_rollback_lock_contention_and_publication_failure_preserve_reservation(
    startup, monkeypatch
):
    cfg, state = startup
    record = runs.reserve(cfg, "demo")
    before = runs.path(cfg, "demo").read_bytes()
    with runs.claim_lock(cfg, "demo"):
        with pytest.raises(OSError):
            win._rollback_start(cfg, "demo", record)
    assert runs.path(cfg, "demo").read_bytes() == before
    with monkeypatch.context() as patch:
        patch.setattr(runs, "publish", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            win._rollback_start(cfg, "demo", record)
    assert runs.path(cfg, "demo").read_bytes() == before
    assert state.events == []
    # An ordinary reservation cannot be inferred to be a failed pre-/Run start.
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == before


@pytest.mark.parametrize("field", ["publisher", "root", "job"])
def test_unclaimed_records_with_runtime_identity_are_not_cancellation_proof(startup, field):
    cfg, _state = startup
    record = runs.reserve(cfg, "demo")
    record.update(state="cancelled")
    record[field] = "unexpected runtime identity"
    runs.publish(cfg, "demo", record)
    before = runs.path(cfg, "demo").read_bytes()
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == before


@pytest.mark.parametrize("failure", ["checkout", "venv", "dist"])
def test_scheduled_preclaim_refusal_is_retired_by_down_and_retried_by_up(
    startup, monkeypatch, tmp_path, failure
):
    """A provisioning refusal is written by a claimed producer `down` can retire.

    The boot claims the reservation before its checkout, venv and dist checks, so
    the refusal's record names the producer (pid + creation time) and carries its
    `finished` receipt and exit code. While that producer is still on its call
    stack `stop` refuses; once it is gone `stop` retires the generation and the
    next `up` admits a fresh one, with no reboot and no manual deletion.
    """
    cfg, state = startup
    checkout = tmp_path / "checkout"
    binary = checkout / "gateway"
    if failure != "checkout":
        checkout.mkdir()
        rt.write_env_file(cfg, "demo", {"CHECKOUT": str(checkout)})
    if failure == "dist":
        binary.write_bytes(b"unused")
        binary.chmod(0o700)
    monkeypatch.setattr(rt.prov, "venv_bin", lambda *_a: binary)
    scheduler = win.schtasks
    record_refusal = rt._record_refusal
    boot_results = []

    def before_refusal_write(config, name, reason):
        # The producer is still running: its record names THIS process, so the
        # only honest answer is to refuse, never to race its remaining writes.
        record = runs.read(config, name)
        assert record["state"] == "preparing" and record["publisher"][0] == os.getpid()
        stopped = win.stop(config, name, timeout=0)
        assert stopped.returncode == 1 and "calling process" in stopped.stderr
        assert runs.read(config, name) == record
        assert state.task_exists and not config.home_dir(name).exists()
        record_refusal(config, name, reason)

    def run_wrapper(*args):
        response = scheduler(*args)
        if args[0] == "/Run":
            boot_results.append(rt.boot(cfg, "demo"))
        return response

    monkeypatch.setattr(rt, "_record_refusal", before_refusal_write)
    monkeypatch.setattr(win, "schtasks", run_wrapper)
    assert win.start(cfg, "demo").returncode == 0  # Scheduler accepted /Run.
    assert boot_results == [3]
    finished = runs.read(cfg, "demo")
    assert finished["state"] == "finished" and finished["exit"] == 3
    assert finished["publisher"][0] == os.getpid()
    assert rt.refusal_reason(cfg, "demo")
    assert win.last_result(cfg, "demo") == 3  # Written by the producer itself.
    assert win.unit_state(cfg, "demo") == ("failed", 1)
    # The producer has exited. Model it as a different, gone process.
    finished["publisher"] = [777, "100"]
    runs.publish(cfg, "demo", finished)
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    monkeypatch.setattr(win, "schtasks", scheduler)
    stopped = rt.stop_pod(cfg, "demo")
    assert stopped.returncode == 0, stopped.stderr
    assert runs.read(cfg, "demo") is None
    assert not state.task_exists and not win.task_script_path(cfg, "demo").exists()
    assert win.last_result(cfg, "demo") is None
    assert "/End" in state.events and "/Delete" in state.events
    assert "create_job" not in state.events and "retire" not in state.events
    assert win.start(cfg, "demo").returncode == 0
    fresh = runs.read(cfg, "demo")
    assert fresh["state"] == "reserved" and fresh["generation"] != finished["generation"]


def _unspawned(cfg, state="preparing", publisher=(777, "100"), exit_code=3):
    """A claimed generation whose producer is a different process."""
    runs.reserve(cfg, "demo")
    record = runs.claim(cfg, "demo")
    record["publisher"] = list(publisher)
    runs.publish(cfg, "demo", record)
    if state == "finished":
        runs.finished(cfg, "demo", record, exit_code)
    return runs.read(cfg, "demo")


def test_producer_killed_before_any_result_is_retired_when_nothing_else_remains(
    startup, monkeypatch
):
    """Hard kill right after the claim: no result, no refusal note, no HOME.

    `open_identity` answering None is the kernel's word that the pid is gone or
    names another process. With no gateway evidence and no HOME the generation is
    retired, the task deleted, and a new `up` admitted.
    """
    cfg, state = startup
    state.task_exists = True
    win.task_script_path(cfg, "demo").write_text("rem wrapper", encoding="utf-8")
    _unspawned(cfg)
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    stopped = rt.stop_pod(cfg, "demo")
    assert stopped.returncode == 0, stopped.stderr
    assert runs.read(cfg, "demo") is None
    assert not state.task_exists and not win.task_script_path(cfg, "demo").exists()
    assert "retire" not in state.events
    assert win.start(cfg, "demo").returncode == 0
    assert runs.read(cfg, "demo")["state"] == "reserved"


def test_live_provisioner_is_waited_for_never_terminated(startup, monkeypatch):
    """`preparing` with a live producer is a boot in progress.

    `stop` waits on the pinned process object up to its timeout. If the object
    stays live it refuses and leaves everything in place; once the object
    signals, the same call retires the generation. `retire_identity` is never
    reached on this path, so an in-flight provisioner cannot be killed.
    """
    cfg, state = startup
    state.task_exists = True
    record = _unspawned(cfg)
    before = runs.path(cfg, "demo").read_bytes()
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: 8100)
    monkeypatch.setattr(win.jobs, "retire_identity", lambda *_a, **_kw: pytest.fail("killed"))
    exited = {"value": False}
    monkeypatch.setattr(win.jobs, "wait_identity", lambda *_a, **_kw: exited["value"])
    refused = win.stop(cfg, "demo", timeout=0)
    assert refused.returncode == 1 and "still in progress" in refused.stderr
    assert runs.path(cfg, "demo").read_bytes() == before
    assert not {"/End", "/Delete"}.intersection(state.events)
    exited["value"] = True
    assert win.stop(cfg, "demo", timeout=0).returncode == 0
    assert runs.read(cfg, "demo")["state"] == "retired"
    assert runs.read(cfg, "demo")["generation"] == record["generation"]
    assert "/End" in state.events and "/Delete" in state.events


def test_finished_producer_still_exiting_is_retired_not_raced(startup, monkeypatch):
    """A `finished` producer that outlives the stop budget is ended, then cleaned.

    `stop` retires the exact pinned handle and waits for its object before it
    touches a sidecar, so the result the producer wrote cannot land after cleanup.
    """
    cfg, state = startup
    state.task_exists = True
    win.record_result(cfg, "demo", 3)
    _unspawned(cfg, state="finished")
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: 8100)
    stopped = rt.stop_pod(cfg, "demo")
    assert stopped.returncode == 0, stopped.stderr
    assert state.events.index("retire") < state.events.index("/Delete")
    assert runs.read(cfg, "demo") is None and win.last_result(cfg, "demo") is None


def test_finished_producer_is_let_exit_before_it_is_ended(startup, monkeypatch):
    """The receipt is the producer's last write to the record, not its last act.

    `boot` publishes the receipt from its `finally`; the caller then logs the
    boot's failure audit, which the audit log's writer flushes as the interpreter
    exits. A `pod up` that reads the receipt and stops the pod at once would end
    the producer before that event lands. So `stop` first waits on the pinned
    object for the whole stop budget and never terminates a producer that exits
    within it; only one that outlives the budget is ended, and the wait comes
    first. Kernel waits here are modelled, since a real producer's exit timing is
    what this test removes from the question.
    """
    cfg, state = startup
    state.task_exists = True
    win.record_result(cfg, "demo", 3)
    _unspawned(cfg, state="finished")
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: 8100)
    state.exited = True
    stopped = win.stop(cfg, "demo", timeout=7.5)
    assert stopped.returncode == 0, stopped.stderr
    assert "retire" not in state.events
    assert state.waits == [7.5]  # The producer gets the full stop budget, not a poll.
    assert state.events.index("wait") < state.events.index("/End")
    assert runs.read(cfg, "demo")["state"] == "retired"
    # A producer that outlives the budget is terminated, but only after the wait.
    runs.publish(cfg, "demo", {**runs.read(cfg, "demo"), "state": "finished", "exit": 3})
    state.events.clear()
    state.waits.clear()
    state.exited = False
    assert win.stop(cfg, "demo", timeout=7.5).returncode == 0
    assert state.waits == [7.5]
    assert state.events.index("wait") < state.events.index("retire") < state.events.index("/End")


@pytest.mark.parametrize(
    "evidence", ["pid", "handoff", "home", "unreadable", "query", "job_key", "no_exit"]
)
def test_unspawned_retirement_fails_closed_on_unprovable_evidence(startup, monkeypatch, evidence):
    """What a gone producer cannot vouch for stays where it is.

    A supervised pid record or handoff marker means a gateway this record never
    published. A HOME under a producer that died WITHOUT its receipt may hold an
    unreaped child's writes. An evidence path that cannot be inspected counts as
    present: only a clean "not found" is absence. An identity query error is
    unknown, not death. A receipt carrying gateway keys, or none of its exit
    code, is malformed.
    """
    cfg, state = startup
    state.task_exists = True
    record = _unspawned(cfg)
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    if evidence == "pid":
        win.record_supervised_pid(cfg, "demo", 4242)
    elif evidence == "handoff":
        win.handoff_marker_path(cfg, "demo").write_text("777\n100\n", encoding="utf-8")
    elif evidence == "home":
        cfg.home_dir("demo").mkdir(parents=True)
    elif evidence == "unreadable":
        unreadable = win.pid_record_path(cfg, "demo")
        real_lstat = Path.lstat

        def lstat(self, *args, **kwargs):
            if self == unreadable:
                raise PermissionError(13, "denied", str(self))
            return real_lstat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", lstat)
    elif evidence == "query":
        monkeypatch.setattr(
            win.jobs, "open_identity", lambda *_a: (_ for _ in ()).throw(OSError(5, "denied"))
        )
    elif evidence == "job_key":
        runs.publish(cfg, "demo", {**record, "job": "Global\\KiroCrew.Pod." + "a" * 32})
    elif evidence == "no_exit":
        runs.publish(cfg, "demo", {**record, "state": "finished"})
    before = runs.path(cfg, "demo").read_bytes()
    result = rt.stop_pod(cfg, "demo")
    assert result.returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == before
    assert "/Delete" not in state.events and state.task_exists
    assert win.start(cfg, "demo").returncode == 1


def test_finished_producer_with_a_home_is_reclaimed_under_its_receipt(startup, monkeypatch):
    """A refused seed leaves a HOME; the producer's receipt says it spawned no gateway.

    That receipt is written only on the orderly refusal path, and `finish`
    consumes it only once the HOME is gone. A descendant of a process the boot
    started before any lifetime Job existed is outside what it vouches for, the
    same residual the ready path's Job proof accepts.
    """
    cfg, state = startup
    state.task_exists = True
    cfg.home_dir("demo").mkdir(parents=True)
    _unspawned(cfg, state="finished")
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    stopped = rt.stop_pod(cfg, "demo")
    assert stopped.returncode == 0, stopped.stderr
    assert not cfg.home_dir("demo").exists() and runs.read(cfg, "demo") is None


def test_retired_receipt_survives_cleanup_failure_and_retries(startup, monkeypatch):
    cfg, state = startup
    state.task_exists = True
    cfg.home_dir("demo").mkdir(parents=True)
    _unspawned(cfg, state="finished")
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    real_cleanup = rt.cleanup_home
    monkeypatch.setattr(rt, "cleanup_home", lambda *_a: 1)
    first = rt.stop_pod(cfg, "demo")
    assert first.returncode == 1
    assert runs.read(cfg, "demo")["state"] == "retired"
    assert win.start(cfg, "demo").returncode == 1  # A receipt is still prior state.
    monkeypatch.setattr(rt, "cleanup_home", real_cleanup)
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: pytest.fail("already proven"))
    cfg.home_dir("demo").rmdir()
    second = rt.stop_pod(cfg, "demo")
    assert second.returncode == 0, second.stderr
    assert runs.read(cfg, "demo") is None


def test_unspawned_retirement_never_touches_a_newer_generation(startup, monkeypatch):
    """A record that changes under `stop` belongs to someone else; nothing is deleted."""
    cfg, state = startup
    state.task_exists = True
    _unspawned(cfg)

    def replace_then_gone(*_a):
        runs.path(cfg, "demo").unlink()
        runs.reserve(cfg, "demo")
        return None

    monkeypatch.setattr(win.jobs, "open_identity", replace_then_gone)
    result = win.stop(cfg, "demo", timeout=0)
    assert result.returncode == 1 and "changed" in result.stderr
    assert runs.read(cfg, "demo")["state"] == "reserved"
    assert not {"/End", "/Delete"}.intersection(state.events) and state.task_exists


def test_boot_without_a_reservation_refuses_and_writes_no_result(model, monkeypatch, capsys):
    """`pod _run` outside a scheduled generation owns nothing to boot or record."""
    cfg, _state = model
    assert rt.boot(cfg, "demo") == 78
    assert "unclaimed run reservation" in capsys.readouterr().out
    assert runs.read(cfg, "demo") is None
    assert win.last_result(cfg, "demo") is None


def test_boot_withholds_the_receipt_when_the_drain_is_unproven(model, monkeypatch, capsys):
    """A supervisor that cannot prove its Job empty leaves `preparing` in place.

    The result is still recorded, so `pod up` stops waiting, but no `finished`
    receipt is published: `stop` must keep refusing while gateway evidence remains.
    """
    cfg, _state = model
    runs.reserve(cfg, "demo")
    monkeypatch.setattr(
        rt, "_boot_unguarded", lambda *_a: (_ for _ in ()).throw(win.DrainUnproven("job busy"))
    )
    monkeypatch.setattr(rt, "_record_refusal", lambda *_a: None)
    assert rt.boot(cfg, "demo") == 78
    assert runs.read(cfg, "demo")["state"] == "preparing"
    assert win.last_result(cfg, "demo") == 78
    monkeypatch.setattr(
        rt, "_boot_unguarded", lambda *_a: (_ for _ in ()).throw(OSError("no built dist"))
    )
    runs.path(cfg, "demo").unlink()
    runs.reserve(cfg, "demo")
    assert rt.boot(cfg, "demo") == 78
    assert runs.read(cfg, "demo")["state"] == "finished"


def test_every_exit_of_a_claimed_boot_records_a_result(model, monkeypatch, capsys):
    """`pod up` waits on the recorded result, so a claimed boot never leaves without one.

    An exception that escapes the boot guards ends the process with Python's own
    exit status 1; the result carries that failure and no receipt is published,
    because the producer cannot vouch for how it ended. A `pod _run` whose claim is
    refused owns no generation and writes nothing at all: the record may belong to
    another producer still booting, and a shared result or refusal note written
    here would make `pod up` abandon that legitimate boot.
    """
    cfg, _state = model
    runs.reserve(cfg, "demo")
    monkeypatch.setattr(
        rt, "_boot_unguarded", lambda *_a: (_ for _ in ()).throw(ValueError("unexpected"))
    )
    with pytest.raises(ValueError):
        rt.boot(cfg, "demo")
    assert win.last_result(cfg, "demo") == 1
    owned = runs.read(cfg, "demo")
    assert owned["state"] == "preparing"
    # A second claimant while another producer owns the generation: refused, silent.
    win.result_path(cfg, "demo").unlink()
    monkeypatch.setattr(
        rt, "_boot_unguarded", lambda *_a: pytest.fail("a rejected claimant must not boot")
    )
    monkeypatch.setattr(rt, "_record_refusal", lambda *_a: pytest.fail("no refusal note"))
    assert rt.boot(cfg, "demo") == 78
    assert "unclaimed run reservation" in capsys.readouterr().out
    assert win.last_result(cfg, "demo") is None
    assert runs.read(cfg, "demo") == owned
    assert not cfg.refusal_file("demo").exists()


def test_boot_that_publishes_its_gateway_while_waited_for_is_not_retired(startup, monkeypatch):
    """`preparing` that turns `ready` during the wait is a gateway now, not a refusal.

    The producer is pinned live, then publishes `ready` and exits during the
    first wait. With no supervised pid record or handoff marker on disk, only the
    state re-check stands between `stop` and a `retired` receipt over a published
    gateway; it refuses, leaves the `ready` record exactly as published, and
    never reaches the task.
    """
    cfg, state = startup
    state.task_exists = True
    record = _unspawned(cfg)
    monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: 8100)
    monkeypatch.setattr(win.jobs, "retire_identity", lambda *_a, **_kw: pytest.fail("killed"))
    published: dict = {}

    def wait(*_a, **_kw):
        if not published:
            published.update(
                runs.ready(cfg, "demo", record, "Global\\KiroCrew.Pod." + "a" * 32, (4242, "100"))
            )
            return False
        return True

    monkeypatch.setattr(win.jobs, "wait_identity", wait)
    stopped = win.stop(cfg, "demo", timeout=5)
    assert stopped.returncode == 1
    assert "published its gateway (ready)" in stopped.stderr
    assert runs.read(cfg, "demo") == published
    assert not {"/End", "/Delete"}.intersection(state.events)


def test_retired_receipt_refuses_a_record_that_moved_on(startup):
    """`retired` is a compare-and-swap: a stale `preparing` view never overwrites `ready`."""
    cfg, _state = startup
    record = _unspawned(cfg)
    ready = runs.ready(cfg, "demo", record, "Global\\KiroCrew.Pod." + "a" * 32, (4242, "100"))
    with pytest.raises(OSError, match="changed while retiring"):
        runs.retired(cfg, "demo", record)
    assert runs.read(cfg, "demo") == ready


@pytest.mark.parametrize("windows", [True, False])
def test_retire_hint_names_the_manual_procedure_only_on_windows(monkeypatch, windows):
    """`pod down` can itself refuse on Windows, so only there does the hint go on."""
    monkeypatch.setattr(rt, "IS_WINDOWS", windows)
    plain = pod_cli._retire_hint("demo")
    deliberate = pod_cli._retire_hint("demo", deliberately=True)
    assert plain.startswith("Retire it with `kirocrew pod down demo`.")
    assert deliberate.startswith("Retire it deliberately with `kirocrew pod down demo`.")
    for hint in (plain, deliberate):
        assert hint.count(win.MANUAL_RETIREMENT_POINTER) == (1 if windows else 0)


def test_windows_down_refusal_names_the_manual_procedure_once(startup, monkeypatch, capsys):
    """The backend's refusal says what is unproven; the CLI alone adds the way out."""
    cfg, _state = startup
    refusal = "the boot is still in progress; retry"
    monkeypatch.setattr(rt, "pod_name_mutex", lambda *_a: contextlib.nullcontext())
    monkeypatch.setattr(rt, "is_active", lambda *_a: False)
    monkeypatch.setattr(
        rt,
        "stop_pod",
        lambda *_a: subprocess.CompletedProcess(["stop"], 1, stdout="", stderr=refusal),
    )
    monkeypatch.setattr(pod_cli, "_audit", lambda *_a, **_kw: None)
    with pytest.raises(SystemExit) as exited:
        pod_cli._down(cfg, argparse.Namespace(name="demo"))
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert refusal in err
    assert err.count(win.MANUAL_RETIREMENT_POINTER) == 1


@pytest.fixture
def clock(startup, monkeypatch):
    """A modelled kernel uptime; a value of None makes the counter unreadable."""
    cfg, state = startup
    state.uptime = 5_000_000

    def uptime_ms():
        if state.uptime is None:
            raise OSError("tick counter unavailable")
        return state.uptime

    monkeypatch.setattr(runs.jobs, "uptime_ms", uptime_ms)
    return cfg, state


def _no_identity_probe(monkeypatch):
    """After a restart the recorded pids may name unrelated processes: never open one."""
    for attr in ("open_identity", "retire_identity", "wait_identity"):
        monkeypatch.setattr(
            win.jobs, attr, lambda *_a, _n=attr, **_kw: pytest.fail(f"{_n} after a restart")
        )


def _seed_run(cfg, state, shape):
    """A run left behind in *shape*, with gateway evidence where that shape has it."""
    win.task_script_path(cfg, "demo").write_text("rem wrapper", encoding="utf-8")
    state.task_exists = True
    if shape in {"reserved", "cancelled"}:
        record = runs.reserve(cfg, "demo")
        if shape == "cancelled":
            runs.publish(cfg, "demo", {**record, "state": "cancelled"})
        win.record_result(cfg, "demo", 3)
        return
    if shape in {"ready", "drained"}:
        contained(cfg)
        win.record_supervised_pid(cfg, "demo", 4242)
        win.handoff_marker_path(cfg, "demo").write_text("777\n100\n", encoding="utf-8")
        if shape == "drained":
            runs.publish(cfg, "demo", {**runs.read(cfg, "demo"), "state": "drained"})
    else:
        _unspawned(cfg, state="finished" if shape == "finished" else "preparing")
    cfg.home_dir("demo").mkdir(parents=True)
    (cfg.home_dir("demo") / "state").write_text("owned", encoding="utf-8")


@pytest.mark.parametrize(
    "shape", ["reserved", "cancelled", "preparing", "finished", "ready", "drained"]
)
def test_run_from_an_earlier_boot_session_is_recovered_by_down(clock, monkeypatch, shape):
    """A restart ends every process a record can name, so `down` reclaims any shape.

    This covers what the same boot session must refuse: a reservation nobody
    claimed, a producer that died mid-provision with a HOME, and a `ready` run
    whose supervisor died with the Job's name. No recorded pid is opened, since
    after a restart it may belong to any process, and `up` then admits a fresh run.
    """
    cfg, state = clock
    _seed_run(cfg, state, shape)
    generation = runs.read(cfg, "demo")["generation"]
    state.uptime = 1_000  # Windows restarted: the counter began again from zero.
    _no_identity_probe(monkeypatch)
    monkeypatch.setattr(
        win.jobs.PodJob, "open_existing", lambda *_a: pytest.fail("job after a restart")
    )
    real_finish = runs.finish

    def finish(config, name):
        receipt = runs.read(config, name)
        assert receipt["generation"] == generation and receipt["rebooted"] == 1_000
        assert receipt["state"] == ("drained" if shape in {"ready", "drained"} else "retired")
        real_finish(config, name)

    monkeypatch.setattr(runs, "finish", finish)
    stopped = rt.stop_pod(cfg, "demo")
    assert stopped.returncode == 0, stopped.stderr
    assert runs.read(cfg, "demo") is None and not cfg.home_dir("demo").exists()
    assert not state.task_exists and not win.task_script_path(cfg, "demo").exists()
    assert not win.pid_record_path(cfg, "demo").exists()
    assert not win.handoff_marker_path(cfg, "demo").exists()
    assert win.last_result(cfg, "demo") is None
    assert win.start(cfg, "demo").returncode == 0
    fresh = runs.read(cfg, "demo")
    assert fresh["state"] == "reserved" and fresh["generation"] != generation
    assert fresh["uptime"] == 1_000


@pytest.mark.parametrize("shape", ["reserved", "preparing", "ready"])
@pytest.mark.parametrize("uptime", ["later", "equal", "unreadable", "legacy"])
def test_same_or_unknown_boot_session_keeps_refusing(clock, monkeypatch, shape, uptime):
    """Only a LOWER uptime proves a restart; sleep and hibernation keep counting.

    A later or equal reading is the same boot session, an unreadable counter is
    unknown, and a record written before this field existed carries no proof.
    Each keeps the refusal that shape already has.
    """
    cfg, state = clock
    _seed_run(cfg, state, shape)
    if uptime == "legacy":
        record = runs.read(cfg, "demo")
        runs.publish(cfg, "demo", {k: v for k, v in record.items() if k != "uptime"})
        state.uptime = 1_000
    else:
        state.uptime = {"later": 9_000_000, "equal": 5_000_000, "unreadable": None}[uptime]
    if shape == "ready":
        state.job_missing = True  # Case A: the supervisor died with the Job's name.
    else:
        monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    before = runs.path(cfg, "demo").read_bytes()
    monkeypatch.setattr(rt, "cleanup_home", lambda *_a: pytest.fail("no reclamation authority"))
    stopped = rt.stop_pod(cfg, "demo")
    assert stopped.returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == before
    assert state.task_exists and "/Delete" not in state.events
    assert cfg.home_dir("demo").exists() == (shape != "reserved")


@pytest.mark.parametrize(
    "corrupt",
    [
        {"uptime": "5000000"},
        {"uptime": -1},
        {"uptime": True},
        {"uptime": 2**64},
        {"rebooted": 1_000},
        {"state": "drained", "rebooted": 6_000_000},
        {"state": "drained", "rebooted": "1000"},
    ],
)
def test_malformed_boot_session_fields_refuse(clock, monkeypatch, corrupt):
    """A bad uptime or restart receipt is malformed evidence, never a restart proof."""
    cfg, state = clock
    _seed_run(cfg, state, "ready")
    runs.publish(cfg, "demo", {**runs.read(cfg, "demo"), **corrupt})
    state.uptime = 1_000
    _no_identity_probe(monkeypatch)
    with pytest.raises(OSError, match="malformed"):
        runs.read(cfg, "demo")
    assert rt.stop_pod(cfg, "demo").returncode == 1
    assert cfg.home_dir("demo").exists() and state.task_exists


def test_restart_receipt_for_a_claimed_run_must_name_its_publisher(clock):
    """Only a never-claimed run may carry a restart receipt without a publisher."""
    cfg, state = clock
    record = _unspawned(cfg)
    receipt = {k: v for k, v in record.items() if k != "publisher"}
    runs.publish(cfg, "demo", {**receipt, "state": "drained", "rebooted": 1_000})
    with pytest.raises(OSError, match="malformed"):
        runs.read(cfg, "demo")
    runs.publish(cfg, "demo", {**receipt, "state": "retired", "rebooted": 1_000, "job": "x"})
    with pytest.raises(OSError, match="malformed"):
        runs.read(cfg, "demo")


def test_claim_restamps_the_reservation_uptime(clock):
    """The claim starts this boot session's writers, so its uptime replaces the reservation's.

    A reservation left by an earlier boot session and then claimed in this one
    must not keep the older, higher uptime: that would read as a restart while
    the producer that just claimed it is alive. If the counter cannot be read at
    the claim, the record carries no uptime at all rather than the stale one.
    """
    cfg, state = clock
    runs.reserve(cfg, "demo")
    state.uptime = 1_000
    claimed = runs.claim(cfg, "demo")
    assert claimed["uptime"] == 1_000 and runs.read(cfg, "demo") == claimed
    assert not runs.earlier_boot(claimed)
    runs.path(cfg, "demo").unlink()
    state.uptime = 5_000_000
    runs.reserve(cfg, "demo")
    state.uptime = None
    claimed = runs.claim(cfg, "demo")
    assert "uptime" not in claimed
    state.uptime = 1_000
    assert not runs.earlier_boot(claimed)


def test_restart_receipt_is_a_compare_and_swap(clock):
    """A record that moved on, or one not proven older than this boot, is not retired."""
    cfg, state = clock
    record = _unspawned(cfg)
    state.uptime = 1_000
    moved = {**record, "state": "finished", "exit": 3}
    runs.publish(cfg, "demo", moved)
    with pytest.raises(OSError, match="changed while retiring"):
        runs.rebooted(cfg, "demo", record)
    assert runs.read(cfg, "demo") == moved
    state.uptime = 6_000_000
    with pytest.raises(OSError, match="not proven"):
        runs.rebooted(cfg, "demo", moved)
    state.uptime = None
    with pytest.raises(OSError, match="not proven"):
        runs.rebooted(cfg, "demo", moved)
    assert runs.read(cfg, "demo") == moved


def test_uptime_negative_control_detects_an_inverted_comparison(clock, monkeypatch):
    """Treating a HIGHER uptime as a restart must reclaim a live run's HOME."""
    cfg, state = clock
    _seed_run(cfg, state, "preparing")
    state.uptime = 9_000_000  # Same boot session: the counter only moved forward.

    def inverted(config, name, record):
        result = {**record, "state": "retired", "rebooted": 1_000}
        runs.publish(config, name, result)
        return result

    monkeypatch.setattr(runs, "earlier_boot", lambda record: record["uptime"] < state.uptime)
    monkeypatch.setattr(runs, "rebooted", inverted)
    assert rt.stop_pod(cfg, "demo").returncode == 0
    assert not cfg.home_dir("demo").exists()


@pytest.mark.parametrize(
    "publisher", ["alive", "gone", "exited", "query", "preparing", "missing", "malformed"]
)
def test_only_a_live_supervisor_makes_a_serving_gateway_adoptable(model, monkeypatch, publisher):
    cfg, state = model
    contained(cfg)
    if publisher == "gone":
        monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    elif publisher == "exited":
        state.exited = True
    elif publisher == "query":
        monkeypatch.setattr(
            win.jobs, "open_identity", lambda *_a: (_ for _ in ()).throw(OSError(5, "denied"))
        )
    elif publisher == "preparing":
        _unspawned_record = runs.read(cfg, "demo")
        runs.publish(
            cfg,
            "demo",
            {
                k: v
                for k, v in {**_unspawned_record, "state": "preparing"}.items()
                if k not in {"root", "job"}
            },
        )
    elif publisher == "missing":
        runs.path(cfg, "demo").unlink()
    elif publisher == "malformed":
        runs.path(cfg, "demo").write_text("{}", encoding="utf-8")
    reason = win.unsupervised_reason(cfg, "demo")
    if publisher == "alive":
        assert reason is None
        assert state.waits == [0]
    else:
        assert reason
    if publisher in {"alive", "exited"}:
        assert state.events.count("close_identity") == 1


@pytest.mark.parametrize("supervised", [False, True])
def test_up_refuses_to_adopt_a_gateway_whose_supervisor_is_gone(
    model, monkeypatch, capsys, supervised
):
    """Case A: the supervisor died, the gateway still serves. `up` issues no token."""
    cfg, state = model
    contained(cfg)
    win.record_supervised_pid(cfg, "demo", 4242)
    monkeypatch.setattr(win, "pid_exists", lambda pid: True)
    if not supervised:
        monkeypatch.setattr(win.jobs, "open_identity", lambda *_a: None)
    minted = []
    monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda *_a: Path("checkout"))
    monkeypatch.setattr(pod_cli, "_audit", lambda *_a, **_kw: None)
    monkeypatch.setattr(pod_cli.prov, "has_venv", lambda *_a: True)
    monkeypatch.setattr(pod_cli.prov, "has_dist", lambda *_a: True)
    monkeypatch.setattr(rt, "pin_checkout", lambda *_a: None)
    monkeypatch.setattr(rt, "derive_port", lambda *_a: 8611)
    monkeypatch.setattr(pod_cli, "_wait_healthy", lambda *_a, **_kw: 200)
    monkeypatch.setattr(rt, "mint_token", lambda *_a: minted.append(1) or "test-token")
    args = argparse.Namespace(
        pod_action="up", name="demo", seed=None, ttl=1, json=True, provision=False
    )
    if supervised:
        with contextlib.suppress(SystemExit):
            pod_cli.dispatch(args)
        assert minted, capsys.readouterr().err
        return
    with pytest.raises(SystemExit) as exited:
        pod_cli.dispatch(args)
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "supervisor (pid 777) is gone" in err and "no token was issued" in err
    assert err.count(win.MANUAL_RETIREMENT_POINTER) == 1
    assert minted == [] and "/Run" not in state.events
    assert runs.read(cfg, "demo")["state"] == "ready"
