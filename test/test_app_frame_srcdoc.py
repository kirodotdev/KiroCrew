"""An app frame behind an authenticating reverse proxy: the srcdoc document and the relay.

A sandboxed frame has an opaque origin, so its own requests carry no cookie. A
reverse proxy that authenticates every request by cookie answers them with its login
page: the frame document never loads, or (where the browser sends the cookie on the
iframe navigation) the module import and every ``ctx.fetch`` are refused. The mint
therefore hands the dashboard the frame document itself, for ``srcdoc``, and the
frame's network goes through the dashboard (``website/src/appFrame/frameRelay.ts``).

Pinned here: the mint's document is the frame's own (same bootstrap, same policy as a
``<meta>``, nothing widened), a renewal carries none, and the bootstrap, run for real
under node, loads a module that registers through the relay when its own import is
refused, keeps the URL import for a module that is served, and sends ``ctx.fetch``
through the relay with the capability.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest
from test_app_frame import MINT, PID, _client, _mint  # noqa: F401  (shared fixtures)

from kiro_crew.apps import app_frame as lp
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

APP = "cards-app"


def _directives(policy: str) -> list[str]:
    return [d.strip() for d in policy.split(";") if d.strip()]


@pytest.mark.asyncio
async def test_a_first_mint_returns_the_frame_document_for_srcdoc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(monkeypatch) as (client, _):
        minted = await _mint(client)
        doc = minted["document"]
        assert doc.startswith("<!doctype html>")
        # The policy is a <meta>, and it is the first thing in <head>, before any script.
        head = doc.split("<head>", 1)[1]
        assert head.index('http-equiv="Content-Security-Policy"') < head.index("<script")
        meta = doc.split('http-equiv="Content-Security-Policy" content="', 1)[1].split('"', 1)[0]
        nonce = meta.split("'nonce-", 1)[1].split("'", 1)[0]
        assert meta == lp.frame_meta_csp(nonce)
        assert f'<script type="module" nonce="{nonce}">' in doc
        island = json.loads(doc.split('id="frame-config">', 1)[1].split("</script>", 1)[0])
        assert island == {
            "app": APP,
            "entry": f"/apps/{APP}/ui/dist/lp.mjs",
            "fetchBase": f"/app-frame-fetch/{APP}/",
            "theme": "light",
            "srcdoc": True,
            "nonce": nonce,
        }


@pytest.mark.asyncio
async def test_a_renewal_carries_no_document(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch) as (client, _):
        first = await _mint(client)
        resp = await client.post(f"{MINT}?frame={first['frameId']}")
        renewed: dict[str, Any] = await resp.json()
        assert renewed["frameId"] == first["frameId"]
        assert "document" not in renewed


def test_the_meta_policy_is_the_frame_policy_and_widens_nothing() -> None:
    """Same directives as the response policy; 'self' is the dashboard's origin."""
    header = _directives(lp.frame_csp("N", "'self'"))
    meta = _directives(lp.frame_meta_csp("N"))
    # Exactly the header's directives, minus the two a <meta> cannot carry.
    expected = [
        d.replace("'self'", lp.SRCDOC_SELF)
        for d in header
        if d.split()[0] not in ("sandbox", "frame-ancestors")
    ]
    assert meta == expected
    joined = " ".join(meta)
    for wider in ("blob:", "'unsafe-eval'", "'unsafe-inline'", "'strict-dynamic'", "*"):
        script_src = next(d for d in meta if d.startswith("script-src"))
        assert wider not in script_src, f"script-src gained {wider!r}"
    assert "'self'" not in joined, "'self' in an opaque srcdoc document matches nothing"
    assert next(d for d in meta if d.startswith("script-src")) == (
        f"script-src {lp.SRCDOC_SELF} 'nonce-N'"
    )
    assert f"connect-src {lp.SRCDOC_SELF}" in meta


@pytest.mark.asyncio
async def test_the_mint_still_refuses_what_it_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document is only ever handed out after the mint's own checks."""
    async with _client(monkeypatch, session={"is_dashboard_user": True, "app": "x"}) as (c, _):
        resp = await c.post(MINT)
        assert resp.status == 403
        assert "document" not in await resp.json()
    async with _client(monkeypatch, entry=None) as (c, _):
        resp = await c.post(MINT)
        assert resp.status == 404
        assert "document" not in await resp.json()


# ---------------------------------------------------------------------------
# The bootstrap, run for real under node
# ---------------------------------------------------------------------------

#: A frame window: what it posts is recorded; an appended inline module is run as a
#: data: module (what a browser does with a nonce'd inline module script).
_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = {
  parent,
  addEventListener: (t, f) => { (listeners[t] ||= []).push(f); },
  removeEventListener: (t, f) => { listeners[t] = (listeners[t] || []).filter((g) => g !== f); },
};
globalThis.location = globalThis.LOCATION || { pathname: 'srcdoc', href: 'about:srcdoc' };
globalThis.inlineRuns = [];
globalThis.document = {
  activeElement: null,
  body: {},
  head: { appendChild: (s) => {
    globalThis.inlineRuns.push({ type: s.type, nonce: s.nonce });
    import('data:text/javascript,' + encodeURIComponent(s.textContent)).catch((e) => (listeners.error || []).forEach((f) => f({ error: e })));
  } },
  createElement: () => ({}),
  getElementById: () => ({ textContent: JSON.stringify(globalThis.CONFIG) }),
};
"""

_DRIVER = r"""
const DTAG = 'kirocrew-app-frame';
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { [DTAG]: 1, ...data } }));
const until = async (f) => { for (let i = 0; i < 200 && !f(); i++) await new Promise((r) => setTimeout(r, 10)); };
const answerRelays = async () => {
  for (const m of posted.filter((p) => p.type === 'relay' && !p.answered)) {
    m.answered = true;
    const out = globalThis.RELAY_ANSWER(m);
    if (out.ok !== true) { deliver({ type: 'relay.response', id: m.id, ok: false }); continue; }
    // The streaming answer: head, then the body in chunks (unless held open), then end.
    const { body, hold, ...head } = out;
    deliver({ type: 'relay.response', id: m.id, ...head });
    if (body) deliver({ type: 'relay.chunk', id: m.id, chunk: body });
    if (!hold) deliver({ type: 'relay.end', id: m.id });
  }
};
const pump = setInterval(answerRelays, 5);
deliver({ type: 'init', capability: 'cap-1', relay: 'nonce-1', base: globalThis.BASE });
await until(() => posted.some((m) => m.type === 'ready' || m.type === 'failed'));
let fetched = null;
if (posted.some((m) => m.type === 'ready') && globalThis.FETCH_PATH) {
  const res = await globalThis.kirocrewCtx.fetch(globalThis.FETCH_PATH, { method: 'POST', body: '{"k":1}', headers: { 'content-type': 'application/json' } });
  fetched = { status: res.status, text: await res.text() };
}
clearInterval(pump);
console.log(JSON.stringify({
  posted: posted.map(({ answered, body, ...m }) => ({ ...m, body: typeof body === 'string' ? body : body ? '<bytes>' : null })),
  inlineRuns: globalThis.inlineRuns,
  activations: globalThis.activations || 0,
  fetched,
}));
"""

#: Provider module bodies. Each counts activations and hands its ctx out.
_REGISTERS = (
    "export function activate(ctx) { globalThis.activations = (globalThis.activations||0)+1;"
    " globalThis.kirocrewCtx = ctx; return { show() {} }; }\n"
    "globalThis.kirocrew.register(activate)\n"
)
_ACTIVATE_ONLY = (
    "export function activate(ctx) { globalThis.activations = (globalThis.activations||0)+1;"
    " globalThis.kirocrewCtx = ctx; return { show() {} }; }\n"
)


def _run(
    config: dict[str, Any], answer_js: str, fetch_path: str = "", base: str = "/mnt"
) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prelude = (
        f"globalThis.CONFIG = {json.dumps(config)};\n"
        f"globalThis.RELAY_ANSWER = {answer_js};\n"
        f"globalThis.FETCH_PATH = {json.dumps(fetch_path)};\n"
        f"globalThis.BASE = {json.dumps(base)};\n"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", prelude + _HARNESS + lp._BOOTSTRAP + _DRIVER],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    result: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return result


def _config(entry: str) -> dict[str, Any]:
    return {
        "app": APP,
        "entry": entry,
        "fetchBase": f"/app-frame-fetch/{APP}/",
        "theme": "light",
        "srcdoc": True,
        "nonce": "N1",
    }


def _serving(module: str) -> str:
    """A relay answer: the module for its UI path, an echo for the fetch route."""
    return (
        "(m) => m.path.startsWith('/apps/') ? { ok: true, status: 200, headers: [['content-type','text/javascript']],"
        f" body: new TextEncoder().encode({json.dumps(module)}).buffer }}"
        " : { ok: true, status: 200, headers: [['content-type','application/json']],"
        " body: new TextEncoder().encode(JSON.stringify({ path: m.path, method: m.method, headers: m.headers, body: m.body, nonce: m.nonce })).buffer }"
    )


#: An entry the frame's own import cannot load: what a proxy's login answer amounts to.
_REFUSED = f"/apps/{APP}/ui/refused-by-the-proxy.mjs"


def test_a_registering_module_loads_through_the_relay_when_its_import_is_refused() -> None:
    result = _run(_config(_REFUSED), _serving(_REGISTERS))
    types = [m["type"] for m in result["posted"]]
    assert "ready" in types, result
    assert "failed" not in types
    assert result["activations"] == 1
    relay = next(m for m in result["posted"] if m["type"] == "relay")
    # The module path as the app's own UI path, without the mount; the frame's nonce.
    assert relay["path"] == _REFUSED
    assert relay["method"] == "GET"
    assert relay["nonce"] == "nonce-1"
    # Run as an inline module carrying the document's nonce.
    assert result["inlineRuns"] == [{"type": "module", "nonce": "N1"}]


def test_a_module_that_only_exports_activate_still_fails_behind_the_proxy() -> None:
    """Nothing changes for it there: it has no way to hand over its entry."""
    result = _run(_config(_REFUSED), _serving(_ACTIVATE_ONLY))
    types = [m["type"] for m in result["posted"]]
    assert "failed" in types
    assert "ready" not in types
    assert result["activations"] == 0


def test_a_served_module_is_imported_by_url_and_activated_once() -> None:
    """The direct path: the URL import wins, and a register call there is ignored."""
    served = "data:text/javascript," + __import__("urllib.parse").parse.quote(_REGISTERS)
    # At the root (no mount), so the entry is the URL itself.
    result = _run(_config(served), "(m) => ({ ok: false })", base="")
    types = [m["type"] for m in result["posted"]]
    assert "ready" in types, result
    assert result["activations"] == 1
    assert result["inlineRuns"] == []
    assert not any(m["type"] == "relay" for m in result["posted"])


def test_ctx_fetch_goes_through_the_relay_with_the_capability() -> None:
    result = _run(_config(_REFUSED), _serving(_REGISTERS), fetch_path="kv?x=1")
    assert result["fetched"]["status"] == 200
    echoed = json.loads(result["fetched"]["text"])
    # The app route, without the mount the dashboard adds back; the capability
    # header the frame always sent; the frame's own nonce.
    assert echoed["path"] == f"/app-frame-fetch/{APP}/kv?x=1"
    assert echoed["method"] == "POST"
    assert ["x-kirocrew-capability", "cap-1"] in echoed["headers"]
    assert ["content-type", "application/json"] in echoed["headers"]
    assert echoed["body"] == '{"k":1}'
    assert echoed["nonce"] == "nonce-1"


def test_a_refused_relay_answer_is_a_network_error_as_a_refused_cors_read_was() -> None:
    js = (
        "(m) => m.path.startsWith('/apps/') ? "
        + _serving(_REGISTERS).split("? ", 1)[1].split(" : ", 1)[0]
        + " : { ok: false }"
    )
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = _DRIVER.replace(
        "  fetched = { status: res.status, text: await res.text() };",
        "  fetched = { status: res.status };",
    ).replace(
        "  const res = await globalThis.kirocrewCtx.fetch(",
        "  const res = await globalThis.kirocrewCtx.fetch(",
    )
    prelude = (
        f"globalThis.CONFIG = {json.dumps(_config(_REFUSED))};\n"
        f"globalThis.RELAY_ANSWER = {js};\n"
        "globalThis.FETCH_PATH = 'kv';\n"
        "globalThis.BASE = '/mnt';\n"
    )
    wrapped = driver.replace(
        "let fetched = null;",
        "let fetched = null; let fetchError = null;",
    ).replace(
        "  fetched = { status: res.status };\n}",
        "  fetched = { status: res.status };\n}\n",
    )
    wrapped = (
        wrapped.replace(
            "if (posted.some((m) => m.type === 'ready') && globalThis.FETCH_PATH) {",
            "if (posted.some((m) => m.type === 'ready') && globalThis.FETCH_PATH) { try {",
        )
        .replace(
            "  fetched = { status: res.status };\n}\n",
            "  fetched = { status: res.status }; } catch (e) { fetchError = e.name; }\n}\n",
        )
        .replace("  fetched,\n", "  fetched, fetchError,\n")
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", prelude + _HARNESS + lp._BOOTSTRAP + wrapped],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["fetched"] is None
    assert result["fetchError"] == "TypeError"
    # capFetch renewed once and asked again, as it does on a refused read today.
    assert [m["type"] for m in result["posted"]].count("capabilityExpired") == 1


def _run_script(config: dict[str, Any], answer_js: str, script: str) -> dict[str, Any]:
    """Run the bootstrap, then *script* (an async body with ``ctx`` = the module's ctx)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = _DRIVER.split("let fetched = null;", 1)[0] + (
        "let out = null;\n"
        "if (posted.some((m) => m.type === 'ready')) { const ctx = globalThis.kirocrewCtx; "
        "try { out = await (async () => {"
        + script
        + "})(); } catch (e) { out = { error: e.name } } }\n"
        "clearInterval(pump);\n"
        "console.log(JSON.stringify({ out, relays: posted.filter((m) => m.type === 'relay' || m.type === 'relay.abort')"
        ".map(({ answered, body, ...m }) => ({ ...m, body: typeof body === 'string' ? body : body ? new TextDecoder().decode(body) : null })) }));\n"
    )
    prelude = (
        f"globalThis.CONFIG = {json.dumps(config)};\n"
        f"globalThis.RELAY_ANSWER = {answer_js};\n"
        "globalThis.FETCH_PATH = '';\n"
        "globalThis.BASE = '/mnt';\n"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", prelude + _HARNESS + lp._BOOTSTRAP + driver],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    result: dict[str, Any] = json.loads(out.stdout.strip().splitlines()[-1])
    return result


def test_a_form_data_body_is_relayed_as_multipart() -> None:
    """FormData goes through the platform's own serialization, boundary included."""
    script = (
        "const fd = new FormData(); fd.append('field', 'value');"
        " const r = await ctx.fetch('upload', { method: 'POST', body: fd }); return { status: r.status };"
    )
    result = _run_script(_config(_REFUSED), _serving(_REGISTERS), script)
    assert result["out"] == {"status": 200}
    upload = next(m for m in result["relays"] if m["type"] == "relay" and "upload" in m["path"])
    ctype = dict(upload["headers"])["content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert 'name="field"' in upload["body"] and "value" in upload["body"]


def test_an_abort_while_the_body_is_read_sends_nothing() -> None:
    script = (
        "const ac = new AbortController();"
        " const blob = new Blob(['x']);"
        " const p = ctx.fetch('save', { method: 'POST', body: blob, signal: ac.signal });"
        " ac.abort(); try { await p; return { resolved: true }; } catch (e) { return { error: e.name }; }"
    )
    result = _run_script(_config(_REFUSED), _serving(_REGISTERS), script)
    assert result["out"] == {"error": "AbortError"}
    assert not any("save" in m.get("path", "") for m in result["relays"] if m["type"] == "relay")


def test_a_held_open_response_resolves_and_streams_before_it_ends() -> None:
    """An event stream: the fetch resolves on the head, and chunks arrive while it stays open."""
    answer = (
        "(m) => m.path.startsWith('/apps/') ? "
        + _serving(_REGISTERS).split("? ", 1)[1].split(" : ", 1)[0]
        + " : { ok: true, status: 200, headers: [['content-type','text/event-stream']],"
        " body: new TextEncoder().encode('data: one\\n\\n').buffer, hold: true }"
    )
    script = (
        "const r = await ctx.fetch('events');"
        " const reader = r.body.getReader(); const first = await reader.read();"
        " await reader.cancel();"
        " return { status: r.status, type: r.headers.get('content-type'), first: new TextDecoder().decode(first.value) };"
    )
    result = _run_script(_config(_REFUSED), answer, script)
    assert result["out"] == {"status": 200, "type": "text/event-stream", "first": "data: one\n\n"}
    # Cancelling the body tells the dashboard to stop the request.
    assert any(m["type"] == "relay.abort" for m in result["relays"])


@pytest.mark.asyncio
async def test_an_app_backend_cannot_set_a_cookie_through_the_fetch_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard relays a srcdoc frame's fetch with its own credentials, so a
    `Set-Cookie` the app's backend returns would land on the dashboard's origin
    (expiring its session). The fetch route drops every one."""
    from aiohttp import web

    async with _client(monkeypatch) as (client, registry):

        async def _sets_cookies(request: web.Request) -> web.Response:
            resp = web.json_response({"ok": True})
            resp.headers.add("Set-Cookie", "mc_token_1=; Max-Age=0; Path=/")
            resp.headers.add("Set-Cookie", "mc_refresh=; Max-Age=0; Path=/api/auth")
            resp.headers.add("Clear-Site-Data", '"cookies", "storage"')
            return resp

        monkeypatch.setattr(registry, "dispatch", _sets_cookies)
        cap = (await _mint(client))["capability"]
        resp = await client.get(
            f"{lp.FETCH_PREFIX}{APP}/status", headers={lp.CAPABILITY_HEADER: cap, "Origin": "null"}
        )
        assert resp.status == 200
        assert "Set-Cookie" not in resp.headers
        assert "Clear-Site-Data" not in resp.headers
        assert resp.headers["Access-Control-Allow-Origin"] == "null"


def test_a_url_loaded_frame_whose_import_is_refused_falls_back_to_the_relay() -> None:
    """The default path loads the frame by URL. Behind a proxy that still lets the
    document through (Firefox sends the cookie on the iframe navigation) but refuses
    the module import, the frame runs the module inline through the relay, and its
    fetches go through the relay too."""
    config = {k: v for k, v in _config(_REFUSED).items() if k != "srcdoc"}
    script = "const r = await ctx.fetch('kv'); return { status: r.status };"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    loc = (
        "globalThis.LOCATION = { pathname: '/app-frame/cards-app/linkPreviews/cards/f/t',"
        " href: 'https://gw.example/app-frame/cards-app/linkPreviews/cards/f/t' };\n"
    )
    driver = _DRIVER.split("let fetched = null;", 1)[0] + (
        "let out = null;\n"
        "if (posted.some((m) => m.type === 'ready')) { const ctx = globalThis.kirocrewCtx; "
        "try { out = await (async () => {"
        + script
        + "})(); } catch (e) { out = { error: e.name } } }\n"
        "clearInterval(pump);\n"
        "console.log(JSON.stringify({ out, types: posted.map((m) => m.type), "
        "relays: posted.filter((m) => m.type === 'relay').map((m) => m.path), inline: globalThis.inlineRuns }));\n"
    )
    prelude = (
        loc
        + f"globalThis.CONFIG = {json.dumps(config)};\n"
        + f"globalThis.RELAY_ANSWER = {_serving(_REGISTERS)};\n"
        + "globalThis.FETCH_PATH = '';\nglobalThis.BASE = '';\n"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", prelude + _HARNESS + lp._BOOTSTRAP + driver],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["types"][0] == "boot", "the URL document says boot as it parses"
    assert "ready" in result["types"]
    assert result["inline"] == [{"type": "module", "nonce": "N1"}]
    assert result["relays"] == [_REFUSED, f"/app-frame-fetch/{APP}/kv"]
    assert result["out"] == {"status": 200}


@pytest.mark.asyncio
async def test_a_cookie_bearing_relayed_fetch_is_signed_as_the_app_not_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relayed fetch carries the page's session cookie, so it can also read as an
    owner session. The backend must still see the app (app-token), never the owner:
    the proxy checks the app identity the fetch route sets before the session."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.apps import routes_proxy

    monkeypatch.setattr(
        lp,
        "_provider_entry",
        lambda name, pid=None: ("dist/lp.mjs", True, APP) if name == APP else None,
    )
    monkeypatch.setattr("kiro_crew.dashboard.server._embed_parent_port", lambda request, app: None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    seen: list[tuple[str, str]] = []

    async def proxy(request: web.Request) -> web.Response:
        seen.append(routes_proxy._proxy_principal(request))
        return web.json_response({"proxied": True})

    monkeypatch.setattr("kiro_crew.apps.routes.handle_app_api_proxy", proxy)

    @web.middleware
    async def cookie_session(request: web.Request, handler: Any) -> web.StreamResponse:
        # Every request carries the owner's session cookie, as a relayed one does.
        request["is_dashboard_user"] = True
        request["auth_from_query_token"] = False
        request["user"] = "owner"
        return await handler(request)

    app = web.Application(middlewares=[cookie_session])
    lp.register_app_frame_routes(app)
    async with TestClient(TestServer(app)) as client:
        cap = (await _mint(client))["capability"]
        resp = await client.get(
            f"{lp.FETCH_PREFIX}{APP}/items", headers={lp.CAPABILITY_HEADER: cap}
        )
        assert resp.status == 200
    assert seen == [(routes_proxy.PRINCIPAL_APP_TOKEN, "")]
