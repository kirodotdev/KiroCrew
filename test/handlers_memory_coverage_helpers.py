"""The request, state and store builders the memory handler coverage modules share.

``_make_request`` builds a faked ``web.Request`` for a ``_make_state`` dashboard state,
``_store`` a vector-store mock and ``_cfg`` a memory config, and the two autouse
fixtures keep the security event log and the embedding setup status out of every test.
``test_handlers_memory_coverage.py`` and its ``_maintenance``, ``_request_contract`` and
``_embeddings`` siblings import what they use of these by name.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.memory as mem_mod


class _BadJSON:
    """Sentinel: make ``request.json()`` raise, as aiohttp does on bad bytes."""


def _body(resp: web.Response) -> Any:
    """Decode a ``web.json_response`` body."""
    return json.loads(resp.text or "")


def _make_state(
    *,
    vector_store: Any = None,
    memory: Any = None,
    consolidator: Any = None,
    restricted_keys: set[str] | None = None,
) -> Any:
    """A DashboardState stand-in wired for the memory handlers.

    ``_slots`` / ``_restricted_keys`` are real containers (not auto-MagicMocks)
    so ``_is_restricted_session`` runs its real logic instead of tripping over a
    truthy mock slot.
    """
    mem = memory if memory is not None else MagicMock()
    if vector_store is not None:
        mem.vector_store = vector_store
    state = MagicMock()
    # This fixture represents a readable, unbound Global V1 session.
    state.conversation_log.get_metadata_status.return_value = ({}, True)
    state.context_builder = MagicMock(memory=mem)
    state.consolidator = consolidator
    state._slots = {}
    state._restricted_keys = set(restricted_keys or ())
    return state


#: The statically-trusted dashboard key ``_recognize_session`` admits without a live
#: slot. Defaulted here so a test whose subject is not authorization does not restate
#: it: the refusal directions are asserted per route, positively, in
#: ``TestMemoryMutationSessionGate``, which is where that vigilance belongs. A test
#: that wants an unrecognized caller passes ``session_key=""`` explicitly.
_TRUSTED_SESSION_KEY = "dashboard:ui"


def _make_request(
    state: Any,
    *,
    method: str = "GET",
    json_body: Any = None,
    query: dict[str, str] | None = None,
    match_info: dict[str, str] | None = None,
    session_key: str = _TRUSTED_SESSION_KEY,
    body_present: bool | None = None,
) -> Any:
    req = MagicMock()
    req.app = {"state": state}
    req.method = method
    req.query = query or {}
    req.match_info = match_info or {}
    req.headers = {"X-Session-Key": session_key} if session_key else {}
    # ``can_read_body`` is what ``_shared.read_bounded_json`` consults for its
    # allow_absent endpoints. It defaults to "a body was supplied", which makes
    # ``json_body=None`` an ABSENT body -- the common case for the GET tests.
    # Pass ``body_present=True`` alongside ``json_body=None`` for the other
    # meaning: a body that is present and whose content is the JSON literal
    # ``null``, which is a non-object and must be refused, not defaulted.
    req.can_read_body = json_body is not None if body_present is None else body_present
    if isinstance(json_body, _BadJSON):
        req.json = AsyncMock(side_effect=ValueError("not json"))
        req.can_read_body = True
    else:
        req.json = AsyncMock(return_value=json_body)
    _as_owner(req, state)
    return req


def _as_owner(req: Any, state: Any) -> None:
    """Give a mock request the dashboard owner's claims, leaving every other key as-is.

    The durable-memory writes are owner-gated, so the dashboard-user request these
    tests model carries ``app == ""`` and the owner's subject: ``state.owner_id``
    when one is configured, else the signed local bootstrap subject.
    """
    owner = str(getattr(state, "owner_id", "") or "") or "local-app"
    claims = {"app": "", "user": owner}
    req.__contains__.side_effect = lambda key: key in claims
    req.__getitem__.side_effect = lambda key: claims[key] if key in claims else DEFAULT
    req.get.side_effect = lambda key, *default: claims[key] if key in claims else DEFAULT


def _store(**attrs: Any) -> Any:
    """A vector-store mock with JSON-serializable defaults."""
    store = MagicMock()
    store.embed_fn = None
    store.get_all_semantic.return_value = []
    store.get_events.return_value = []
    store.get_episodic_list.return_value = []
    store.search_episodic.return_value = []
    store.set_semantic.return_value = None
    store.delete_semantic.return_value = True
    store.delete_episodic.return_value = True
    store.memory_stats.return_value = {"semantic_count": 0}
    store.get_rejection_stats.return_value = {}
    store.get_context_preview.return_value = {"semantic": ""}
    store.read_counters.return_value = {"rows_read": 0}
    store.get_semantic_context.return_value = ""
    store.get_episodic_context.return_value = ""
    store.import_memory.return_value = {"semantic": 0, "episodic": 0}
    store.migrate_from_markdown.return_value = {"semantic": 0, "episodic": 0}
    store.promote_episodic_patterns.return_value = 0
    for k, v in attrs.items():
        setattr(store, k, v)
    return store


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim and no test in the memory handler coverage files calls monkeypatch.undo()
def _fake_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write to the real security event log from a unit test."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())


@pytest.fixture(autouse=True)
def _reset_setup_status() -> Any:
    saved = dict(mem_mod._embedding_setup_status)
    mem_mod._embedding_setup_status = {"step": "idle", "error": ""}
    yield
    mem_mod._embedding_setup_status = saved


def _cfg(idle: float = 6.0, days: int = 30, migrated: bool = False) -> Any:
    cfg = MagicMock()
    cfg.memory.history_idle_hours = idle
    cfg.memory.history_max_days = days
    cfg.memory.migrated = migrated
    return cfg
