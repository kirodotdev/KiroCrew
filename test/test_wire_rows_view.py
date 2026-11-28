"""``WireRows`` reads a stored row exactly as ``_prepare_messages`` sends it.

The Regenerate scan judges rows through this view because the dashboard judges
the wire rows, not the stored bytes; each case compares the view with
``_prepare_messages`` on the keys the hidden-reply rule reads.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard.hidden_reply import is_hidden_reply_at
from kiro_crew.dashboard.wire_rows import WireRows
from kiro_crew.testing.synthetic_credentials import github_classic

_TOKEN = github_classic()


def _shown(row: dict) -> dict:
    [out] = cu._prepare_messages([row], False, live_child="")
    return out


def test_a_row_without_content_has_none_on_either_side() -> None:
    row = {"role": "assistant", "cls": "msg msg-a"}
    assert "content" not in WireRows([row])[0]
    assert "content" not in _shown(row)


@pytest.mark.parametrize(
    "content",
    [
        None,
        {"text": "a structured legacy row"},
        f"[STEERING steer-0a1b: retried with {_TOKEN}]",
        f"see {_TOKEN} here",
    ],
    ids=["none", "dict", "note-with-credential", "prose-with-credential"],
)
def test_content_is_what_the_wire_sends(content: object) -> None:
    row = {"role": "assistant", "content": content, "cls": "msg msg-a"}
    assert WireRows([row])[0]["content"] == _shown(row)["content"]
    assert _TOKEN not in str(WireRows([row])[0]["content"])


def test_user_content_stays_raw_on_either_side() -> None:
    row = {"role": "user", "content": f"my key is {_TOKEN}", "cls": "msg msg-u"}
    assert WireRows([row])[0]["content"] == _shown(row)["content"]


@pytest.mark.parametrize(
    ("cls", "stored"),
    [
        ("{}", {"file_changes": [{"path": "notes.md"}]}),
        (json.dumps({"file_changes": [{"path": "a.md"}]}), None),
        ("msg msg-a", {"file_changes": [{"path": "b.md"}]}),
    ],
    ids=["empty-json-cls-replaces-stored", "json-cls", "plain-cls-keeps-stored"],
)
def test_meta_follows_the_wire_on_file_changes(cls: str, stored: dict | None) -> None:
    row: dict = {"role": "assistant", "content": "\u200b", "cls": cls}
    if stored is not None:
        row["meta"] = stored
    view_meta = WireRows([row])[0].get("meta") or {}
    wire_meta = _shown(row).get("meta") or {}
    assert isinstance(view_meta, dict)
    assert view_meta.get("file_changes") == wire_meta.get("file_changes")


def test_variants_are_what_the_wire_sends() -> None:
    row = {
        "role": "assistant",
        "content": "\u200b",
        "cls": "msg msg-a",
        "variants": [
            {"content": "An earlier answer."},
            {"content": f"leaked {_TOKEN}"},
            "not a dict",
        ],
    }
    view = WireRows([row])[0]["variants"]
    assert isinstance(view, list)
    assert [v["content"] for v in view] == [v["content"] for v in _shown(row)["variants"]]


class _ContentTripwire(dict):
    """A stored row whose ``content`` fails the test if anything reads it."""

    def _trip(self, key: object) -> None:
        if key == "content":
            raise AssertionError("the walk projected a row it only needed the role of")

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        self._trip(key)
        return super().get(key, default)

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        self._trip(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._trip(key)
        return super().__contains__(key)


def test_the_walk_projects_no_row_whose_text_it_does_not_need() -> None:
    rows = [
        {"role": "user", "content": "draft the plan", "cls": "msg msg-u"},
        {"role": "assistant", "content": "Stage one ships Monday.", "cls": "msg msg-a"},
        _ContentTripwire(role="tool", content="huge tool output", cls="msg msg-t"),
        {
            "role": "inject",
            "content": "[Tool blocked — reason sent to the agent] no",
            "cls": "msg msg-inject",
        },
        {"role": "assistant", "content": "[STEERING steer-0a: ok]", "cls": "msg msg-a"},
    ]
    assert is_hidden_reply_at(WireRows(rows), 4) is True
    # The tripwire is live: projecting that row's text does fail.
    with pytest.raises(AssertionError, match="projected"):
        WireRows(rows)[2]["content"]
