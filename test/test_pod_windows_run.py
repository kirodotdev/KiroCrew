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
def test_scheduled_preclaim_refusal_has_no_producer_retirement_proof(
    startup, monkeypatch, tmp_path, failure
):
    """A provisioning refusal leaves an unresolved run, not a drain receipt."""
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
    snapshots = []
    boot_results = []

    def before_refusal_write(config, name, reason):
        # The scheduled producer is still on its call stack and has a write
        # ahead of it. Even without a HOME, reserved does not mean retired.
        record = runs.read(config, name)
        assert record["state"] == "reserved" and "publisher" not in record
        snapshots.append(runs.path(config, name).read_bytes())
        stopped = win.stop(config, name, timeout=0)
        assert stopped.returncode == 1
        assert runs.path(config, name).read_bytes() == snapshots[-1]
        assert state.task_exists and not config.home_dir(name).exists()
        record_refusal(config, name, reason)

    def run_wrapper(*args):
        response = scheduler(*args)
        if args[0] == "/Run":
            code = rt.boot(cfg, "demo")
            boot_results.append(code)
            # cmd.exe writes this AFTER Python's boot body returns; it carries
            # neither a generation nor the wrapper's creation identity.
            win.result_path(cfg, "demo").write_text(f"{code}\n", encoding="utf-8")
        return response

    monkeypatch.setattr(rt, "_record_refusal", before_refusal_write)
    monkeypatch.setattr(win, "schtasks", run_wrapper)
    assert win.start(cfg, "demo").returncode == 0  # Scheduler accepted /Run.
    assert boot_results == [3]
    assert snapshots and runs.path(cfg, "demo").read_bytes() == snapshots[0]
    assert rt.refusal_reason(cfg, "demo")
    assert win.last_result(cfg, "demo") == 3
    assert win.unit_state(cfg, "demo") == ("failed", 1)
    assert win.stop(cfg, "demo", timeout=0).returncode == 1
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == snapshots[0]
    assert state.task_exists and win.task_script_path(cfg, "demo").exists()
    assert not cfg.home_dir("demo").exists()
    assert not {"/End", "/Delete", "create_job"}.intersection(state.events)
