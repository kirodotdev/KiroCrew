# SPDX-License-Identifier: Apache-2.0
"""A regenerated reply's engine sub-agent record moves with its variant.

The record (``meta.engine_subagents``) is put on the reply row at its turn's end. A
Regenerate stashes the row as a variant (``variant_from_row``) and the new reply
lists its own variant when its text is written, before its record is put on the
row; a switch moves the row onto a variant (``adopt_variant_text``). Each version
shows its own record, and a version that ran no engine sub-agent shows none.
"""

from __future__ import annotations

import copy
from typing import Any

from kiro_crew.dashboard.chat_utils_display import adopt_variant_text, variant_from_row
from kiro_crew.dashboard.engine_subagent_record import RECORD_META_KEY


def _record(task: str) -> dict[str, Any]:
    return {"children": [{"agent": "kirocrew-helper", "task": task, "end": "completed"}]}


def _regenerated(old_meta: dict[str, Any], new_meta: dict[str, Any]) -> dict[str, Any]:
    """The new reply row after a Regenerate of a reply with *old_meta*, as committed."""
    old_row = {"role": "assistant", "content": "old answer", "ts": "t1", "meta": old_meta}
    stashed = variant_from_row(old_row)
    # The new reply's own variant is listed when its text is written (the segment
    # flush): text and timestamp only. Its turn's record lands on the row later.
    newest = {"content": "new answer", "ts": "t2"}
    return {
        "role": "assistant",
        "content": "new answer",
        "ts": "t2",
        "meta": new_meta,
        "variants": [stashed, newest],
        "variant_idx": 1,
    }


def _switch(row: dict[str, Any], idx: int) -> None:
    """What the switch-variant route does to the row."""
    adopt_variant_text(row, row["variants"][idx])
    row["variant_idx"] = idx


def _shown_record(row: dict[str, Any]) -> Any:
    return (row.get("meta") or {}).get(RECORD_META_KEY)


def test_each_version_shows_its_own_record() -> None:
    row = _regenerated(
        {RECORD_META_KEY: _record("old task")}, {RECORD_META_KEY: _record("new task")}
    )

    _switch(row, 0)
    assert row["content"] == "old answer"
    assert _shown_record(row) == _record("old task")

    _switch(row, 1)
    assert row["content"] == "new answer"
    assert _shown_record(row) == _record("new task")


def test_a_version_that_ran_no_engine_sub_agent_shows_none() -> None:
    row = _regenerated({}, {RECORD_META_KEY: _record("new task")})

    _switch(row, 0)
    assert _shown_record(row) is None

    _switch(row, 1)
    assert _shown_record(row) == _record("new task")

    old = _regenerated({RECORD_META_KEY: _record("old task")}, {})
    _switch(old, 1)
    assert _shown_record(old) is None
    _switch(old, 0)
    assert _shown_record(old) == _record("old task")


def test_with_no_record_the_variant_dicts_are_as_before() -> None:
    """Preview off, or a turn with no engine child: nothing gains the key."""
    row = _regenerated({"turn_stats": {"model": "m"}}, {})
    assert RECORD_META_KEY not in row["variants"][0]
    before = copy.deepcopy(row["variants"])

    _switch(row, 0)
    _switch(row, 1)

    assert row["variants"] == before
    assert all(RECORD_META_KEY not in v for v in row["variants"])
    assert _shown_record(row) is None


_PASSWORD = "Pw" + "6" * 40
_SECRET_TASK = f"Migrate with postgres://admin:{_PASSWORD}@db.internal/app then report."


def _emitted(row: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.dashboard import chat_utils as cu

    [out] = cu._prepare_messages([row], False, live_child="")
    return out


def _secret_row() -> dict[str, Any]:
    row = _regenerated({RECORD_META_KEY: _record(_SECRET_TASK)}, {RECORD_META_KEY: _record("new")})
    # The row shows the older version, so both the row and a variant carry the secret.
    _switch(row, 0)
    return row


def test_a_teammate_reads_a_variants_record_redacted_as_the_rows() -> None:
    row = _secret_row()
    out = _emitted(row)

    shown_row = out["meta"][RECORD_META_KEY]
    shown_variant = out["variants"][0][RECORD_META_KEY]
    assert _PASSWORD not in str(shown_row) and _PASSWORD not in str(shown_variant)
    # The same redaction, not a second one: the variant's record reads as the row's.
    assert shown_variant == shown_row
    # The newest version's record, a variant now, leaves unchanged by a pass with
    # nothing to remove.
    assert out["variants"][1][RECORD_META_KEY] == _record("new")
    # The stored rows are untouched.
    assert _PASSWORD in str(row["variants"][0][RECORD_META_KEY])


def test_the_owners_own_view_reads_a_variants_record_as_the_rows() -> None:
    from kiro_crew.security import redaction_switch

    row = _secret_row()
    with redaction_switch.owner_view():
        out = _emitted(row)

    assert out["meta"][RECORD_META_KEY] == _record(_SECRET_TASK)
    assert out["variants"][0][RECORD_META_KEY] == _record(_SECRET_TASK)


def test_with_no_record_the_emitted_variants_are_as_before() -> None:
    row = _regenerated({"turn_stats": {"model": "m"}}, {})
    out = _emitted(row)
    assert all(RECORD_META_KEY not in v for v in out["variants"])
