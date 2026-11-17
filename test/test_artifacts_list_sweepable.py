"""``GET /api/artifacts`` says which records the auto-widget sweep could delete.

The Artifacts page's "Recent output" row lists exactly those records, so the
flag is the sweep's own predicate, not a client-side copy. The response also
names the cap the sweep keeps, so the page can say how close output is to it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import MAX_AUTO_WIDGET_ARTIFACTS, ArtifactStore
from kiro_crew.dashboard.handlers.artifacts import api_artifacts_list


def _request() -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = {}
    req.query = {}
    req.read = AsyncMock(return_value=b"")
    req.app = {}
    return req


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", s)
    return s


@pytest.mark.asyncio
async def test_flags_only_unclaimed_auto_registered_records(store: ArtifactStore) -> None:
    store.create(name="saved", slug="saved", content="x")
    store.create(name="auto", slug="auto", content="<div>a</div>", auto_registered=True)
    store.create(name="starred", slug="starred", content="<div>b</div>", auto_registered=True)
    store.set_pinned("starred", True)
    store.create(
        name="tagged", slug="tagged", content="<div>c</div>", auto_registered=True, tags=["t"]
    )

    body = json.loads((await api_artifacts_list(_request())).body)

    flags = {a["slug"]: a["sweepable"] for a in body["artifacts"]}
    assert flags == {"saved": False, "auto": True, "starred": False, "tagged": False}
    # Each flag is the sweep's own verdict on the same record.
    for a in body["artifacts"]:
        assert a["sweepable"] is store._is_sweepable_auto_widget(store.get(a["slug"]))


@pytest.mark.asyncio
async def test_reports_the_sweep_cap(store: ArtifactStore) -> None:
    body = json.loads((await api_artifacts_list(_request())).body)
    assert body["output_cap"] == MAX_AUTO_WIDGET_ARTIFACTS
