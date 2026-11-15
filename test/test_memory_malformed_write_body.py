"""Malformed memory write bodies are refused with 400 before anything is written.

A non-string semantic ``key`` raised inside the key validator as a 500, and a null
or non-list import collection raised inside ``import_memory`` -- after the earlier
collection's rows were already saved.
"""

from __future__ import annotations

import pytest
from handlers_memory_coverage_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _body,
    _fake_sel,
    _make_request,
    _make_state,
    _reset_setup_status,
    _store,
)

import kiro_crew.dashboard.handlers.memory as mem_mod


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [5, ["fact.a"], {"a": 1}, True])
async def test_semantic_write_refuses_a_non_string_key(key: object) -> None:
    store = _store()
    state = _make_state(vector_store=store)
    req = _make_request(
        state, method="PUT", json_body={"key": key, "value": "v"}, session_key="dashboard:ui"
    )
    assert (await mem_mod.api_memory_semantic_write(req)).status == 400
    store.set_semantic.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"semantic": None},
        {"semantic": 5},
        {"episodic": {"text": "x"}},
        {"semantic": [{"key": "fact.a", "value": "v"}], "episodic": None},
    ],
)
async def test_import_refuses_a_collection_that_is_not_a_list(payload: dict) -> None:
    store = _store()
    state = _make_state(vector_store=store)
    req = _make_request(state, method="POST", json_body=payload, session_key="dashboard:ui")
    resp = await mem_mod.api_memory_import(req)
    assert resp.status == 400
    body = _body(resp)
    assert body["code"] == "import_collection_not_list"
    # Refused before any write: nothing was saved, and the answer says so.
    assert (body["semantic"], body["episodic"]) == (0, 0)
    store.import_memory.assert_not_called()


@pytest.mark.asyncio
async def test_import_still_takes_an_absent_collection() -> None:
    store = _store()
    store.import_memory.return_value = {"semantic": 1, "episodic": 0, "skipped": 0}
    state = _make_state(vector_store=store)
    req = _make_request(
        state, method="POST", json_body={"semantic": [{"key": "k"}]}, session_key="dashboard:ui"
    )
    resp = await mem_mod.api_memory_import(req)
    assert resp.status == 200
    store.import_memory.assert_called_once()
