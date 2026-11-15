"""The memory app host facade: one audited door from ``memoryAdmin`` to listed owner routes.

``/api/memory/provider/host/<rest>`` dispatches to ``/api/<rest>`` only for a route in
``HOST_ROUTES``, only for the owner or an app holding ``permissions.memoryAdmin``, and the
grant it hands the inner route lives in request state, where no header can put it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import _shared_access
from kiro_crew.dashboard.handlers import memory_app_host as host
from kiro_crew.dashboard.handlers import memory_provider

pytestmark = pytest.mark.xdist_group("memory_provider")


_PLANTED = "sk-ant-api03-" + "a" * 90


_FILES = {"user/notes.md": f"token {_PLANTED}", "user/plain.md": "run the tests"}
_WRITES: list[str] = []
_STREAMED: list[bytes] = []


async def _steering_route(request: web.Request) -> web.Response:
    # Stands in for the owner's steering editor, which returns the file as written to
    # the owner; only the facade's answer to an app is redacted.
    key = request.match_info["key"]
    if request.method == "PUT":
        _WRITES.append(key)
        _STREAMED.append(b"".join([c async for c in request.content.iter_chunked(8192)]))
        return web.json_response({"ok": True})
    return web.json_response({"content": _FILES[key]})


async def _owner_route(request: web.Request) -> web.Response:
    refusal = await _shared_access.require_owner_dashboard_request(request, "memory.records")
    if refusal is not None:
        return refusal
    return web.json_response(
        {"grant": request.get(_shared_access.MEMORY_ADMIN_GRANT), "q": request.query.get("q")}
    )


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={})
    app.router.add_route("*", "/api/memory/provider/host/{tail:.+}", host.api_memory_app_host)
    app.router.add_get("/api/memory/records", _owner_route)
    app.router.add_get("/api/config", _owner_route)  # an owner route the facade does not list
    app.router.add_get("/api/members", _owner_route)  # the roster carries each Bot's last DM
    app.router.add_route("*", "/api/steering/{key:.+}", _steering_route)
    app.router.add_route("*", "/api/members/{slug}/rules", _member_doc_route)
    app.router.add_get("/api/members/{slug}/goals", _member_doc_route)
    app.router.add_put("/api/members/{slug}/goals/long-term", _member_doc_route)
    return app


_MEMBER_DOCS = {"held": f"token {_PLANTED}", "plain": "be kind"}


async def _member_doc_route(request: web.Request) -> web.Response:
    # Like the real rules and goals routes: GET names the member in the query only,
    # PUT in the body only.
    if request.method == "PUT":
        member = (await request.json())["member"]
        _WRITES.append(member)
        return web.json_response({"ok": True})
    member = request.query.get("member", "")
    if member not in _MEMBER_DOCS:
        return web.json_response({"code": "missing_member"}, status=400)
    return web.json_response({"text": _MEMBER_DOCS[member]})


class _Body:
    """A request payload stream that yields *data* once, as aiohttp's reader does."""

    def __init__(self, data: bytes) -> None:
        self._chunks = [data] if data else []

    async def readany(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def set_read_chunk_size(self, size: int) -> None:
        pass

    async def iter_chunked(self, size: int):
        # How ``read_bounded_json`` reads: the stream itself, never the cached copy.
        while self._chunks:
            yield self._chunks.pop(0)


def _request(
    app: web.Application,
    path: str,
    *,
    app_name: str = "memory",
    headers=None,
    method="GET",
    body: dict | None = None,
):
    prefix = "/api/memory/provider/host/"
    tail = path.split("?", 1)[0][len(prefix) :] if path.startswith(prefix) else ""
    request = make_mocked_request(
        method,
        path,
        app=app,
        headers=headers or {},
        match_info={"tail": tail},
        payload=_Body(json.dumps(body).encode() if body is not None else b""),
    )
    request["user"] = "owner"
    request["app"] = app_name
    return request


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(memory_provider, "_audit", lambda *a, **k: seen.append(a))
    return seen


@pytest.mark.asyncio
async def test_a_memory_admin_app_reaches_a_listed_route_with_the_grant(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    response = await host.api_memory_app_host(
        _request(_app(), "/api/memory/provider/host/memory/records?store=default")
    )
    assert response.status == 200
    assert isinstance(response, web.Response) and response.text is not None
    assert json.loads(response.text) == {"grant": "app:memory", "q": None}
    assert audits[-1][:3] == ("app:memory", "host", "allowed")


@pytest.mark.asyncio
async def test_a_query_value_with_escaped_separators_reaches_the_route_as_sent(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    for raw, value in (("R%26D", "R&D"), ("c%2B%2B", "c++"), ("a%3Db", "a=b")):
        response = await host.api_memory_app_host(
            _request(_app(), f"/api/memory/provider/host/memory/records?store=default&q={raw}")
        )
        assert isinstance(response, web.Response) and response.text is not None
        assert json.loads(response.text)["q"] == value


@pytest.mark.asyncio
async def test_an_app_without_memory_admin_is_refused(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: False)
    with pytest.raises(web.HTTPForbidden) as refused:
        await host.api_memory_app_host(_request(_app(), "/api/memory/provider/host/memory/records"))
    assert json.loads(refused.value.text or "{}")["code"] == "memory_admin_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["config", "memory/../config", "nowhere", "members"])
async def test_an_unlisted_route_is_not_found(monkeypatch, audits, tail):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    response = await host.api_memory_app_host(_request(_app(), f"/api/memory/provider/host/{tail}"))
    assert response.status == 404


@pytest.mark.asyncio
async def test_an_app_calling_the_owner_route_directly_is_still_refused(monkeypatch):
    # The grant is request state the facade sets; the route itself stays owner-only.
    request = _request(
        _app(), "/api/memory/records", headers={"X-Memory-Admin-Grant": "app:memory"}
    )
    response = await _owner_route(request)
    assert response.status in (401, 403)


def test_block_spans_tile_the_prompt_in_send_order():
    from kiro_crew.context_blocks import block_spans

    prompt = "intro\n[Memory tools]\nCall memory_recall.\n[CURRENT DATE] today\ntail"
    spans = block_spans(prompt)
    assert "".join(prompt[s:e] for _label, s, e in spans) == prompt
    assert [s for _label, s, _e in spans] == sorted(s for _label, s, _e in spans)
    assert "memory_tools" in [label for label, _s, _e in spans]


@pytest.mark.asyncio
async def test_an_app_reads_a_steering_file_through_the_facade_redacted(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    path = "/api/memory/provider/host/steering/user/notes.md"
    as_app = await host.api_memory_app_host(_request(_app(), path))
    assert isinstance(as_app, web.Response) and as_app.text is not None
    assert as_app.status == 200 and _PLANTED not in as_app.text


@pytest.mark.asyncio
async def test_an_app_cannot_save_back_a_document_it_only_saw_redacted(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    _WRITES.clear()
    held = await host.api_memory_app_host(
        _request(_app(), "/api/memory/provider/host/steering/user/notes.md", method="PUT")
    )
    assert held.status == 409
    assert isinstance(held, web.Response) and json.loads(held.text or "{}")["code"] == (
        "document_holds_secret"
    )
    _STREAMED.clear()
    clean = await host.api_memory_app_host(
        _request(
            _app(),
            "/api/memory/provider/host/steering/user/plain.md",
            method="PUT",
            body={"content": "run the tests"},
        )
    )
    assert clean.status == 200 and _WRITES == ["user/plain.md"]
    # A handler that reads the stream capped still gets the whole body.
    assert _STREAMED == [json.dumps({"content": "run the tests"}).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["members/m/rules"])
async def test_a_member_document_is_checked_for_the_member_the_body_names(
    monkeypatch, audits, tail
):
    # The PUT names its member in the body only; the check must read that member's
    # stored copy, not a GET that fails for want of a query and lets the write through.
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    _WRITES.clear()
    path = f"/api/memory/provider/host/{tail}"
    held = await host.api_memory_app_host(
        _request(_app(), path, method="PUT", body={"member": "held", "rules": "x"})
    )
    assert isinstance(held, web.Response) and json.loads(held.text or "{}")["code"] == (
        "document_holds_secret"
    )
    spoof = await host.api_memory_app_host(
        _request(_app(), path + "?member=plain", method="PUT", body={"member": "held"})
    )
    assert spoof.status == 409
    unread = await host.api_memory_app_host(
        _request(_app(), path, method="PUT", body={"member": "ghost"})
    )
    assert isinstance(unread, web.Response) and json.loads(unread.text or "{}")["code"] == (
        "document_unchecked"
    )
    clean = await host.api_memory_app_host(
        _request(_app(), path, method="PUT", body={"member": "plain", "rules": "x"})
    )
    assert clean.status == 200 and _WRITES == ["plain"]


@pytest.mark.asyncio
async def test_a_goal_rewrite_is_checked_against_the_stored_goal(monkeypatch, audits):
    # The goals read scrubs every goal, so the check reads the stored record itself.
    from kiro_crew import member_goals

    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    rec = {"goals": [{"id": "g1", "text": f"ship {_PLANTED}"}, {"id": "g2", "text": "ship it"}]}
    monkeypatch.setattr(member_goals, "read_goals", lambda slug, member: rec)
    _WRITES.clear()

    async def put(goal_id):
        body = {"member": "plain", "long_term": "ship it now", "goal_id": goal_id}
        return await host.api_memory_app_host(
            _request(
                _app(),
                "/api/memory/provider/host/members/m/goals/long-term",
                method="PUT",
                body=body,
            )
        )

    held = await put("g1")
    assert isinstance(held, web.Response) and json.loads(held.text or "{}")["code"] == (
        "document_holds_secret"
    )
    assert (await put("g2")).status == 200
    assert (await put(None)).status == 200
    assert _WRITES == ["plain", "plain"]


@pytest.mark.asyncio
async def test_an_app_cannot_preview_a_text_replace_over_a_stored_credential(
    tmp_path, monkeypatch, audits
):
    # Replacing the token's prefix would show the rest of it in the preview's ``after``,
    # which the redaction cannot recognise without the prefix.
    from kiro_crew import memory_edit
    from kiro_crew.vector_memory import VectorMemoryStore

    store = VectorMemoryStore(db_path=tmp_path / "memory.db")
    store.init()
    try:
        assert store.set_semantic("user.tok", f"token {_PLANTED}", 1.0, "user_explicit") is None
        assert store.set_semantic("user.lang", "speaks Chinese", 1.0, "user_explicit") is None

        async def bulk_preview(request: web.Request) -> web.Response:
            return web.json_response(
                memory_edit.preview_edit(store, "default", b"k", await request.json())
            )

        async def tier_for(state, name):
            return store

        monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
        monkeypatch.setattr(host, "vector_memory_for_store", tier_for)
        app = _app()
        app.router.add_post("/api/memory/bulk/preview", bulk_preview)

        async def preview(find: str, replacement: str) -> web.Response:
            body = {
                "selection": {"query": {}},
                "operation": {"type": "replace_text", "find": find, "replacement": replacement},
            }
            response = await host.api_memory_app_host(
                _request(
                    app, "/api/memory/provider/host/memory/bulk/preview", method="POST", body=body
                )
            )
            assert isinstance(response, web.Response)
            return response

        held = await preview("sk-ant-", "sk-bnt-")
        assert held.status == 409 and "a" * 36 not in (held.text or "")
        assert json.loads(held.text or "{}")["code"] == "document_holds_secret"
        clean = await preview("Chinese", "Mandarin")
        assert clean.status == 200
        assert json.loads(clean.text or "{}")["changed_count"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_owner_reaches_a_listed_route_raw_and_without_the_grant(monkeypatch):
    async def owner(request, operation, **_kwargs):
        return "owner"

    monkeypatch.setattr(memory_provider, "_caller", owner)
    raw = await host.api_memory_app_host(
        _request(_app(), "/api/memory/provider/host/steering/user/notes.md")
    )
    assert isinstance(raw, web.Response) and raw.text is not None
    # The owner's own editor shows the file as written; only an app's copy is redacted.
    assert raw.status == 200 and _PLANTED in raw.text


async def _probe_route(request: web.Request) -> web.Response:
    # Each key answers the facade's pre-write GET a different way; a PUT always saves.
    key = request.match_info["key"]
    if request.method == "PUT":
        _WRITES.append(key)
        return web.json_response({"ok": True})
    if key == "raises-404":
        raise web.HTTPNotFound()
    if key == "raises-403":
        raise web.HTTPForbidden()
    if key == "answers-404":
        return web.json_response({"code": "missing"}, status=404)
    return web.Response(body=b"not json", content_type="application/json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "status", "code"),
    [
        ("raises-404", 200, None),
        ("answers-404", 200, None),
        ("raises-403", 409, "document_unchecked"),
        ("not-json", 409, "document_unchecked"),
    ],
)
async def test_a_put_is_saved_only_when_its_stored_copy_is_absent_or_checked(
    monkeypatch, audits, key, status, code
):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={})
    app.router.add_route("*", "/api/memory/provider/host/{tail:.+}", host.api_memory_app_host)
    app.router.add_route("*", "/api/steering/{key:.+}", _probe_route)
    _WRITES.clear()

    response = await host.api_memory_app_host(
        _request(app, f"/api/memory/provider/host/steering/{key}", method="PUT", body={})
    )

    assert isinstance(response, web.Response) and response.status == status
    if code is None:
        assert _WRITES == [key]
    else:
        assert json.loads(response.text or "{}")["code"] == code and _WRITES == []


@pytest.mark.asyncio
async def test_a_put_whose_route_has_no_read_is_refused_unchecked(monkeypatch, audits):
    # SOUL's PUT is listed, but this app serves no GET for it: nothing can be checked.
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    app = _app()
    app.router.add_put("/api/members/{slug}/soul", _probe_route)
    _WRITES.clear()

    response = await host.api_memory_app_host(
        _request(app, "/api/memory/provider/host/members/m/soul", method="PUT", body={"text": "x"})
    )

    assert isinstance(response, web.Response) and response.status == 409
    assert json.loads(response.text or "{}")["code"] == "document_unchecked" and _WRITES == []


@pytest.mark.asyncio
async def test_a_member_put_with_a_body_that_is_not_json_is_refused_unchecked(monkeypatch, audits):
    monkeypatch.setattr(memory_provider, "_app_holds_memory_admin", lambda app: True)
    _WRITES.clear()
    request = make_mocked_request(
        "PUT",
        "/api/memory/provider/host/members/m/rules",
        app=_app(),
        match_info={"tail": "members/m/rules"},
        payload=_Body(b"{not json"),
    )
    request["user"] = "owner"
    request["app"] = "memory"

    response = await host.api_memory_app_host(request)

    # No member to read, so the stored copy answers 400 and the write is not trusted.
    assert isinstance(response, web.Response) and response.status == 409
    assert json.loads(response.text or "{}")["code"] == "document_unchecked" and _WRITES == []


@pytest.mark.asyncio
async def test_a_goal_rewrite_that_cannot_be_checked_is_refused(monkeypatch):
    from yarl import URL

    from kiro_crew import member_goals

    url = URL("/api/members/m/goals/long-term")
    no_member = await host._refuse_masked_goal(url, {"goal_id": "g1"})
    assert no_member is not None and json.loads(no_member.text or "{}")["code"] == (
        "document_unchecked"
    )

    def unreadable(slug, member):
        raise member_goals.GoalsUnreadable("corrupt")

    monkeypatch.setattr(member_goals, "read_goals", unreadable)
    refused = await host._refuse_masked_goal(url, {"goal_id": "g1", "member": "plain"})
    assert refused is not None and refused.status == 409
    assert await host._refuse_masked_goal(url, ["not", "a", "dict"]) is None


def _token(payload: object) -> str:
    import base64

    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{data}.signature"


def test_a_preview_answer_yields_its_signed_payload_only_when_well_formed():
    payload = {"operation": {"type": "replace_text"}, "store": "", "selection": {}}
    assert host._preview_payload(web.json_response({"preview_id": _token(payload)})) == payload
    assert host._preview_payload(web.json_response({"error": "x"}, status=400)) is None
    assert host._preview_payload(web.json_response({"changed_count": 1})) is None
    assert host._preview_payload(web.json_response({"preview_id": "%%%.sig"})) is None
    assert host._preview_payload(web.json_response({"preview_id": _token([1, 2])})) is None
    assert host._preview_payload(web.StreamResponse()) is None


@pytest.mark.asyncio
async def test_a_text_replace_preview_that_cannot_be_checked_is_refused(monkeypatch):
    import threading

    from kiro_crew import memory_edit

    replace = web.json_response(
        {
            "preview_id": _token(
                {"operation": {"type": "replace_text"}, "store": "s", "selection": {}}
            )
        }
    )
    forget = web.json_response({"preview_id": _token({"operation": {"type": "forget"}})})
    tiers: list = [None]

    async def tier_for(state, name):
        return tiers[0]

    monkeypatch.setattr(host, "vector_memory_for_store", tier_for)
    assert await host._refuse_secret_rewrite(SimpleNamespace(), forget) is None
    no_tier = await host._refuse_secret_rewrite(SimpleNamespace(), replace)
    assert no_tier is not None and json.loads(no_tier.text or "{}")["code"] == (
        "document_unchecked"
    )

    def refuse(*_args):
        raise memory_edit.MemoryEditError("bad selection")

    tiers[0] = SimpleNamespace(_db_lock=threading.Lock())
    monkeypatch.setattr(memory_edit, "_collect", refuse)
    # The rows the edit changes cannot be listed, so none of them can be checked.
    failed = await host._refuse_secret_rewrite(SimpleNamespace(), replace)
    assert failed is not None and failed.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [f"token {_PLANTED}", "plain text"])
async def test_an_app_set_over_a_row_holding_a_credential_is_refused(monkeypatch, stored):
    import threading

    from kiro_crew import memory_edit

    set_text = web.json_response(
        {"preview_id": _token({"operation": {"type": "set"}, "store": "s", "selection": {}})}
    )

    async def tier_for(state, name):
        return SimpleNamespace(_db_lock=threading.Lock())

    monkeypatch.setattr(host, "vector_memory_for_store", tier_for)
    # The app saw the row masked and sends the masked copy back with its edit.
    monkeypatch.setattr(memory_edit, "_collect", lambda *_a: ("", [], [(stored, "edited")]))
    refused = await host._refuse_secret_rewrite(SimpleNamespace(), set_text)
    if stored == "plain text":
        assert refused is None
    else:
        assert refused is not None and json.loads(refused.text or "{}")["code"] == (
            "document_holds_secret"
        )


def test_only_a_json_answer_is_redacted_for_an_app():
    text = web.Response(text=f"token {_PLANTED}", content_type="text/plain")
    assert host._redacted_for_app(text) is text and _PLANTED in (text.text or "")
    broken = web.Response(body=b"{oops " + _PLANTED.encode(), content_type="application/json")
    assert host._redacted_for_app(broken).body == b"{oops " + _PLANTED.encode()
    stream = web.StreamResponse()
    assert host._redacted_for_app(stream) is stream
    redacted = host._redacted_for_app(web.json_response({"t": _PLANTED}))
    assert isinstance(redacted, web.Response) and _PLANTED.encode() not in bytes(redacted.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [True, False])
async def test_a_goals_read_through_the_facade_is_the_record_its_writes_edit(monkeypatch, granted):
    from kiro_crew.dashboard.handlers import member_goals as goals_handlers
    from kiro_crew.dashboard.handlers import members

    record = {"goals": [{"id": "g1", "text": "ship it"}]}

    async def owner_member(request, operation, member, *, write):
        return "m", "M", SimpleNamespace()

    async def view(request, slug, rec):
        return rec

    async def trimmed(request):
        return web.json_response({"trimmed": True})

    monkeypatch.setattr(goals_handlers, "_owner_member", owner_member)
    monkeypatch.setattr(goals_handlers, "_view", view)
    monkeypatch.setattr(goals_handlers, "_read_work_spend", lambda cfg, slug, member: None)
    monkeypatch.setattr(goals_handlers.member_goals, "read_goals", lambda slug, member: record)
    monkeypatch.setattr(members, "app_bot_goals", trimmed)
    request = make_mocked_request("GET", "/api/members/m/goals?member=M", match_info={"slug": "m"})
    request["app"] = "memory-app"
    if granted:
        request[_shared_access.MEMORY_ADMIN_GRANT] = "app:memory-app"
    body = json.loads((await goals_handlers.api_member_goals_get(request)).text or "{}")
    assert body == (
        {"slug": "m", "member": "M", **record, "spend": None} if granted else {"trimmed": True}
    )
