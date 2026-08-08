"""Regression tests for the macOS process-table amplification.

The bug: ``_get_rss_tree_mb``'s non-Linux branch took a WHOLE-MACHINE
``ps -Ao pid=,ppid=,rss=`` snapshot, and ``session_memory._blocking_sample``
calls it once per live runtime pid. So sampling N sessions walked the host's
entire process table N times, every 5s, serialized in one worker — which
saturated the pool the event loop shares and made the dashboard stop answering.

These tests are written against the SHARED-snapshot contract rather than against
timings, so they fail if the per-pid snapshot is ever reintroduced.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from kiro_crew.acp import runtime as rt

# The Linux branch reads /proc directly and never spawns, so the behaviour under
# test only exists on the non-Linux path. Force that path rather than skipping,
# so the contract is verified on the CI runners too (which are mostly Linux).
pytestmark = pytest.mark.usefixtures("_force_posix_ps_branch")


@pytest.fixture
def _force_posix_ps_branch(monkeypatch):
    """Route _get_rss_tree_mb down the ``ps`` branch regardless of host OS."""
    monkeypatch.setattr(rt.sys, "platform", "darwin")
    monkeypatch.setattr(rt.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(rt.platform_compat, "trusted_system_bin", lambda name: f"/bin/{name}")
    rt._reset_ps_table_cache()
    yield
    rt._reset_ps_table_cache()


#: pid ppid rss(KiB) — 100 is a runtime with one child, 200 an unrelated runtime.
_PS_OUTPUT = b"\n".join(
    [
        b"  1     0   1024",
        b"100     1   2048",
        b"101   100   1024",
        b"200     1   4096",
    ]
)


def _counting_ps(monkeypatch, output: bytes = _PS_OUTPUT) -> list[list[str]]:
    """Replace subprocess.check_output and record every argv it is handed."""
    calls: list[list[str]] = []

    def fake(argv, *a, **kw):
        calls.append(list(argv))
        return output

    monkeypatch.setattr(rt.subprocess, "check_output", fake)
    return calls


def test_many_pids_share_one_process_table_snapshot(monkeypatch):
    """N pids must cost ONE ps walk, not N.

    This is the actual regression. With the per-pid snapshot restored this
    asserts 1 and gets 3.
    """
    calls = _counting_ps(monkeypatch)

    for pid in (100, 101, 200):
        assert rt._get_rss_tree_mb(pid) is not None

    assert len(calls) == 1, f"expected one ps snapshot for three pids, got {len(calls)}"
    assert calls[0][1:] == ["-Ao", "pid=,ppid=,rss="]


def test_tree_sum_still_includes_descendants(monkeypatch):
    """Sharing the snapshot must not change the ANSWER.

    pid 100 owns child 101, so its tree is 2048+1024 KiB. Measuring only the
    root would report 2.0 — which is what the stale docstring's "on macOS the
    tree is just the process itself" claim would have licensed, and what would
    blind the watchdog to an MCP-child leak.
    """
    _counting_ps(monkeypatch)

    assert rt._get_rss_tree_mb(100) == pytest.approx(3072 / 1024.0)
    # A leaf is just itself, and an unrelated root does not absorb the tree above.
    assert rt._get_rss_tree_mb(101) == pytest.approx(1024 / 1024.0)
    assert rt._get_rss_tree_mb(200) == pytest.approx(4096 / 1024.0)


def test_cache_expiry_takes_a_fresh_snapshot(monkeypatch):
    """The cache is a short TTL, not a permanent freeze — RSS must still move."""
    calls = _counting_ps(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])

    rt._get_rss_tree_mb(100)
    assert len(calls) == 1

    clock["t"] += rt._PS_TABLE_TTL_S / 2
    rt._get_rss_tree_mb(100)
    assert len(calls) == 1, "within the TTL the snapshot must be reused"

    clock["t"] += rt._PS_TABLE_TTL_S
    rt._get_rss_tree_mb(100)
    assert len(calls) == 2, "past the TTL a fresh snapshot must be taken"


def test_ps_failure_is_not_cached(monkeypatch):
    """A transient ps failure must not pin callers to the fallback for a whole
    TTL — otherwise one blip degrades every reading in the window."""
    outcomes = {"fail": True}

    def flaky(argv, *a, **kw):
        if outcomes["fail"]:
            raise subprocess.SubprocessError("boom")
        return _PS_OUTPUT

    monkeypatch.setattr(rt.subprocess, "check_output", flaky)
    assert rt._ps_process_table() is None

    outcomes["fail"] = False
    assert rt._ps_process_table() is not None


def test_missing_ps_binary_falls_back_to_single_pid(monkeypatch):
    """No usable ps → single-pid read, never a phantom-empty tree."""
    monkeypatch.setattr(rt.platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(rt, "_get_rss_mb", lambda pid: 7.5)

    assert rt._get_rss_tree_mb(100) == 7.5


def test_linux_branch_spawns_nothing(monkeypatch):
    """The Linux path must stay pure /proc — it is why Linux never showed this
    bug, and routing it through ps would import the problem."""
    monkeypatch.setattr(rt.sys, "platform", "linux")
    calls = _counting_ps(monkeypatch)
    monkeypatch.setattr(rt, "_iter_descendant_pids", lambda pid: [pid])
    monkeypatch.setattr(rt, "_get_rss_mb", lambda pid: 5.0)

    assert rt._get_rss_tree_mb(100) == 5.0
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps branch")
def test_sampler_pass_over_many_pids_takes_one_snapshot(monkeypatch):
    """End-to-end at the layer that regressed: the dashboard sampler.

    ``_blocking_sample`` is what fans out over every live runtime pid, so the
    guarantee has to hold there and not merely in isolation.
    """
    from kiro_crew.dashboard import session_memory as sm

    calls = _counting_ps(monkeypatch)
    monkeypatch.setattr(sm, "_iter_descendant_pids", lambda pid: [pid])
    monkeypatch.setattr(sm, "_unattributed", lambda owned, self_pid: None)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.usage.slot_spend", lambda: {})

    sampler = sm.SessionMemorySampler()
    rows = [{"pid": 100}, {"pid": 101}, {"pid": 200}]
    out = sampler._blocking_sample(rows)

    assert len(out["per_pid"]) == 3
    assert len(calls) == 1, f"one ps snapshot per sampling pass, got {len(calls)}"
