"""Coverage tests for ``kiro_crew.subagent`` helpers and manager bookkeeping.

Focused on the module-level probes (memory / CPU / cgroup readers, env
parsing, agent + governance vetting) and the ``SubagentManager`` bookkeeping
surfaces — slot accounting, pid and cost records, stall detection, read
surfaces, queue depth, the stagger gate — none of which need a real agent
process. Every process boundary is stubbed: no test here launches a
binary, opens a socket, or writes outside ``tmp_path`` / the autouse isolated
``KIROCREW_HOME``.

What a finished run delivers is covered in ``test_subagent_coverage_completion.py``;
steering, cancelling and reaping a live run in ``test_subagent_coverage_live_runs.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_coverage_helpers import _info, _manager

from conftest import absent_sysconf
from kiro_crew import subagent as sa
from kiro_crew.subagent import SubagentInfo, SubagentManager

# ── _safe_fire ────────────────────────────────────────────────────────────


class TestSafeFire:
    @pytest.mark.asyncio
    async def test_runs_coroutine_and_drops_ref(self) -> None:
        seen: list[str] = []

        async def _work() -> None:
            seen.append("ran")

        sa._safe_fire(_work())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == ["ran"]

    @pytest.mark.asyncio
    async def test_swallows_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        reached: list[str] = []

        async def _boom() -> None:
            reached.append("raised")
            raise RuntimeError("nope")

        with caplog.at_level("WARNING", logger="kiro_crew.subagent"):
            sa._safe_fire(_boom())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert reached == ["raised"]
        warnings = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.subagent"]
        assert "Subagent callback failed" in warnings


# ── _validate_agent ───────────────────────────────────────────────────────


class TestValidateAgent:
    def test_empty_request_is_the_default(self) -> None:
        assert sa._validate_agent("") == ("", "", "")

    def test_known_user_agent_accepted(self) -> None:
        with patch.object(sa, "list_agents", return_value=[SimpleNamespace(name="scout")]):
            assert sa._validate_agent("scout") == ("scout", "", "")

    def test_unknown_agent_refused_not_defaulted(self) -> None:
        with patch.object(sa, "list_agents", return_value=[SimpleNamespace(name="scout")]):
            name, err, code = sa._validate_agent("typo")
        assert name == ""
        assert "not found" in err
        assert code == sa.AGENT_NOT_FOUND_CODE

    def test_project_scope_widens_known_set(self, tmp_path: Path) -> None:
        with (
            patch.object(sa, "list_agents", return_value=[]),
            patch.object(sa, "cached_project_agent_names", return_value=frozenset({"local"})),
        ):
            assert sa._validate_agent("local", str(tmp_path)) == ("local", "", "")

    def test_project_scope_cold_cache_refuses(self, tmp_path: Path) -> None:
        with (
            patch.object(sa, "list_agents", return_value=[]),
            patch.object(sa, "cached_project_agent_names", return_value=None),
        ):
            name, err, code = sa._validate_agent("local", str(tmp_path))
        assert (name, "not found" in err, code) == ("", True, sa.AGENT_NOT_FOUND_CODE)


# ── _vet_spawn_governance ─────────────────────────────────────────────────


def _verdict(permitted: bool, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(permitted=permitted, reason=reason)


class TestVetSpawnGovernance:
    def test_permitted_returns_none(self) -> None:
        with patch(
            "kiro_crew.platform.governance_profiles.governance_permits",
            return_value=_verdict(True),
        ):
            assert sa._vet_spawn_governance("dash:1", "scout") is None

    def test_capability_disabled_returns_reason(self) -> None:
        with patch(
            "kiro_crew.platform.governance_profiles.governance_permits",
            return_value=_verdict(False, "spawn off for this app"),
        ):
            assert sa._vet_spawn_governance("dash:1", "", app="notes") == "spawn off for this app"

    def test_agent_scope_denied(self) -> None:
        calls: list[str] = []

        def _permits(_scope: str, item: str = "", **_kw: object) -> SimpleNamespace:
            calls.append(item)
            return _verdict(not item.startswith("agents:"))

        with patch(
            "kiro_crew.platform.governance_profiles.governance_permits", side_effect=_permits
        ):
            reason = sa._vet_spawn_governance("dash:1", "scout")
        assert reason == "agent 'scout' not permitted by spawn policy"
        assert "agents:scout" in calls

    def test_composition_error_propagates(self) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with patch(
            "kiro_crew.platform.governance_profiles.governance_permits",
            side_effect=PlatformCompositionError("bad platform"),
        ):
            with pytest.raises(PlatformCompositionError):
                sa._vet_spawn_governance("dash:1", "scout")

    def test_other_error_fails_closed_and_audits(self) -> None:
        audit = MagicMock()
        with (
            patch(
                "kiro_crew.platform.governance_profiles.governance_permits",
                side_effect=RuntimeError("store down"),
            ),
            patch(
                "kiro_crew.platform.governance_profiles.audit_governance_degraded",
                audit,
            ),
        ):
            reason = sa._vet_spawn_governance("dash:1", "scout")
        assert reason is not None
        assert "fail-closed" in reason
        assert audit.call_count == 1

    def test_audit_failure_still_fails_closed(self) -> None:
        with (
            patch(
                "kiro_crew.platform.governance_profiles.governance_permits",
                side_effect=RuntimeError("store down"),
            ),
            patch(
                "kiro_crew.platform.governance_profiles.audit_governance_degraded",
                side_effect=RuntimeError("audit down"),
            ),
        ):
            assert sa._vet_spawn_governance("dash:1", "scout") is not None


# ── _done_result / _timeout_context ───────────────────────────────────────


class TestDoneResult:
    def test_empty_stays_empty(self) -> None:
        assert sa._done_result("") == ""

    def test_short_text_passes_through(self) -> None:
        assert sa._done_result("all good") == "all good"

    def test_oversize_truncates_from_the_head(self) -> None:
        out = sa._done_result("x" * (sa._MAX_DONE_RESULT_LEN + 500))
        assert out.startswith("…(truncated)\n")
        assert len(out) == sa._MAX_DONE_RESULT_LEN + len("…(truncated)\n")


class TestTimeoutContext:
    def test_elapsed_falls_back_to_wall_clock(self) -> None:
        info = _info(turns=1, max_turns=5, started=0.0)
        assert "elapsed:" in sa._timeout_context(info)


# ── env parsing ───────────────────────────────────────────────────────────


class TestEnvFloat:
    def test_absent_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_TEST_FLOAT", raising=False)
        assert sa._env_float("KIROCREW_TEST_FLOAT", 7.5) == 7.5

    def test_valid_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_TEST_FLOAT", "12.25")
        assert sa._env_float("KIROCREW_TEST_FLOAT", 7.5) == 12.25

    @pytest.mark.parametrize("raw", ["0", "-3", "abc"])
    def test_invalid_or_nonpositive_falls_back(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_TEST_FLOAT", raw)
        assert sa._env_float("KIROCREW_TEST_FLOAT", 7.5) == 7.5


class TestResolveInjectionTimeout:
    def test_clamped_to_outer_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", str(sa._ON_DONE_TIMEOUT * 10))
        assert sa._resolve_injection_timeout() == sa._ON_DONE_TIMEOUT

    def test_below_cap_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_INJECTION_TIMEOUT", "60")
        assert sa._resolve_injection_timeout() == 60.0


class TestSubagentRolePins:
    def test_model_pin_returns_normalized_value(self) -> None:
        cfg = MagicMock()
        cfg.agent.role_models = {"subagent": " sonnet "}
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.config.loader.normalize_agent_model", side_effect=str.strip),
        ):
            assert sa._subagent_default_model() == "sonnet"

    def test_model_pin_never_raises(self) -> None:
        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load", side_effect=RuntimeError("no config")
        ):
            assert sa._subagent_default_model() == ""

    @pytest.mark.parametrize(
        "pinned, expected",
        [
            pytest.param("high", "high", id="test_effort_pin_returns_string"),
            pytest.param(3, "", id="test_effort_pin_rejects_non_string"),
        ],
    )
    def test_effort_pin(self, pinned: object, expected: str) -> None:
        cfg = MagicMock()
        cfg.agent.role_efforts = {"subagent": pinned}
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            assert sa._subagent_default_effort() == expected

    def test_effort_pin_never_raises(self) -> None:
        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load", side_effect=RuntimeError("no config")
        ):
            assert sa._subagent_default_effort() == ""


class TestDigestHoldSecs:
    def test_absent_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", raising=False)
        assert sa._digest_hold_secs() == sa._DEFAULT_DIGEST_HOLD_SECS

    def test_nan_is_malformed_not_a_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "nan")
        val = sa._digest_hold_secs()
        assert not math.isnan(val)
        assert val == sa._DEFAULT_DIGEST_HOLD_SECS

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_nonpositive_opts_out(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", raw)
        assert sa._digest_hold_secs() == 0.0

    def test_clamped_to_hard_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", str(sa._TIMEOUT_SECS * 5))
        assert sa._digest_hold_secs() == float(sa._TIMEOUT_SECS)

    def test_valid_value_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "45")
        assert sa._digest_hold_secs() == 45.0


# ── memory probes ─────────────────────────────────────────────────────────


class TestCheckMemoryAvailable:
    def test_path_gate_not_consulted(self, tmp_path: Path) -> None:
        """The fixed kernel path is read with plain open, never the gate."""
        f = tmp_path / "meminfo"
        f.write_text("MemAvailable:    8388608 kB\n")
        with (
            patch("kiro_crew.hooks.safe_read_file", side_effect=AssertionError),
            patch("kiro_crew.hooks.is_sensitive_path", side_effect=AssertionError),
        ):
            assert sa.check_memory_available(min_gb=4.0, path=str(f)) == (True, 8.0)

    @pytest.mark.parametrize(
        "meminfo",
        [
            pytest.param("MemAvailable:  notanumber kB\n", id="test_malformed_line_fails_open"),
            pytest.param("MemTotal: 12 kB\n", id="test_missing_key_fails_open"),
        ],
    )
    def test_unreadable_meminfo_fails_open(self, tmp_path: Path, meminfo: str) -> None:
        f = tmp_path / "meminfo"
        f.write_text(meminfo)
        assert sa.check_memory_available(path=str(f)) == (True, -1.0)


class TestReadIntFile:
    def test_reads_integer(self, tmp_path: Path) -> None:
        path = tmp_path / "n"
        path.write_text("4096\n", newline="\n")
        assert sa._read_int_file(str(path)) == 4096

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert sa._read_int_file(str(tmp_path / "absent")) is None


class TestCgroupAvailable:
    @pytest.fixture(autouse=True)
    def _fixed_cgroup_roots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calculation cases use fixed mounts, independent of the host's /proc."""
        v2 = PurePosixPath("/sys/fs/cgroup")
        v1 = PurePosixPath("/sys/fs/cgroup/memory")
        monkeypatch.setattr(sa, "_cgroup_memory_roots", lambda: [(v2, v2, True), (v1, v1, False)])
        # These cases fabricate only the limit/usage files; the page-cache read
        # goes through ``open`` and would otherwise see the runner's real
        # /sys/fs/cgroup/memory.stat.
        monkeypatch.setattr(sa, "_read_reclaimable_file_bytes", lambda directory, v2: 0)

    def _reader(self, values: dict[str, int | None]):
        return lambda path: values.get(path)

    def test_v1_unlimited_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sa,
            "_read_int_file",
            self._reader({"/sys/fs/cgroup/memory/memory.limit_in_bytes": sa._CGROUP_UNLIMITED}),
        )
        assert sa._cgroup_available_gb() == -1.0

    @pytest.mark.parametrize("usage, expected", [(0, 2.0)])
    def test_usage_unknown_is_distinct_from_zero(
        self, monkeypatch: pytest.MonkeyPatch, usage: int | None, expected: float
    ) -> None:
        gib = 1024**3
        monkeypatch.setattr(
            sa,
            "_read_int_file",
            self._reader(
                {
                    "/sys/fs/cgroup/memory.max": 2 * gib,
                    "/sys/fs/cgroup/memory.current": usage,
                }
            ),
        )
        assert sa._cgroup_available_gb() == expected


class TestAvailableMemoryGb:
    def test_windows_unreadable_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``host_available_mib`` answers 0 for "cannot read", never "no memory"."""
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", False, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", False, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_WINDOWS", True, raising=False)
        monkeypatch.setattr(sa.platform_compat, "host_available_mib", lambda: 0)
        assert sa._available_memory_gb() == -1.0


class TestMacosAvailableMemory:
    """Windows has no ``os.sysconf`` at all, so every patch here is
    ``raising=False`` — the probe's own ``hasattr`` guard is what CI exercises."""

    def test_page_size_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_sysconf = getattr(os, "sysconf", absent_sysconf)

        def fake_sysconf(name):
            if name == "SC_PAGE_SIZE":
                raise ValueError("SC_PAGE_SIZE unavailable")
            return real_sysconf(name)

        monkeypatch.setattr(os, "sysconf", fake_sysconf, raising=False)
        assert sa._macos_available_memory_gb() == -1.0

    def test_nonpositive_page_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_sysconf = getattr(os, "sysconf", absent_sysconf)
        monkeypatch.setattr(
            os, "sysconf", lambda n: 0 if n == "SC_PAGE_SIZE" else real_sysconf(n), raising=False
        )
        assert sa._macos_available_memory_gb() == -1.0

    @pytest.mark.parametrize(
        "pages",
        [
            pytest.param(None, id="test_no_reclaimable_pages"),
            pytest.param(0, id="test_zero_pages_fails_open"),
        ],
    )
    def test_no_usable_pages_fails_open(
        self, monkeypatch: pytest.MonkeyPatch, pages: int | None
    ) -> None:
        real_sysconf = getattr(os, "sysconf", absent_sysconf)
        monkeypatch.setattr(
            os,
            "sysconf",
            lambda n: 4096 if n == "SC_PAGE_SIZE" else real_sysconf(n),
            raising=False,
        )
        monkeypatch.setattr(sa, "_macos_vm_reclaimable_pages", lambda: pages)
        assert sa._macos_available_memory_gb() == -1.0

    def test_computes_gb_from_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_sysconf = getattr(os, "sysconf", absent_sysconf)
        monkeypatch.setattr(
            os,
            "sysconf",
            lambda n: 4096 if n == "SC_PAGE_SIZE" else real_sysconf(n),
            raising=False,
        )
        monkeypatch.setattr(sa, "_macos_vm_reclaimable_pages", lambda: 262144)
        assert sa._macos_available_memory_gb() == pytest.approx(1.0)


# ── CPU probes ────────────────────────────────────────────────────────────


class TestCpuJiffies:
    """``_subtree_cpu_jiffies`` — the Sessions rows' CPU reading.

    The per-process reads and the walk itself belong to
    ``platform_compat.proc_subtree_sample`` and are pinned in
    ``test_proc_subtree_sample.py``; what is subagent's own is that this wrapper
    asks for the CPU column of that walk.
    """

    def test_subtree_skips_already_seen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa.platform_compat, "_proc_cpu_jiffies", lambda pid: 5)
        monkeypatch.setattr(
            sa.platform_compat, "_proc_children", lambda pid: [1] if pid == 1 else []
        )
        assert sa._subtree_cpu_jiffies(1) == 5


# ── validate_cwd / resolve_max_subagents ──────────────────────────────────


class TestValidateCwdEdges:
    def test_resolution_failure_reported(self, tmp_path: Path) -> None:
        with patch.object(os.path, "realpath", side_effect=OSError("boom")):
            resolved, err = sa.validate_cwd(str(tmp_path), [str(tmp_path)])
        assert resolved == ""
        assert "cwd resolution failed" in err

    def test_realpath_agreement_on_both_sides(self, tmp_path: Path) -> None:
        # realpath BOTH sides: on Windows tmp_path is the 8.3 short form while
        # validate_cwd returns the long form, so a raw compare passes only on POSIX.
        resolved, err = sa.validate_cwd(str(tmp_path), [str(tmp_path)])
        assert err == ""
        assert os.path.realpath(resolved) == os.path.realpath(str(tmp_path))


class TestResolveMaxSubagents:
    def test_broken_config_falls_back_to_legacy(self) -> None:
        cfg = SimpleNamespace(agent=SimpleNamespace())
        assert sa.resolve_max_subagents(cfg) == sa._LEGACY_DEFAULT_MAX  # type: ignore[arg-type]

    def test_non_numeric_pin_falls_back(self) -> None:
        cfg = SimpleNamespace(agent=SimpleNamespace(max_subagents="lots"))
        assert sa.resolve_max_subagents(cfg) == sa._LEGACY_DEFAULT_MAX  # type: ignore[arg-type]


# ── SubagentInfo surface ──────────────────────────────────────────────────


class TestOutcome:
    def test_error_is_failed(self) -> None:
        assert _info(error="boom").outcome == "failed"

    def test_clean_is_completed(self) -> None:
        assert _info().outcome == "completed"


class TestContextGroups:
    def test_all_groups_on(self) -> None:
        groups = sa._context_groups_of(_info())
        assert groups == frozenset(
            {sa.CONTEXT_GROUP_MEMORY, sa.CONTEXT_GROUP_LESSONS, sa.CONTEXT_GROUP_PROJECT}
        )

    def test_withheld_group_absent(self) -> None:
        info = _info(include_memory=False)
        assert sa.CONTEXT_GROUP_MEMORY not in sa._context_groups_of(info)

    def test_field_is_sorted_and_comma_joined(self) -> None:
        field = sa._context_groups_field(_info(include_project=False))
        assert field == ",".join(sorted(field.split(",")))
        assert sa.CONTEXT_GROUP_PROJECT not in field.split(",")

    def test_all_withheld_is_empty_string(self) -> None:
        info = _info(include_memory=False, include_lessons=False, include_project=False)
        assert sa._context_groups_field(info) == ""


# ── Manager: orphan / pid helpers ─────────────────────────────────────────


class TestPidHelpers:
    def test_is_pid_alive_delegates_to_platform(self) -> None:
        with patch.object(sa.platform_compat, "pid_exists", return_value=True) as probe:
            assert SubagentManager._is_pid_alive(4242) is True
        probe.assert_called_once_with(4242)

    def test_orphan_check_false_for_missing_proc(self) -> None:
        with patch.object(os, "stat", side_effect=FileNotFoundError):
            assert SubagentManager._is_orphan_process(4242, 100.0) is False

    def test_orphan_check_true_when_created_before_spawn(self) -> None:
        with patch.object(os, "stat", return_value=SimpleNamespace(st_ctime=99.0)):
            assert SubagentManager._is_orphan_process(4242, 100.0) is True

    def test_orphan_check_false_for_recycled_pid(self) -> None:
        with patch.object(os, "stat", return_value=SimpleNamespace(st_ctime=500.0)):
            assert SubagentManager._is_orphan_process(4242, 100.0) is False

    @pytest.mark.asyncio
    async def test_kill_orphan_swallows_missing_process(self) -> None:
        with patch.object(sa.platform_compat, "kill_pid", side_effect=ProcessLookupError) as kill:
            # An already-gone process is not a kill failure.
            assert await SubagentManager._kill_orphan_pid(4242) is None
        assert kill.call_args[0][0] == 4242

    @pytest.mark.asyncio
    async def test_kill_orphan_calls_platform_kill(self) -> None:
        with patch.object(sa.platform_compat, "kill_pid") as kill:
            await SubagentManager._kill_orphan_pid(4242)
        assert kill.call_args[0][0] == 4242


class TestLiveSharedCount:
    def test_no_pid_counts_as_one(self) -> None:
        assert _manager()._live_shared_count(None, []) == 1

    def test_counts_live_sharers_on_the_same_pid(self) -> None:
        mgr = _manager()
        for idx in range(3):
            info = _info(f"s{idx}")
            info._session_sharing = True
            info._pid = 777
            mgr._agents[info.id] = info
        dead = _info("dead", done=True)
        dead._session_sharing = True
        dead._pid = 777
        mgr._agents["dead"] = dead
        assert mgr._live_shared_count(777, list(mgr._agents.values())) == 3

    def test_unknown_pid_floors_at_one(self) -> None:
        mgr = _manager()
        assert mgr._live_shared_count(31337, list(mgr._agents.values())) == 1

    def test_snapshot_is_required_not_read_from_the_live_registry(self) -> None:
        """The sole caller runs off-loop, so the count must never touch
        ``self._agents``: it can only see what the caller snapshotted."""
        mgr = _manager()
        info = _info("s0")
        info._session_sharing = True
        info._pid = 777
        mgr._agents[info.id] = info
        assert mgr._live_shared_count(777, []) == 1, "an empty snapshot means no sharers seen"
        with pytest.raises(TypeError):
            mgr._live_shared_count(777)  # type: ignore[call-arg]


class TestAttributedCount:
    def test_unmeasured_keeps_the_last_good_reading(self) -> None:
        assert sa._attributed_count(None, 1, 7) == 7

    def test_sole_tenant_reports_the_raw_count(self) -> None:
        assert sa._attributed_count(18, 1, None) == 18

    def test_splits_across_sharers(self) -> None:
        assert sa._attributed_count(18, 3, None) == 6

    def test_rounds_to_a_whole_process(self) -> None:
        assert sa._attributed_count(19, 3, None) == 6

    def test_a_nonzero_count_never_reads_as_zero(self) -> None:
        assert sa._attributed_count(1, 6, None) == 1

    def test_zero_stays_zero(self) -> None:
        """Pooling off is a real zero, not a floor-to-one."""
        assert sa._attributed_count(0, 3, None) == 0


class TestSampleLiveCounts:
    """The sweep must write procs/mcp on both the shared and exclusive paths."""

    def _patched(self, counts: tuple[int, int]):
        sample = sa.platform_compat.SubtreeSample(1024 * 1024, 0, counts[0], counts[1])
        return (patch.object(sa, "_proc_subtree_sample", return_value=sample),)

    def test_shared_runtime_counts_are_split_per_sharer(self) -> None:
        mgr = _manager()
        for idx in range(3):
            info = _info(f"s{idx}")
            info._session_sharing = True
            info._pid = 777
            mgr._agents[info.id] = info
        (sample,) = self._patched((21, 18))
        with sample:
            mgr._sample_live_costs()
        for info in mgr._agents.values():
            assert info.last_procs == 7
            assert info.last_stubs == 6

    def test_exclusive_process_counts_are_not_split(self) -> None:
        mgr = _manager()
        info = _info("solo")
        info._pid = 999
        mgr._agents["solo"] = info
        (sample,) = self._patched((7, 6))
        with sample:
            mgr._sample_live_costs()
        assert info.last_procs == 7
        assert info.last_stubs == 6

    def test_unmeasurable_sweep_does_not_blank_a_prior_reading(self) -> None:
        mgr = _manager()
        info = _info("solo")
        info._pid = 999
        info.last_procs, info.last_stubs = 7, 6
        mgr._agents["solo"] = info
        with patch.object(
            sa,
            "_proc_subtree_sample",
            return_value=sa.platform_compat.SubtreeSample(-1, 0, None, None),
        ):
            mgr._sample_live_costs()
        assert (info.last_procs, info.last_stubs) == (7, 6)

    def test_one_walk_per_agent_not_one_per_metric(self) -> None:
        """The whole point: RSS, CPU and both counts come off ONE walk.

        Patching the shared sample is not enough to prove that — a sweep that
        still called three readers would also pass the assertions above. Count
        the walks instead.
        """
        mgr = _manager()
        for idx in range(2):
            info = _info(f"s{idx}")
            info._pid = 900 + idx
            mgr._agents[info.id] = info
        walks: list[object] = []

        def _sample(pid, **kwargs):  # noqa: ANN001, ANN003 — test double
            walks.append(pid)
            return sa.platform_compat.SubtreeSample(1024, 0, 4, 2)

        with patch.object(sa, "_proc_subtree_sample", side_effect=_sample):
            mgr._sample_live_costs()
        assert walks == [900, 901], "exactly one subtree walk per live agent"

    def test_sweep_reads_the_registry_once_so_it_is_thread_safe(self) -> None:
        """The sweep runs on an executor thread, so it must not iterate the live
        registry lazily: a spawn or eviction landing mid-sweep would raise
        ``RuntimeError: dictionary changed size during iteration``."""
        mgr = _manager()
        for idx in range(2):
            info = _info(f"s{idx}")
            info._session_sharing = True
            info._pid = 777
            mgr._agents[info.id] = info

        reads = {"n": 0}
        real = mgr._agents

        class _CountingRegistry(dict):
            def values(self):  # type: ignore[override]
                reads["n"] += 1
                return super().values()

        mgr._agents = _CountingRegistry(real)  # type: ignore[assignment]
        (sample,) = self._patched((4, 2))
        with sample:
            mgr._sample_live_costs()
        assert reads["n"] == 1, "the registry must be snapshotted exactly once"


class TestReaperOffloadsTheSweep:
    @pytest.mark.asyncio
    async def test_sample_runs_on_the_maintenance_executor(self) -> None:
        """``_sample_live_costs`` walks ``/proc`` once per live agent, so the reaper
        must hand it to an executor rather than block the loop the chat turns and
        heartbeats share."""
        mgr = _manager()
        seen: list[object] = []

        def _capture(executor, fn, *args):  # noqa: ANN001 — test double
            seen.append(executor)
            fn(*args)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            fut.set_result(None)
            return fut

        with (
            patch.object(sa, "_REAPER_INTERVAL", 0),
            patch.object(sa, "compact_cost_log"),
            patch.object(sa, "maintenance_executor", return_value="mc-maint") as pool,
            patch.object(asyncio.get_running_loop(), "run_in_executor", side_effect=_capture),
            patch.object(mgr, "_sample_live_costs") as sample,
            patch.object(mgr, "_rebuild_conversation_registry", new=AsyncMock()),
        ):
            task = asyncio.ensure_future(mgr._reaper_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert pool.called, "the sweep must be offloaded to the maintenance pool"
        assert seen and seen[0] == "mc-maint"
        assert sample.called


class TestRecordCost:
    def test_unsampled_run_records_nothing(self) -> None:
        mgr = _manager()
        info = _info()
        with patch.object(sa, "append_cost_sample") as append:
            mgr._record_cost(info)
            append.assert_not_called()
            # The same run records once a sweep has sampled it, so the skip
            # above was the never-sampled gate and not a dead path.
            info.peak_cpu_cores = 0.5
            mgr._record_cost(info)
        assert append.call_count == 1

    def test_sampled_run_appends(self) -> None:
        mgr = _manager()
        info = _info(agent="scout")
        info.peak_rss_gb = 1.5
        info.peak_cpu_cores = 0.75
        with patch.object(sa, "append_cost_sample") as append:
            mgr._record_cost(info)
        append.assert_called_once_with("scout", 1.5, 0.75, shared=False, settled_gb=0.0)

    def test_store_failure_is_swallowed(self) -> None:
        mgr = _manager()
        info = _info()
        info.peak_rss_gb = 1.0
        info.settled_rss_gb = 0.5
        mgr._learned_settled_dirty = False
        with patch.object(sa, "append_cost_sample", side_effect=OSError) as append:
            mgr._record_cost(info)
        assert append.call_count == 1
        # A failed append does not mark the learned map for a re-read.
        assert mgr._learned_settled_dirty is False


class TestSlowCommandRecord:
    def test_records_redacted_fields(self) -> None:
        info = _info(last_tool="read /tmp/x", tool_count=4, turns=2)
        with patch.object(sa, "record_slow_command") as rec:
            SubagentManager._record_slow_command(info, 300.0)
        assert rec.call_args.kwargs["idle_secs"] == 300

    def test_write_failure_is_swallowed(self) -> None:
        with patch.object(sa, "record_slow_command", side_effect=OSError) as rec:
            SubagentManager._record_slow_command(_info("slow1"), 1.0)
        assert rec.call_args.args == ("slow1",)
        assert rec.call_args.kwargs["idle_secs"] == 1


class TestWriteTombstone:
    def test_failure_is_swallowed(self) -> None:
        info = _info("ts1")
        info.started -= 5
        with patch.object(sa, "write_tombstone", side_effect=OSError) as write:
            SubagentManager._write_tombstone(info, "reaped")
        assert write.call_args.args == ("ts1",)
        assert write.call_args.kwargs["cause"] == "reaped"
        # The elapsed time is finalized before the write is attempted.
        assert info.elapsed >= 5

    def test_passes_outcome_through(self) -> None:
        with patch.object(sa, "write_tombstone") as write:
            SubagentManager._write_tombstone(_info(user_stopped=True), "user_stop")
        assert write.call_args.kwargs["outcome"] == "stopped"


# ── Manager: stall detection ──────────────────────────────────────────────


class TestStartupStall:
    def test_not_yet_executing_is_never_stalled(self) -> None:
        mgr = _manager(startup_timeout=10)
        assert mgr._is_startup_stalled(_info(), now=1e9) is False

    def test_agent_with_a_turn_is_not_stalled(self) -> None:
        mgr = _manager(startup_timeout=10)
        info = _info(turns=1)
        info._exec_started = 100.0
        assert mgr._is_startup_stalled(info, now=200.0) is False


class TestIdleStall:
    @pytest.mark.asyncio
    async def test_event_failure_does_not_break_flagging(self) -> None:
        async def _on_event(_etype: str, _info: SubagentInfo, _extra: dict) -> None:
            raise RuntimeError("ws down")

        mgr = _manager(stall_idle_secs=10, on_event=_on_event)
        info = _info(turns=1, last_activity=0.0)
        info._stall_suspect_at = 1.0
        with patch.object(sa, "record_slow_command"):
            await mgr._maybe_flag_stall("a1", info, now=200.0)
        assert info.stalled is True

    @pytest.mark.asyncio
    async def test_already_stalled_agent_not_reflagged(self) -> None:
        calls: list[str] = []

        async def _on_event(etype: str, _info: SubagentInfo, _extra: dict) -> None:
            calls.append(etype)

        mgr = _manager(stall_idle_secs=1, on_event=_on_event)
        info = _info(turns=1, last_activity=0.0, stalled=True)
        await mgr._maybe_flag_stall("a1", info, now=1e9)
        assert calls == []


# ── Manager: read surfaces ────────────────────────────────────────────────


class TestReadSurfaces:
    def test_running_agents_for_filters_by_parent(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", parent_session_key="dash:1")
        mgr._agents["b"] = _info("b", parent_session_key="dash:2")
        mgr._agents["c"] = _info("c", parent_session_key="dash:1", done=True)
        rows = mgr.running_agents_for("dash:1")
        assert [r["id"] for r in rows] == ["a"]

    def test_running_agents_for_redacts_task(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info(
            "a", task="use AKIAIOSFODNN7EXAMPLE now", parent_session_key="dash:1"
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in mgr.running_agents_for("dash:1")[0]["task"]

    def test_task_memory_rows_reports_sampled_flag(self) -> None:
        mgr = _manager()
        fresh = _info("fresh", parent_session_key="dash:1")
        sampled = _info("sampled", parent_session_key="dash:1")
        sampled.last_rss_gb = 0.5
        sampled.peak_rss_gb = 0.75
        sampled.last_cpu_cores = 1.234
        sampled._rss_samples = 1
        # A respawned run: the peak survives from the dead process, but THIS
        # process has not been measured yet, so it must render as unmeasured
        # rather than as "0 MB resident".
        respawned = _info("respawned", parent_session_key="dash:1")
        respawned.peak_rss_gb = 0.75
        mgr._agents.update({"fresh": fresh, "sampled": sampled, "respawned": respawned})
        rows = {r["id"]: r for r in mgr.task_memory_rows()}
        assert rows["fresh"]["sampled"] is False
        assert rows["sampled"]["sampled"] is True
        assert rows["sampled"]["rss_mb"] == pytest.approx(512.0)
        assert rows["sampled"]["cpu_cores"] == pytest.approx(1.23)
        assert rows["respawned"]["sampled"] is False
        assert rows["respawned"]["rss_mb"] == 0.0

    def test_task_memory_rows_carry_proc_and_stub_counts(self) -> None:
        """The regression this fixes: the fields were absent, so the Sessions
        surface rendered every subagent as carrying no MCP stubs at all."""
        mgr = _manager()
        fresh = _info("fresh", parent_session_key="dash:1")
        counted = _info("counted", parent_session_key="dash:1")
        counted.last_procs, counted.last_stubs = 7, 6
        mgr._agents.update({"fresh": fresh, "counted": counted})
        rows = {r["id"]: r for r in mgr.task_memory_rows()}
        assert rows["counted"]["procs"] == 7
        assert rows["counted"]["mcp"] == 6
        # Never measured ⇒ null, which the surface renders as an em dash.
        assert rows["fresh"]["procs"] is None
        assert rows["fresh"]["mcp"] is None

    def test_task_memory_rows_skips_done_and_queued(self) -> None:
        mgr = _manager()
        mgr._agents["done"] = _info("done", done=True)
        mgr._agents["queued"] = _info("queued", queued=True)
        assert mgr.task_memory_rows() == []

    def test_task_memory_rows_carry_the_key_a_child_names_as_parent(self) -> None:
        """The System Sessions table nests a nested run under its parent
        task by matching the child's ``parent`` to this key. A continued run
        keeps ``subagent:<original>``, so the key is NOT always ``subagent:<id>``."""
        mgr = _manager()
        mgr._agents["fresh"] = _info("fresh", parent_session_key="dash:1")
        mgr._agents["cont"] = _info(
            "cont", parent_session_key="dash:1", conversation_key="subagent:orig"
        )
        mgr._agents["kid"] = _info("kid", parent_session_key="subagent:orig")
        rows = {r["id"]: r for r in mgr.task_memory_rows()}
        assert rows["fresh"]["session_key"] == "subagent:fresh"
        assert rows["cont"]["session_key"] == "subagent:orig"
        assert rows["kid"]["parent"] == rows["cont"]["session_key"]

    def test_task_memory_rows_redact_before_truncate(self) -> None:
        """A credential straddling the 80-char cut must not leak a fragment.

        The old spelling ``_redact(a.task[:80])`` sliced first, so a key cut at
        the boundary loses its tail and does not match the credential regex —
        the raw prefix escaped into the session-memory surface.
        The fabricated AKIA-shaped literal is inlined rather than bound to a
        ``secret``-named variable, which would trip CodeQL's name-based
        sensitive-source heuristic on this real call path.
        """
        # cut lands 8 chars into the fabricated 20-char key
        task = "x" * 72 + "AKIAIOSFODNN7EXAMPLE" + " trailing"
        mgr = _manager()
        mgr._agents["a"] = _info("a", task=task, parent_session_key="dash:1")
        row = {r["id"]: r for r in mgr.task_memory_rows()}["a"]
        assert "AKIA" not in row["task"]
        assert len(row["task"]) <= 80

    def test_task_memory_rows_plain_task_truncation_unchanged(self) -> None:
        """Ordinary path is result-preserving: no secret ⇒ the same 80-char slice."""
        mgr = _manager()
        mgr._agents["a"] = _info("a", task="t" * 100, parent_session_key="dash:1")
        row = {r["id"]: r for r in mgr.task_memory_rows()}["a"]
        assert row["task"] == "t" * 80

    def test_get_running_all_and_count(self) -> None:
        mgr = _manager()
        live = _info("live")
        mgr._agents.update({"live": live, "gone": _info("gone", done=True)})
        assert mgr.get("live") is live
        assert mgr.get("absent") is None
        assert [a.id for a in mgr.running] == ["live"]
        assert len(mgr.all_agents) == 2
        assert mgr.count == 1

    def test_max_concurrent_and_running_count_properties(self) -> None:
        mgr = _manager(max_concurrent=7)
        mgr._running_count = 4
        assert (mgr.max_concurrent, mgr.running_count) == (7, 4)

    @pytest.mark.asyncio
    async def test_pending_work_count_covers_post_slot_lifecycle_tasks(self) -> None:
        mgr = _manager()
        release = asyncio.Event()
        recovery = asyncio.create_task(release.wait())
        report = asyncio.create_task(release.wait())
        followup = asyncio.create_task(release.wait())
        reconcile = asyncio.create_task(release.wait())
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        mgr._running_count = 0
        mgr._queue = [{"parent_session_key": "dash:1"}]
        mgr._tasks = {"run:recovery": recovery, "done": completed}
        mgr._report_tasks = {report}
        mgr._followup_watchers = {"run": followup}
        mgr._reconcile_task = reconcile
        mgr._abandoned_state_writers = {"state-writer": {report}}
        try:
            # queue + recovery + report + follow-up + reconciliation + writer
            assert mgr.pending_work_count == 6
        finally:
            release.set()
            await asyncio.gather(recovery, report, followup, reconcile)


class TestQueueDepth:
    def test_depth_counts_only_matching_parent(self) -> None:
        mgr = _manager()
        mgr._queue = [
            {"parent_session_key": "dash:1"},
            {"parent_session_key": "dash:1"},
            {"parent_session_key": "dash:2"},
        ]
        assert mgr.queued_count_for("dash:1") == 2
        assert mgr.queued_count_for("dash:9") == 0

    def test_pending_work_sees_queued_agents(self) -> None:
        mgr = _manager()
        mgr._queue = [{"parent_session_key": "dash:1"}]
        assert mgr.has_pending_work_for("dash:1") is True

    def test_pending_work_sees_running_agents(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", parent_session_key="dash:1")
        assert mgr.has_pending_work_for("dash:1") is True

    def test_pending_work_false_when_idle(self) -> None:
        assert _manager().has_pending_work_for("dash:1") is False

    def test_emit_queue_depth_without_loop_is_a_noop(self) -> None:
        on_event = AsyncMock()
        mgr = _manager(on_event=on_event)
        mgr._emit_queue_depth("dash:1", wait={"reason": "capacity"})
        # The wait label is still recorded, but with no running loop no burst
        # is started and nothing is published.
        assert mgr._queue_wait["dash:1"] == {"reason": "capacity"}
        assert "dash:1" not in mgr._queue_depth_emits
        # A request without a label records none.
        mgr._emit_queue_depth("dash:2")
        assert "dash:2" not in mgr._queue_wait
        on_event.assert_not_called()


class TestStaggerGate:
    def test_at_capacity_queues(self) -> None:
        mgr = _manager(max_concurrent=1)
        mgr._running_count = 1
        should_queue, slot_free = mgr._should_stagger_queue(now=1e9)
        assert (should_queue, slot_free) == (True, False)

    @pytest.mark.parametrize(
        "stagger_secs, now, expected",
        [
            pytest.param(100.0, 1001.0, (True, True), id="test_slot_free_but_too_soon_queues"),
            pytest.param(
                1.0, 2000.0, (False, True), id="test_slot_free_and_interval_elapsed_starts"
            ),
        ],
    )
    def test_slot_free_waits_out_the_stagger_interval(
        self, stagger_secs: float, now: float, expected: tuple[bool, bool]
    ) -> None:
        mgr = _manager(max_concurrent=3)
        mgr._spawn_stagger_secs = stagger_secs
        mgr._last_spawn_ts = 1000.0
        assert mgr._should_stagger_queue(now=now) == expected
