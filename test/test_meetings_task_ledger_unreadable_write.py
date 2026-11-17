"""Filing a task never saves over a task ledger it could not read.

``task-ledger.json`` holds every task the local provider has filed. ``create`` reads it,
appends one entry and writes the list back. When the read answered "empty" for a ledger
that was cut off mid-write, had the wrong shape, or could not be opened, the write
published a one-task ledger over everything that was in it, and the filing still
reported success.

A ledger that does not parse is moved aside to ``task-ledger.json.corrupt-<utc>`` before
the save, so its bytes outlive it. A read that fails refuses the filing and leaves the
file as it is.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov

_LEDGER = "task-ledger.json"
_HELD = {
    "tasks": [
        {"id": "mt-a", "description": "keep a", "state": "open"},
        {"id": "mt-b", "description": "keep b", "state": "open"},
        {"id": "mt-c", "description": "keep c", "state": "open"},
    ],
    "updated_at": "2026-01-01T00:00:00Z",
}


def _ledger(root: Path) -> Path:
    return root / _LEDGER


def _asides(root: Path) -> list[Path]:
    return sorted(root.glob(f"{_LEDGER}.corrupt-*"))


def _file(root: Path, description: str = "new one") -> None:
    taskprov.LocalTaskProvider(root).create(taskprov.TaskDraft(description=description).sanitized())


def _descriptions(root: Path) -> list[str]:
    return [
        t["description"] for t in json.loads(_ledger(root).read_text(encoding="utf-8"))["tasks"]
    ]


class TestCorruptLedgerIsKept:
    def test_a_truncated_ledger_is_moved_aside_before_the_save(self, root: Path):
        raw = json.dumps(_HELD).encode("utf-8")
        truncated = raw[: len(raw) // 2]
        _ledger(root).write_bytes(truncated)

        _file(root)

        asides = _asides(root)
        assert len(asides) == 1
        assert asides[0].read_bytes() == truncated
        assert _descriptions(root) == ["new one"]

    @pytest.mark.parametrize(
        "body",
        [
            b'[{"id": "mt-a", "description": "keep a"}]',
            b'{"items": [{"id": "mt-a", "description": "keep a"}]}',
            b'{"tasks": {"mt-a": {"description": "keep a"}}}',
        ],
        ids=["list", "no-tasks-key", "tasks-not-a-list"],
    )
    def test_json_that_is_not_a_ledger_is_moved_aside(self, root: Path, body: bytes):
        _ledger(root).write_bytes(body)

        _file(root)

        asides = _asides(root)
        assert len(asides) == 1
        assert asides[0].read_bytes() == body
        assert _descriptions(root) == ["new one"]

    def test_a_ledger_that_is_not_utf8_is_moved_aside(self, root: Path):
        body = b'{"tasks": [{"description": "caf\xe9"}]}'
        _ledger(root).write_bytes(body)

        _file(root)

        assert [p.read_bytes() for p in _asides(root)] == [body]


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a file the current user cannot read",
)
class TestUnreadableLedgerIsKept:
    def test_a_ledger_that_cannot_be_opened_refuses_the_filing(self, root: Path):
        _ledger(root).write_text(json.dumps(_HELD), encoding="utf-8")
        before = _ledger(root).read_bytes()
        _ledger(root).chmod(0)
        try:
            with pytest.raises(OSError):
                _file(root)
        finally:
            _ledger(root).chmod(0o600)

        assert _ledger(root).read_bytes() == before
        assert _asides(root) == []


class TestHealthyLedgerStillWorks:
    def test_an_existing_ledger_gains_the_new_task(self, root: Path):
        _ledger(root).write_text(json.dumps(_HELD), encoding="utf-8")

        _file(root)

        assert _descriptions(root) == ["keep a", "keep b", "keep c", "new one"]
        assert _asides(root) == []

    def test_a_missing_ledger_is_created(self, root: Path):
        _file(root)

        assert _descriptions(root) == ["new one"]
        assert _asides(root) == []

    def test_a_ledger_with_a_byte_order_mark_keeps_its_tasks(self, root: Path):
        _ledger(root).write_bytes(b"\xef\xbb\xbf" + json.dumps(_HELD).encode("utf-8"))

        _file(root)

        assert _descriptions(root) == ["keep a", "keep b", "keep c", "new one"]
        assert _asides(root) == []
