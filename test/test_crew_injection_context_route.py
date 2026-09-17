"""``GET /api/crew/injection-context`` — Plane B memory/lessons/ledger (N5-2).

The sidecar renders the SAME memory, lessons and ledger blocks
``ContextBuilder.build_session_context`` prepends, so KAS can inject them
natively. Coverage:

* ``render_injection_blocks`` returns the three kinds on a populated store,
* omits a kind that has nothing to inject,
* keys the ledger block by the caller's session, and
* the handler requires ``internal_auth`` + ``X-Session-Key`` and returns the
  ``{sessionId, blocks}`` shape,
* the route is in the supervised-only admission set.

The renderers themselves are exercised through ``build_session_context`` in
``test_context.py`` — this file asserts the extracted helpers produce the same
content the prefix path does by populating a store and checking the block text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import session_ledger as sl
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers_system import api_crew_injection_context
from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    supervised_mixed_internal_paths,
)
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from test.test_token_auth import _make_request, _ok_handler

pytestmark = pytest.mark.asyncio

SECRET = "phase5-injection-secret"


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _builder(tmp_path, *, prefs: str = "", lesson: str = "") -> ContextBuilder:
    lessons = LessonStore(base_dir=tmp_path / "lessons")
    if lesson:
        lessons.save(Lesson(ts=_now(), rule=lesson, category="preference"))
    mem = MemoryStore(workspace=tmp_path / "ws")
    if prefs:
        mem.write_preferences(prefs)
    return ContextBuilder(
        memory=mem,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=lessons,
    )


# -- render_injection_blocks ---------------------------------------------------


def test_three_kinds_present_on_populated_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    session = "chat-inject-1"
    sl.record(
        session,
        goal="ship the injection route",
        next_step="wire the handler",
    )
    builder = _builder(
        tmp_path,
        prefs="- Prefers concise answers with code first.",
        lesson="Always run the tests before committing.",
    )
    blocks = builder.render_injection_blocks(session)
    kinds = {b["kind"] for b in blocks}
    assert kinds == {"memory", "lessons", "ledger"}
    by_kind = {b["kind"]: b["text"] for b in blocks}
    assert "concise answers" in by_kind["memory"]
    assert "run the tests before committing" in by_kind["lessons"]
    assert "ship the injection route" in by_kind["ledger"]
    assert all(b["text"].strip() for b in blocks)


def test_kinds_omitted_when_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    # No prefs, no lessons, no ledger for this session -> no blocks at all.
    builder = _builder(tmp_path)
    blocks = builder.render_injection_blocks("chat-empty-1")
    assert blocks == []


def test_ledger_is_keyed_by_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    sl.record("chat-owner", goal="owner goal", next_step="do the owner thing")
    builder = _builder(tmp_path)
    # The owner sees its ledger...
    owner_blocks = builder.render_injection_blocks("chat-owner")
    assert any(b["kind"] == "ledger" and "owner goal" in b["text"] for b in owner_blocks)
    # ...a different session does not.
    other_blocks = builder.render_injection_blocks("chat-other")
    assert all(b["kind"] != "ledger" for b in other_blocks)


# -- handler -------------------------------------------------------------------


def _request(
    *, session_key: str | None, builder, internal_auth: bool = True
) -> MagicMock:
    request = MagicMock()
    store = {"internal_auth": internal_auth}
    request.get.side_effect = store.get
    headers = {}
    if session_key is not None:
        headers["X-Session-Key"] = session_key
    request.headers = headers
    request.app = {"state": SimpleNamespace(context_builder=builder)}
    return request


async def test_handler_returns_session_id_and_blocks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    builder = _builder(tmp_path, prefs="- Prefers dark mode.")
    resp = await api_crew_injection_context(
        _request(session_key="kiro-cli:abc", builder=builder)
    )
    data = json.loads(resp.body)
    assert data["sessionId"] == "kiro-cli:abc"
    assert any(b["kind"] == "memory" for b in data["blocks"])


async def test_handler_requires_internal_auth(tmp_path) -> None:
    builder = _builder(tmp_path)
    resp = await api_crew_injection_context(
        _request(session_key="kiro-cli:abc", builder=builder, internal_auth=False)
    )
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "internal_required"


async def test_handler_requires_session_key(tmp_path) -> None:
    builder = _builder(tmp_path)
    resp = await api_crew_injection_context(_request(session_key=None, builder=builder))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "session_required"


async def test_handler_no_builder_returns_empty_blocks() -> None:
    request = MagicMock()
    store = {"internal_auth": True}
    request.get.side_effect = store.get
    request.headers = {"X-Session-Key": "kiro-cli:abc"}
    request.app = {"state": SimpleNamespace(context_builder=None)}
    resp = await api_crew_injection_context(request)
    data = json.loads(resp.body)
    assert data == {"sessionId": "kiro-cli:abc", "blocks": []}


# -- admission -----------------------------------------------------------------


def _mw(supervised: bool):
    return token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(supervised),
        internal_secret=SECRET,
    )


def test_route_is_supervised_only() -> None:
    added = supervised_mixed_internal_paths(True) - _MIXED_INTERNAL_API_PATHS
    assert "/api/crew/injection-context" in added
    assert "/api/crew/injection-context" not in _MIXED_INTERNAL_API_PATHS


async def test_supervised_secret_reaches_route() -> None:
    req = _make_request(
        path="/api/crew/injection-context", headers={"X-Internal-Secret": SECRET}
    )
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


async def test_unsupervised_denied() -> None:
    req = _make_request(
        path="/api/crew/injection-context", headers={"X-Internal-Secret": SECRET}
    )
    resp = await _mw(False)(req, _ok_handler)
    assert resp.status == 403
