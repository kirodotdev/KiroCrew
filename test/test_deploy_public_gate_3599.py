"""The public-web deploy destination is closable by the operator (issue #3599).

Before this, ``deploy-web-aws`` was the one publish destination exempt from the
``capabilities.publish`` chokepoint: the provider row was appended to
``/api/publish-providers`` unconditionally and ``POST /api/deploy/deploy``
consulted no ceiling at all. These tests pin the three properties that make the
destination genuinely closable rather than merely hidden:

1. the provider row disappears from the registry when the destination is denied;
2. ``/api/deploy/deploy`` answers 403 on its own, so a caller that never reads the
   registry (a direct POST, or the internal-secret MCP preview) is refused too;
3. ``/api/deploy/pending/{id}/confirm`` answers 403 **before** claiming the entry,
   so an entry created while the destination was open is neither deployable nor
   silently consumed after the operator closes it.

Plus one wiring test that goes through the REAL decision (no stubbed gate) via the
operator's ``publish.allowed_destinations`` config, because every mock-level test
above would still pass if the handler called a function that always permitted.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.deploy import handlers


def _run(coro):
    """Drive one handler coroutine to completion.

    ``asyncio.run`` and not a hand-rolled ``new_event_loop()``: the handlers under
    test call ``asyncio.to_thread``, which binds a default ThreadPoolExecutor to
    the RUNNING loop. A loop that is never closed leaves that executor attached to
    a dead loop, and the threads surface later as "Event loop is closed" — which
    is what turned Backend Tests shard 3 red on 5ff7c2fe7 while every assertion
    passed. ``asyncio.run`` closes the loop and awaits
    ``shutdown_default_executor()`` on the way out. This mirrors
    ``test_deploy_web_handlers._run``.
    """
    return asyncio.run(coro)


class _NonRestrictedState:
    _restricted_keys: set = set()
    _slots: dict = {}


class _Req:
    """Non-restricted dashboard request."""

    def __init__(self, body=None, *, match_info=None, headers=None):
        self._body = body or {}
        self.headers = headers if headers is not None else {"X-Session-Key": "dashboard:ui"}
        self.app = {"state": _NonRestrictedState()}
        self.match_info = match_info or {}
        self.method = "POST"

    async def json(self):
        return self._body

    async def read(self):
        return json.dumps(self._body).encode()


# ── /api/deploy/deploy ──────────────────────────────────────────────────────

def test_deploy_denied_when_destination_closed(monkeypatch):
    """A denied destination returns 403 and never reaches the deploy engine."""
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )

    async def _must_not_run(_params):  # pragma: no cover - reached only on regression
        raise AssertionError("_do_deploy ran despite a denied destination")

    monkeypatch.setattr(handlers, "_do_deploy", _must_not_run)
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x", "artifact_slug": "a"})))
    assert resp.status == 403
    assert "closed by policy" in resp.text


def test_deploy_asks_about_the_deploy_web_destination(monkeypatch):
    """The gate is consulted for ``deploy-web-aws``, not some other provider id."""
    seen: list[str] = []

    def _record(_req, provider_id):
        seen.append(provider_id)
        return None

    monkeypatch.setattr(handlers, "publish_denied_reason", _record)
    monkeypatch.setattr(handlers, "_do_deploy", lambda _p: _permitted())
    _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert seen == ["deploy-web-aws"]


async def _permitted():
    return 200, {"requires_confirm": True}


def test_deploy_proceeds_when_permitted(monkeypatch):
    monkeypatch.setattr(handlers, "publish_denied_reason", lambda _req, _pid: None)
    monkeypatch.setattr(handlers, "_do_deploy", lambda _p: _permitted())
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert resp.status == 200


def test_denials_carry_a_machine_readable_code(monkeypatch):
    """Both 403s carry `code`, not prose alone.

    The dashboard renders ``error`` verbatim into a localized UI, so a coded
    denial is the only translatable one — and this denial is the message a whole
    team sees after an operator closes the destination.
    """
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    deploy = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    pending = _run(handlers._handle_pending_confirm(_Req(match_info={"id": "p1"})))
    for resp in (deploy, pending):
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "publish_destination_disabled"


def test_the_gate_never_runs_on_the_event_loop():
    """Both deploy call sites offload the decision to a thread.

    ``publish_denied_reason`` reads the trust-root policy, every governance
    profile and ``config.json`` from disk. Run inline it stalls the whole gateway
    (and its heartbeat) on a slow or contended data home — for every caller, not
    just the one publishing. The provider-registry call site is offloaded for the
    same reason, so this pins all three rather than leaving one shape behind.
    """
    import inspect

    for fn in (handlers._handle_deploy, handlers._handle_pending_confirm):
        src = inspect.getsource(fn)
        assert "publish_denied_reason" in src, f"{fn.__name__} lost the gate entirely"
        assert "asyncio.to_thread(" in src, (
            f"{fn.__name__} calls publish_denied_reason on the event loop"
        )

    from kiro_crew.apps import routes

    registry_src = inspect.getsource(routes.handle_publish_providers)
    assert "asyncio.to_thread(" in registry_src


def test_internal_secret_preview_is_denied_too(monkeypatch):
    """The MCP ``deploy_artifact`` preview rides this endpoint, so it is gated too.

    The preview is the surface that tells an agent the destination exists; a closed
    destination must not advertise one.
    """
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    req = _Req({"site_id": "x"}, headers={"X-Internal-Secret": "s"})
    resp = _run(handlers._handle_deploy(req))
    assert resp.status == 403


# ── /api/deploy/pending/{id}/confirm ────────────────────────────────────────

def test_pending_confirm_denied_without_consuming_the_entry(monkeypatch):
    """403 lands BEFORE claim_pending, so the entry survives the refusal.

    Order matters: claiming is destructive (the entry is removed so concurrent
    confirms cannot double-deploy), so gating after the claim would turn every
    denied confirm into a silently discarded pending deploy.
    """
    monkeypatch.setattr(
        handlers, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    from kiro_crew.deploy import pending as pending_mod

    def _must_not_claim(_entry_id):  # pragma: no cover - reached only on regression
        raise AssertionError("claim_pending ran despite a denied destination")

    monkeypatch.setattr(pending_mod, "claim_pending", _must_not_claim)
    resp = _run(handlers._handle_pending_confirm(_Req(match_info={"id": "p1"})))
    assert resp.status == 403
    assert "closed by policy" in resp.text


# ── GET /api/publish-providers ──────────────────────────────────────────────

def test_provider_registry_omits_closed_destination(monkeypatch):
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", lambda: [])
    monkeypatch.setattr(
        routes, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    resp = _run(routes.handle_publish_providers(_Req()))
    ids = [p["id"] for p in json.loads(resp.text)["providers"]]
    assert "deploy-web-aws" not in ids


def test_provider_registry_lists_open_destination(monkeypatch):
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", lambda: [])
    monkeypatch.setattr(routes, "publish_denied_reason", lambda _req, _pid: None)
    resp = _run(routes.handle_publish_providers(_Req()))
    rows = json.loads(resp.text)["providers"]
    assert [p["id"] for p in rows] == ["deploy-web-aws"]
    # The row still carries the fields the Publish panel renders from.
    assert rows[0]["endpoint"] == "/api/deploy/deploy"
    assert rows[0]["origin"] == "core"


# ── the destination id is reserved ──────────────────────────────────────────

def _squatter_app():
    """An enabled app whose manifest claims the CORE deploy destination id.

    ``collect_publish_providers`` validates the declared *endpoint* (it must sit
    under the app's own namespace) but never the declared *id*, so this manifest
    is accepted today.
    """
    return [{
        "name": "squatter",
        "enabled": True,
        "manifest": {"publishProvider": {
            "id": "deploy-web-aws",
            "label": "Publish (totally the real one)",
            "endpoint": "/api/apps/squatter/publish",
            "setupRoute": "/apps/squatter",
        }},
    }]


def test_an_app_cannot_squat_the_core_destination_id_when_denied(monkeypatch):
    """A denied destination leaves NO row carrying its id — app-declared included.

    Otherwise the operator's shutdown is undone by an install: the row survives,
    and the Publish panel posts to the app's own endpoint, which this chokepoint
    does not cover.
    """
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", _squatter_app)
    monkeypatch.setattr(routes, "_provider_is_configured", lambda _n, _pp: True)
    monkeypatch.setattr(
        routes, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    resp = _run(routes.handle_publish_providers(_Req()))
    rows = json.loads(resp.text)["providers"]
    assert [p["id"] for p in rows] == []


def test_an_app_cannot_squat_the_core_destination_id_when_cloud_is_withheld(monkeypatch):
    """The PLATFORM withhold closes the path as completely as the governance deny.

    These are two independent controls on the same destination, so an early return
    for one of them is how a closed destination stays reachable: the core row goes
    away but the app-declared row carrying the same id survives and publishes at
    its OWN endpoint, which this chokepoint does not cover. Both closures share one
    closed path, and this pins that they do.
    """
    from kiro_crew.apps import routes
    from kiro_crew.dashboard.handlers import _shared

    monkeypatch.setattr(routes, "list_apps", _squatter_app)
    monkeypatch.setattr(routes, "_provider_is_configured", lambda _n, _pp: True)
    monkeypatch.setattr(_shared, "admits_cloud_deployment", lambda _t: False)

    def _must_not_run(_req, _pid):  # pragma: no cover - reached only on regression
        raise AssertionError("gate 2 consulted although the platform already closed it")

    monkeypatch.setattr(routes, "publish_denied_reason", _must_not_run)

    resp = _run(routes.handle_publish_providers(_Req()))
    assert [p["id"] for p in json.loads(resp.text)["providers"]] == []


def test_the_permitted_path_leaves_a_shadowing_app_row_alone(monkeypatch):
    """Permitted, an app row sharing the id is NOT dropped — deliberately.

    `test_publish_providers` documents this as existing behaviour: its fixture app
    declares this very id and the test asserts the APP's endpoint is the one
    resolved for it. Removing the shadow is a behaviour change on its own merits,
    not something to fold into closing the denial hole, so this pins the scope of
    the fix rather than the shape someone might assume it has.
    """
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", _squatter_app)
    monkeypatch.setattr(routes, "_provider_is_configured", lambda _n, _pp: True)
    monkeypatch.setattr(routes, "publish_denied_reason", lambda _req, _pid: None)
    resp = _run(routes.handle_publish_providers(_Req()))
    rows = [p for p in json.loads(resp.text)["providers"] if p["id"] == "deploy-web-aws"]
    assert [r["endpoint"] for r in rows] == [
        "/api/apps/squatter/publish",
        "/api/deploy/deploy",
    ]


def test_an_app_declaring_a_different_id_is_untouched(monkeypatch):
    """The filter is scoped to the reserved id, not to app providers generally."""
    from kiro_crew.apps import routes

    monkeypatch.setattr(routes, "list_apps", lambda: [{
        "name": "notes",
        "enabled": True,
        "manifest": {"publishProvider": {
            "id": "notes-publish",
            "endpoint": "/api/apps/notes/publish",
        }},
    }])
    monkeypatch.setattr(routes, "_provider_is_configured", lambda _n, _pp: True)
    monkeypatch.setattr(
        routes, "publish_denied_reason", lambda _req, _pid: "closed by policy"
    )
    resp = _run(routes.handle_publish_providers(_Req()))
    assert [p["id"] for p in json.loads(resp.text)["providers"]] == ["notes-publish"]


# ── the real decision, not a stub ───────────────────────────────────────────

@pytest.fixture()
def narrowed_allowlist(monkeypatch):
    """Operator config permits only the internal registry — deploy is excluded."""
    from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

    cfg = KiroCrewConfig.load()
    cfg.publish = PublishConfig(allowed_destinations=["internal-registry"])
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    return cfg


def test_config_allowlist_reaches_the_deploy_endpoint(narrowed_allowlist, monkeypatch):
    """End-to-end through the REAL gate: config narrowing closes /api/deploy/deploy.

    Every test above stubs ``publish_denied_reason``, so all of them would still
    pass if the handler consulted a gate that never denied. This one exercises the
    genuine decision path (governance ceiling ungoverned by default → config
    allowlist) and is what actually proves the operator's knob is wired through.
    """

    async def _must_not_run(_params):  # pragma: no cover - reached only on regression
        raise AssertionError("_do_deploy ran despite a narrowed allowlist")

    monkeypatch.setattr(handlers, "_do_deploy", _must_not_run)
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert resp.status == 403
    assert "deploy-web-aws" in resp.text


def test_deploy_permitted_when_allowlist_names_it(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

    cfg = KiroCrewConfig.load()
    cfg.publish = PublishConfig(allowed_destinations=["deploy-web-aws"])
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(handlers, "_do_deploy", lambda _p: _permitted())
    resp = _run(handlers._handle_deploy(_Req({"site_id": "x"})))
    assert resp.status == 200


# ── one decision, not two ───────────────────────────────────────────────────

def test_artifact_publish_and_deploy_share_one_decision():
    """The artifact-publish alias must stay the shared helper, never a fork.

    Two copies of an authorization decision drift; the point of moving it into
    ``publish_governance`` was that a policy change lands on every publish surface
    at once.
    """
    from kiro_crew.dashboard.handlers import artifacts as art
    from kiro_crew.publish_governance import publish_denied_reason

    assert art.publish_denied_reason is publish_denied_reason
    # The alias forwards rather than reimplementing: its body is a single call.
    import inspect

    src = inspect.getsource(art._publish_governance_denied)
    assert "publish_denied_reason(request, provider_name)" in src
    assert "governance_permits" not in src


# ── both denial layers are audited ──────────────────────────────────────────

def test_the_config_allowlist_denial_is_audited(narrowed_allowlist, monkeypatch):
    """A config-allowlist refusal reaches SEL, exactly like a ceiling refusal.

    There are TWO denial layers and they produce the same user-visible 403, so an
    operator reconstructing "why was this publish refused" must find either one on
    the record. The ceiling deny was always audited; this pins the config deny,
    which returned its reason silently and left the trail half-written.
    """
    import kiro_crew.publish_governance as pg

    events: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            events.append(kw)

    monkeypatch.setattr(pg._sel_mod, "sel", lambda: _Sel())

    reason = pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID)

    assert reason is not None, "narrowed allowlist must deny the deploy destination"
    assert len(events) == 1, f"expected exactly one audit event, got {events}"
    ev = events[0]
    assert ev["outcome"] == "denied"
    assert ev["scope"] == "capabilities.publish"
    assert pg.DEPLOY_WEB_PROVIDER_ID in ev["item"]
    # The layer must distinguish this from the ceiling deny, or the audit trail
    # cannot tell an operator WHICH control refused.
    assert ev["layer"] == "config"


def test_a_permitted_publish_is_not_audited(monkeypatch):
    """The permit path stays silent — deliberately, and this pins it.

    ``GET /api/publish-providers`` evaluates the gate once per candidate row on
    every panel open. Auditing allows would turn an authorization log into a
    page-view log, so the publish itself is audited where the bytes leave instead.
    """
    import kiro_crew.publish_governance as pg
    from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

    cfg = KiroCrewConfig.load()
    cfg.publish = PublishConfig(allowed_destinations=[])
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))

    events: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):  # pragma: no cover - must not run
            events.append(kw)

    monkeypatch.setattr(pg._sel_mod, "sel", lambda: _Sel())

    assert pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID) is None
    assert events == [], f"permit path must not audit, emitted {events}"


def test_a_broken_audit_sink_still_denies(narrowed_allowlist, monkeypatch):
    """The allowlist denial survives an SEL that raises — audit is not the gate."""
    import kiro_crew.publish_governance as pg

    class _Sel:
        def log_governance_decision(self, **kw):
            raise RuntimeError("sel down")

    monkeypatch.setattr(pg._sel_mod, "sel", lambda: _Sel())

    assert pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID) is not None


# ── a config we cannot parse must DENY, not degrade to allow-all ─────────────

@pytest.fixture()
def _quiet_sel(monkeypatch):
    """Swallow SEL writes so these tests assert on the decision, not the log."""
    import kiro_crew.publish_governance as pg

    events: list[dict] = []

    class _Sel:
        def log_governance_decision(self, **kw):
            events.append(kw)

    monkeypatch.setattr(pg._sel_mod, "sel", lambda: _Sel())
    return events


def test_a_malformed_config_denies_instead_of_reopening_the_path(
    tmp_path, monkeypatch, _quiet_sel
):
    """A corrupt ``config.json`` must not present as an empty (allow-all) allowlist.

    ``KiroCrewConfig.load()`` catches ``JSONDecodeError``/``OSError``, warns, and
    returns DEFAULTS — so it never raises, a ``try/except`` around it never fires,
    and an operator who narrowed ``allowed_destinations`` to close the public-web
    path would have that narrowing silently replaced by the empty default the
    moment the file was corrupted. This module documents fail-CLOSED, so it checks
    parseability itself.
    """
    import kiro_crew.publish_governance as pg
    bad = tmp_path / "config.json"
    bad.write_text('{"publish": {"allowed_destinations": ["internal-registry"]')  # truncated
    monkeypatch.setattr(pg, "config_path", lambda: bad)
    monkeypatch.setattr(pg, "config_local_path", lambda: tmp_path / "config.local.json")

    reason = pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID)

    assert reason is not None, "a config that cannot be parsed must DENY"
    assert "config.json" in reason
    assert [e["outcome"] for e in _quiet_sel] == ["denied"]


def test_a_malformed_overlay_denies_too(tmp_path, monkeypatch, _quiet_sel):
    """``config.local.json`` is deep-merged and swallows its own parse errors.

    A corrupt overlay hides an allowlist exactly as effectively as a corrupt base,
    so checking only the base would leave half the hole open.
    """
    import kiro_crew.publish_governance as pg
    good = tmp_path / "config.json"
    good.write_text("{}")
    bad_overlay = tmp_path / "config.local.json"
    bad_overlay.write_text("}not json{")
    monkeypatch.setattr(pg, "config_path", lambda: good)
    monkeypatch.setattr(pg, "config_local_path", lambda: bad_overlay)

    reason = pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID)

    assert reason is not None
    assert "config.local.json" in reason


def test_a_non_object_config_denies(tmp_path, monkeypatch, _quiet_sel):
    """Valid JSON that is not an object also degrades to defaults in the loader."""
    import kiro_crew.publish_governance as pg
    weird = tmp_path / "config.json"
    weird.write_text('["not", "an", "object"]')
    monkeypatch.setattr(pg, "config_path", lambda: weird)
    monkeypatch.setattr(pg, "config_local_path", lambda: tmp_path / "nope.json")

    assert pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID) is not None


def test_a_config_load_failure_is_audited_too(tmp_path, monkeypatch, _quiet_sel):
    """The LAST denial path that returned silently now audits like the rest.

    `_audit_deny` claims every layer that can refuse routes through it, and that
    claim was false for the ``except`` around ``KiroCrewConfig.load()``. A refusal
    an operator cannot find in the audit log is indistinguishable, to them, from
    the publish never having been attempted.

    The parseability guard sits in front of this branch, so the config on disk has
    to be VALID while ``load()`` itself fails — hence a raising ``load``.
    """
    import kiro_crew.publish_governance as pg
    good = tmp_path / "config.json"
    good.write_text("{}")
    monkeypatch.setattr(pg, "config_path", lambda: good)
    monkeypatch.setattr(pg, "config_local_path", lambda: tmp_path / "absent.json")

    class _Raising:
        """Only publish_governance's binding is replaced, so the governance layer
        (which loads config through its own import) still evaluates normally and
        the failure lands on the branch under test."""

        @staticmethod
        def load():
            raise RuntimeError("schema rejected publish.allowed_destinations")

    monkeypatch.setattr(pg, "KiroCrewConfig", _Raising)

    reason = pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID)

    assert reason is not None, "a config that cannot be loaded must DENY"
    assert "could not be loaded" in reason
    assert [e["outcome"] for e in _quiet_sel] == ["denied"], (
        f"the config-load refusal must be audited exactly once, got {_quiet_sel}"
    )


def test_an_absent_config_still_permits(tmp_path, monkeypatch, _quiet_sel):
    """The guard must not over-deny: no config at all is the standalone default.

    This is the other half of fail-closed — a check that denies when the file is
    merely MISSING would break every ordinary install, where an unnamed publish is
    ungoverned and permitted.
    """
    import kiro_crew.publish_governance as pg
    monkeypatch.setattr(pg, "config_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(pg, "config_local_path", lambda: tmp_path / "absent.local.json")

    assert pg.publish_denied_reason(_Req(), pg.DEPLOY_WEB_PROVIDER_ID) is None
    assert _quiet_sel == [], "a permitted publish must not audit"
