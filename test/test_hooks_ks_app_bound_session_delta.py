"""Registry errors, the app mapping routes and the reserved key prefix.

2  an unreadable bound-session registry is 503 for BOTH an app source and a plain source: a
   read error is never read as "unbound".
3  route level: the app mapping is set only by the owner-only mint, an unknown value is
   `app_invalid`, and the source PATCH cannot set or change it.
4  the `hook:ks-session:` prefix is reserved for EVERY source: a key that has the prefix but
   is not the exact bound shape is refused, never run as an ordinary key.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from webhooks_api_helpers import wired_fixture  # noqa: F401
from webhooks_api_helpers import _payload, _req

from kiro_crew import webhooks
from kiro_crew.config import sections
from kiro_crew.config.sections_shared_crew import shared_crew_config_from
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.shared_crew import room_sessions
from kiro_crew.shared_crew.preview import PREVIEW_ENV
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, await_until

H = hooks_handlers  # the route tests below name the handlers the way test_webhooks_api.py does
TS = 1_700_000_000
BOUND = "hook:ks-session:art_room1"
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
    monkeypatch.setenv(PREVIEW_ENV, "1")
    monkeypatch.setattr(
        sections,
        "load_shared_crew_config",
        lambda: shared_crew_config_from({"room": True}),
    )
    room_sessions.bindings().bind(BOUND)


class _Runs:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.release = asyncio.Event()

    async def run(  # type: ignore[no-untyped-def]
        self, state, session_key, message, name, agent, deliver, timeout_secs, **kwargs
    ):
        self.calls.append({"session_key": session_key, "deliver": deliver, **kwargs})
        try:
            await await_until(self.release)
        finally:
            hooks_handlers._hook_inflight_sessions.discard(session_key)
            hooks_handlers._hook_semaphore.release()


@pytest.fixture
def runs(monkeypatch):
    r = _Runs()
    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", r.run)
    return r


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_post("/api/hooks/agent", hooks_handlers.api_hooks_agent)
    return app


def _source(home, *, app: str) -> dict[str, str]:
    raw, _secret, _entry = webhooks.WebhookTokenStore(home).create(
        "src", require_signature=False, app=app
    )
    return {"Authorization": f"Bearer {raw}", "Content-Type": "application/json"}


async def _post(http, headers, **body):
    body.setdefault("message", "continue the plan")
    resp = await http.post("/api/hooks/agent", data=json.dumps(body), headers=headers)
    return resp.status, await resp.json()


# -- item 2: an unreadable registry is 503, never "unbound" ---------------------


@pytest.mark.parametrize("app", ["kiro-space", ""])
@pytest.mark.asyncio
async def test_item2_an_unreadable_registry_is_503_for_every_source(home, room_on, runs, app):
    (home / "room_sessions.json").write_text("{not json", encoding="utf-8")
    headers = _source(home, app=app)
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=BOUND, author=AUTHOR)
    assert status == 503
    assert resp["code"] == "webhooks_unavailable"
    assert runs.calls == []
    assert BOUND not in hooks_handlers._hook_inflight_sessions


# -- item 3: the app mapping through the real routes -------------------------


class TestAppMappingRoutes:
    _MINT = {"label": "Space app", "agent": "kirocrew", "require_signature": False}

    @pytest.mark.asyncio
    async def test_the_owner_mints_a_source_mapped_to_the_app(self, wired):
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {**self._MINT, "app": "kiro-space"},
                user="U0THEOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 201
        data = await _payload(resp)
        assert data["entry"]["app"] == "kiro-space"
        assert webhooks.token_store().entry_for(data["entry"]["id"])["app"] == "kiro-space"

    @pytest.mark.asyncio
    async def test_a_mint_without_app_has_no_mapping(self, wired):
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                dict(self._MINT),
                user="U0THEOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 201
        assert (await _payload(resp))["entry"]["app"] == ""

    @pytest.mark.parametrize("bad", ["somebody-else", "KIRO-SPACE", 7, ["kiro-space"], {}])
    @pytest.mark.asyncio
    async def test_an_unknown_app_value_is_app_invalid_and_mints_nothing(self, wired, bad):
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {**self._MINT, "app": bad},
                user="U0THEOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 400
        assert (await _payload(resp))["code"] == "app_invalid"
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_a_non_owner_cannot_mint_a_mapped_source(self, wired):
        resp = await H.api_webhook_token_create(
            _req(
                "POST",
                "/api/webhooks/tokens",
                {**self._MINT, "app": "kiro-space"},
                user="U0NONOWNER",
                owner_id="U0THEOWNER",
            )
        )
        assert resp.status == 403
        assert (await _payload(resp))["code"] == "owner_only"
        assert webhooks.token_store().count() == 0

    @pytest.mark.asyncio
    async def test_the_source_patch_cannot_set_or_change_the_mapping(self, wired):
        _raw, _secret, entry = webhooks.token_store().create(
            "plain", require_signature=False, agent="kirocrew"
        )
        for patch_body in ({"app": "kiro-space"}, {"app": "", "enabled": True}):
            resp = await H.api_webhook_token_update(
                _req(
                    "PATCH",
                    f"/api/webhooks/tokens/{entry['id']}",
                    patch_body,
                    match_info={"token_id": entry["id"]},
                    user="U0THEOWNER",
                    owner_id="U0THEOWNER",
                )
            )
            assert resp.status == 400
            assert (await _payload(resp))["code"] == "invalid_source_patch"
        assert webhooks.token_store().entry_for(entry["id"]).get("app", "") == ""


# -- item 4: the prefix is reserved for every source -------------------------

RESERVED_VARIANTS = [
    "hook:ks-session:",
    "hook:ks-session:art_room1 ",
    "hook:ks-session:art_room1\n",
    "hook:ks-session:art_room1/extra",
    "hook:ks-session:art room1",
    "hook:ks-session:art_room1:more",
    "hook:ks-session:" + "a" * 129,
]


@pytest.mark.parametrize("key", RESERVED_VARIANTS)
@pytest.mark.parametrize("app", ["kiro-space", ""])
@pytest.mark.asyncio
async def test_item4_a_reserved_prefix_key_that_is_not_the_exact_shape_is_refused(
    home, room_on, runs, key, app
):
    headers = _source(home, app=app)
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey=key, author=AUTHOR)
    assert status == 400
    assert resp["code"] == "session_key_reserved"
    assert runs.calls == []
    assert key not in hooks_handlers._hook_inflight_sessions


@pytest.mark.asyncio
async def test_item4_the_refusal_holds_with_the_switches_off_and_no_registry(
    home, monkeypatch, runs
):
    monkeypatch.delenv(PREVIEW_ENV, raising=False)
    headers = _source(home, app="")
    async with TestClient(TestServer(_app())) as http:
        status, resp = await _post(http, headers, sessionKey="hook:ks-session:x y", deliver=True)
    assert (status, resp["code"]) == (400, "session_key_reserved")
    assert runs.calls == []


@pytest.mark.asyncio
async def test_item4_ordinary_hook_keys_are_untouched(home, runs):
    headers = _source(home, app="")
    async with TestClient(TestServer(_app())) as http:
        status, _ = await _post(http, headers, sessionKey="hook:ci:ks-session:1")
        await async_wait_until(lambda: len(runs.calls) >= 1, describe=lambda: "waiting for run")
        runs.release.set()
    assert status == 200
    assert runs.calls[0]["session_key"] == "hook:ci:ks-session:1"
