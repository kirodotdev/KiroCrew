"""``POST /api/hooks/agent`` for the Kiro Space app: post into a BOUND shared session.

Only a webhook source token the operator mapped to the app (``app="kiro-space"``) may
post into a session key the host bound (``hook:ks-session:<artifactId>``). That call
runs with ``deliver`` false and carries an app-attested author ``{userId, displayName}``,
stamped as ``Provenance.KIRO_SPACE_ROOM`` through ``room_principal``. Every other token
keeps today's behavior exactly: an isolated ``hook:`` session, any author ignored.

Trust boundary notes the tests pin:

* the bound key is unreachable for any other token (403), and an unbound
  ``hook:ks-session:*`` key is unreachable for the app token (403);
* the author comes only from the app-token path, never from another token;
* off unless Shared Crew's Feature Preview AND the room switch are on;
* the kill switch, the one-turn-per-key 409 and the route table are unchanged
  (the route stays on the existing dashboard listener: no new server).

The real handler is served by an aiohttp test server on a tmp_path home. The agent run
is a recorder that releases the claim and the permit as the real runner does.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import webhooks
from kiro_crew.config import sections
from kiro_crew.config.sections_shared_crew import shared_crew_config_from
from kiro_crew.dashboard import token_auth_paths
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.shared_crew import attribution, room_sessions
from kiro_crew.shared_crew.contracts import PrincipalKind, Provenance
from kiro_crew.shared_crew.identity import room_principal
from kiro_crew.shared_crew.preview import PREVIEW_ENV
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, await_until

TS = 1_700_000_000
BOUND = "hook:ks-session:art_room1"
UNBOUND = "hook:ks-session:art_other"
AUTHOR = {"userId": "usr_bob", "displayName": "Bob"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setattr(webhooks, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", tmp_path / "hooks.json")
    monkeypatch.setattr(hooks_handlers, "_sel", MagicMock())
    ManualClock(start=float(TS)).install(monkeypatch, webhooks)
    ManualClock(start=float(TS) + 0.25).install(monkeypatch, hooks_handlers)
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    inherited = hooks_handlers._hook_semaphore._value
    hooks_handlers._reset_hook_inflight()
    yield tmp_path
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    hooks_handlers._reset_hook_inflight()
    while hooks_handlers._hook_semaphore._value < inherited:
        hooks_handlers._hook_semaphore.release()


@pytest.fixture
def room_on(monkeypatch, home):
    """Feature Preview on, room switch on, and one bound key."""
    monkeypatch.setenv(PREVIEW_ENV, "1")
    monkeypatch.setattr(
        sections,
        "load_shared_crew_config",
        lambda: shared_crew_config_from({"room": True}),
    )
    room_sessions.bindings().bind(BOUND)


class _Runs:
    """Records each accepted run, then releases the claim and permit like the real runner."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.release = asyncio.Event()

    async def run(  # type: ignore[no-untyped-def]
        self, state, session_key, message, name, agent, deliver, timeout_secs, **kwargs
    ):
        self.calls.append(
            {"session_key": session_key, "message": message, "deliver": deliver, **kwargs}
        )
        try:
            await await_until(self.release)
        finally:
            hooks_handlers._hook_inflight_sessions.discard(session_key)
            hooks_handlers._hook_semaphore.release()

    @property
    def keys(self) -> list[str]:
        return [c["session_key"] for c in self.calls]


async def _until_runs(runs: _Runs, n: int) -> None:
    await async_wait_until(lambda: len(runs.calls) >= n, describe=lambda: f"calls={runs.keys}")


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_post("/api/hooks/agent", hooks_handlers.api_hooks_agent)
    return app


def _token(home, *, app: str = "") -> dict[str, str]:
    raw, _secret, _entry = webhooks.WebhookTokenStore(home).create(
        "src", require_signature=False, app=app
    )
    return {"Authorization": f"Bearer {raw}", "Content-Type": "application/json"}


async def _post(http, headers, **body):
    body.setdefault("message", "continue the plan")
    resp = await http.post("/api/hooks/agent", data=json.dumps(body), headers=headers)
    return resp.status, await resp.json()


@pytest.fixture
def runs(monkeypatch):
    r = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", r.run)
    return r


# ── the app token on a bound key ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_app_token_posts_into_the_bound_key_with_deliver_false_and_the_author(
    home, room_on, runs
):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, body = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
        await _until_runs(runs, 1)
        runs.release.set()
    assert (status, body["sessionKey"]) == (200, BOUND)
    assert runs.keys == [BOUND]
    call = runs.calls[0]
    assert call["deliver"] is False  # the request named no deliver: false is the default here
    author = call["author"]
    assert author.provenance is Provenance.KIRO_SPACE_ROOM
    assert author.kind is PrincipalKind.HUMAN
    assert (author.subject, author.display.name) == ("usr_bob", "Bob")


@pytest.mark.asyncio
async def test_a_request_asking_for_delivery_is_still_not_delivered_on_this_path(
    home, room_on, runs
):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, _ = await _post(http, headers, sessionKey=BOUND, author=AUTHOR, deliver=True)
        await _until_runs(runs, 1)
        runs.release.set()
    assert status == 200
    assert runs.calls[0]["deliver"] is False


@pytest.mark.asyncio
async def test_the_author_is_stamped_on_the_turn_meta(home, room_on, runs):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
        await _until_runs(runs, 1)
        runs.release.set()
    meta = runs.calls[0]["message_meta"]
    stamped = attribution.message_author(meta)
    assert stamped.kind is PrincipalKind.HUMAN
    assert stamped.display.name == "Bob"
    assert stamped.id == room_principal("usr_bob", "Bob").id


@pytest.mark.parametrize(
    "author",
    [
        None,
        "usr_bob",
        [],
        {},
        {"userId": "usr_bob"},
        {"displayName": "Bob"},
        {"userId": "", "displayName": "Bob"},
        {"userId": "   ", "displayName": "Bob"},
        {"userId": 7, "displayName": "Bob"},
        {"userId": "usr_bob", "displayName": 7},
        {"userId": "u" * 300, "displayName": "Bob"},
    ],
)
@pytest.mark.asyncio
async def test_a_missing_or_malformed_author_is_refused_400_and_nothing_runs(
    home, room_on, runs, author
):
    headers = _token(home, app="kiro-space")
    body: dict = {"sessionKey": BOUND}
    if author is not None:
        body["author"] = author
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, **body)
    assert status == 400
    assert resp["code"] == "author_invalid"
    assert runs.calls == []
    assert BOUND not in hooks_handlers._hook_inflight_sessions


# ── every other token keeps today's behavior ───────────────────────────────


@pytest.mark.asyncio
async def test_a_plain_token_cannot_target_the_bound_key(home, room_on, runs):
    headers = _token(home)
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
    assert status == 403
    assert resp["code"] == "session_key_bound"
    assert runs.calls == []


@pytest.mark.asyncio
async def test_a_plain_token_cannot_set_an_author_on_an_ordinary_key(home, room_on, runs):
    headers = _token(home)
    async with TestClient(TestServer(_app())) as http:
        status, _ = await _post(http, headers, sessionKey="hook:ci:1", author=AUTHOR)
        await _until_runs(runs, 1)
        runs.release.set()
    assert status == 200
    call = runs.calls[0]
    assert call.get("author") is None
    assert call.get("message_meta") in (None, {})
    # Today's behavior: delivery defaults to true for an ordinary token.
    assert call["deliver"] is True


@pytest.mark.asyncio
async def test_the_app_token_on_an_ordinary_hook_key_behaves_as_before_and_ignores_author(
    home, room_on, runs
):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, _ = await _post(http, headers, sessionKey="hook:ci:2", author=AUTHOR)
        await _until_runs(runs, 1)
        runs.release.set()
    assert status == 200
    assert runs.calls[0].get("author") is None


@pytest.mark.asyncio
async def test_the_app_token_cannot_target_an_unbound_ks_session_key(home, room_on, runs):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=UNBOUND, author=AUTHOR)
    assert status == 403
    assert resp["code"] == "session_key_unbound"
    assert runs.calls == []


@pytest.mark.asyncio
async def test_the_prefix_rule_still_refuses_a_key_without_hook_prefix(home, room_on, runs):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey="ks-session:art_room1", author=AUTHOR)
    assert status == 400
    assert resp["code"] == "session_key_prefix_invalid"


# ── the switches ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_by_default_without_the_feature_preview(home, monkeypatch, runs):
    monkeypatch.delenv(PREVIEW_ENV, raising=False)
    room_sessions.bindings().bind(BOUND)
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
    assert status == 403
    assert resp["code"] == "room_disabled"
    assert runs.calls == []


@pytest.mark.asyncio
async def test_off_when_the_preview_is_on_but_the_room_switch_is_off(home, monkeypatch, runs):
    monkeypatch.setenv(PREVIEW_ENV, "1")
    monkeypatch.setattr(sections, "load_shared_crew_config", lambda: shared_crew_config_from({}))
    room_sessions.bindings().bind(BOUND)
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
    assert status == 403
    assert resp["code"] == "room_disabled"
    assert runs.calls == []


def test_the_room_switch_and_the_app_field_default_off():
    assert shared_crew_config_from({}).room is False
    assert shared_crew_config_from({"room": "yes"}).room is False


def test_a_token_minted_without_an_app_has_none_and_the_field_is_validated(home):
    store = webhooks.WebhookTokenStore(home)
    _raw, _secret, entry = store.create("plain", require_signature=False)
    assert entry["app"] == ""
    _raw, _secret, mapped = store.create("ks", require_signature=False, app="kiro-space")
    assert mapped["app"] == "kiro-space"
    with pytest.raises(webhooks.WebhookError):
        store.create("bad", require_signature=False, app="somebody-else")


# ── what still applies ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_kill_switch_still_refuses_the_app_token_first(home, room_on, runs):
    headers = _token(home, app="kiro-space")
    webhooks.token_store().set_switch(False)
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
    assert status == 503
    assert resp["code"] == "webhooks_disabled"
    assert runs.calls == []


@pytest.mark.asyncio
async def test_a_second_post_to_a_running_bound_key_is_409_session_busy(home, room_on, runs):
    headers = _token(home, app="kiro-space")
    async with TestClient(TestServer(_app())) as http:
        first, _ = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
        await _until_runs(runs, 1)
        second, resp = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
        runs.release.set()
    assert (first, second) == (200, 409)
    assert resp["code"] == "session_busy"
    assert len(runs.calls) == 1


@pytest.mark.asyncio
async def test_an_unauthenticated_caller_gets_401_before_anything_about_the_key(
    home, room_on, runs
):
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(
            http, {"Content-Type": "application/json"}, sessionKey=BOUND, author=AUTHOR
        )
    assert status == 401
    assert resp["code"] == "unauthorized"
    assert runs.calls == []


# ── no new exposure ────────────────────────────────────────────────────────


def test_the_route_stays_on_the_existing_listener_with_the_same_bypass():
    assert token_auth_paths.AGENT_HOOK_PATH == "/api/hooks/agent"
    assert set(token_auth_paths._BYPASS_EXACT_METHODS) == {
        "/api/hooks/agent",
        "/api/hooks/agent/status",
        "/api/connections/oauth/callback",
        "/api/relay-viewer/token",
        "/api/messaging/teams",
        "/api/update/revalidate",
    }
    assert token_auth_paths._BYPASS_EXACT_METHODS["/api/hooks/agent"] == frozenset({"POST"})


def test_the_new_module_opens_no_listener():
    src = inspect.getsource(room_sessions)
    for forbidden in (
        "TCPSite",
        "UnixSite",
        "create_server",
        "start_server",
        ".listen(",
        ".bind_socket",
        "web.run_app",
    ):
        assert forbidden not in src


def test_the_binding_registry_binds_only_ks_session_keys(home):
    reg = room_sessions.bindings()
    assert reg.is_bound(BOUND) is False
    reg.bind(BOUND)
    assert reg.is_bound(BOUND) is True
    assert reg.is_bound(UNBOUND) is False
    with pytest.raises(ValueError):
        reg.bind("hook:default:1")
    with pytest.raises(ValueError):
        reg.bind("hook:ks-session:")
    reg.unbind(BOUND)
    assert reg.is_bound(BOUND) is False
