"""``contributes.messageActions``: one message's text, on one click, in a sandboxed frame.

Asserted here rather than assumed:

* the manifest refuses a wrong type instead of coercing it, and caps every bound;
* what an action may see (its roles, markdown) is a grant: an update that adds the
  ``user`` role or switches to markdown waits for the owner, and the action keeps
  working at its approved reach meanwhile;
* the frame and its fetch are bound to one action: a token minted for one action
  opens neither another action's frame nor the app's link-preview frame, and a
  revoked action's capability stops reaching the app;
* the frame runtime numbers each click and settles every offer on the host's answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import app_frame as af
from kiro_crew.apps import message_action_grants as mag
from kiro_crew.apps import point_grants as lpg
from kiro_crew.apps.manager_records import (
    STAGED_GRANT_FAMILIES,
    _approved_grants_after_manifest_change,
    _declared_grant_entries,
    _held_back_entries,
)
from kiro_crew.apps.manifest import CORE_APP_ROUTE_SEGMENTS, AppManifest
from kiro_crew.apps.manifest_contributes import (
    MAX_MESSAGE_ACTION_CHARS,
    Contributes,
    MessageActionConfig,
)

APP = "notes-app"


def _action(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "save", "title": "Save to notes", "entry": "dist/frame.mjs"}
    base.update(over)
    return base


def _manifest(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"name": APP, "contributes": {"messageActions": list(actions or [_action()])}}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_defaults_are_the_narrowest_reach() -> None:
    cfg = MessageActionConfig.from_dict(_action())
    assert cfg.validate() == []
    d = cfg.to_dict()
    assert d["roles"] == ["assistant"]
    assert d["format"] == "plain"
    assert d["maxChars"] == MAX_MESSAGE_ACTION_CHARS
    assert d["popover"] == {"width": 360, "maxHeight": 480}


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"roles": "assistant"}, "roles has the wrong type"),
        ({"roles": []}, "roles must list"),
        ({"roles": ["system"]}, "unknown role"),
        ({"roles": ["user", "user"]}, "repeats"),
        ({"format": "html"}, "format must be"),
        ({"format": 1}, "format has the wrong type"),
        ({"maxChars": True}, "maxChars has the wrong type"),
        ({"maxChars": 1.5}, "maxChars has the wrong type"),
        ({"maxChars": 0}, "maxChars must be"),
        ({"maxChars": MAX_MESSAGE_ACTION_CHARS + 1}, "maxChars must be"),
        ({"popover": {"width": 100}}, "popover.width must be"),
        ({"popover": {"maxHeight": 601}}, "popover.maxHeight must be"),
        ({"popover": {"height": 300}}, "popover has the wrong type"),
        ({"popover": {"width": "300"}}, "popover.width has the wrong type"),
        ({"title": "x" * 41}, "title exceeds 40"),
        ({"title": ""}, "missing title"),
        ({"entry": ""}, "missing entry"),
        ({"id": "Bad Id"}, "storage-safe slug"),
        ({"autoSend": True}, "unknown key"),
        ({"icon": "bookmark"}, "unknown key"),
    ],
)
def test_a_bad_field_is_refused_not_coerced(over: dict[str, Any], needle: str) -> None:
    errors = MessageActionConfig.from_dict(_action(**over)).validate()
    assert any(needle in e for e in errors), errors


def test_the_title_is_measured_in_utf16_units() -> None:
    # 20 astral characters are 40 UTF-16 units: at the cap, not over it.
    assert MessageActionConfig.from_dict(_action(title="\U0001f4dd" * 20)).validate() == []
    errors = MessageActionConfig.from_dict(_action(title="\U0001f4dd" * 21)).validate()
    assert any("title exceeds" in e for e in errors)


def test_the_list_is_capped_and_ids_are_unique() -> None:
    c = Contributes.from_dict(
        {"messageActions": [_action(id="a"), _action(id="b"), _action(id="c")]}
    )
    assert any("exceeds the limit of 2" in e for e in c.validate())
    c = Contributes.from_dict({"messageActions": [_action(), _action()]})
    assert any("duplicate id 'save'" in e for e in c.validate())
    c = Contributes.from_dict({"messageActions": {"id": "save"}})
    assert any("must be an array" in e for e in c.validate())
    c = Contributes.from_dict({"messageActions": ["save"]})
    assert any("must be an object" in e for e in c.validate())


def test_the_entry_cannot_leave_the_app_and_the_action_is_signed() -> None:
    m = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", **_manifest(_action(entry="../../x.mjs"))}
    )
    assert any("contributes.messageActions entry" in e for e in m.validate())
    signed = AppManifest.from_dict({"name": APP, "version": "1.0.0", **_manifest()})
    widened = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", **_manifest(_action(roles=["assistant", "user"]))}
    )
    assert b"frame.mjs" in signed.signing_payload()
    assert signed.signing_payload() != widened.signing_payload()


def test_the_point_is_registered_and_its_mint_segment_reserved() -> None:
    assert "messageActions" in af.registered_frame_points()
    assert "frames" in CORE_APP_ROUTE_SEGMENTS


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def test_the_descriptor_is_roles_and_format() -> None:
    src = lpg.grant_source(_manifest(_action(roles=["assistant", "user"], format="markdown")))
    assert src is not None
    assert src["messageActions"] == [
        "save:role:assistant",
        "save:role:user",
        "save:format:markdown",
    ]
    assert "messageActions" in STAGED_GRANT_FAMILIES
    assert lpg.grant_source(_manifest())["messageActions"] == ["save:role:assistant"]  # type: ignore[index]


def _update(old: dict[str, Any], new: dict[str, Any]) -> tuple[dict[str, list[str]], Any]:
    o, n = lpg.grant_source(old), lpg.grant_source(new)
    approved = _approved_grants_after_manifest_change(
        existing_approved=_declared_grant_entries(o), old_permissions=o, new_permissions=n
    )
    return approved, n


def test_adding_the_user_role_or_markdown_is_held_back() -> None:
    approved, new = _update(
        _manifest(), _manifest(_action(roles=["assistant", "user"], format="markdown"))
    )
    assert approved["messageActions"] == ["save:role:assistant"]
    assert _held_back_entries(approved, new)["messageActions"] == [
        "save:role:user",
        "save:format:markdown",
    ]


def test_a_new_action_is_held_back_and_a_narrowing_needs_nothing() -> None:
    approved, new = _update(_manifest(), _manifest(_action(), _action(id="clip", title="Clip")))
    assert _held_back_entries(approved, new)["messageActions"] == ["clip:role:assistant"]
    approved, new = _update(
        _manifest(_action(roles=["assistant", "user"], format="markdown")), _manifest()
    )
    assert not any(_held_back_entries(approved, new).values())


def _staged(approved: list[str]) -> Any:
    def staged(name: str, family: str, read: Any) -> list[str]:
        return [e for e in read() if e in approved]

    return staged


def test_the_row_keeps_an_action_at_its_approved_reach(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.apps.manager_records.staged_app_grants", _staged(["save:role:assistant"])
    )
    m = _manifest(_action(roles=["assistant", "user"], format="markdown", maxChars=500))
    row = mag.message_action_grant_row(APP, m)["messageActionGrant"]
    assert row["actions"] == [
        {
            "id": "save",
            "title": "Save to notes",
            "roles": ["assistant"],
            "format": "plain",
            "maxChars": 500,
            "popover": {"width": 360, "maxHeight": 480},
        }
    ]
    assert row["staged"] == ["save:role:user", "save:format:markdown"]
    # Nothing approved: the action is not offered at all.
    monkeypatch.setattr("kiro_crew.apps.manager_records.staged_app_grants", _staged([]))
    assert mag.message_action_grant_row(APP, m)["messageActionGrant"]["actions"] == []
    assert mag.approved_message_action(APP, m, "save") is None
    assert mag.message_action_grant_row(APP, {"contributes": {}}) == {}


# ---------------------------------------------------------------------------
# Frame and fetch
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def dispatch(self, request: web.Request) -> web.Response:
        self.calls.append((request.match_info["app_name"], request.match_info["path"]))
        return web.json_response({"ok": True})


def _admit_all(name: str, point: str, pid: str, query: Mapping[str, str]) -> bool:
    return True


@asynccontextmanager
async def _client(
    monkeypatch: pytest.MonkeyPatch,
    granted: set[str],
    admits: Callable[[str, str, str, Mapping[str, str]], bool] = _admit_all,
) -> AsyncIterator[tuple[TestClient, _Registry]]:
    registry = _Registry()

    def entry(name: str, point: str, pid: str) -> tuple[str, bool, str] | None:
        key = f"{point}/{pid}"
        return ("dist/frame.mjs", False, APP) if name == APP and key in granted else None

    monkeypatch.setattr(af, "_point_entry", entry)
    monkeypatch.setattr(af, "_point_admits", admits)
    monkeypatch.setattr("kiro_crew.apps.hooks_integration.get_route_registry", lambda: registry)
    monkeypatch.setattr("kiro_crew.dashboard.server._embed_parent_port", lambda request, app: None)

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
        yield client, registry
    finally:
        await client.close()


async def _mint(client: TestClient, action: str = "save") -> dict[str, Any]:
    resp = await client.post(f"/api/apps/{APP}/frames/messageActions/{action}")
    assert resp.status == 200, await resp.text()
    data: dict[str, Any] = await resp.json()
    return data


@pytest.mark.asyncio
async def test_the_action_frame_is_the_shared_sandbox_with_its_own_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(monkeypatch, {"messageActions/save"}) as (client, _):
        minted = await _mint(client)
        assert minted["frame"].startswith(f"{af.FRAME_PREFIX}{APP}/messageActions/save/")
        resp = await client.get(minted["frame"])
        assert resp.status == 200
        assert "sandbox allow-scripts" in resp.headers["Content-Security-Policy"]
        assert "allow-same-origin" not in resp.headers["Content-Security-Policy"]
        html = await resp.text()
        assert "m.type !== 'message.invoke'" in html
        assert "case 'show'" not in html


@pytest.mark.asyncio
async def test_a_token_opens_only_its_own_point(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(
        monkeypatch, {"messageActions/save", "messageActions/clip", "linkPreviews/save"}
    ) as (client, _):
        minted = await _mint(client)
        frame_id, token = minted["frame"].rsplit("/", 2)[1:]
        for other_entry in ("messageActions/clip", "linkPreviews/save"):
            other = f"{af.FRAME_PREFIX}{APP}/{other_entry}/{frame_id}/{token}"
            assert (await client.get(other)).status == 404, other_entry
        fid, point, pid, signed = minted["capability"].split("~")
        assert (point, pid) == ("messageActions", "save")
        assert not af._capability_ok(f"{fid}~messageActions~clip~{signed}", APP, "127.0.0.1")
        assert not af._capability_ok(f"{fid}~linkPreviews~save~{signed}", APP, "127.0.0.1")
        assert af._capability_ok(minted["capability"], APP, "127.0.0.1")


@pytest.mark.asyncio
async def test_an_ungranted_action_gets_no_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _client(monkeypatch, set()) as (client, _):
        resp = await client.post(f"/api/apps/{APP}/frames/messageActions/save")
        assert resp.status == 404
        assert (await resp.json())["code"] == "no_app_frame"
        resp = await client.post(f"/api/apps/{APP}/frames/messageActions/Not%20A%20Slug")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_a_mint_outside_the_live_grant_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    def admits(name: str, point: str, pid: str, query: Mapping[str, str]) -> bool:
        seen.append(dict(query))
        return query.get("role") == "assistant"

    async with _client(monkeypatch, {"messageActions/save"}, admits) as (client, _):
        base = f"/api/apps/{APP}/frames/messageActions/save"
        ok = await client.post(f"{base}?role=assistant&format=plain")
        assert ok.status == 200
        refused = await client.post(f"{base}?role=user&format=plain&frame=abcdefghijkl")
        assert refused.status == 403
        assert (await refused.json())["code"] == "not_granted"
    # The frame id a renewal names belongs to the route, not to the request checked.
    assert seen == [{"role": "assistant", "format": "plain"}, {"role": "user", "format": "plain"}]


def test_admission_follows_the_live_approved_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    rows: list[dict[str, Any] | None] = [{"id": "save", "roles": ["assistant"], "format": "plain"}]
    monkeypatch.setattr(af, "get_app_manifest", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(af, "approved_message_action", lambda name, manifest, aid: rows[-1])
    admit = af._admit_message_action
    assert admit(APP, "save", {"role": "assistant", "format": "plain"})
    # A role the owner took back, or markdown after a downgrade to plain: refused.
    assert not admit(APP, "save", {"role": "user", "format": "plain"})
    assert not admit(APP, "save", {"role": "assistant", "format": "markdown"})
    # A missing or unknown value is refused, never defaulted.
    assert not admit(APP, "save", {"role": "assistant"})
    assert not admit(APP, "save", {"format": "plain"})
    assert not admit(APP, "save", {"role": "system", "format": "plain"})
    rows.append({"id": "save", "roles": ["assistant", "user"], "format": "markdown"})
    assert admit(APP, "save", {"role": "user", "format": "markdown"})
    assert admit(APP, "save", {"role": "user", "format": "plain"})
    rows.append(None)
    assert not admit(APP, "save", {"role": "assistant", "format": "plain"})
    assert af._POINTS["messageActions"].admit is af._admit_message_action
    assert af._POINTS["linkPreviews"].admit is None


@pytest.mark.asyncio
async def test_revoking_the_action_stops_its_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    granted = {"messageActions/save"}
    async with _client(monkeypatch, granted) as (client, registry):
        cap = (await _mint(client))["capability"]
        url = f"{af.FETCH_PREFIX}{APP}/notes"
        assert (await client.post(url, headers={af.CAPABILITY_HEADER: cap})).status == 200
        assert registry.calls == [(APP, "notes")]
        granted.clear()
        assert (await client.post(url, headers={af.CAPABILITY_HEADER: cap})).status == 401
        assert registry.calls == [(APP, "notes")]


def test_the_point_entry_needs_an_approved_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        name=APP,
        contributes=SimpleNamespace(
            linkPreviews=[], messageActions=[SimpleNamespace(id="save", entry="dist/frame.mjs")]
        ),
        backend=SimpleNamespace(entryPoint=""),
        mcpServers={},
    )
    approved: dict[str, Any] = {"save": {"id": "save"}}
    monkeypatch.setattr(af, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(af, "get_app_manifest", lambda name: manifest)
    monkeypatch.setattr(af, "approved_message_action", lambda n, m, aid: approved.get(aid))
    assert af._point_entry(APP, "messageActions", "save") == ("dist/frame.mjs", False, APP)
    assert af._point_entry(APP, "messageActions", "other") is None
    assert af._point_entry(APP, "messageActions", "../x") is None
    approved.clear()
    assert af._point_entry(APP, "messageActions", "save") is None
    manifest.name = "other-app"
    approved["save"] = {"id": "save"}
    assert af._point_entry(APP, "messageActions", "save") is None


#: Runs the real message-action runtime under node with a stub window.
_HARNESS = r"""
const posted = [];
const listeners = {};
const parent = { postMessage: (m) => posted.push(m) };
globalThis.window = { parent, addEventListener: (t, f) => { (listeners[t] ||= []).push(f); } };
globalThis.location = { pathname: '/app-frame/a/messageActions/save/f/t', href: 'http://gw/app-frame/a/messageActions/save/f/t' };
const actionModule = 'export function activate(ctx) { return { invoke(message, call) { globalThis.calls.push([message, call]); }, cancel(id, why) { globalThis.cancels.push([id, why]); } }; }';
globalThis.calls = [];
globalThis.cancels = [];
globalThis.document = {
  activeElement: null,
  body: {},
  getElementById: () => ({ textContent: JSON.stringify({
    app: 'a', entry: 'data:text/javascript,' + encodeURIComponent(actionModule),
    fetchBase: '/app-frame-fetch/a/', theme: 'light',
  }) }),
};
"""
_DRIVER = r"""
const deliver = (data) => (listeners.message || []).forEach((f) => f({ source: parent, data: { 'kirocrew-app-frame': 1, ...data } }));
deliver({ type: 'init', capability: 'c' });
for (let i = 0; i < 50 && !posted.some((m) => m.type === 'ready'); i++) await new Promise((r) => setTimeout(r, 10));
deliver({ type: 'message.invoke', invocationId: 'inv-1', text: 'hello', format: 'html', truncated: 'yes', role: 'system' });
deliver({ type: 'message.invoke', text: 'no id' });
const [message, controls] = calls[0];
const first = controls.offerCopy('note link');
const second = controls.offerOpen('https://notes.example.com/1');
const offers = posted.filter((m) => m.type === 'result.offer');
deliver({ type: 'result.ack', invocationId: 'inv-1', seq: offers[0].seq });
deliver({ type: 'result.error', invocationId: 'inv-1', seq: offers[1].seq, code: 'stale' });
deliver({ type: 'invoke.cancel', invocationId: 'inv-1', reason: 'closed' });
console.log(JSON.stringify({
  calls: calls.length, message, frozen: Object.isFrozen(controls),
  offers: offers.map((m) => [m.invocationId, m.kind, m.value]),
  results: [await first, await second], cancels,
}));
"""


def test_the_runtime_numbers_each_click_and_settles_each_offer() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            _HARNESS + af._RUNTIME + af._INVOCATION + af._MESSAGE_ACTION_POINT + _DRIVER,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    # An invoke without the host's id is not delivered: nothing could answer it.
    assert result["calls"] == 1
    # Out-of-contract values are read as the narrowest ones.
    assert result["message"] == {
        "text": "hello",
        "format": "plain",
        "truncated": False,
        "role": "assistant",
    }
    assert result["frozen"] is True
    assert result["offers"] == [
        ["inv-1", "copy", "note link"],
        ["inv-1", "open", "https://notes.example.com/1"],
    ]
    assert result["results"] == [{"ok": True}, {"ok": False, "code": "stale"}]
    assert result["cancels"] == [["inv-1", "closed"]]
