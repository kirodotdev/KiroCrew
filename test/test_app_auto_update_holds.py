"""An automatic app update waits for the owner, or for a later check, in these cases.

A version that adds ``network`` is refused by the real Update sequence
(``routes_install.apply_app_update`` with ``refuse_permission_widening=True``), from
a folder and from a registry, before any installed file moves. A memory consent
that names the installed version holds the update and posts the needs-consent
note, since the update would end that consent and ask again. A gateway that is
stopping starts no update, and one still cloning, building or making its last
look when the stop comes is put off before its backend is taken down. A later
boot in the same process starts with no stop, hold or kept outcome from the
earlier one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kiro_crew.apps import auto_update
from kiro_crew.apps.update_consent import NEEDS_CONSENT_CODE

_CAND = auto_update.Candidate("cov-test-app", "1.0.0", "1.1.0")
_STAMP = "2026-11-01T00:00:00Z"


class _State:
    def __init__(self) -> None:
        self._slots: dict = {}
        self.crons = None
        self.notes: list[tuple[str, str, dict]] = []

    def notify(self, kind, title, body, *, meta=None, url=None, actions=None, channel=None):
        self.notes.append((title, channel, {"body": body, "meta": meta, "actions": actions}))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from apps_routes_coverage_helpers import _setup_env

    return _setup_env(tmp_path, monkeypatch)


def _manifest_of(name: str) -> Any:
    from kiro_crew.apps.manager import get_app_manifest

    return get_app_manifest(name)


# ---------------------------------------------------------------------------
# The real Update sequence refuses an added permission
# ---------------------------------------------------------------------------


class TestARealUpdateThatAddsNetworkIsRefused:
    @pytest.mark.asyncio
    async def test_from_a_folder_nothing_is_replaced(self, tmp_path, home, monkeypatch):
        from apps_routes_coverage_helpers import APP, _install

        import kiro_crew.apps.routes as routes_mod
        import kiro_crew.apps.routes_install as ri
        from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

        _install(tmp_path)
        source = tmp_path / "source" / APP
        manifest = json.loads((source / APP_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        manifest.update(version="1.1.0", permissions={"network": True})
        (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        status, body = await ri.apply_app_update(APP, refuse_permission_widening=True)

        assert status >= 400, body
        assert body["code"] == NEEDS_CONSENT_CODE
        assert body["error"].endswith(": network")
        installed = _manifest_of(APP)
        assert (installed.version, installed.permissions.network) == ("1.0.0", False)

    @pytest.mark.asyncio
    async def test_from_a_registry_it_is_refused_before_the_clone(
        self, tmp_path, home, monkeypatch
    ):
        from apps_routes_coverage_helpers import APP, _install

        import kiro_crew.apps.registry as registry
        import kiro_crew.apps.routes as routes_mod
        import kiro_crew.apps.routes_install as ri

        _install(tmp_path)
        repo = "https://example.com/cov-test-app.git"
        entry = {"name": APP, "repo": repo, "branch": "main"}
        published = {
            "name": APP,
            "version": "1.1.0",
            "displayName": "Coverage Test App",
            "description": "App for routes coverage testing",
            "author": "tester",
            "permissions": {"network": True},
        }

        async def _fetch_manifest(*args: Any, **kwargs: Any) -> dict:
            return published

        cloned: list[str] = []

        async def _clone_build(*args: Any, **kwargs: Any) -> dict:
            cloned.append("clone")
            raise AssertionError("cloned a version whose manifest adds a permission")

        # The network and trust edges only: every gate between them and the
        # widening check runs as it does for a real registry update.
        monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(registry, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(registry, "_resolve_install_entry", lambda n: (entry, ""))
        monkeypatch.setattr(registry, "repository_bound_grant_denied", lambda n, **k: "")
        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch_manifest)
        monkeypatch.setattr(registry, "_clone_build_app", _clone_build)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        status, body = await ri.apply_app_update(
            APP, source=f"registry:{APP}", refuse_permission_widening=True
        )

        assert status >= 400, body
        assert body["code"] == NEEDS_CONSENT_CODE, body
        assert body["error"].endswith(": network")
        assert cloned == []
        installed = _manifest_of(APP)
        assert (installed.version, installed.permissions.network) == ("1.0.0", False)


# ---------------------------------------------------------------------------
# A memory consent on the installed version holds the update
# ---------------------------------------------------------------------------


class TestAMemoryConsentHoldsTheUpdate:
    @pytest.fixture()
    def installed(self, home, monkeypatch):
        import types

        from kiro_crew.memory_provider import app_client

        now = {"version": "1.0.0", "stamp": _STAMP}
        monkeypatch.setattr(
            app_client,
            "_manifest",
            lambda app: types.SimpleNamespace(version=now["version"]) if app == "mem-app" else None,
        )
        monkeypatch.setattr(app_client, "_installed_at", lambda app: now["stamp"])
        return now

    def test_only_a_grant_on_the_installed_version_of_this_install_would_end(self, installed):
        from kiro_crew.memory_provider.consent import (
            grant_egress,
            revoke_egress,
            update_would_end_consent,
        )

        assert not update_would_end_consent("mem-app")
        grant_egress("app:mem-app/notes", "1.0.0", _STAMP)
        assert update_would_end_consent("mem-app")
        # Another app's grant, an older version's, or an earlier install's ends nothing.
        assert not update_would_end_consent("mem")
        installed["version"] = "0.9.0"
        assert not update_would_end_consent("mem-app")
        installed["version"] = "1.0.0"
        installed["stamp"] = "2026-11-02T00:00:00Z"
        assert not update_would_end_consent("mem-app")
        installed["stamp"] = _STAMP
        revoke_egress("app:mem-app/notes")
        assert not update_would_end_consent("mem-app")

    @pytest.mark.asyncio
    async def test_the_update_posts_the_needs_consent_note_and_settles(self, home, monkeypatch):
        import kiro_crew.apps.routes as routes_mod
        from kiro_crew.memory_provider import consent

        async def _apply(name: str, **kwargs: Any) -> tuple[int, dict]:
            raise AssertionError("an update that would end a memory consent was applied")

        monkeypatch.setattr(consent, "update_would_end_consent", lambda name: True)
        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        state = _State()

        assert await auto_update.update_one(state, _CAND) == auto_update.OUTCOME_NEEDS_CONSENT

        title, channel, extra = state.notes[0]
        assert (title, channel) == ("cov-test-app 1.1.0 needs your review", "system.apps")
        assert extra["body"] == (
            "Updating it would end the memory consent you gave this version. Review to update."
        )
        assert extra["meta"]["needsConsent"] is True
        assert auto_update.already_settled(auto_update.read_ledger(), _CAND)

    @pytest.mark.asyncio
    async def test_a_grant_given_while_it_builds_puts_it_off(self, home, monkeypatch):
        import kiro_crew.apps.routes as routes_mod
        from kiro_crew.memory_provider import consent

        granted = {"now": False}
        seen: list[str] = []

        async def _apply(name: str, **kwargs: Any) -> tuple[int, dict]:
            granted["now"] = True
            seen.append(await kwargs["still_idle"]())
            return 409, {"ok": False, "code": "app_busy", "retryable": True, "error": seen[0]}

        monkeypatch.setattr(consent, "update_would_end_consent", lambda name: granted["now"])
        monkeypatch.setattr(auto_update, "still_candidate_reason", lambda candidate: "")
        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        state = _State()

        assert await auto_update.update_one(state, _CAND) == ""

        assert seen == [auto_update.MEMORY_CONSENT_REFUSAL]
        assert state.notes == []
        assert _CAND.name not in auto_update.read_ledger()


# ---------------------------------------------------------------------------
# A stopping gateway
# ---------------------------------------------------------------------------


class TestAStoppingGatewayStartsNoUpdate:
    @pytest.mark.asyncio
    async def test_a_stop_before_the_first_look_applies_nothing(self, home, monkeypatch):
        import kiro_crew.apps.routes as routes_mod

        async def _apply(name: str, **kwargs: Any) -> tuple[int, dict]:
            raise AssertionError("an update started after the stop request")

        monkeypatch.setattr(routes_mod, "apply_app_update", _apply)
        auto_update.request_stop()
        state = _State()

        assert await auto_update.update_one(state, _CAND) == ""
        assert state.notes == [] and auto_update.read_ledger() == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_during", ["build", "take_down_reads"])
    async def test_a_stop_before_the_take_down_puts_it_off(
        self, stop_during, tmp_path, home, monkeypatch
    ):
        from apps_routes_coverage_helpers import APP, _deregister_into, _install

        import kiro_crew.apps.routes as routes_mod
        from kiro_crew.apps.manager import enable_app

        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        looks: list[str] = []

        async def _install_from_registry(name: str, **kwargs: Any) -> dict:
            calls.append("install")
            if stop_during == "build":
                # The gateway begins stopping while this update clones and builds.
                auto_update.request_stop()
            try:
                await kwargs["before_replace"]()
            except Exception as exc:  # noqa: BLE001 - what install_from_registry answers
                return {"ok": False, "name": name, "error": str(exc)}
            calls.append("replace")
            return {"ok": True, "name": name}

        def _still_candidate(candidate: auto_update.Candidate) -> str:
            looks.append(candidate.name)
            if stop_during == "take_down_reads" and len(looks) == 2:
                # The second look is the take-down's: the gateway begins
                # stopping while that look reads the disk.
                auto_update.request_stop()
            return ""

        # The app is installed from a folder and read as a registry app here, so
        # the candidate recheck would refuse it for its source on the first look,
        # before the stop this test is about.
        monkeypatch.setattr(auto_update, "still_candidate_reason", _still_candidate)
        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
        monkeypatch.setattr(routes_mod, "install_from_registry", _install_from_registry)
        monkeypatch.setattr(routes_mod, "deregister_app", _deregister_into(calls))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))
        state = _State()

        assert await auto_update.update_one(state, _CAND) == ""

        # Nothing was stopped, deregistered or replaced: exit cannot cut a swap.
        assert calls == ["install"], calls
        assert state.notes == [] and auto_update.read_ledger() == {}


# ---------------------------------------------------------------------------
# A later boot in the same process
# ---------------------------------------------------------------------------


class TestALaterBootStartsClear:
    def test_it_forgets_the_stop_holds_and_outcomes_but_keeps_an_applying_app(
        self, home, monkeypatch
    ):
        import kiro_crew.atomic_write as atomic_write_mod

        def _full_disk(path: Any, content: Any, **kwargs: Any) -> None:
            raise OSError("no space left on device")

        # With the write refused, the outcome lives only in this process's copy.
        monkeypatch.setattr(atomic_write_mod, "atomic_write", _full_disk)
        auto_update.request_stop()
        auto_update._RESTART_HOLD["holders"] += 1
        auto_update.record(_CAND.name, _CAND.published, auto_update.OUTCOME_FAILED, attempts=1)
        auto_update._APPLYING.add(_CAND.name)
        assert _CAND.name in auto_update.read_ledger()

        auto_update.reset_for_boot()

        assert auto_update.stop_reason() == ""
        assert auto_update.restart_hold_reason() == ""
        assert auto_update.read_ledger() == {}
        # A worker thread can still be copying that update; its own finally ends it.
        assert auto_update.applying()

    @pytest.mark.asyncio
    async def test_a_loop_started_after_a_stopped_one_runs_its_first_check(self, monkeypatch):
        import kiro_crew.config as config_mod

        checks: list[Any] = []

        class _Cfg:
            @staticmethod
            def load() -> Any:
                return object()

        async def _run_check(state: Any, config: Any) -> dict:
            checks.append(config)
            auto_update.request_stop()
            return {}

        async def _sleep(secs: float) -> None:
            return None

        monkeypatch.setattr(config_mod, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr(auto_update, "checks_wanted", lambda config: True)
        monkeypatch.setattr(auto_update, "run_check", _run_check)
        monkeypatch.setattr(auto_update, "_sleep", _sleep)
        # The first boot's shutdown. The bound on each run is a lost-run deadline,
        # well under pytest's outer timeout, so a loop that never ends fails here.
        auto_update.request_stop()
        await asyncio.wait_for(auto_update.run_forever(_State()), timeout=30)
        assert checks == []

        auto_update.reset_for_boot()
        await asyncio.wait_for(auto_update.run_forever(_State()), timeout=30)
        assert len(checks) == 1
