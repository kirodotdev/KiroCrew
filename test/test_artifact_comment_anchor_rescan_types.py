"""A non-string ``anchor_quote`` in ``comments.json`` must not stop the anchor rescan.

``comments.json`` is decoded verbatim, so a hand edit can leave a quote that is a
number or a list. The rescan after a content write skips that one comment and
still flags every other thread whose quote is missing from the new content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.artifact_store.comments import rescan_anchors
from kiro_crew.artifact_store.model import ArtifactComment
from kiro_crew.artifacts import ArtifactStore


def _clock() -> str:
    return "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("bad", [7, ["hello"], {"q": "hello"}], ids=["number", "list", "object"])
def test_a_content_write_still_flags_the_other_threads(tmp_path: Path, bad: Any) -> None:
    store = ArtifactStore(root=tmp_path / "artifacts")
    art = store.create(name="A", content="hello world")
    (store.root / art.slug / "comments.json").write_text(
        json.dumps(
            [
                {"id": "bad", "body": "b", "anchor_quote": bad},
                {"id": "ok", "body": "b", "anchor_quote": "world"},
            ]
        ),
        encoding="utf-8",
    )

    store.update(art.slug, content="hello there")

    by_id = {c.id: c for c in store.list_comments(art.slug)}
    assert by_id["ok"].anchor_orphaned is True
    assert by_id["bad"].anchor_orphaned is False
    # The bad quote is left exactly as stored.
    assert by_id["bad"].anchor_quote == bad


def test_rescan_skips_a_non_string_quote_and_flags_the_rest() -> None:
    comments = [
        ArtifactComment(id="bad", anchor_quote=7),  # type: ignore[arg-type]
        ArtifactComment(id="ok", anchor_quote="gone"),
    ]
    assert rescan_anchors(comments, "content", clock=_clock) is True
    assert [c.anchor_orphaned for c in comments] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [7, ["hello"], {"q": "hello"}], ids=["number", "list", "object"])
async def test_the_comment_list_route_answers_over_non_string_anchor_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    from unittest.mock import MagicMock

    from kiro_crew import artifacts as art_mod
    from kiro_crew.dashboard.handlers import artifacts as handlers

    store = ArtifactStore(root=tmp_path / "artifacts")
    art = store.create(name="A", content="hello world")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(handlers, "_is_restricted_session", lambda state, request: False)
    (store.root / art.slug / "comments.json").write_text(
        json.dumps(
            [
                {"id": "bad_quote", "body": "b", "anchor_quote": bad},
                {"id": "bad_context", "body": "b", "anchor_quote": "hello", "anchor_prefix": bad},
                {"id": "ok", "body": "b", "anchor_quote": "world", "anchor_suffix": "!"},
            ]
        ),
        encoding="utf-8",
    )
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = {"slug": art.slug}
    req.query = {}
    req.app = {"state": MagicMock()}

    resp = await handlers.api_artifact_comments(req)

    assert resp.status == 200
    by_id = {c["id"]: c for c in json.loads(resp.body)["comments"]}
    assert "anchor" not in by_id["bad_quote"]
    assert by_id["bad_context"]["anchor"]["quote"] == "hello"
    assert by_id["bad_context"]["anchor"]["prefix"] is None
    assert by_id["ok"]["anchor"]["quote"] == "world"
    assert by_id["ok"]["anchor"]["suffix"] == "!"
