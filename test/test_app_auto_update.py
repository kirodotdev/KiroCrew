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


def _config(mode: str = "off", hours: int = 6, **overrides: str) -> Any:
    return types.SimpleNamespace(
        app_updates=AppUpdatesConfig(mode=mode, check_interval_hours=hours),
        apps={name: AppSettingsConfig(auto_update=value) for name, value in overrides.items()},
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_are_off_and_six_hours(self):
        assert _parse_app_updates({}) == AppUpdatesConfig(mode="off", check_interval_hours=6)

    @pytest.mark.parametrize("mode", ["off", "ask", "on"])
    def test_known_modes_are_kept(self, mode):
        assert _parse_app_updates({"mode": mode}).mode == mode

    @pytest.mark.parametrize("raw", ["ON", "yes", True, 1, None])
    def test_an_unreadable_mode_is_off(self, raw):
        assert _parse_app_updates({"mode": raw}).mode == "off"

    @pytest.mark.parametrize(("raw", "hours"), [(0, 1), (-3, 1), (500, 168), ("x", 6), (12, 12)])
    def test_the_interval_is_clamped_not_refused(self, raw, hours):
        assert _parse_app_updates({"check_interval_hours": raw}).check_interval_hours == hours

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

        assert _editable_config_spec("app_updates.mode")["values"] == ["off", "ask", "on"]
        hours = _editable_config_spec("app_updates.check_interval_hours")
        assert (hours["min"], hours["max"]) == (1, 168)
        assert _editable_config_spec("apps.my-app.auto_update")["values"] == ["", "always", "never"]
        assert _editable_config_spec("apps.Not_Kebab.auto_update") is None


class TestEffectiveMode:
    def test_never_wins_over_on(self):
        assert auto_update.effective_mode(_config("on", x="never"), "x") == "never"

    def test_always_wins_over_off(self):
        assert auto_update.effective_mode(_config("off", x="always"), "x") == "on"

    def test_no_override_follows_the_global_mode(self):
        assert auto_update.effective_mode(_config("ask"), "x") == "ask"

    def test_checks_run_when_one_app_is_always(self):
        assert not auto_update.checks_wanted(_config("ask"))
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

    def test_ask_and_off_apps_are_never_candidates(self, store):
        apps, index = store
        apps.append(_installed("a", "1.0.0"))
        index["a"] = ("1.1.0", None)
        assert auto_update.find_candidates(_config("ask")) == []
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


class TestBusyReason:
    def test_an_idle_app_is_not_busy(self):
        state = types.SimpleNamespace(_slots={}, crons=None)
        assert auto_update.busy_reason(state, "x") == ""
        assert auto_update.cron_busy_reason(state, "x") == ""

    def test_a_turn_in_one_of_its_sessions_is_busy(self):
        slot = types.SimpleNamespace(_app="x", task=_Task(done=False))
        other = types.SimpleNamespace(_app="y", task=_Task(done=False))
        assert "turn" in auto_update.busy_reason(types.SimpleNamespace(_slots={"s": slot}), "x")
        assert auto_update.busy_reason(types.SimpleNamespace(_slots={"s": other}), "x") == ""

    def test_a_finished_turn_is_not_busy(self):
        slot = types.SimpleNamespace(_app="x", task=_Task(done=True))
        assert auto_update.busy_reason(types.SimpleNamespace(_slots={"s": slot}), "x") == ""

    def test_a_running_cron_of_its_own_is_busy(self, monkeypatch):
        import kiro_crew.apps.cron_sdk as cron_sdk_mod

        monkeypatch.setattr(cron_sdk_mod, "owner_tag", lambda name: f"app:{name}")
        state = types.SimpleNamespace(_slots={}, crons=_Crons({"j1"}))
        assert "cron" in auto_update.cron_busy_reason(state, "busy-app")
        state.crons.running.clear()
        assert auto_update.cron_busy_reason(state, "busy-app") == ""

    def test_unreadable_cron_jobs_count_as_busy(self):
        class _Broken:
            def list_jobs(self, include_disabled=False):
                raise OSError("store busy")

        state = types.SimpleNamespace(_slots={}, crons=_Broken())
        assert "could not be read" in auto_update.cron_busy_reason(state, "x")

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
        self.notes.append((title, channel, {"meta": meta, "url": url, "actions": actions}))


@pytest.fixture(autouse=True)
def _fresh_recorded(_floor_monkeypatch):
    """The in-process outcome copy and the restart hold are module state; each test starts clear."""
    _floor_monkeypatch.setattr(auto_update, "_RECORDED", {})
    _floor_monkeypatch.setattr(auto_update, "_RESTART_HOLD", {"holders": 0})


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
    async def test_needs_consent_posts_an_ask_note_and_is_not_retried(self, ledger_home, apply):
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
        _, answers = apply
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        answers.append((400, {"ok": False, "error": f"clone failed with token {secret}"}))
        state = _State()
        await auto_update.update_one(state, _CAND)
        assert secret not in json.dumps(state.notes)

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
        monkeypatch.setattr(auto_update, "cron_busy_reason", lambda state, name: "busy")
        assert await auto_update.update_one(_State(), _CAND) == ""
        assert calls == []


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
        assert await auto_update.run_check(_State(), _config("ask")) == {}

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

        configs = [_config("on", hours=1)] * 3 + [_config("off")]
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
        # One check on the first pass; the next passes fall inside the hour.
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

    def test_ask_puts_an_update_button_on_the_note(self, ledger_home):
        notes = self._notes(_config("ask", b="never"))
        by_app = {n["meta"]["app"]: n for n in notes}
        assert by_app["a"]["actions"] == [
            {"id": "update", "label": "Update", "url": "/apps/detail/a"}
        ]
        # A pinned app is still told a newer version exists, with no button.
        assert "actions" not in by_app["b"]
        # The body points at the button it sits beside, not at opening the note.
        assert by_app["a"]["body"].endswith("Press Update to install it.")
        assert by_app["b"]["body"].endswith("Open it to update.")

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
        monkeypatch.setattr(ri, "stop_app_backend", lambda name: steps.append("stop"))
        monkeypatch.setattr(ri, "_deregister_app_off_loop", _stop_line)
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

        class _Reached(Exception):
            pass

        def _stop(name):
            raise _Reached

        monkeypatch.setattr(ri, "stop_app_backend", _stop)
        with pytest.raises(_Reached):
            await ri.apply_app_update("auto-app", source="registry:auto-app")

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

    def test_no_hold_while_an_update_applies(self, monkeypatch):
        monkeypatch.setattr(auto_update, "_APPLYING", {"my-app"})
        assert auto_update.hold_for_restart() is False
        assert auto_update.restart_hold_reason() == ""

    def test_a_hold_is_taken_and_released_when_nothing_applies(self, monkeypatch):
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        assert auto_update.hold_for_restart() is True
        assert auto_update.restart_hold_reason() == auto_update.RESTART_HOLD_REASON
        auto_update.release_restart_hold()
        assert auto_update.restart_hold_reason() == ""

    @pytest.mark.asyncio
    async def test_a_held_loop_starts_no_update_and_records_nothing(
        self, ledger_home, apply, monkeypatch
    ):
        calls, _ = apply
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        assert auto_update.hold_for_restart()
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
        monkeypatch.setattr(auto_update, "cron_busy_reason", lambda state, name: "")
        looks: list[str] = []

        def _busy(state, name):
            if not looks:
                assert auto_update.hold_for_restart()
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
    async def test_settle_holds_at_once_when_nothing_applies(self, monkeypatch):
        monkeypatch.setattr(auto_update, "_APPLYING", set())
        assert await auto_update.settle_for_restart(0) is True
        assert auto_update.restart_hold_reason() == auto_update.RESTART_HOLD_REASON

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

    @pytest.mark.asyncio
    async def test_a_restart_that_cannot_wait_leaves_a_waiting_one_held(self, monkeypatch):
        applying = {"my-app"}
        monkeypatch.setattr(auto_update, "_APPLYING", applying)
        seen: list[str] = []

        async def _wait(secs):
            # The coordinator's restart, which defers rather than wait.
            assert auto_update.hold_for_restart() is False
            seen.append(auto_update.restart_hold_reason())
            applying.clear()

        monkeypatch.setattr(auto_update, "_sleep", _wait)
        assert await auto_update.settle_for_restart(3600) is True
        assert seen == [auto_update.RESTART_HOLD_REASON]
        auto_update.release_restart_hold()
        assert auto_update.restart_hold_reason() == ""


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

        compared = set(update_consent._LIST_FIELDS) | set(update_consent._BOOL_FIELDS) | {"memory"}
        assert compared == {f.name for f in dataclasses.fields(Permissions)}


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
