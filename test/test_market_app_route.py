"""Tests for routing a ``kind = app`` marketplace plan to the app installer.

A plan whose requested item is an app is not installed by the edition's package
path. ``market_apply`` hands it to the gateway app installer
(``kiro_crew.apps.marketplace_install``): resolve, download, verify the sha256,
unpack safely, read the capabilities in the folder, and return the confirm data
(the plan's permission union plus ``inFolder``) WITHOUT installing. Only an
explicit yes on ``/api/capability/market/confirm``, after the plan digest is
re-checked, runs ``complete_market_install``, which installs the app disabled with
nothing registered. Plans for skills, MCP servers and themes are unchanged.

The installer is replaced by a fake here: what it does with the folder is tested
with the installer itself.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.platform.capability_bound as capability_bound
import kiro_crew.platform.context as platform_context
from kiro_crew.apps import marketplace_install
from kiro_crew.dashboard.handlers import market
from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.platform.capability_bound import BoundedCapabilityManager, MarketAppPending
from kiro_crew.platform.interfaces import CapabilityResult

_FIXTURE = Path(__file__).parent / "fixtures" / "market_install_plan.json"
_APP_REF = "team-a/demo-app"
_APP_DIGEST = "a" * 64
_SKILL_REF = "team-a/pdf-tools"
_SKILL_DIGEST = "5016ee2afad0fa54c71fa8cf4dbb57afb85ea238cb68911db0e4f5875b99156c"
_IN_FOLDER = {"crons": ["daily-digest"], "mcpServers": ["tickets"], "agents": []}


def _skill_plan() -> Dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _app_plan(digest: str = _APP_DIGEST) -> Dict[str, Any]:
    plan = _skill_plan()
    item = copy.deepcopy(plan["items"][0])
    item.update(ref=_APP_REF, kind="app", version=3, target="$HOME/.kiro/crew/apps/demo-app")
    plan["request"] = {"ref": _APP_REF, "range": None}
    plan["items"] = [item]
    plan["planDigest"] = digest
    return plan


#: The real preview reader, before the autouse fixture below replaces it.
_REAL_MARKET_APPS_ENABLED = getattr(capability_bound, "market_apps_enabled", None)


@pytest.fixture(autouse=True)
def _market_apps_preview_on(_floor_monkeypatch):
    """Every test here runs the app route with its preview turned ON.

    The route is held behind ``dashboard.feature_previews.marketplace_apps``
    (default off); ``TestThePreviewFlag`` turns it back off, or reads the real
    config, to pin what OFF does.
    """
    _floor_monkeypatch.setattr(capability_bound, "market_apps_enabled", lambda: True, raising=False)


@pytest.fixture()
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kiro" / "crew"))
    return tmp_path


class _Edition:
    """An edition manager with the marketplace ops and a listing client."""

    def __init__(self, plan: Dict[str, Any]) -> None:
        self.plan = plan
        self.client = object()
        self.calls: List[tuple] = []

    def available(self) -> bool:
        return True

    def market_available(self) -> bool:
        return True

    def market_client(self) -> Any:
        return self.client

    async def market_plan(self, ref: str) -> Dict[str, Any]:
        self.calls.append(("plan", ref))
        return self.plan

    async def market_apply(self, ref: str, plan_digest: str) -> CapabilityResult:
        self.calls.append(("apply", ref, plan_digest))
        return CapabilityResult(ok=True)


class _Prepared:
    def __init__(self, ref: str = _APP_REF, version: int = 3, sha256: str = "") -> None:
        root = marketplace_install.market_staging_root()
        root.mkdir(parents=True, exist_ok=True)
        self.staging_dir = Path(tempfile.mkdtemp(prefix="install-", dir=root))
        self.resolved = SimpleNamespace(
            ref=ref,
            version=version,
            content_sha256=sha256 or _skill_plan()["items"][0]["sha256"],
            permissions={
                "commands": ["uvx docs-mcp-server==1.2.3"],
                "secrets": ["DOCS_TOKEN"],
            },
        )
        self.confirm = {"inFolder": _IN_FOLDER, "app": {"ref": _APP_REF}}
        self.discarded = False
        self.discard_threads: List[threading.Thread] = []

    def discard(self) -> None:
        self.discarded = True
        self.discard_threads.append(threading.current_thread())
        shutil.rmtree(self.staging_dir, ignore_errors=True)


class _Installer:
    """Stands in for ``kiro_crew.apps.marketplace_install``."""

    def __init__(self) -> None:
        self.prepared: List[tuple] = []
        self.completed: List[tuple] = []
        self.last: _Prepared | None = None
        #: Overrides for the release the fake "resolves" (a listing that moved on).
        self.resolve_as: Dict[str, Any] = {}

    market_staging_root = staticmethod(marketplace_install.market_staging_root)

    def prepare_market_install(self, client: Any, ref: str, version: int | None = None) -> Any:
        self.prepared.append((client, ref, version))
        kw: Dict[str, Any] = {"ref": ref, "version": version if version is not None else 4}
        kw.update(self.resolve_as)
        self.last = _Prepared(**kw)
        return self.last

    def complete_market_install(self, prepared: Any, *, confirmed: bool) -> Any:
        self.completed.append((prepared, confirmed))
        prepared.discard()
        return SimpleNamespace(ok=True, name="demo-app", error="", error_code="")


@pytest.fixture()
def installer(monkeypatch):
    fake = _Installer()
    monkeypatch.setattr(capability_bound, "_app_installer", fake)
    return fake


@pytest.fixture()
def no_registration(monkeypatch):
    """Registering anything after a marketplace app install is a failure."""

    def _boom(*_a, **_k):
        raise AssertionError("a marketplace app install must register nothing")

    monkeypatch.setattr("kiro_crew.apps.bridges_register.register_app", _boom)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.market.rebuild_agent_spec", _boom)


class TestApplyRoutesAnApp:
    def test_an_app_plan_goes_to_the_app_installer_not_the_package_path(self, temp_home, installer):
        edition = _Edition(_app_plan())
        res = asyncio.run(BoundedCapabilityManager(edition).market_apply(_APP_REF, _APP_DIGEST))
        assert isinstance(res, MarketAppPending)
        assert res.ok is True
        assert installer.prepared == [(edition.client, _APP_REF, 3)]
        assert installer.completed == [], "nothing is installed before the yes"
        assert ("apply", _APP_REF, _APP_DIGEST) not in edition.calls
        assert res.confirm["inFolder"] == _IN_FOLDER
        assert res.confirm["planPermissions"] == edition.plan["permissions"]
        assert res.token

    def test_an_edition_without_a_listing_client_refuses_an_app(self, temp_home, installer):
        edition = _Edition(_app_plan())
        edition.market_client = None  # type: ignore[assignment]
        res = asyncio.run(BoundedCapabilityManager(edition).market_apply(_APP_REF, _APP_DIGEST))
        assert res.ok is False and not isinstance(res, MarketAppPending)
        assert installer.prepared == [] and edition.calls == [("plan", _APP_REF)]


class TestCompleteAfterTheYes:
    def test_the_yes_installs_through_the_installer_and_registers_nothing(
        self, temp_home, installer, no_registration
    ):
        edition = _Edition(_app_plan())
        bounded = BoundedCapabilityManager(edition)
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        res = asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert res.ok is True
        assert installer.completed == [(installer.last, True)]
        assert ("apply", _APP_REF, _APP_DIGEST) not in edition.calls

    def test_the_yes_drops_the_proxy_key_cached_for_the_app(self, temp_home, installer):
        # A reinstall writes a new proxy key; a key cached from the previous
        # install would sign proxied requests the new backend rejects (401).
        from kiro_crew.apps import routes_proxy

        routes_proxy._app_secret_cache["demo-app"] = "stale-key"
        try:
            bounded = BoundedCapabilityManager(_Edition(_app_plan()))
            pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
            res = asyncio.run(
                bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
            )
            assert res.ok is True
            assert "demo-app" not in routes_proxy._app_secret_cache
        finally:
            routes_proxy._app_secret_cache.pop("demo-app", None)

    def test_a_changed_digest_at_complete_refuses_and_writes_nothing(self, temp_home, installer):
        edition = _Edition(_app_plan())
        bounded = BoundedCapabilityManager(edition)
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        edition.plan = _app_plan(digest="b" * 64)
        res = asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert res.ok is False
        assert installer.completed == []
        assert installer.last is not None and installer.last.discarded is True

    def test_no_yes_or_an_unknown_token_installs_nothing(self, temp_home, installer):
        edition = _Edition(_app_plan())
        bounded = BoundedCapabilityManager(edition)
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        assert (
            asyncio.run(
                bounded.market_app_complete(_APP_REF, _APP_DIGEST, "nope", confirmed=True)
            ).ok
            is False
        )
        res = asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=False)
        )
        assert res.ok is False
        assert installer.completed == []
        assert installer.last.discarded is True
        # The token is spent: a second try finds nothing.
        assert (
            asyncio.run(
                bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
            ).ok
            is False
        )


class TestRefusalsAroundTheInstaller:
    def test_governance_is_asked_again_at_the_yes(self, temp_home, installer, monkeypatch):
        bounded = BoundedCapabilityManager(_Edition(_app_plan()))
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        monkeypatch.setattr(capability_bound._gov, "extensions_off", lambda action: "off now")
        res = asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert res.ok is False and "off now" in res.message
        assert installer.completed == [] and installer.last.discarded is True

    def test_a_bundled_skill_denied_after_apply_stops_the_install(
        self, temp_home, installer, monkeypatch
    ):
        plan = _app_plan()
        skill = copy.deepcopy(_skill_plan()["items"][0])
        skill.update(reason="DEPENDENCY", requiredBy=[_APP_REF])
        plan["items"].append(skill)
        bounded = BoundedCapabilityManager(_Edition(plan))
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        assert isinstance(pending, MarketAppPending)
        staging = installer.last.staging_dir
        assert staging.exists()
        asked: List[tuple] = []

        def _deny(kind, ident):
            asked.append((kind, ident))
            return "pdf-tools is on the deny list" if kind == "skill" else None

        monkeypatch.setattr(capability_bound._gov, "extension_install_denied", _deny)
        res = asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert res.ok is False and "deny list" in res.message
        assert ("skill", "pdf-tools") in asked
        assert installer.completed == [], "install_app is never reached"
        assert not staging.exists()

    def test_a_prepared_install_expires_and_its_staging_is_deleted(
        self, temp_home, installer, monkeypatch
    ):
        bounded = BoundedCapabilityManager(_Edition(_app_plan()))
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        later = capability_bound._clock() + capability_bound.MARKET_APP_PENDING_TTL + 1
        monkeypatch.setattr(capability_bound, "_clock", lambda: later)
        res = asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert res.ok is False
        assert installer.completed == [] and installer.last.discarded is True


class TestDiscardRunsOffTheLoop:
    """``discard`` is a ``shutil.rmtree``: it must never run on the gateway loop."""

    def _loop_thread_and_pending(self, bounded):
        async def go():
            pending = await bounded.market_apply(_APP_REF, _APP_DIGEST)
            return threading.current_thread(), pending

        return asyncio.run(go())

    def test_a_refused_yes_discards_off_the_loop(self, temp_home, installer):
        bounded = BoundedCapabilityManager(_Edition(_app_plan()))

        async def go():
            pending = await bounded.market_apply(_APP_REF, _APP_DIGEST)
            await bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=False)
            return threading.current_thread()

        loop_thread = asyncio.run(go())
        assert installer.last is not None and installer.last.discard_threads
        assert all(t is not loop_thread for t in installer.last.discard_threads)

    def test_ttl_expiry_discards_off_the_loop(self, temp_home, installer, monkeypatch):
        bounded = BoundedCapabilityManager(_Edition(_app_plan()))
        loop_thread, pending = self._loop_thread_and_pending(bounded)
        later = capability_bound._clock() + capability_bound.MARKET_APP_PENDING_TTL + 1
        monkeypatch.setattr(capability_bound, "_clock", lambda: later)
        asyncio.run(
            bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert installer.last is not None and installer.last.discard_threads
        assert all(t is not loop_thread for t in installer.last.discard_threads)

    def test_a_stale_prepared_install_for_the_same_ref_discards_off_the_loop(
        self, temp_home, installer
    ):
        bounded = BoundedCapabilityManager(_Edition(_app_plan()))
        loop_thread, _first = self._loop_thread_and_pending(bounded)
        stale = installer.last
        self._loop_thread_and_pending(bounded)
        assert stale is not None and stale.discard_threads
        assert all(t is not loop_thread for t in stale.discard_threads)


class TestThePinnedReleaseIsTheOneInstalled:
    """The person approves one release; no other release may be installed."""

    def _unpinned_plan(self) -> Dict[str, Any]:
        plan = _app_plan()
        del plan["items"][0]["version"]
        return plan

    def test_a_plan_item_without_a_version_is_not_showable(self):
        assert market.plan_is_showable(_app_plan(), apps=True) is True
        assert market.plan_is_showable(self._unpinned_plan(), apps=True) is False
        plan = _app_plan()
        plan["items"][0]["version"] = "3"
        assert market.plan_is_showable(plan, apps=True) is False

    def test_apply_refuses_an_unpinned_app_without_preparing(self, temp_home, installer):
        res = asyncio.run(
            BoundedCapabilityManager(_Edition(self._unpinned_plan())).market_apply(
                _APP_REF, _APP_DIGEST
            )
        )
        assert res.ok is False and not isinstance(res, MarketAppPending)
        assert "pinned version" in res.message
        assert installer.prepared == []

    @pytest.mark.parametrize(
        "resolve_as, what",
        [
            ({"version": 4}, "version"),
            ({"sha256": "f" * 64}, "sha256"),
        ],
    )
    def test_a_release_other_than_the_plans_is_refused_and_its_staging_deleted(
        self, temp_home, installer, resolve_as, what
    ):
        installer.resolve_as = resolve_as
        res = asyncio.run(
            BoundedCapabilityManager(_Edition(_app_plan())).market_apply(_APP_REF, _APP_DIGEST)
        )
        assert res.ok is False and not isinstance(res, MarketAppPending)
        assert what in res.message
        assert installer.last is not None and not installer.last.staging_dir.exists()

    def test_commands_or_secrets_other_than_the_dialogs_are_refused(self, temp_home, installer):
        plan = _app_plan()
        plan["permissions"]["secrets"] = ["OTHER_TOKEN"]
        res = asyncio.run(
            BoundedCapabilityManager(_Edition(plan)).market_apply(_APP_REF, _APP_DIGEST)
        )
        assert res.ok is False and "secrets" in res.message
        assert installer.last is not None and not installer.last.staging_dir.exists()


class TestStagingNeverLeaks:
    def test_a_confirm_whose_plan_reread_raises_leaves_no_staging(self, temp_home, installer):
        edition = _Edition(_app_plan())
        bounded = BoundedCapabilityManager(edition)
        pending = asyncio.run(bounded.market_apply(_APP_REF, _APP_DIGEST))
        staging = installer.last.staging_dir
        assert staging.exists()

        async def _boom(_ref):
            raise RuntimeError("edition fault")

        edition.market_plan = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            asyncio.run(
                bounded.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
            )
        assert not staging.exists()

    def test_a_prepare_that_outlives_its_timeout_is_deleted_when_it_finishes(
        self, temp_home, installer, monkeypatch
    ):
        release = threading.Event()
        real_prepare = installer.prepare_market_install

        def _slow(*a, **k):
            release.wait(5)
            return real_prepare(*a, **k)

        monkeypatch.setattr(installer, "prepare_market_install", _slow)
        monkeypatch.setattr(capability_bound, "CAPABILITY_INSTALL_TIMEOUT", 0.05)
        bounded = BoundedCapabilityManager(_Edition(_app_plan()))

        async def go():
            res = await bounded.market_apply(_APP_REF, _APP_DIGEST)
            release.set()
            for _ in range(200):
                await asyncio.sleep(0.01)
                if installer.last is not None and not installer.last.staging_dir.exists():
                    break
            return res

        res = asyncio.run(go())
        assert res.ok is False and "in time" in res.message
        assert installer.last is not None and not installer.last.staging_dir.exists()

    def test_the_startup_sweep_removes_a_stale_install_dir(self, temp_home, installer):
        root = marketplace_install.market_staging_root()
        root.mkdir(parents=True, exist_ok=True)
        stale = Path(tempfile.mkdtemp(prefix="install-", dir=root))
        fresh = Path(tempfile.mkdtemp(prefix="install-", dir=root))
        other = root / "keep-me"
        other.mkdir()
        old = time.time() - capability_bound.MARKET_APP_PENDING_TTL - 60
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        assert capability_bound.sweep_stale_market_staging() == 1
        assert not stale.exists() and fresh.exists() and other.exists()


class TestNonAppPlansAreUnchanged:
    def test_a_skill_plan_still_goes_to_the_edition(self, temp_home, installer):
        edition = _Edition(_skill_plan())
        res = asyncio.run(BoundedCapabilityManager(edition).market_apply(_SKILL_REF, _SKILL_DIGEST))
        assert res.ok is True and not isinstance(res, MarketAppPending)
        assert ("apply", _SKILL_REF, _SKILL_DIGEST) in edition.calls
        assert installer.prepared == []


class TestThePreviewFlag:
    """``dashboard.feature_previews.marketplace_apps``: OFF (the default) is today's behaviour.

    Off, an app listing is not a plan the dialog can show (as before this route
    existed), ``market_apply`` hands an app plan to the edition's package path
    without touching the app installer, and ``/market/confirm`` installs nothing.
    """

    @pytest.fixture()
    def apps_off(self, monkeypatch):
        monkeypatch.setattr(capability_bound, "market_apps_enabled", lambda: False)

    def test_the_preview_reads_off_by_default_and_on_when_stored(self, temp_home, monkeypatch):
        monkeypatch.setattr(capability_bound, "market_apps_enabled", _REAL_MARKET_APPS_ENABLED)
        assert capability_bound.market_apps_enabled() is False
        cfg = Path(os.environ["KIROCREW_HOME"]) / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps({"dashboard": {"feature_previews": {"marketplace_apps": True}}}),
            encoding="utf-8",
        )
        assert capability_bound.market_apps_enabled() is True

    def test_the_preview_is_a_known_dashboard_feature_preview(self):
        from kiro_crew.config.sections import FEATURE_PREVIEW_FLAGS

        assert capability_bound.MARKET_APPS_PREVIEW == "marketplace_apps"
        assert "marketplace_apps" in FEATURE_PREVIEW_FLAGS

    def test_off_an_app_plan_is_not_showable(self, apps_off):
        assert market.plan_is_showable(_app_plan()) is False
        assert market.plan_is_showable(_skill_plan()) is True

    def test_off_the_plan_route_refuses_an_app_listing_as_before(
        self, temp_home, installer, monkeypatch, apps_off
    ):
        status, body = _post(
            _app(BoundedCapabilityManager(_Edition(_app_plan())), monkeypatch),
            "/api/capability/market/plan",
            {"ref": _APP_REF},
        )
        assert status == 502, body
        assert body["code"] == "market_plan_malformed"

    def test_off_apply_never_reaches_the_app_installer(self, temp_home, installer, apps_off):
        edition = _Edition(_app_plan())
        res = asyncio.run(BoundedCapabilityManager(edition).market_apply(_APP_REF, _APP_DIGEST))
        assert not isinstance(res, MarketAppPending)
        assert installer.prepared == [] and installer.completed == []
        assert ("apply", _APP_REF, _APP_DIGEST) in edition.calls

    def test_off_a_skill_plan_is_unchanged(self, temp_home, installer, apps_off):
        edition = _Edition(_skill_plan())
        res = asyncio.run(BoundedCapabilityManager(edition).market_apply(_SKILL_REF, _SKILL_DIGEST))
        assert res.ok is True and ("apply", _SKILL_REF, _SKILL_DIGEST) in edition.calls

    def test_turned_off_before_the_yes_installs_nothing(self, temp_home, installer, monkeypatch):
        manager = BoundedCapabilityManager(_Edition(_app_plan()))
        pending = asyncio.run(manager.market_apply(_APP_REF, _APP_DIGEST))
        assert isinstance(pending, MarketAppPending)
        monkeypatch.setattr(capability_bound, "market_apps_enabled", lambda: False)
        res = asyncio.run(
            manager.market_app_complete(_APP_REF, _APP_DIGEST, pending.token, confirmed=True)
        )
        assert res.ok is False and installer.completed == []
        assert installer.last is not None and installer.last.discarded is True


# ---- the routes -------------------------------------------------------------------------


class _FakeState:
    def __init__(self) -> None:
        self.refreshed: List[str] = []

    def push_refresh(self, what: str) -> None:
        self.refreshed.append(what)


def _app(manager: Any, monkeypatch) -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        request["user"] = "local-app"
        request["app"] = ""
        return await handler(request)

    class _Ctx:
        capability_manager = manager

    monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agent_config_routes.register(app)
    return app


def _post(app: web.Application, path: str, body: Dict[str, Any]) -> tuple[int, Any]:
    async def go():
        async with TestClient(TestServer(app)) as client:
            first = await client.post(path, json=body)
            return first.status, await first.json()

    return asyncio.run(go())


def _two_posts(app: web.Application, first: tuple, second_body) -> tuple:
    async def go():
        async with TestClient(TestServer(app)) as client:
            r1 = await client.post(first[0], json=first[1])
            b1 = await r1.json()
            r2 = await client.post("/api/capability/market/confirm", json=second_body(b1))
            return (r1.status, b1), (r2.status, await r2.json())

    return asyncio.run(go())


class TestRoutes:
    def test_an_app_plan_is_showable(self, temp_home, installer, monkeypatch):
        status, body = _post(
            _app(BoundedCapabilityManager(_Edition(_app_plan())), monkeypatch),
            "/api/capability/market/plan",
            {"ref": _APP_REF},
        )
        assert status == 200, body
        assert body["items"][0]["kind"] == "app"

    def test_apply_asks_for_the_yes_and_confirm_installs_registering_nothing(
        self, temp_home, installer, no_registration, monkeypatch
    ):
        app = _app(BoundedCapabilityManager(_Edition(_app_plan())), monkeypatch)
        (s1, b1), (s2, b2) = _two_posts(
            app,
            ("/api/capability/market/apply", {"ref": _APP_REF, "plan_digest": _APP_DIGEST}),
            lambda b: {
                "ref": _APP_REF,
                "plan_digest": _APP_DIGEST,
                "token": b.get("token", ""),
                "confirmed": True,
            },
        )
        assert s1 == 202, b1
        assert b1["needsConfirm"] is True
        assert b1["confirm"]["inFolder"] == _IN_FOLDER
        assert installer.completed and installer.completed[0][1] is True
        assert (s2, b2["ok"]) == (200, True), b2
        assert app["state"].refreshed == [], "an app install refreshes no agent spec"

    def test_an_install_that_fails_after_the_yes_is_not_a_pre_install_refusal(
        self, temp_home, installer, monkeypatch
    ):
        # install_app writes installed.json before the app secret, so a failure
        # past the yes may leave the app installed: never answer market_refused.
        def _fails(prepared, *, confirmed):
            installer.completed.append((prepared, confirmed))
            prepared.discard()
            return SimpleNamespace(ok=False, name="", error="secret write failed", error_code="x")

        monkeypatch.setattr(installer, "complete_market_install", _fails)
        app = _app(BoundedCapabilityManager(_Edition(_app_plan())), monkeypatch)
        _, (s2, b2) = _two_posts(
            app,
            ("/api/capability/market/apply", {"ref": _APP_REF, "plan_digest": _APP_DIGEST}),
            lambda b: {
                "ref": _APP_REF,
                "plan_digest": _APP_DIGEST,
                "token": b.get("token", ""),
                "confirmed": True,
            },
        )
        assert s2 == 500, b2
        assert b2["code"] == "market_install_failed"

    def test_confirm_without_an_explicit_true_is_refused(self, temp_home, installer, monkeypatch):
        app = _app(BoundedCapabilityManager(_Edition(_app_plan())), monkeypatch)
        _, (s2, b2) = _two_posts(
            app,
            ("/api/capability/market/apply", {"ref": _APP_REF, "plan_digest": _APP_DIGEST}),
            lambda b: {
                "ref": _APP_REF,
                "plan_digest": _APP_DIGEST,
                "token": b["token"],
                "confirmed": "yes",
            },
        )
        assert s2 == 400, b2
        assert installer.completed == []
