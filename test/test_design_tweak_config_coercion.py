"""Design Tweak keeps serving when its stored registry or a draft holds a wrong-typed field.

``config.json`` and the queue records are plain files on disk. A field with the
wrong JSON type (``"projects": null``, a project row with no ``id``, a word for
``counter``, ``"comments": null`` in a draft) must read as that field's default,
not raise out of every route that indexes it. Valid values pass through as-is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from design_tweak_backend_helpers import isolated_queue_fixture  # noqa: F401
from design_tweak_backend_helpers import _JsonHandler

from kiro_crew.apps.builtins.design_tweak.backend import request_state, server


def _load(monkeypatch, tmp_path: Path, stored: object) -> dict:
    data = tmp_path / "cfg"
    data.mkdir()
    (data / "config.json").write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", data)
    config = request_state.load_config(server)
    monkeypatch.setattr(server, "_CFG", config)
    return config


@pytest.mark.parametrize(
    "stored",
    [
        {"projects": None},
        {"projects": "p1"},
        {"projects": [1, "x", None]},
        {"projects": [{"name": "no id"}], "activeId": None},
        {"projects": [{"id": ["p1"], "path": "/x"}], "activeId": ["p1"]},
        {"projects": [{"id": True, "path": "/x"}], "activeId": {"p": 1}},
        {"projects": [{"id": "p1"}], "activeId": "p1"},
        {"projects": [{"id": "p1", "path": 3}], "activeId": "p1"},
    ],
)
def test_wrong_typed_projects_read_as_an_empty_registry(monkeypatch, tmp_path, stored):
    config = _load(monkeypatch, tmp_path, stored)
    assert config["projects"] == []
    assert isinstance(config["activeId"], str)
    assert server._active_project() is None
    assert server._project_by_id("p1") is None


def test_an_int_project_id_keeps_its_row(monkeypatch, tmp_path):
    """An int id compares and keys like a string id, so its row and the active pointer stay."""
    row = {"id": 7, "path": "/x", "name": "x"}
    config = _load(monkeypatch, tmp_path, {"projects": [row], "activeId": 7})
    assert config["projects"] == [row]
    assert server._active_project() == row


@pytest.mark.parametrize("counter", ["x", None, [1], {"n": 1}, 1e999, "nan"])
def test_unconvertible_counter_restarts_the_legacy_numbering(monkeypatch, tmp_path, counter):
    monkeypatch.setattr(server, "_save_cfg", lambda cfg: None)
    data = tmp_path / "cfg"
    data.mkdir()
    # Written raw so the non-finite float reaches the reader as JSON ``Infinity``.
    (data / "config.json").write_text(json.dumps({"counter": counter}), encoding="utf-8")
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "_CFG", request_state.load_config(server))
    assert server._CFG["counter"] == 0
    assert server._next_number() == 1


@pytest.mark.parametrize(("counter", "expected"), [("5", 6), (4.0, 5), (True, 2), (-3, 1)])
def test_convertible_counter_keeps_its_number(monkeypatch, tmp_path, counter, expected):
    monkeypatch.setattr(server, "_save_cfg", lambda cfg: None)
    _load(monkeypatch, tmp_path, {"counter": counter})
    assert server._next_number() == expected


def test_valid_registry_passes_through_unchanged(monkeypatch, tmp_path):
    stored = {
        "projects": [
            {"id": "p1", "path": "/a", "name": "a", "previewUrl": "http://127.0.0.1:1"},
            {"id": "p2", "path": "/b", "name": "b"},
        ],
        "activeId": "p2",
        "counter": 4,
        "extra": {"kept": True},
    }
    config = _load(monkeypatch, tmp_path, stored)
    assert config == stored
    assert server._active_project() == stored["projects"][1]


def test_usable_rows_survive_beside_unusable_ones(monkeypatch, tmp_path):
    good = {"id": "p1", "path": "/a", "name": "a"}
    config = _load(monkeypatch, tmp_path, {"projects": [good, {"name": "bad"}, 3]})
    assert config["projects"] == [good]
    assert server._project_by_id("p1") == good


def _submit(project_id: str, text: str) -> _JsonHandler:
    payload = {
        "type": "visual_edit_request",
        "comment": text,
        "projectId": project_id,
        "selection": {"mode": "single", "elements": [{"tag": "div", "id": text}]},
    }
    handler = _JsonHandler(body=json.dumps(payload).encode("utf-8"))
    handler._h_submit()
    return handler


@pytest.mark.parametrize("stored_comments", [None, "x", {"cid": "c"}, [1, "x", None]])
def test_draft_with_non_list_comments_takes_the_next_comment(isolated_queue, stored_comments):
    first = _submit("p-null-comments", "one")
    assert first.json_sent[0][0] == 200
    fp = next(iter(isolated_queue.glob("*.json")))
    record = json.loads(fp.read_text(encoding="utf-8"))
    record["comments"] = stored_comments
    record["state"] = "draft"
    fp.write_text(json.dumps(record), encoding="utf-8")

    second = _submit("p-null-comments", "two")
    assert second.json_sent[0][0] == 200, second.json_sent
    comments = json.loads(fp.read_text(encoding="utf-8"))["comments"]
    assert isinstance(comments, list)
    assert [c["comment"] for c in comments][-1:] == ["two"]


@pytest.mark.parametrize("stored_comments", [None, "x", []])
def test_sent_record_with_no_readable_comments_stays_sealed(isolated_queue, stored_comments):
    """A sealed record must not reopen as a draft and swallow the next comment."""
    assert _submit("p-sealed", "one").json_sent[0][0] == 200
    sealed = next(iter(isolated_queue.glob("*.json")))
    record = json.loads(sealed.read_text(encoding="utf-8"))
    record["state"] = "sent"
    record["comments"] = stored_comments
    sealed.write_text(json.dumps(record), encoding="utf-8")

    assert _submit("p-sealed", "two").json_sent[0][0] == 200
    assert json.loads(sealed.read_text(encoding="utf-8"))["comments"] == stored_comments
    drafts = [fp for fp in isolated_queue.glob("*.json") if fp != sealed]
    assert len(drafts) == 1, "the comment opens a fresh draft"
    comments = json.loads(drafts[0].read_text(encoding="utf-8"))["comments"]
    assert [c["comment"] for c in comments] == ["two"]


def test_dropped_comment_entries_leave_no_repeated_index(isolated_queue):
    assert _submit("p-index", "one").json_sent[0][0] == 200
    fp = next(iter(isolated_queue.glob("*.json")))
    record = json.loads(fp.read_text(encoding="utf-8"))
    first = record["comments"][0]
    third = {**first, "cid": "c3", "index": 3}
    record["comments"] = [first, "junk", third]
    record["state"] = "draft"
    fp.write_text(json.dumps(record), encoding="utf-8")

    assert _submit("p-index", "two").json_sent[0][0] == 200
    indexes = [c["index"] for c in json.loads(fp.read_text(encoding="utf-8"))["comments"]]
    assert indexes == [1, 2, 3]


def test_reset_counter_still_numbers_past_existing_unscoped_requests(
    isolated_queue, monkeypatch, tmp_path
):
    """A counter reset to 0 must not hand out a number an unscoped record already holds."""
    monkeypatch.setattr(server, "_save_cfg", lambda cfg: None)
    (isolated_queue / "old.json").write_text(json.dumps({"number": 4}), encoding="utf-8")
    (server.HANDLED_DIR / "older.json").write_text(
        json.dumps({"number": 9, "projectId": ""}), encoding="utf-8"
    )
    (isolated_queue / "scoped.json").write_text(
        json.dumps({"number": 50, "projectId": "p1"}), encoding="utf-8"
    )
    data = server.DATA_DIR
    (data / "config.json").write_text(json.dumps({"counter": "x"}), encoding="utf-8")
    monkeypatch.setattr(server, "_CFG", request_state.load_config(server))
    assert server._next_number() == 10
    assert server._next_number() == 11
