"""Session control create: a doc or board conductor opening another page.

Covers ``sc.create_session(doc_kind=..., content=...)`` through
``dashboard.doc_child``: the child is bound to a fresh doc id of its
kind at birth, its starting file is written (a doc with a random icon and cover),
its birth metadata carries the binding, and the refusals: an unknown kind, a caller
that is not a page, a chain past the depth cap, a board whose content is not its
JSON. Each refusal creates nothing.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _agent_resolves,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import doc_child, doc_files
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _docs_home(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_files, "data_home", lambda: tmp_path)
    # A page opens on the worker template, which this home has no file for.
    _agent_resolves(monkeypatch, "default")
    # The goal sync a new page schedules is not what these tests are about.
    monkeypatch.setattr("kiro_crew.dashboard.doc_goal_loop.schedule_goal_sync", lambda *a: None)


def _page(state, key: str, kind: str = "board"):
    slot = _slot(state, key)
    slot._doc = doc_files.mint_doc_id(kind)
    return slot


def _create(state, caller, **kw):
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller), **kw))


def test_a_board_conductor_opens_a_doc_with_its_text_an_icon_and_a_cover(tmp_path):
    state = _make_state(tmp_path)
    board = _page(state, "chat-1", "board")
    created = _create(
        state, board, title="Issue #42", doc_kind="doc", content="# Issue #42\n\nFix it.\n"
    )
    child = state.get_slot(created["target"])
    assert created["doc_kind"] == "doc" and created["doc"] == child._doc
    assert doc_files.doc_kind(child._doc) == "doc"
    assert child._created_by == board.key
    text = doc_files.read_doc(child._doc).content
    assert text.startswith("---\nicon: ")
    assert "\ncover: gradient:" in text
    assert text.endswith("---\n# Issue #42\n\nFix it.\n")
    # The binding is on disk, so an idle newborn page comes back a page after a restart.
    assert state.conversation_log.get_metadata(slot_history_key(child)).get("doc") == child._doc


def test_a_doc_conductor_opens_a_board_with_its_json_as_written(tmp_path):
    state = _make_state(tmp_path)
    doc = _page(state, "chat-1", "doc")
    board_json = json.dumps({"shapes": [{"id": "s1", "kind": "sticky", "text": "Step 1"}]})
    created = _create(state, doc, doc_kind="board", content=board_json)
    child = state.get_slot(created["target"])
    assert doc_files.doc_kind(child._doc) == "board"
    assert doc_files.read_doc(child._doc).content == board_json


def test_a_doc_with_its_own_front_matter_keeps_it(tmp_path):
    state = _make_state(tmp_path)
    board = _page(state, "chat-1")
    created = _create(state, board, doc_kind="doc", content="---\nicon: 🐛\n---\n# Bug\n")
    assert doc_files.read_doc(created["doc"]).content == "---\nicon: 🐛\n---\n# Bug\n"


def test_a_page_with_no_content_starts_empty_but_named(tmp_path):
    state = _make_state(tmp_path)
    board = _page(state, "chat-1")
    created = _create(state, board, doc_kind="board")
    assert doc_files.read_doc(created["doc"]).content == ""
    assert (
        doc_files.read_doc(created["doc"]).version != ""
    ), "the file exists, so the page opens as itself"


@pytest.mark.parametrize(
    ("kind", "content", "code"),
    [
        ("codebase", None, "invalid_doc_kind"),
        ("", "# text with no kind", "invalid_doc_kind"),
        ("board", "not json", "invalid_board_content"),
        ("board", "[1, 2]", "invalid_board_content"),
    ],
)
def test_a_bad_page_request_is_refused_and_creates_nothing(tmp_path, kind, content, code):
    state = _make_state(tmp_path)
    board = _page(state, "chat-1")
    before = set(state._slots)
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, board, doc_kind=kind, content=content)
    assert refused.value.code == code
    assert set(state._slots) == before


def test_only_a_page_s_conductor_may_open_a_page(tmp_path):
    state = _make_state(tmp_path)
    chat = _slot(state, "chat-1")
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, chat, doc_kind="doc")
    assert refused.value.code == "doc_child_not_conductor"
    # The same caller still opens an ordinary session.
    assert "doc" not in _create(state, chat)


def test_a_chain_of_conductor_opened_pages_stops_two_below_the_person_s_page(tmp_path):
    state = _make_state(tmp_path)
    person_board = _page(state, "chat-1", "board")
    level1 = state.get_slot(_create(state, person_board, doc_kind="doc")["target"])
    level2 = state.get_slot(_create(state, level1, doc_kind="board")["target"])
    assert doc_child.doc_chain_depth(state, person_board) == 0
    assert doc_child.doc_chain_depth(state, level1) == 1
    assert doc_child.doc_chain_depth(state, level2) == 2
    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, level2, doc_kind="doc")
    assert refused.value.code == "doc_chain_too_deep"
    # Ordinary sessions are not pages, so the cap does not stop workers.
    assert "doc" not in _create(state, level2)


def test_a_page_opened_by_a_chat_counts_from_zero(tmp_path):
    state = _make_state(tmp_path)
    chat = _slot(state, "chat-1")
    page = _page(state, "chat-2")
    page._created_by = chat.key
    assert doc_child.doc_chain_depth(state, page) == 0


def test_the_tool_schema_takes_a_kind_and_content():
    from kiro_crew.validation import SESSION_CREATE_SCHEMA, ValidationError, validate_tool_args

    out = validate_tool_args({"kind": "doc", "content": "# x"}, SESSION_CREATE_SCHEMA)
    assert out["kind"] == "doc" and out["content"] == "# x"
    with pytest.raises(ValidationError):
        validate_tool_args({"kind": "codebase"}, SESSION_CREATE_SCHEMA)
    with pytest.raises(ValidationError):
        validate_tool_args({"content": "x" * 200_001}, SESSION_CREATE_SCHEMA)


def test_a_page_runs_on_the_worker_unless_the_conductor_names_an_agent(tmp_path):
    state = _make_state(tmp_path)
    board = _page(state, "chat-1")
    board.agent = "kirocrew-conductor"
    plain = state.get_slot(_create(state, board, doc_kind="doc")["target"])
    assert plain.agent == "kirocrew-worker" and plain.agent_kind == "template"
    chat = state.get_slot(_create(state, board)["target"])
    assert (
        chat.agent == "kirocrew-conductor"
    ), "an ordinary session still inherits the caller's agent"
