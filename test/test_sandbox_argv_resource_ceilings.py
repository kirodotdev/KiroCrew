"""Kernel resource ceilings on agent spawns.

The rlimit and NOFILE preexecs, the ``systemd --user`` cgroup scope argv and the limits
it carries, the aggregate ``kirocrew-agents.slice`` limits, and the user-bus env the
scope needs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import sandbox_argv_helpers
from sandbox_argv_helpers import _POSIX_ONLY

# Several tests of this suite spawn real child interpreters (subprocess.run([sys.executable,
# ...])); every part of it pins itself to the same dedicated xdist worker so concurrent
# cold-starts under -n auto don't starve each other / blow the 30s timeout. Requires
# --dist loadgroup.
pytestmark = [
    pytest.mark.xdist_group(name="subprocess_spawn"),
    # test_sandbox_argv.py replaces ctypes.CDLL, find_library or a cached libc handle for a
    # test; every part of the suite puts the shared macOS handle caches back after each
    # test (see conftest.py).
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]


# The shared fixtures, bound here by name so pytest registers them for this module.
clean_backend = sandbox_argv_helpers.clean_backend
systemd_run_resolvable = sandbox_argv_helpers.systemd_run_resolvable


class TestResourceLimitPreexec:
    """resource_limit_preexec() is the cached companion to sandboxed_spawn_argv:
    it hands every agent-influenced spawn the kernel resource ceiling."""

    def _reset_cache(self):
        import kiro_crew.sandbox as sb

        sb._RESOURCE_PREEXEC = sb._UNSET

    @_POSIX_ONLY
    def test_returns_callable_and_caches(self):
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            first = sb.resource_limit_preexec()
            second = sb.resource_limit_preexec()
            assert callable(first)
            assert first is second
        finally:
            self._reset_cache()

    @_POSIX_ONLY
    def test_config_read_failure_falls_back_to_defaults(self):
        """If config load raises, the preexec still builds from safe defaults
        (no crash, protection still applied)."""
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            with patch("kiro_crew.config.loader._raw_config", side_effect=RuntimeError("boom")):
                fn = sb.resource_limit_preexec()
            assert callable(fn)
        finally:
            self._reset_cache()

    def test_non_posix_returns_none(self):
        """On non-POSIX (os.name != 'posix'), returns None — create_subprocess_exec
        rejects any non-None preexec_fn on Windows with ValueError, so the
        contract must be None there."""
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            with patch("kiro_crew.sandbox.os.name", "nt"):
                assert sb.resource_limit_preexec() is None
        finally:
            self._reset_cache()


class TestSessionHostPreexec:
    """session_host_preexec() raises NOFILE to the hard limit for trusted
    session host processes (kiro-cli-chat), preventing EMFILE crashes when
    managing many MCP server subprocesses."""

    def _reset_cache(self):
        import kiro_crew.sandbox as sb

        sb._SESSION_HOST_PREEXEC = sb._UNSET

    @_POSIX_ONLY
    def test_returns_callable_and_caches(self):
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            first = sb.session_host_preexec()
            second = sb.session_host_preexec()
            assert callable(first)
            assert first is second
        finally:
            self._reset_cache()

    @_POSIX_ONLY
    def test_raises_nofile_to_hard_limit(self):
        """The preexec callable raises NOFILE soft to the hard limit."""
        import resource

        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            fn = sb.session_host_preexec()
            assert fn is not None
            # Save current limits, lower soft to simulate the problem.
            orig_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            if hard < 2048:
                pytest.skip("hard limit too low for test")
            resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
            try:
                fn()
                new_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
                if hard == resource.RLIM_INFINITY:
                    # Implementation contract: unlimited hard (macOS) caps the
                    # soft limit at max(inherited_soft, 65536), never infinity.
                    assert new_soft == 65536
                else:
                    assert new_soft == hard
            finally:
                resource.setrlimit(resource.RLIMIT_NOFILE, (orig_soft, hard))
        finally:
            self._reset_cache()

    def test_non_posix_returns_none(self):
        import kiro_crew.sandbox as sb

        self._reset_cache()
        try:
            with patch("kiro_crew.sandbox.os.name", "nt"):
                assert sb.session_host_preexec() is None
        finally:
            self._reset_cache()


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestCgroupScopeArgv:
    """cgroup_scope_argv() wraps agent spawns in a transient systemd --user
    --scope with pids.max + memory.max — the default-on fork-bomb / memory-DoS
    ceiling the finding's headline threats require."""

    def _reset_probe(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False

    def test_available_prepends_systemd_scope_with_limits(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=True),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            # Absolute: the wrapper is pinned where it is prepended, so a caller
            # passing a config-declared PATH in env= cannot redirect argv[0].
            assert os.path.basename(out[0]) == "systemd-run"
            assert "--user" in out and "--scope" in out
            assert "TasksMax=8192" in out
            assert "MemoryMax=8192M" in out
            assert "MemorySwapMax=0" in out
            assert "CPUWeight=50" in out
            # CPUQuota is opt-in: absent unless max_cpu_percent > 0.
            assert not any(a.startswith("CPUQuota=") for a in out)
            assert out[out.index("--") + 1 :] == ["kiro-cli", "chat"]
        finally:
            self._reset_probe()

    @_POSIX_ONLY
    def test_cpu_controller_delegated_real_path(self):
        """Cover the uncached probe body: reads the user-slice controllers file
        and reports cpu presence; failures report False (skip CPU properties,
        keep pids/memory enforcement)."""
        from unittest.mock import mock_open

        import kiro_crew.sandbox as sb

        try:
            sb._CPU_DELEGATED = None
            with patch("builtins.open", mock_open(read_data="cpu memory pids\n")):
                assert sb._cpu_controller_delegated() is True
            sb._CPU_DELEGATED = None
            with patch("builtins.open", mock_open(read_data="memory pids\n")):
                assert sb._cpu_controller_delegated() is False
            sb._CPU_DELEGATED = None
            with patch("builtins.open", side_effect=OSError("no cgroup")):
                assert sb._cpu_controller_delegated() is False
            # Cached: second call must not re-read.
            with patch("builtins.open", side_effect=AssertionError("must not open")):
                assert sb._cpu_controller_delegated() is False
        finally:
            sb._CPU_DELEGATED = None

    def test_cpu_quota_emitted_when_configured(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 75, 200),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=True),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            assert "CPUWeight=75" in out
            assert "CPUQuota=200%" in out
        finally:
            self._reset_probe()

    def test_no_cpu_properties_without_cpu_delegation(self):
        """pids/memory enforcement must not be lost when only cpu delegation
        is missing — the scope is still created, minus the CPU properties."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 200),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            assert os.path.basename(out[0]) == "systemd-run"
            assert "TasksMax=8192" in out
            assert not any(a.startswith("CPUWeight=") for a in out)
            assert not any(a.startswith("CPUQuota=") for a in out)
        finally:
            self._reset_probe()

    def test_unavailable_is_passthrough_and_warns_once(self, caplog):
        import logging

        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with patch(
                "kiro_crew.sandbox._probe_cgroup_scope",
                return_value=(False, "not Linux"),
            ):
                with caplog.at_level(logging.WARNING):
                    out1 = sb.cgroup_scope_argv(["git", "status"])
                    out2 = sb.cgroup_scope_argv(["git", "log"])
            assert out1 == ["git", "status"]
            assert out2 == ["git", "log"]
            sec = [r for r in caplog.records if "SECURITY" in r.getMessage()]
            assert len(sec) == 1
            assert "not Linux" in sec[0].getMessage()
        finally:
            self._reset_probe()

    def test_config_overrides_cgroup_limits(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 200,
                        "max_memory_mb": 2048,
                        "cpu_weight": 80,
                        "max_cpu_percent": 400,
                    }
                },
            ):
                procs, mem, weight, quota = sb._cgroup_limits_from_config()
            assert procs == 200
            assert mem == 2048
            assert weight == 80
            assert quota == 400
        finally:
            self._reset_probe()

    def test_config_defaults_when_absent_or_zero(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            # Missing block -> module defaults (never leave the cgroup ceiling
            # unset). Memory default is host-proportional (65% of RAM).
            with patch("kiro_crew.config.loader._raw_config", return_value={}):
                procs, mem, weight, quota = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == sb._default_max_memory_mb()
            assert weight == sb._CGROUP_DEFAULT_CPU_WEIGHT
            assert quota == 0  # opt-in: no CPUQuota by default
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 0,
                        "max_memory_mb": "x",
                        "cpu_weight": 0,
                        "max_cpu_percent": -5,
                    }
                },
            ):
                procs, mem, weight, quota = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == sb._default_max_memory_mb()
            assert weight == sb._CGROUP_DEFAULT_CPU_WEIGHT
            assert quota == 0
            # Fractions must not truncate into invalid TasksMax=0 /
            # MemoryMax=0M properties.
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 0.5,
                        "max_memory_mb": 0.9,
                    }
                },
            ):
                procs, mem, _, _ = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == sb._default_max_memory_mb()
            # NaN/Infinity (json.loads accepts both) must fall back to
            # defaults WITHOUT raising: int(nan)/int(inf) raise inside the
            # surrounding try/except, which would silently discard an
            # otherwise-valid stricter limit on a later field in the same
            # block (e.g. a legitimate max_memory_mb after a bogus
            # max_processes).
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": float("nan"),
                        "max_memory_mb": 512,
                    }
                },
            ):
                procs, mem, _, _ = sb._cgroup_limits_from_config()
            assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
            assert mem == 512  # must not be discarded by the NaN above it
            with patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_processes": 64,
                        "max_memory_mb": float("inf"),
                    }
                },
            ):
                procs, mem, _, _ = sb._cgroup_limits_from_config()
            assert procs == 64  # must not be discarded by the inf below it
            assert mem == sb._default_max_memory_mb()
        finally:
            self._reset_probe()

    @_POSIX_ONLY
    def test_default_max_memory_is_host_proportional(self):
        """The memory default scales with physical RAM (65%), not a flat cap."""
        import kiro_crew.sandbox as sb

        # A known 16 GiB box -> 65% -> ~10649 MB.
        sixteen_g = 16 * 1024**3
        with patch("os.sysconf", side_effect=lambda n: sixteen_g // 4096 if "PHYS" in n else 4096):
            mb = sb._default_max_memory_mb()
        assert mb == int(sixteen_g * sb._CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        assert 10_000 < mb < 11_000  # ~10.6 GB, expected range

    @_POSIX_ONLY
    def test_default_max_memory_falls_back_when_ram_unknown(self):
        """If sysconf can't report RAM, fall back to the flat MB constant.

        ``system_memory`` is stubbed out alongside ``os.sysconf`` because it is
        the second probe: on Windows ``GlobalMemoryStatusEx`` answers, so patching
        only ``sysconf`` would not make RAM unknown and this would assert
        against a derived value instead of the fallback.
        """
        import kiro_crew.sandbox as sb

        with (
            patch("os.sysconf", side_effect=OSError("no sysconf")),
            patch.object(sb.platform_compat, "system_memory", return_value=None),
        ):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB
        # Non-positive product also falls back (never returns 0 -> unlimited).
        with (
            patch("os.sysconf", return_value=0),
            patch.object(sb.platform_compat, "system_memory", return_value=None),
        ):
            assert sb._default_max_memory_mb() == sb._CGROUP_FALLBACK_MAX_MEMORY_MB

    @pytest.mark.skipif(sys.platform != "linux", reason="cgroup v2 scope enforcement is Linux-only")
    @pytest.mark.usefixtures("real_user_session")
    def test_real_pids_max_enforced_when_available(self):
        """If this host actually has cgroup delegation, the scope must ENFORCE
        pids.max — a child under a tiny TasksMax cannot fork past it. Skips
        cleanly where delegation is unavailable (the probe returns False).

        The floor runs the suite without a systemd user session, so this is the
        one test that opts back in (``real_user_session``), and that fixture stops
        the transient slice the real ``systemd-run`` creates."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            available, _ = sb._probe_cgroup_scope()
            if not available:
                pytest.skip("no cgroup v2 delegation on this host")
            # Delegated controllers do not prove the user bus is reachable.
            # Only an unavailable bus is a skip; a broken scope stays a failure.
            preflight = subprocess.run(
                sb.cgroup_scope_argv([sys.executable, "-c", ""]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            if preflight.returncode != 0 and "Failed to connect to bus" in preflight.stderr:
                pytest.skip(f"user session bus unavailable: {preflight.stderr.strip()}")
            assert preflight.returncode == 0, preflight.stderr
            with patch(
                "kiro_crew.sandbox._cgroup_limits_from_config", return_value=(20, 8192, 50, 0)
            ):
                argv = sb.cgroup_scope_argv(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys\n"
                        "n=0\n"
                        "try:\n"
                        "    for _ in range(200):\n"
                        "        if os.fork()==0:\n"
                        "            import time; time.sleep(1); os._exit(0)\n"
                        "        n+=1\n"
                        "    print('forked-all')\n"
                        "except OSError:\n"
                        "    print('hit-limit')\n",
                    ]
                )
            out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == "hit-limit"
        finally:
            self._reset_probe()


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestAgentsSliceLimits:
    """ensure_agents_slice_limits() puts an AGGREGATE MemoryMax/TasksMax on
    kirocrew-agents.slice — the parent of every per-spawn scope — so N
    concurrent scopes cannot collectively request N x 65% of host RAM while
    each stays inside its own per-scope ceiling."""

    def _reset(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False
        sb._SLICE_LIMITS_APPLIED = False
        sb._SLICE_OOM_SEEN = None

    def test_applies_runtime_property_once_idempotent(self):
        """One systemctl invocation with the exact property set; a second call
        is a no-op returning True (idempotent across restarts of the caller).
        argv[0] must be the TRUSTED absolute path, never a bare name PATH
        could resolve to an agent-planted shim."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch("kiro_crew.sandbox._slice_limits_from_config", return_value=(10000, 32768)),
                patch(
                    "kiro_crew.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
            ):
                assert sb.ensure_agents_slice_limits() is True
                assert sb.ensure_agents_slice_limits() is True
            assert run_mock.call_count == 1
            argv = run_mock.call_args[0][0]
            assert argv == [
                "/usr/bin/systemctl",
                "--user",
                "set-property",
                "--runtime",
                "kirocrew-agents.slice",
                "MemoryMax=10000M",
                "MemorySwapMax=0",
                "TasksMax=32768",
            ]
        finally:
            self._reset()

    def test_no_trusted_systemctl_means_no_apply(self):
        """PATH is never consulted: when no trusted systemctl exists, the
        ceiling is skipped (returns False), not resolved through PATH."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock()
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch("kiro_crew.platform_compat.trusted_system_bin", return_value=None),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
            ):
                assert sb.ensure_agents_slice_limits() is False
            run_mock.assert_not_called()
        finally:
            self._reset()

    def test_skipped_when_unavailable_and_no_second_warning(self, caplog):
        """No delegation -> no systemctl call, and the slice site plus the
        per-spawn site together emit exactly ONE SECURITY warning."""
        import logging

        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock()
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(False, "not Linux")),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
                caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"),
            ):
                assert sb.ensure_agents_slice_limits() is False
                out = sb.cgroup_scope_argv(["git", "status"])
            run_mock.assert_not_called()
            assert out == ["git", "status"]
            security = [r for r in caplog.records if "SECURITY" in r.getMessage()]
            assert len(security) == 1
        finally:
            self._reset()

    def test_failed_apply_is_retried_next_call(self):
        """A nonzero rc leaves the ceiling unapplied — the next call retries
        rather than caching the failure as success."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            run_mock = MagicMock(return_value=MagicMock(returncode=1, stderr="boom"))
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch("kiro_crew.sandbox._slice_limits_from_config", return_value=(10000, 32768)),
                patch(
                    "kiro_crew.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemctl",
                ),
                patch("kiro_crew.sandbox.subprocess.run", run_mock),
            ):
                assert sb.ensure_agents_slice_limits() is False
                assert sb.ensure_agents_slice_limits() is False
            assert run_mock.call_count == 2
        finally:
            self._reset()

    def test_config_overrides_slice_limits(self):
        import kiro_crew.sandbox as sb

        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={
                "resource_limits": {
                    "max_total_memory_mb": 4096,
                    "max_total_processes": 1000,
                }
            },
        ):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == 4096
        assert tasks == 1000

    def test_config_defaults_when_absent_or_junk(self):
        """Zero/junk falls back to the default rather than leaving the
        aggregate unset — same rule as the per-scope ceiling."""
        import kiro_crew.sandbox as sb

        with patch("kiro_crew.config.loader._raw_config", return_value={}):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == sb._default_max_total_memory_mb()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={
                "resource_limits": {
                    "max_total_memory_mb": 0,
                    "max_total_processes": "x",
                }
            },
        ):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == sb._default_max_total_memory_mb()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS
        # Fractional values pass a naive `> 0` check but truncate to 0, which
        # would emit MemoryMax=0M and kill every agent scope — must fall back.
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={
                "resource_limits": {
                    "max_total_memory_mb": 0.5,
                    "max_total_processes": 0.9,
                }
            },
        ):
            mem, tasks = sb._slice_limits_from_config()
        assert mem == sb._default_max_total_memory_mb()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS

    @_POSIX_ONLY
    def test_default_total_memory_fraction_and_fallback(self):
        """80% of RAM by default; flat fallback when RAM is unreadable. Both
        must sit ABOVE their per-scope counterparts, or the slice would clamp
        a single spawn tighter than its own documented ceiling."""
        import kiro_crew.sandbox as sb

        sixteen_g = 16 * 1024**3
        with patch("os.sysconf", side_effect=lambda n: sixteen_g // 4096 if "PHYS" in n else 4096):
            mb = sb._default_max_total_memory_mb()
        assert mb == int(sixteen_g * sb._CGROUP_TOTAL_MEMORY_FRACTION) // (1024 * 1024)
        with patch("os.sysconf", side_effect=OSError("no sysconf")):
            assert sb._default_max_total_memory_mb() == sb._CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB
        assert sb._CGROUP_TOTAL_MEMORY_FRACTION > sb._CGROUP_MEMORY_FRACTION
        assert sb._CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB > sb._CGROUP_FALLBACK_MAX_MEMORY_MB

    def test_per_scope_property_still_emitted_ratchet(self):
        """RATCHET: the two-level model needs BOTH layers. The per-spawn scope
        must keep emitting its own MemoryMax under the slice — a future change
        must not silently replace per-tree bounding with aggregate-only."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 4096, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
            ):
                out = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            # Still placed under the aggregate boundary, now via a per-instance
            # child of it (see _agents_slice_name) — cgroup v2 bounds a
            # descendant by the minimum effective limit along its ancestor
            # chain, so the aggregate layer of the two-level model is intact.
            parent_stem = sb._CGROUP_AGENTS_SLICE[: -len(".slice")]
            assert any(
                a.startswith(f"--slice={parent_stem}") and a.endswith(".slice") for a in out
            ), out
            assert "MemoryMax=4096M" in out
            assert "TasksMax=8192" in out
        finally:
            self._reset()

    def _fake_slice(self, tmp_path, *, oom_kill=0, local_max=0, current=100, mem_max="1000"):
        d = tmp_path / "kirocrew-agents.slice"
        d.mkdir(exist_ok=True)
        (d / "memory.events").write_text(f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {oom_kill}\n")
        (d / "memory.events.local").write_text(
            f"low 0\nhigh 0\nmax {local_max}\noom 0\noom_kill 0\n"
        )
        (d / "memory.current").write_text(f"{current}\n")
        (d / "memory.max").write_text(f"{mem_max}\n")
        return d

    def test_slice_pressure_seeds_then_reports_new_kills(self, tmp_path):
        """First read seeds the counters (no spurious boot warning); a later
        oom_kill increase is reported with the victim scope and whether the
        slice-level ceiling engaged."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path, oom_kill=2)
            with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d):
                assert sb.check_agents_slice_pressure() is None  # seed only
                # A scope takes a kill and the slice's own limit engaged.
                self._fake_slice(tmp_path, oom_kill=3, local_max=1)
                victim = d / "run-r1.scope"
                victim.mkdir()
                (victim / "memory.events.local").write_text(
                    "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n"
                )
                msg = sb.check_agents_slice_pressure()
                assert msg is not None
                assert "1 new kill(s)" in msg
                assert "run-r1.scope" in msg
                assert "aggregate ceiling engaged: yes" in msg
                # No further change -> quiet.
                assert sb.check_agents_slice_pressure() is None
        finally:
            self._reset()

    def test_slice_pressure_scope_local_breach_is_distinguished(self, tmp_path):
        """A kill without a slice-level max event reads as a per-scope breach."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path)
            with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d):
                assert sb.check_agents_slice_pressure() is None
                self._fake_slice(tmp_path, oom_kill=1, local_max=0)
                msg = sb.check_agents_slice_pressure()
                assert msg is not None
                assert "a scope hit its own per-tree limit" in msg
        finally:
            self._reset()

    def test_slice_pressure_none_when_slice_absent(self):
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            with patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=None):
                assert sb.check_agents_slice_pressure() is None
        finally:
            self._reset()

    def test_slice_pressure_self_heals_a_vanished_ceiling(self, tmp_path):
        """A user-manager restart drops the --runtime property. The sampler
        detects memory.max reading 'max' and re-applies — but only when WE
        applied the ceiling before."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path, mem_max="max")
            ensure_mock = MagicMock(return_value=True)
            with (
                patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d),
                patch("kiro_crew.sandbox.ensure_agents_slice_limits", ensure_mock),
            ):
                sb._SLICE_LIMITS_APPLIED = True
                sb.check_agents_slice_pressure()
                ensure_mock.assert_called_once()
                assert sb._SLICE_LIMITS_APPLIED is False  # reset so the re-apply is real
        finally:
            self._reset()

    def test_slice_pressure_no_heal_when_never_applied(self, tmp_path):
        """A host that never passed the delegation gate must not start
        shelling out from the sampler."""
        import kiro_crew.sandbox as sb

        self._reset()
        try:
            d = self._fake_slice(tmp_path, mem_max="max")
            ensure_mock = MagicMock(return_value=True)
            with (
                patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=d),
                patch("kiro_crew.sandbox.ensure_agents_slice_limits", ensure_mock),
            ):
                sb._SLICE_LIMITS_APPLIED = False
                sb.check_agents_slice_pressure()
                ensure_mock.assert_not_called()
        finally:
            self._reset()

    @pytest.mark.skipif(sys.platform != "linux", reason="cgroup v2 scope enforcement is Linux-only")
    def test_real_scope_nests_under_agents_slice(self):
        """On a delegation-capable host, a real scope's cgroup path runs
        through kirocrew-agents.slice — the structural premise of the
        aggregate boundary: whatever limit the slice carries, the kernel
        min-composes it over every scope. When the live slice carries a
        MemoryMax (a running gateway applied one), assert the scope's own
        limit is not the only bound in the ancestry. Skips cleanly where
        delegation is unavailable. No host state is mutated: the test only
        spawns a scope (as every spawn does) and reads cgroup files."""
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        try:
            available, _ = sb._probe_cgroup_scope()
            if not available:
                pytest.skip("no cgroup v2 delegation on this host")
            with patch(
                "kiro_crew.sandbox._cgroup_limits_from_config", return_value=(50, 512, 50, 0)
            ):
                argv = sb.cgroup_scope_argv(
                    [
                        sys.executable,
                        "-c",
                        "cg=open('/proc/self/cgroup').read().split('::',1)[1].strip()\n"
                        "print(cg)\n"
                        "print(open('/sys/fs/cgroup'+cg+'/memory.max').read().strip())\n",
                    ]
                )
            out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert out.returncode == 0, out.stderr
            cg_path, scope_max = out.stdout.strip().splitlines()
            assert "/kirocrew-agents.slice/" in cg_path
            assert scope_max == str(512 * 1024 * 1024)
        finally:
            sb._CGROUP_SCOPE_PROBE = None


@pytest.mark.usefixtures("systemd_run_resolvable")
class TestCgroupScopeBusEnv:
    """The systemd-run scope prepended by cgroup_scope_argv needs the user
    session bus in the environment it is spawned with. Callers that build that
    environment from a strict allowlist (source_providers.py) drop the bus
    locators, and systemd-run then dies with "Failed to connect to bus: No
    medium found" before it ever exec's the wrapped command.

    The locators must NOT survive into the sandboxed child, though: a live
    user-bus address there can start a systemd unit outside the sandbox. So the
    forward is paired with an `env -u` shim inside the scope."""

    def _reset_probe(self):
        import kiro_crew.sandbox as sb

        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False

    def test_forwards_bus_locators_into_allowlist_env(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch.dict(
                    os.environ,
                    {
                        "XDG_RUNTIME_DIR": "/run/user/4242",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
                    },
                    clear=False,
                ),
            ):
                out, injected = sb.cgroup_scope_bus_env(
                    {"PATH": "/usr/bin:/bin", "HOME": "/home/u"}
                )
            assert out["XDG_RUNTIME_DIR"] == "/run/user/4242"
            assert out["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"
            assert injected == ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
            # The caller's own keys survive untouched.
            assert out["PATH"] == "/usr/bin:/bin"
            assert out["HOME"] == "/home/u"
        finally:
            self._reset_probe()

    def test_caller_value_wins_and_missing_keys_stay_absent(self):
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            env = {"XDG_RUNTIME_DIR": "/caller/runtime"}
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False),
            ):
                os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
                out, injected = sb.cgroup_scope_bus_env(env)
            assert out["XDG_RUNTIME_DIR"] == "/caller/runtime"
            # Nothing to forward -> the key is not invented.
            assert "DBUS_SESSION_BUS_ADDRESS" not in out
            # A caller-supplied value is NOT ours to strip inside the scope.
            assert injected == ()
            # Input dict is never mutated in place.
            assert env == {"XDG_RUNTIME_DIR": "/caller/runtime"}
        finally:
            self._reset_probe()

    def test_passthrough_when_scope_unavailable(self):
        """No systemd-run prefix -> the caller's environment is handed through
        exactly as given, bus locators included or not."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch(
                    "kiro_crew.sandbox._probe_cgroup_scope",
                    return_value=(False, "not Linux"),
                ),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False),
            ):
                out, injected = sb.cgroup_scope_bus_env({"PATH": "/usr/bin"})
            assert out == {"PATH": "/usr/bin"}
            assert injected == ()
        finally:
            self._reset_probe()

    def test_unset_env_argv_prefix_and_absence(self):
        """The shim is built from an absolute path (never PATH-resolved), and
        reports None when no env binary exists so callers can fail closed."""
        import kiro_crew.sandbox as sb

        argv = sb._unset_env_argv(("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"))
        if argv is not None:
            assert argv[0] in sb._ENV_BINARY_CANDIDATES
            assert os.path.isabs(argv[0])
            assert argv[1:] == [
                "-u",
                "XDG_RUNTIME_DIR",
                "-u",
                "DBUS_SESSION_BUS_ADDRESS",
            ]
        with patch("kiro_crew.sandbox.os.path.isfile", return_value=False):
            assert sb._unset_env_argv(("XDG_RUNTIME_DIR",)) is None

    def test_sandboxed_spawn_argv_forwards_bus_but_child_cannot_keep_it(self):
        """End-to-end at the chokepoint: the spawn env carries the locators (so
        systemd-run can reach the bus) AND the argv drops them again inside the
        scope (so the sandboxed child cannot use the bus)."""
        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox.wrap_argv", return_value=(["gh", "pr", "view"], None)),
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
                patch(
                    "kiro_crew.sandbox._unset_env_argv",
                    return_value=[
                        "/usr/bin/env",
                        "-u",
                        "XDG_RUNTIME_DIR",
                        "-u",
                        "DBUS_SESSION_BUS_ADDRESS",
                    ],
                ),
                patch.dict(
                    os.environ,
                    {
                        "XDG_RUNTIME_DIR": "/run/user/4242",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
                    },
                    clear=False,
                ),
            ):
                argv, env, _cleanup = sb.sandboxed_spawn_argv(
                    ["gh", "pr", "view"],
                    env={"PATH": "/usr/bin:/bin", "HOME": "/home/u"},
                )
            assert os.path.basename(argv[0]) == "systemd-run"
            assert env["XDG_RUNTIME_DIR"] == "/run/user/4242"
            assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"
            # The shim sits INSIDE the scope, immediately after `--`, so the real
            # command execs without the locators.
            inner = argv[argv.index("--") + 1 :]
            assert inner == [
                "/usr/bin/env",
                "-u",
                "XDG_RUNTIME_DIR",
                "-u",
                "DBUS_SESSION_BUS_ADDRESS",
                "gh",
                "pr",
                "view",
            ]
        finally:
            self._reset_probe()

    def test_no_env_binary_fails_closed_without_leaking_bus(self, caplog):
        """If the locators cannot be dropped again, they are not forwarded at
        all: systemd-run fails loudly rather than the child getting a live bus."""
        import logging

        import kiro_crew.sandbox as sb

        self._reset_probe()
        try:
            with (
                patch("kiro_crew.sandbox.wrap_argv", return_value=(["gh"], None)),
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
                patch("kiro_crew.sandbox._unset_env_argv", return_value=None),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=False),
                caplog.at_level(logging.WARNING),
            ):
                argv, env, _cleanup = sb.sandboxed_spawn_argv(["gh"], env={"PATH": "/usr/bin:/bin"})
            assert "XDG_RUNTIME_DIR" not in env
            assert "DBUS_SESSION_BUS_ADDRESS" not in env
            assert argv[argv.index("--") + 1 :] == ["gh"]
            assert any("SECURITY" in r.getMessage() for r in caplog.records)
        finally:
            self._reset_probe()
