"""Shared pieces for ``test_artifacts_handlers.py`` and its item, actions and update parts.

``isolated_store`` swaps ``kiro_crew.artifacts``' default :class:`ArtifactStore` for one rooted
at the test's tmp dir, ``_request`` builds the MagicMock aiohttp request the handlers read,
``patch_restricted`` makes the restricted-session check read
``req.app['_restricted_session']``, and ``_json_body`` decodes a handler's JSON response.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_json_body

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore


@pytest.fixture(name="isolated_store")
def isolated_store_fixture(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """Replace the module-level default store with one rooted at tmp_path."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


def _request(
    *,
    body: dict | bytes | None = None,
    match: dict | None = None,
    query: dict | None = None,
    session_key: str = "dashboard:test",
    restricted: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock aiohttp Request with the right shape for our handlers."""
    req = MagicMock()
    headers = {"X-Session-Key": session_key}
    if extra_headers:
        headers.update(extra_headers)
    req.headers = headers
    req.match_info = match or {}
    req.query = query or {}
    if isinstance(body, dict):
        encoded = json.dumps(body).encode()
        attach_json_body(req, encoded)
    elif isinstance(body, bytes):
        attach_json_body(req, body)
    else:
        attach_json_body(req, b"")
    # Attach the restricted-session flag via a stub on the request app.
    # Provide a non-None state so handlers don't short-circuit on the
    # state-is-None deny-by-default guard.
    state = MagicMock()
    # ``get_slot`` must return None (session gone) rather than a bare MagicMock:
    # for any artifact carrying a ``session_key``, the list/detail handlers
    # resolve a title through it and then redact the result, and a MagicMock
    # title blows up inside the redaction regex. None yields the real
    # "(deleted session)" string, which is a valid state for a test store.
    state.get_slot.return_value = None
    req.app = {"state": state, "_restricted_session": restricted}
    return req


@pytest.fixture(name="patch_restricted")
def patch_restricted_fixture(monkeypatch):
    """Make _is_restricted_session read req.app['_restricted_session']."""
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _json_body(resp) -> dict:
    return json.loads(resp.body)
