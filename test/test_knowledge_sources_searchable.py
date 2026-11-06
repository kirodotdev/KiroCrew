"""GET /api/knowledge/sources marks each bedrock_kb row ``searchable``.

The dashboard's Live chip reads it: a row can sit under a valid grant and still
be skipped by every search, because its attestation was recorded under the
account an earlier grant confirmed. The field is the search enumeration's own
registration check, read-only, and false on any error.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import aws_consent
from kiro_crew.dashboard.handlers.knowledge import list_sources
from kiro_crew.knowledge.connectors import bedrock_kb
from kiro_crew.knowledge.store import KnowledgeStore

_URI = "bedrock-kb://us-east-1/ABCDEFGHIJ"
_PROPS = {"kb_ids": "ABCDEFGHIJ", "region": "us-east-1", "profile": "team-a"}


@pytest.fixture()
def store(tmp_path, opened):
    return opened(KnowledgeStore(str(tmp_path / "test.db")))


def _grant(account: str) -> None:
    aws_consent.record_grant(
        aws_consent.SERVICE_BEDROCK_KB,
        profile="team-a",
        region="us-east-1",
        account=account,
        arn=f"arn:aws:iam::{account}:user/x",
        granted_at="2026-09-06T00:00:00+00:00",
    )


def _registered_row(store) -> str:
    sid = store.add_source(name="KB", source_type="bedrock_kb", uri=_URI, properties=_PROPS)
    aws_consent.record_source_attestation(sid, _URI, _PROPS, expected_account="111122223333")
    return sid


async def _list(store) -> list[dict]:
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    state.owner_id = ""
    app["state"] = state
    app.router.add_get("/api/knowledge/sources", list_sources)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/knowledge/sources")
        assert resp.status == 200, await resp.text()
        return await resp.json()


@pytest.mark.asyncio
async def test_a_row_registered_under_the_current_grant_is_searchable(store):
    _grant("111122223333")
    _registered_row(store)
    rows = await _list(store)
    assert [r["searchable"] for r in rows] == [True]
    assert "searchable_reason" not in rows[0]


@pytest.mark.asyncio
async def test_a_profile_switched_to_another_account_leaves_the_row_unsearchable(store):
    _grant("111122223333")
    _registered_row(store)
    # The profile now resolves to account B and the owner confirmed B: the grant
    # matches the row's (profile, region), but its attestation names account A.
    _grant("444455556666")
    granted, _ = aws_consent.is_granted(
        aws_consent.SERVICE_BEDROCK_KB, profile="team-a", region="us-east-1"
    )
    assert granted is True
    rows = await _list(store)
    assert [r["searchable"] for r in rows] == [False]
    assert rows[0]["searchable_reason"] == "other_account"


@pytest.mark.asyncio
async def test_a_row_the_gateway_never_registered_is_not_offered_a_re_add(store):
    # knowledge.db is agent-writable: a row planted under the granted target
    # with no attestation is unsearchable, but not "added under another account".
    _grant("111122223333")
    store.add_source(name="KB", source_type="bedrock_kb", uri=_URI, properties=_PROPS)
    rows = await _list(store)
    assert rows[0]["searchable"] is False
    assert rows[0]["searchable_reason"] == "unknown"


@pytest.mark.asyncio
async def test_a_registered_row_edited_afterwards_is_not_offered_a_re_add(store):
    _grant("111122223333")
    sid = _registered_row(store)
    _grant("444455556666")
    store.db.execute(
        "UPDATE sources SET properties = ? WHERE id = ?",
        ('{"kb_ids": "PLANTED123", "region": "us-east-1", "profile": "team-a"}', sid),
    )
    store.db.commit()
    rows = await _list(store)
    assert rows[0]["searchable"] is False
    assert rows[0]["searchable_reason"] == "unknown"


@pytest.mark.asyncio
async def test_an_unreadable_consent_store_fails_closed(store, monkeypatch):
    _grant("111122223333")
    _registered_row(store)

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(aws_consent, "bedrock_authorization_snapshot", boom)
    monkeypatch.setattr(aws_consent, "recorded_source_attestations", boom)
    rows = await _list(store)
    assert [r["searchable"] for r in rows] == [False]
    assert rows[0]["searchable_reason"] == "unknown"


@pytest.mark.asyncio
async def test_only_bedrock_rows_carry_the_field(store, tmp_path):
    store.add_source(name="notes", source_type="local_folder", uri=str(tmp_path), properties={})
    rows = await _list(store)
    assert "searchable" not in rows[0]
    assert "searchable_reason" not in rows[0]


_LAST = {"outcome": "refused", "at": "2026-11-06T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_a_searchable_row_carries_its_last_search(store, monkeypatch):
    _grant("111122223333")
    sid = _registered_row(store)
    monkeypatch.setattr(bedrock_kb, "_last_searches", {sid: _LAST})
    rows = await _list(store)
    assert rows[0]["last_search"] == _LAST


@pytest.mark.asyncio
async def test_an_unsearchable_row_shows_no_last_search(store, monkeypatch):
    # Recorded before the profile moved to another account: no search reaches
    # the row now, so the old outcome would describe a search that cannot run.
    _grant("111122223333")
    sid = _registered_row(store)
    _grant("444455556666")
    monkeypatch.setattr(bedrock_kb, "_last_searches", {sid: _LAST})
    rows = await _list(store)
    assert rows[0]["searchable"] is False
    assert "last_search" not in rows[0]
