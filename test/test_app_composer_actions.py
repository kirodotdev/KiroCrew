"""``contributes.composerActions``: manifest, grant, resolver, frame and bootstrap.

The point runs on the shared app frame (``apps/app_frame.py``), whose per-entry
token binding is pinned by ``test_app_frame_points.py``. These pin what the
composer action may declare, that its grant is the button (``<id>:insert``), that
its resolver answers only for an enabled app's approved action (so a revoked one
stops its frame at the next request), and that its frame script hands the app's
module nothing from the composer and runs one invocation at a time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import app_frame as af
from kiro_crew.apps import composer_action_frame as caf
from kiro_crew.apps.composer_action_grants import (
    COMPOSER_ACTION_FAMILY,
    action_grant,
    composer_action_grant_entries,
)
from kiro_crew.apps.manifest_contributes import Contributes
from kiro_crew.apps.point_grants import grant_source, point_grant_families

APP = "snippets"
POINT = caf.COMPOSER_ACTION_POINT


def _action(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "insert", "title": "Insert snippet", "entry": "dist/a.mjs"}
    base.update(over)
    return base


# ── manifest ──


def test_a_well_formed_action_validates_and_serializes() -> None:
    c = Contributes.from_dict({"composerActions": [_action(icon="Zap")]})
    assert c.validate() == []
    assert c.to_dict()["composerActions"] == [
        {
            "id": "insert",
            "title": "Insert snippet",
            "icon": "Zap",
            "entry": "dist/a.mjs",
        }
    ]


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"id": "Bad Id"}, "storage-safe slug"),
        ({"id": ""}, "missing id"),
        ({"title": ""}, "missing title"),
        ({"title": "x" * 41}, "title exceeds 40"),
        ({"entry": ""}, "missing entry"),
        # No per-action text cap: the host caps every offer at 32,000.
        ({"maxChars": 500}, "maxChars is not a known field"),
        ({"id": 5}, "id is not a known field"),
        ({"icon": "https://x.example/i.svg"}, "icon must be a glyph name"),
        ({"popover": {"width": 100}}, "popover.width must be between"),
        ({"popover": {"depth": 3}}, "popover is not a known field"),
        ({"sendsDraft": True}, "sendsDraft is not a known field"),
        ({"entry": "./" * 500000 + "dist/a.mjs"}, "entry exceeds 512 characters"),
        ({"title": "x" * 513}, "title exceeds 512 characters"),
    ],
)
def test_a_malformed_action_is_refused_by_name(over: dict[str, Any], needle: str) -> None:
    c = Contributes.from_dict({"composerActions": [_action(**over)]})
    errors = c.validate()
    assert any(needle in e for e in errors), errors


def test_the_list_shape_and_cap_are_enforced() -> None:
    assert any(
        "must be an array" in e for e in Contributes.from_dict({"composerActions": {}}).validate()
    )
    assert any(
        "must be an object" in e
        for e in Contributes.from_dict({"composerActions": ["x"]}).validate()
    )
    three = [_action(id=f"a{i}") for i in range(3)]
    assert any(
        "exceeds the limit of 2" in e
        for e in Contributes.from_dict({"composerActions": three}).validate()
    )
    dup = [_action(), _action(title="Other")]
    assert any(
        "duplicate id" in e for e in Contributes.from_dict({"composerActions": dup}).validate()
    )


def test_the_entry_is_contained_and_signed(tmp_path) -> None:
    from kiro_crew.apps.manifest import AppManifest

    raw: dict[str, Any] = {
        "name": APP,
        "version": "1.0.0",
        "displayName": "Snippets",
        "description": "Inserts snippets.",
        "contributes": {"composerActions": [_action(entry="../../escape.mjs")]},
    }
    m = AppManifest.from_dict(raw)
    assert any("composerActions entry" in e for e in m.validate(tmp_path))
    raw["contributes"]["composerActions"] = [_action()]
    signed = AppManifest.from_dict(raw).signing_payload()
    raw["contributes"]["composerActions"] = [_action(entry="dist/evil.mjs")]
    # Repointing the entry changes what the signature covers.
    assert AppManifest.from_dict(raw).signing_payload() != signed
    assert b"composerActions" in signed


# ── grant ──


def test_the_grant_is_the_button_not_its_label() -> None:
    a = composer_action_grant_entries({"composerActions": [_action()]})
    b = composer_action_grant_entries({"composerActions": [_action(title="Renamed", icon="x")]})
    c = composer_action_grant_entries({"composerActions": [_action(id="other")]})
    assert a == b == {COMPOSER_ACTION_FAMILY: [action_grant("insert")]}
    assert c[COMPOSER_ACTION_FAMILY] != a[COMPOSER_ACTION_FAMILY]
    assert action_grant("insert") == "insert:insert"


def test_the_family_is_staged_like_every_point() -> None:
    from kiro_crew.apps.manager_records import STAGED_GRANT_FAMILIES

    assert COMPOSER_ACTION_FAMILY in point_grant_families()
    assert COMPOSER_ACTION_FAMILY in STAGED_GRANT_FAMILIES
    src = grant_source({"contributes": {"composerActions": [_action()]}})
    assert src is not None and src[COMPOSER_ACTION_FAMILY] == ["insert:insert"]


def test_a_new_action_on_update_is_held_back() -> None:
    from kiro_crew.apps.manager_records import (
        _approved_grants_after_manifest_change,
        _held_back_entries,
    )

    old = grant_source({"contributes": {"composerActions": [_action()]}})
    new = grant_source({"contributes": {"composerActions": [_action(), _action(id="two")]}})
    approved = _approved_grants_after_manifest_change(
        existing_approved=None, old_permissions=old, new_permissions=new
    )
    assert approved[COMPOSER_ACTION_FAMILY] == ["insert:insert"]
    assert _held_back_entries(approved, new)[COMPOSER_ACTION_FAMILY] == ["two:insert"]


# ── resolver ──


def _manifest(actions: list[dict[str, Any]], name: str = APP) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        contributes=Contributes.from_dict({"composerActions": actions}),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={},
    )


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An installed app whose enabled flag, manifest and approved ids a test sets."""
    state: dict[str, Any] = {
        "enabled": True,
        "manifest": _manifest([_action(), _action(id="other", entry="dist/b.mjs")]),
        "approved": {"insert", "other"},
    }
    monkeypatch.setattr(caf, "is_app_enabled", lambda name: name == APP and state["enabled"])
    monkeypatch.setattr(caf, "get_app_manifest", lambda name: state["manifest"])
    monkeypatch.setattr(
        caf,
        "approved_composer_actions",
        lambda name, m: [
            a.to_dict() for a in m.contributes.composerActions if a.id in state["approved"]
        ],
    )
    return state


def test_the_resolver_serves_an_enabled_apps_approved_action(installed) -> None:
    assert caf.resolve_composer_action(APP, "insert") == ("dist/a.mjs", False, APP)
    assert caf.resolve_composer_action(APP, "other") == ("dist/b.mjs", False, APP)


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"approved": {"other"}},
        {"manifest": _manifest([_action()], name="someone-else")},
        {"manifest": _manifest([_action(entry="../x.mjs")])},
        {"manifest": _manifest([])},
    ],
)
def test_the_resolver_refuses_anything_else(installed, change: dict[str, Any]) -> None:
    installed.update(change)
    assert caf.resolve_composer_action(APP, "insert") is None
    assert caf.resolve_composer_action(APP, "missing") is None


def test_the_point_is_registered_on_the_shared_frame() -> None:
    assert POINT == "composerActions" and POINT in af.registered_frame_points()
    assert "composer.invoke" in caf.COMPOSER_ACTION_SCRIPT


# ── routes, through the shared frame ──


@asynccontextmanager
async def _client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[TestClient, list[str]]]:
    calls: list[str] = []

    class _Registry:
        async def dispatch(self, request: web.Request) -> web.Response:
            calls.append(request.match_info["path"])
            return web.json_response({"ok": True})

    monkeypatch.setattr("kiro_crew.apps.hooks_integration.get_route_registry", _Registry)
    monkeypatch.setattr("kiro_crew.dashboard.server._embed_parent_port", lambda r, a: None)

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path.startswith("/api/"):
            request["is_dashboard_user"] = True
        return await handler(request)

    app = web.Application(middlewares=[auth])
    af.register_app_frame_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_frame_carries_the_point_script_and_no_composer_data(
    monkeypatch: pytest.MonkeyPatch, installed
) -> None:
    async with _client(monkeypatch) as (client, _):
        resp = await client.post(f"/api/apps/{APP}/frames/{POINT}/insert")
        assert resp.status == 200, await resp.text()
        minted = await resp.json()
        doc = await client.get(minted["frame"])
        assert doc.status == 200
        html = await doc.text()
        assert f'"entry": "/apps/{APP}/ui/dist/a.mjs"' in html
        assert "accepts: (h) => !!h && typeof h.invoke === 'function'" in html
        island = html.split('id="frame-config">', 1)[1].split("</script>", 1)[0]
        for word in ("draft", "session", "selection"):
            assert word not in island


@pytest.mark.asyncio
async def test_a_revoked_action_stops_its_frame_and_fetch(
    monkeypatch: pytest.MonkeyPatch, installed
) -> None:
    async with _client(monkeypatch) as (client, calls):
        minted = await (await client.post(f"/api/apps/{APP}/frames/{POINT}/insert")).json()
        headers = {af.CAPABILITY_HEADER: minted["capability"]}
        assert (await client.get(f"/app-frame-fetch/{APP}/notes", headers=headers)).status == 200
        installed["approved"] = {"other"}
        assert (await client.get(minted["frame"])).status == 404
        assert (await client.get(f"/app-frame-fetch/{APP}/notes", headers=headers)).status == 401
        assert (await client.post(f"/api/apps/{APP}/frames/{POINT}/insert")).status == 404
        assert calls == ["notes"]


# ── frame script, run under node with the shared runtime ──

_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/composerActions/insert/f/t', href: 'http://gw/app-frame/a/composerActions/insert/f/t' };
globalThis.calls = [];
const mod = `export function activate(ctx) { globalThis.ctxKeys = Object.keys(ctx); return { async invoke(call) {
  globalThis.calls.push({ id: call.invocationId, keys: Object.keys(call), frozen: Object.isFrozen(call) });
  if (globalThis.calls.length === 1) return 'hello';
  throw new Error('boom');
} }; }`;
globalThis.document = {
  activeElement: null, body: {},
  getElementById: () => ({ textContent: JSON.stringify({
    app: 'a', entry: 'data:text/javascript,' + encodeURIComponent(mod),
    fetchBase: '/app-frame-fetch/a/', theme: 'light',
  }) }),
};
"""
_DRIVER = r"""
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { 'kirocrew-app-frame': 1, ...data } }));
const tick = () => new Promise((r) => setTimeout(r, 10));
deliver({ type: 'init', capability: 'c' });
for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await tick();
deliver({ type: 'composer.invoke', invocationId: 'inv-0001', deadlineMs: 1000 });
// A second invoke while the first is open is not delivered.
deliver({ type: 'composer.invoke', invocationId: 'inv-0002', deadlineMs: 1000 });
await tick();
const offer = posted.find((m) => m.type === 'result.offer');
deliver({ type: 'result.ack', invocationId: 'inv-0001', seq: offer.seq });
await tick();
deliver({ type: 'composer.invoke', invocationId: 'inv-0003', deadlineMs: 1000 });
await tick();
console.log(JSON.stringify({ posted, calls, ctxKeys }));
"""


def test_the_script_runs_one_invocation_at_a_time_and_hands_over_nothing() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    boot = af._RUNTIME + af._INVOCATION + caf.COMPOSER_ACTION_SCRIPT
    out = subprocess.run(
        [node, "--input-type=module", "-e", _HARNESS + boot + _DRIVER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert sorted(result["ctxKeys"]) == ["appName", "fetch", "point", "theme"]
    assert [c["id"] for c in result["calls"]] == ["inv-0001", "inv-0003"]
    assert all(c["frozen"] for c in result["calls"])
    # The call names its own controls and no composer state.
    assert sorted(result["calls"][0]["keys"]) == [
        "close",
        "deadlineMs",
        "error",
        "insert",
        "invocationId",
        "resize",
        "signal",
    ]
    offers = [m for m in result["posted"] if m["type"] == "result.offer"]
    assert [(o["invocationId"], o["kind"], o["value"]) for o in offers] == [
        ("inv-0001", "insert", "hello")
    ]
    err = next(m for m in result["posted"] if m["type"] == "invocation.error")
    assert err["invocationId"] == "inv-0003" and "boom" in err["message"]


def test_the_grant_row_lists_approved_actions_and_holds_new_ones_back(monkeypatch):
    """The row renders only approved actions; a declared id still waiting is staged."""
    from types import SimpleNamespace

    from kiro_crew.apps import composer_action_grants as cag

    granted = {"one:insert"}
    monkeypatch.setattr(
        "kiro_crew.apps.manager.staged_app_grants",
        lambda name, family, declared: [e for e in declared() if e in granted],
    )

    class _Entry:
        def __init__(self, **kw):
            self._kw = kw

        def to_dict(self):
            return dict(self._kw)

    contributes = SimpleNamespace(
        composerActions=[
            _Entry(id="one", title="One", icon="Zap", entry="a.mjs"),
            {"id": "two", "title": "Two"},
            {"id": "one", "title": "Duplicate"},
            "not an entry",
            {"title": "no id"},
        ]
    )
    manifest = SimpleNamespace(contributes=contributes)

    assert cag.composer_action_grant_entries(contributes) == {
        "composerActions": ["one:insert", "two:insert"]
    }
    row = cag.composer_action_grant_row("snip", manifest)["composerActionGrant"]
    # Only the fields the dashboard renders, and never `entry`.
    assert row["actions"][0] == {"id": "one", "title": "One", "icon": "Zap"}
    assert [a["id"] for a in row["actions"]] == ["one", "one"]
    assert row["staged"] == ["two"]
    # The raw-dict manifest shape reads the same.
    raw = {"contributes": {"composerActions": [{"id": "two", "title": "Two"}]}}
    assert cag.approved_composer_actions("snip", raw) == []


def test_an_app_without_composer_actions_has_no_grant_row():
    from types import SimpleNamespace

    from kiro_crew.apps import composer_action_grants as cag

    assert cag.composer_action_grant_row("snip", {"contributes": {}}) == {}
    assert cag.composer_action_grant_row("snip", SimpleNamespace(contributes=None)) == {}
    assert cag.approved_composer_actions("snip", {"contributes": None}) == []
    assert cag.composer_action_grant_entries(None) == {"composerActions": []}
    assert cag.composer_action_grant_entries({"composerActions": "nope"}) == {"composerActions": []}


def test_an_oversized_entry_is_never_kept() -> None:
    """Padding normalizes away, so the bound is checked before the value is kept."""
    c = Contributes.from_dict({"composerActions": [_action(entry="./" * 500000 + "dist/a.mjs")]})
    assert c.composerActions[0].entry == ""
    assert len(str(c.to_dict())) < 1000
