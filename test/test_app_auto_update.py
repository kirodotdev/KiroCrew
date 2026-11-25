"""Installed apps update themselves only when the owner chose it, and never gain a permission.

Covers ``app_updates`` / ``apps.<name>.auto_update`` parsing and the PATCH specs,
``apps.update_consent``, the ``update_app`` refusal inside the widening scope,
``apps.auto_update`` (which apps are due, the idle check, outcomes, notes and
the ledger), and the ``update_notify`` hand-off between the two note producers.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import auto_update
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    get_app_manifest,
    install_app,
    update_app,
)
from kiro_crew.apps.update_consent import (
    NEEDS_CONSENT_CODE,
    permission_widening_refused,
    refusing_permission_widening,
    widened_permissions,
)
from kiro_crew.config.integration_sections import AppSettingsConfig, AppUpdatesConfig
from kiro_crew.config.loader_pipeline import _parse_app_settings, _parse_app_updates


def _config(mode: str = "off", **overrides: str) -> Any:
    return types.SimpleNamespace(
        app_updates=AppUpdatesConfig(mode=mode),
        apps={name: AppSettingsConfig(auto_update=value) for name, value in overrides.items()},
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_the_default_is_off(self):
        assert _parse_app_updates({}) == AppUpdatesConfig(mode="off")

    @pytest.mark.parametrize("mode", ["off", "on"])
    def test_known_modes_are_kept(self, mode):
        assert _parse_app_updates({"mode": mode}).mode == mode

    # ``ask`` is a value an earlier build of this setting wrote; there is no such
    # mode, so it reads as off like any other unknown spelling.
    @pytest.mark.parametrize("raw", ["ask", "ON", "yes", True, 1, None])
    def test_an_unreadable_mode_is_off(self, raw):
        assert _parse_app_updates({"mode": raw}).mode == "off"

    def test_the_check_runs_every_six_hours_and_is_not_a_setting(self):
        import dataclasses

        assert auto_update.check_interval_secs() == 6 * 3600
        assert [f.name for f in dataclasses.fields(AppUpdatesConfig)] == ["mode"]
        # A hand-written interval is dropped, never read.
        assert _parse_app_updates({"mode": "on", "check_interval_hours": 1}) == AppUpdatesConfig(
            mode="on"
        )

    def test_a_per_app_override_is_read_beside_the_model(self):
        apps = _parse_app_settings(
            {"a": {"model": "m", "auto_update": "never"}, "b": {"auto_update": "always"}}
        )
        assert apps["a"] == AppSettingsConfig(model="m", auto_update="never")
        assert apps["b"].auto_update == "always"

    def test_an_unknown_override_follows_the_global_mode(self):
        assert _parse_app_settings({"a": {"auto_update": "sometimes"}})["a"].auto_update == ""

    def test_the_settings_are_patchable_with_closed_values(self):
        from kiro_crew.dashboard.handlers.core_config_schema import _editable_config_spec

        assert _editable_config_spec("app_updates.mode")["values"] == ["off", "on"]
        assert _editable_config_spec("apps.my-app.auto_update")["values"] == ["", "always", "never"]
        assert _editable_config_spec("apps.Not_Kebab.auto_update") is None


class TestEffectiveMode:
    def test_never_wins_over_on(self):
        assert auto_update.effective_mode(_config("on", x="never"), "x") == "never"

    def test_always_wins_over_off(self):
        assert auto_update.effective_mode(_config("off", x="always"), "x") == "on"

    def test_no_override_follows_the_global_mode(self):
        assert auto_update.effective_mode(_config("on"), "x") == "on"
        assert auto_update.effective_mode(_config("off"), "x") == "off"

    def test_a_mode_outside_off_and_on_reads_as_off(self):
        assert auto_update.effective_mode(_config("ask"), "x") == "off"

    def test_checks_run_when_one_app_is_always(self):
        assert not auto_update.checks_wanted(_config("off"))
        assert auto_update.checks_wanted(_config("off", x="always"))
        assert auto_update.checks_wanted(_config("on"))


# ---------------------------------------------------------------------------
# Permission widening
# ---------------------------------------------------------------------------


class TestWidenedPermissions:
    def test_identical_permissions_widen_nothing(self):
        perms = {"api": ["/api/chat"], "storage": True, "memory": "app-scoped"}
        assert widened_permissions(perms, dict(perms)) == []

    def test_narrowing_widens_nothing(self):
        assert widened_permissions({"api": ["/a", "/b"], "network": True}, {"api": ["/a"]}) == []

    def test_each_kind_of_addition_is_named(self):
        old = {"api": ["/a"], "memory": "app-scoped"}
        new = {
            "api": ["/a", "/b"],
            "events": ["x"],
            "network": True,
            "sessionApproval": True,
            "memory": "shared",
        }
        assert widened_permissions(old, new) == [
            "api",
            "events",
            "network",
            "sessionApproval",
            "memory",
        ]

    def test_reaching_bot_threads_on_a_held_session_grant_is_a_widening(self):
        # The manual Update disables an app that newly declares this scope.
        held = {"sessionApproval": True}
        assert widened_permissions(held, {**held, "memberThreads": True}) == ["memberThreads"]

    @pytest.mark.parametrize("grant", ["agentTurns", "workerAutoApprove"])
    def test_starting_agent_turns_or_auto_approving_tools_is_a_widening(self, grant):
        assert widened_permissions({}, {grant: True}) == [grant]

    @pytest.mark.parametrize(
        "grant", ["reachableByUserSessions", "desktopCapabilities", "browser", "bots"]
    )
    def test_a_grant_the_manual_update_holds_back_is_a_widening(self, grant):
        assert widened_permissions({grant: ["read"]}, {grant: ["read", "send"]}) == [grant]

    def test_a_new_browser_site_is_a_widening(self):
        old = {"browserSites": ["https://example.com/a/*"]}
        new = {"browserSites": ["https://example.com/a/*", "https://example.com/b/*"]}
        assert widened_permissions(old, new) == ["browserSites"]

    @pytest.mark.parametrize("grant", ["chatSlots", "hiddenSessions", "webhookSource"])
    def test_a_trust_prompt_switch_turned_on_is_a_widening(self, grant):
        assert widened_permissions({}, {grant: True}) == [grant]

    def test_a_chat_launch_wider_than_the_installed_one_is_a_widening(self):
        launch = {"keyPrefix": "notes-", "agents": ["notes-agent"]}
        wider = {**launch, "agents": ["notes-agent", "other-agent"]}
        assert widened_permissions({}, {"chatLaunch": launch}) == ["chatLaunch"]
        assert widened_permissions({"chatLaunch": launch}, {"chatLaunch": wider}) == ["chatLaunch"]
        assert widened_permissions({"chatLaunch": wider}, {"chatLaunch": launch}) == []

    def test_an_unrankable_memory_value_counts_as_widened(self):
        assert widened_permissions({"memory": ""}, {"memory": "global"}) == ["memory"]

    def test_an_unreadable_old_manifest_makes_every_declared_permission_new(self):
        assert widened_permissions(None, {"storage": True, "api": ["/a"]}) == ["api", "storage"]

    def test_the_scope_is_off_by_default_and_restored(self):
        assert not permission_widening_refused()
        with refusing_permission_widening():
            assert permission_widening_refused()
        assert not permission_widening_refused()


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    import kiro_crew.apps.bridges as bridges_mod
    import kiro_crew.apps.execution as execution_mod

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    monkeypatch.setattr(bridges_mod, "_mcp_json_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    return home


def _source(tmp_path: Path, folder: str, version: str, permissions: dict) -> Path:
    src = tmp_path / "source" / folder
    src.mkdir(parents=True)
    manifest = {
        "name": "auto-app",
        "version": version,
        "displayName": "Auto App",
        "description": "An app that updates",
        "author": "tester",
        "permissions": permissions,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


class TestUpdateAppRefusal:
    def test_a_widening_update_is_refused_in_scope_and_leaves_the_app(self, tmp_path, app_home):
        assert install_app(_source(tmp_path, "v1", "1.0.0", {"storage": True})).ok
        v2 = _source(tmp_path, "v2", "1.1.0", {"storage": True, "network": True})
        with refusing_permission_widening():
            result = update_app(v2)
        assert not result.ok
        assert result.error_code == NEEDS_CONSENT_CODE
        assert "network" in result.error
        installed = get_app_manifest("auto-app")
        assert installed is not None and installed.version == "1.0.0"

    def test_the_same_update_out_of_scope_is_the_button_and_applies(self, tmp_path, app_home):
        assert install_app(_source(tmp_path, "v1", "1.0.0", {})).ok
        assert update_app(_source(tmp_path, "v2", "1.1.0", {"network": True})).ok
        installed = get_app_manifest("auto-app")
        assert installed is not None and installed.version == "1.1.0"

    def test_a_non_widening_update_applies_in_scope_and_keeps_data(self, tmp_path, app_home):
        from kiro_crew.apps.manager import app_dir

        assert install_app(_source(tmp_path, "v1", "1.0.0", {"storage": True})).ok
        (app_dir("auto-app") / "data").mkdir(exist_ok=True)
        (app_dir("auto-app") / "data" / "kept.txt").write_text("mine", encoding="utf-8")
        with refusing_permission_widening():
            assert update_app(_source(tmp_path, "v2", "1.1.0", {"storage": True})).ok
        assert (app_dir("auto-app") / "data" / "kept.txt").read_text(encoding="utf-8") == "mine"

    def test_an_unreadable_installed_manifest_refuses_in_scope(self, tmp_path, app_home):
        # A version that drops a restricted mcpTools list declares less, so with
        # no readable baseline it would compare as no widening at all.
        from kiro_crew.apps.manager import app_dir

        assert install_app(_source(tmp_path, "v1", "1.0.0", {"mcpTools": ["search"]})).ok
        (app_dir("auto-app") / APP_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        assert get_app_manifest("auto-app") is None
        with refusing_permission_widening():
            result = update_app(_source(tmp_path, "v2", "1.1.0", {}))
        assert not result.ok
        assert result.error_code == NEEDS_CONSENT_CODE
        assert "could not be read" in result.error


# ---------------------------------------------------------------------------
# Candidates and the idle check
# ---------------------------------------------------------------------------


def _installed(name: str, version: str, *, source: str = "", lifecycle: str = "gateway") -> dict:
    return {
        "name": name,
        "version": version,
        "source": source or f"registry:{name}",
        "lifecycle": lifecycle,
    }


class TestFindCandidates:
    @pytest.fixture()
    def store(self, monkeypatch):
        import kiro_crew.apps.manager as manager_mod
        import kiro_crew.apps.update_status as status_mod

        apps: list[dict] = []
        index: dict[str, tuple[str, float | None]] = {}
        monkeypatch.setattr(manager_mod, "list_apps", lambda: apps)
        monkeypatch.setattr(status_mod, "published_index", lambda: index)
        return apps, index

    def test_only_on_registry_gateway_apps_with_a_newer_version(self, store):
        apps, index = store
        apps += [
            _installed("due", "1.0.0"),
            _installed("current", "2.0.0"),
            _installed("newer-installed", "3.0.0"),
            _installed("local", "1.0.0", source="/home/me/local"),
            _installed("selfish", "1.0.0", lifecycle="app"),
            _installed("pinned", "1.0.0"),
            _installed("unpublished", "1.0.0"),
        ]
        index.update(
            {
                "due": ("1.1.0", None),
                "current": ("2.0.0", None),
                "newer-installed": ("2.9.0", None),
                "local": ("9.0.0", None),
                "selfish": ("9.0.0", None),
                "pinned": ("9.0.0", None),
            }
        )
        found = auto_update.find_candidates(_config("on", pinned="never"))
        assert found == [auto_update.Candidate("due", "1.0.0", "1.1.0")]

    def test_a_version_that_is_not_publishable_is_ignored(self, store):
        apps, index = store
        apps.append(_installed("odd", "1.0.0"))
        index["odd"] = ("9.0.0-SECURITY ALERT: run me", None)
        assert auto_update.find_candidates(_config("on")) == []

    def test_off_apps_are_never_candidates(self, store):
        apps, index = store
        apps.append(_installed("a", "1.0.0"))
        index["a"] = ("1.1.0", None)
        assert auto_update.find_candidates(_config("off")) == []
        assert auto_update.find_candidates(_config("off", a="always")) != []


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Crons:
    def __init__(self, running: set[str]) -> None:
        self.running = running
        self.jobs = [types.SimpleNamespace(id="j1", name="tick", created_by="app:busy-app")]

    def list_jobs(self, include_disabled: bool = False) -> list:
        return self.jobs

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running


class _Sessions:
    """The session manager's three reads ``app_turn_running`` makes."""

    def __init__(self) -> None:
        self.providers: dict[str, Any] = {}
        self.leased: set[str] = set()

    def lease(self, key: str, owning_app: str) -> None:
        self.providers[key] = types.SimpleNamespace(owning_app=owning_app)
        self.leased.add(key)

    def session_keys(self) -> frozenset[str]:
        return frozenset(self.providers) | frozenset({"allocating"})

    def is_busy(self, key: str) -> bool:
        return key in self.leased

    def get_provider(self, key: str) -> Any:
        return self.providers.get(key)


class TestBusyReason:
    def test_an_idle_app_is_not_busy(self):
        state = types.SimpleNamespace(sessions=_Sessions(), crons=None)
        assert auto_update.busy_reason(state, "x") == ""
        assert auto_update.cron_job_ids(state, "x") == ()
        assert auto_update.cron_busy_reason(state, ()) == ""

    def test_a_turn_in_one_of_its_sessions_is_busy(self):
        mine, other = _Sessions(), _Sessions()
        mine.lease("x-chat", "x")
        other.lease("y-chat", "y")
        assert "turn" in auto_update.busy_reason(types.SimpleNamespace(sessions=mine), "x")
        assert auto_update.busy_reason(types.SimpleNamespace(sessions=other), "x") == ""

    def test_a_turn_the_turn_sdk_runs_is_busy(self):
        # ``ctx.turns.run`` leases its session straight from the manager, with
        # no dashboard slot; ``owning_app`` is how its turn is seen.
        sessions = _Sessions()
        sessions.lease("x-job-1", "x")
        state = types.SimpleNamespace(_slots={}, sessions=sessions)
        assert "turn" in auto_update.busy_reason(state, "x")

    def test_a_finished_turn_is_not_busy(self):
        sessions = _Sessions()
        sessions.lease("x-chat", "x")
        sessions.leased.clear()
        assert auto_update.busy_reason(types.SimpleNamespace(sessions=sessions), "x") == ""

    @pytest.mark.asyncio
    async def test_the_real_session_manager_shows_an_app_turn_until_its_release(self, monkeypatch):
        from test_session import _mock_provider_factory

        from kiro_crew import session
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(session, "_is_acp_provider", lambda _provider: True)
        mgr = session.SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())
        state = types.SimpleNamespace(sessions=mgr)
        try:
            # How ``AgentTurnSDK.run`` claims its session.
            await mgr.get_or_create("x-turn-1", owning_app="x")
            assert "turn" in auto_update.busy_reason(state, "x")
            assert auto_update.busy_reason(state, "y") == ""
            mgr.release("x-turn-1")
            assert auto_update.busy_reason(state, "x") == ""
        finally:
            await mgr.close_all()

    def test_a_running_cron_of_its_own_is_busy(self, monkeypatch):
        import kiro_crew.apps.cron_sdk as cron_sdk_mod

        monkeypatch.setattr(cron_sdk_mod, "owner_tag", lambda name: f"app:{name}")
        state = types.SimpleNamespace(_slots={}, crons=_Crons({"j1"}))
        job_ids = auto_update.cron_job_ids(state, "busy-app")
        assert job_ids == ("j1",)
        assert "cron" in auto_update.cron_busy_reason(state, job_ids)
        state.crons.running.clear()
        assert auto_update.cron_busy_reason(state, job_ids) == ""

    def test_unreadable_cron_jobs_count_as_busy(self):
        class _Broken:
            def list_jobs(self, include_disabled=False):
                raise OSError("store busy")

        state = types.SimpleNamespace(_slots={}, crons=_Broken())
        assert auto_update.cron_job_ids(state, "x") is None
        assert "could not be read" in auto_update.cron_busy_reason(state, None)

    def test_a_startup_hook_still_running_is_busy(self, monkeypatch):
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setitem(lifecycle_mod._DETACHED_HOOK_TASKS, "x", {_Task(done=False)})
        state = types.SimpleNamespace(_slots={}, crons=None)
        assert "startup hook" in auto_update.busy_reason(state, "x")

    def test_a_residual_hook_marker_is_busy(self, monkeypatch):
        import kiro_crew.apps.lifecycle as lifecycle_mod

        monkeypatch.setattr(lifecycle_mod, "_DETACHED_HOOK_RESIDUALS", {"x"})
        assert "startup hook" in auto_update.busy_reason(types.SimpleNamespace(), "x")

    def test_a_job_this_process_runs_is_busy(self, monkeypatch):
        import kiro_crew.apps.job_sdk as job_sdk_mod

        sdk = types.SimpleNamespace(
            cancelling_and_live_ids=lambda: (frozenset(), frozenset({"run-1"}))
        )
        monkeypatch.setattr(job_sdk_mod, "_SDKS", {"x": sdk})
        assert "jobs is running" in auto_update.job_busy_reason("x")
        assert auto_update.job_busy_reason("y") == ""

    def test_an_app_with_no_live_job_is_not_busy(self, monkeypatch):
        import kiro_crew.apps.job_sdk as job_sdk_mod

        # A stale ``running`` record is not in ``live``: the disable cleanup
        # does not stop it, so it must not hold the update back.
        sdk = types.SimpleNamespace(cancelling_and_live_ids=lambda: (frozenset(), frozenset()))
        monkeypatch.setattr(job_sdk_mod, "_SDKS", {"x": sdk})
        assert auto_update.job_busy_reason("x") == ""

    def test_unreadable_jobs_count_as_busy(self, monkeypatch):
        import kiro_crew.apps.job_sdk as job_sdk_mod

        def _broken():
            raise RuntimeError("lock broken")

        sdk = types.SimpleNamespace(cancelling_and_live_ids=_broken)
        monkeypatch.setattr(job_sdk_mod, "_SDKS", {"x": sdk})
        assert "could not be read" in auto_update.job_busy_reason("x")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("starts", ["turn", "cron"])
    async def test_work_that_starts_during_the_off_loop_reads_is_seen(self, starts, monkeypatch):
        import kiro_crew.apps.cron_sdk as cron_sdk_mod

        monkeypatch.setattr(cron_sdk_mod, "owner_tag", lambda name: f"app:{name}")
        state = types.SimpleNamespace(sessions=_Sessions(), crons=_Crons(set()))

        def _jobs(name: str) -> str:
            # The last off-loop read: an app-owned turn or one of its cron
            # runs starts while it runs.
            if starts == "turn":
                state.sessions.lease(f"{name}-chat", name)
            else:
                state.crons.running.add("j1")
            return ""

        monkeypatch.setattr(auto_update, "job_busy_reason", _jobs)
        assert starts in await auto_update.idle_reason(state, "busy-app")

    @pytest.mark.asyncio
    async def test_a_running_job_puts_the_update_off_before_and_during_it(self, monkeypatch):
        import kiro_crew.apps.job_sdk as job_sdk_mod
        import kiro_crew.apps.routes as routes_mod

        live: set[str] = {"run-1"}
        sdk = types.SimpleNamespace(cancelling_and_live_ids=lambda: (frozenset(), frozenset(live)))
        monkeypatch.setattr(job_sdk_mod, "_SDKS", {"x": sdk})
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        monkeypatch.setattr(auto_update, "still_candidate_reason", lambda candidate: "")
        seen: list[str] = []

        async def _apply(name, **kwargs):
            # A job starts after the first look; the locked re-check sees it.
            live.add("run-2")
            seen.append(await kwargs["still_idle"]())
            return 409, {"ok": False, "retryable": True, "code": "app_busy", "error": "busy"}

        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        state = types.SimpleNamespace(_slots={}, crons=None)
        candidate = auto_update.Candidate("x", "1.0.0", "1.1.0")
        assert await auto_update.update_one(state, candidate) == ""
        assert seen == []
        live.clear()
        assert await auto_update.update_one(state, candidate) == ""
        assert seen == ["one of its jobs is running"]


# ---------------------------------------------------------------------------
# Outcomes, notes and the ledger
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self._slots: dict = {}
        self.crons = None
        self.notes: list[tuple[str, str, dict]] = []

    def notify(self, kind, title, body, *, meta=None, url=None, actions=None, channel=None):
        extra = {"body": body, "meta": meta, "url": url, "actions": actions}
        self.notes.append((title, channel, extra))


@pytest.fixture()
def ledger_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.fixture()
def installed_after(monkeypatch):
    """The version the installed record reports after an update; tests may change it."""
    after = {"version": "1.1.0"}
    monkeypatch.setattr(auto_update, "_installed_version", lambda name: after["version"])
    return after


@pytest.fixture()
def apply(monkeypatch, installed_after):
    """Replace the Update sequence with a scripted answer, recording each call."""
    import kiro_crew.apps.routes as routes_mod

    calls: list[dict] = []
    answers: list[tuple[int, dict]] = []

    async def _apply(name, **kwargs):
        calls.append({"name": name, **kwargs})
        return answers.pop(0)

    monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
    return calls, answers


_CAND = auto_update.Candidate("my-app", "1.0.0", "1.1.0")


class TestUpdateOne:
    @pytest.mark.asyncio
    async def test_success_records_and_posts_one_updated_note(self, ledger_home, apply):
        calls, answers = apply
        answers.append((200, {"ok": True}))
        state = _State()
        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_UPDATED
        assert calls[0]["refuse_permission_widening"] is True
        assert calls[0]["caller"] == "app_auto_update"
        assert [(t, c) for t, c, _ in state.notes] == [("my-app updated to 1.1.0", "system.apps")]
        assert state.notes[0][2]["url"] == "/apps/detail/my-app"
        assert auto_update.read_ledger()["my-app"] == {"version": "1.1.0", "outcome": "updated"}

    @pytest.mark.asyncio
    async def test_the_note_names_the_version_installed_not_the_index_one(
        self, ledger_home, apply, installed_after
    ):
        _, answers = apply
        answers.append((200, {"ok": True}))
        installed_after["version"] = "1.0.5"
        state = _State()
        await auto_update.update_one(state, _CAND)
        assert state.notes[0][0] == "my-app updated to 1.0.5"
        # Short of the advertised 1.1.0: settled, so the next check does not
        # rebuild the app only to be refused as not newer.
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)

    @pytest.mark.asyncio
    async def test_an_update_that_leaves_the_version_is_settled_not_repeated(
        self, ledger_home, apply, installed_after
    ):
        _, answers = apply
        answers.append((200, {"ok": True}))
        installed_after["version"] = "1.0.0"
        state = _State()
        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_NO_CHANGE
        assert [t for t, _, _ in state.notes] == ["my-app 1.1.0 could not be installed"]
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)

    @pytest.mark.asyncio
    async def test_needs_consent_posts_a_note_and_is_not_retried(self, ledger_home, apply):
        calls, answers = apply
        answers.append(
            (400, {"ok": False, "code": NEEDS_CONSENT_CODE, "error": "asks for network"})
        )
        state = _State()
        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_NEEDS_CONSENT
        title, _, extra = state.notes[0]
        assert title == "my-app 1.1.0 needs your review"
        assert extra["actions"] == [
            {"id": "review", "label": "Review update", "url": "/apps/detail/my-app"}
        ]
        assert extra["meta"]["needsConsent"] is True
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)

    def test_a_consent_note_leads_with_the_permissions_it_adds(self):
        # The feed cuts a row at its end, so the reason must come first.
        from kiro_crew.apps.update_consent import consent_refusal

        note = auto_update.consent_note(_CAND, consent_refusal(["network", "api"]))
        assert note["title"] == "my-app 1.1.0 wants new permissions"
        assert note["body"] == "Adds: network, api. Review to update."
        assert note["actions"][0]["label"] == "Review update"

    @pytest.mark.asyncio
    async def test_a_version_that_keeps_failing_settles_after_the_cap(self, ledger_home, apply):
        # An admission denial recurs every time; each try stops and restarts
        # the app, so the version settles instead of repeating forever.
        _, answers = apply
        state = _State()
        for attempt in range(auto_update.MAX_FAILED_ATTEMPTS):
            assert not auto_update.already_settled(auto_update.read_ledger(), _CAND)
            answers.append((403, {"ok": False, "error": "blocked by admission policy"}))
            assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_FAILED
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)
        assert len(state.notes) == 1
        newer = auto_update.Candidate(_CAND.name, _CAND.installed, "1.2.0")
        assert not auto_update.already_settled(auto_update.read_ledger(), newer)

    @pytest.mark.asyncio
    async def test_a_failure_is_noted_once_and_retried(self, ledger_home, apply):
        _, answers = apply
        answers += [(400, {"ok": False, "error": "clone failed"})] * 2
        state = _State()
        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_FAILED
        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_FAILED
        assert [t for t, _, _ in state.notes] == ["my-app 1.1.0 could not be installed"]
        assert not auto_update.already_settled(auto_update.read_ledger(), _CAND)

    @pytest.mark.asyncio
    async def test_a_failure_note_is_redacted(self, ledger_home, apply):
        from kiro_crew.testing.synthetic_credentials import github_classic

        _, answers = apply
        secret = github_classic()
        answers.append((400, {"ok": False, "error": f"clone failed with token {secret}"}))
        state = _State()
        await auto_update.update_one(state, _CAND)
        body = state.notes[0][2]["body"]
        # The note still says what failed; only the token is gone.
        assert body.startswith("1.0.0 is still running. clone failed with token ")
        assert secret not in body

    @pytest.mark.asyncio
    async def test_a_retryable_refusal_records_nothing(self, ledger_home, apply):
        _, answers = apply
        answers.append((409, {"error": "startup hook", "retryable": True}))
        state = _State()
        assert await auto_update.update_one(state, _CAND) == ""
        assert state.notes == [] and auto_update.read_ledger() == {}

    @pytest.mark.asyncio
    async def test_a_busy_app_is_put_off_without_calling_update(
        self, ledger_home, apply, monkeypatch
    ):
        calls, _ = apply
        monkeypatch.setattr(auto_update, "cron_busy_reason", lambda state, job_ids: "busy")
        assert await auto_update.update_one(_State(), _CAND) == ""
        assert calls == []


class TestAnAppliedUpdateReloadsOpenTabs:
    """An update the loop applied sends the ``app_reload`` frame the Update route sends."""

    @staticmethod
    def _state_with_socket() -> tuple[_State, list[tuple[str, str]]]:
        state = _State()
        frames: list[tuple[str, str]] = []
        state.broadcast_ws = lambda kind, data: frames.append((kind, data["app"]))  # type: ignore[attr-defined]
        return state, frames

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version_after", ["1.1.0", "1.0.0"])
    async def test_replaced_files_announce_the_new_ui(
        self, ledger_home, apply, installed_after, version_after
    ):
        # 1.0.0 is an apply that left the version where it was: the files were
        # still replaced, so a tab holding the old module must drop it too.
        _, answers = apply
        answers.append((200, {"ok": True}))
        installed_after["version"] = version_after
        state, frames = self._state_with_socket()
        await auto_update.update_one(state, _CAND)
        assert frames == [("app_reload", "my-app")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            (400, {"ok": False, "error": "clone failed"}),
            (409, {"error": "a session it owns is running a turn", "retryable": True}),
        ],
    )
    async def test_an_update_that_replaced_nothing_announces_nothing(
        self, ledger_home, apply, answer
    ):
        _, answers = apply
        answers.append(answer)
        state, frames = self._state_with_socket()
        await auto_update.update_one(state, _CAND)
        assert frames == []


class TestAnAppliedUpdateReachesBotChats:
    """An update the loop applied ends as every update route does: ``after_app_change``."""

    @staticmethod
    def _seen(monkeypatch) -> list[object]:
        import kiro_crew.bot_chat_app_tools as bot_chat_app_tools

        seen: list[object] = []

        async def _after(state: object) -> None:
            seen.append(state)

        monkeypatch.setattr(bot_chat_app_tools, "after_app_change", _after)
        return seen

    @pytest.mark.asyncio
    async def test_replaced_files_reach_bot_chats_once(self, ledger_home, apply, monkeypatch):
        _, answers = apply
        answers.append((200, {"ok": True}))
        seen = self._seen(monkeypatch)
        state = _State()
        await auto_update.update_one(state, _CAND)
        assert seen == [state]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            (400, {"ok": False, "error": "clone failed"}),
            (409, {"error": "a session it owns is running a turn", "retryable": True}),
        ],
    )
    async def test_an_update_that_replaced_nothing_reaches_no_bot_chat(
        self, ledger_home, apply, monkeypatch, answer
    ):
        _, answers = apply
        answers.append(answer)
        seen = self._seen(monkeypatch)
        await auto_update.update_one(_State(), _CAND)
        assert seen == []


class TestNotesAreValidPayloads:
    """Each note survives the real legacy converter, which drops an invalid one."""

    @pytest.mark.parametrize(
        "note",
        [
            auto_update.updated_note(_CAND, "1.1.0"),
            auto_update.no_change_note(_CAND, "1.0.0"),
            auto_update.consent_note(_CAND, "asks for network"),
            auto_update.failed_note(_CAND, "boom"),
        ],
        ids=["updated", "no-change", "consent", "failed"],
    )
    def test_the_converter_accepts_it_on_the_apps_channel(self, note):
        from kiro_crew.notifications.bus import payload_from_legacy

        payload = payload_from_legacy(
            auto_update.NOTE_KIND,
            note["title"],
            note["body"],
            note["meta"],
            url=note["url"],
            actions=note.get("actions"),
            channel=auto_update.NOTE_CHANNEL,
        )
        assert payload.channel == "system.apps"
        assert payload.actions == note.get("actions")


class TestRefreshPublished:
    @pytest.mark.asyncio
    async def test_the_catalog_cache_is_never_dropped(self, monkeypatch):
        import kiro_crew.apps.official_catalog as catalog_mod
        import kiro_crew.apps.registry as registry_mod

        calls: list[str] = []

        async def _catalog():
            calls.append("catalog")
            return []

        async def _registries(repo):
            calls.append("registries")
            return {}

        monkeypatch.setattr(
            catalog_mod, "forget_cache", lambda: pytest.fail("dropped the catalog cache")
        )
        monkeypatch.setattr(registry_mod, "list_catalog_apps", _catalog)
        monkeypatch.setattr(registry_mod, "refresh_registries", _registries)
        await auto_update.refresh_published()
        assert calls == ["catalog", "registries"]


class TestRunCheck:
    @pytest.mark.asyncio
    async def test_nothing_is_fetched_when_no_app_updates_automatically(self, monkeypatch):
        async def _refresh():
            raise AssertionError("fetched with automatic updates off")

        monkeypatch.setattr(auto_update, "refresh_published", _refresh)
        assert await auto_update.run_check(_State(), _config("off")) == {}

    @pytest.mark.asyncio
    async def test_a_settled_version_is_skipped_and_others_run(
        self, ledger_home, apply, installed_after, monkeypatch
    ):
        calls, answers = apply
        answers.append((200, {"ok": True}))
        installed_after["version"] = "2.1.0"

        async def _refresh():
            return None

        monkeypatch.setattr(auto_update, "refresh_published", _refresh)
        other = auto_update.Candidate("other-app", "2.0.0", "2.1.0")
        monkeypatch.setattr(auto_update, "find_candidates", lambda config: [_CAND, other])
        auto_update.record("my-app", "1.1.0", auto_update.OUTCOME_NEEDS_CONSENT)
        outcomes = await auto_update.run_check(_State(), _config("on"))
        assert outcomes == {"other-app": auto_update.OUTCOME_UPDATED}
        assert [c["name"] for c in calls] == ["other-app"]

    @pytest.mark.asyncio
    async def test_a_newer_release_after_a_consent_refusal_is_tried(
        self, ledger_home, apply, installed_after, monkeypatch
    ):
        calls, answers = apply
        answers.append((200, {"ok": True}))
        installed_after["version"] = "1.2.0"

        async def _refresh():
            return None

        monkeypatch.setattr(auto_update, "refresh_published", _refresh)
        newer = auto_update.Candidate("my-app", "1.0.0", "1.2.0")
        monkeypatch.setattr(auto_update, "find_candidates", lambda config: [newer])
        auto_update.record("my-app", "1.1.0", auto_update.OUTCOME_NEEDS_CONSENT)
        assert await auto_update.run_check(_State(), _config("on")) == {
            "my-app": auto_update.OUTCOME_UPDATED
        }


class TestRunForever:
    @pytest.mark.asyncio
    async def test_it_checks_once_per_interval_and_rereads_config(self, monkeypatch):
        import kiro_crew.config as config_mod

        configs = [_config("on")] * 3 + [_config("off")]
        checks: list[Any] = []
        sleeps: list[float] = []

        class _Cfg:
            @staticmethod
            def load():
                return configs.pop(0) if len(configs) > 1 else configs[0]

        async def _run_check(state, config):
            checks.append(config)
            return {}

        async def _sleep(secs):
            sleeps.append(secs)
            if len(sleeps) > 4:
                raise asyncio.CancelledError

        monkeypatch.setattr(config_mod, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(auto_update, "run_check", _run_check)
        monkeypatch.setattr(auto_update, "_sleep", _sleep)
        with pytest.raises(asyncio.CancelledError):
            await auto_update.run_forever(_State())
        # One check on the first pass; the next passes fall inside the 6 hours.
        assert len(checks) == 1
        assert sleeps[0] == auto_update.FIRST_CHECK_DELAY_SECS
        assert set(sleeps[1:]) == {auto_update.POLL_SECS}


# ---------------------------------------------------------------------------
# The listing-driven note
# ---------------------------------------------------------------------------


class TestUpdateNotifyHandOff:
    @staticmethod
    def _row(name: str) -> dict:
        return {
            "name": name,
            "installed": True,
            "updateAvailable": True,
            "lifecycle": "gateway",
            "version": "1.1.0",
            "installedVersion": "1.0.0",
        }

    def _notes(self, config: Any) -> list[dict]:
        from kiro_crew.apps.update_notify import prepare_app_update_notes

        # A first observation adopts silently; the second call announces.
        prepare_app_update_notes([], config)
        return prepare_app_update_notes([self._row("a"), self._row("b")], config)

    @pytest.fixture(autouse=True)
    def _eligible(self, _floor_monkeypatch):
        import kiro_crew.apps.update_notify as notify_mod

        eligible = {"a", "b"}
        _floor_monkeypatch.setattr(notify_mod, "eligible_names", lambda: eligible)
        return eligible

    def test_an_on_app_is_left_to_the_updater(self, ledger_home):
        notes = self._notes(_config("off", a="always"))
        assert [n["meta"]["app"] for n in notes] == ["b"]

    def test_an_on_app_the_updater_cannot_touch_is_still_announced(self, ledger_home, _eligible):
        _eligible.discard("a")
        notes = self._notes(_config("on"))
        assert [n["meta"]["app"] for n in notes] == ["a"]

    def test_a_pinned_or_stored_ask_app_is_announced_like_off(self, ledger_home):
        # ``ask`` was written by an earlier build of this setting and reads as off;
        # a Never app is still told a newer version exists.
        notes = self._notes(_config("ask", b="never"))
        assert sorted(n["meta"]["app"] for n in notes) == ["a", "b"]
        assert all("actions" not in n for n in notes)
        assert all(n["body"].endswith("Open it to update.") for n in notes)

    def test_off_notes_carry_no_button(self, ledger_home):
        assert all("actions" not in n for n in self._notes(_config("off")))


# ---------------------------------------------------------------------------
# Review hardening: failed ledger writes, the locked re-checks
# ---------------------------------------------------------------------------


class TestLedgerWriteFailure:
    def test_a_settled_outcome_survives_an_unwritable_ledger(self, ledger_home, monkeypatch):
        import kiro_crew.atomic_write as atomic_write_mod

        def _fail(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(atomic_write_mod, "atomic_write", _fail)
        auto_update.record("my-app", "1.1.0", auto_update.OUTCOME_NO_CHANGE)
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)


class TestLockedPreflight:
    @pytest.fixture()
    def route(self, monkeypatch):
        import kiro_crew.apps.routes_install as ri

        records = {"auto-app": {"name": "auto-app", "source": "registry:auto-app"}}
        steps: list[str] = []

        async def _no_hook(name):
            return None

        async def _stop_line(name):
            steps.append("deregister")

        monkeypatch.setattr(ri, "get_app", lambda name: records.get(name))
        monkeypatch.setattr(ri, "_startup_refusal_body", _no_hook)

        async def _reinstall(name, registry_name, log_lines=None, **_kwargs):
            # The registry branch takes the app down inside this call, right
            # before the copy, so reaching it is the first change to the app.
            steps.append("reinstall")
            return {"ok": False, "error": "stub"}

        monkeypatch.setattr(ri, "get_app", lambda name: records.get(name))
        monkeypatch.setattr(ri, "_startup_refusal_body", _no_hook)
        monkeypatch.setattr(ri, "git_unavailable_refusal", lambda name: None)
        monkeypatch.setattr(ri, "stop_app_backend", lambda name: steps.append("stop"))
        monkeypatch.setattr(ri, "_deregister_app_off_loop", _stop_line)
        monkeypatch.setattr(ri, "_reinstall_from_registry", _reinstall)
        return ri, records, steps

    @pytest.mark.asyncio
    async def test_work_that_started_since_the_first_look_puts_it_off(self, route):
        ri, _, steps = route

        async def _busy():
            return "a session it owns is running a turn"

        status, body = await ri.apply_app_update("auto-app", still_idle=_busy)
        assert (status, body["code"], body["retryable"]) == (409, "app_busy", True)
        assert steps == []

    @pytest.mark.asyncio
    async def test_an_uninstall_that_won_the_lock_ends_the_update(self, route, monkeypatch):
        ri, records, steps = route
        real_preflight = ri._locked_preflight

        async def _uninstalled_meanwhile(name, still_idle):
            records.clear()
            return await real_preflight(name, still_idle)

        monkeypatch.setattr(ri, "_locked_preflight", _uninstalled_meanwhile)
        status, body = await ri.apply_app_update("auto-app")
        assert (status, body["code"]) == (404, "app_not_installed")
        assert steps == []

    @pytest.mark.asyncio
    async def test_a_requested_registry_source_is_not_read_as_a_change(self, route, monkeypatch):
        # A path-installed app updated with an explicit registry source: the
        # locked re-read compares with the recorded source, not the requested one.
        ri, records, steps = route
        records["auto-app"]["source"] = "/home/u/apps/auto-app"
        status, body = await ri.apply_app_update("auto-app", source="registry:auto-app")
        # Past the locked re-read: the reinstall ran, and its stub failure is the answer.
        assert steps == ["reinstall"]
        assert (status, body["error"]) == (400, "stub")

    @pytest.mark.asyncio
    async def test_a_source_change_that_won_the_lock_still_ends_it(self, route, monkeypatch):
        ri, records, steps = route
        real_preflight = ri._locked_preflight

        async def _moved_meanwhile(name, still_idle):
            records["auto-app"] = {**records["auto-app"], "source": "registry:other/auto-app"}
            return await real_preflight(name, still_idle)

        monkeypatch.setattr(ri, "_locked_preflight", _moved_meanwhile)
        status, body = await ri.apply_app_update("auto-app", source="registry:auto-app")
        assert (status, body["code"]) == (409, "app_changed")
        assert steps == []


class TestCiRoundFixes:
    def test_dropping_the_mcp_tool_list_is_a_widening(self):
        assert widened_permissions({"mcpTools": ["a"]}, {"mcpTools": []}) == ["mcpTools"]
        assert widened_permissions({"mcpTools": ["a"]}, {}) == ["mcpTools"]
        assert widened_permissions({"mcpTools": ["a", "b"]}, {"mcpTools": ["a"]}) == []

    def test_an_older_source_is_refused_before_any_file_moves(self, tmp_path, app_home):
        from kiro_crew.apps.update_consent import NOT_NEWER_CODE

        assert install_app(_source(tmp_path, "v2", "2.0.0", {})).ok
        with refusing_permission_widening():
            result = update_app(_source(tmp_path, "v1", "1.0.0", {}))
        assert not result.ok and result.error_code == NOT_NEWER_CODE
        installed = get_app_manifest("auto-app")
        assert installed is not None and installed.version == "2.0.0"

    def test_the_button_path_still_accepts_any_version(self, tmp_path, app_home):
        assert install_app(_source(tmp_path, "v2", "2.0.0", {})).ok
        assert update_app(_source(tmp_path, "v1", "1.0.0", {})).ok

    @pytest.mark.asyncio
    async def test_a_not_newer_refusal_settles_without_a_failure_note(self, ledger_home, apply):
        from kiro_crew.apps.update_consent import NOT_NEWER_CODE

        _, answers = apply
        answers.append((400, {"ok": False, "code": NOT_NEWER_CODE, "error": "older"}))
        state = _State()
        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_NO_CHANGE
        assert [t for t, _, _ in state.notes] == ["my-app 1.1.0 could not be installed"]
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)

    @pytest.mark.asyncio
    async def test_an_app_uninstalled_meanwhile_is_silent(self, ledger_home, apply):
        _, answers = apply
        answers.append((404, {"error": "gone", "code": "app_not_installed"}))
        state = _State()
        assert await auto_update.update_one(state, _CAND) == ""
        assert state.notes == [] and auto_update.read_ledger() == {}

    def test_an_installed_version_that_is_not_publishable_is_skipped(self, monkeypatch):
        import kiro_crew.apps.manager as manager_mod
        import kiro_crew.apps.update_status as status_mod

        monkeypatch.setattr(
            manager_mod, "list_apps", lambda: [_installed("odd", "1.0.0-click here now")]
        )
        monkeypatch.setattr(status_mod, "published_index", lambda: {"odd": ("2.0.0", None)})
        assert auto_update.find_candidates(_config("on")) == []

    @pytest.mark.asyncio
    async def test_a_stop_request_starts_no_further_update(self, ledger_home, apply, monkeypatch):
        calls, _ = apply

        async def _refresh():
            return None

        monkeypatch.setattr(auto_update, "refresh_published", _refresh)
        monkeypatch.setattr(auto_update, "find_candidates", lambda config: [_CAND])
        monkeypatch.setattr(auto_update, "_STOP", {"requested": False})
        auto_update.request_stop()
        assert await auto_update.run_check(_State(), _config("on")) == {}
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_update_counts_as_applying_only_while_it_runs(
        self, ledger_home, monkeypatch, installed_after
    ):
        import kiro_crew.apps.routes as routes_mod

        seen: list[bool] = []

        async def _apply(name, **kwargs):
            seen.append(auto_update.applying())
            return 200, {"ok": True}

        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        await auto_update.update_one(_State(), _CAND)
        assert seen == [True] and not auto_update.applying()


class TestRestartHold:
    """A process restart and an update cannot both pass: one of them waits."""

    @pytest.mark.asyncio
    async def test_a_hold_is_taken_and_released(self, monkeypatch):
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        assert await auto_update.settle_for_restart(0) is True
        assert auto_update.restart_hold_reason() == auto_update.RESTART_HOLD_REASON
        auto_update.release_restart_hold()
        assert auto_update.restart_hold_reason() == ""

    @pytest.mark.asyncio
    async def test_a_held_loop_starts_no_update_and_records_nothing(
        self, ledger_home, apply, monkeypatch
    ):
        calls, _ = apply
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        assert await auto_update.settle_for_restart(0)
        assert await auto_update.update_one(_State(), _CAND) == ""
        assert calls == []
        assert auto_update.read_ledger() == {}

    @pytest.mark.asyncio
    async def test_a_hold_taken_after_the_first_look_is_read_under_the_lock(
        self, ledger_home, monkeypatch, installed_after
    ):
        # The restart takes its hold after update_one's first look, before this
        # app is marked applying; the locked re-check before the backend stops
        # reads it and the update is put off with nothing recorded.
        import kiro_crew.apps.routes as routes_mod

        monkeypatch.setattr(auto_update, "_APPLYING", set())
        monkeypatch.setattr(auto_update, "still_candidate_reason", lambda candidate: "")
        monkeypatch.setattr(auto_update, "cron_busy_reason", lambda state, job_ids: "")
        looks: list[str] = []

        def _busy(state, name):
            if not looks:
                # The hold settle_for_restart takes before it waits.
                auto_update._RESTART_HOLD["holders"] += 1
            looks.append(name)
            return ""

        monkeypatch.setattr(auto_update, "busy_reason", _busy)
        seen: list[str] = []

        async def _apply(name, **kwargs):
            seen.append(await kwargs["still_idle"]())
            return 409, {"ok": False, "retryable": True, "code": "app_busy", "error": "busy"}

        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        assert await auto_update.update_one(_State(), _CAND) == ""
        assert seen == [auto_update.RESTART_HOLD_REASON]
        assert auto_update.read_ledger() == {}

    @pytest.mark.asyncio
    async def test_settle_gives_up_at_the_deadline_without_cancelling(self, monkeypatch):
        monkeypatch.setattr(auto_update, "_APPLYING", {"my-app"})
        assert await auto_update.settle_for_restart(0) is False
        # Held, so nothing new starts; the update in flight is left to finish.
        assert auto_update.restart_hold_reason() == auto_update.RESTART_HOLD_REASON
        assert auto_update.applying()

    @pytest.mark.asyncio
    async def test_settle_returns_once_the_update_in_flight_finishes(self, monkeypatch):
        applying = {"my-app"}
        monkeypatch.setattr(auto_update, "_APPLYING", applying)
        waits: list[float] = []

        async def _wait(secs):
            # The update finishes while settle waits between its looks.
            waits.append(secs)
            applying.clear()

        monkeypatch.setattr(auto_update, "_sleep", _wait)
        assert await auto_update.settle_for_restart(3600) is True
        assert waits == [auto_update._SETTLE_POLL_SECS]
        assert auto_update.restart_hold_reason() == auto_update.RESTART_HOLD_REASON

    @pytest.mark.asyncio
    async def test_no_update_starts_while_a_restart_waits(self, ledger_home, apply, monkeypatch):
        # Two apps due: the first is applying when the owner restarts. Once it
        # ends, the loop reaches the second while settle still waits; the hold
        # taken before the wait must put it off, not let it start mid-wait.
        calls, _ = apply
        applying = {"first-app"}
        monkeypatch.setattr(auto_update, "_APPLYING", applying)
        second: list[str] = []

        async def _wait(secs):
            applying.clear()
            second.append(await auto_update.update_one(_State(), _CAND))

        monkeypatch.setattr(auto_update, "_sleep", _wait)
        assert await auto_update.settle_for_restart(3600) is True
        assert second == [""]
        assert calls == []
        assert auto_update.read_ledger() == {}


class TestSelfManagedRefusal:
    def test_the_registry_pipeline_refuses_a_switch_to_self_managed(self):
        source = Path(__file__).resolve().parents[1] / (
            "src/kiro_crew/apps/registry_pipeline/install.py"
        )
        text = source.read_text(encoding="utf-8")
        guard = "if is_self_managed and update_consent.permission_widening_refused():"
        # The pre-clone check refuses an unreadable installed manifest too.
        assert "update_consent.UNREADABLE_BASELINE_REFUSAL" in text
        assert guard in text
        # The refusal sits before the self-managed branch runs its clone.
        assert text.index(guard) < text.index("startup_refusal = await _retained_startup_refusal")


class TestStillACandidate:
    """The locked re-check applies the same judge ``find_candidates`` does."""

    @pytest.fixture()
    def saved(self, monkeypatch):
        import kiro_crew.apps.manager as manager_mod
        import kiro_crew.apps.update_status as status_mod
        import kiro_crew.config as config_mod

        current: dict[str, Any] = {
            "config": _config("on"),
            "records": [_installed("my-app", _CAND.installed)],
            "index": {"my-app": (_CAND.published, None)},
        }

        class _Cfg:
            @staticmethod
            def load():
                value = current["config"]
                if isinstance(value, Exception):
                    raise value
                return value

        monkeypatch.setattr(config_mod, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(manager_mod, "list_apps", lambda: list(current["records"]))
        monkeypatch.setattr(status_mod, "published_index", lambda: current["index"])
        return current

    def test_an_unchanged_candidate_is_no_reason(self, saved):
        assert auto_update.still_candidate_reason(_CAND) == ""
        assert auto_update.find_candidates(saved["config"]) == [_CAND]

    @pytest.mark.parametrize(
        "change, reason",
        [
            ({"config": _config("on", **{"my-app": "never"})}, "update setting is not automatic"),
            ({"config": _config("off")}, "update setting is not automatic"),
            ({"config": _config("ask")}, "update setting is not automatic"),
            ({"config": OSError("gone")}, "update setting could not be read"),
            ({"records": [_installed("my-app", "1.1.0")]}, "installed version changed to 1.1.0"),
            ({"records": []}, "no longer installed"),
            (
                {"records": [_installed("my-app", "1.0.0", source="/home/me/my-app")]},
                "source or lifecycle",
            ),
            ({"records": [_installed("my-app", "1.0.0", lifecycle="app")]}, "source or lifecycle"),
            ({"index": {}}, "no newer version is published"),
            ({"index": {"my-app": ("1.2.0", None)}}, "published version changed to 1.2.0"),
        ],
        ids=[
            "never",
            "off",
            "ask",
            "unreadable-config",
            "update-pressed",
            "uninstalled",
            "local-checkout",
            "app-lifecycle",
            "unpublished",
            "newer-published",
        ],
    )
    def test_any_change_to_what_was_judged_is_a_reason(self, saved, change, reason):
        saved.update(change)
        assert reason in auto_update.still_candidate_reason(_CAND)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [
            {"config": _config("on", **{"my-app": "never"})},
            {"records": [_installed("my-app", _CAND.published)]},
            {"records": [_installed("my-app", _CAND.installed, source="/home/me/my-app")]},
        ],
        ids=["never-saved", "update-pressed", "local-checkout"],
    )
    async def test_a_change_while_an_earlier_app_built_puts_the_candidate_off(
        self, ledger_home, saved, monkeypatch, change
    ):
        # The change lands while the update waits for the lifecycle lock; this
        # is the preflight the Update sequence awaits under it. Nothing is
        # stopped, noted or recorded.
        import kiro_crew.apps.routes as routes_mod

        seen: list[str] = []

        async def _apply(name, **kwargs):
            saved.update(change)
            seen.append(await kwargs["still_idle"]())
            return 409, {"ok": False, "code": "app_busy", "retryable": True, "error": seen[0]}

        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        state = _State()
        assert await auto_update.update_one(state, _CAND) == ""
        assert seen and seen[0]
        assert state.notes == []
        assert "my-app" not in auto_update.read_ledger()


class TestEveryPermissionIsCompared:
    def test_the_widening_check_names_every_permissions_field(self):
        # A field added to Permissions but missing here would read as never
        # widening, so an automatic update could turn it on without consent.
        import dataclasses

        from kiro_crew.apps import update_consent
        from kiro_crew.apps.manifest_sections import Permissions

        compared = (
            set(update_consent._LIST_FIELDS)
            | set(update_consent._BOOL_FIELDS)
            | set(update_consent._NOT_PERMISSIONS)
            | {"memory", "chatLaunch", "browserRecipes"}
        )
        assert compared == {f.name for f in dataclasses.fields(Permissions)}

    def test_an_added_or_changed_browser_recipe_is_a_widening(self):
        # A recipe's grant entry is its whole definition, so a changed step or a
        # watch turned on is a recipe the owner never approved.
        play = {
            "name": "play",
            "site": "https://www.youtube.com/watch",
            "steps": [{"click": "button.ytp-play-button"}],
        }
        skip = {
            "name": "skip_ad",
            "site": "https://www.youtube.com/watch",
            "steps": [{"click": "button.ytp-skip-ad-button"}],
        }
        old = {"browserRecipes": [play]}
        assert widened_permissions(old, {"browserRecipes": [play]}) == []
        assert widened_permissions(old, {"browserRecipes": []}) == []
        assert widened_permissions(old, {"browserRecipes": [play, skip]}) == ["browserRecipes"]
        moved = {**play, "steps": [{"click": "button.another-button"}]}
        assert widened_permissions(old, {"browserRecipes": [moved]}) == ["browserRecipes"]
        watching = {**play, "watch": True}
        assert widened_permissions(old, {"browserRecipes": [watching]}) == ["browserRecipes"]
        assert widened_permissions(None, {"browserRecipes": [play]}) == ["browserRecipes"]

    def test_an_added_kiro_operation_is_a_widening(self):
        # Each name is a call the app makes as the owner's Kiro sign-in.
        old = {"kiroOperations": ["ListThings"]}
        assert widened_permissions(old, {"kiroOperations": ["ListThings", "DeleteThing"]}) == [
            "kiroOperations"
        ]
        assert widened_permissions(old, {"kiroOperations": []}) == []


class TestShutdownNeverCancelsAnApply:
    def test_exit_path_drains_an_applying_update_and_never_cancels_it(self):
        # A cancel lands in the coroutine around the copy worker; its cleanup
        # would restore the checkout while the worker still copies from it.
        source = Path(__file__).resolve().parents[1] / (
            "src/kiro_crew/slack/gateway_runtime/exit_path.py"
        )
        text = source.read_text(encoding="utf-8")
        assert "app_update_drain.add(app_update_task)" in text
        assert "installer_tasks | app_update_drain" in text
        assert "pending & app_update_drain" not in text
        drain_start = text.index("app_update_drain: set[")
        drain_end = text.index("# Stop speculative session starts", drain_start)
        # Only the idle branch cancels: the task is cancelled when nothing applies.
        assert text[drain_start:drain_end].count(".cancel()") == 1
        assert "else:\n            app_update_task.cancel()" in text[drain_start:drain_end]


class TestRoundTwoReview:
    def test_naming_tools_where_there_were_none_narrows(self):
        assert widened_permissions({"mcpTools": []}, {"mcpTools": ["x"]}) == []
        assert widened_permissions({"mcpTools": ["a"]}, {"mcpTools": ["a", "b"]}) == ["mcpTools"]

    @pytest.mark.asyncio
    async def test_the_loop_ends_after_a_stop_request(self, monkeypatch):
        import kiro_crew.config as config_mod

        class _Cfg:
            @staticmethod
            def load():
                return _config("on")

        async def _run_check(state, config):
            auto_update.request_stop()
            return {}

        async def _sleep(secs):
            return None

        monkeypatch.setattr(config_mod, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(auto_update, "run_check", _run_check)
        monkeypatch.setattr(auto_update, "_sleep", _sleep)
        monkeypatch.setattr(auto_update, "_STOP", {"requested": False})
        # The stop request is the signal run_forever returns on. The bound is a
        # lost-run deadline, well under pytest's outer timeout, so a broken stop
        # fails here instead of killing the worker.
        await asyncio.wait_for(auto_update.run_forever(_State()), timeout=30)


class TestRegistryTakeDownRecheck:
    """The registry update takes the app down after the clone and build, so it looks again."""

    @staticmethod
    def _wire(tmp_path, monkeypatch, calls):
        from apps_routes_coverage_helpers import APP, _deregister_into, _install, _setup_env

        import kiro_crew.apps.routes as routes_mod
        from kiro_crew.apps.manager import enable_app

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)

        async def _install_from_registry(name, **kwargs):
            calls.append("install")
            try:
                await kwargs["before_replace"]()
            except Exception as exc:  # noqa: BLE001 - what install_from_registry answers
                return {"ok": False, "name": name, "error": str(exc)}
            calls.append("replace")
            return {"ok": True, "name": name}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)
        monkeypatch.setattr(routes_mod, "deregister_app", _deregister_into(calls))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))
        return APP

    @pytest.mark.asyncio
    async def test_work_that_started_during_the_build_puts_it_off(self, tmp_path, monkeypatch):
        import kiro_crew.apps.routes_install as ri

        calls: list[str] = []
        app = self._wire(tmp_path, monkeypatch, calls)
        answers = iter(["", "a session it owns is running a turn"])

        async def _still_idle():
            return next(answers)

        status, body = await ri.apply_app_update(
            app, source=f"registry:{app}", still_idle=_still_idle
        )
        assert (status, body["code"], body["retryable"]) == (409, "app_busy", True)
        # Nothing was stopped, deregistered or replaced: the app keeps running.
        assert calls == ["install"], calls

    @pytest.mark.asyncio
    async def test_an_idle_app_is_taken_down_only_before_the_copy(self, tmp_path, monkeypatch):
        import kiro_crew.apps.routes_install as ri

        calls: list[str] = []
        app = self._wire(tmp_path, monkeypatch, calls)
        looks: list[str] = []

        async def _still_idle():
            looks.append("look")
            return ""

        status, _ = await ri.apply_app_update(app, source=f"registry:{app}", still_idle=_still_idle)
        assert status == 200
        assert looks == ["look", "look"]
        assert calls[:4] == ["install", "stop", "deregister (agents kept)", "replace"], calls


class TestAnUnpressedUpdateLeavesOpenChatsAlone:
    """The Update route reaches open chats; the automatic updater must not reset them."""

    @pytest.mark.asyncio
    async def test_apply_without_a_reach_never_touches_open_chats(self, tmp_path, monkeypatch):
        from apps_routes_coverage_helpers import APP, _install, _setup_env

        import kiro_crew.apps.routes as routes_mod
        import kiro_crew.apps.routes_install as ri
        import kiro_crew.dashboard.handlers.template_server_request as tsr
        from kiro_crew.apps.manager import enable_app

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, mcpServers={"srv": {"url": "http://127.0.0.1:7778/mcp"}})
        assert enable_app(APP).ok
        reached: list[object] = []

        async def _reach(request: object) -> int:
            reached.append(request)
            return 0

        async def _registry_install(name, **kwargs):
            await kwargs["before_replace"]()
            return {"ok": True, "name": name}

        monkeypatch.setattr(tsr, "_reach_sessions", _reach)
        monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(routes_mod, "install_from_registry", _registry_install)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        # What auto_update.update_one passes: no reach_sessions.
        status, _ = await ri.apply_app_update(APP, source=f"registry:{APP}")
        assert status == 200
        assert reached == []

        # The route's reach runs for the same update.
        async def _route_reach():
            await tsr._reach_sessions("request")

        status, _ = await ri.apply_app_update(
            APP, source=f"registry:{APP}", reach_sessions=_route_reach
        )
        assert status == 200
        assert reached == ["request"]
