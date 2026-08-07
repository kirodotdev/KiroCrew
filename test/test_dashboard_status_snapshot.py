"""Tests for DashboardState.status_snapshot() — shared status payload."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    crons = MagicMock()
    crons.list_jobs.return_value = [{"id": "j1"}, {"id": "j2"}]
    lessons = MagicMock()
    lessons.load_all.return_value = [{"rule": "r1"}]
    return DashboardState(
        sessions=MagicMock(count=3),
        crons=crons,
        lessons=lessons,
        start_time=time.time() - 120,
        subagents=MagicMock(count=1),
    )


class TestStatusSnapshot:
    def test_contains_core_fields(self, state: DashboardState) -> None:
        snap = state.status_snapshot()
        assert snap["sessions"] == 3
        assert snap["cron_jobs"] == 2
        assert snap["lessons"] == 1
        assert snap["subagents"] == 1
        assert snap["no_crons"] is False
        assert "uptime" in snap
        assert "start_time" in snap

    def test_no_crons_true(self, state: DashboardState) -> None:
        state.no_crons = True
        assert state.status_snapshot()["no_crons"] is True

    def test_governance_health_field_present(self, state: DashboardState) -> None:
        # the snapshot surfaces governance enforcement health.
        snap = state.status_snapshot()
        assert snap["governance"] in {"active", "degraded", "disabled", "unknown"}

    def test_no_subagents(self, state: DashboardState) -> None:
        state.subagents = None
        assert state.status_snapshot()["subagents"] == 0

    def test_slack_connected_reflects_client(self, state: DashboardState) -> None:
        # No Slack client wired up (pure-dashboard / Slack disabled).
        assert state.slack_client is None
        assert state.status_snapshot()["slack_connected"] is False
        # Gateway wires up a live Slack client once Socket Mode connects.
        state.slack_client = MagicMock()
        assert state.status_snapshot()["slack_connected"] is True

    def test_new_fields_propagate_to_all_callers(self, state: DashboardState) -> None:
        """Any field added to status_snapshot is automatically in SSE/WS/API."""
        snap = state.status_snapshot()
        # These keys must exist — if one is missing, a caller will lose it
        required = {"uptime", "start_time", "sessions", "messages",
                    "cron_jobs", "lessons", "subagents", "update_available",
                    "no_crons", "slack_connected", "branch", "commit"}
        assert required.issubset(snap.keys())

    def test_includes_build_branch_and_commit(self, state: DashboardState) -> None:
        """branch/commit come from the build info resolved at construction."""
        state._build_info = ("beta-braveheart", "abc1234")
        snap = state.status_snapshot()
        assert snap["branch"] == "beta-braveheart"
        assert snap["commit"] == "abc1234"

    def test_build_fields_empty_for_non_git_install(self, state: DashboardState) -> None:
        """Toolbox/pip installs (no source tree) yield empty strings, not missing keys."""
        state._build_info = ("", "")
        snap = state.status_snapshot()
        assert snap["branch"] == ""
        assert snap["commit"] == ""

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.1.4", "stable"),
            ("0.1.4-nightly.20260807t061500", "nightly"),
            ("0.1.4-insider.2", "insider"),
            # PEP 440 spellings — what a CLI/wheel install actually reports,
            # because build-wheel.yml rewrites __version__ to the wheel version.
            ("0.1.4rc4", "insider"),
            ("0.1.4.dev20260807061500", "nightly"),
        ],
    )
    def test_ships_the_resolved_release_channel(
        self, state: DashboardState, monkeypatch, version: str, expected: str
    ) -> None:
        """The dashboard is told the LANE, not left to parse the version itself.

        The prerelease bug-report chip in the header keys off this field, so a
        wrong answer here means a nightly user silently loses their obvious way
        to report a bug — or a stable user gets an affordance implying the build
        is expected to break.
        """
        monkeypatch.setattr("kiro_crew.release_channel.__version__", version)
        assert state.status_snapshot()["release_channel"] == expected

    def test_release_channel_is_always_present(self, state: DashboardState) -> None:
        """Never omitted: the frontend must not have to distinguish absent-from-
        old-gateway from absent-because-stable within one payload version."""
        snap = state.status_snapshot()
        assert snap["release_channel"] in ("nightly", "insider", "stable")

    def test_cached_overrides_skip_expensive_calls(self, state: DashboardState) -> None:
        """Passing cron_jobs/lessons skips list_jobs()/load_all()."""
        state.crons.list_jobs.reset_mock()
        state.lessons.load_all.reset_mock()
        snap = state.status_snapshot(cron_jobs=99, lessons=42)
        assert snap["cron_jobs"] == 99
        assert snap["lessons"] == 42
        state.crons.list_jobs.assert_not_called()
        state.lessons.load_all.assert_not_called()

    def test_update_available_passthrough(self, state: DashboardState) -> None:
        assert state.status_snapshot()["update_available"] is False
        assert state.status_snapshot(update_available=True)["update_available"] is True


class TestAllStatusSnapshotCallersPassUpdateAvailable:
    """Every call to status_snapshot() must pass update_available explicitly."""

    def test_ws_passes_update_available(self) -> None:
        """Regression: ws.py must pass update_available to status_snapshot()."""
        import inspect

        from kiro_crew.dashboard import ws
        source = inspect.getsource(ws)
        assert "update_available=" in source, (
            "ws.py calls status_snapshot() without update_available — "
            "it will default to False, hiding real update availability from WebSocket clients"
        )

    def test_sse_handler_passes_update_available(self) -> None:
        import inspect

        from kiro_crew.dashboard import handlers
        source = inspect.getsource(handlers)
        assert "update_available=" in source

    def test_system_api_passes_update_available(self) -> None:
        import inspect

        from kiro_crew.dashboard import handlers_system
        source = inspect.getsource(handlers_system)
        assert "update_available=" in source


class TestBuildInfoResolution:
    """set_build_info() is the ONLY resolver — build info is never resolved at import.

    Regression (dogfood 2026-07-06): an earlier revision resolved git_build_info()
    at state.py *module import*. Under systemd the entrypoint imports this module
    BEFORE main() detects KIROCREW_PROJECT_DIR, so it resolved with no project dir
    and lru_cache then pinned ("", "") for the process lifetime — the dropdown was
    always blank. The value is now recorded by the CLI gateway entrypoint (sync,
    pre-loop, post-detection) via set_build_info() and only read here.
    """

    def test_setter_flows_into_new_state(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard import state as state_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state_mod.set_build_info(("beta-braveheart", "4f753ed0"))
        try:
            st = DashboardState(
                sessions=MagicMock(count=0),
                crons=MagicMock(),
                lessons=MagicMock(),
                start_time=time.time(),
            )
            assert st._build_info == ("beta-braveheart", "4f753ed0")
        finally:
            state_mod.set_build_info(("", ""))  # restore shared module global

    def test_default_is_empty_when_setter_never_called(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard import state as state_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state_mod.set_build_info(("", ""))  # simulate non-git / not-yet-resolved
        st = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=time.time(),
        )
        assert st._build_info == ("", "")
