"""Host-sized task and thread-pool ceilings (``kiro_crew.host_task_sizing``).

The formulas are pure over :class:`HostTaskFacts`, so each row states a host and
the value it must get. The wiring rows pin that an unset config key takes the
host-sized value and that an explicit value still wins.
"""

from __future__ import annotations

import sys
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew import host_task_sizing as hts
from kiro_crew.host_task_sizing import HostTaskFacts

GiB = 2**30

# The host the slice outage happened on: 128 vCPUs, 493 GiB, a systemd user
# slice capped at a third of pid_max.
BIG = HostTaskFacts(
    cpus=128,
    mem_bytes=493 * GiB,
    pid_max=4194304,
    threads_max=4040459,
    user_tasks_max=1333351,
)
# A 16 GiB laptop: threads-max is RAM-derived and small.
LAPTOP = HostTaskFacts(
    cpus=8, mem_bytes=16 * GiB, pid_max=4194304, threads_max=126000, user_tasks_max=None
)
# An old systemd whose UserTasksMax default was 12288.
TIGHT_USER = HostTaskFacts(
    cpus=4, mem_bytes=8 * GiB, pid_max=32768, threads_max=63000, user_tasks_max=12288
)
UNKNOWN = HostTaskFacts(None, None, None, None, None)


class TestSliceTasks:
    def test_many_core_host_gets_512_tasks_per_runtime_its_memory_holds(self):
        # 80% of 493 GiB is 403865 MB: 788 runtimes of 512 MB, plus one
        # 8192-task scope.
        assert hts.max_runtimes(BIG) == 788
        assert hts.size_slice_tasks(BIG) == 788 * 512 + 8192

    def test_follows_a_configured_memory_ceiling(self):
        assert hts.size_slice_tasks(BIG, slice_memory_mb=100_000) == 195 * 512 + 8192

    def test_capped_at_half_the_tightest_host_ceiling(self):
        facts = HostTaskFacts(128, 493 * GiB, 4194304, 4040459, 400_000)
        assert hts.size_slice_tasks(facts) == 200_000

    def test_small_host_keeps_the_old_fixed_value_as_a_floor(self):
        # 25 runtimes of 512 MB in 16 GiB: 12800 + 8192, below the floor.
        assert hts.size_slice_tasks(LAPTOP) == hts.SLICE_TASKS_FLOOR

    def test_never_above_the_tightest_user_or_kernel_ceiling(self):
        assert hts.size_slice_tasks(TIGHT_USER) == 12288

    def test_memory_bounds_a_host_whose_kernel_limits_are_loose(self):
        facts = HostTaskFacts(64, 64 * GiB, 4194304, 4194304, None)
        assert hts.size_slice_tasks(facts) == 102 * 512 + 8192

    def test_scope_room_matches_the_per_scope_default(self):
        from kiro_crew import sandbox_cgroup

        assert hts.SCOPE_TASKS == sandbox_cgroup._CGROUP_DEFAULT_MAX_PROCESSES

    def test_memory_share_matches_the_slice_memory_default(self):
        from kiro_crew import sandbox_slice

        assert hts.SLICE_MEMORY_SHARE == sandbox_slice._CGROUP_TOTAL_MEMORY_FRACTION

    def test_unknown_host_gets_the_floor(self):
        assert hts.size_slice_tasks(UNKNOWN) == hts.SLICE_TASKS_FLOOR


class TestThreadPool:
    @pytest.mark.parametrize("cpus, want", [(1, 1), (2, 2), (4, 4), (8, 4), (128, 4)])
    def test_min_of_cpus_and_four(self, cpus, want):
        facts = HostTaskFacts(cpus, None, None, None, None)
        assert hts.thread_pool_env_defaults(facts, ceiling_in_force=True) == {
            "TOKIO_WORKER_THREADS": str(want),
            "RAYON_NUM_THREADS": str(want),
        }

    def test_unknown_cpus_sets_nothing(self):
        assert hts.thread_pool_env_defaults(UNKNOWN, ceiling_in_force=True) == {}

    def test_no_task_ceiling_sets_nothing(self):
        """Without the agents slice there is no TasksMax for the cap to protect."""
        assert hts.thread_pool_env_defaults(BIG, ceiling_in_force=False) == {}

    @pytest.mark.parametrize("available", [True, False])
    def test_the_default_follows_the_cgroup_scope_probe(self, monkeypatch, available):
        monkeypatch.setattr("kiro_crew.sandbox._probe_cgroup_scope", lambda: (available, "test"))
        assert bool(hts.thread_pool_env_defaults(BIG)) is available

    def test_a_failing_probe_means_no_ceiling(self, monkeypatch):
        def boom() -> tuple[bool, str]:
            raise RuntimeError("probe failed")

        monkeypatch.setattr("kiro_crew.sandbox._probe_cgroup_scope", boom)
        assert hts.thread_pool_env_defaults(BIG) == {}

    def test_every_name_is_accepted_by_the_config_allowlist(self):
        from kiro_crew.config.sections_agent_env import CHILD_ENV_DEFAULT_NAMES

        assert set(hts.THREAD_POOL_ENV) <= CHILD_ENV_DEFAULT_NAMES


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads Linux procfs")
class TestProbe:
    def test_reads_kernel_and_user_cgroup_files(self, tmp_path: Path, monkeypatch):
        kernel = tmp_path / "kernel"
        kernel.mkdir()
        (kernel / "pid_max").write_text("4194304\n")
        (kernel / "threads-max").write_text("4040459\n")
        user_mgr = tmp_path / "user@1.service"
        user_slice = tmp_path / "user-1.slice"
        top = tmp_path / "user.slice"
        for d, value in ((user_mgr, "max"), (user_slice, "1333351"), (top, "max")):
            d.mkdir()
            (d / "pids.max").write_text(value + "\n")
        monkeypatch.setattr(hts, "_PROC_SYS_KERNEL", kernel)
        monkeypatch.setattr(hts, "_USER_CGROUP_CHAIN", (str(user_mgr), str(user_slice), str(top)))
        facts = hts.probe_host_task_facts()
        assert facts.pid_max == 4194304
        assert facts.threads_max == 4040459
        assert facts.user_tasks_max == 1333351
        assert facts.host_task_budget() == 1333351

    def test_cpus_come_from_affinity_not_the_gateway_quota(self, monkeypatch):
        """A pod gateway runs under CPUQuota=200%, but its agent scopes do not, and
        their runtimes size pools by the affinity count. Reading the quota sized a
        128-core host's scopes as if it had 2 cores."""
        import kiro_crew.cpu_affinity as aff
        import kiro_crew.session_start_sizing as sss

        monkeypatch.setattr(aff, "affinity_cpu_count", lambda: 128)
        monkeypatch.setattr(sss, "cgroup_cpu_quota", lambda roots=None: 2.0)
        assert hts.probe_host_task_facts().cpus == 128

    def test_unreadable_files_read_as_unknown(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(hts, "_PROC_SYS_KERNEL", tmp_path / "absent")
        monkeypatch.setattr(hts, "_USER_CGROUP_CHAIN", (str(tmp_path / "absent"),))
        facts = hts.probe_host_task_facts()
        assert (facts.pid_max, facts.threads_max, facts.user_tasks_max) == (None, None, None)


def _with_facts(monkeypatch, facts: HostTaskFacts, *, ceiling: bool = True) -> None:
    """Pin the host reading, and whether the agents slice's task ceiling is in force."""
    monkeypatch.setattr(hts, "host_task_facts", lambda: facts)
    monkeypatch.setattr("kiro_crew.sandbox._probe_cgroup_scope", lambda: (ceiling, "test"))


class TestWiring:
    @staticmethod
    def _raw(raw: dict):
        return unittest.mock.patch(
            "kiro_crew.config.loader._raw_config", return_value={"resource_limits": raw}
        )

    def test_unset_slice_tasks_take_the_host_value(self, monkeypatch):
        import kiro_crew.sandbox as sb

        _with_facts(monkeypatch, BIG)
        with self._raw({}):
            mem, tasks = sb._slice_limits_from_config()
        # Sized against the memory ceiling applied with it, read from this host.
        assert tasks == hts.size_slice_tasks(BIG, mem)

    def test_slice_tasks_follow_a_configured_memory_ceiling(self, monkeypatch):
        import kiro_crew.sandbox as sb

        _with_facts(monkeypatch, BIG)
        with self._raw({"max_total_memory_mb": 100_000}):
            _, tasks = sb._slice_limits_from_config()
        assert tasks == 195 * 512 + 8192

    def test_configured_slice_tasks_win(self, monkeypatch):
        import kiro_crew.sandbox as sb

        _with_facts(monkeypatch, BIG)
        with self._raw({"max_total_processes": 131072}):
            _, tasks = sb._slice_limits_from_config()
        assert tasks == 131072

    def test_a_failed_host_reading_falls_back_to_the_fixed_values(self, monkeypatch):
        import kiro_crew.sandbox as sb

        def boom() -> HostTaskFacts:
            raise OSError("no procfs")

        monkeypatch.setattr(hts, "host_task_facts", boom)
        with self._raw({}):
            _, tasks = sb._slice_limits_from_config()
            procs, _, _, _ = sb._cgroup_limits_from_config()
        assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS
        assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES


class TestChildEnvDefaults:
    @staticmethod
    def _apply(monkeypatch, configured: dict[str, str], env: dict[str, str]) -> dict[str, str]:
        from kiro_crew.acp import child_env_defaults as ced

        monkeypatch.setattr(ced, "_configured_defaults", lambda: dict(configured))
        ced.apply_child_env_defaults(env)
        return env

    def test_host_default_fills_an_unset_pool(self, monkeypatch):
        _with_facts(monkeypatch, BIG)
        env = self._apply(monkeypatch, {}, {})
        assert env == {"TOKIO_WORKER_THREADS": "4", "RAYON_NUM_THREADS": "4"}

    def test_configured_value_wins_over_the_host_default(self, monkeypatch):
        _with_facts(monkeypatch, BIG)
        env = self._apply(monkeypatch, {"TOKIO_WORKER_THREADS": "16"}, {})
        assert env == {"TOKIO_WORKER_THREADS": "16", "RAYON_NUM_THREADS": "4"}

    def test_inherited_value_wins_over_both(self, monkeypatch):
        _with_facts(monkeypatch, BIG)
        env = self._apply(
            monkeypatch, {"TOKIO_WORKER_THREADS": "16"}, {"TOKIO_WORKER_THREADS": "2"}
        )
        assert env["TOKIO_WORKER_THREADS"] == "2"

    def test_unknown_host_adds_nothing(self, monkeypatch):
        _with_facts(monkeypatch, UNKNOWN)
        assert self._apply(monkeypatch, {}, {}) == {}

    def test_a_host_without_the_ceiling_keeps_its_pools(self, monkeypatch):
        """macOS, Windows and Linux without the agents slice: nothing is capped."""
        _with_facts(monkeypatch, BIG, ceiling=False)
        assert self._apply(monkeypatch, {}, {}) == {}

    def test_a_configured_value_still_applies_without_the_ceiling(self, monkeypatch):
        _with_facts(monkeypatch, BIG, ceiling=False)
        env = self._apply(monkeypatch, {"TOKIO_WORKER_THREADS": "16"}, {})
        assert env == {"TOKIO_WORKER_THREADS": "16"}

    def test_an_inherited_value_is_never_overridden(self, monkeypatch):
        for ceiling in (True, False):
            _with_facts(monkeypatch, BIG, ceiling=ceiling)
            env = self._apply(monkeypatch, {}, {"RAYON_NUM_THREADS": "12"})
            assert env["RAYON_NUM_THREADS"] == "12"

    def test_a_failing_host_reading_never_breaks_the_spawn(self, monkeypatch):
        def boom() -> HostTaskFacts:
            raise RuntimeError("probe failed")

        monkeypatch.setattr(hts, "host_task_facts", boom)
        assert self._apply(monkeypatch, {"RAYON_NUM_THREADS": "2"}, {}) == {
            "RAYON_NUM_THREADS": "2"
        }


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="cgroup v2 row")
class TestDoctorRow:
    @staticmethod
    def _slice(tmp_path: Path, current: int, maximum: int, scopes: list[int]) -> Path:
        root = tmp_path / "kirocrew-agents.slice"
        inst = root / "kirocrew-agents-abc.slice"
        inst.mkdir(parents=True)
        (root / "pids.current").write_text(f"{current}\n")
        (root / "pids.max").write_text(f"{maximum}\n")
        for i, n in enumerate(scopes):
            scope = inst / f"run-r{i}.scope"
            scope.mkdir()
            (scope / "pids.current").write_text(f"{n}\n")
            (scope / "pids.max").write_text("16384\n")
        return root

    @staticmethod
    def _run(
        monkeypatch,
        slice_dir: Path | None,
        raw_limits: dict,
        agent_env: dict[str, str] | None = None,
        child_env: dict[str, str] | None = None,
        ceiling: bool = True,
    ) -> str:
        from kiro_crew import cli_doctor
        from kiro_crew.config.sections_limits import ResourceLimitsConfig
        from kiro_crew.doctor_checks import resources

        _with_facts(monkeypatch, BIG, ceiling=ceiling)
        cfg = unittest.mock.MagicMock()
        cfg.resource_limits = ResourceLimitsConfig.from_raw(raw_limits)
        cfg.agent.child_env_defaults = dict(child_env or {})
        cfg.agent.env = dict(agent_env or {})
        monkeypatch.setattr(cli_doctor.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        monkeypatch.setattr(cli_doctor.sandbox, "_agents_slice_cgroup_dir", lambda: slice_dir)
        return "\n".join(resources._task_ceiling_lines())

    def test_reports_host_values_usage_and_headroom(self, tmp_path, monkeypatch):
        slice_dir = self._slice(tmp_path, 9000, 411648, [200, 180, 220])
        out = self._run(monkeypatch, slice_dir, {})
        assert "TasksMax 411648 (host-sized)" in out
        assert "9000 of 411648 tasks (2%)" in out
        assert "3 scopes, 200 tasks each on average, largest 220" in out
        assert "TasksMax 8192 (default)" in out
        assert "TOKIO_WORKER_THREADS: 4 (host-sized)" in out

    def test_live_ceiling_below_this_install_warns(self, tmp_path, monkeypatch):
        slice_dir = self._slice(tmp_path, 9000, 32768, [200])
        out = self._run(monkeypatch, slice_dir, {"max_total_processes": 131072})
        assert "is below this install's 131072" in out

    def test_nearly_full_slice_warns_and_names_the_fix(self, tmp_path, monkeypatch):
        slice_dir = self._slice(tmp_path, 30000, 32768, [800] * 37)
        out = self._run(monkeypatch, slice_dir, {"max_total_processes": 32768})
        assert "close to the ceiling" in out
        assert "raise resource_limits.max_total_processes toward 411648" in out

    def test_pool_values_follow_spawn_precedence(self, monkeypatch):
        """agent.env is laid over the inherited environment before the
        set-if-unset defaults run, so doctor must report it first. The gateway's
        own environment is never read back, because it can change after exec."""
        out = self._run(
            monkeypatch,
            None,
            {},
            agent_env={"TOKIO_WORKER_THREADS": "12", "RAYON_NUM_THREADS": "3"},
            child_env={"TOKIO_WORKER_THREADS": "8"},
        )
        assert "TOKIO_WORKER_THREADS: 12 (agent.env)" in out
        assert "RAYON_NUM_THREADS: 3 (agent.env)" in out
        out = self._run(monkeypatch, None, {}, child_env={"TOKIO_WORKER_THREADS": "8"})
        assert "TOKIO_WORKER_THREADS: 8 (agent.child_env_defaults)" in out
        assert "RAYON_NUM_THREADS: 4 (host-sized)" in out
        assert "gateway's environment" in out

    def test_without_the_ceiling_the_pools_are_reported_unset(self, monkeypatch):
        """Doctor reports what a spawn gets: no host default where no slice exists."""
        out = self._run(monkeypatch, None, {}, ceiling=False)
        assert "TOKIO_WORKER_THREADS: unset (kiro-cli uses one worker per core)" in out
        assert "RAYON_NUM_THREADS: unset (kiro-cli uses one worker per core)" in out

    def test_the_section_never_adds_an_issue(self, tmp_path, monkeypatch, capsys):
        """The slice is shared by every gateway under the user, so its live state
        is host context, not a fault in this install: a full slice warns but
        leaves the doctor verdict alone."""
        from kiro_crew.doctor_checks import resources

        slice_dir = self._slice(tmp_path, 30000, 32768, [800] * 37)
        self._run(monkeypatch, slice_dir, {"max_total_processes": 131072})
        issues: list[str] = []
        resources._doctor_task_ceilings(issues)
        assert "close to the ceiling" in capsys.readouterr().out
        assert issues == []

    def test_inactive_slice_measures_nothing(self, monkeypatch):
        out = self._run(monkeypatch, None, {})
        assert "not active" in out
