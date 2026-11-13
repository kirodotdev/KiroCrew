"""A dashboard route answers a JSON body that is not an object with a 400.

Each of these handlers parses with ``await request.json()`` and then reads its
fields with ``body.get()``. A list, string, number or ``null`` is valid JSON, so
the parse succeeds and the ``.get()`` raised ``AttributeError``: a 500 for a
client mistake. Each now returns ``body_not_object_error``'s 400 right after the
parse (the OpenAI-compatible route keeps its own error shape and the same code).

Each handler is driven directly with its access gates opened, so the call
reaches the parse. Every route also gets an object body, which must pass the
guard to the route's own checks, as before.

``api_terminal_complete``, ``api_themes_create`` and ``api_theme_detail`` also
parse a body, but already answer a non-object with a 400 of their own, so they
are not here.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

#: Gate helpers opened for the drive, grouped by what an open gate returns.
_ASYNC_NONE = (
    "require_owner_dashboard_request",
    "_require_monitor_owner",
    "reject_if_kiro_unverified",
)
_SYNC_NONE = ("refuse_unattributable_caller", "member_slot_write_refused", "deny_app_slot_access")
_SYNC_TRUE = ("app_owns_transcript",)

#: (module, handler, method, match_info, an object body that stops at the route's
#: own first check). The update simulation's ``reject`` answers before it starts a
#: background walk; the loop update's non-string ``expect_fingerprint`` answers
#: before it reaches the service.
_ROUTES = [
    ("kiro_crew.dashboard.chat_folders", "api_chat_slot_folder", "POST", {"slot": "s1"}, {}),
    ("kiro_crew.dashboard.chat_folders", "api_chat_slot_pin", "POST", {"slot": "s1"}, {}),
    ("kiro_crew.dashboard.chat_folders", "api_chat_slot_mode", "POST", {"slot": "s1"}, {}),
    ("kiro_crew.dashboard.handlers.autonudge", "api_autonudge_start", "POST", {}, {}),
    (
        "kiro_crew.dashboard.handlers.autonudge",
        "api_autonudge_update",
        "PATCH",
        {"loop_id": "L1"},
        {"expect_fingerprint": 7},
    ),
    ("kiro_crew.dashboard.handlers.core_config_view", "api_kirocrew_config", "PUT", {}, {}),
    ("kiro_crew.dashboard.handlers.prompts_skill_edits", "api_skill_pin", "POST", {}, {}),
    (
        "kiro_crew.dashboard.handlers.prompts_skill_edits",
        "_skill_detail_verbs",
        "PUT",
        {"name": "s"},
        {},
    ),
    ("kiro_crew.dashboard.handlers.prompts_skill_edits", "api_skills_create", "POST", {}, {}),
    ("kiro_crew.dashboard.handlers.steering", "api_steering_create", "POST", {}, {}),
    ("kiro_crew.dashboard.handlers.steering", "api_steering_detail", "PUT", {"key": "a.md"}, {}),
    (
        "kiro_crew.dashboard.handlers.updates_apply",
        "api_update_simulate",
        "POST",
        {},
        {"reject": True},
    ),
    ("kiro_crew.dashboard.handlers.updates_streams", "api_log_level", "POST", {}, {}),
    ("kiro_crew.dashboard.openai_compat", "api_completions", "POST", {}, {}),
]

_NON_OBJECTS = [[1, 2], [], "text", 7, None]


class _Req(dict):
    """A request double: an owner caller, a JSON body, and the route's match info."""

    def __init__(self, method: str, match_info: dict[str, str], body: Any) -> None:
        super().__init__(user="owner", app="")
        self.method = method
        self.match_info = match_info
        self.headers = {"Content-Type": "application/json"}
        self.content_type = "application/json"
        self.can_read_body = True
        self.remote = "127.0.0.1"
        self.query: dict[str, str] = {}
        self.rel_url = SimpleNamespace(query={})
        self.path = "/test"
        self.app = {"state": _state()}
        self.parsed = False
        self._body = body

    async def json(self) -> Any:
        self.parsed = True
        return self._body


def _state() -> MagicMock:
    state = MagicMock()
    state.owner_id = "owner"
    slot = MagicMock()
    slot.key = "dashboard:s1"
    state._slots = {"s1": slot}
    state._folders = []
    return state


def _open_gates(monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    for name in _ASYNC_NONE:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, _none)
    for name in _SYNC_NONE:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, lambda *_a, **_k: None)
    for name in _SYNC_TRUE:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, lambda *_a, **_k: True)


@pytest.fixture()
def drive(tmp_path, monkeypatch):
    """Call one handler with *body*, its gates open; returns ``(request, response)``."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    steering = tmp_path / "steering"
    steering.mkdir()
    (steering / "a.md").write_text("hello\n", encoding="utf-8")

    async def _drive(modname: str, fn: str, method: str, match: dict, body: Any):
        mod = importlib.import_module(modname)
        _open_gates(monkeypatch, mod)
        if modname.endswith(".autonudge"):
            svc = MagicMock()
            svc.get_by_id.return_value = None
            monkeypatch.setattr(mod, "_autonudge_get", lambda: svc)
        req = _Req(method, match, body)
        try:
            resp = await getattr(mod, fn)(req)
        except Exception as exc:  # noqa: BLE001 -- aiohttp answers this as a 500
            # What a client sees when a handler raises: aiohttp turns the
            # exception into a 500, which is the failure this file pins.
            resp = web.json_response({"error": repr(exc)}, status=500)
        return req, resp

    return _drive


def _code(resp: Any) -> Any:
    import json

    payload = json.loads(resp.text)
    err = payload.get("error")
    return err.get("code") if isinstance(err, dict) else payload.get("code")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", _NON_OBJECTS, ids=repr)
@pytest.mark.parametrize(
    "modname,fn,method,match,_object_body", _ROUTES, ids=[r[1] for r in _ROUTES]
)
async def test_a_non_object_body_is_a_400(
    drive, modname, fn, method, match, _object_body, body
) -> None:
    req, resp = await drive(modname, fn, method, match, body)
    assert req.parsed, "the drive never reached the parse"
    assert resp.status == 400, resp.text
    assert _code(resp) == "body_not_object"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modname,fn,method,match,object_body", _ROUTES, ids=[r[1] for r in _ROUTES]
)
async def test_an_object_body_passes_the_guard(
    drive, modname, fn, method, match, object_body
) -> None:
    req, resp = await drive(modname, fn, method, match, object_body)
    assert req.parsed
    assert resp.status != 500, resp.text
    assert _code(resp) != "body_not_object"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "content"])
@pytest.mark.parametrize("value", [5, ["a"], {"a": 1}, True, None], ids=repr)
async def test_skills_create_refuses_a_non_string_field(drive, field, value) -> None:
    """A non-string ``name`` or ``content`` has no ``.strip()``, so each is a coded
    400."""
    body = {"name": "my-skill", "content": "# Skill\n", field: value}
    _req, resp = await drive(
        "kiro_crew.dashboard.handlers.prompts_skill_edits", "api_skills_create", "POST", {}, body
    )
    assert resp.status == 400, resp.text
    assert _code(resp) == "invalid_field_type"
