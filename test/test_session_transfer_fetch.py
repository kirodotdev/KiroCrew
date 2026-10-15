"""``fetch-session``: the hub pulls a crew's session back and installs it as a new local slot.

Pins the pull counterpart of ``send-session``: the hub reads the crew's export over the
peer proxy, names the sender from its own registry rather than the crew's bytes, refuses
a slot key that could leave the export path, maps only the export's own 404 to a missing
session, relays nothing of a crew's refusal but its status, and is owner-only. The last
section pins the premise the route rests on: the credential the hub mints reaches the
crew's export as that crew's owner. How the installed bundle lands is pinned in
``test_session_transfer_landing.py``.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from aiohttp import web
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _async_value,
    _isolate_import_tmp,
    _make_request,
    _OwnerReq,
    _stub_state,
    _valid,
)


def _filed_folder(state):
    """The folder the fetched slot was filed into, as a name; "" when unfiled."""
    slot = state._imported_slot
    fid = getattr(slot, "folder_id", "")
    if not fid:
        return ""
    return next((str(f["name"]) for f in state._folders if str(f["id"]) == fid), "<dangling>")


# ── fetch-session: the pull counterpart of send-session ──────────────────


class _PeerExport:
    """The live peer response ``proxy_request`` yields: a status and a stream."""

    def __init__(self, status: int, data: bytes = b"") -> None:
        self.status = status
        self.content = _make_request(None, None, gz=data).content


def _fetch_state(st, monkeypatch, upstream=None, *, error=None):
    """A stubbed hub state whose manager records each peer request."""
    from kiro_crew.dashboard import handlers_instances as hi

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )
    state = _stub_state(st, monkeypatch)
    calls: list = []

    class _Mgr:
        @contextlib.asynccontextmanager
        async def proxy_request(self, instance_id, method, path, *, params=None, **_kw):
            calls.append((instance_id, method, path, dict(params or {})))
            if error is not None:
                raise error
            yield upstream

    state.instances_manager = _Mgr()
    state.instances_registry = SimpleNamespace(
        get=lambda _i: SimpleNamespace(id="crew", name="crew-a")
    )
    state.owner_id = "owner"
    return state, calls


def _fetch_request(state, slot):
    return _OwnerReq(
        app={"state": state},
        match_info={"id": "crew"},
        headers={},
        json=_async_value({"slot": slot}),
    )


@pytest.mark.asyncio
async def test_fetch_session_installs_the_peers_export_as_a_new_local_slot(monkeypatch):
    """The hub asks the peer's OWN export route, with Layer B requested, and the
    reply arrives through the one local arrival path as a new slot."""
    import gzip

    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    # A crew's real export stamps no sender (``session_export`` leaves ``origin``
    # empty), so the fixture does too: the sender must come from the hub.
    export = gzip.compress(json.dumps(_valid(origin="")).encode())
    state, calls = _fetch_state(st, monkeypatch, _PeerExport(200, export))

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 200, resp.body
    assert calls == [
        ("crew", "GET", "api/chat/slots/chat-1-17/export", {"include_layer_b": "true"})
    ]
    assert list(state._slots) == [state._imported_slot.key]
    # Filed like a push from that crew, named from THIS hub's registry entry.
    assert _filed_folder(state) == "from crew-a"
    assert "(from crew-a)" in state._imported_slot.title


@pytest.mark.asyncio
async def test_fetch_session_names_the_sender_from_the_hub_not_the_crews_bytes(monkeypatch):
    """A crew that does put an ``origin`` in its export does not get to choose
    where its copy is filed or what the title claims: the hub's registry name wins."""
    import gzip

    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    export = gzip.compress(json.dumps(_valid(origin="someone-else")).encode())
    state, _calls = _fetch_state(st, monkeypatch, _PeerExport(200, export))

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 200, resp.body
    assert _filed_folder(state) == "from crew-a"
    assert "someone-else" not in state._imported_slot.title


@pytest.mark.asyncio
async def test_fetch_session_refuses_a_slot_key_that_could_leave_the_export_path(monkeypatch):
    """The key becomes part of a peer path, so a traversal never reaches the peer."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    state, calls = _fetch_state(st, monkeypatch, _PeerExport(200))

    for bad in ("../status", "chat/1", "", "a" * 250, "%2e%2e", ".", ".."):
        resp = await hi.api_instances_fetch_session(_fetch_request(state, bad))
        assert resp.status == 400, bad
        assert json.loads(resp.body)["code"] == "transfer_missing_slot"
    assert calls == []


@pytest.mark.asyncio
async def test_fetch_session_reaches_the_peer_for_every_key_the_peer_keeps(monkeypatch):
    """A key ``_normalize_slot_key`` keeps is fetchable: a leading ``_`` or ``.``
    and the full 249-character length included."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.handlers_instances_peer import _FETCH_SLOT_KEY_MAX_CHARS
    from kiro_crew.dashboard.state_parts.slot_directory import (
        _SLOT_KEY_MAX_CHARS,
        _normalize_slot_key,
    )

    assert _FETCH_SLOT_KEY_MAX_CHARS == _SLOT_KEY_MAX_CHARS
    keys = ["_notes", ".hidden", "a" * 201, "b" * _SLOT_KEY_MAX_CHARS, "slack:C1.17"]
    for key in keys[:-1]:
        assert _normalize_slot_key(key) == key
    state, calls = _fetch_state(st, monkeypatch, _PeerExport(404, b"404: Not Found"))
    for key in keys:
        await hi.api_instances_fetch_session(_fetch_request(state, key))
    assert [c[2] for c in calls] == [f"api/chat/slots/{quote(k, safe='')}/export" for k in keys]


@pytest.mark.asyncio
async def test_fetch_session_maps_a_missing_peer_session_to_404(monkeypatch):
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    state, _calls = _fetch_state(
        st,
        monkeypatch,
        _PeerExport(404, b'{"error": "session not found", "code": "export_slot_not_found"}'),
    )

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 404
    assert json.loads(resp.body)["code"] == "transfer_slot_not_found"
    assert state._slots == {}


@pytest.mark.asyncio
async def test_fetch_session_does_not_read_a_crew_without_the_route_as_a_missing_session(
    monkeypatch,
):
    """A crew that has no export route answers aiohttp's bare 404. That is not
    "the session is gone", so it is a peer refusal, not ``transfer_slot_not_found``."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    state, _calls = _fetch_state(st, monkeypatch, _PeerExport(404, b"404: Not Found"))

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 502
    body = json.loads(resp.body)
    assert body == {
        "error": "the crew refused the export (HTTP 404)",
        "code": "transfer_peer_refused",
        "peer_status": 404,
    }


@pytest.mark.asyncio
async def test_fetch_session_relays_the_status_of_an_export_refusal_and_nothing_else(monkeypatch):
    """The owner can tell "never exportable" (400) from "try again" (503) from
    "failed" (500) by ``peer_status`` alone; the crew's code and words stay put."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    for status, code in (
        (400, "export_slot_not_persistent"),
        (400, "export_bundle_empty"),
        (503, "export_snapshot_unstable"),
        (500, "export_failed"),
    ):
        refusal = json.dumps({"error": "peer words", "code": code}).encode()
        state, _calls = _fetch_state(st, monkeypatch, _PeerExport(status, refusal))

        resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

        assert resp.status == 502, code
        body = json.loads(resp.body)
        assert body == {
            "error": f"the crew refused the export (HTTP {status})",
            "code": "transfer_peer_refused",
            "peer_status": status,
        }
        assert state._slots == {}


@pytest.mark.asyncio
async def test_fetch_session_maps_only_the_exports_own_404_to_a_missing_session(monkeypatch):
    """A 404 is a missing session only when it carries ``export_slot_not_found``
    in a small JSON body: another code, an over-long body or non-JSON is a refusal."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    for refusal in (
        b'{"code": "AKIAIOSFODNN7EXAMPLE"}',
        json.dumps({"code": "export_slot_not_found", "pad": "x" * 5000}).encode(),
        b"not json",
    ):
        state, _calls = _fetch_state(st, monkeypatch, _PeerExport(404, refusal))

        resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

        assert resp.status == 502, refusal[:40]
        assert json.loads(resp.body) == {
            "error": "the crew refused the export (HTTP 404)",
            "code": "transfer_peer_refused",
            "peer_status": 404,
        }
        assert "AKIA" not in resp.body.decode()


@pytest.mark.asyncio
async def test_fetch_session_blames_the_peer_not_the_caller_for_a_bad_bundle(monkeypatch):
    """The arrival answers 400 for a body it cannot read; here that body is the
    PEER's export, so the owner gets a 502 that keeps the arrival's code."""
    import gzip

    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    cases = {
        b"\x1f\x8b\x08\x00truncated": "transfer_invalid_gzip",
        gzip.compress(b"not json"): "transfer_invalid_json",
    }
    for data, code in cases.items():
        state, _calls = _fetch_state(st, monkeypatch, _PeerExport(200, data))

        resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

        assert resp.status == 502, (code, resp.body)
        assert json.loads(resp.body)["code"] == code
        assert state._slots == {}


@pytest.mark.asyncio
async def test_fetch_session_relays_only_the_status_of_a_peer_refusal(monkeypatch):
    """A peer refusal body is peer text; it is never forwarded."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    state, _calls = _fetch_state(
        st, monkeypatch, _PeerExport(400, b'{"error": "AKIAIOSFODNN7EXAMPLE"}')
    )

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 502
    body = json.loads(resp.body)
    assert body["code"] == "transfer_peer_refused"
    assert "AKIA" not in resp.body.decode()
    assert state._slots == {}


@pytest.mark.asyncio
async def test_fetch_session_reports_a_peer_that_is_not_connected(monkeypatch):
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

    err = ProxyRequestError(
        "proxy_peer_not_connected", "instance is not connected", http_status=503
    )
    state, _calls = _fetch_state(st, monkeypatch, error=err)

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "proxy_peer_not_connected"


@pytest.mark.asyncio
async def test_fetch_session_answers_a_stopped_manager_like_every_instances_route(monkeypatch):
    """``instances.enabled`` turned on in a running gateway leaves the manager unset
    until a restart. The route then gives the shared 503, whose code the dashboard
    keys on and whose message says to restart, not a code of its own."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import handlers_instances_gate as gate
    from kiro_crew.dashboard import session_transfer as st

    state, calls = _fetch_state(st, monkeypatch)
    state.instances_manager = None

    resp = await hi.api_instances_fetch_session(_fetch_request(state, "chat-1-17"))

    assert resp.status == 503
    assert json.loads(resp.body) == {
        "error": gate.MANAGER_UNAVAILABLE_MESSAGE,
        "code": gate.MANAGER_UNAVAILABLE_CODE,
    }
    assert calls == []


@pytest.mark.asyncio
async def test_fetch_session_is_owner_only(monkeypatch):
    """The peer request spends the owner's credential, so an app token is refused
    before anything is asked of the peer."""
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st

    state, calls = _fetch_state(st, monkeypatch, _PeerExport(200))
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "crew"},
        headers={},
        claims={"user": "owner", "app": "some-app"},
        json=_async_value({"slot": "chat-1-17"}),
    )

    resp = await hi.api_instances_fetch_session(request)

    assert resp.status == 403
    assert calls == []


# ── fetch-session premise: the hub's credential IS the crew's owner ───────


@pytest.fixture
def _isolated_dashboard_tokens(tmp_path, monkeypatch):
    """Fresh token/nonce state on a tmp config dir (as ``test_token_auth`` does)."""
    import kiro_crew.dashboard.revocation_gen as _rg
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(_rg, "_gen", 0)
    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    _ta._app_perms_cache.clear()
    yield
    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    _ta._app_perms_cache.clear()


def _crew_side_request(app, *, path, query=None, cookies=None, headers=None):
    """A request on the CREW's gateway whose item store is a real dict, so what
    the auth middleware stamps (``user``, ``app``) is what the handler reads."""
    from unittest.mock import MagicMock

    store: dict = {}
    req = MagicMock(spec=web.Request)
    req.app = app
    req.path = path
    req.query = query or {}
    req.cookies = cookies or {}
    # The hub reaches the crew through its local forward, so the Host header
    # carries the forward's port and the crew names its cookie after it.
    req.headers = {"Host": "127.0.0.1:7781", **(headers or {})}
    req.remote = "127.0.0.1"
    req.method = "GET"
    req.__getitem__.side_effect = store.__getitem__
    req.__setitem__.side_effect = store.__setitem__
    req.__contains__.side_effect = store.__contains__
    req.get.side_effect = store.get
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("crew_owner_id", ["", "U0CREWOWNER"])
async def test_the_hubs_minted_credential_reaches_the_crews_export_as_its_owner(
    crew_owner_id, monkeypatch, _isolated_dashboard_tokens
):
    """fetch-session's "full context when the crew allows it" rests on one
    premise: the credential the hub holds passes the crew export's Layer B gate
    (``is_owner_dashboard_request``). This drives the crew's REAL chain with no
    stub between the steps: ``kirocrew token`` (the crew's ``/api/token/local``
    mint, which the hub's token mint runs), the hub manager's link exchange
    (one ``GET /api/status?token=`` keeping the session cookie), and then the
    export request carrying that cookie through the crew's auth middleware.

    The export's own test (``test_an_export_carries_layer_b_on_explicit_opt_in``)
    covers the other half: owner + ``include_layer_b`` + the operator setting
    yields Layer B. Both owner shapes are covered: a configured owner id, and
    none (the mint's ``local-app`` subject).
    """
    import kiro_crew.member_memory_auth as mma
    from kiro_crew.dashboard import handlers as dash_handlers
    from kiro_crew.dashboard import session_export as se
    from kiro_crew.dashboard.handlers import core
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    monkeypatch.setattr(core, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_k: None))
    monkeypatch.setattr(dash_handlers, "is_loopback", lambda _r: True, raising=False)
    monkeypatch.setattr(
        mma, "local_owner_bootstrap_provenance", lambda _r: mma.LocalOwnerProvenance()
    )
    crew_app = {"state": SimpleNamespace(owner_id=crew_owner_id), "local_secret": "crew-secret"}

    # 1. The mint ``kirocrew token`` performs on the crew.
    minted = await core.api_token_local(
        _crew_side_request(
            crew_app, path="/api/token/local", headers={"X-Local-Secret": "crew-secret"}
        )
    )
    assert minted.status == 200, minted.body
    link = json.loads(minted.body)["token"]

    middleware = token_auth_middleware()

    async def _status(_request):
        return web.Response(text="ok")

    # 2. The hub manager's link exchange (``_exchange_link``).
    exchanged = await middleware(
        _crew_side_request(crew_app, path="/api/status", query={"token": link}), _status
    )
    assert exchanged.status == 200
    # The manager keeps exactly the port-scoped cookie (``_peer_target``).
    cookie_name = "mc_token_7781"
    session_cookie = exchanged.cookies[cookie_name].value

    # 3. The export request fetch-session sends, judged by the gate the export uses.
    seen: dict = {}

    async def _export(request):
        seen["owner"] = se.is_owner_dashboard_request(request)
        return web.Response(text="ok")

    answered = await middleware(
        _crew_side_request(
            crew_app,
            path="/api/chat/slots/chat-1-17/export",
            query={"include_layer_b": "true"},
            cookies={cookie_name: session_cookie},
        ),
        _export,
    )
    assert answered.status == 200
    assert seen == {"owner": True}
