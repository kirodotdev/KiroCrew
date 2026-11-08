"""Pausing RUNNING agent shell groups under severe memory pressure.

The spawn gate's pause only refuses new starts. In the OOM this guards against,
~40 pytest-xdist workers kept growing after the gate paused, on a 184 GiB host with
no swap, and the kernel OOM killer fired eight times. These tests pin the policy's
lines, which processes count as an agent shell group, the pause/resume order, and
that a paused group is resumed on every way out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from kiro_crew.adaptive import shell_pause as sp
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.adaptive.policy import (
    SHELL_NONE,
    SHELL_PAUSE,
    SHELL_PAUSE_ALL,
    SHELL_RESUME,
    AdaptivePolicy,
    PolicyParams,
)
from kiro_crew.adaptive.signals import Sample
from kiro_crew.testing.wait import default_timeout, wait_until

pytestmark = pytest.mark.timeout(60)

GIB = 1024.0
HOST = 184 * GIB  # the OOM host, in MiB


def _policy() -> AdaptivePolicy:
    return AdaptivePolicy(PolicyParams(exec_ceiling=4))


def _shell(free_gib: float, total_mib: float = HOST, paused: int = 0) -> str:
    sample = Sample(t=1.0, free_mem_mb=free_gib * GIB, total_mem_mb=total_mib, shell_paused=paused)
    return _policy().observe(sample).shell_action


# -- the policy's lines --------------------------------------------------------


def test_the_oom_host_pauses_at_a_fraction_of_ram_not_at_2_gib() -> None:
    """10 GiB free on 184 GiB is where the incident was still growing toward OOM.

    The absolute critical line (2 GiB) would not fire until 1.1% of RAM was left.
    """
    critical, pause = _policy().shell_lines(HOST)
    assert critical == 2048.0
    assert pause == pytest.approx(0.08 * HOST)
    assert _shell(10) == SHELL_PAUSE


def test_at_the_absolute_critical_line_everything_pauses_at_once() -> None:
    assert _shell(1.5) == SHELL_PAUSE_ALL


def test_a_small_host_keeps_the_absolute_floor() -> None:
    """8% of 8 GiB is 0.64 GiB; the 2 GiB critical line stays the pause line."""
    assert _policy().shell_lines(8 * GIB) == (2048.0, 2048.0)


@pytest.mark.parametrize(
    ("free_gib", "paused", "expected"),
    [
        (20, 0, SHELL_NONE),  # above the pause line, nothing paused
        (20, 3, SHELL_RESUME),  # above the pause line: resume one
        (14, 3, SHELL_PAUSE),  # under it: pause, never resume by the line
    ],
)
def test_resume_only_above_the_pause_line(free_gib: float, paused: int, expected: str) -> None:
    assert _shell(free_gib, paused=paused) == expected


def test_a_4_gib_pod_can_still_resume() -> None:
    """Free memory inside a 4 GiB cgroup never exceeds 4 GiB, so the line must fit."""
    pod = 4 * GIB
    critical, pause = _policy().shell_lines(pod)
    assert pause == pytest.approx(0.25 * pod)
    assert critical == pytest.approx(0.25 * pod)
    assert _shell(1.5, total_mib=pod, paused=1) == SHELL_RESUME
    assert _shell(0.9, total_mib=pod) == SHELL_PAUSE_ALL


def test_the_probe_scales_by_the_cgroup_ceiling_free_memory_is_read_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import platform_compat
    from kiro_crew import subagent as subagent_mod
    from kiro_crew.adaptive import controller as ctl_mod

    monkeypatch.setattr(platform_compat, "host_total_mib", lambda: int(HOST))
    monkeypatch.setattr(subagent_mod, "cgroup_memory_ceiling_gb", lambda: 4.0)
    assert ctl_mod.probe_host().total_mem_mb == 4 * GIB
    monkeypatch.setattr(subagent_mod, "cgroup_memory_ceiling_gb", lambda: -1.0)
    assert ctl_mod.probe_host().total_mem_mb == HOST


def test_the_policy_keeps_no_shell_state_between_samples() -> None:
    """The turn bugs came from shell state split between policy and pauser. The
    same sample must give the same answer whatever came before it."""
    policy = _policy()
    for free in (40, 10, 1, -1, 15, 10, 30):
        policy.observe(Sample(t=1.0, free_mem_mb=free * GIB, total_mem_mb=HOST, shell_paused=3))
    fresh = _policy()
    for free in (10, 20, 1, -1):
        probe = Sample(t=1.0, free_mem_mb=free * GIB, total_mem_mb=HOST, shell_paused=3)
        assert policy.observe(probe).shell_action == fresh.observe(probe).shell_action


def test_unknown_memory_never_resumes_however_long_it_lasts() -> None:
    """GPT finding 3: an unknown-memory sample must never let anything resume."""
    policy = _policy()
    blind = Sample(t=1.0, free_mem_mb=-1.0, total_mem_mb=HOST, shell_paused=4)
    assert {policy.observe(blind).shell_action for _ in range(50)} == {SHELL_NONE}
    low = Sample(t=1.0, free_mem_mb=10 * GIB, total_mem_mb=HOST, shell_paused=4)
    assert policy.observe(low).shell_action == SHELL_PAUSE


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_group_paused_too_long_resumes_and_the_rest_follow_in_order(
    proc_tree: Path, tmp_path: Path
) -> None:
    """GPT finding 1, in the new design: one resume can never skip or shorten
    another. Starved groups resume oldest-paused first, one per call, and one
    that is paused again goes to the back."""
    clock = _Clock()
    pauser, _sigs, _state = _pauser(proc_tree, tmp_path, clock=clock)
    pauser.pause([(200, "c")], everything=False)  # 310
    clock.now += 1
    pauser.pause([(200, "c")], everything=False)  # 300
    assert pauser.resume_one(older_than=600) is None  # nothing has waited long enough
    clock.now += 600
    first = pauser.resume_one(older_than=600)
    assert first is not None and first.pgid == 310
    pauser.pause([(200, "c")], everything=False)  # 310 is paused again, at the back
    clock.now += 1
    second = pauser.resume_one(older_than=600)
    assert second is not None and second.pgid == 300
    assert pauser.resume_one(older_than=600) is None  # 310 restarted its wait


def test_line_recovery_resumes_the_group_paused_longest_first(
    proc_tree: Path, tmp_path: Path
) -> None:
    pauser, _sigs, _state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)  # 310, then 300
    first, second = pauser.resume_one(), pauser.resume_one()
    assert first is not None and second is not None
    assert [first.pgid, second.pgid] == [310, 300]
    assert pauser.resume_one() is None


def test_a_reused_pgid_is_never_signalled(proc_tree: Path, tmp_path: Path) -> None:
    """GPT finding 2: a pgid whose leader is now a different process (another start
    tick) gets no SIGCONT, from resume or from crash recovery."""
    pauser, _sigs, state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    # 310 exits and its pid is reused by an unrelated, deliberately stopped job.
    fields = ["T", "1", "310", "310"] + ["0"] * 15 + ["9999"] + ["0"] * 5
    (proc_tree / "310" / "stat").write_text("310 (other job) " + " ".join(fields))
    fresh, fresh_sigs, _ = _pauser(proc_tree, tmp_path)
    fresh.recover()
    assert (310, sp._SIGCONT) not in fresh_sigs.calls
    assert (300, sp._SIGCONT) in fresh_sigs.calls
    assert not state.exists()


def test_resume_all_and_reopen_leave_nothing_exempt(proc_tree: Path, tmp_path: Path) -> None:
    """GPT finding 2: stale turn state after resume_all/reopen exempted a group."""
    pauser, _sigs, _state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    pauser.resume_one()
    pauser.resume_all("stop", close=True)
    pauser.reopen()
    assert sorted(g.pgid for g in pauser.pause([(200, "c")], everything=True)) == [300, 310]


def test_no_state_file_means_no_pause(proc_tree: Path) -> None:
    sigs: list[tuple[int, int]] = []
    pauser = sp.ShellPauser(
        None, proc_root=str(proc_tree), gateway_pid=100, killpg=lambda g, s: sigs.append((g, s))
    )
    assert pauser.pause([(200, "c")], everything=True) == []
    assert sigs == []


def test_unknown_memory_moves_nothing() -> None:
    sample = Sample(t=1.0, free_mem_mb=-1.0, total_mem_mb=HOST, shell_paused=2)
    assert _policy().observe(sample).shell_action == SHELL_NONE


# -- which processes are agent shell groups ------------------------------------


def _proc(root: Path, pid: int, *, pgrp: int, sid: int, start: int, children=()) -> None:
    d = root / str(pid)
    (d / "task" / str(pid)).mkdir(parents=True)
    fields = ["S", "1", str(pgrp), str(sid)] + ["0"] * 15 + [str(start)] + ["0"] * 5
    (d / "stat").write_text(f"{pid} (p {pid}) " + " ".join(fields))
    (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children))


@pytest.fixture
def proc_tree(tmp_path: Path) -> Path:
    """The real layout, read off a live host: gateway -> sandbox launcher (the runtime,
    its own session) -> aim -> kiro-cli -> KAS node (own GROUP, runtime's session) ->
    an MCP launcher in node's group, and two setsid'd shell commands."""
    root = tmp_path / "proc"
    _proc(root, 100, pgrp=100, sid=90, start=1, children=[200])  # gateway
    _proc(root, 200, pgrp=200, sid=200, start=2, children=[210])  # runtime
    _proc(root, 210, pgrp=200, sid=200, start=3, children=[220])  # aim
    _proc(root, 220, pgrp=200, sid=200, start=4, children=[230])  # kiro-cli
    _proc(root, 230, pgrp=230, sid=200, start=5, children=[240, 300, 310])  # KAS node
    _proc(root, 240, pgrp=230, sid=200, start=6)  # MCP launcher
    _proc(root, 300, pgrp=300, sid=300, start=1000, children=[301])  # older shell
    _proc(root, 301, pgrp=300, sid=300, start=1001)  # a pytest worker inside it
    _proc(root, 310, pgrp=310, sid=310, start=2000)  # newer shell
    return root


def test_only_setsid_shell_commands_are_found_newest_first(proc_tree: Path) -> None:
    groups = sp.discover_shell_groups([(200, "chat-1")], proc_root=str(proc_tree), gateway_pid=100)
    assert [(g.pgid, g.session) for g in groups] == [(310, "chat-1"), (300, "chat-1")]


def test_a_runtime_row_naming_the_gateway_is_skipped(proc_tree: Path) -> None:
    assert sp.discover_shell_groups([(100, "x")], proc_root=str(proc_tree), gateway_pid=100) == []


# -- pause, resume, and the state file -----------------------------------------


class _Signals:
    def __init__(self, state: Path, fail: Optional[set[int]] = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.listed_at_stop: list[bool] = []
        self._state = state
        self._fail = fail or set()

    def __call__(self, pgid: int, sig: int) -> None:
        if sig == sp._SIGSTOP:
            rows = json.loads(self._state.read_text()) if self._state.exists() else []
            self.listed_at_stop.append(any(r["pgid"] == pgid for r in rows))
            if pgid in self._fail:
                raise ProcessLookupError(pgid)
        self.calls.append((pgid, sig))


def _pauser(
    proc_tree: Path, tmp_path: Path, *, clock: Any = None, **kw: Any
) -> tuple[sp.ShellPauser, _Signals, Path]:
    state = tmp_path / "run" / "paused.json"
    sigs = _Signals(state, **kw)
    pauser = sp.ShellPauser(
        state,
        proc_root=str(proc_tree),
        gateway_pid=100,
        killpg=sigs,
        **({"clock": clock} if clock is not None else {}),
    )
    return pauser, sigs, state


def test_pause_takes_the_newest_half_and_records_before_signalling(
    proc_tree: Path, tmp_path: Path
) -> None:
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    runtimes = [(200, "chat-1")]
    assert [g.pgid for g in pauser.pause(runtimes, everything=False)] == [310]
    assert [g.pgid for g in pauser.pause(runtimes, everything=False)] == [300]
    assert sigs.listed_at_stop == [True, True], "recorded BEFORE the signal"
    assert [r["pgid"] for r in json.loads(state.read_text())] == [310, 300]
    assert pauser.resume_one().pgid == 310  # type: ignore[union-attr]
    assert pauser.resume_all("test") == 1
    assert sigs.calls == [
        (310, sp._SIGSTOP),
        (300, sp._SIGSTOP),
        (310, sp._SIGCONT),
        (300, sp._SIGCONT),
    ]
    assert not state.exists()


def test_a_group_that_cannot_be_stopped_is_not_recorded(proc_tree: Path, tmp_path: Path) -> None:
    pauser, _sigs, state = _pauser(proc_tree, tmp_path, fail={310})
    assert [g.pgid for g in pauser.pause([(200, "c")], everything=True)] == [300]
    assert [r["pgid"] for r in json.loads(state.read_text())] == [300]


def test_a_crashed_gateways_groups_are_resumed_on_the_next_start(
    proc_tree: Path, tmp_path: Path
) -> None:
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    # A new gateway process: fresh pauser, same state file.
    fresh, fresh_sigs, _ = _pauser(proc_tree, tmp_path)
    assert fresh.recover() == 2
    assert sorted(fresh_sigs.calls) == [(300, sp._SIGCONT), (310, sp._SIGCONT)]
    assert not state.exists()


def test_every_group_in_the_shell_session_is_stopped_and_continued(
    proc_tree: Path, tmp_path: Path
) -> None:
    """``timeout 900 pytest -n 16`` runs pytest in its OWN group inside the shell's
    session (timeout calls setpgid). Stopping only the leader's group would leave
    the workers growing."""
    (proc_tree / "301" / "task" / "301" / "children").write_text("302")
    _proc(proc_tree, 302, pgrp=302, sid=300, start=1002, children=[303])  # timeout
    _proc(proc_tree, 303, pgrp=302, sid=300, start=1003)  # pytest worker
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    assert (300, sp._SIGSTOP) in sigs.calls and (302, sp._SIGSTOP) in sigs.calls
    rows = {r["pgid"]: r["pgids"] for r in json.loads(state.read_text())}
    assert rows[300] == [[300, 1000.0], [302, 1002.0]]  # each with its start tick
    pauser.resume_all()
    assert (302, sp._SIGCONT) in sigs.calls


def test_a_group_created_between_the_scan_and_the_stop_is_stopped_too(
    proc_tree: Path, tmp_path: Path
) -> None:
    """``timeout`` calls setpgid right after it starts; the scan can miss it."""
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    real = sigs.__call__

    def _racing(pgid: int, sig: int) -> None:
        real(pgid, sig)
        if pgid == 300 and sig == sp._SIGSTOP and not (proc_tree / "304").exists():
            (proc_tree / "301" / "task" / "301" / "children").write_text("304")
            _proc(proc_tree, 304, pgrp=304, sid=300, start=1004)

    pauser._killpg = _racing
    pauser.pause([(200, "c")], everything=True)
    assert (304, sp._SIGSTOP) in sigs.calls
    rows = {r["pgid"]: r["pgids"] for r in json.loads(state.read_text())}
    assert [gid[0] for gid in rows[300]] == [300, 304]


def test_a_record_that_cannot_be_written_leaves_the_group_running(
    proc_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import atomic_write as aw

    def _enospc(*_a: Any, **_kw: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(aw, "atomic_write", _enospc)
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    assert pauser.pause([(200, "c")], everything=True) == []
    assert sigs.calls == [] and pauser.paused_count == 0
    assert not state.exists()


def test_a_closed_pauser_pauses_nothing(proc_tree: Path, tmp_path: Path) -> None:
    pauser, sigs, _state = _pauser(proc_tree, tmp_path)
    pauser.resume_all("stop", close=True)
    assert pauser.pause([(200, "c")], everything=True) == []
    pauser.reopen()
    assert len(pauser.pause([(200, "c")], everything=True)) == 2


# -- the controller resumes on every way out -----------------------------------


class _FakePauser:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.paused_count = 0

    def pause(self, runtimes: list[tuple[int, str]], *, everything: bool) -> list[Any]:
        self.calls.append("pause_all" if everything else "pause")
        self.paused_count += 1
        return [SimpleNamespace(pgid=1)]

    def resume_one(self, *, older_than: float = 0.0) -> None:
        if older_than > 0:
            self.calls.append("starve_check")
            return
        self.calls.append("resume_one")
        self.paused_count = max(0, self.paused_count - 1)

    def resume_all(self, why: str = "", *, close: bool = False) -> int:
        self.calls.append("resume_all" if close else "resume_all(open)")
        n, self.paused_count = self.paused_count, 0
        return n

    def reopen(self) -> None:
        pass

    def recover(self) -> int:
        return 0

    def paused(self) -> list[dict[str, object]]:
        return []


def _controller(free_gib: list[float], pauser: _FakePauser) -> AdaptiveController:
    manager = SimpleNamespace(
        user_max_concurrent=4, running_count=0, _queue=[], _agents={}, set_effective_cap=lambda c: c
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency=True))
    return AdaptiveController(
        manager,  # type: ignore[arg-type]
        cfg=cfg,
        host_probe=lambda: HostSample(free_mem_mb=free_gib[0] * GIB, total_mem_mb=HOST),
        read_runtimes=lambda: [(200, "chat-1")],
        shell_pauser=pauser,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_the_controller_pauses_on_pressure_and_resumes_on_recovery_and_stop() -> None:
    free = [10.0]
    pauser = _FakePauser()
    ctl = _controller(free, pauser)
    await ctl.tick()
    free[0] = 40.0
    await ctl.tick()
    free[0] = 1.0
    await ctl.tick()
    await ctl.stop()
    # Under the pause line every pause is followed by the starvation check.
    assert pauser.calls == ["pause", "starve_check", "resume_one", "pause_all", "resume_all"]


@pytest.mark.asyncio
async def test_a_failing_cap_actuator_does_not_skip_the_shell_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauser = _FakePauser()
    ctl = _controller([10.0], pauser)

    async def _boom(_decision: object) -> None:
        raise RuntimeError("gate down")

    monkeypatch.setattr(ctl, "apply", _boom)
    with pytest.raises(RuntimeError):
        await ctl.tick()
    assert pauser.calls == ["pause", "starve_check"]


@pytest.mark.asyncio
async def test_disabling_the_controller_resumes_every_group() -> None:
    pauser = _FakePauser()
    ctl = _controller([10.0], pauser)
    await ctl.tick()
    ctl.apply_config(SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency=False)))
    await ctl._run_shell(lambda: None)  # the resume runs on the pauser's worker
    assert pauser.calls == ["pause", "starve_check", "resume_all"]


@pytest.mark.asyncio
async def test_a_pause_decided_before_a_disable_does_not_land_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauser = _FakePauser()
    ctl = _controller([10.0], pauser)
    real_apply = ctl.apply

    async def _disable_mid_step(decision: Any) -> None:
        await real_apply(decision)
        ctl.apply_config(SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency=False)))

    monkeypatch.setattr(ctl, "apply", _disable_mid_step)
    await ctl.tick()
    await ctl._run_shell(lambda: None)
    assert pauser.calls == ["resume_all"]


@pytest.mark.asyncio
async def test_a_cancelled_stop_still_resumes() -> None:
    import asyncio

    pauser = _FakePauser()
    ctl = _controller([10.0], pauser)
    await ctl.tick()

    async def _never(_secs: float) -> None:
        await asyncio.Event().wait()

    ctl._sleep = _never  # type: ignore[assignment]
    ctl.start()
    await asyncio.sleep(0)
    stopper = asyncio.ensure_future(ctl.stop())
    await asyncio.sleep(0)
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    await ctl._run_shell(lambda: None)  # cancelled waiting, not cancelled working
    assert pauser.calls[-1] == "resume_all"


# -- a real process ------------------------------------------------------------


def _state_of(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text()
    return raw[raw.rfind(")") + 2]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_real_shell_group_is_stopped_and_continued(tmp_path: Path) -> None:
    """A fake runtime starts ``sleep`` in its own session, as kiro-cli's shell tool does."""
    runtime_code = textwrap.dedent("""
        import subprocess, sys
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        print(child.pid, flush=True)
        sys.stdin.read()
        child.kill()
        """)
    runtime = subprocess.Popen(
        [sys.executable, "-c", runtime_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        assert runtime.stdout is not None
        shell = int(runtime.stdout.readline())
        pauser = sp.ShellPauser(tmp_path / "paused.json", gateway_pid=os.getpid())
        paused = pauser.pause([(runtime.pid, "real")], everything=True)
        assert [g.pgid for g in paused] == [shell]
        wait_until(lambda: _state_of(shell) == "T", describe=lambda: _state_of(shell))
        assert _state_of(runtime.pid) != "T", "the runtime itself is never paused"
        assert pauser.resume_all("test") == 1
        wait_until(lambda: _state_of(shell) != "T", describe=lambda: _state_of(shell))
    finally:
        assert runtime.stdin is not None
        runtime.stdin.close()
        runtime.wait(timeout=default_timeout())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_a_real_timeout_wrapped_command_is_stopped_too(tmp_path: Path) -> None:
    """GNU ``timeout`` moves its child into a new process group; it must stop as well."""
    runtime_code = textwrap.dedent("""
        import os, signal, subprocess, sys
        shell = subprocess.Popen(
            ["sh", "-c", "timeout 60 sleep 60 & echo $!; wait"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        timeout_pid = int(shell.stdout.readline())
        print(shell.pid, timeout_pid, flush=True)
        sys.stdin.read()
        # Orderly, so every process is reaped by its own parent: continue both
        # groups, TERM timeout (it forwards the signal to sleep and reaps it), and
        # sh's `wait` then reaps timeout and exits. SIGKILL only as a fallback.
        for group in (timeout_pid, shell.pid):
            try:
                os.killpg(group, signal.SIGCONT)
            except OSError:
                pass
        try:
            os.kill(timeout_pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            shell.wait(timeout=30)
        except subprocess.TimeoutExpired:
            for group in (timeout_pid, shell.pid):
                try:
                    os.killpg(group, signal.SIGKILL)
                except OSError:
                    pass
            shell.wait()
        """)
    runtime = subprocess.Popen(
        [sys.executable, "-c", runtime_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    timeout_pid = 0
    try:
        assert runtime.stdout is not None
        shell, timeout_pid = (int(x) for x in runtime.stdout.readline().split())
        wait_until(lambda: (sp._stat("/proc", timeout_pid) or (0,))[0] == timeout_pid)
        sleeper = wait_until(
            lambda: next((p for p in sp.iter_descendants("/proc", timeout_pid)[1:]), None)
        )
        pauser = sp.ShellPauser(tmp_path / "paused.json", gateway_pid=os.getpid())
        assert [g.pgid for g in pauser.pause([(runtime.pid, "real")], everything=True)] == [shell]
        wait_until(lambda: _state_of(sleeper) == "T", describe=lambda: _state_of(sleeper))
        assert pauser.resume_all("test") == 1
        wait_until(lambda: _state_of(sleeper) != "T", describe=lambda: _state_of(sleeper))
    finally:
        # An assertion that failed while the groups were stopped must not leave
        # them stopped: the fake runtime's cleanup continues them, terminates
        # timeout and reaps everything, and this waits for it.
        assert runtime.stdin is not None
        runtime.stdin.close()
        runtime.wait(timeout=default_timeout())


# -- process identity: a pgid is never trusted by its number alone ------------


def _replace_leader(proc_tree: Path, pid: int, *, pgrp: int, sid: int, start: int) -> None:
    """The pid now belongs to another process: same number, another start tick."""
    fields = ["S", "1", str(pgrp), str(sid)] + ["0"] * 15 + [str(start)] + ["0"] * 5
    (proc_tree / str(pid) / "stat").write_text(f"{pid} (other) " + " ".join(fields))


def test_a_reused_pgid_is_not_exempt_from_pausing(proc_tree: Path, tmp_path: Path) -> None:
    """GPT: an old record for pgid 310 must not shield a NEW shell that reuses 310."""
    pauser, sigs, _state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    _replace_leader(proc_tree, 310, pgrp=310, sid=310, start=9999)  # a new shell
    sigs.calls.clear()
    paused = pauser.pause([(200, "c")], everything=True)
    assert [(g.pgid, g.started) for g in paused] == [(310, 9999.0)]
    assert (310, sp._SIGSTOP) in sigs.calls


def test_pause_never_adopts_a_leader_replaced_after_discovery(
    proc_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: discovery saw 310 started at 2000; by the stop the pid is another process."""
    pauser, sigs, _state = _pauser(proc_tree, tmp_path)
    monkeypatch.setattr(
        sp, "discover_shell_groups", lambda *_a, **_kw: [sp.ShellGroup(310, "c", 2000.0)]
    )
    _replace_leader(proc_tree, 310, pgrp=310, sid=310, start=9999)
    assert pauser.pause([(200, "c")], everything=True) == []
    assert sigs.calls == []


def test_recovery_checks_a_subgroup_recorded_without_a_tick(
    proc_tree: Path, tmp_path: Path
) -> None:
    """GPT: a subgroup recorded as (302, -1) is continued only while 302 is still a
    group leader in the shell's session, never when the id is someone else's."""
    state = tmp_path / "run" / "paused.json"
    state.parent.mkdir(parents=True)
    row = {
        "pgid": 300,
        "started": 1000.0,
        "session": "c",
        "pgids": [[300, 1000.0], [302, -1.0]],
    }
    state.write_text(json.dumps([row]))
    _proc(proc_tree, 302, pgrp=302, sid=777, start=9999)  # unrelated job, other session
    pauser, sigs, _ = _pauser(proc_tree, tmp_path)
    pauser.recover()
    assert (300, sp._SIGCONT) in sigs.calls
    assert (302, sp._SIGCONT) not in sigs.calls
    # A group that a live member of the shell's OWN session (sid 300) holds right now
    # is continued: it is found by the session scan, not taken from the record.
    state.write_text(json.dumps([row]))
    _replace_leader(proc_tree, 302, pgrp=302, sid=300, start=1002)
    pauser2, sigs2, _ = _pauser(proc_tree, tmp_path)
    pauser2.recover()
    assert (302, sp._SIGCONT) in sigs2.calls


def test_resume_skips_a_group_whose_leader_was_replaced(proc_tree: Path, tmp_path: Path) -> None:
    """GPT + Opus: a reused leader gets no SIGCONT, and the dead record does not cost
    a sample -- the next group resumes in the same call."""
    pauser, sigs, _state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)  # 310, then 300
    _replace_leader(proc_tree, 310, pgrp=310, sid=310, start=9999)
    sigs.calls.clear()
    resumed = pauser.resume_one()
    assert resumed is not None and resumed.pgid == 300
    assert (310, sp._SIGCONT) not in sigs.calls
    assert pauser.paused_count == 0


def test_line_recovery_keeps_the_others_pause_age(proc_tree: Path, tmp_path: Path) -> None:
    """GPT: an ordinary resume must not restart the starvation clock of the rest."""
    clock = _Clock()
    pauser, _sigs, _state = _pauser(proc_tree, tmp_path, clock=clock)
    pauser.pause([(200, "c")], everything=False)  # 310 at t
    clock.now += 1
    pauser.pause([(200, "c")], everything=False)  # 300 at t + 1
    clock.now += 300
    assert pauser.resume_one() is not None  # ordinary recovery of 310, at t + 301
    clock.now += 300
    # 300 has waited 600 s since ITS pause; a clock restarted by the resume would
    # say 300 s and keep it stopped.
    starved = pauser.resume_one(older_than=600)
    assert starved is not None and starved.pgid == 300


def test_resume_all_counts_only_groups_it_reached(proc_tree: Path, tmp_path: Path) -> None:
    pauser, _sigs, _state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)  # 310 and 300
    _replace_leader(proc_tree, 310, pgrp=310, sid=310, start=9999)
    assert pauser.resume_all("test") == 1


def test_a_tickless_subgroup_is_continued_after_its_shell_leader_exits(
    proc_tree: Path, tmp_path: Path
) -> None:
    """Opus: 302's leader had exited at pause time (recorded with no tick); then the
    shell leader 300 itself dies (OOM, a tool timeout). A member with session 300
    and group 302 is still alive and stopped, and must get its SIGCONT."""
    import shutil

    (proc_tree / "301" / "task" / "301" / "children").write_text("303")
    _proc(proc_tree, 303, pgrp=302, sid=300, start=1003)  # 302's leader already gone
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    rows = {r["pgid"]: r["pgids"] for r in json.loads(state.read_text())}
    assert [302, -1.0] in rows[300]
    shutil.rmtree(proc_tree / "300")  # the shell leader dies; 303 lives on
    sigs.calls.clear()
    pauser.resume_all("test")
    assert (302, sp._SIGCONT) in sigs.calls


# -- GPT f8ba1d5803caabac: a record is dropped only once resuming is confirmed --


class _ContFails:
    """killpg whose SIGCONT fails with EPERM until ``healed``; SIGSTOP works."""

    def __init__(self, inner: _Signals) -> None:
        self.inner = inner
        self.healed = False

    def __call__(self, pgid: int, sig: int) -> None:
        if sig == sp._SIGCONT and not self.healed:
            raise PermissionError(1, "Operation not permitted")
        self.inner(pgid, sig)


def test_a_failed_sigcont_keeps_the_record_and_is_retried(proc_tree: Path, tmp_path: Path) -> None:
    """GPT: a failed resume must keep the record, or the group is stranded."""
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    flaky = _ContFails(sigs)
    pauser._killpg = flaky
    pauser.pause([(200, "c")], everything=True)
    assert pauser.resume_one() is None  # failed: nothing resumed...
    assert pauser.paused_count == 2  # ...and nothing forgotten
    assert pauser.resume_all("stop") == 0
    assert pauser.paused_count == 2 and state.exists()  # on disk for the next recovery
    flaky.healed = True
    assert pauser.resume_all("stop") == 2
    assert pauser.paused_count == 0 and not state.exists()


def test_recovery_keeps_a_row_whose_sigcont_failed(proc_tree: Path, tmp_path: Path) -> None:
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    pauser.pause([(200, "c")], everything=True)
    fresh, fresh_sigs, _ = _pauser(proc_tree, tmp_path)
    fresh._killpg = _ContFails(fresh_sigs)
    assert fresh.recover() == 0
    assert state.exists() and fresh.paused_count == 2  # kept, to be retried


def test_a_late_subgroup_is_on_disk_before_its_sigstop(proc_tree: Path, tmp_path: Path) -> None:
    """GPT: a late subgroup must be recorded before it is stopped, or a crash in
    between leaves it stopped with no record."""
    pauser, sigs, state = _pauser(proc_tree, tmp_path)
    seen: list[bool] = []
    inner = sigs.__call__

    def _racing(pgid: int, sig: int) -> None:
        if sig == sp._SIGSTOP and pgid == 304:
            rows = json.loads(state.read_text())
            seen.append(any(gid[0] == 304 for r in rows for gid in r["pgids"]))
        inner(pgid, sig)
        if pgid == 300 and sig == sp._SIGSTOP and not (proc_tree / "304").exists():
            (proc_tree / "301" / "task" / "301" / "children").write_text("304")
            _proc(proc_tree, 304, pgrp=304, sid=300, start=1004)

    pauser._killpg = _racing
    pauser.pause([(200, "c")], everything=True)
    assert seen == [True]


def test_a_late_subgroup_that_cannot_be_recorded_is_not_stopped(
    proc_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import atomic_write as aw

    pauser, sigs, _state = _pauser(proc_tree, tmp_path)
    real_write = aw.atomic_write
    inner = sigs.__call__

    def _racing(pgid: int, sig: int) -> None:
        inner(pgid, sig)
        if pgid == 300 and sig == sp._SIGSTOP and not (proc_tree / "304").exists():
            (proc_tree / "301" / "task" / "301" / "children").write_text("304")
            _proc(proc_tree, 304, pgrp=304, sid=300, start=1004)

            def _full(path: Any, text: str, **kw: Any) -> None:
                if "[304, " in text:
                    raise OSError(28, "No space left on device")
                real_write(path, text, **kw)

            monkeypatch.setattr(aw, "atomic_write", _full)

    pauser._killpg = _racing
    pauser.pause([(200, "c")], everything=True)
    assert (304, sp._SIGSTOP) not in sigs.calls


def test_startup_recovers_paused_shells_with_adaptive_concurrency_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: with the controller off at start, nothing resumed a crashed gateway's
    paused shells. The record is found where the controller writes it."""
    from kiro_crew.adaptive import controller as ctl_mod
    from kiro_crew.config import paths
    from kiro_crew.slack.gateway_runtime import admission

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    record = ctl_mod._shell_state_path()
    assert record == tmp_path / "run" / "paused-shell-groups.json"
    record.parent.mkdir(parents=True)
    record.write_text("[]")
    recovered: list[Path] = []
    monkeypatch.setattr(sp, "recover_paused_shells", lambda path: recovered.append(path) or 0)
    orch = SimpleNamespace(
        _adaptive_controller=None,
        subagent_mgr=object(),
        _cfg=SimpleNamespace(agent=SimpleNamespace(adaptive_concurrency=False)),
        _adaptive_start_sub=object(),  # the live watch is already armed
    )
    admission._start_adaptive_controller(orch)  # type: ignore[arg-type]
    wait_until(lambda: recovered == [record], describe=lambda: recovered)
