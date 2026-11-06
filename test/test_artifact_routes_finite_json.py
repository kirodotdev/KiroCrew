"""An ``Infinity`` / ``NaN`` stored in one ``meta.json`` must not break an artifact route's JSON.

``meta.json`` is parsed with ``json.loads``, which accepts those constants, and the
events log and metadata blocks reach the response as stored. The browser's
``Response.json()`` refuses them, so a reply carrying one fails as a whole: one
hand-edited artifact would blank the Library list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers import artifacts as handlers


def _strict_json(raw: bytes) -> Any:
    """Parse as the browser does: ``Infinity`` / ``NaN`` are errors."""

    def _refuse(token: str) -> Any:
        raise ValueError(f"not JSON: {token}")

    return json.loads(raw, parse_constant=_refuse)


def _request(slug: str = "") -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = {"slug": slug} if slug else {}
    req.query = {}
    req.app = {"state": MagicMock()}
    return req


@pytest.fixture
def edited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, Path]:
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(handlers, "_is_restricted_session", lambda state, request: False)
    bad = store.create(name="Bad", content="x").slug
    good = store.create(name="Good", content="y").slug
    path = store.root / bad / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["events"].append(
        {"type": "edited", "at": "2026-01-01T00:00:00Z", "metadata": {"n": "INF", "m": ["NAN"]}}
    )
    raw = json.dumps(meta).replace('"INF"', "Infinity").replace('"NAN"', "NaN")
    path.write_text(raw, encoding="utf-8")
    return bad, good, path


@pytest.mark.asyncio
async def test_the_events_route_answers_valid_json(edited: tuple[str, str, Path]) -> None:
    bad, _good, path = edited
    before = path.read_text(encoding="utf-8")

    resp = await handlers.api_artifact_events(_request(bad))

    assert resp.status == 200
    events = _strict_json(resp.body)["events"]
    assert events[-1]["metadata"] == {"n": None, "m": [None]}
    # Serializing never rewrites the stored file.
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_the_list_route_answers_valid_json_with_every_artifact(
    edited: tuple[str, str, Path],
) -> None:
    bad, good, _path = edited

    resp = await handlers.api_artifacts_list(_request())

    assert resp.status == 200
    body = _strict_json(resp.body)
    slugs = {a["slug"] for a in body["artifacts"]}
    assert {bad, good} <= slugs


def test_finite_values_are_sent_unchanged() -> None:
    from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import _json_response

    data = {"n": 1.5, "i": 3, "s": "Infinity", "l": [0.0, -2], "b": True, "z": None}
    assert _strict_json(_json_response(data).body) == data
