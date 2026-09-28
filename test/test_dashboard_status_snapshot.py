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
        # cron_jobs/lessons are caller-supplied; with no args they are unknown
        # (None), never computed inline on the loop.
        assert snap["cron_jobs"] is None
        assert snap["lessons"] is None
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
        # Mode came up. The bug guarded: a green "Connected"
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
        """Caller-supplied cron_jobs/lessons pass straight through and never
        touch list_jobs()/load_all() — the counts are always caller-supplied."""
        state.crons.list_jobs.reset_mock()
        state.lessons.load_all.reset_mock()
        snap = state.status_snapshot(cron_jobs=99, lessons=42)
        assert snap["cron_jobs"] == 99
        assert snap["lessons"] == 42
        state.crons.list_jobs.assert_not_called()
        state.lessons.load_all.assert_not_called()

    def test_no_counts_emits_none_without_blocking_calls(self, state: DashboardState) -> None:
        """With no counts passed, both keys are None and neither blocking store
        is touched. Fails on the previous head, which fell back to
        ``crons.list_jobs()``/``_count_lessons()`` inline on the event loop —
        the ``no-blocking-call-on-event-loop`` freeze class this path avoids.
        """
        state.crons.list_jobs.side_effect = AssertionError("list_jobs must not run on the loop")
        state._count_lessons = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("_count_lessons must not run on the loop")
        )
        snap = state.status_snapshot()
        assert snap["cron_jobs"] is None
        assert snap["lessons"] is None
        state._count_lessons.assert_not_called()
        state.crons.list_jobs.assert_not_called()

    def test_update_available_passthrough(self, state: DashboardState) -> None:
        # The default is None, not False: a snapshot taken before any check has run
        # carries NO VERDICT, and defaulting to False is what let the dashboard
        # render "you're on the latest version" for a check that never happened.
        assert state.status_snapshot()["update_available"] is None
        assert state.status_snapshot(update_available=True)["update_available"] is True
        assert state.status_snapshot(update_available=False)["update_available"] is False


class TestAllStatusSnapshotCallersPassTheUpdateFields:
    """The status funnel joins the update fields to the snapshot for every emitter.

    A snapshot without the update fields carries ``update_available=None`` and a
    dark badge, hiding a real update from that transport. Rather than trust each
    emitter to spread ``status_update_fields()``, the sole funnel
    (``status_counts.cached_status_snapshot``) reads them itself, so the contract
    is structural — "the only ``status_snapshot`` call lives in the funnel" — not
    "the literal kwarg appears at every call site".
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

    def test_the_funnel_joins_every_update_field_into_the_snapshot(self, state, monkeypatch):
        """``cached_status_snapshot`` surfaces every ``status_update_fields`` key.

        This pins the funnel — the one place the update fields and the cached
        counts are joined — not any caller: it patches the reader to a sentinel
        dict of the real keys and asserts each surfaces with its value in the
        funnel's output. It fails on the previous head only if a caller forgot
        to spread the fields; now that the funnel reads them itself, an emitter
        cannot drop them.
        """
        import asyncio

        from kiro_crew.dashboard import status_counts
        from kiro_crew.dashboard.handlers.updates import status_update_fields

        sentinel = {key: f"S:{key}" for key in status_update_fields()}
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.updates.status_update_fields",
            lambda: dict(sentinel),
        )

        async def _run():
            return await status_counts.cached_status_snapshot(state)

        snap = asyncio.run(_run())
        for key, value in sentinel.items():
            assert snap[key] == value

    def test_every_status_snapshot_call_site_uses_the_shared_reader(self) -> None:
        """Named-module checks miss a NEW emitter, which is how one already slipped.

        The SSE stream in ``handlers/updates.py`` read the cache directly, on a key
        this contract had renamed — so it published a hardcoded ``False`` and no test
        noticed, because the earlier per-emitter checks only looked at the two modules
        that were known emitters when they were written. This walks the AST instead.

        The three emitters now funnel through
        ``status_counts.cached_status_snapshot``, which is the ONE place that
        may call ``DashboardState.status_snapshot`` — and which joins in the
        update fields from ``status_update_fields()`` itself, so no caller has
        to spread them. The structural contract is therefore: exactly ONE
        ``status_snapshot(...)`` call exists in the package, it is the one
        inside ``cached_status_snapshot`` in ``status_counts.py``, and it
        spreads ``status_update_fields()``. A call anywhere else, or a second
        call anywhere in ``status_counts.py``, bypasses the off-loop count
        cache (reintroducing the blocking-call freeze class) or the shared
        update-fields join, so the ratchet counts calls rather than trusting a
        filename.
        """
        import ast
        import pathlib

        # Scoped to the dashboard package on purpose: `DashboardState.status_snapshot`
        # and its sole permitted caller (`status_counts.cached_status_snapshot`) both
        # live here, while `tunnel/manager.py` and `platform/interfaces.py` define and
        # call an UNRELATED `status_snapshot()` on a provider that a whole-tree
        # name match would flag.
        dashboard = pathlib.Path(inspect_module_root()) / "dashboard"
        funnel = ("status_counts.py", "cached_status_snapshot")
        # Every status_snapshot(...) call in the package, with the function that
        # encloses it, so the assertion below can count them and place them.
        calls: list[tuple[str, str, int, str]] = []
        for path in dashboard.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - not our syntax to fix
                continue
            enclosing: dict[int, str] = {}
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for inner in ast.walk(fn):
                        if isinstance(inner, ast.Call):
                            # Innermost function wins: nested defs are walked
                            # after their parent and overwrite its entry.
                            enclosing[id(inner)] = fn.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "status_snapshot":
                    continue
                calls.append(
                    (path.name, enclosing.get(id(node), "<module>"), node.lineno, ast.unparse(node))
                )

        described = "\n  ".join(f"{f}:{ln} in {fn}: {src[:90]}" for f, fn, ln, src in calls)
        assert len(calls) == 1, (
            "exactly one status_snapshot call is permitted, inside "
            "status_counts.cached_status_snapshot; any other bypasses the off-loop "
            "count cache or the shared update reader:\n  " + described
        )
        fname, fn, _lineno, src = calls[0]
        assert (fname, fn) == funnel, (
            "the one status_snapshot call is not the funnel:\n  " + described
        )
        spreads_update_fields = any(
            kw.arg is None
            and isinstance(kw.value, ast.Call)
            and getattr(kw.value.func, "id", getattr(kw.value.func, "attr", ""))
            == "status_update_fields"
            for kw in ast.parse(src, mode="eval").body.keywords
        )
        assert spreads_update_fields, (
            "the funnel's status_snapshot call must spread status_update_fields():\n  " + described
        )


def inspect_module_root() -> str:
    """The installed package root, so the walk follows the code under test."""
    import kiro_crew

    return str(pathlib.Path(kiro_crew.__file__).parent)


def status_fields_of(updates_module) -> dict:
    return updates_module.status_update_fields()


class TestBuildInfoResolution:
    """set_build_info() is the ONLY resolver — build info is never resolved at import.

    Resolving git_build_info() at state.py *module import* is wrong: under systemd
    the entrypoint imports this module BEFORE main() detects KIROCREW_PROJECT_DIR,
    so it resolves with no project dir and lru_cache then pins ("", "") for the
    process lifetime, leaving the dropdown blank. The value is recorded by the CLI
    gateway entrypoint (sync, pre-loop, post-detection) via set_build_info() and
    only read here.
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


class TestGatewayMemoryFields:
    """`/api/status` publishes the gateway's own RSS and the session ceiling so
    `kirocrew status` can show what is bounding memory."""

    def test_fields_read_rss_and_the_configured_ceiling(self, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_system

        monkeypatch.setattr(
            handlers_system.platform_compat, "proc_rss_bytes", lambda: 321 * 1024 * 1024 + 7
        )
        cfg = MagicMock()
        cfg.session.watchdog_rss_max_mb = 1536
        monkeypatch.setattr(handlers_system.KiroCrewConfig, "load", lambda: cfg)
        assert handlers_system._gateway_memory_fields() == (321, 1536)

    def test_each_reading_degrades_alone(self, monkeypatch) -> None:
        from kiro_crew.dashboard import handlers_system

        def _boom():
            raise OSError("no procfs")

        monkeypatch.setattr(handlers_system.platform_compat, "proc_rss_bytes", _boom)
        cfg = MagicMock()
        cfg.session.watchdog_rss_max_mb = 1536
        monkeypatch.setattr(handlers_system.KiroCrewConfig, "load", lambda: cfg)
        assert handlers_system._gateway_memory_fields() == (0, 1536)

        monkeypatch.setattr(handlers_system.platform_compat, "proc_rss_bytes", lambda: 2**30)
        monkeypatch.setattr(
            handlers_system.KiroCrewConfig, "load", MagicMock(side_effect=RuntimeError)
        )
        assert handlers_system._gateway_memory_fields() == (1024, 0)

    def test_api_status_publishes_both_fields_off_loop(self) -> None:
        import inspect

        from kiro_crew.dashboard import handlers_system

        source = inspect.getsource(handlers_system.api_status)
        assert '"gateway_rss_mb": gateway_rss_mb' in source
        assert '"watchdog_rss_max_mb": watchdog_rss_max_mb' in source
        # procfs + config read: never inline on the event loop.
        assert "to_thread(_gateway_memory_fields)" in source
