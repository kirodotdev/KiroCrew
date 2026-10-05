"""The sandbox chokepoint's tool marker, and the separation its meaning rests on.

A tree spawned through :func:`kiro_crew.sandbox.sandboxed_spawn_argv` lands in this
install's agent slice carrying the inherited ``KIROCREW_SPAWNED`` ownership marker and
appears in no membership record the runtime reconciler reads. Once such a tree outlives
the reconciler's age floor -- a LaTeX build, an ``npx`` install, a provisioning run --
the argv0 basename test is the only thing between it and a signal.

``KIROCREW_SANDBOX_TOOL`` is the evidence that replaces the name: stamped by the
chokepoint, inherited by the whole tree, and read back out of the kernel's exec-time
copy, which a same-uid process cannot alter on another process.

The marker describes a TREE, not each process in it, and the chokepoint takes harness
argv on purpose -- ``is_kiro_cli`` exists so a delegating spawn can route through it.
So a harness can carry the marker two ways: routed through the chokepoint directly, or
spawned inside a marked tool tree. Either way it is an orphaned harness in the slice,
in no record, past the age floor -- the one stray class this arm can reach. That is why
the exclusion requires the marker AND a non-harness argv0, and why these tests pin the
harness-routing sites rather than a separation that does not exist.
"""

from __future__ import annotations

import ast
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import runtime_reconcile, sandbox, session_pid
from kiro_crew.constants import (
    KIROCREW_SANDBOX_TOOL_ENV,
    KIROCREW_SANDBOX_TOOL_VALUE,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_SRC = Path(sandbox.__file__).resolve().parent

#: The spawn paths that put a session-leader runtime on the host: the two drivers, which
#: hand their own ``wrap_argv`` to the launch tail they share, and that tail, which
#: threads the ``is_kiro_cli`` delegation decision into it directly -- so a runtime this
#: install starts as a leader carries no tool marker.
_LEADER_SPAWN_SITES = ("acp/runtime.py", "acp/client.py", "acp/launch.py")

#: Sites that route a MANAGED-HARNESS argv through the tool chokepoint, which is why
#: the exclusion cannot rest on the marker alone. The pod child probe spawns
#: ``kiro-cli`` through it with ``is_kiro_cli=delegate``; the auto-improvement agent
#: runner spawns ``claude`` through it with ``start_new_session=True``. Both basenames
#: are in ``session_pid._MANAGED_AGENT_BASENAMES``, both land in the slice through
#: ``cgroup_scope_argv``, and neither is in any membership record.
_HARNESS_ROUTING_SITES = (
    "agent_sdk/pod_child_probe.py",
    "apps/builtins/auto_improvement/spine/agent_runner.py",
)


def _marked_env() -> dict[str, str]:
    """The environment the chokepoint hands its caller, on a host with no backend.

    ``detect_backend`` answering ``"none"`` takes the fail-open path, so the wrap adds
    no launcher and the env is the chokepoint's own contribution. The consent gate is
    patched because an unsandboxed exec is otherwise refused outright.
    """
    with (
        patch("kiro_crew.sandbox.detect_backend", return_value="none"),
        patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True),
    ):
        _argv, env, cleanup = sandbox.sandboxed_spawn_argv(["echo", "hi"])
    if cleanup:
        Path(cleanup).unlink(missing_ok=True)
    return env


def test_the_chokepoint_stamps_the_tool_marker_alongside_the_ownership_marker() -> None:
    """Both markers, under DIFFERENT keys.

    The ownership marker is the reconciler's kill-enabling condition and the tool marker
    withholds a kill, so one key carrying both meanings could not express the second
    without weakening the first.
    """
    env = _marked_env()
    assert env[KIROCREW_SPAWNED_ENV] == KIROCREW_SPAWNED_VALUE
    assert env[KIROCREW_SANDBOX_TOOL_ENV] == KIROCREW_SANDBOX_TOOL_VALUE
    assert KIROCREW_SANDBOX_TOOL_ENV != KIROCREW_SPAWNED_ENV


def test_the_marker_read_answers_from_a_fixture_process_table(tmp_path: Path) -> None:
    """POSITIVE CONTROL for the read: a reader that answered ``None`` everywhere would
    satisfy every fail-open case below and mean nothing."""
    proc = tmp_path / "4242"
    proc.mkdir()
    (proc / "environ").write_bytes(b"PATH=/usr/bin\0KIROCREW_SPAWNED=1\0KIROCREW_SANDBOX_TOOL=1\0")
    assert session_pid._env_is_sandbox_tool(4242, proc_root=tmp_path) is True


def test_a_readable_environment_without_the_marker_reads_false(tmp_path: Path) -> None:
    """An agent runtime is exactly this case: our spawn marker, no tool marker."""
    proc = tmp_path / "4243"
    proc.mkdir()
    (proc / "environ").write_bytes(b"PATH=/usr/bin\0KIROCREW_SPAWNED=1\0")
    assert session_pid._env_is_sandbox_tool(4243, proc_root=tmp_path) is False


def test_an_unreadable_environment_reads_none_and_not_false(tmp_path: Path) -> None:
    """The tri-state contract, asserted as ``is None`` rather than as falsy.

    ``False`` and ``None`` both leave a pid in the candidate population, so collapsing
    them would pass a falsy assertion while destroying the distinction a caller that
    reads this marker to GRANT something would need.
    """
    assert session_pid._env_is_sandbox_tool(4244, proc_root=tmp_path) is None


def test_a_marker_value_other_than_ours_does_not_match(tmp_path: Path) -> None:
    """The read is a whole-entry match, so a same-named variable set to something else
    by a user's own shell is not our marker."""
    proc = tmp_path / "4245"
    proc.mkdir()
    (proc / "environ").write_bytes(b"KIROCREW_SANDBOX_TOOL=0\0")
    assert session_pid._env_is_sandbox_tool(4245, proc_root=tmp_path) is False


@pytest.mark.skipif(sys.platform != "linux", reason="needs /proc for a live read")
def test_a_grandchild_the_spawn_never_recorded_carries_the_marker() -> None:
    """END TO END: the stamped env reaches a descendant's ``/proc`` entry.

    The claim the exclusion rests on is about processes no spawn recorded, and a
    grandchild is the smallest one of those. Read live, with no fixture seam, so the
    env-inheritance step is the kernel's rather than the test's.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(g.pid, flush=True)\n"
            "time.sleep(30)\n",
        ],
        env=_marked_env(),
        stdout=subprocess.PIPE,
        # The child is a Python process whose only output is a pid we print, so its
        # encoding is knowable and pinning UTF-8 is the correct call here.
        **UTF8_TEXT,
    )
    grandchild: int | None = None
    try:
        assert child.stdout is not None
        grandchild = int(child.stdout.readline().strip())
        assert session_pid._env_is_sandbox_tool(child.pid) is True
        assert session_pid._env_is_sandbox_tool(grandchild) is True
    finally:
        child.kill()
        child.wait(timeout=10)
        if grandchild is not None:
            # Signalled directly: the parent's kill does not reach it, and spawning a
            # helper to do it would be another child to clean up.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(grandchild, signal.SIGKILL)


def _referenced_names(relative: str) -> set[str]:
    """Every name and attribute this module references.

    Encoding pinned: these sources are UTF-8 and the Windows ANSI code page raises on
    bytes some of them carry, which would fail the scan for a reason unrelated to what
    it asserts.
    """
    tree = ast.parse((_SRC / relative).read_text(encoding="utf-8"))
    return {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }


@pytest.mark.parametrize("relative", _HARNESS_ROUTING_SITES)
def test_the_chokepoint_still_has_a_harness_named_caller(relative: str) -> None:
    """Why the exclusion needs the argv conjunction, pinned where the need comes from.

    The chokepoint accepts harness argv on purpose -- ``is_kiro_cli`` exists so a
    delegating spawn can route through it instead of calling ``wrap_argv`` directly --
    and these sites take that route with an argv0 in ``_MANAGED_AGENT_BASENAMES``. A
    stranded one of them is an orphaned harness inside the slice carrying the tool
    marker, which is the population the conjunction in ``_unowned`` keeps reachable.

    This fails when a site stops routing that way, which is the moment to re-check
    whether the conjunction still earns its place -- rather than discovering later that
    it guards nothing, or that a third site reintroduced the case unnoticed.
    """
    chokepoint = "sandboxed_spawn" + "_argv"
    assert chokepoint in _referenced_names(relative), (
        f"{relative} no longer routes its harness spawn through the chokepoint -- "
        "re-check whether _unowned still needs the managed-argv conjunction"
    )


@pytest.mark.parametrize("relative", _LEADER_SPAWN_SITES)
def test_a_session_leader_spawn_reaches_the_wrap_directly(relative: str) -> None:
    """A runtime this install starts as a session leader carries no tool marker at all.

    A narrower claim than it looks, and deliberately so: it says nothing about other
    harness spawns, which is why the conjunction carries the guarantee and this scan
    does not. The positive assertion pairs with the negative one so an empty result
    means "reaches the wrap" rather than "the scan matched nothing".
    """
    names = _referenced_names(relative)
    chokepoint = "sandboxed_spawn" + "_argv"
    assert "wrap_argv" in names, f"{relative} reaches no sandbox wrap at all"
    assert chokepoint not in names, f"{relative} now routes its leader spawn through the chokepoint"


def _reconciler(
    *,
    kernel: set[int],
    sandbox_tools: set[int] | None = None,
    managed: set[int] | None = None,
    tool_check_raises: bool = False,
    killed: list[int] | None = None,
    audited: list[tuple[int, str, str]] | None = None,
) -> runtime_reconcile.RuntimeReconciler:
    """A reconciler over a fake kernel whose pids satisfy EVERY kill condition.

    Nothing is recorded, everything is ours, managed, alive, past the age floor and
    authorized, and the budget is the shipped one. So a pid reaching the arm IS killed
    here, which is what makes the exclusion's effect observable rather than inferred:
    a test where nothing could be killed anyway would pass with the exclusion removed.

    The identity and argv seams are faked because their defaults read the HOST for
    these invented numbers, which would make a verdict depend on what happens to run
    at pid 100 on the box.
    """
    tools = sandbox_tools or set()

    def is_sandbox_tool(pid: int) -> bool:
        if tool_check_raises:
            raise OSError("environ unreadable")
        return pid in tools

    return runtime_reconcile.RuntimeReconciler(
        slice_pids=lambda: set(kernel),
        recorded_pids=set,
        is_alive=lambda pid: pid in kernel,
        is_ours=lambda pid: True,
        is_managed=lambda pid: pid in (kernel if managed is None else managed),
        is_sandbox_tool=is_sandbox_tool,
        leases_on=lambda pid: 0,
        claims_on=lambda pid: 0,
        authorize=lambda pid, reason: True,
        identity_of=lambda pid: f"id-{pid}",
        kill_tree=lambda pid, expected=None: (killed if killed is not None else []).append(pid)
        or 1,
        forget=lambda pid: "retracted",
        notify_dead=lambda pid: None,
        age_secs=lambda pid: 10_000.0,
        audit=lambda pid, outcome, why: (audited if audited is not None else []).append(
            (pid, outcome, why)
        ),
    )


def test_a_sandbox_tool_leaves_the_population_before_it_is_counted() -> None:
    """Excluded in ``_unowned``, not held later: it never reaches the count, the gate
    or an attribution row.

    The control pid is the same case in every respect but the marker, so a pass that
    excluded everything would fail here.
    """
    killed: list[int] = []
    audited: list[tuple[int, str, str]] = []
    # 4101 is the tool: marked, and not a harness. 4102 is the control harness.
    reconciler = _reconciler(
        kernel={4101, 4102},
        sandbox_tools={4101},
        managed={4102},
        killed=killed,
        audited=audited,
    )

    first = reconciler.run_once()
    assert first.unowned_alive == 1, "only the control pid is a candidate"
    second = reconciler.run_once()

    assert second.unowned_alive == 1
    assert killed == [4102], "the tool is spared and the control pid is signalled"
    assert all(
        pid != 4101 for pid, _outcome, _why in audited
    ), "an excluded pid collects no attribution row at all"


def test_a_harness_inside_a_tool_tree_stays_a_candidate() -> None:
    """The marker is inherited by the whole tree; the argv test is per process.

    An agent's terminal command routes through the same chokepoint, and a shell can
    launch a harness, so a runtime leaked inside a tool tree carries the tool marker
    without being tool work. It is the one stray class this arm can reach, and the
    exclusion must not take it. Both pids here are marked as tools -- only the
    harness bit differs -- so a pass that excluded on the marker alone would spare
    both and fail here.
    """
    killed: list[int] = []
    reconciler = _reconciler(
        kernel={4111, 4112}, sandbox_tools={4111, 4112}, managed={4112}, killed=killed
    )

    reading = reconciler.run_once()
    assert reading.unowned_alive == 1, "the leaked harness is still counted"
    reconciler.run_once()
    assert killed == [4112], "the harness is reachable and the tool is not"


def test_an_unreadable_marker_does_not_exclude() -> None:
    """FAIL-OPEN: the exclusion is an extra sparing, so doubt must not widen it.

    With the marker read raising, both pids stay candidates and the arm reaches them,
    which is exactly where they sit on a host with no marker at all.
    """
    killed: list[int] = []
    reconciler = _reconciler(kernel={4201, 4202}, tool_check_raises=True, killed=killed)

    reading = reconciler.run_once()
    assert reading.unowned_alive == 2
    reconciler.run_once()
    assert sorted(killed) == [4201, 4202]


def test_the_exclusion_reads_the_marker_from_a_fixture_process_table(tmp_path: Path) -> None:
    """POSITIVE CONTROL for the production seam: without this, the default seam never
    answering True would satisfy every case above and mean nothing."""
    proc = tmp_path / "4301"
    proc.mkdir()
    (proc / "environ").write_bytes(b"KIROCREW_SPAWNED=1\0KIROCREW_SANDBOX_TOOL=1\0")
    assert runtime_reconcile.process_is_sandbox_tool(4301, proc_root=tmp_path) is True

    runtime = tmp_path / "4302"
    runtime.mkdir()
    (runtime / "environ").write_bytes(b"KIROCREW_SPAWNED=1\0")
    assert runtime_reconcile.process_is_sandbox_tool(4302, proc_root=tmp_path) is False
    assert (
        runtime_reconcile.process_is_ours(4302, proc_root=tmp_path) is True
    ), "the two markers are read independently: a runtime is ours and not a tool"


def test_an_unreadable_environment_does_not_read_as_a_tool(tmp_path: Path) -> None:
    """The boolean wrapper's half of fail-open: no ``/proc`` entry, no exclusion."""
    assert runtime_reconcile.process_is_sandbox_tool(4303, proc_root=tmp_path) is False
