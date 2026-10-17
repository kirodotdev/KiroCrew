"""Characterization of the ACP process-tree helpers: descendants, RSS, identity, sweep.

Pins the per-platform answers of the child-walk, RSS and process-identity helpers
the client and the shared runtime define: the Windows short-circuits, the darwin
``ps`` parsing and memoized process table, the Linux ``/proc`` reads, the
leaf-first escaped-child sweep over the production ``ChildRecord`` shape, and that
``kiro_crew.acp.runtime`` re-exports the same objects the client defines.

The code is reached only through the ``kiro_crew.acp.client`` /
``kiro_crew.acp.runtime`` facades. Platform state is faked only through SHARED
module attributes (``sys.platform``, ``platform_compat.*``,
``subprocess.check_output``) plus names defined on a facade, so these tests hold
unchanged before and after the definitions move to their owner module. Every such
patch is scoped to the calls under test, never to fixture teardown.

No fabricated pid here can name a live process: the ones handed to a patched
sweep sit above every platform's ``pid_max``, and the Linux ``/proc`` probes use
the test's own pid or one past the kernel's ``PID_MAX_LIMIT``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime

_MIB = 1024 * 1024
# Above every supported platform's pid_max (the house spelling is 99_999_999_999).
_PID = 99_999_999_999
_LEAF_A, _LEAF_B, _LEAF_C = 99_999_999_910, 99_999_999_920, 99_999_999_930
# One past Linux's PID_MAX_LIMIT (2**22): /proc can never hold this pid.
_NO_SUCH_LINUX_PID = 2**22 + 12345

_PS_TABLE = b"1 0 1024\nbad line\n2 1 x\n3 1 2048\n4 3 4096\n3 1 2048\n"


@pytest.fixture(autouse=True)
def _fresh_ps_table() -> Iterator[None]:
    acp_runtime._reset_ps_table_cache()
    yield
    acp_runtime._reset_ps_table_cache()


def _refuse_spawn(argv):
    raise AssertionError(f"no process may be spawned here: {argv!r}")


@contextlib.contextmanager
def _as_windows() -> Iterator[tuple[pytest.MonkeyPatch, list[list[str]]]]:
    """win32, with every ``check_output`` recorded: the helpers under test swallow
    a raising spawn, so only the record can show that one was attempted."""
    spawned: list[list[str]] = []

    def _record_spawn(argv, **kwargs):
        spawned.append(list(argv))
        return b""

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(sys, "platform", "win32")
        patched.setattr(platform_compat, "IS_WINDOWS", True)
        patched.setattr(subprocess, "check_output", _record_spawn)
        yield patched, spawned


@contextlib.contextmanager
def _as_darwin(check_output) -> Iterator[list[list[str]]]:
    """darwin with a trusted ``ps`` whose output ``check_output(argv)`` decides."""
    calls: list[list[str]] = []

    def _recording(argv, **kwargs):
        calls.append(list(argv))
        return check_output(argv)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(sys, "platform", "darwin")
        patched.setattr(platform_compat, "IS_WINDOWS", False)
        patched.setattr(platform_compat, "trusted_system_bin", lambda name: f"/bin/{name}")
        # The ps path is under test; on a real Mac the footprint reader would
        # answer first for a same-uid pid the fake output names.
        patched.setattr(platform_compat, "proc_phys_footprint_bytes_for_pid", lambda pid: None)
        patched.setattr(subprocess, "check_output", _recording)
        yield calls


def test_runtime_reexports_the_client_child_helpers():
    assert acp_runtime._get_child_pids is acp_client._get_child_pids
    assert acp_runtime._capture_child_records is acp_client._capture_child_records
    assert acp_runtime.ChildRecord is acp_client.ChildRecord


@pytest.mark.parametrize(
    "pid, max_depth, children, expected",
    [
        pytest.param(1, None, {1: [2, 3], 3: [2]}, [1, 3, 2], id="reachable-twice-counted-once"),
        pytest.param(1, 1, {1: [2, 3], 3: [4]}, [1, 3, 2], id="depth-one-stops-at-children"),
        pytest.param(1, 0, {1: [2]}, [1], id="depth-zero-is-the-root"),
        pytest.param(9, None, {1: [2]}, [9], id="root-missing-from-map"),
    ],
)
def test_iter_descendant_pids_over_a_parent_map(pid, max_depth, children, expected):
    assert acp_runtime._iter_descendant_pids(pid, max_depth, children=children) == expected


# ── Windows ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rss_bytes, expected", [(3 * _MIB, 3.0), (None, None)])
def test_windows_rss_reads_the_working_set_shim(rss_bytes, expected):
    asked: list[int] = []

    def _working_set(pid: int) -> int | None:
        asked.append(pid)
        return rss_bytes

    with _as_windows() as (patched, spawned):
        patched.setattr(platform_compat, "proc_rss_bytes_for_pid", _working_set)
        result = acp_runtime._get_rss_mb(_PID)
    assert result == expected
    assert asked == [_PID]
    assert spawned == []


def test_windows_rss_tree_uses_the_validated_walk_and_refuses_a_depth_bound():
    asked: list[int] = []

    def _tree_mb(pid: int) -> float:
        asked.append(pid)
        return 42.5

    with _as_windows() as (patched, spawned):
        patched.setattr(platform_compat, "proc_rss_tree_mb_for_pid", _tree_mb)
        whole_tree = acp_runtime._get_rss_tree_mb(_PID)
        bounded = acp_runtime._get_rss_tree_mb(_PID, 1)
    assert whole_tree == 42.5
    assert bounded is None
    assert asked == [_PID]
    assert spawned == []


def test_windows_client_helpers_short_circuit_without_spawning():
    # The test's own pid: every non-Windows branch would answer something else
    # for it (a /proc read or a spawn), so only the short-circuit yields these.
    me = os.getpid()
    with _as_windows() as (_, spawned):
        children = acp_client._direct_children(me)
        start = acp_client._get_start_time(me)
        basename = acp_client._read_basename(me)
    assert (children, start, basename) == ([], None, None)
    assert spawned == []


def test_windows_escaped_child_sweep_is_a_no_op():
    probed: list[int] = []
    with _as_windows() as (patched, _):
        patched.setattr(platform_compat, "pid_exists", lambda pid: probed.append(pid) or True)
        result = acp_client._kill_escaped_children({_LEAF_A: ("s", b"x")})
    assert result is None
    assert probed == []


# ── darwin ───────────────────────────────────────────────────────────────────


def _ps_raises(argv):
    raise OSError("ps failed")


@pytest.mark.parametrize(
    "ps_output, expected",
    [
        pytest.param(lambda argv: b" 2048\n", 2.0, id="kib-to-mib"),
        pytest.param(lambda argv: b"garbage", None, id="unparseable"),
        pytest.param(_ps_raises, None, id="ps-fails"),
    ],
)
def test_darwin_rss_reads_ps(ps_output, expected):
    with _as_darwin(ps_output) as calls:
        result = acp_runtime._get_rss_mb(_PID)
    assert result == expected
    assert calls == [["/bin/ps", "-o", "rss=", "-p", str(_PID)]]


def test_darwin_rss_without_a_trusted_ps_is_unknown():
    with _as_darwin(_refuse_spawn) as calls, pytest.MonkeyPatch.context() as patched:
        patched.setattr(platform_compat, "trusted_system_bin", lambda name: None)
        assert acp_runtime._get_rss_mb(_PID) is None
        assert acp_runtime._ps_process_table() is None
    assert calls == []


def test_darwin_process_table_skips_malformed_lines():
    with _as_darwin(lambda argv: _PS_TABLE) as calls:
        table = acp_runtime._ps_process_table()
    assert table == ({0: [1], 1: [3, 3], 3: [4]}, {1: 1024, 3: 2048, 4: 4096})
    assert calls == [["/bin/ps", "-Ao", "pid=,ppid=,rss="]]


@pytest.mark.parametrize(
    "pid, max_depth, expected",
    [
        pytest.param(1, None, 7.0, id="whole-subtree-duplicate-edge-once"),
        pytest.param(1, 1, 3.0, id="depth-bounded"),
        pytest.param(99, None, None, id="pid-absent-from-table"),
    ],
)
def test_darwin_rss_tree_sums_the_shared_table(pid, max_depth, expected):
    with _as_darwin(lambda argv: _PS_TABLE):
        assert acp_runtime._get_rss_tree_mb(pid, max_depth) == expected


def test_darwin_rss_tree_falls_back_to_the_single_pid_read_when_ps_table_fails():
    def _only_single_pid(argv):
        if "-Ao" in argv:
            raise OSError("table walk failed")
        return b" 2048\n"

    with _as_darwin(_only_single_pid) as calls:
        assert acp_runtime._get_rss_tree_mb(_PID) == 2.0
    assert [argv[1] for argv in calls] == ["-Ao", "-o"]


# ── Linux /proc ──────────────────────────────────────────────────────────────

_linux_only = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")


@_linux_only
def test_linux_start_time_is_stat_field_22():
    stat = Path(f"/proc/{os.getpid()}/stat").read_text()
    assert acp_client._get_start_time(os.getpid()) == int(stat.rsplit(")", 1)[1].split()[19])


@_linux_only
def test_linux_basename_is_argv0_basename():
    expected = os.fsencode(os.path.basename(sys.orig_argv[0]))
    assert acp_client._read_basename(os.getpid()) == expected


@_linux_only
def test_linux_helpers_answer_none_for_a_pid_proc_cannot_hold():
    assert acp_client._get_start_time(_NO_SUCH_LINUX_PID) is None
    assert acp_client._read_basename(_NO_SUCH_LINUX_PID) is None


# ── escaped-child sweep ──────────────────────────────────────────────────────


@contextlib.contextmanager
def _sweep_spies(*, lookup_error_for: int | None = None) -> Iterator[dict[str, list]]:
    calls: dict[str, list] = {"our": [], "kill": []}

    def _is_ours(pid, expected_start=None, expected_basename=None):
        calls["our"].append((pid, expected_start, expected_basename))
        return True

    def _kill(pid, sig):
        calls["kill"].append((pid, sig))
        if pid == lookup_error_for:
            raise ProcessLookupError(pid)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(platform_compat, "IS_WINDOWS", False)
        patched.setattr(platform_compat, "pid_exists", lambda pid: True)
        patched.setattr(platform_compat, "kill_pid", _kill)
        patched.setattr(acp_client, "_is_our_child", _is_ours)
        yield calls


def test_escaped_child_sweep_is_leaf_first_over_child_records():
    records = {_LEAF_A: ("s10", b"node"), _LEAF_B: ("s20", b"py"), _LEAF_C: None}
    with _sweep_spies(lookup_error_for=_LEAF_B) as calls:
        assert acp_client._kill_escaped_children(records) is None
    assert calls["our"] == [
        (_LEAF_C, None, None),
        (_LEAF_B, "s20", b"py"),
        (_LEAF_A, "s10", b"node"),
    ]
    sigkill = platform_compat.SIGKILL
    assert calls["kill"] == [(_LEAF_C, sigkill), (_LEAF_B, sigkill), (_LEAF_A, sigkill)]


def test_escaped_child_sweep_carries_a_legacy_int_record_as_unproven():
    with _sweep_spies() as calls:
        acp_client._kill_escaped_children({_LEAF_A: 12345})
    assert calls["our"] == [(_LEAF_A, None, None)]


def test_is_our_child_denies_when_the_start_id_read_raises():
    def _raises(pid: int) -> str:
        raise RuntimeError("start id unreadable")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(platform_compat, "get_process_start_id", _raises)
        assert acp_client._is_our_child(_PID, "s", b"x") is False
