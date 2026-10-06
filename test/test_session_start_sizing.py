"""``agent.session_start_concurrency = "auto"``: host sizing of the SessionStartGate.

The sizing is ``clamp(min(cpus // 4, available_GB // 3), 2, 16)`` with cpus the
affinity count capped by a cgroup v2 ``cpu.max`` quota. An explicit integer in
config still wins, clamped to 1..64 as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import session_start_sizing as sss
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture(autouse=True)
def _fresh_cache():
    sss._reset_for_tests()
    yield
    sss._reset_for_tests()


# ── pure sizing table ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cpus, gb, expected",
    [
        pytest.param(8, 64.0, 2, id="8-core-laptop"),
        pytest.param(16, 64.0, 4, id="16-core"),
        pytest.param(32, 128.0, 8, id="32-core"),
        pytest.param(64, 256.0, 16, id="64-core"),
        pytest.param(128, 1000.0, 16, id="128-core-capped"),
        pytest.param(128, 14.0, 4, id="memory-limited"),
        pytest.param(64, 2.0, 2, id="memory-starved-floor"),
        pytest.param(4, 64.0, 2, id="tiny-host-floor"),
        pytest.param(32, None, 8, id="memory-unknown-uses-cpu"),
        pytest.param(32, -1.0, 8, id="memory-unreadable-uses-cpu"),
        pytest.param(None, 30.0, 10, id="cpu-unknown-uses-memory"),
        pytest.param(None, None, 2, id="nothing-known-floor"),
    ],
)
def test_size_table(cpus, gb, expected):
    assert sss.size_session_start_concurrency(cpus, gb) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("max 100000\n", None),
        ("200000 100000\n", 2.0),
        ("250000 100000", 2.5),
        ("50000", 0.5),
        ("garbage", None),
        ("", None),
        ("0 100000", None),
    ],
)
def test_parse_cpu_max(text, expected):
    assert sss.parse_cpu_max(text) == expected


def _cgroup_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel / "cpu.max" if rel else root / "cpu.max"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_cgroup_quota_takes_tightest_ancestor(tmp_path: Path):
    _cgroup_tree(
        tmp_path,
        {
            "system.slice": "800000 100000",
            "system.slice/kirocrew.service": "max 100000",
            "system.slice/kirocrew.service/inner": "max 100000",
        },
    )
    quota = sss.cgroup_cpu_quota("0::/system.slice/kirocrew.service/inner\n", root=tmp_path)
    assert quota == 8.0


def test_cgroup_quota_container_namespace_root(tmp_path: Path):
    """Under cgroupns (docker --cpus) the quota sits on the namespace root itself."""
    _cgroup_tree(tmp_path, {"": "300000 100000"})
    assert sss.cgroup_cpu_quota("0::/\n", root=tmp_path) == 3.0


def test_cgroup_quota_absent_or_v1(tmp_path: Path):
    _cgroup_tree(tmp_path, {"user.slice": "max 100000"})
    assert sss.cgroup_cpu_quota("0::/user.slice\n", root=tmp_path) is None
    assert sss.cgroup_cpu_quota("4:cpu,cpuacct:/docker/abc\n", root=tmp_path) is None
    assert sss.cgroup_cpu_quota("0::/../escape\n", root=tmp_path) is None


@pytest.mark.parametrize(
    "affinity, quota, expected",
    [
        (128, None, 128),
        (128, 8.0, 8),
        (128, 2.5, 2),
        (4, 8.0, 4),
        (128, 0.5, 1),
        (None, 6.0, 6),
        (None, None, None),
    ],
)
def test_effective_cpus(affinity, quota, expected):
    assert sss.effective_cpus(affinity, quota) == expected


def test_cpu_max_quota_caps_a_big_host(monkeypatch):
    """128 visible cores under ``--cpus=8``: sized as an 8-core host, not 128."""
    monkeypatch.setattr(sss, "affinity_cpu_count", lambda: 128)
    monkeypatch.setattr(sss, "cgroup_cpu_quota", lambda: 8.0)
    monkeypatch.setattr(sss, "_read_available_gb", lambda: 500.0)
    sizing = sss.resolve_session_start_sizing("auto")
    assert sizing.host is not None
    assert (sizing.limit, sizing.host.cpus, sizing.host.quota_cpus) == (2, 8, 8.0)
    assert "cpu.max=8" in sizing.describe()


# ── resolution: auto is probed once, explicit ints win ───────────────────────


def test_auto_is_resolved_once_per_process(monkeypatch):
    calls: list[int] = []

    def _probe():
        calls.append(1)
        return sss.HostCapacity(affinity_cpus=32, quota_cpus=None, cpus=32, available_gb=100.0)

    monkeypatch.setattr(sss, "probe_host_capacity", _probe)
    assert sss.effective_session_start_concurrency("auto") == 8
    assert sss.effective_session_start_concurrency("AUTO") == 8
    assert sss.host_capacity().cpus == 32
    assert len(calls) == 1


def test_explicit_integer_wins(monkeypatch):
    monkeypatch.setattr(sss, "probe_host_capacity", lambda: pytest.fail("must not probe"))
    sizing = sss.resolve_session_start_sizing(5)
    assert (sizing.limit, sizing.source) == (5, "config")
    assert sizing.describe() == "5 (config)"


# ── config loading ───────────────────────────────────────────────────────────


def _load_with(tmp_path: Path, monkeypatch, agent: dict | None) -> KiroCrewConfig:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({} if agent is None else {"agent": agent}))
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
    return KiroCrewConfig.load()


def test_default_is_auto():
    assert KiroCrewConfig().agent.session_start_concurrency == "auto"


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param(None, "auto", id="absent"),
        pytest.param("auto", "auto", id="auto"),
        pytest.param("Auto", "auto", id="auto-any-case"),
        pytest.param(8, 8, id="explicit-int"),
        pytest.param("8", 8, id="numeric-string"),
        pytest.param(0, 1, id="below-range-clamped"),
        pytest.param(500, 64, id="above-range-clamped"),
        pytest.param("fast", "auto", id="junk-falls-back-to-default"),
        pytest.param(True, "auto", id="bool-falls-back-to-default"),
    ],
)
def test_loader_accepts_auto_or_int(tmp_path, monkeypatch, raw, expected):
    agent = {} if raw is None else {"session_start_concurrency": raw}
    cfg = _load_with(tmp_path, monkeypatch, agent)
    assert cfg.agent.session_start_concurrency == expected


def test_schema_accepts_integer_and_string():
    from kiro_crew.config.schema import build_json_schema

    node = build_json_schema(KiroCrewConfig)["properties"]["agent"]["properties"][
        "session_start_concurrency"
    ]
    assert node["type"] == ["integer", "string"]
    assert node["default"] == "auto"


# ── consumers ────────────────────────────────────────────────────────────────


def test_runtime_resolver_resolves_auto(monkeypatch):
    from kiro_crew.acp import runtime as acp_runtime

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(
            lambda cls, *a, **k: SimpleNamespace(
                agent=SimpleNamespace(session_start_concurrency="auto")
            )
        ),
    )
    monkeypatch.setattr(
        sss,
        "probe_host_capacity",
        lambda: sss.HostCapacity(affinity_cpus=48, quota_cpus=None, cpus=48, available_gb=None),
    )
    assert acp_runtime._resolve_session_start_concurrency() == 12


@pytest.mark.asyncio
async def test_gate_snapshot_auto_is_resolved_off_loop(monkeypatch):
    """An ``auto`` live snapshot sizes the gate from the host, not as int("auto")."""
    from kiro_crew.acp import runtime as acp_runtime
    from kiro_crew.acp import runtime_start

    monkeypatch.setattr(
        runtime_start.live,
        "snapshot",
        lambda: SimpleNamespace(agent=SimpleNamespace(session_start_concurrency="auto")),
    )
    monkeypatch.setattr(acp_runtime, "_resolve_session_start_concurrency", lambda: 11)
    runtime_start._session_start_gates.clear()
    try:
        gate = await runtime_start.session_start_gate()
        assert gate.limit == 11
    finally:
        runtime_start._session_start_gates.clear()


def test_doctor_shows_resolved_auto_value_and_inputs(monkeypatch, capsys):
    from kiro_crew import cli_doctor

    monkeypatch.setattr(sss, "affinity_cpu_count", lambda: 64)
    monkeypatch.setattr(sss, "cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(sss, "_read_available_gb", lambda: 200.0)
    cfg = KiroCrewConfig()
    cli_doctor._doctor_overload_resilience(cfg)
    out = capsys.readouterr().out
    assert "session_start_concurrency=16 (auto: cpus=64" in out
    assert "available=200.0GB" in out
    assert "[probed now; running gateway: boot log]" in out


def test_doctor_explicit_value_is_not_labelled_as_a_probe(capsys):
    from kiro_crew import cli_doctor

    cfg = KiroCrewConfig()
    cfg.agent.session_start_concurrency = 3
    cli_doctor._doctor_overload_resilience(cfg)
    out = capsys.readouterr().out
    assert "session_start_concurrency=3 (config) " in out
    assert "probed now" not in out


@pytest.mark.asyncio
async def test_gateway_boot_sizes_auto_off_the_event_loop(monkeypatch, caplog):
    """The boot path probes the host in a worker thread and logs the value in force."""
    import asyncio
    import logging
    import threading

    from kiro_crew.slack.gateway import GatewayOrchestrator

    loop_thread = threading.get_ident()
    probed_on: list[int] = []

    def _probe():
        probed_on.append(threading.get_ident())
        return sss.HostCapacity(affinity_cpus=64, quota_cpus=None, cpus=64, available_gb=200.0)

    monkeypatch.setattr(sss, "probe_host_capacity", _probe)
    orch = SimpleNamespace(_cfg=KiroCrewConfig())
    with caplog.at_level(logging.INFO, logger="kiro_crew.slack.gateway"):
        await GatewayOrchestrator._log_session_start_sizing(orch)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert probed_on and probed_on[0] != loop_thread
    assert any("Session start concurrency: 16 (auto:" in r.getMessage() for r in caplog.records)
    # The manager and the gate read the cached value: no second probe.
    assert sss.effective_session_start_concurrency("auto") == 16
    assert len(probed_on) == 1


def test_gateway_boot_sizes_only_after_the_socket_binds():
    """AUTOSDE no-new-work-on-gateway-boot-path: no host probe before the bind.

    ``run()`` must not await the sizing; it schedules it after
    ``_init_dashboard()`` / ``_init_api_server()`` have bound the socket.
    """
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator.run)
    assert "await self._log_session_start_sizing()" not in src
    schedule_at = src.index("self._schedule_session_start_sizing()")
    assert schedule_at > src.index("await self._init_dashboard()")
    assert schedule_at > src.index("await self._init_api_server()")


def test_subagent_manager_never_probes_the_host(monkeypatch):
    """Built pre-bind and read on the loop: the manager only reads the cached host reading.

    Before the gateway's post-bind sizing publishes a reading, ``auto`` uses
    the floor; after it, the host-sized width. Neither path probes, and
    ``_startup_cap`` never waits on ``_host_lock`` (a worker holds it while it
    probes).
    """
    from overload_fakes import mock_ctx, mock_sessions

    from kiro_crew.subagent import _STARTUP_CAP_GATE_ROUNDS, SubagentManager

    probes: list[int] = []

    def _probe():
        probes.append(1)
        return sss.HostCapacity(affinity_cpus=32, quota_cpus=None, cpus=32, available_gb=200.0)

    monkeypatch.setattr(sss, "probe_host_capacity", _probe)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: KiroCrewConfig()))
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=64)
    assert probes == []
    real_lock = sss._host_lock

    class _HeldLock:  # a worker mid-probe holds the lock; the loop read must not wait on it
        def __enter__(self):
            raise AssertionError("_startup_cap waited on _host_lock")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sss, "_host_lock", _HeldLock())
    assert mgr._startup_cap() == _STARTUP_CAP_GATE_ROUNDS * sss.AUTO_FLOOR
    assert probes == []
    monkeypatch.setattr(sss, "_host_lock", real_lock)
    sss.host_capacity()  # the gateway's post-bind sizing task
    assert mgr._startup_cap() == _STARTUP_CAP_GATE_ROUNDS * 8
    assert probes == [1]
