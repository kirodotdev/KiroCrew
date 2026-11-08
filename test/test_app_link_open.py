"""``contributes.linkOpen``: an app claims hosts, and a click can open the link in it.

* The manifest entry: hosts follow the host rule every point shares (``app_hosts``),
  one entry per app, entry path containment, serialized (so signed).
* The grant: the normalized hosts and the whole-URL opt-in are grant families; a
  widening update holds them back, and the app row says what was approved.
* The frame: the point is registered on the shared app frame, so its mint, frame
  and fetch follow the link-open grant alone, and its script answers one
  invocation with a size report or an error.
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
from kiro_crew.apps import link_open_frame as lof
from kiro_crew.apps.link_open_grants import LINK_OPEN_GRANT_FAMILIES, link_open_grant_row
from kiro_crew.apps.manager_records import (
    STAGED_GRANT_FAMILIES,
    _approved_grants_after_manifest_change,
    _declared_grant_entries,
    _held_back_entries,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_contributes import Contributes, LinkOpenConfig
from kiro_crew.apps.point_grants import grant_source, point_grant_families

APP = "tickets"


def _entry(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"id": "open", "entry": "dist/open.mjs", "hosts": ["tickets.example.com"]}
    d.update(over)
    return d


def _manifest(hosts: list[str], full_url: bool = False) -> dict[str, Any]:
    entry = _entry(hosts=hosts)
    if full_url:
        entry["fullUrl"] = True
    return {"name": APP, "permissions": {"api": []}, "contributes": {"linkOpen": [entry]}}


# Manifest


def test_an_entry_is_normalized_validated_and_serialized() -> None:
    cfg = LinkOpenConfig.from_dict(_entry(hosts=["Tickets.Example.COM.", ".Docs.example.com"]))
    assert cfg.hosts == ["tickets.example.com", ".docs.example.com"]
    assert cfg.validate() == []
    contributes = Contributes.from_dict({"linkOpen": [cfg.to_dict()]})
    # Serialized, so the signing payload covers which links the app is handed.
    assert contributes.to_dict()["linkOpen"] == [cfg.to_dict()]


@pytest.mark.parametrize(
    "host", ["co.uk", ".co.uk", "github.io", "com", ".dev", "faß.de", "127.0.0.1", "a.localhost"]
)
def test_a_host_no_point_may_claim_is_refused(host: str) -> None:
    errors = LinkOpenConfig.from_dict(_entry(hosts=[host])).validate()
    assert any("hosts entry" in e for e in errors), errors


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        (_entry(hosts="tickets.example.com"), "hosts must be an array"),
        (_entry(hosts=[]), "at least one hostname"),
        (_entry(hosts=["a.example.com", "A.example.com"]), "repeats"),
        (_entry(hosts=[f"h{i}.example.com" for i in range(65)]), "exceeds the limit"),
        (_entry(fullUrl="yes"), "fullUrl must be true or false"),
        (_entry(id=""), "missing id"),
        (_entry(id="Bad Id"), "storage-safe slug"),
        (_entry(entry=""), "missing entry"),
    ],
)
def test_a_malformed_entry_is_refused(data: dict[str, Any], needle: str) -> None:
    errors = LinkOpenConfig.from_dict(data).validate()
    assert any(needle in e for e in errors), errors


def test_one_entry_per_app_and_the_block_must_be_a_list() -> None:
    two = Contributes.from_dict({"linkOpen": [_entry(), _entry(id="other")]})
    assert any("exceeds the limit of 1" in e for e in two.validate())
    bad = Contributes.from_dict({"linkOpen": {"id": "open"}})
    assert any("linkOpen must be an array" in e for e in bad.validate())
    dropped = Contributes.from_dict({"linkOpen": ["x"]})
    assert any("must be an object" in e for e in dropped.validate())


def test_the_entry_cannot_leave_the_app_root(tmp_path: Any) -> None:
    m = AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "description": "d",
            "contributes": {"linkOpen": [_entry(entry="../../escape.mjs")]},
        }
    )
    errors = m.validate(tmp_path)
    assert any("contributes.linkOpen entry" in e for e in errors), errors


# Grants


def test_the_families_are_registered_and_staged() -> None:
    assert set(LINK_OPEN_GRANT_FAMILIES) <= set(point_grant_families())
    assert set(LINK_OPEN_GRANT_FAMILIES) <= set(STAGED_GRANT_FAMILIES)


def test_the_descriptor_is_the_normalized_hosts_and_the_url_policy() -> None:
    src = grant_source(_manifest(["A.Example.com.", ".b.example.com"], full_url=True))
    assert src is not None
    assert src["linkOpenHosts"] == ["a.example.com", ".b.example.com"]
    assert src["linkOpenFullUrl"] == ["fullUrl"]


def test_a_widening_update_holds_the_new_host_and_whole_urls_back() -> None:
    old = grant_source(_manifest(["a.example.com"]))
    new = grant_source(_manifest(["a.example.com", "b.example.com"], full_url=True))
    approved = _approved_grants_after_manifest_change(
        existing_approved=_declared_grant_entries(old), old_permissions=old, new_permissions=new
    )
    assert approved["linkOpenHosts"] == ["a.example.com"]
    assert approved["linkOpenFullUrl"] == []
    held = _held_back_entries(approved, new)
    assert held["linkOpenHosts"] == ["b.example.com"]
    assert held["linkOpenFullUrl"] == ["fullUrl"]


def test_the_grant_row_reports_approved_and_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    def staged(name: str, family: str, read: Any) -> list[str]:
        return read()[:1] if family == "linkOpenHosts" else []

    monkeypatch.setattr("kiro_crew.apps.manager_records.staged_app_grants", staged)
    row = link_open_grant_row(APP, _manifest(["a.example.com", "b.example.com"], full_url=True))
    assert row["linkOpenGrant"] == {
        "hosts": ["a.example.com"],
        "stagedHosts": ["b.example.com"],
        "fullUrl": False,
        "stagedFullUrl": True,
    }
    assert link_open_grant_row(APP, {"contributes": {}}) == {}


# Frame point


@pytest.fixture
def granted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A manifest with a link-open entry; ``state["hosts"]`` is what the owner approved."""
    manifest = SimpleNamespace(
        name=APP,
        contributes=SimpleNamespace(
            linkOpen=[SimpleNamespace(id="open", entry="./dist/open.mjs", fullUrl=True)]
        ),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={},
    )
    state: dict[str, Any] = {"enabled": True, "hosts": ["tickets.example.com"], "fullUrl": False}
    monkeypatch.setattr(lof, "is_app_enabled", lambda name: state["enabled"])
    monkeypatch.setattr(lof, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(
        lof,
        "link_open_grant_row",
        lambda n, m: {"linkOpenGrant": {"hosts": state["hosts"], "fullUrl": state["fullUrl"]}},
    )
    return state


def test_the_resolver_follows_the_link_open_grant(granted: dict[str, Any]) -> None:
    assert "linkOpen" in af.registered_frame_points()
    assert lof.resolve_link_open(APP, "open") == ("dist/open.mjs", False, APP)
    assert lof.resolve_link_open(APP, "other") is None
    granted["hosts"] = []
    assert lof.resolve_link_open(APP, "open") is None
    granted["hosts"] = ["tickets.example.com"]
    granted["enabled"] = False
    assert lof.resolve_link_open(APP, "open") is None


#: A mint for a stripped open of a link on the approved host.
_MINT = f"/api/apps/{APP}/frames/linkOpen/open?host=tickets.example.com&url=stripped"


def test_a_mint_is_admitted_only_for_a_live_host_and_url_mode(granted: dict[str, Any]) -> None:
    def admits(**query: str) -> bool:
        return lof.admit_link_open(APP, "open", query)

    assert admits(host="tickets.example.com", url="stripped")
    # No whole URL until the owner approved it.
    assert not admits(host="tickets.example.com", url="full")
    granted["fullUrl"] = True
    assert admits(host="tickets.example.com", url="full")
    # A host the grant does not cover, a malformed request, or another entry.
    assert not admits(host="other.example.com", url="stripped")
    assert not admits(host="TICKETS.example.com", url="stripped")
    assert not admits(host=".tickets.example.com", url="stripped")
    assert not admits(host="tickets.example.com")
    assert not admits(url="stripped")
    assert not lof.admit_link_open(APP, "other", {"host": "tickets.example.com", "url": "stripped"})
    # A leading-dot grant covers the domain and its subdomains, as the dashboard reads it.
    granted["hosts"] = [".example.com"]
    assert admits(host="example.com", url="stripped")
    assert admits(host="a.b.example.com", url="stripped")
    assert not admits(host="badexample.com", url="stripped")
    # The owner withdrew the host: refused even though the entry still declares it.
    granted["hosts"] = ["docs.example.com"]
    assert not admits(host="tickets.example.com", url="stripped")


@pytest.mark.asyncio
async def test_a_withdrawn_host_or_whole_url_is_refused_at_the_mint(
    monkeypatch: pytest.MonkeyPatch, granted: dict[str, Any]
) -> None:
    async with _client(monkeypatch) as client:
        base = f"/api/apps/{APP}/frames/linkOpen/open"
        assert (await client.post(base)).status == 403
        assert (await client.post(f"{base}?host=tickets.example.com&url=full")).status == 403
        assert (await client.post(f"{base}?host=gone.example.com&url=stripped")).status == 403
        minted = await (await client.post(_MINT)).json()
        # A renewal is a mint too: the frame's next capability follows the grant.
        granted["hosts"] = ["tickets.example.com", "docs.example.com"]
        renew = f"{_MINT}&frame={minted['frameId']}"
        assert (await client.post(renew)).status == 200
        granted["hosts"] = ["docs.example.com"]
        assert (await client.post(renew)).status == 403


@asynccontextmanager
async def _client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    class _Registry:
        async def dispatch(self, request: web.Request) -> web.Response:
            return web.json_response({"app": request.get("app")})

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
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_link_open_frame_runs_the_link_open_script(
    monkeypatch: pytest.MonkeyPatch, granted: dict[str, Any]
) -> None:
    async with _client(monkeypatch) as client:
        resp = await client.post(_MINT)
        assert resp.status == 200, await resp.text()
        minted = await resp.json()
        doc = await client.get(minted["frame"])
        assert doc.status == 200
        html = await doc.text()
        assert "sandbox allow-scripts" in doc.headers["Content-Security-Policy"]
        assert "link.open" in html and "cardControls" not in html
        config = json.loads(html.split('id="frame-config">', 1)[1].split("</script>", 1)[0])
        assert config["entry"] == f"/apps/{APP}/ui/dist/open.mjs"


@pytest.mark.asyncio
async def test_revoking_the_hosts_stops_the_frame_and_its_fetch(
    monkeypatch: pytest.MonkeyPatch, granted: dict[str, Any]
) -> None:
    async with _client(monkeypatch) as client:
        minted = await (await client.post(_MINT)).json()
        cap = {af.CAPABILITY_HEADER: minted["capability"]}
        assert (await client.get(f"{af.FETCH_PREFIX}{APP}/x", headers=cap)).status == 200
        granted["hosts"] = []
        assert (await client.get(minted["frame"])).status == 404
        assert (await client.get(f"{af.FETCH_PREFIX}{APP}/x", headers=cap)).status == 401
        assert (await client.post(_MINT)).status == 404


# Frame script: one invocation, answered once

_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/linkOpen/open/f/t', href: 'http://gw/app-frame/a/linkOpen/open/f/t' };
globalThis.seen = [];
globalThis.cancels = [];
const opener = `export function activate(ctx) { return {
  async open(link) {
    globalThis.seen.push({ url: link.url, id: link.invocationId, frozen: Object.isFrozen(link), hasRegions: typeof ctx.setRegions });
    if (link.url.endsWith('/fail')) throw new Error('nope');
  },
  cancel(id, why) { globalThis.cancels.push([id, why]); },
}; }`;
globalThis.document = {
  activeElement: null,
  body: {},
  documentElement: { scrollWidth: 640, scrollHeight: 480 },
  getElementById: () => ({ textContent: JSON.stringify({
    app: 'a', entry: 'data:text/javascript,' + encodeURIComponent(opener),
    fetchBase: '/app-frame-fetch/a/', theme: 'light',
  }) }),
};
"""

_DRIVER = r"""
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { 'kirocrew-app-frame': 1, ...data } }));
const tick = (ms) => new Promise((r) => setTimeout(r, ms));
deliver({ type: 'init', capability: 'c' });
for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await tick(10);
deliver({ type: 'link.open', invocationId: 'aaaaaaaa-1', deadlineMs: 1000, url: 'https://x.example.com/ok' });
// The same id again, and a malformed id: ignored.
deliver({ type: 'link.open', invocationId: 'aaaaaaaa-1', url: 'https://x.example.com/again' });
deliver({ type: 'link.open', invocationId: 'bad id', url: 'https://x.example.com/bad' });
deliver({ type: 'link.open', invocationId: 'cccccccc-1', url: 'https://x.example.com/fail' });
// A show is a link-preview message: a link-open frame drops it.
deliver({ type: 'show', invocation: 1, url: 'https://x.example.com/s', rect: {}, theme: 'light' });
await tick(30);
deliver({ type: 'invoke.cancel', invocationId: 'aaaaaaaa-1', reason: 'closed' });
console.log(JSON.stringify({
  seen: globalThis.seen,
  cancels: globalThis.cancels,
  answers: posted.filter((m) => m.type.startsWith('invocation.')).map((m) => [m.type, m.invocationId, m.width ?? m.message ?? null]),
}));
"""


def test_the_script_answers_each_invocation_once() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = _HARNESS + af._RUNTIME + af._INVOCATION + lof.LINK_OPEN_POINT_SCRIPT + _DRIVER
    out = subprocess.run(
        [node, "--input-type=module", "-e", src],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert [s["url"] for s in result["seen"]] == [
        "https://x.example.com/ok",
        "https://x.example.com/fail",
    ]
    assert {s["frozen"] for s in result["seen"]} == {True}
    # A link-open frame's ctx has no link-preview controls.
    assert {s["hasRegions"] for s in result["seen"]} == {"undefined"}
    assert result["answers"] == [
        ["invocation.resize", "aaaaaaaa-1", 640],
        ["invocation.error", "cccccccc-1", "nope"],
    ]
    assert result["cancels"] == [["aaaaaaaa-1", "closed"]]
