"""The internal remote-search route answers an infinite ``limit`` with its default.

``request.json()`` parses ``1e999`` as ``float("inf")``, and ``int(inf)`` raises
``OverflowError``, which the route did not catch, so the call ended in a 500.
The route reads the limit through ``body_int``, which turns an infinite number
into the ``ValueError`` the route already answers with its default of 5, the
same answer NaN and a non-numeric string get.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import knowledge as handler
from kiro_crew.knowledge.connectors import bedrock_kb
from kiro_crew.knowledge.store import KnowledgeStore


def _searched_limit(monkeypatch, tmp_path, limit: object) -> tuple[int, int | None]:
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    req = MagicMock()
    req.app = {}
    marker = {"internal_auth": True}
    req.get = lambda key, default=None: marker.get(key, default)

    async def fake_json():
        return {"query": "q", "limit": limit}

    req.json = fake_json
    seen: dict[str, int] = {}

    def fake_search(_store, _query, n, source_id=None):
        seen["limit"] = n
        return []

    monkeypatch.setattr(handler, "_store", lambda request: store)
    monkeypatch.setattr(bedrock_kb, "search_remote_sources_bounded", fake_search)
    try:
        resp = asyncio.run(handler.remote_search(req))
    except OverflowError as exc:
        pytest.fail(f"limit {limit!r} escaped the route as {exc!r}, which aiohttp serves as a 500")
    assert json.loads(resp.body) == {"results": []}
    return resp.status, seen.get("limit")


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (float("inf"), 5),
        (float("-inf"), 5),
        (10**30, 20),
        (-7, 1),
        (3, 3),
        (float("nan"), 5),
        ("lots", 5),
        (None, 5),
    ],
)
def test_limit_is_bounded_or_defaulted_never_a_500(monkeypatch, tmp_path, limit, expected):
    status, searched = _searched_limit(monkeypatch, tmp_path, limit)
    assert status == 200
    assert searched == expected


def test_json_infinity_reaches_the_handler_as_a_float():
    """The input this guards is real: Python's JSON decoder yields inf for 1e999."""
    assert json.loads('{"limit": 1e999}')["limit"] == float("inf")
