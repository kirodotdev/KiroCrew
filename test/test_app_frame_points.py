"""The shared app-frame contract every frame-backed point builds on.

* Frames, mints and fetches are keyed per ``(point, id)``: a token or capability
  minted for one entry opens nothing else, and an unregistered point or a bad id
  gets nothing.
* A point registers a resolver; every route asks it, so a revoked entry stops at
  its next request.
* The frame runtime's invocation contract: host-minted id, sequenced offers settled
  by the host's answer, frozen controls, cancel.
* Grant families come from one registry, and ``app_hosts`` owns the host rule.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import app_frame as af
from kiro_crew.apps import app_hosts, link_preview_grants, point_grants
from kiro_crew.apps.manager_records import STAGED_GRANT_FAMILIES

APP = "notes-app"


@pytest.fixture
def points(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], bool]:
    """Two test points, ``alphaPoint`` and ``betaPoint``; the dict says which entries resolve."""
    granted: dict[tuple[str, str], bool] = {}
    monkeypatch.setattr(af, "_POINTS", dict(af._POINTS))
    for name in ("alphaPoint", "betaPoint"):

        def resolve(app: str, pid: str, name: str = name) -> af.FrameEntry | None:
            return ("dist/f.mjs", False, APP) if app == APP and granted.get((name, pid)) else None

        af.register_frame_point(name, resolve, f"const POINT = {{ point: '{name}' }};")
    return granted


@asynccontextmanager
async def _client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
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
        yield client
    finally:
        await client.close()


async def _mint(client: TestClient, point: str, pid: str) -> dict[str, Any]:
    resp = await client.post(f"/api/apps/{APP}/frames/{point}/{pid}")
    assert resp.status == 200, await resp.text()
    data: dict[str, Any] = await resp.json()
    return data


def test_a_point_name_must_be_a_manifest_key() -> None:
    for bad in ("", "Link", "link-previews", "a" * 33, "lp/x"):
        with pytest.raises(ValueError):
            af.register_frame_point(bad, lambda a, p: None, "")
    assert "linkPreviews" in af.registered_frame_points()


@pytest.mark.asyncio
async def test_each_entry_gets_its_own_frame_and_capability(
    monkeypatch: pytest.MonkeyPatch, points: dict[tuple[str, str], bool]
) -> None:
    points.update(
        {("alphaPoint", "one"): True, ("alphaPoint", "two"): True, ("betaPoint", "one"): True}
    )
    async with _client(monkeypatch) as client:
        minted = await _mint(client, "alphaPoint", "one")
        assert minted["frame"].startswith(f"{af.FRAME_PREFIX}{APP}/alphaPoint/one/")
        resp = await client.get(minted["frame"])
        assert resp.status == 200
        # The point's own script is in its frame, after the shared runtime.
        html = await resp.text()
        assert "const POINT = { point: 'alphaPoint' }" in html
        assert "function invocationControls(invocationId)" in html
        frame_id, token = minted["frame"].rsplit("/", 2)[1:]
        for other in ("alphaPoint/two", "betaPoint/one"):
            assert (
                await client.get(f"{af.FRAME_PREFIX}{APP}/{other}/{frame_id}/{token}")
            ).status == 404
        fid, point, pid, signed = minted["capability"].split("~")
        assert (point, pid) == ("alphaPoint", "one")
        assert af._capability_point(minted["capability"], APP, "127.0.0.1") == ("alphaPoint", "one")
        for relabelled in (f"{fid}~alphaPoint~two~{signed}", f"{fid}~betaPoint~one~{signed}"):
            assert not af._capability_ok(relabelled, APP, "127.0.0.1")


@pytest.mark.asyncio
async def test_an_unknown_point_or_a_bad_id_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, points: dict[tuple[str, str], bool]
) -> None:
    points[("alphaPoint", "one")] = True
    async with _client(monkeypatch) as client:
        for path in ("gammaPoint/one", "alphaPoint/One", "alphaPoint/1x", "alphaPoint/nope"):
            resp = await client.post(f"/api/apps/{APP}/frames/{path}")
            assert resp.status == 404, path


@pytest.mark.asyncio
async def test_revoking_the_entry_stops_its_frame_and_its_fetch(
    monkeypatch: pytest.MonkeyPatch, points: dict[tuple[str, str], bool]
) -> None:
    points[("alphaPoint", "one")] = True
    async with _client(monkeypatch) as client:
        minted = await _mint(client, "alphaPoint", "one")
        url = f"{af.FETCH_PREFIX}{APP}/notes"
        headers = {af.CAPABILITY_HEADER: minted["capability"]}
        assert (await client.post(url, headers=headers)).status == 200
        points[("alphaPoint", "one")] = False
        assert (await client.post(url, headers=headers)).status == 401
        assert (await client.get(minted["frame"])).status == 404
        assert (await client.post(f"/api/apps/{APP}/frames/alphaPoint/one")).status == 404


#: Runs the real runtime and invocation contract under node with a stub window and
#: a point that hands its handler `invocationControls` for each `test.invoke`.
_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/alphaPoint/one/f/t', href: 'http://gw/app-frame/a/alphaPoint/one/f/t' };
const appModule = 'export function activate(ctx) { return { invoke(c) { globalThis.calls.push(c); }, cancel(id, why) { globalThis.cancels.push([id, why]); } }; }';
globalThis.calls = [];
globalThis.cancels = [];
globalThis.document = {
  activeElement: null,
  body: {},
  getElementById: () => ({ textContent: JSON.stringify({
    app: 'a', entry: 'data:text/javascript,' + encodeURIComponent(appModule),
    fetchBase: '/app-frame-fetch/a/', theme: 'light',
  }) }),
};
const POINT = {
  ctx: {},
  accepts: (h) => !!h && typeof h.invoke === 'function',
  missing: 'no invoke',
  message(m) { if (m.type === 'test.invoke' && typeof m.invocationId === 'string') call('invoke', invocationControls(m.invocationId)); },
};
"""
_DRIVER = r"""
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { 'kirocrew-app-frame': 1, ...data } }));
deliver({ type: 'init', capability: 'c' });
for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await new Promise((r) => setTimeout(r, 10));
deliver({ type: 'test.invoke', invocationId: 'inv-1' });
deliver({ type: 'test.invoke', invocationId: 'inv-2' });
const [first, second] = calls;
const a = first.offerCopy('one');
const b = first.offerOpen('https://x.example.com/');
const c = second.offerCopy('two');
first.resize({ width: 10, height: Infinity });
first.close();
second.error('bad');
const offers = posted.filter((m) => m.type === 'result.offer');
deliver({ type: 'result.ack', invocationId: 'inv-1', seq: offers[0].seq });
deliver({ type: 'result.error', invocationId: 'inv-1', seq: offers[1].seq, code: 'stale' });
deliver({ type: 'result.ack', invocationId: 'inv-2', seq: offers[2].seq });
deliver({ type: 'invoke.cancel', invocationId: 'inv-1', reason: 'closed' });
console.log(JSON.stringify({
  frozen: Object.isFrozen(first),
  offers: offers.map((m) => [m.invocationId, m.seq, m.kind, m.value]),
  rest: posted.filter((m) => m.type.startsWith('invocation.')).map((m) => [m.type, m.invocationId, m.height ?? m.message ?? null]),
  results: [await a, await b, await c],
  cancels,
}));
"""


def test_the_invocation_contract_numbers_offers_and_settles_each_on_its_answer() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    src = _HARNESS + af._RUNTIME + af._INVOCATION + _DRIVER
    out = subprocess.run(
        [node, "--input-type=module", "-e", src],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["frozen"] is True
    # One rising sequence across invocations, each offer naming its own invocation.
    assert result["offers"] == [
        ["inv-1", 1, "copy", "one"],
        ["inv-1", 2, "open", "https://x.example.com/"],
        ["inv-2", 3, "copy", "two"],
    ]
    assert result["rest"] == [
        ["invocation.resize", "inv-1", 0],
        ["invocation.close", "inv-1", None],
        ["invocation.error", "inv-2", "bad"],
    ]
    assert result["results"] == [{"ok": True}, {"ok": False, "code": "stale"}, {"ok": True}]
    assert result["cancels"] == [["inv-1", "closed"]]


def test_point_grant_families_come_from_one_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    assert point_grants.point_grant_families()[
        : len(link_preview_grants.LINK_PREVIEW_GRANT_FAMILIES)
    ] == (link_preview_grants.LINK_PREVIEW_GRANT_FAMILIES)
    assert STAGED_GRANT_FAMILIES == ("api", "events", *point_grants.point_grant_families())
    monkeypatch.setattr(point_grants, "_REGISTRY", list(point_grants._REGISTRY))
    with pytest.raises(ValueError):
        point_grants.register_point_grants(("linkPreviewHosts",), lambda c: {})
    with pytest.raises(ValueError):
        point_grants.register_point_grants(("api",), lambda c: {})
    point_grants.register_point_grants(
        ("testFamily",), lambda c: {"testFamily": ["x"] if c else []}
    )
    src = point_grants.grant_source({"permissions": {"api": ["/api/x"]}, "contributes": {"a": 1}})
    assert src is not None
    assert src["api"] == ["/api/x"]
    assert src["testFamily"] == ["x"]
    assert point_grants.grant_source(None) is None


def test_the_host_rule_has_one_home() -> None:
    assert link_preview_grants.normalize_host is app_hosts.normalize_host
    assert link_preview_grants.host_refusal is app_hosts.host_refusal
    assert app_hosts.normalize_host("Tickets.Example.COM.") == "tickets.example.com"
    assert app_hosts.host_refusal(".co.uk") is not None
