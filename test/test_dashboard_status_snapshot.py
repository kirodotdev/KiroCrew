"""Tests for DashboardState.status_snapshot() — shared status payload."""

from __future__ import annotations

import pathlib
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

    def test_slack_connected_reflects_socket_outcome(self, state: DashboardState) -> None:
        # No Slack client wired up (pure-dashboard / Slack disabled).
        assert state.slack_client is None
        assert state.status_snapshot()["slack_connected"] is False
        # Tokens were present at boot (client wired) but the socket connect
        # failed, e.g. invalid_auth or a network error. The badge must NOT show
        # green: slack_client alone only proves tokens existed, not that Socket
        # Mode came up. This is the reported bug (#1770): a green "Connected"
        # over a Slack that never received an event.
        state.slack_client = MagicMock()
        state.slack_socket_connected = False
        assert state.status_snapshot()["slack_connected"] is False
        # Socket Mode actually connected this session.
        state.slack_socket_connected = True
        assert state.status_snapshot()["slack_connected"] is True

    def test_new_fields_propagate_to_all_callers(self, state: DashboardState) -> None:
        """Any field added to status_snapshot is automatically in SSE/WS/API."""
        snap = state.status_snapshot()
        # These keys must exist — if one is missing, a caller will lose it
        required = {
            "uptime",
            "start_time",
            "sessions",
            "messages",
            "cron_jobs",
            "lessons",
            "subagents",
            "update_available",
            "no_crons",
            "slack_connected",
            "branch",
            "commit",
        }
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
        # The default is None, not False: a snapshot taken before any check has run
        # carries NO VERDICT, and defaulting to False is what let the dashboard
        # render "you're on the latest version" for a check that never happened.
        assert state.status_snapshot()["update_available"] is None
        assert state.status_snapshot(update_available=True)["update_available"] is True
        assert state.status_snapshot(update_available=False)["update_available"] is False


class TestAllStatusSnapshotCallersPassTheUpdateFields:
    """Every status emitter must fill the update fields from the shared reader.

    A caller that omits them gets ``update_available=None`` and a dark badge,
    which hides a real update from that transport. One reader
    (``status_update_fields``) is what keeps the two emitters from drifting, so
    the contract is now "you call it", not "the literal kwarg appears".
    """

    def test_the_shared_reader_carries_every_update_field(self) -> None:
        from kiro_crew.dashboard.handlers.updates import status_update_fields

        assert set(status_update_fields()) == {
            "update_available",
            "update_can_apply",
            "update_check_status",
            "update_command",
            "update_latest_version",
            "update_latest_version_display",
            "update_channel",
            "update_channel_move_pending",
            "update_managed_by",
            "update_commits_ahead",
            "update_commits_behind",
            "update_can_arm",
            "update_last_checked_at",
            "update_check_interval_secs",
            "update_required",
            "update_min_version",
            "version_display",
        }

    def test_the_shared_reader_never_flattens_a_missing_verdict(self) -> None:
        from kiro_crew.dashboard.handlers import updates

        original = dict(updates._update_info)
        try:
            updates._update_info.clear()
            assert status_fields_of(updates)["update_available"] is None
            updates._update_info.update({"update_available": False})
            assert status_fields_of(updates)["update_available"] is False
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)

    def test_version_display_folds_the_running_stamp_on_stable_only(self, monkeypatch) -> None:
        """The About page's version chip reads ``version_display`` — the
        RUNNING build's promoted-stamp fold. The raw ``version`` the WS frame
        appends is NOT part of this reader and stays untouched: the SPA
        compares it across pushes to force a reload over a gateway upgrade,
        and folding it would collapse two RCs of the same release into one
        string, masking the very upgrade that comparison exists to catch."""
        from kiro_crew.dashboard.handlers import updates

        original = dict(updates._update_info)
        monkeypatch.setattr(updates, "_local_version", "0.4.0rc14")
        try:
            updates._update_info.clear()
            updates._update_info.update({"channel": "stable", "latest_version": "0.5.0rc3"})
            fields = status_fields_of(updates)
            assert fields["version_display"] == "0.4.0"
            # The candidate's fold rides the same rule; its raw sibling (the
            # snooze/skip and arm key) is untouched.
            assert fields["update_latest_version_display"] == "0.5.0"
            assert fields["update_latest_version"] == "0.5.0rc3"
            updates._update_info.update({"channel": "insider"})
            fields = status_fields_of(updates)
            assert fields["version_display"] == "0.4.0rc14"
            assert fields["update_latest_version_display"] == "0.5.0rc3"
            # Channel not yet resolved (no check has run): raw, never "".
            updates._update_info.clear()
            assert status_fields_of(updates)["version_display"] == "0.4.0rc14"
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)

    def test_latest_version_rides_the_hot_path_as_a_plain_string(self) -> None:
        """The popup keys its per-version snooze/skip on this field, so an
        absent value must read as "" (no candidate), never as None or a stale
        non-string the cache happened to hold."""
        from kiro_crew.dashboard.handlers import updates

        original = dict(updates._update_info)
        try:
            updates._update_info.clear()
            assert status_fields_of(updates)["update_latest_version"] == ""
            updates._update_info.update({"latest_version": "0.5.0"})
            assert status_fields_of(updates)["update_latest_version"] == "0.5.0"
        finally:
            updates._update_info.clear()
            updates._update_info.update(original)

    def test_snapshot_accepts_every_shared_reader_field(self) -> None:
        """Every emitter calls ``status_snapshot(**status_update_fields())``,
        so a key the reader gains that the snapshot's keyword-only signature
        lacks is not a missing feature — it is a TypeError that takes down
        /api/status, the WS status frame, and the SSE stream at once."""
        import inspect

        from kiro_crew.dashboard.handlers.updates import status_update_fields
        from kiro_crew.dashboard.state import DashboardState

        params = inspect.signature(DashboardState.status_snapshot).parameters
        missing = set(status_update_fields()) - set(params)
        assert not missing, (
            f"status_update_fields() emits {sorted(missing)} but "
            "DashboardState.status_snapshot() does not accept them — every "
            "status emitter spreads the reader into the snapshot, so this "
            "crashes all three transports"
        )

    def test_ws_uses_the_shared_reader(self) -> None:
        import inspect

        from kiro_crew.dashboard import ws

        source = inspect.getsource(ws)
        assert "status_update_fields()" in source, (
            "ws.py calls status_snapshot() without the shared update fields — "
            "they default to no-verdict, hiding real availability from WebSocket clients"
        )

    def test_system_api_uses_the_shared_reader(self) -> None:
        import inspect

        from kiro_crew.dashboard import handlers_system

        source = inspect.getsource(handlers_system)
        assert "status_update_fields()" in source

    def test_every_status_snapshot_call_site_uses_the_shared_reader(self) -> None:
        """Named-module checks miss a NEW emitter, which is how one already slipped.

        The SSE stream in ``handlers/updates.py`` read the cache directly, on a key
        this contract had renamed — so it published a hardcoded ``False`` and no test
        noticed, because the two assertions above only look at the two modules that
        were known emitters when they were written. This walks the AST instead: every
        ``status_snapshot(...)`` call anywhere in the package must take its update
        fields from ``status_update_fields()``, so the guard covers emitters nobody
        has written yet.
        """
        import ast
        import pathlib

        # Scoped to the dashboard package on purpose: `DashboardState.status_snapshot`
        # lives here and so does every emitter, while `platform/interfaces.py` defines
        # an UNRELATED `status_snapshot()` on the platform provider that a
        # name-only match would flag.
        root = pathlib.Path(inspect_module_root()) / "dashboard"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not our syntax to fix
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "status_snapshot":
                    continue
                # The fields may arrive as `**status_update_fields()` or as an
                # explicit `update_available=...`; only the shared reader is
                # accepted, because hand-passing one field is how drift starts.
                srcseg = ast.unparse(node)
                if "status_update_fields()" not in srcseg:
                    offenders.append(f"{path.name}:{node.lineno}: {srcseg[:90]}")

        assert not offenders, (
            "these status_snapshot() call sites bypass status_update_fields(), so "
            "their transport reports a stale or missing update verdict:\n  "
            + "\n  ".join(offenders)
        )


def inspect_module_root() -> str:
    """The installed package root, so the walk follows the code under test."""
    import kiro_crew

    return str(pathlib.Path(kiro_crew.__file__).parent)


def status_fields_of(updates_module) -> dict:
    return updates_module.status_update_fields()


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


class TestServedBundleId:
    """The served-bundle hash the SPA compares across status pushes.

    It is what lets a tab reload over a SAME-version rebuild (a git checkout's
    in-app update), which moves neither ``version`` nor ``commit`` — see
    ``DashboardState.served_bundle_id``.
    """

    def test_missing_bundle_reports_empty(self, tmp_path: pathlib.Path) -> None:
        # No built frontend (source tree, unit tests): empty means UNKNOWN to
        # the SPA — never a change, so no reload can fire off it.
        assert DashboardState.served_bundle_id(tmp_path / "absent.html") == ""

    def test_hashes_and_caches_by_stat(self, tmp_path: pathlib.Path) -> None:
        index = tmp_path / "index.html"
        index.write_text("<html>build-one</html>")
        first = DashboardState.served_bundle_id(index)
        assert first and len(first) == 16
        # Same stat → same id, answered from cache (idempotent read).
        assert DashboardState.served_bundle_id(index) == first

    def test_rebuild_changes_id(self, tmp_path: pathlib.Path) -> None:
        # A rebuild rewrites index.html with new hashed asset names — in
        # practice a different length and a later mtime. The cache key is
        # (mtime_ns, size), so model both moving: a same-length rewrite inside
        # one mtime tick is not a case a real `npm run build` can produce.
        import os

        index = tmp_path / "index.html"
        index.write_text("<html>build-one</html>")
        first = DashboardState.served_bundle_id(index)
        index.write_text("<html>build-two, with new hashed asset names</html>")
        st = index.stat()
        os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert DashboardState.served_bundle_id(index) != first

    def test_snapshot_carries_bundle_id(self, state: DashboardState) -> None:
        # The field rides the shared snapshot (WS push + /api/status alike);
        # in this test env there is a real built bundle or there is not — both
        # shapes are legal, but the KEY must be present so the SPA can compare.
        snap = state.status_snapshot()
        assert "bundle_id" in snap
        assert isinstance(snap["bundle_id"], str)
