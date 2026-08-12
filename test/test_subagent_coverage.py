"""Coverage tests for ``kiro_crew.subagent`` helpers and manager bookkeeping.

Focused on the module-level probes (memory / CPU / cgroup readers, env
parsing, agent + governance vetting) and the ``SubagentManager`` bookkeeping
surfaces — slot accounting, queue depth, wave submission reconciliation,
digest-hold release, continuable-conversation retention — none of which need a
real agent process. Every process boundary is stubbed: no test here launches a
binary, opens a socket, or writes outside ``tmp_path`` / the autouse isolated
``KIROCREW_HOME``.
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from kiro_crew import subagent as sa
from kiro_crew.subagent import SubagentInfo, SubagentManager

# ── Fixtures / builders ───────────────────────────────────────────────────


def _sessions() -> MagicMock:
    """A SessionManager double with the seams this module touches."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.mark_continuable = MagicMock()
    sessions.forget_conversation = MagicMock(return_value="")
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.is_session_sharing_eligible = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _manager(**kwargs: object) -> SubagentManager:
    """Build a manager with every callback off unless a test wires one."""
    kwargs.setdefault("sessions", _sessions())
    kwargs.setdefault("ctx_builder", None)
    return SubagentManager(**kwargs)  # type: ignore[arg-type]


def _info(agent_id: str = "a1", **kwargs: object) -> SubagentInfo:
    kwargs.setdefault("task", "t")
    return SubagentInfo(id=agent_id, **kwargs)  # type: ignore[arg-type]


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
    async def test_swallows_exception(self) -> None:
        async def _boom() -> None:
            raise RuntimeError("nope")

        sa._safe_fire(_boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Reaching here without an unretrieved-exception crash is the assertion.
        assert True


# ── _validate_agent ───────────────────────────────────────────────────────


class TestValidateAgent:
    def test_empty_request_is_the_default(self) -> None:
        assert sa._validate_agent("") == ("", "")

    def test_known_user_agent_accepted(self) -> None:
        with patch.object(sa, "list_agents", return_value=[SimpleNamespace(name="scout")]):
            assert sa._validate_agent("scout") == ("scout", "")

    def test_unknown_agent_refused_not_defaulted(self) -> None:
        with patch.object(sa, "list_agents", return_value=[SimpleNamespace(name="scout")]):
            name, err = sa._validate_agent("typo")
        assert name == ""
        assert "not found" in err

    def test_project_scope_widens_known_set(self, tmp_path: Path) -> None:
        with (
            patch.object(sa, "list_agents", return_value=[]),
            patch.object(sa, "cached_project_agent_names", return_value=frozenset({"local"})),
        ):
            assert sa._validate_agent("local", str(tmp_path)) == ("local", "")

    def test_project_scope_cold_cache_refuses(self, tmp_path: Path) -> None:
        with (
            patch.object(sa, "list_agents", return_value=[]),
            patch.object(sa, "cached_project_agent_names", return_value=None),
        ):
            name, err = sa._validate_agent("local", str(tmp_path))
        assert (name, "not found" in err) == ("", True)


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
    def test_includes_turn_tool_and_elapsed(self) -> None:
        info = _info(turns=3, max_turns=10, last_tool="read", elapsed=42.0)
        out = sa._timeout_context(info)
        assert "turn 3/10" in out
        assert "last tool: read" in out
        assert "elapsed: 42s" in out

    def test_elapsed_omitted_when_requested(self) -> None:
        info = _info(turns=1, max_turns=5)
        out = sa._timeout_context(info, include_elapsed=False)
        assert "elapsed" not in out
        assert "last tool" not in out

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

    def test_effort_pin_returns_string(self) -> None:
        cfg = MagicMock()
        cfg.agent.role_efforts = {"subagent": "high"}
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            assert sa._subagent_default_effort() == "high"

    def test_effort_pin_rejects_non_string(self) -> None:
        cfg = MagicMock()
        cfg.agent.role_efforts = {"subagent": 3}
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            assert sa._subagent_default_effort() == ""

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
    def test_parses_mem_available(self) -> None:
        text = "MemTotal:       1000 kB\nMemAvailable:    8388608 kB\n"
        with patch.object(sa, "safe_read_file", return_value=text):
            ok, gb = sa.check_memory_available(min_gb=4.0)
        assert ok is True
        assert gb == 8.0

    def test_below_threshold_reports_not_ok(self) -> None:
        text = "MemAvailable:    1048576 kB\n"
        with patch.object(sa, "safe_read_file", return_value=text):
            ok, gb = sa.check_memory_available(min_gb=4.0)
        assert (ok, gb) == (False, 1.0)

    def test_sensitive_path_fails_open(self) -> None:
        with patch.object(sa, "safe_read_file", side_effect=PermissionError):
            assert sa.check_memory_available() == (True, -1.0)

    def test_read_error_fails_open(self) -> None:
        with patch.object(sa, "safe_read_file", side_effect=OSError):
            assert sa.check_memory_available() == (True, -1.0)

    def test_malformed_line_fails_open(self) -> None:
        with patch.object(sa, "safe_read_file", return_value="MemAvailable:  notanumber kB\n"):
            assert sa.check_memory_available() == (True, -1.0)

    def test_missing_key_fails_open(self) -> None:
        with patch.object(sa, "safe_read_file", return_value="MemTotal: 12 kB\n"):
            assert sa.check_memory_available() == (True, -1.0)


class TestProcRssReaders:
    def test_single_proc_parses_vmrss(self) -> None:
        with patch("builtins.open", mock_open(read_data="Name:\tx\nVmRSS:\t 2048 kB\n")):
            assert sa._single_proc_rss_kb(1234) == 2048

    def test_single_proc_missing_returns_minus_one(self) -> None:
        with patch("builtins.open", side_effect=OSError):
            assert sa._single_proc_rss_kb(1234) == -1

    def test_children_listing_unavailable(self) -> None:
        with patch.object(os, "listdir", side_effect=OSError):
            assert sa._proc_children(1234) == []

    def test_children_parsed_per_thread(self) -> None:
        with (
            patch.object(os, "listdir", return_value=["1234"]),
            patch("builtins.open", mock_open(read_data="11 12 13")),
        ):
            assert sa._proc_children(1234) == [11, 12, 13]

    def test_children_garbage_skipped(self) -> None:
        with (
            patch.object(os, "listdir", return_value=["1234"]),
            patch("builtins.open", mock_open(read_data="not-a-pid")),
        ):
            assert sa._proc_children(1234) == []

    def test_subtree_rss_falsy_pid(self) -> None:
        assert sa._proc_rss_kb(None) == -1
        assert sa._proc_rss_kb(0) == -1

    def test_subtree_rss_unreadable_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "_single_proc_rss_kb", lambda _pid: -1)
        assert sa._proc_rss_kb(99) == -1

    def test_subtree_rss_sums_descendants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tree = {1: [2, 3], 2: [4], 3: [], 4: []}
        monkeypatch.setattr(sa, "_single_proc_rss_kb", lambda pid: 100 * pid)
        monkeypatch.setattr(sa, "_proc_children", lambda pid: tree.get(pid, []))
        assert sa._proc_rss_kb(1) == 100 + 200 + 300 + 400

    def test_subtree_rss_ignores_cycles_and_dead_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sa, "_single_proc_rss_kb", lambda pid: 100 if pid == 1 else -1)
        monkeypatch.setattr(sa, "_proc_children", lambda pid: [1, 2] if pid == 1 else [])
        assert sa._proc_rss_kb(1) == 100


class TestReadIntFile:
    def test_reads_integer(self, tmp_path: Path) -> None:
        path = tmp_path / "n"
        path.write_text("4096\n", newline="\n")
        assert sa._read_int_file(str(path)) == 4096

    def test_max_sentinel_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "m"
        path.write_text("max", newline="\n")
        assert sa._read_int_file(str(path)) is None

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert sa._read_int_file(str(tmp_path / "absent")) is None

    def test_garbage_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "g"
        path.write_text("banana", newline="\n")
        assert sa._read_int_file(str(path)) is None


class TestCgroupAvailable:
    def _reader(self, values: dict[str, int | None]):
        return lambda path: values.get(path)

    def test_v2_unlimited_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sa,
            "_read_int_file",
            self._reader({"/sys/fs/cgroup/memory.max": sa._CGROUP_UNLIMITED}),
        )
        assert sa._cgroup_available_gb() == -1.0

    def test_v2_headroom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gib = 1024**3
        monkeypatch.setattr(
            sa,
            "_read_int_file",
            self._reader(
                {
                    "/sys/fs/cgroup/memory.max": 8 * gib,
                    "/sys/fs/cgroup/memory.current": 2 * gib,
                }
            ),
        )
        assert sa._cgroup_available_gb() == pytest.approx(6.0)

    def test_v1_headroom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gib = 1024**3
        monkeypatch.setattr(
            sa,
            "_read_int_file",
            self._reader(
                {
                    "/sys/fs/cgroup/memory/memory.limit_in_bytes": 4 * gib,
                    "/sys/fs/cgroup/memory/memory.usage_in_bytes": 1 * gib,
                }
            ),
        )
        assert sa._cgroup_available_gb() == pytest.approx(3.0)

    def test_v1_unlimited_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sa,
            "_read_int_file",
            self._reader({"/sys/fs/cgroup/memory/memory.limit_in_bytes": sa._CGROUP_UNLIMITED}),
        )
        assert sa._cgroup_available_gb() == -1.0

    def test_no_controller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "_read_int_file", self._reader({}))
        assert sa._cgroup_available_gb() == -1.0

    def test_usage_absent_treated_as_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gib = 1024**3
        monkeypatch.setattr(
            sa, "_read_int_file", self._reader({"/sys/fs/cgroup/memory.max": 2 * gib})
        )
        assert sa._cgroup_available_gb() == pytest.approx(2.0)


class TestAvailableMemoryGb:
    def test_linux_clamped_by_cgroup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", True, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", False, raising=False)
        monkeypatch.setattr(sa, "check_memory_available", lambda min_gb=0.0: (True, 16.0))
        monkeypatch.setattr(sa, "_cgroup_available_gb", lambda: 4.0)
        assert sa._available_memory_gb() == 4.0

    def test_linux_without_cgroup_uses_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", True, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", False, raising=False)
        monkeypatch.setattr(sa, "check_memory_available", lambda min_gb=0.0: (True, 9.0))
        monkeypatch.setattr(sa, "_cgroup_available_gb", lambda: -1.0)
        assert sa._available_memory_gb() == 9.0

    def test_linux_unreadable_propagates_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", True, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", False, raising=False)
        monkeypatch.setattr(sa, "check_memory_available", lambda min_gb=0.0: (True, -1.0))
        assert sa._available_memory_gb() == -1.0

    def test_macos_branch_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", False, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", True, raising=False)
        monkeypatch.setattr(sa, "_macos_available_memory_gb", lambda: 3.5)
        assert sa._available_memory_gb() == 3.5

    def test_unsupported_platform_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa.platform_compat, "IS_LINUX", False, raising=False)
        monkeypatch.setattr(sa.platform_compat, "IS_MACOS", False, raising=False)
        assert sa._available_memory_gb() == -1.0


class TestMacosAvailableMemory:
    """Windows has no ``os.sysconf`` at all, so every patch here is
    ``raising=False`` — the probe's own ``hasattr`` guard is what CI exercises."""

    def test_page_size_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            os, "sysconf", lambda _name: (_ for _ in ()).throw(ValueError), raising=False
        )
        assert sa._macos_available_memory_gb() == -1.0

    def test_nonpositive_page_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "sysconf", lambda _name: 0, raising=False)
        assert sa._macos_available_memory_gb() == -1.0

    def test_no_reclaimable_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "sysconf", lambda _name: 4096, raising=False)
        monkeypatch.setattr(sa, "_macos_vm_reclaimable_pages", lambda: None)
        assert sa._macos_available_memory_gb() == -1.0

    def test_zero_pages_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "sysconf", lambda _name: 4096, raising=False)
        monkeypatch.setattr(sa, "_macos_vm_reclaimable_pages", lambda: 0)
        assert sa._macos_available_memory_gb() == -1.0

    def test_computes_gb_from_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "sysconf", lambda _name: 4096, raising=False)
        monkeypatch.setattr(sa, "_macos_vm_reclaimable_pages", lambda: 262144)
        assert sa._macos_available_memory_gb() == pytest.approx(1.0)


# ── CPU probes ────────────────────────────────────────────────────────────


class TestCpuJiffies:
    def test_parses_utime_plus_stime(self) -> None:
        fields = " ".join(str(i) for i in range(3, 30))
        raw = f"42 (weird name (x)) S {fields}".encode("ascii")
        # After the final ')' the tokens start at 'S'; utime/stime are indices 11/12.
        tokens = raw[raw.rindex(b")") + 2 :].split()
        expected = int(tokens[11]) + int(tokens[12])
        assert sa._parse_cpu_jiffies(raw) == expected

    @pytest.mark.parametrize("raw", [b"", b"no-parens-here", b"1 (x) S 1 2 3"])
    def test_malformed_returns_zero(self, raw: bytes) -> None:
        assert sa._parse_cpu_jiffies(raw) == 0

    def test_proc_cpu_jiffies_missing_pid(self) -> None:
        with patch("builtins.open", side_effect=OSError):
            assert sa._proc_cpu_jiffies(1234) == 0

    def test_proc_cpu_jiffies_reads_stat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "_parse_cpu_jiffies", lambda _raw: 77)
        with patch("builtins.open", mock_open(read_data=b"whatever")):
            assert sa._proc_cpu_jiffies(1234) == 77

    def test_subtree_sums_descendants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tree = {1: [2], 2: [3], 3: []}
        monkeypatch.setattr(sa, "_proc_cpu_jiffies", lambda pid: pid * 10)
        monkeypatch.setattr(sa, "_proc_children", lambda pid: tree.get(pid, []))
        assert sa._subtree_cpu_jiffies(1) == 10 + 20 + 30

    def test_subtree_skips_already_seen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "_proc_cpu_jiffies", lambda pid: 5)
        monkeypatch.setattr(sa, "_proc_children", lambda pid: [1] if pid == 1 else [])
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
    def test_user_stop_is_neutral(self) -> None:
        assert _info(user_stopped=True, error="ignored").outcome == "stopped"

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


# ── Manager: slot + finalize tokens ───────────────────────────────────────


class TestSlotAndFinalizeTokens:
    def test_slot_released_exactly_once(self) -> None:
        mgr = _manager()
        info = _info()
        assert mgr._release_slot(info) is True
        assert mgr._release_slot(info) is False

    def test_finalize_claimed_exactly_once(self) -> None:
        mgr = _manager()
        info = _info()
        assert mgr._claim_finalize(info) is True
        assert mgr._claim_finalize(info) is False

    def test_recovering_withholds_the_claim(self) -> None:
        mgr = _manager()
        info = _info()
        info._recovering = True
        assert mgr._claim_finalize(info) is False
        assert info._finalized is False

    def test_supersede_recovery_takes_claim_and_clears_flag(self) -> None:
        mgr = _manager()
        info = _info()
        info._recovering = True
        assert mgr._claim_finalize(info, supersede_recovery=True) is True
        assert info._recovering is False


class TestCompletionKeepSetter:
    def test_updates_live_values(self) -> None:
        mgr = _manager()
        mgr.update_completion_keep("tail", 1234)
        assert (mgr._completion_keep, mgr._completion_keep_chars) == ("tail", 1234)


class TestApprovalLogging:
    @pytest.mark.asyncio
    async def test_approve_logs_auto_when_reason_present(self) -> None:
        client = AsyncMock()
        event = SimpleNamespace(title="read", tool_kind="fs")
        with patch.object(sa, "sel") as sel_mock:
            await SubagentManager._approve_and_log(
                client, "req-1", "subagent:a1", event, metadata={"reason": "allowlisted"}
            )
        client.approve_tool.assert_awaited_once_with("req-1")
        assert sel_mock().log_tool_invocation.call_args.kwargs["outcome"] == "auto_approved"

    @pytest.mark.asyncio
    async def test_approve_logs_plain_without_reason(self) -> None:
        client = AsyncMock()
        event = SimpleNamespace(title="read", tool_kind="fs")
        with patch.object(sa, "sel") as sel_mock:
            await SubagentManager._approve_and_log(client, 2, "subagent:a1", event)
        assert sel_mock().log_tool_invocation.call_args.kwargs["outcome"] == "approved"

    @pytest.mark.asyncio
    async def test_reject_logs_denied_with_error(self) -> None:
        client = AsyncMock()
        event = SimpleNamespace(title="write", tool_kind="fs")
        with patch.object(sa, "sel") as sel_mock:
            await SubagentManager._reject_and_log(
                client, 3, "subagent:a1", event, error="policy denied"
            )
        client.reject_tool.assert_awaited_once_with(3)
        assert sel_mock().log_tool_invocation.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_reject_logs_rejected_without_error(self) -> None:
        client = AsyncMock()
        event = SimpleNamespace(title="write", tool_kind="fs")
        with patch.object(sa, "sel") as sel_mock:
            await SubagentManager._reject_and_log(client, 4, "subagent:a1", event)
        assert sel_mock().log_tool_invocation.call_args.kwargs["outcome"] == "rejected"


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

    def test_kill_orphan_swallows_missing_process(self) -> None:
        with patch.object(sa.platform_compat, "kill_pid", side_effect=ProcessLookupError):
            SubagentManager._kill_orphan_pid(4242)  # must not raise

    def test_kill_orphan_calls_platform_kill(self) -> None:
        with patch.object(sa.platform_compat, "kill_pid") as kill:
            SubagentManager._kill_orphan_pid(4242)
        assert kill.call_args[0][0] == 4242


class TestLiveSharedCount:
    def test_no_pid_counts_as_one(self) -> None:
        assert _manager()._live_shared_count(None) == 1

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
        assert mgr._live_shared_count(777) == 3

    def test_unknown_pid_floors_at_one(self) -> None:
        assert _manager()._live_shared_count(31337) == 1


class TestRecordCost:
    def test_unsampled_run_records_nothing(self) -> None:
        mgr = _manager()
        with patch.object(sa, "append_cost_sample") as append:
            mgr._record_cost(_info())
        append.assert_not_called()

    def test_sampled_run_appends(self) -> None:
        mgr = _manager()
        info = _info(agent="scout")
        info.peak_rss_gb = 1.5
        info.peak_cpu_cores = 0.75
        with patch.object(sa, "append_cost_sample") as append:
            mgr._record_cost(info)
        append.assert_called_once_with("scout", 1.5, 0.75)

    def test_store_failure_is_swallowed(self) -> None:
        mgr = _manager()
        info = _info()
        info.peak_rss_gb = 1.0
        with patch.object(sa, "append_cost_sample", side_effect=OSError):
            mgr._record_cost(info)  # must not raise


class TestSlowCommandRecord:
    def test_records_redacted_fields(self) -> None:
        info = _info(last_tool="read /tmp/x", tool_count=4, turns=2)
        with patch.object(sa, "record_slow_command") as rec:
            SubagentManager._record_slow_command(info, 300.0)
        assert rec.call_args.kwargs["idle_secs"] == 300

    def test_write_failure_is_swallowed(self) -> None:
        with patch.object(sa, "record_slow_command", side_effect=OSError):
            SubagentManager._record_slow_command(_info(), 1.0)  # must not raise


class TestWriteTombstone:
    def test_failure_is_swallowed(self) -> None:
        with patch.object(sa, "write_tombstone", side_effect=OSError):
            SubagentManager._write_tombstone(_info(), "reaped")  # must not raise

    def test_passes_outcome_through(self) -> None:
        with patch.object(sa, "write_tombstone") as write:
            SubagentManager._write_tombstone(_info(user_stopped=True), "user_stop")
        assert write.call_args.kwargs["outcome"] == "stopped"


# ── Manager: stall detection ──────────────────────────────────────────────


class TestStartupStall:
    def test_not_yet_executing_is_never_stalled(self) -> None:
        mgr = _manager(startup_timeout=10)
        assert mgr._is_startup_stalled(_info(), now=1e9) is False

    def test_runtimeless_past_deadline_is_stalled(self) -> None:
        mgr = _manager(startup_timeout=10)
        info = _info()
        info._exec_started = 100.0
        assert mgr._is_startup_stalled(info, now=200.0) is True

    def test_agent_with_pid_is_not_stalled(self) -> None:
        mgr = _manager(startup_timeout=10)
        info = _info()
        info._exec_started = 100.0
        info._pid = 5
        assert mgr._is_startup_stalled(info, now=200.0) is False

    def test_agent_with_a_turn_is_not_stalled(self) -> None:
        mgr = _manager(startup_timeout=10)
        info = _info(turns=1)
        info._exec_started = 100.0
        assert mgr._is_startup_stalled(info, now=200.0) is False


class TestIdleStall:
    @pytest.mark.asyncio
    async def test_prestart_agent_ignored(self) -> None:
        mgr = _manager(stall_idle_secs=1)
        info = _info(last_activity=0.0)
        await mgr._maybe_flag_stall("a1", info, now=1e9)
        assert info.stalled is False

    @pytest.mark.asyncio
    async def test_awaiting_approval_is_exempt(self) -> None:
        mgr = _manager(stall_idle_secs=1)
        info = _info(turns=1, last_activity=0.0)
        info._awaiting_approval = True
        await mgr._maybe_flag_stall("a1", info, now=1e9)
        assert info.stalled is False

    @pytest.mark.asyncio
    async def test_two_sweep_confirmation_before_flagging(self) -> None:
        events: list[tuple[str, dict]] = []

        async def _on_event(etype: str, _info: SubagentInfo, extra: dict) -> None:
            events.append((etype, extra))

        mgr = _manager(stall_idle_secs=10, on_event=_on_event)
        info = _info(turns=1, last_activity=0.0)
        await mgr._maybe_flag_stall("a1", info, now=100.0)
        assert (info.stalled, info._stall_suspect_at) == (False, 100.0)
        with patch.object(sa, "record_slow_command"):
            await mgr._maybe_flag_stall("a1", info, now=200.0)
        assert info.stalled is True
        assert events and events[0][0] == "subagent_stalled"

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
        mgr._agents.update({"fresh": fresh, "sampled": sampled})
        rows = {r["id"]: r for r in mgr.task_memory_rows()}
        assert rows["fresh"]["sampled"] is False
        assert rows["sampled"]["sampled"] is True
        assert rows["sampled"]["rss_mb"] == pytest.approx(512.0)
        assert rows["sampled"]["cpu_cores"] == pytest.approx(1.23)

    def test_task_memory_rows_skips_done_and_queued(self) -> None:
        mgr = _manager()
        mgr._agents["done"] = _info("done", done=True)
        mgr._agents["queued"] = _info("queued", queued=True)
        assert mgr.task_memory_rows() == []

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
        mgr = _manager(on_event=AsyncMock())
        mgr._emit_queue_depth("dash:1")  # no running loop — advisory event skipped

    @pytest.mark.asyncio
    async def test_emit_queue_depth_fires_event(self) -> None:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        mgr._queue = [{"parent_session_key": "dash:1"}]
        mgr._emit_queue_depth("dash:1", batch_id="w1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == [{"queued": 1}]


class TestStaggerGate:
    def test_at_capacity_queues(self) -> None:
        mgr = _manager(max_concurrent=1)
        mgr._running_count = 1
        should_queue, slot_free = mgr._should_stagger_queue(now=1e9)
        assert (should_queue, slot_free) == (True, False)

    def test_slot_free_but_too_soon_queues(self) -> None:
        mgr = _manager(max_concurrent=3)
        mgr._spawn_stagger_secs = 100.0
        mgr._last_spawn_ts = 1000.0
        assert mgr._should_stagger_queue(now=1001.0) == (True, True)

    def test_slot_free_and_interval_elapsed_starts(self) -> None:
        mgr = _manager(max_concurrent=3)
        mgr._spawn_stagger_secs = 1.0
        mgr._last_spawn_ts = 1000.0
        assert mgr._should_stagger_queue(now=2000.0) == (False, True)


# ── Manager: injection-failure notice ─────────────────────────────────────


class TestNotifyInjectionFailed:
    def test_parent_without_a_tab_is_skipped(self) -> None:
        mgr = _manager(on_event=AsyncMock())
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value=""):
            mgr.notify_injection_failed(_info(parent_session_key="cron:1"))

    @pytest.mark.asyncio
    async def test_queues_failure_for_the_next_turn(self, tmp_path: Path) -> None:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        result = tmp_path / "result.txt"
        result.write_text("hello", newline="\n")
        mgr = _manager(on_event=_on_event)
        info = _info(parent_session_key="dash:1", result_path=str(result))
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen and seen[0]["slot"] == "slot-1"
        assert "Result saved at" in seen[0]["failure_msg"]

    @pytest.mark.asyncio
    async def test_unreadable_result_path_omits_size(self, tmp_path: Path) -> None:
        seen: list[dict] = []

        async def _on_event(_etype: str, _info: SubagentInfo, extra: dict) -> None:
            seen.append(extra)

        mgr = _manager(on_event=_on_event)
        info = _info(parent_session_key="dash:1", result_path=str(tmp_path / "absent.txt"))
        with patch("kiro_crew.dashboard.chat_utils.dashboard_slot_key", return_value="slot-1"):
            mgr.notify_injection_failed(info)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen and "bytes" not in seen[0]["failure_msg"]

    def test_lookup_failure_is_swallowed(self) -> None:
        mgr = _manager(on_event=AsyncMock())
        with patch(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
            side_effect=RuntimeError("no dashboard"),
        ):
            mgr.notify_injection_failed(_info(parent_session_key="dash:1"))


# ── Manager: continuable conversations ────────────────────────────────────


class TestConversationBusy:
    def test_live_run_is_busy(self) -> None:
        mgr = _manager()
        mgr._agents["c1"] = _info("c1")
        busy = mgr._conversation_busy("subagent:c1")
        assert busy is not None and busy.id == "c1"

    def test_queued_continuation_is_busy(self) -> None:
        mgr = _manager()
        mgr._queue = [{"conversation_key": "subagent:c1", "_preassigned_id": "q1"}]
        busy = mgr._conversation_busy("subagent:c1")
        assert busy is not None and busy.queued is True

    def test_queued_without_explicit_key_matches_preassigned_id(self) -> None:
        mgr = _manager()
        mgr._queue = [{"_preassigned_id": "q1"}]
        assert mgr._conversation_busy("subagent:q1") is not None

    def test_idle_conversation_is_not_busy(self) -> None:
        assert _manager()._conversation_busy("subagent:nope") is None


class TestKeepRecordedOnDisk:
    def test_non_subagent_key_short_circuits(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state") as read:
            assert mgr._keep_recorded_on_disk("dash:1") is False
        read.assert_not_called()

    def test_keep_true_recorded(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value={"keep": True}):
            assert mgr._keep_recorded_on_disk("subagent:c1") is True

    def test_missing_state_is_false(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value=None):
            assert mgr._keep_recorded_on_disk("subagent:c1") is False

    def test_read_failure_is_false(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", side_effect=OSError):
            assert mgr._keep_recorded_on_disk("subagent:c1") is False


class TestPromoteConversation:
    def test_writes_all_three_retention_surfaces(self) -> None:
        mgr = _manager()
        with patch.object(sa, "update_state") as update:
            mgr._promote_conversation("c1", "subagent:c1", last_used=1234.0)
        update.assert_called_once_with("c1", keep=True)
        mgr._sessions.mark_continuable.assert_called_once_with("subagent:c1")
        assert mgr._conversations["subagent:c1"] == 1234.0

    def test_state_write_failure_still_registers(self) -> None:
        mgr = _manager()
        with patch.object(sa, "update_state", side_effect=OSError):
            mgr._promote_conversation("c1", "subagent:c1")
        assert "subagent:c1" in mgr._conversations


class TestScanKeepStates:
    def test_missing_base_dir_returns_empty(self, tmp_path: Path) -> None:
        mgr = _manager()
        with patch.object(sa, "_subagents_dir", return_value=tmp_path / "absent"):
            assert mgr._scan_keep_states() == []

    def test_collects_only_keep_runs(self, tmp_path: Path) -> None:
        mgr = _manager()
        (tmp_path / "c1").mkdir()
        (tmp_path / "c2").mkdir()
        (tmp_path / "loose.txt").write_text("x", newline="\n")
        states = {
            "c1": {
                "keep": True,
                "conversation_key": "subagent:c1",
                "session_id": "sid-1",
                "provider": "acp",
                "cwd": str(tmp_path),
                "updated_at": 42.0,
            },
            "c2": {"keep": False},
        }
        with (
            patch.object(sa, "_subagents_dir", return_value=tmp_path),
            patch.object(sa, "read_state", side_effect=lambda name: states.get(name)),
        ):
            rows = mgr._scan_keep_states()
        assert [r[0] for r in rows] == ["c1"]
        assert rows[0][2] == "sid-1"
        assert rows[0][5] == 42.0

    def test_unreadable_entry_is_skipped(self, tmp_path: Path) -> None:
        mgr = _manager()
        (tmp_path / "c1").mkdir()
        with (
            patch.object(sa, "_subagents_dir", return_value=tmp_path),
            patch.object(sa, "read_state", side_effect=OSError),
        ):
            assert mgr._scan_keep_states() == []

    def test_scan_error_returns_empty(self) -> None:
        mgr = _manager()
        with patch.object(sa, "_subagents_dir", side_effect=RuntimeError("no home")):
            assert mgr._scan_keep_states() == []


class TestReleaseConversation:
    def test_busy_conversation_refused(self) -> None:
        mgr = _manager()
        mgr._agents["c1"] = _info("c1")
        ok, detail = mgr.release_conversation("c1")
        assert ok is False
        assert "conversation_busy" in detail

    def test_nothing_to_release(self) -> None:
        mgr = _manager()
        mgr._sessions.forget_conversation.return_value = ""
        with patch.object(sa, "update_state"):
            ok, detail = mgr.release_conversation("c1")
        assert (ok, detail) == (False, "conversation_gone: nothing to release")

    def test_released_deletes_session_files(self) -> None:
        mgr = _manager()
        mgr._conversations["subagent:c1"] = 1.0
        mgr._sessions.forget_conversation.return_value = "sid-1"
        with (
            patch.object(sa, "update_state") as update,
            patch.object(sa, "_cleanup_session_files_sync") as cleanup,
        ):
            ok, detail = mgr.release_conversation("c1")
        assert (ok, detail) == (True, "released")
        update.assert_called_once_with("c1", keep=False)
        cleanup.assert_called_once_with("sid-1", "acp")
        assert "subagent:c1" not in mgr._conversations

    def test_file_cleanup_failure_still_reports_released(self) -> None:
        mgr = _manager()
        mgr._sessions.forget_conversation.return_value = "sid-1"
        with (
            patch.object(sa, "update_state"),
            patch.object(sa, "_cleanup_session_files_sync", side_effect=OSError),
        ):
            assert mgr.release_conversation("c1")[0] is True


class TestSweepConversations:
    def test_fresh_conversation_kept(self) -> None:
        mgr = _manager()
        mgr._conversations["subagent:c1"] = 1000.0
        mgr._sweep_conversations(now=1001.0)
        assert "subagent:c1" in mgr._conversations

    def test_busy_conversation_refreshed_not_released(self) -> None:
        mgr = _manager()
        mgr._conversations["subagent:c1"] = 0.0
        mgr._agents["c1"] = _info("c1")
        now = float(sa._CONVERSATION_TTL_SECS * 3)
        mgr._sweep_conversations(now=now)
        assert mgr._conversations["subagent:c1"] == now

    def test_expired_conversation_released(self) -> None:
        mgr = _manager()
        mgr._conversations["subagent:c1"] = 0.0
        mgr._sessions.forget_conversation.return_value = "sid-1"
        with (
            patch.object(sa, "update_state"),
            patch.object(sa, "_cleanup_session_files_sync"),
        ):
            mgr._sweep_conversations(now=float(sa._CONVERSATION_TTL_SECS * 3))
        assert "subagent:c1" not in mgr._conversations


class TestInheritedContextGroups:
    def test_live_record_wins(self) -> None:
        mgr = _manager()
        mgr._agents["c1"] = _info("c1", include_memory=False)
        assert mgr._inherited_context_groups("c1") == (False, True, True)

    def test_run_predating_the_field_defaults_all_on(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value={}):
            assert mgr._inherited_context_groups("c1") == (True, True, True)

    def test_persisted_scope_is_read_back(self) -> None:
        mgr = _manager()
        raw = f"{sa.CONTEXT_GROUP_LESSONS},{sa.CONTEXT_GROUP_PROJECT}"
        with patch.object(sa, "read_state", return_value={"context_groups": raw}):
            assert mgr._inherited_context_groups("c1") == (False, True, True)

    def test_empty_scope_means_every_group_withheld(self) -> None:
        mgr = _manager()
        with patch.object(sa, "read_state", return_value={"context_groups": ""}):
            assert mgr._inherited_context_groups("c1") == (False, False, False)


# ── Manager: wave / batch bookkeeping ─────────────────────────────────────


class TestBatchMembersPending:
    def test_no_batch_id_is_never_pending(self) -> None:
        assert _manager().batch_members_pending("") is False

    def test_submissions_in_flight_are_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 3]
        assert mgr.batch_members_pending("w1") is True

    def test_live_member_is_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        mgr._agents["a"] = _info("a", batch_id="w1")
        assert mgr.batch_members_pending("w1") is True

    def test_queued_member_is_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        mgr._queue = [{"batch_id": "w1"}]
        assert mgr.batch_members_pending("w1") is True

    def test_closed_wave_not_pending(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        mgr._agents["a"] = _info("a", batch_id="w1", done=True)
        assert mgr.batch_members_pending("w1") is False


class TestFinalizeBatch:
    def test_prunes_every_wave_map(self) -> None:
        mgr = _manager()
        mgr._seen_batches.add("w1")
        mgr._batch_submitted["w1"] = [1, 1]
        mgr._batch_progress_ts["w1"] = 1.0
        mgr.finalize_batch("w1")
        assert "w1" not in mgr._seen_batches
        assert "w1" not in mgr._batch_submitted
        assert "w1" not in mgr._batch_progress_ts

    def test_empty_id_is_a_noop(self) -> None:
        mgr = _manager()
        mgr._seen_batches.add("w1")
        mgr.finalize_batch("")
        assert "w1" in mgr._seen_batches


class TestRecordLostSubmission:
    def test_empty_batch_id_is_a_noop(self) -> None:
        mgr = _manager()
        mgr.record_lost_submission("", 2, "transport error")
        assert mgr._batch_submitted == {}

    @pytest.mark.asyncio
    async def test_counts_submission_and_announces_failure(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        with patch.object(sa, "sel"):
            mgr.record_lost_submission("w1", 3, "POST timed out", parent_session_key="dash:1")
        await asyncio.gather(*mgr._tasks.values())
        assert mgr._batch_submitted["w1"][0] == 1
        assert announced and announced[0].batch_id == "w1"
        assert announced[0].done is True
        assert "spawn submission lost" in announced[0].error

    def test_audit_failure_does_not_block_accounting(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            mgr.record_lost_submission("w1", 2, "boom")
        assert mgr._batch_submitted["w1"][0] == 1

    def test_without_completion_consumer_only_accounting_runs(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel"):
            mgr.record_lost_submission("w1", 2, "boom")
        assert mgr._batch_submitted["w1"] == [1, 2]
        assert mgr._tasks == {}

    def test_negative_total_is_floored_at_zero(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel"):
            mgr.record_lost_submission("w1", -5, "boom")
        assert mgr._batch_submitted["w1"][1] == 0


class TestSweepStuckWaves:
    def test_complete_wave_skipped(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [2, 2]
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=1e9)
        rec.assert_not_called()

    def test_within_grace_window_skipped(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 1000.0
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=1001.0)
        rec.assert_not_called()

    def test_live_member_defers_reconciliation(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 0.0
        mgr._agents["a"] = _info("a", batch_id="w1")
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
        rec.assert_not_called()

    def test_queued_member_defers_reconciliation(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 0.0
        mgr._queue = [{"batch_id": "w1"}]
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
        rec.assert_not_called()

    def test_wedged_wave_reconciled_once(self) -> None:
        mgr = _manager()
        mgr._batch_submitted["w1"] = [1, 2]
        mgr._batch_progress_ts["w1"] = 0.0
        mgr._agents["a"] = _info("a", batch_id="w1", done=True, parent_session_key="dash:1")
        with patch.object(mgr, "record_lost_submission") as rec:
            mgr._sweep_stuck_waves(now=float(sa._WAVE_STUCK_SECS * 3))
        assert rec.call_count == 1
        assert rec.call_args.kwargs["parent_session_key"] == "dash:1"


class TestSweepDigestHolds:
    def test_disabled_deadline_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 0.0)
        mgr = _manager(on_done=AsyncMock())
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1e9)
        flush.assert_not_called()

    def test_no_completion_consumer_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager()
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1e9)
        flush.assert_not_called()

    def test_hold_within_window_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 100.0)
        mgr = _manager(on_done=AsyncMock())
        held = _info("a", batch_id="w1", batch_total=2)
        held._digest_held_at = 1000.0
        mgr._agents["a"] = held
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1050.0)
        flush.assert_not_called()

    def test_closing_wave_not_forced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        held = _info("a", batch_id="w1", batch_total=1, done=True)
        held._digest_held_at = 1.0
        mgr._agents["a"] = held
        mgr._batch_submitted["w1"] = [1, 1]
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1e6)
        flush.assert_not_called()

    def test_expired_hold_forces_partial_flush(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        held = _info("a", batch_id="w1", batch_total=2, done=True, parent_session_key="dash:1")
        held._digest_held_at = 100.0
        newer = _info("b", batch_id="w1", batch_total=2, done=True)
        newer._digest_held_at = 500.0
        mgr._agents.update({"a": held, "b": newer})
        mgr._batch_submitted["w1"] = [1, 2]  # still pending
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1000.0)
        assert flush.call_count == 1
        assert flush.call_args[0][0] == "w1"
        assert flush.call_args[0][1] == "dash:1"
        assert flush.call_args[0][3] == pytest.approx(900.0)  # oldest hold wins

    def test_unheld_members_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sa, "DIGEST_HOLD_SECS", 10.0)
        mgr = _manager(on_done=AsyncMock())
        mgr._agents["a"] = _info("a", batch_id="w1")
        mgr._agents["b"] = _info("b")
        with patch.object(mgr, "force_digest_flush") as flush:
            mgr._sweep_digest_holds(now=1e9)
        flush.assert_not_called()


class TestForceDigestFlush:
    def test_missing_batch_id_is_a_noop(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        mgr.force_digest_flush("", "dash:1", 2, 300.0)
        assert mgr._tasks == {}

    def test_without_completion_consumer_is_a_noop(self) -> None:
        mgr = _manager()
        mgr.force_digest_flush("w1", "dash:1", 2, 300.0)
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_announces_flush_only_record(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        with patch.object(sa, "sel"):
            mgr.force_digest_flush("w1", "dash:1", 3, 300.0)
        await asyncio.gather(*mgr._tasks.values())
        assert announced and announced[0]._digest_flush_only is True
        assert announced[0].batch_id == "w1"
        assert announced[0].batch_total == 3

    @pytest.mark.asyncio
    async def test_audit_failure_still_schedules(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            mgr.force_digest_flush("w1", "dash:1", 1, 1.0)
        await asyncio.gather(*mgr._tasks.values())
        assert announced and announced[0].batch_id == "w1"


class TestAnnounceDigestFlush:
    @pytest.mark.asyncio
    async def test_settles_holds_after_clean_handoff(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        info = _info(batch_id="w1")
        info._digest_settle_ids = ["m1", "m2"]
        with patch.object(sa, "mark_delivered") as mark:
            await mgr._announce_digest_flush(info)
        assert [c[0][0] for c in mark.call_args_list] == ["m1", "m2"]
        assert info._digest_settle_ids == []

    @pytest.mark.asyncio
    async def test_routing_failure_leaves_holds_unsettled(self) -> None:
        mgr = _manager(on_done=AsyncMock(side_effect=RuntimeError("route down")))
        info = _info(batch_id="w1")
        info._digest_settle_ids = ["m1"]
        with patch.object(sa, "mark_delivered") as mark:
            await mgr._announce_digest_flush(info)
        mark.assert_not_called()
        assert info._digest_settle_ids == ["m1"]

    def test_settle_swallows_tombstone_failure(self) -> None:
        mgr = _manager()
        info = _info()
        info._digest_settle_ids = ["m1"]
        with patch.object(sa, "mark_delivered", side_effect=OSError):
            mgr._settle_digest_holds(info)
        assert info._digest_settle_ids == []


class TestAnnounceRejection:
    def test_non_batch_rejection_is_not_announced(self) -> None:
        mgr = _manager(on_done=AsyncMock())
        info = _info(done=True, error="rejected")
        assert mgr._announce_rejection(info) is info
        assert mgr._tasks == {}

    @pytest.mark.asyncio
    async def test_batch_rejection_reaches_the_wave(self) -> None:
        announced: list[SubagentInfo] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append(info)

        mgr = _manager(on_done=_on_done)
        info = _info(done=True, error="rejected", batch_id="w1")
        mgr._announce_rejection(info)
        await asyncio.gather(*mgr._tasks.values())
        assert announced == [info]

    @pytest.mark.asyncio
    async def test_safe_announce_swallows_callback_failure(self) -> None:
        mgr = _manager(on_done=AsyncMock(side_effect=RuntimeError("boom")))
        await mgr._safe_announce(_info())  # must not raise


# ── Manager: session sharing / provider probes ────────────────────────────


class TestShouldUseSessionSharing:
    def test_disabled_by_config(self) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = False
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(_info(parent_session_key="dash:1")) is False

    def test_config_failure_fails_closed(self) -> None:
        mgr = _manager()
        with patch("kiro_crew.subagent.KiroCrewConfig.load", side_effect=RuntimeError):
            assert mgr._should_use_session_sharing(_info(parent_session_key="dash:1")) is False

    @pytest.mark.parametrize(
        "override",
        [{"model": "sonnet"}, {"allowed_tools": ["read"]}, {"bare": True}],
    )
    def test_cc_specific_spawn_excluded(self, override: dict) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = True
        info = _info(parent_session_key="dash:1", **override)
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(info) is False

    def test_parentless_spawn_excluded(self) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = True
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(_info()) is False

    def test_eligible_parent_accepted(self) -> None:
        mgr = _manager()
        cfg = MagicMock()
        cfg.agent.session_sharing = True
        with patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg):
            assert mgr._should_use_session_sharing(_info(parent_session_key="dash:1")) is True


class TestGetParentRuntime:
    def test_no_provider(self) -> None:
        mgr = _manager()
        mgr._sessions.get_provider.return_value = None
        assert mgr._get_parent_runtime("dash:1") is None

    def test_non_acp_session_provider(self) -> None:
        mgr = _manager()
        mgr._sessions.get_provider.return_value = SimpleNamespace(client=object())
        assert mgr._get_parent_runtime("dash:1") is None

    def test_acp_session_provider_yields_runtime(self) -> None:
        mgr = _manager()
        runtime = object()
        inner = MagicMock(spec=sa.AcpSessionProvider)
        inner._runtime = runtime
        mgr._sessions.get_provider.return_value = SimpleNamespace(client=inner)
        assert mgr._get_parent_runtime("dash:1") is runtime


class TestIsCcProvider:
    def test_delegates_to_backend_probe(self) -> None:
        with patch("kiro_crew.providers.acp.is_claude_backend", return_value=True):
            assert SubagentManager._is_cc_provider(object()) is True

    def test_non_claude_backend(self) -> None:
        with patch("kiro_crew.providers.acp.is_claude_backend", return_value=False):
            assert SubagentManager._is_cc_provider(object()) is False


# ── Manager: intentional cancel contract ──────────────────────────────────


class TestCancelTaskIntentionally:
    @pytest.mark.asyncio
    async def test_marked_cancel_leaves_recovery_budget(self) -> None:
        mgr = _manager()
        info = _info(user_stopped=True)

        async def _sleep() -> None:
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_sleep())
        mgr._cancel_task_intentionally(task, info, reason="user_stop")
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True
        assert info._cancel_retry_used is False

    @pytest.mark.asyncio
    async def test_unmarked_cancel_consumes_recovery_budget(self) -> None:
        mgr = _manager()
        info = _info()

        async def _sleep() -> None:
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_sleep())
        mgr._cancel_task_intentionally(task, info, reason="oops")
        await asyncio.gather(task, return_exceptions=True)
        assert info._cancel_retry_used is True

    @pytest.mark.asyncio
    async def test_shutdown_counts_as_a_marker(self) -> None:
        mgr = _manager()
        mgr._shutting_down = True

        async def _sleep() -> None:
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_sleep())
        mgr._cancel_task_intentionally(task, None, reason="shutdown")
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True


class TestCancelLookup:
    @pytest.mark.asyncio
    async def test_unknown_agent_is_false(self) -> None:
        assert await _manager().cancel("nope") is False

    @pytest.mark.asyncio
    async def test_finished_agent_is_false(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", done=True)
        assert await mgr.cancel("a") is False


# ── Manager: steer / follow-up refusals ───────────────────────────────────


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_unknown_run(self) -> None:
        assert await _manager().steer_run("nope", "hi") == (False, "not_found")

    @pytest.mark.asyncio
    async def test_finished_run_points_at_continue(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", done=True)
        ok, detail = await mgr.steer_run("a", "hi")
        assert ok is False
        assert detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_shared_provider_steer_accepted(self) -> None:
        mgr = _manager()
        info = _info("a", parent_session_key="dash:1")
        info._session_sharing = True
        info._shared_provider = MagicMock(steer=AsyncMock(return_value=True))
        mgr._agents["a"] = info
        with patch.object(sa, "sel"):
            assert await mgr.steer_run("a", "hi") == (True, "ok")

    @pytest.mark.asyncio
    async def test_provider_rejection_reported(self) -> None:
        mgr = _manager()
        info = _info("a")
        info._session_sharing = True
        info._shared_provider = MagicMock(steer=AsyncMock(return_value=False))
        mgr._agents["a"] = info
        assert await mgr.steer_run("a", "hi") == (False, "steer rejected by provider")

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_change_the_verdict(self) -> None:
        mgr = _manager()
        info = _info("a")
        info._session_sharing = True
        info._shared_provider = MagicMock(steer=AsyncMock(return_value=True))
        mgr._agents["a"] = info
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            assert await mgr.steer_run("a", "hi") == (True, "ok")

    @pytest.mark.asyncio
    async def test_startup_grace_returns_typed_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sa, "_STEER_STARTUP_WAIT_SECS", 0.05)
        monkeypatch.setattr(sa, "_STEER_STARTUP_POLL_SECS", 0.01)
        mgr = _manager()
        mgr._agents["a"] = _info("a")
        ok, detail = await mgr.steer_run("a", "hi")
        assert ok is False
        assert detail.startswith("session_starting")

    @pytest.mark.asyncio
    async def test_run_finishing_during_the_grace_wait_flips_to_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sa, "_STEER_STARTUP_WAIT_SECS", 1.0)
        monkeypatch.setattr(sa, "_STEER_STARTUP_POLL_SECS", 0.01)
        mgr = _manager()
        info = _info("a")
        mgr._agents["a"] = info

        async def _finish() -> None:
            await asyncio.sleep(0.02)
            info.done = True

        finisher = asyncio.ensure_future(_finish())
        ok, detail = await mgr.steer_run("a", "hi")
        await finisher
        assert ok is False
        assert detail.startswith("not_running")


class TestFollowUpRun:
    @pytest.mark.asyncio
    async def test_unknown_run(self) -> None:
        assert await _manager().follow_up_run("nope", "hi") == (False, "not_found")

    @pytest.mark.asyncio
    async def test_finished_run_refused(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a", done=True)
        ok, detail = await mgr.follow_up_run("a", "hi")
        assert ok is False
        assert detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_shutdown_refuses_rather_than_dropping(self) -> None:
        mgr = _manager()
        mgr._agents["a"] = _info("a")
        mgr._shutting_down = True
        ok, detail = await mgr.follow_up_run("a", "hi")
        assert ok is False
        assert detail.startswith("shutting_down")

    @pytest.mark.asyncio
    async def test_queued_and_watcher_armed_once(self) -> None:
        mgr = _manager()
        info = _info("a")
        mgr._agents["a"] = info
        with (
            patch.object(sa, "sel"),
            patch.object(mgr, "_arm_followup_watcher") as arm,
        ):
            assert await mgr.follow_up_run("a", "first") == (True, "queued")
            info._followup_watcher = True
            assert await mgr.follow_up_run("a", "second") == (True, "queued")
        assert info.pending_followups == ["first", "second"]
        assert arm.call_count == 1

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_lose_the_message(self) -> None:
        mgr = _manager()
        info = _info("a")
        info._followup_watcher = True
        mgr._agents["a"] = info
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            assert await mgr.follow_up_run("a", "hi") == (True, "queued")
        assert info.pending_followups == ["hi"]

    def test_audit_helper_swallows_failure(self) -> None:
        mgr = _manager()
        with patch.object(sa, "sel", side_effect=RuntimeError("sel down")):
            mgr._audit_followup(_info(), "followup_expired")  # must not raise


# ── Cross-platform constants ──────────────────────────────────────────────


class TestPlatformConstants:
    def test_clock_ticks_positive_on_every_platform(self) -> None:
        """``_CLK_TCK`` divides a CPU delta, so a zero/absent value would make
        the cost sampler raise. Windows has no ``os.sysconf``; the module falls
        back to 100 there."""
        assert isinstance(sa._CLK_TCK, int)
        assert sa._CLK_TCK > 0

    def test_subtree_walk_caps_are_bounded(self) -> None:
        assert sa._RSS_SUBTREE_MAX_PROCS > 0
        assert sa._CPU_SUBTREE_MAX_PROCS > 0
