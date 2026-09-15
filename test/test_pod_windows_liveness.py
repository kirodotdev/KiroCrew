"""Windows supervision: immutable containment, restart visibility and safe refusal."""

from __future__ import annotations

import subprocess
import threading

import pytest
from test_pod_windows_run import contained
from test_pod_windows_run import model as model

from kiro_crew import platform_compat as pc
from kiro_crew.pod import _windows_run as runs
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import windows as win
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE

__all__ = ["model", "supervisor"]


def test_the_mutex_locks_through_the_shared_cross_platform_helper(model, monkeypatch):
    cfg, _state = model
    seen = {}
    real_open, real_lock = rt.open_lock_file, rt.file_lock

    def opening(path):
        seen["path"] = str(path)
        return real_open(path)

    def locking(fd, **kwargs):
        seen.update(fd=fd, exclusive=kwargs.get("exclusive"))
        return real_lock(fd, **kwargs)

    monkeypatch.setattr(rt, "open_lock_file", opening)
    monkeypatch.setattr(rt, "file_lock", locking)
    with rt.pod_name_mutex(cfg, "demo"):
        pass
    assert seen["path"] == str(cfg.pods_dir / f"{cfg.unit_prefix}@demo.lock")
    assert isinstance(seen["fd"], int) and seen["exclusive"] is True


def test_two_contenders_on_one_name_serialize(model):
    cfg, _state = model
    entered = threading.Event()
    release = threading.Event()
    second = threading.Event()
    events = []

    def first():
        with rt.pod_name_mutex(cfg, "demo"):
            events.append("first")
            entered.set()
            assert release.wait(5)
            events.append("first_exit")

    def other():
        assert entered.wait(5)
        with rt.pod_name_mutex(cfg, "demo"):
            events.append("second")
            second.set()

    threads = [threading.Thread(target=first), threading.Thread(target=other)]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(5)
        assert not second.is_set()
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert events == ["first", "first_exit", "second"]


def test_the_mutex_stays_reentrant_within_one_thread(model):
    cfg, _state = model
    with rt.pod_name_mutex(cfg, "demo"):
        with rt.pod_name_mutex(cfg, "demo"):
            pass


def test_a_stuck_holder_refuses_rather_than_running_unserialized(model, monkeypatch):
    cfg, _state = model
    monkeypatch.setattr(pc, "IS_POSIX", False)
    monkeypatch.setattr(pc, "_win_acquire_blocking", lambda fd, timeout=None: False)
    with pytest.raises(OSError, match="refusing to proceed unserialized"):
        with rt.pod_name_mutex(cfg, "demo"):
            pytest.fail("must not enter without the lock")


@pytest.fixture
def supervisor(model, monkeypatch, tmp_path):
    cfg, state = model
    monkeypatch.setattr(win, "CREATE_SUSPENDED", 0x4)
    monkeypatch.setattr(win, "CREATE_NEW_PROCESS_GROUP", 0x200)
    runs.reserve(cfg, "demo")
    alive = {4242}
    tokens = {4242: "100", 4300: "200", 4400: "300", 4500: "400"}
    state.alive, state.tokens, state.recorded = alive, tokens, []
    state.sidecar, state.stale_reads = None, 0
    state.argv, state.env, state.exit_code = ["gateway"], {}, 0
    state.binary = tmp_path / "kirocrew"

    class Proc:
        pid = 4242
        _handle = 8001

        def poll(self):
            return None if self.pid in alive else 0

        def kill(self):
            state.events.append("kill_original")
            alive.discard(self.pid)

        def wait(self, timeout=None):
            alive.discard(self.pid)
            return state.exit_code

    def spawn(*args, **kwargs):
        state.spawn_args, state.spawn_kwargs = args, kwargs
        state.events.append("spawn")
        assert kwargs["creationflags"] & win.CREATE_SUSPENDED
        assert runs.read(cfg, "demo")["state"] == "preparing"
        return Proc()

    monkeypatch.setattr(win.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        win.jobs.pc, "_windows_process_handle_identity", lambda _h: (4242, 100, None)
    )
    monkeypatch.setattr(win, "process_start_time", tokens.get)
    monkeypatch.setattr(win, "pid_exists", lambda pid: pid in alive)
    monkeypatch.setattr(win, "apply_windows_resource_ceiling", lambda _pid: False)
    monkeypatch.setattr(win.jobs, "open_identity", lambda pid, token: pid + 10000)
    monkeypatch.setattr(win, "resume_process_main_thread", lambda _pid: True)
    real_record = win.record_supervised_pid

    def record(config, name, pid):
        state.recorded.append(pid)
        real_record(config, name, pid)

    monkeypatch.setattr(win, "record_supervised_pid", record)

    def sidecar(_path):
        if state.stale_reads:
            state.stale_reads -= 1
            return None
        return state.sidecar

    monkeypatch.setattr(win.run_marker, "read_pid_record_path", sidecar)
    monkeypatch.setattr(win.run_marker, "pid_start_token", tokens.get)
    monkeypatch.setattr(
        win, "_live_children_of", lambda pid, _token: [child for child in alive if child > pid]
    )

    def run():
        return win.supervise_gateway(
            cfg,
            "demo",
            state.binary,
            state.argv,
            state.env,
            gateway_pid_record=tmp_path / "gateway.pid",
        )

    return cfg, state, run


@pytest.mark.parametrize("count", [1, 2, 3])
def test_each_restart_is_adopted_with_advanced_anchor_and_unchanged_generation(
    supervisor, monkeypatch, count
):
    cfg, state, run = supervisor
    generation = runs.read(cfg, "demo")["generation"]
    sequence = [4300, 4400, 4500][:count]
    state.alive.add(sequence[0])
    state.sidecar = (sequence[0], state.tokens[sequence[0]])

    def wait(pid, token, *, on_poll=None):
        assert win._read_pid_record(cfg, "demo") == (pid, token)
        assert runs.read(cfg, "demo")["generation"] == generation
        on_poll()
        state.alive.discard(pid)
        index = sequence.index(pid) + 1
        if index < len(sequence):
            successor = sequence[index]
            state.alive.add(successor)
            state.sidecar = (successor, state.tokens[successor])
            state.stale_reads = 1

    monkeypatch.setattr(win, "_wait_for_pid", wait)
    assert run() == 0
    assert state.recorded == [4242, *sequence]
    assert runs.read(cfg, "demo")["state"] == "drained"
    assert state.events.count("close_identity") == count


@pytest.mark.parametrize(
    "failure",
    [
        "initial_record",
        "partial_record",
        "successor_record",
        "successor_identity",
        "marker",
        "foreign_successor",
    ],
)
def test_untrackable_boot_or_successor_never_serves_outside_retained_containment(
    supervisor, monkeypatch, failure
):
    cfg, state, run = supervisor
    state.alive.add(4300)
    state.sidecar = (4300, "200")
    if failure in {"initial_record", "partial_record", "successor_record"}:
        original = win.record_supervised_pid

        def write(config, name, pid):
            if pid == (4300 if failure == "successor_record" else 4242):
                if failure == "partial_record":
                    win.pid_record_path(config, name).write_text("4242\n", encoding="utf-8")
                raise OSError("pid record unavailable")
            original(config, name, pid)

        monkeypatch.setattr(win, "record_supervised_pid", write)
    elif failure == "successor_identity":
        state.tokens.pop(4300)
        monkeypatch.setattr(win, "_await_successor", lambda *_a: 4300)
    elif failure == "marker":
        monkeypatch.setattr(win, "_begin_handoff", lambda *_a: False)
    else:
        monkeypatch.setattr(win.jobs.PodJob, "contains", lambda *_a: False)
    result = run()
    assert result == (0 if failure == "marker" else EXIT_REFUSED_UNRECOVERABLE)
    assert state.active == 0
    assert "job_zero" in state.events
    assert not win.pid_record_path(cfg, "demo").exists()
    assert not win.handoff_marker_path(cfg, "demo").exists()
    expected = "preparing" if failure in {"initial_record", "partial_record"} else "drained"
    assert runs.read(cfg, "demo")["state"] == expected


def test_ordinary_child_is_not_adopted_and_adoption_timeout_still_drains_job(
    supervisor, monkeypatch
):
    cfg, state, run = supervisor
    state.alive.add(4300)
    monkeypatch.setattr(win, "SUCCESSOR_ADOPT_TIMEOUT_SECS", 0)
    assert run() == 0
    assert state.recorded == [4242]
    assert state.active == 0
    assert runs.read(cfg, "demo")["state"] == "drained"


def test_launcher_anchor_uses_dead_gateway_sidecar(supervisor, monkeypatch):
    _cfg, state, run = supervisor
    state.sidecar = (4300, "200")
    anchors = []
    monkeypatch.setattr(
        win, "_await_successor", lambda _path, pid, token: anchors.append((pid, token))
    )
    assert run() == 0
    assert anchors == [(4300, "200")]


@pytest.mark.parametrize("state_name", ["reserved", "preparing"])
def test_interrupted_boot_publication_preserves_evidence_and_rejects_retry(
    model, monkeypatch, state_name
):
    cfg, state = model
    runs.reserve(cfg, "demo")
    if state_name == "preparing":
        runs.claim(cfg, "demo")
    before = runs.path(cfg, "demo").read_bytes()
    monkeypatch.setattr(
        win.jobs, "open_identity", lambda *_a: None
    )  # Publisher gone is not containment.
    assert win.stop(cfg, "demo", timeout=0).returncode == 1
    assert win.start(cfg, "demo").returncode == 1
    assert runs.path(cfg, "demo").read_bytes() == before
    assert not {"create_job", "retire", "/End", "/Delete"}.intersection(state.events)


def test_claim_and_publication_failures_never_spawn_an_unreserved_gateway(
    model, monkeypatch, tmp_path
):
    cfg, _state = model
    runs.reserve(cfg, "demo")
    monkeypatch.setattr(
        runs, "publish", lambda *_a: (_ for _ in ()).throw(OSError("publish failed"))
    )
    monkeypatch.setattr(
        win.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("no claimed publisher")
    )
    with pytest.raises(OSError, match="publish failed"):
        win.supervise_gateway(
            cfg, "demo", tmp_path / "gateway", [], {}, gateway_pid_record=tmp_path / "gw.pid"
        )
    assert runs.read(cfg, "demo")["state"] == "reserved"


def test_pid_record_probes_keep_identity_separate_from_liveness(model, monkeypatch):
    cfg, _state = model
    win.record_supervised_pid(cfg, "demo", 4242)
    monkeypatch.setattr(win, "pid_exists", lambda _pid: False)
    assert win.supervised_pid(cfg, "demo") is None
    monkeypatch.setattr(win, "pid_exists", lambda _pid: True)
    assert win.supervised_pid(cfg, "demo") == 4242
    monkeypatch.setattr(win, "process_start_time", lambda _pid: "200")
    assert win.supervised_pid(cfg, "demo") is None


@pytest.mark.parametrize("failure", ["wrapper", "delete"])
def test_unload_failure_preserves_home_and_durable_receipt(model, monkeypatch, failure):
    cfg, state = model
    contained(cfg)
    wrapper = win.write_task_script(cfg, "demo")
    if failure == "wrapper":
        real_unlink = type(wrapper).unlink

        def unlink(path, *args, **kwargs):
            if path == wrapper:
                raise OSError("wrapper refused")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(type(wrapper), "unlink", unlink)
    else:
        monkeypatch.setattr(
            win,
            "schtasks",
            lambda *args: subprocess.CompletedProcess([], int(args[0] == "/Delete"), "", ""),
        )
    monkeypatch.setattr(rt, "cleanup_home", lambda *_a: pytest.fail("failed unload must keep HOME"))
    assert rt.stop_pod(cfg, "demo").returncode == 1
    assert wrapper.exists()
    assert runs.read(cfg, "demo")["state"] == "drained"
