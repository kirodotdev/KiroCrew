"""The History-resume hydration must restore the agent's NAMESPACE, not just its name.

``_save_slot_to_history`` persists ``agent_kind`` in the same metadata line as
``agent``, and both ``chat_persistence`` loaders read it back under the same
two-value guard.  The resume/import path (``_hydrate_slot_from_history``)
restored the name and dropped the namespace, which costs twice:

* the agent dropdown gets ``activeKind=''`` and falls back to lighting the
  same-name MEMBER row -- the outcome ``chat_persistence``'s own restore comment
  says a template-picked slot must not come back with; and
* the next canonical full save rebuilds ``meta_line`` from the live slot, so a
  now-empty ``agent_kind`` is omitted and the atomic file replace STRIPS the
  recorded namespace from disk -- the same erasure hazard the ``title_origin``
  comment in that function warns about, after which even a restart (whose
  loaders would have restored it) has nothing left to read.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

import kiro_crew.dashboard.chat_handlers as ch


def _resumed(state, key, meta):
    """Materialise a slot from history exactly as the resume handler does."""
    return ch._materialise_slot_from_history(
        state,
        name=key,
        history_key=key,
        meta=meta,
        all_messages=[{"role": "assistant", "content": "a", "ts": ""}],
    )


@pytest.mark.parametrize("kind", ["template", "member"])
def test_resume_restores_the_recorded_agent_kind(tmp_path, monkeypatch, kind):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "dashboard:resume-kind"

    slot = _resumed(state, key, {"agent": "alice", "agent_kind": kind})

    assert slot.agent == "alice"
    # Without the namespace the dropdown's name-only fallback lights the
    # same-name member row, and the next full save drops the field from disk.
    assert slot.agent_kind == kind, (
        f"resume restored the agent name but dropped its namespace: "
        f"agent={slot.agent!r} agent_kind={slot.agent_kind!r} (recorded {kind!r})"
    )


def test_resume_ignores_an_unknown_agent_kind(tmp_path, monkeypatch):
    """Transcript metadata is operator-editable, so only the two known values ride.

    Mirrors the ``in ("member", "template")`` guard both persistence loaders
    apply to the same field.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    slot = _resumed(state, "dashboard:resume-kind-bogus", {"agent": "alice", "agent_kind": "admin"})

    assert (
        slot.agent_kind == ""
    ), f"an unrecognised namespace was honoured from editable metadata: {slot.agent_kind!r}"


def test_resume_without_a_recorded_kind_leaves_it_empty(tmp_path, monkeypatch):
    """An older transcript recorded no namespace; nothing may be invented for it."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)

    slot = _resumed(state, "dashboard:resume-kind-absent", {"agent": "alice"})

    assert slot.agent == "alice"
    assert slot.agent_kind == ""
