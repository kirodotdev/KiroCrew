"""``contributes.widgets``: manifest, resolver, offline frame, script and parser.

The point runs on the shared app frame (``apps/app_frame.py``), whose per-entry
token binding is pinned by ``test_app_frame_points.py``. These pin what a widget may
declare, that its resolver answers only for an enabled app's declared widget, that
its frame is offline (no capability on the mint, no network image in either
policy), that its script hands the app's module the payload and nothing else, and
that a ``use`` widget is parsed but never stored as an artifact.
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
from kiro_crew.apps import widget_frame as wf
from kiro_crew.apps.manifest_contributes import Contributes
from kiro_crew.apps.manifest_widgets import MAX_WIDGETS_PER_APP
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from kiro_crew.widget_parse import parse_widgets

APP = "issue-radar"
POINT = wf.WIDGETS_POINT


def _widget(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "issue-card", "title": "Issue card", "entry": "widgets/card.mjs"}
    base.update(over)
    return base


# ── manifest ──


def test_a_well_formed_widget_validates_and_serializes() -> None:
    c = Contributes.from_dict({"widgets": [_widget(description="One GitHub issue.")]})
    assert c.validate() == []
    assert c.to_dict()["widgets"] == [
        {
            "id": "issue-card",
            "title": "Issue card",
            "entry": "widgets/card.mjs",
            "description": "One GitHub issue.",
        }
    ]


def test_an_app_without_widgets_serializes_as_before() -> None:
    assert "widgets" not in Contributes.from_dict({}).to_dict()
    assert Contributes.from_dict({}).validate() == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"id": "Bad Id"}, "storage-safe slug"),
        ({"id": ""}, "missing id"),
        ({"title": ""}, "missing title"),
        ({"title": "x" * 41}, "title exceeds 40"),
        ({"description": "x" * 201}, "description exceeds 200"),
        ({"entry": ""}, "missing entry"),
        ({"entry": "./" * 500 + "w.mjs"}, "entry exceeds 512"),
        ({"id": 5}, "id is not a known field"),
        ({"fetch": True}, "fetch is not a known field"),
    ],
)
def test_a_malformed_widget_is_refused_by_name(over: dict[str, Any], needle: str) -> None:
    errors = Contributes.from_dict({"widgets": [_widget(**over)]}).validate()
    assert any(needle in e for e in errors), errors


def test_a_title_is_measured_in_utf16_units() -> None:
    # 21 astral characters are 42 UTF-16 units: over the cap the dashboard measures.
    errors = Contributes.from_dict({"widgets": [_widget(title="\U0001f600" * 21)]}).validate()
    assert any("title exceeds 40" in e for e in errors), errors


def test_the_list_shape_and_cap_are_enforced() -> None:
    assert any("must be an array" in e for e in Contributes.from_dict({"widgets": {}}).validate())
    assert any(
        "1 entry must be an object" in e
        for e in Contributes.from_dict({"widgets": [_widget(), 3]}).validate()
    )
    many = [_widget(id=f"w{i}") for i in range(MAX_WIDGETS_PER_APP + 1)]
    assert any(
        "exceeds the limit" in e for e in Contributes.from_dict({"widgets": many}).validate()
    )
    dup = [_widget(), _widget(entry="widgets/other.mjs")]
    assert any("duplicate id" in e for e in Contributes.from_dict({"widgets": dup}).validate())


def test_the_entry_is_contained_signed_and_browser_only(tmp_path) -> None:
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.apps.startup_modules import eligibility_refusal

    raw: dict[str, Any] = {
        "name": APP,
        "version": "1.0.0",
        "displayName": "Issue Radar",
        "description": "Issues.",
        "contributes": {"widgets": [_widget(entry="../../escape.mjs")]},
    }
    assert any("widgets entry" in e for e in AppManifest.from_dict(raw).validate(tmp_path))
    raw["contributes"]["widgets"] = [_widget()]
    signed = AppManifest.from_dict(raw).signing_payload()
    assert b'"widgets"' in signed
    raw["contributes"]["widgets"] = [_widget(entry="widgets/evil.mjs")]
    # Repointing the entry changes what the signature covers.
    assert AppManifest.from_dict(raw).signing_payload() != signed
    # A widget starts no process, so it keeps a browser-only app eligible.
    assert eligibility_refusal(raw, "host") is None


# ── resolver ──


def _manifest(widgets: list[dict[str, Any]], name: str = APP) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        contributes=Contributes.from_dict({"widgets": widgets}),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={},
    )


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An installed app whose enabled flag and manifest a test sets."""
    state: dict[str, Any] = {
        "enabled": True,
        "manifest": _manifest([_widget(), _widget(id="other", entry="widgets/b.mjs")]),
    }
    monkeypatch.setattr(wf, "is_app_enabled", lambda name: name == APP and state["enabled"])
    monkeypatch.setattr(wf, "get_app_manifest", lambda name: state["manifest"])
    return state


def test_the_resolver_serves_an_enabled_apps_declared_widget(installed) -> None:
    assert wf.resolve_widget(APP, "issue-card") == ("widgets/card.mjs", False, APP)
    assert wf.resolve_widget(APP, "other") == ("widgets/b.mjs", False, APP)


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"manifest": None},
        {"manifest": _manifest([_widget()], name="someone-else")},
        {"manifest": _manifest([_widget(entry="../x.mjs")])},
        {"manifest": _manifest([])},
    ],
)
def test_the_resolver_refuses_anything_else(installed, change: dict[str, Any]) -> None:
    installed.update(change)
    assert wf.resolve_widget(APP, "issue-card") is None
    assert wf.resolve_widget(APP, "missing") is None


def test_the_point_is_registered_offline_on_the_shared_frame() -> None:
    assert POINT == "widgets" and POINT in af.registered_frame_points()
    assert af._POINTS[POINT].offline is True
    # Every other point keeps its capability and its network images.
    assert all(not p.offline for name, p in af._POINTS.items() if name != POINT)


def test_the_offline_policy_loads_no_network_image() -> None:
    def img(policy: str) -> str:
        return next(d.strip() for d in policy.split(";") if d.strip().startswith("img-src"))

    assert img(af.frame_csp("N", "'self'", offline=True)) == "img-src 'self' data: blob:"
    assert img(af.frame_meta_csp("N", offline=True)).startswith("img-src")
    assert "https:" not in img(af.frame_meta_csp("N", offline=True))
    assert "https:" in img(af.frame_csp("N", "'self'"))


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
async def test_the_mint_gives_no_capability_and_the_frame_is_offline(
    monkeypatch: pytest.MonkeyPatch, installed
) -> None:
    async with _client(monkeypatch) as (client, _):
        resp = await client.post(f"/api/apps/{APP}/frames/{POINT}/issue-card")
        assert resp.status == 200, await resp.text()
        minted = await resp.json()
        assert minted["capability"] is None
        # The srcdoc copy carries the same offline image rule as the served document.
        meta = minted["document"].split('content="', 1)[1].split('"', 1)[0]
        img = next(d.strip() for d in meta.split(";") if d.strip().startswith("img-src"))
        assert "https:" not in img and img.endswith("data: blob:")
        doc = await client.get(minted["frame"])
        assert doc.status == 200
        assert "img-src 'self' data: blob:" in doc.headers["Content-Security-Policy"]
        html = await doc.text()
        assert f'"entry": "/apps/{APP}/ui/widgets/card.mjs"' in html
        assert "typeof h.render === 'function'" in html


@pytest.mark.asyncio
async def test_an_unknown_widget_or_a_disabled_app_gets_no_frame(
    monkeypatch: pytest.MonkeyPatch, installed
) -> None:
    async with _client(monkeypatch) as (client, _):
        minted = await (await client.post(f"/api/apps/{APP}/frames/{POINT}/issue-card")).json()
        assert (await client.post(f"/api/apps/{APP}/frames/{POINT}/missing")).status == 404
        installed["enabled"] = False
        assert (await client.post(f"/api/apps/{APP}/frames/{POINT}/issue-card")).status == 404
        assert (await client.get(minted["frame"])).status == 404


# ── frame script, run under node with the shared runtime ──

_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/widgets/card/f/t', href: 'http://gw/app-frame/a/widgets/card/f/t' };
globalThis.renders = [];
const mod = `export function activate(ctx) { globalThis.ctxKeys = Object.keys(ctx); return { render(props, view) {
  globalThis.renders.push({ props, viewKeys: Object.keys(view), frozen: Object.isFrozen(view), widgetId: view.widgetId });
  view.resize({ height: 222 });
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
deliver({ type: 'init', capability: null });
for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await tick();
// Not an object: never rendered.
deliver({ type: 'widget.render', props: [1, 2], theme: 'dark', widgetId: 'card' });
deliver({ type: 'widget.render', props: { number: 7 }, theme: 'dark', widgetId: 'card' });
// A second render of a finished tag is ignored.
deliver({ type: 'widget.render', props: { number: 8 }, theme: 'dark', widgetId: 'card' });
await tick();
console.log(JSON.stringify({ posted, renders, ctxKeys }));
"""


def test_the_script_renders_the_payload_once_and_hands_over_nothing_else() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    boot = af._RUNTIME + af._INVOCATION + wf.WIDGET_SCRIPT
    out = subprocess.run(
        [node, "--input-type=module", "-e", _HARNESS + boot + _DRIVER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    # No `fetch`: the point is offline.
    assert sorted(result["ctxKeys"]) == ["appName", "point", "theme"]
    assert result["renders"] == [
        {
            "props": {"number": 7},
            "viewKeys": ["widgetId", "theme", "resize"],
            "frozen": True,
            "widgetId": "card",
        }
    ]
    resizes = [m for m in result["posted"] if m["type"] == "widget.resize"]
    assert [m["height"] for m in resizes] == [222]


# ── the tag ──


def test_the_parser_reads_use_and_its_artifact_is_never_stored(monkeypatch) -> None:
    text = (
        'Here:\n<mcwidget use="issue-radar:issue-card" title="Issue 7">\n'
        '{"number": 7}\n</mcwidget>\n<mcwidget title="Page"><b>hi</b></mcwidget>\n'
    )
    widgets = parse_widgets(text)
    assert [(w.use, w.title, w.content) for w in widgets] == [
        ("issue-radar:issue-card", "Issue 7", '{"number": 7}'),
        ("", "Page", "<b>hi</b>"),
    ]

    from kiro_crew import widget_artifacts

    created: list[str] = []

    class _Store:
        def create(self, **kw: Any) -> None:
            created.append(kw["content"])

        def prune_auto_widgets(self, keep: int) -> int:
            return 0

    monkeypatch.setattr(widget_artifacts, "get_default_store", lambda: _Store())
    widget_artifacts.register_widgets(text, "1700000000.0", "dashboard:s")
    assert created == ["<b>hi</b>"]


def test_the_example_app_declares_a_valid_widget() -> None:
    from pathlib import Path

    from kiro_crew.apps.manifest import AppManifest

    root = Path(__file__).resolve().parents[1] / "docs/app-kit/examples/widget-app"
    m = AppManifest.from_dict(json.loads((root / "app.json").read_text(encoding="utf-8")))
    assert m.validate(root) == []
    entry = m.contributes.widgets.widgets[0].entry
    module = (root / "ui" / entry).read_text(encoding="utf-8")
    # The example sets payload text, never markup.
    assert "export function activate" in module and "innerHTML =" not in module
