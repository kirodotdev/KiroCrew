"""PPTX Maker's two spawn sites hand the spawn exactly ONE ``systemd-run --scope`` layer.

``sandboxed_spawn_argv`` applies ``cgroup_scope_argv`` as its outermost layer, so a
call site that wraps its result again nests a second ``systemd-run --user --scope``,
which newer systemd refuses, and the command never runs. The app's other spawn tests
mock the subprocess, so only an argv-level check sees the layer count. The core
pins the chokepoint itself, and sweeps ``src/kiro_crew`` for a double wrap, in
``test/test_cgroup_scope_single_layer_ratchet.py``.

Both sites are driven through the real chokepoint with the cgroup backend forced
available and the host-dependent OS-sandbox layer passed through, so this runs
identically on Linux, macOS and Windows.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.apps.builtins.pptx_maker.backend import engine, provision

_SYSTEMD_RUN = "/usr/bin/systemd-run"


@pytest.fixture
def real_chokepoint_with_a_cgroup_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The REAL ``sandboxed_spawn_argv``, with ``cgroup_scope_argv`` forced to wrap.

    The probe, the trusted ``systemd-run`` resolution, the slice reconcile and the
    limits are replaced, and so is the namespace/seatbelt layer, which adds no scope.
    Everything after it, including the chokepoint's own outermost scope, runs as
    shipped, so a second wrap at the call site goes red, and so does the chokepoint
    dropping its own.
    """
    monkeypatch.setattr(sb, "_CGROUP_SCOPE_PROBE", None)
    monkeypatch.setattr(sb, "_CGROUP_WARNED", False)
    monkeypatch.setattr(sb, "_probe_cgroup_scope", lambda: (True, "ok"))
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_system_bin", lambda name: _SYSTEMD_RUN)
    monkeypatch.setattr(sb, "_reconcile_slice_memory_high_off_thread", lambda *a, **k: None)
    monkeypatch.setattr(sb, "_cgroup_limits_from_config", lambda _rl=None: (8192, 8192, 50, 0))
    monkeypatch.setattr(sb, "_cpu_controller_delegated", lambda: False)
    monkeypatch.setattr(sb, "wrap_argv", lambda argv, **_kw: (list(argv), None))


def _scope_layers(argv: list[str]) -> int:
    return sum(1 for a in argv if a == _SYSTEMD_RUN)


def test_the_fixture_really_wraps(real_chokepoint_with_a_cgroup_backend) -> None:
    """Guard the guard: with the backend forced, one wrap is one layer."""
    assert _scope_layers(sb.cgroup_scope_argv(["/bin/true"])) == 1


def test_provision_run_hands_one_scope_layer_to_the_spawn(
    real_chokepoint_with_a_cgroup_backend, tmp_path: Path
) -> None:
    with mock.patch.object(provision, "run_limited") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        code, _ = provision._run(["/opt/uv", "sync"], cwd=str(tmp_path), timeout=5)
    spawned = run.call_args.args[0]
    assert code == 0
    assert spawned[0] == _SYSTEMD_RUN
    assert _scope_layers(spawned) == 1, spawned
    assert spawned[-2:] == ["/opt/uv", "sync"]


def test_engine_spawn_hands_one_scope_layer_to_the_spawn(
    real_chokepoint_with_a_cgroup_backend, tmp_path: Path
) -> None:
    with mock.patch.object(engine, "run_limited") as run:
        run.return_value = mock.Mock(returncode=0, stdout="{}", stderr="")
        result = engine._spawn(["/venv/python", "-c", "1"], cwd=str(tmp_path), timeout=5)
    spawned = run.call_args.args[0]
    assert result.returncode == 0
    assert spawned[0] == _SYSTEMD_RUN
    assert _scope_layers(spawned) == 1, spawned
    assert spawned[-3:] == ["/venv/python", "-c", "1"]
