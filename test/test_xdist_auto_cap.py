"""Tests for the memory-aware pytest-xdist ``-n auto`` cap.

Covers :func:`kiro_crew.resource_status.compute_xdist_auto_workers` (clamping),
:func:`kiro_crew.resource_status.inject_xdist_auto_cap` (config modes,
respect-existing-env, fail-open on an unreadable probe), and the presence of
the injection in the environment built by the ACP client spawn path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import resource_status as rs

# ── compute_xdist_auto_workers ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "available_gb,cpu_count,expected",
    [
        # CPU-bound: plenty of memory → the CPU count is the ceiling.
        (64.0, 8, 8),
        # Memory-bound: 16 CPUs but 8 GB free → floor(8 * 0.5 / 1.0) = 4.
        (8.0, 16, 4),
        # The observed incident shape: 16 CPUs, ~12 GB free → 6, not 16.
        (12.0, 16, 6),
        # Low-memory floor: never below 1, even with (near-)zero memory.
        (0.5, 16, 1),
        (0.0, 16, 1),
        # Fractional result truncates down (floor): 5 GB * 0.5 = 2.5 → 2.
        (5.0, 16, 2),
        # Degenerate CPU counts clamp to at least 1.
        (64.0, 0, 1),
        (64.0, -3, 1),
    ],
)
def test_compute_clamping(available_gb: float, cpu_count: int, expected: int) -> None:
    assert rs.compute_xdist_auto_workers(available_gb, cpu_count) == expected


def test_compute_respects_overrides() -> None:
    # 2 GB per worker halves the memory-bound count relative to the default.
    assert rs.compute_xdist_auto_workers(8.0, 16, per_worker_gb=2.0) == 2
    # A full share doubles it.
    assert rs.compute_xdist_auto_workers(8.0, 16, share=1.0) == 8


# ── _xdist_cap_config ────────────────────────────────────────────────────────


def _with_raw_config(raw: dict):
    return patch("kiro_crew.config.loader._raw_config", return_value=raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, -1),  # unset → auto
        ({"resource_limits": {}}, -1),
        ({"resource_limits": {"xdist_auto_cap": -1}}, -1),
        ({"resource_limits": {"xdist_auto_cap": 0}}, 0),  # disabled
        ({"resource_limits": {"xdist_auto_cap": 6}}, 6),  # fixed
        ({"resource_limits": {"xdist_auto_cap": -5}}, -1),  # junk → default
        ({"resource_limits": {"xdist_auto_cap": True}}, -1),  # bool is junk
        ({"resource_limits": {"xdist_auto_cap": "8"}}, -1),  # str is junk
        ({"resource_limits": "nope"}, -1),  # non-dict block
    ],
)
def test_cap_config_modes(raw: dict, expected: int) -> None:
    with _with_raw_config(raw):
        assert rs._xdist_cap_config() == expected


# ── inject_xdist_auto_cap ────────────────────────────────────────────────────


def test_inject_respects_existing_env() -> None:
    env = {rs.XDIST_AUTO_ENV: "3"}
    with _with_raw_config({"resource_limits": {"xdist_auto_cap": 9}}):
        rs.inject_xdist_auto_cap(env)
    assert env[rs.XDIST_AUTO_ENV] == "3"  # operator/user value wins


def test_inject_disabled_injects_nothing() -> None:
    env: dict[str, str] = {}
    with _with_raw_config({"resource_limits": {"xdist_auto_cap": 0}}):
        rs.inject_xdist_auto_cap(env)
    assert rs.XDIST_AUTO_ENV not in env


def test_inject_fixed_cap() -> None:
    env: dict[str, str] = {}
    with _with_raw_config({"resource_limits": {"xdist_auto_cap": 6}}):
        rs.inject_xdist_auto_cap(env)
    assert env[rs.XDIST_AUTO_ENV] == "6"


def test_inject_auto_computes_from_probe() -> None:
    env: dict[str, str] = {}
    with (
        _with_raw_config({}),
        patch.object(rs, "_read_available_gb", return_value=8.0),
        patch("os.cpu_count", return_value=16),
    ):
        rs.inject_xdist_auto_cap(env)
    assert env[rs.XDIST_AUTO_ENV] == "4"  # floor(8 * 0.5 / 1.0)


def test_inject_auto_fails_open_when_probe_unavailable() -> None:
    env: dict[str, str] = {}
    with (
        _with_raw_config({}),
        patch.object(rs, "_read_available_gb", return_value=-1.0),
    ):
        rs.inject_xdist_auto_cap(env)
    assert rs.XDIST_AUTO_ENV not in env  # leave xdist's own default


def test_inject_auto_low_memory_floors_at_one_worker() -> None:
    env: dict[str, str] = {}
    with (
        _with_raw_config({}),
        patch.object(rs, "_read_available_gb", return_value=0.2),
        patch("os.cpu_count", return_value=16),
    ):
        rs.inject_xdist_auto_cap(env)
    assert env[rs.XDIST_AUTO_ENV] == "1"


# ── injection presence in the built spawn env (ACP client path) ─────────────


@pytest.mark.asyncio
async def test_spawn_env_carries_xdist_cap(tmp_path, monkeypatch) -> None:
    """The env handed to the agent subprocess carries the computed cap."""
    from kiro_crew.acp.client import AcpClient

    monkeypatch.delenv(rs.XDIST_AUTO_ENV, raising=False)
    client = AcpClient(work_dir=tmp_path, session_key="k")
    with (
        patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
        patch(
            "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
        _with_raw_config({}),
        patch.object(rs, "_read_available_gb", return_value=8.0),
        patch("os.cpu_count", return_value=16),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc

        await client._spawn()

        call_kwargs = mock_exec.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is not None
        assert env[rs.XDIST_AUTO_ENV] == "4"


@pytest.mark.asyncio
async def test_spawn_env_leaves_preset_xdist_cap_alone(tmp_path, monkeypatch) -> None:
    """A value inherited from the gateway environment is never overridden."""
    from kiro_crew.acp.client import AcpClient

    monkeypatch.setenv(rs.XDIST_AUTO_ENV, "2")
    client = AcpClient(work_dir=tmp_path, session_key="k")
    with (
        patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
        patch(
            "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
        ),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
        _with_raw_config({"resource_limits": {"xdist_auto_cap": 9}}),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc

        await client._spawn()

        call_kwargs = mock_exec.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is not None
        assert env[rs.XDIST_AUTO_ENV] == "2"
