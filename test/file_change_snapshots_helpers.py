"""The fixture and turn helpers the file-change snapshot test modules share.

``test_file_change_snapshots.py``, ``test_file_change_snapshots_turn_budget.py``
and ``test_file_change_snapshots_edits.py`` import what each uses by name: ``short_tmp_dir``
(declared ``@pytest.fixture(name=...)`` and imported under its function name), a
slot with one assistant message, a write announced on its tool call, the events of
a turn that lands, and one real turn through ``run_turn`` with the meta of the row
it persisted.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base
from turn_harness import Raise, SlotSpec, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture(name="short_tmp_dir")
def short_tmp_dir_fixture():
    """A short-path temp dir under ``/tmp``, removed on teardown.

    These tests assert on a file's PATH as it appears in message metadata, and a
    macOS ``tmp_path`` carries high-entropy directory ids that trip
    ``redact_credentials()`` on that field -- so the path has to come from ``/tmp``
    rather than from ``tmp_path``. ``mkdtemp`` registers no finalizer, though, so
    the nine inline calls this replaces each leaked a directory that survived the
    run; ``/tmp`` is not swept per-run the way pytest's own basetemp is.
    """
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "snap-", dir=short_tmp_base()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _make_slot_with_assistant_message() -> _ChatSlot:
    """Build a _ChatSlot with one assistant message ready to receive file_changes."""
    slot = _ChatSlot("test-flush")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)
    return slot


def _write_call(index: int, path: Path, before: str) -> list[AcpEvent]:
    """A write announced with its arguments on the TOOL_CALL itself."""
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=f"call-{index}",
            tool_name="fs_write",
            tool_kind="edit",
            title="Write",
            raw_tool_params={"command": "create", "path": str(path)},
            diff_old_text=before,
            diff_path=str(path),
        )
    ]


_LANDS = [
    AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
]


async def _snapshot_turn(*events: AcpEvent | Raise, seeded: bool = True) -> TurnRecord:
    """One real turn; *seeded* gives the slot an earlier exchange to stay clean."""
    rows = [("user", "earlier"), ("assistant", "earlier answer")] if seeded else []
    return await run_turn(TurnScript(events=list(events)), slot=SlotSpec(rows=rows))


def _persisted_meta(record: TurnRecord, content: str) -> dict:
    """The meta of the last PERSISTED assistant row whose text is *content*."""
    [*_, row] = [row for row in record.rows("assistant") if row["content"] == content]
    return row.get("meta") or {}
