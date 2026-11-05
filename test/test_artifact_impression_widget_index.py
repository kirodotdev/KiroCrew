"""A boolean ``widget_index`` on an artifact impression is ignored, never stored.

``POST /api/artifacts/{slug}/events`` records a ``referenced`` impression with the
widget's ordinal in its message. A JSON ``true`` arrives as a Python ``bool``, an
``int`` subclass, so the integer check let it through and the event stored
``widget_index: true``. The impression is still recorded -- the request saved
before and still does -- but without the index, and the answer says why in a
``warnings`` list. A real integer is stored as before and the answer carries no
``warnings`` key; a string is still refused, as before.

Harness as in ``test_handlers_artifacts_coverage.py``: a MagicMock aiohttp
request and a real :class:`ArtifactStore` under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_json_body

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import api_artifact_record_event


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    """A real store under tmp_path, an unrestricted session and no SEL writes."""
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", s)
    monkeypatch.setattr(art_handlers, "_is_restricted_session", lambda _state, _req: False)
    monkeypatch.setattr(art_handlers, "_audit", lambda **_kw: None)
    return s


def _request(slug: str, metadata: dict[str, Any]) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = {"slug": slug}
    req.query = {}
    req.rel_url.query = {}
    attach_json_body(req, json.dumps({"type": "referenced", "metadata": metadata}).encode())
    state = MagicMock()
    state.get_slot.return_value = None
    req.app = {"state": state, "_restricted_session": False}
    return req


async def _post(store: ArtifactStore, metadata: dict[str, Any]) -> tuple[Any, dict, Any]:
    art = store.create(name="Doc", content="x", kind="markdown")
    resp = await api_artifact_record_event(_request(art.slug, metadata))
    return resp, json.loads(resp.body), store.get(art.slug)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_a_boolean_widget_index_is_ignored_with_a_warning(
    store: ArtifactStore, flag: bool
) -> None:
    resp, body, art = await _post(store, {"message_ts": "170.5", "widget_index": flag})
    assert resp.status == 200
    assert body["event"]["type"] == "referenced"
    assert body["event"]["metadata"] == {"message_ts": "170.5"}
    assert art.events[-1]["metadata"] == {"message_ts": "170.5"}
    assert body["warnings"] == ["metadata.widget_index must be an integer; the boolean was ignored"]


@pytest.mark.asyncio
async def test_an_integer_widget_index_is_stored_without_warnings(store: ArtifactStore) -> None:
    resp, body, art = await _post(store, {"widget_index": 2})
    assert resp.status == 200
    assert body["event"]["metadata"]["widget_index"] == 2
    assert art.events[-1]["metadata"]["widget_index"] == 2
    assert "warnings" not in body


@pytest.mark.asyncio
async def test_a_string_widget_index_is_still_refused(store: ArtifactStore) -> None:
    resp, body, _art = await _post(store, {"widget_index": "2"})
    assert resp.status == 400
    assert "widget_index" in body["error"]
