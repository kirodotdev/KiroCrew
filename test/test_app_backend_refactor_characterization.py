"""Behaviour the backend's split into supervision owners must not move.

Each case drives a path that crosses what are now two or more owners of
``kiro_crew.apps.backend`` -- the process table and a writer of it, a health verdict
and the MCP transition, the boot wave and the reap -- and pins an invariant the
existing suites leave implicit: a generation-safety check, an ordering, or an
identity guard. Every patch goes through the facade, as the rest of the suite's do,
so a case also fails when a facade write stops reaching the call site it names.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess, HealthProbeOutcome


class _Sel:
    """Records the SEL rows a path writes, without a data home."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    saved = (
        dict(bmod._processes),
        dict(bmod._allocated_ports),
        dict(bmod._restart_attempts),
        dict(bmod._lifecycle_generation),
    )
    for table in (
        bmod._processes,
        bmod._allocated_ports,
        bmod._restart_attempts,
        bmod._lifecycle_generation,
    ):
        table.clear()
    try:
        yield
    finally:
        for table, before in zip(
            (
                bmod._processes,
                bmod._allocated_ports,
                bmod._restart_attempts,
                bmod._lifecycle_generation,
            ),
            saved,
        ):
            table.clear()
            table.update(before)


@pytest.fixture
def sel(monkeypatch: pytest.MonkeyPatch) -> _Sel:
    recorder = _Sel()
    monkeypatch.setattr(bmod, "sel", lambda: recorder)
    return recorder


# ---------------------------------------------------------------------------
# Generation safety: a verdict about a replaced record never lands on its successor
# ---------------------------------------------------------------------------


class TestHealthVerdictsStayOnTheirGeneration:
    def test_the_startup_polls_exhaustion_scrub_spares_a_successor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A restart landing during the LAST startup attempt installs a successor; the
        # retiring poll's scrub is identity-guarded, so it never reaches the MCP gate.
        polled = AppProcess(app_name="gen-app", port=9150)
        successor = AppProcess(app_name="gen-app", port=9151, healthy=True, mcp_healthy=True)
        bmod._processes["gen-app"] = polled
        gated: list[tuple[str, int, bool]] = []

        def probe_and_replace(port: int, health_path: str, **kwargs: Any) -> HealthProbeOutcome:
            bmod._processes["gen-app"] = successor
            return HealthProbeOutcome(None, "connection refused")

        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 1)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_revoke_if_ceiling_closed", lambda *a: "proceed")
        monkeypatch.setattr(bmod, "_health_probe", probe_and_replace)
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: gated.append((name, port, healthy)) or True,
        )
        assert bmod._health_check_loop(polled, "/health") is None
        assert gated == []
        assert (successor.healthy, successor.mcp_healthy) == (True, True)

    def test_a_record_replaced_during_the_enablement_read_is_not_promoted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The identity check is repeated under ``_lock`` AFTER the enabled-state file
        # read, so a stop/start landing in that read cannot be promoted over.
        ap = AppProcess(app_name="race-app", port=9152)
        successor = AppProcess(app_name="race-app", port=9153)
        bmod._processes["race-app"] = ap
        gated: list[bool] = []

        def enabled_then_replaced(name: str) -> bool:
            bmod._processes[name] = successor
            return True

        monkeypatch.setattr(bmod, "_app_enabled_state", enabled_then_replaced)
        monkeypatch.setattr(
            bmod, "_gate_mcp_registration", lambda *a, healthy: gated.append(healthy) or True
        )
        assert bmod._set_backend_health(ap, healthy=True) is False
        assert gated == [] and ap.healthy is False and ap.mcp_healthy is None

    def test_a_steady_healthy_backend_retries_an_unlanded_registration_until_it_lands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``mcp_healthy`` None (a startup reconcile that never landed) behind an
        # unchanged HEALTHY verdict is retried every sweep, and only until it lands.
        ap = AppProcess(app_name="retry-app", port=9154, healthy=True)
        bmod._processes["retry-app"] = ap
        transitions: list[bool] = []
        sweeps = {"n": 0}

        def fake_transition(record: AppProcess, *, healthy: bool) -> bool:
            transitions.append(healthy)
            if len(transitions) == 2:
                record.mcp_healthy = healthy  # the second attempt lands
            return True

        def probe(port: int, health_path: str, **kwargs: Any) -> HealthProbeOutcome:
            sweeps["n"] += 1
            if sweeps["n"] == 4:
                bmod._processes.pop("retry-app")  # ends the watch after this sweep
            return HealthProbeOutcome.answered(200)

        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_revoke_if_ceiling_closed", lambda *a: "proceed")
        monkeypatch.setattr(bmod, "_health_probe", probe)
        monkeypatch.setattr(bmod, "_set_backend_health", fake_transition)
        bmod._watch_backend_health_sweeps(ap, "/health")
        assert sweeps["n"] == 4
        assert transitions == [True, True]


# ---------------------------------------------------------------------------
# The lifecycle table: stops, in-flight starts, reservations
# ---------------------------------------------------------------------------


class TestLifecycleGuards:
    def test_a_stop_for_a_record_that_is_no_longer_tracked_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracked = AppProcess(app_name="exp-app", port=9155)
        bmod._processes["exp-app"] = tracked
        bmod._allocated_ports["exp-app"] = 9155
        bmod._restart_attempts["exp-app"] = 3
        generation = dict(bmod._lifecycle_generation)
        forgot: list[tuple[Any, ...]] = []
        monkeypatch.setattr(bmod, "_forget_app_pid", lambda *a: forgot.append(a))
        monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *a: forgot.append(a))
        stale = AppProcess(app_name="exp-app", port=9156)
        assert bmod.stop_app_backend("exp-app", _expected=stale) is False
        assert bmod._processes["exp-app"] is tracked
        assert bmod._allocated_ports["exp-app"] == 9155
        assert bmod._restart_attempts["exp-app"] == 3
        assert bmod._lifecycle_generation == generation
        assert forgot == []

    def test_a_stop_advances_the_generation_even_with_nothing_tracked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_forget_app_pid", lambda name: None)
        assert bmod.stop_app_backend("idle-app") is False
        assert bmod._lifecycle_generation["idle-app"] == (1, bmod._LIFECYCLE_STOP)

    @pytest.mark.skipif(sys.platform == "win32", reason="flock contention is a POSIX shape")
    def test_a_slow_owner_keeps_its_placeholder_past_the_wait(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The owner still holds the lifecycle flock (provisioning outlives the wait),
        # so the waiter returns None WITHOUT clearing the owner's STARTING placeholder.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        placeholder = AppProcess(app_name="slow-app", starting=True)
        bmod._processes["slow-app"] = placeholder
        with bmod.app_backend_lifecycle_flock("slow-app"):
            assert bmod._await_inflight_spawn("slow-app", timeout=0.2) is None
        assert bmod._processes["slow-app"] is placeholder

    def test_a_fixed_port_held_by_another_app_is_refused_before_any_adoption_probe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A latecomer declaring another app's port must never adopt what is listening
        # there: the claim refuses first, and the incumbent keeps its reservation.
        root = tmp_path / "late-app"
        root.mkdir()
        (root / "server.py").write_text("print('x')\n", encoding="utf-8")
        bmod._allocated_ports["incumbent"] = 9157
        manifest = SimpleNamespace(
            backend=SimpleNamespace(entryPoint="server.py", port="9157", healthCheck="/h")
        )
        monkeypatch.setattr(bmod, "app_dir", lambda name: root)
        monkeypatch.setattr(bmod, "app_execution_denied", lambda *a, **k: None)
        monkeypatch.setattr(bmod, "is_builtin_app", lambda **k: False)

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the adoption path ran for a refused claim")

        monkeypatch.setattr(bmod, "_probe_adoption_health", refuse)
        monkeypatch.setattr(bmod, "_capture_adopted_owners", refuse)
        assert bmod._start_app_backend_body("late-app", manifest) is None
        assert bmod._allocated_ports == {"incumbent": 9157}

    def test_the_ancestry_walk_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A chain root <- 1 <- 2 <- ... : exactly _PID_ANCESTRY_MAX_DEPTH hops count as
        # owned (a sandbox launcher chain), one more does not.
        depth = bmod._PID_ANCESTRY_MAX_DEPTH
        parents = {1000 + i + 1: 1000 + i for i in range(depth + 1)}
        monkeypatch.setattr(bmod.platform_compat, "get_ppid", lambda pid: parents.get(pid, 0))
        assert bmod._pid_is_self_or_descendant_of(1000 + depth, 1000) is True
        assert bmod._pid_is_self_or_descendant_of(1000 + depth + 1, 1000) is False


# ---------------------------------------------------------------------------
# PID identity persistence
# ---------------------------------------------------------------------------


class TestPidfileIdentity:
    def test_a_conditional_forget_keeps_a_recycled_pids_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = tmp_path / "app_backends.pids.json"
        rows = {
            "a-app": {"pid": 10, "start_time": "old", "port": 9160},
            "b-app": {"pid": 11, "start_time": "b", "port": 9161},
        }
        path.write_text(json.dumps(rows), encoding="utf-8")
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: path)
        bmod._forget_app_pid_if("a-app", 10, "new")  # same pid, another process
        assert json.loads(path.read_text(encoding="utf-8")) == rows
        bmod._forget_app_pid_if("a-app", 10, "old")
        assert json.loads(path.read_text(encoding="utf-8")) == {"b-app": rows["b-app"]}

    def test_a_recorded_row_carries_the_spawn_instance_and_the_probed_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = tmp_path / "app_backends.pids.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: path)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        assert bmod._record_app_pid("r-app", 42, 9162, "tok") == "st-42"
        assert bmod._record_app_pid("s-app", 43, 9163) == "st-43"
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "r-app": {"pid": 42, "start_time": "st-42", "port": 9162, "spawn_instance": "tok"},
            "s-app": {"pid": 43, "start_time": "st-43", "port": 9163},
        }


# ---------------------------------------------------------------------------
# Provisioning failures are audited once
# ---------------------------------------------------------------------------


class TestProvisioningFailureAudit:
    @pytest.mark.skipif(sys.platform == "win32", reason="the fixture needs a symlink")
    def test_a_dangling_requirements_link_fails_with_one_log_and_one_sel_row(
        self, tmp_path: Path, sel: _Sel, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "deps-app"
        root.mkdir()
        (root / "requirements.txt").symlink_to(root / "missing.txt")
        with caplog.at_level(logging.ERROR, logger="kiro_crew.apps.backend"):
            error = bmod.provision_app_deps("deps-app", root)
        assert "present but not a readable regular file" in error
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [error]
        assert sel.rows == [
            {
                "caller": "gateway",
                "operation": "app_backend_spawn",
                "outcome": "deps_provision_failed",
                "resources": "deps-app",
            }
        ]

    @pytest.mark.skipif(sys.platform == "win32", reason="the fixture needs a symlink")
    def test_a_linked_data_directory_is_refused_before_anything_is_created(
        self, tmp_path: Path, sel: _Sel
    ) -> None:
        root = tmp_path / "link-app"
        root.mkdir()
        (root / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (root / "data").symlink_to(elsewhere, target_is_directory=True)
        error = bmod.provision_app_deps("link-app", root)
        assert error.startswith("Failed to serialize dependency provisioning for app link-app")
        assert list(elsewhere.iterdir()) == []
        assert [row["outcome"] for row in sel.rows] == ["deps_provision_failed"]


# ---------------------------------------------------------------------------
# The boot wave: order across the reap, the listing, the preclaim and the spawns
# ---------------------------------------------------------------------------


class TestBootOrdering:
    def test_boot_reaps_before_it_lists_any_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        order: list[str] = []
        monkeypatch.setattr(bmod, "_DEV_FLEET_DEFERRED", False)
        monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: order.append("reap") or 0)
        monkeypatch.setattr(bmod, "list_apps", lambda: order.append("list") or [])
        assert bmod.start_enabled_app_backends() == []
        assert order == ["reap", "list"]

    def test_fixed_ports_are_preclaimed_once_before_any_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        monkeypatch.setattr(
            bmod, "_preclaim_fixed_ports", lambda names: order.append(f"preclaim:{names}")
        )

        def start(name: str) -> AppProcess:
            order.append(f"start:{name}")
            return AppProcess(app_name=name, port=9170)

        monkeypatch.setattr(bmod, "start_app_backend", start)
        assert sorted(bmod._start_backends_concurrently(["b1", "b2"])) == ["b1", "b2"]
        assert order[0] == "preclaim:['b1', 'b2']"
        assert sorted(order[1:]) == ["start:b1", "start:b2"]

    def test_an_adopted_healthy_instance_is_registered_once_at_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The adopted record runs no startup poll, so boot registers it itself --
        # through the serialized transition, with the exact record -- and a fresh,
        # not-yet-healthy spawn is left to its own poll.
        adopted = AppProcess(app_name="adopted-app", port=9171, healthy=True)
        fresh = AppProcess(app_name="fresh-app", port=9172)
        records = {"adopted-app": adopted, "fresh-app": fresh}
        registered: list[tuple[AppProcess, bool]] = []
        monkeypatch.setattr(bmod, "_preclaim_fixed_ports", lambda names: None)
        monkeypatch.setattr(bmod, "start_app_backend", lambda name: records[name])
        monkeypatch.setattr(
            bmod,
            "_set_backend_health",
            lambda ap, *, healthy: registered.append((ap, healthy)) or True,
        )
        started = bmod._start_backends_concurrently(["adopted-app", "fresh-app"])
        assert sorted(started) == ["adopted-app", "fresh-app"]
        assert registered == [(adopted, True)]

    def test_a_refused_deferred_spawn_does_not_re_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The deferral is consumed before the re-checks: a Dev Fleet disabled in the
        # window is not spawned later when it comes back.
        spawned: list[list[str]] = []
        monkeypatch.setattr(bmod, "_DEV_FLEET_DEFERRED", True)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(
            bmod, "_start_backends_concurrently", lambda names: spawned.append(names) or names
        )
        assert bmod.start_deferred_app_backends() == []
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        assert bmod.start_deferred_app_backends() == []
        assert spawned == [] and bmod._DEV_FLEET_DEFERRED is False


# ---------------------------------------------------------------------------
# Dependency activation and the path-based (no pinned walk) arm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "volatile"),
    [
        (b"requests==2.31.0", False),
        (b"# -r other.txt", False),
        (b"pkg[extra]>=1; python_version >= '3.10'", False),
        (b"-r base.txt", True),
        (b"-rbase.txt", True),
        (b"--constraint c.txt", True),
        (b"--editable ./lib", True),
        (b"--index-url https://example.invalid/simple", True),
        (b"--extra-index-url https://example.invalid/simple", True),
        (b"--no-index", True),
        (b"  -e ./lib", True),
        (b"git+https://example.invalid/pkg.git", True),
        (b"pkg @ file:vendor", True),
        (b"./local", True),
        (b"C:\\wheels\\pkg.whl", True),
        (b"wheels/pkg.whl", True),
        (b"vendor.tar.gz", True),
        (b"vendor.ZIP", True),
    ],
)
def test_a_requirement_whose_resolution_can_change_disables_the_stamp(
    line: bytes, volatile: bool
) -> None:
    assert bmod._requirements_volatile(b"flask==3.0\n" + line + b"\n") is volatile


class TestDepsActivationGate:
    def _app(self, tmp_path: Path, requirements: bytes = b"requests==2.0\n") -> Path:
        root = tmp_path / "gate-app"
        root.mkdir()
        (root / "requirements.txt").write_bytes(requirements)
        bmod.app_deps_dir(root).mkdir(parents=True)
        return root

    def _mark(self, root: Path, name: str, value: str) -> None:
        (bmod.app_deps_dir(root) / name).write_text(value, encoding="utf-8")

    def test_a_matching_stamp_activates(self, tmp_path: Path) -> None:
        root = self._app(tmp_path)
        self._mark(root, bmod._DEPS_STAMP_NAME, bmod._deps_digest(b"requests==2.0\n"))
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is True

    def test_a_stale_stamp_on_the_right_abi_still_activates(self, tmp_path: Path) -> None:
        # The last good install keeps serving when only the requirements moved.
        root = self._app(tmp_path)
        self._mark(root, bmod._DEPS_STAMP_NAME, "stale")
        self._mark(root, bmod._DEPS_ABI_NAME, bmod._deps_abi_tag())
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is True

    def test_a_foreign_abi_or_a_missing_marker_never_activates(self, tmp_path: Path) -> None:
        root = self._app(tmp_path)
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is False
        self._mark(root, bmod._DEPS_ABI_NAME, "another-interpreter")
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is False

    def test_an_oversized_requirements_file_never_activates(self, tmp_path: Path) -> None:
        big = b"#" * (bmod._DEPS_REQ_MAX_BYTES + 1)
        root = self._app(tmp_path, big)
        self._mark(root, bmod._DEPS_ABI_NAME, bmod._deps_abi_tag())
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is False

    @pytest.mark.skipif(sys.platform == "win32", reason="the fixture needs a symlink")
    def test_links_never_activate(self, tmp_path: Path) -> None:
        root = self._app(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"requests==2.0\n")
        abi = tmp_path / "abi"
        abi.write_text(bmod._deps_abi_tag(), encoding="utf-8")
        # A marker planted as a link reads as absent, even when it names the right tag.
        (bmod.app_deps_dir(root) / bmod._DEPS_ABI_NAME).symlink_to(abi)
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is False
        # A requirements link out of the app root is not the app's requirements.
        (root / "requirements.txt").unlink()
        (root / "requirements.txt").symlink_to(outside)
        (bmod.app_deps_dir(root) / bmod._DEPS_ABI_NAME).unlink()
        self._mark(root, bmod._DEPS_ABI_NAME, bmod._deps_abi_tag())
        assert bmod._deps_tree_stamp_current(root, root / "requirements.txt") is False


class TestPathBasedPinnedDir:
    """The arm a platform without a pinned walk (Windows) takes, on any host."""

    @pytest.fixture(autouse=True)
    def _no_pinned_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.pinned_fs, "supports_pinned_walk", lambda: False)

    def test_a_swapped_directory_is_refused(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        data.mkdir()
        pin = bmod._PinnedDir(data)
        assert pin.fd is None
        pin.verify()
        data.rename(tmp_path / "moved")
        data.mkdir()
        with pytest.raises(OSError, match="replaced mid-provisioning"):
            pin.verify()

    def test_renames_and_removals_go_through_the_identity_check(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        (data / "staging" / "pkg").mkdir(parents=True)
        (data / "stale" / "deep").mkdir(parents=True)
        (data / "loose.txt").write_text("x", encoding="utf-8")
        pin = bmod._PinnedDir(data)
        pin.rename("staging", "live")
        assert (data / "live" / "pkg").is_dir()
        pin.rename_out("loose.txt", tmp_path / "out.txt")
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "x"
        bmod._pinned_remove_entry(pin, data, "stale")
        bmod._pinned_remove_entry(pin, data, "absent")
        assert sorted(p.name for p in data.iterdir()) == ["live"]

    def test_a_linked_data_directory_is_refused_at_pin_time(self, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("the fixture needs a symlink")
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "data").symlink_to(real, target_is_directory=True)
        with pytest.raises(OSError, match="symlink/junction"):
            bmod._PinnedDir(tmp_path / "data")


# ---------------------------------------------------------------------------
# The watch's enforcement of a closed execution ceiling, verdict by verdict
# ---------------------------------------------------------------------------


class TestCeilingRevocationVerdicts:
    """``_revoke_if_ceiling_closed`` answers ``proceed``, ``stopped`` or ``retry``."""

    def _record(self, **fields: Any) -> AppProcess:
        values: dict[str, Any] = {"app_name": "ceil-app", "port": 9180, "gateway_started": True}
        values.update(fields)
        ap = AppProcess(**values)
        bmod._processes["ceil-app"] = ap
        return ap

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
        seen: list[tuple[str, Any]] = []
        monkeypatch.setattr(bmod, "third_party_ceiling_closed", lambda name: "closed")
        monkeypatch.setattr(bmod, "app_execution_denied", lambda name, **kw: "not admitted")
        monkeypatch.setattr(bmod, "_demote", lambda ap, *, reason: seen.append(("demote", ap)))
        monkeypatch.setattr(
            bmod,
            "_retry_mcp_reconcile",
            lambda ap, *, healthy: seen.append(("retry_mcp", healthy)),
        )

        def rebind(ap: AppProcess, health_path: str) -> bool:
            seen.append(("rebind", health_path))
            return True

        monkeypatch.setattr(bmod, "_rebind_adopted_owners", rebind)

        import kiro_crew.apps.bridges as bridges_mod

        monkeypatch.setattr(
            bridges_mod,
            "_deregister_mcp_servers",
            lambda name: seen.append(("scrub", name)) or 1,
        )
        return seen

    def _stop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        calls: list[tuple[str, Any]],
        *,
        result: bool,
        pop: bool,
        successor: AppProcess | None = None,
    ) -> None:
        def fake_stop(name: str, *, _expected: Any = None, _retry_if_serving: Any = None) -> bool:
            calls.append(("stop", (_expected, _retry_if_serving)))
            if pop:
                bmod._processes.pop(name, None)
            if successor is not None:
                bmod._processes[name] = successor
            return result

        monkeypatch.setattr(bmod, "stop_app_backend", fake_stop)

    @pytest.mark.parametrize(
        "case", ["not gateway started", "shipped builtin", "ceiling open", "gate re-admits"]
    )
    def test_a_record_outside_the_ceiling_proceeds_untouched(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]], case: str
    ) -> None:
        ap = self._record(
            gateway_started=case != "not gateway started",
            admitted_builtin=case == "shipped builtin",
        )
        if case == "ceiling open":
            monkeypatch.setattr(bmod, "third_party_ceiling_closed", lambda name: None)
        if case == "gate re-admits":
            monkeypatch.setattr(bmod, "app_execution_denied", lambda name, **kw: None)
        self._stop(monkeypatch, calls, result=True, pop=True)
        assert bmod._revoke_if_ceiling_closed(ap, "/h") == "proceed"
        assert calls == [] and bmod._processes["ceil-app"] is ap

    def test_a_healthy_revoked_backend_is_demoted_stopped_and_scrubbed_by_name(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]
    ) -> None:
        # The demote runs while the record is still tracked; the stop is identity-scoped
        # and asks for the strict reading; an MCP entry the demote did not confirm
        # gone is scrubbed by name once the record is popped.
        ap = self._record(healthy=True, mcp_healthy=True)
        self._stop(monkeypatch, calls, result=True, pop=True)
        assert bmod._revoke_if_ceiling_closed(ap, "/h") == "stopped"
        assert calls == [("demote", ap), ("stop", (ap, "/h")), ("scrub", "ceil-app")]

    def test_an_unconfirmed_entry_is_unwound_before_the_stop(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]
    ) -> None:
        ap = self._record(healthy=False, mcp_healthy=None)
        self._stop(monkeypatch, calls, result=True, pop=True)
        assert bmod._revoke_if_ceiling_closed(ap, "/h") == "stopped"
        assert calls[:2] == [("retry_mcp", False), ("stop", (ap, "/h"))]

    def test_a_stop_that_did_not_take_is_retried_with_rebound_owners(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]
    ) -> None:
        ap = self._record(proc=None, mcp_healthy=False)
        self._stop(monkeypatch, calls, result=False, pop=False)
        assert bmod._revoke_if_ceiling_closed(ap, "/h") == "retry"
        assert calls == [("stop", (ap, "/h")), ("rebind", "/h")]

    def test_a_successor_keeps_the_mcp_entry_it_owns(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]
    ) -> None:
        ap = self._record(mcp_healthy=None)
        successor = AppProcess(app_name="ceil-app", port=9181)
        self._stop(monkeypatch, calls, result=True, pop=True, successor=successor)
        assert bmod._revoke_if_ceiling_closed(ap, "/h") == "stopped"
        assert ("scrub", "ceil-app") not in calls

    def test_a_fault_leaves_the_watch_running(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, Any]]
    ) -> None:
        def boom(name: str) -> str:
            raise RuntimeError("ceiling read failed")

        ap = self._record()
        monkeypatch.setattr(bmod, "third_party_ceiling_closed", boom)
        assert bmod._revoke_if_ceiling_closed(ap, "/h") == "proceed"
