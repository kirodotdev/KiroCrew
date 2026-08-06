"""Tests for the xdist host worker budget in ``test/conftest.py``.

The budget exists because two worktrees each running ``pytest -n auto`` on the
same 10-core host took 10 workers *each*, swapped the machine to a load average
of ~590, and completed zero tests in 21 minutes while xdist silently cloned
replacement workers.

Capacity is a set of advisory locks held for the run's lifetime, so the property
these tests care most about is that a dead holder's share comes back with no
cleanup logic -- the orphaned-run case that caused the incident.
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import textwrap

import pytest

import conftest as ct

needs_symlinks = pytest.mark.skipif(
    os.name != "posix", reason="symlink creation needs privileges on Windows"
)


@pytest.fixture
def slot_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point the budget at a throwaway slot dir and isolate held locks.

    Returns the HOST-SCOPED leaf, which is what the budget actually uses; the
    env var names the root above it. Any fds a test acquires are closed on
    teardown, so a test can never hold real capacity for the rest of the run.
    """
    root = tmp_path / "slots"
    monkeypatch.setenv(ct._SLOT_DIR_ENV, str(root))
    held: list[int] = []
    monkeypatch.setattr(ct, "_held_slots", held)
    yield root / ct._host_key()
    for fd in held:
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.fixture
def budget_host(slot_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A deterministic 10-core / 32 GiB host, so only contention varies."""
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    monkeypatch.setattr(ct, "_host_total_gib", lambda: 32)
    monkeypatch.delenv(ct._MAX_WORKERS_ENV, raising=False)
    return slot_dir


def _hold_slots_in_subprocess(slot_dir: pathlib.Path, count: int) -> subprocess.Popen[str]:
    """Start a child holding ``count`` slot locks; it exits when stdin closes."""
    code = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(pathlib.Path(ct.__file__).parent)!r})
        sys.path.insert(0, {str(pathlib.Path(ct.__file__).parent.parent / "src")!r})
        from kiro_crew import platform_compat
        held = []
        for i in range({count}):
            fd = os.open(os.path.join({str(slot_dir)!r}, "worker-%03d.lock" % i),
                         os.O_CREAT | os.O_RDWR, 0o600)
            assert platform_compat.try_acquire_lock(fd, exclusive=True), i
            held.append(fd)
        print("ready", flush=True)
        sys.stdin.read()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


# ── slot directory resolution ──────────────────────────────────────────


def test_slot_dir_is_scoped_by_host(slot_dir: pathlib.Path) -> None:
    assert ct._slot_dir() == slot_dir
    assert slot_dir.name == ct._host_key()


def test_slot_root_defaults_under_cache_not_kirocrew_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must be host-global: two worktrees with different homes still coordinate."""
    monkeypatch.delenv(ct._SLOT_DIR_ENV, raising=False)
    monkeypatch.setenv("KIROCREW_HOME", "/tmp/some-unrelated-home")
    resolved = ct._slot_root()
    assert resolved == pathlib.Path.home() / ".cache" / "kirocrew" / "test-slots"
    assert "some-unrelated-home" not in str(resolved)


def test_slot_dir_separates_hosts_sharing_a_network_home(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared ~/.cache must not mix contention across machines."""
    root = tmp_path / "shared-home"
    monkeypatch.setenv(ct._SLOT_DIR_ENV, str(root))

    monkeypatch.setattr(socket, "gethostname", lambda: "build-host-a")
    dir_a = ct._slot_dir()
    monkeypatch.setattr(socket, "gethostname", lambda: "build-host-b")
    dir_b = ct._slot_dir()

    assert dir_a != dir_b
    assert dir_a.parent == dir_b.parent == root


@pytest.mark.parametrize("raw", ["host-1.example.com", "", "../../etc", "a/b", "  ", "."])
def test_host_key_is_a_safe_single_path_segment(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: raw)
    key = ct._host_key()

    assert key, "must never be empty -- that would collapse to the root dir"
    assert "/" not in key and os.sep not in key
    assert key not in (".", "..")
    base = pathlib.Path(os.sep) / "tmp" / "base"
    assert str((base / key).resolve()).startswith(str(base.resolve()))


def test_slot_path_is_zero_padded(tmp_path: pathlib.Path) -> None:
    assert ct._slot_path(tmp_path, 7).name == "worker-007.lock"
    assert ct._slot_path(tmp_path, 31).name == "worker-031.lock"


# ── memory term ────────────────────────────────────────────────────────


def test_host_total_gib_converts_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_PHYS_PAGES": 2097152, "SC_PAGE_SIZE": 16384}[name],
        raising=False,
    )
    assert ct._host_total_gib() == 32


@pytest.mark.parametrize("pages,size", [(0, 4096), (100, 0), (-1, 4096)])
def test_host_total_gib_rejects_nonsense(
    monkeypatch: pytest.MonkeyPatch, pages: int, size: int
) -> None:
    monkeypatch.setattr(
        os,
        "sysconf",
        lambda name: {"SC_PHYS_PAGES": pages, "SC_PAGE_SIZE": size}[name],
        raising=False,
    )
    assert ct._host_total_gib() == 0


def test_host_total_gib_survives_unsupported_sysconf(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(name: str) -> int:
        raise ValueError(name)

    monkeypatch.setattr(os, "sysconf", _boom, raising=False)
    assert ct._host_total_gib() == 0


# ── claiming ───────────────────────────────────────────────────────────


def test_claim_alone_takes_the_ceiling(slot_dir: pathlib.Path) -> None:
    assert ct._claim_worker_slots(6, 64) == 6
    assert len(ct._held_slots) == 6
    assert sorted(p.name for p in slot_dir.iterdir()) == [
        f"worker-{i:03d}.lock" for i in range(6)
    ]


def test_claim_respects_the_per_run_cap(slot_dir: pathlib.Path) -> None:
    """Capacity is what exists; cap is what one run may take."""
    assert ct._claim_worker_slots(64, 32) == 32
    assert len(ct._held_slots) == 32


def test_big_host_lets_a_second_run_use_the_free_half(slot_dir: pathlib.Path) -> None:
    """Regression: probing only `cap` slots starved a second run on a big host.

    With 64 cores and a cap of 32, run A takes slots 0-31. Probing only 32 slots
    made run B find them all locked and collapse to one worker while half the
    machine sat idle -- worse than the pre-budget behaviour, where both runs got
    32 and coexisted.
    """
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(slot_dir, 32)
    try:
        assert ct._claim_worker_slots(64, 32) == 32
    finally:
        holder.communicate()


def test_floor_applies_only_when_the_host_is_really_full(slot_dir: pathlib.Path) -> None:
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(slot_dir, 10)
    try:
        assert ct._claim_worker_slots(10, 32) == 1
    finally:
        holder.communicate()


def test_claim_takes_only_unlocked_capacity(slot_dir: pathlib.Path) -> None:
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(slot_dir, 4)
    try:
        assert ct._claim_worker_slots(10, 64) == 6
    finally:
        holder.communicate()


def test_claim_never_returns_zero_when_host_is_full(slot_dir: pathlib.Path) -> None:
    """Floor of 1 -- a late run is slow, never stalled."""
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(slot_dir, 4)
    try:
        assert ct._claim_worker_slots(4, 64) == 1
        assert ct._held_slots == []  # took nothing, but still runs
    finally:
        holder.communicate()


def test_dead_holder_releases_its_share_with_no_cleanup(slot_dir: pathlib.Path) -> None:
    """THE property: an orphaned or terminated run frees capacity by dying.

    This is the incident case -- both wedged runs were reparented to init with
    nobody reading their results. The kernel drops their locks; no pruning, no
    PID probing, no staleness heuristics.
    """
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(slot_dir, 10)
    assert ct._claim_worker_slots(10, 64) == 1  # fully contended while it lives

    for fd in ct._held_slots:
        os.close(fd)
    ct._held_slots.clear()
    holder.communicate()  # closing stdin ends the child
    assert holder.wait() == 0

    assert ct._claim_worker_slots(10, 64) == 10  # capacity is back


def test_claim_is_idempotent_within_a_process(slot_dir: pathlib.Path) -> None:
    """A second call returns the same share instead of collapsing to one worker.

    flock treats two fds on one file as independent even in the same process, so
    a naive re-claim would take nothing.
    """
    assert ct._claim_worker_slots(3, 64) == 3
    assert ct._claim_worker_slots(3, 64) == 3
    assert len(ct._held_slots) == 3


@needs_symlinks
def test_claim_refuses_symlinked_slot_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink at either level could redirect our writes."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv(ct._SLOT_DIR_ENV, str(link))
    monkeypatch.setattr(ct, "_held_slots", [])

    assert ct._claim_worker_slots(10, 64) == 10  # fails open to unbudgeted
    assert list(real.iterdir()) == []
    assert ct._held_slots == []


@needs_symlinks
def test_claim_refuses_symlinked_host_leaf(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ct._host_key()).symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv(ct._SLOT_DIR_ENV, str(root))
    monkeypatch.setattr(ct, "_held_slots", [])

    assert ct._claim_worker_slots(10, 64) == 10
    assert list(elsewhere.iterdir()) == []
    assert ct._held_slots == []


@pytest.mark.parametrize("boom", [RuntimeError, OSError, ValueError])
def test_claim_fails_open_when_path_resolution_raises(
    monkeypatch: pytest.MonkeyPatch, boom: type[Exception]
) -> None:
    """Resolution must not break pytest startup.

    Regression: the slot path was resolved OUTSIDE the guard, so an
    unresolvable home (``Path.home()`` -> RuntimeError) or a failing
    ``gethostname()`` propagated out of the hook instead of failing open.
    """
    monkeypatch.delenv(ct._SLOT_DIR_ENV, raising=False)
    monkeypatch.setattr(ct, "_held_slots", [])

    def _raise() -> pathlib.Path:
        raise boom("nope")

    monkeypatch.setattr(pathlib.Path, "home", staticmethod(_raise))

    assert ct._claim_worker_slots(10, 4) == 4  # min(capacity, cap)
    assert ct._held_slots == []


def test_claim_fails_open_when_hostname_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ct, "_held_slots", [])

    def _raise() -> str:
        raise OSError("no hostname")

    monkeypatch.setattr(socket, "gethostname", _raise)

    assert ct._claim_worker_slots(10, 6) == 6
    assert ct._held_slots == []


def test_claim_fails_open_when_the_dir_cannot_be_made(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bookkeeping trouble must never block a test run."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv(ct._SLOT_DIR_ENV, str(blocker))
    monkeypatch.setattr(ct, "_held_slots", [])

    assert ct._claim_worker_slots(8, 64) == 8
    assert ct._held_slots == []


# ── the hook ───────────────────────────────────────────────────────────


def test_alone_gets_the_whole_machine(budget_host: pathlib.Path) -> None:
    """The speed guarantee: testing alone is unchanged by this budget."""
    assert ct.pytest_xdist_auto_num_workers(None) == 10


def test_second_run_takes_what_is_left(budget_host: pathlib.Path) -> None:
    budget_host.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(budget_host, 7)
    try:
        assert ct.pytest_xdist_auto_num_workers(None) == 3
    finally:
        holder.communicate()


def test_memory_binds_before_cores(budget_host: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """8 GiB cannot back 10 workers, whatever the core count says."""
    monkeypatch.setattr(ct, "_host_total_gib", lambda: 8)
    assert ct.pytest_xdist_auto_num_workers(None) == 8 // ct._GIB_PER_WORKER


def test_unknown_memory_falls_back_to_cores(
    budget_host: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ct, "_host_total_gib", lambda: 0)
    assert ct.pytest_xdist_auto_num_workers(None) == 10


def test_env_cap_lowers_the_ceiling(budget_host: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ct._MAX_WORKERS_ENV, "3")
    assert ct.pytest_xdist_auto_num_workers(None) == 3


def test_garbage_env_cap_falls_back_to_default(
    budget_host: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ct._MAX_WORKERS_ENV, "not-a-number")
    assert ct.pytest_xdist_auto_num_workers(None) == 10


def test_big_host_still_capped_at_default(
    slot_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original 64-core regression finding stays enforced."""
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(ct, "_host_total_gib", lambda: 512)
    monkeypatch.delenv(ct._MAX_WORKERS_ENV, raising=False)
    assert ct.pytest_xdist_auto_num_workers(None) == ct._DEFAULT_WORKER_CAP


def test_two_runs_on_a_big_host_both_get_the_cap(
    slot_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End of the same regression, seen through the hook."""
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(ct, "_host_total_gib", lambda: 512)
    monkeypatch.delenv(ct._MAX_WORKERS_ENV, raising=False)
    slot_dir.mkdir(parents=True, exist_ok=True)
    holder = _hold_slots_in_subprocess(slot_dir, ct._DEFAULT_WORKER_CAP)
    try:
        assert ct.pytest_xdist_auto_num_workers(None) == ct._DEFAULT_WORKER_CAP
    finally:
        holder.communicate()


# ── release ────────────────────────────────────────────────────────────


def test_sessionfinish_releases_every_slot(slot_dir: pathlib.Path) -> None:
    ct._claim_worker_slots(5, 64)
    assert len(ct._held_slots) == 5

    ct.pytest_sessionfinish(None, 0)

    assert ct._held_slots == []


def test_sessionfinish_is_a_noop_without_slots(slot_dir: pathlib.Path) -> None:
    """xdist workers also run this hook and hold nothing."""
    assert ct._held_slots == []
    ct.pytest_sessionfinish(None, 0)
    assert ct._held_slots == []


def test_released_capacity_is_reusable(slot_dir: pathlib.Path) -> None:
    assert ct._claim_worker_slots(4, 64) == 4
    ct.pytest_sessionfinish(None, 0)
    assert ct._claim_worker_slots(4, 64) == 4
