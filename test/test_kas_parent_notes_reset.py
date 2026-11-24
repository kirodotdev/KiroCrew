"""The test floor clears the noted sub-agent run parents around every test.

``kas_parent_limits._NOTED`` is filled by every manager run (``note_parent``) and
keyed by run key, which tests reuse. ``_reset_kas_parent_notes`` in
``conftest_resets.py`` is the floor's guard for it; the conftest's seal refuses a
guard it does not bind, so these tests drive the guard itself.
"""

from __future__ import annotations

import sys

from conftest_resets import _reset_kas_parent_notes

from kiro_crew.acp import kas_parent_limits


def test_a_parent_noted_by_an_earlier_test_is_gone_at_setup_and_after_teardown():
    kas_parent_limits.note_parent("subagent:reused", "dashboard:earlier-test")
    guard = _reset_kas_parent_notes()
    next(guard)
    assert kas_parent_limits._parent_keys("subagent:reused") == ()
    kas_parent_limits.note_parent("subagent:reused", "dashboard:this-test")
    for _ in guard:
        pass
    assert not kas_parent_limits._NOTED


def test_a_directory_noted_for_a_parent_is_gone_at_setup_and_after_teardown(tmp_path):
    earlier, this = str(tmp_path / "earlier"), str(tmp_path / "this")
    kas_parent_limits.note_parent("subagent:reused", "subagent:above", earlier)
    assert kas_parent_limits._unresolved_dirs("subagent:above", this) == (earlier, "")
    guard = _reset_kas_parent_notes()
    next(guard)
    assert kas_parent_limits._unresolved_dirs("subagent:above", this) == ("",)
    kas_parent_limits.note_parent("subagent:reused", "subagent:above", this)
    for _ in guard:
        pass
    assert not kas_parent_limits._NOTED_DIRS


def test_the_guard_never_imports_the_module(monkeypatch):
    # A None entry makes an import in this window raise instead of building a
    # second module, and monkeypatch puts the real module back at teardown.
    monkeypatch.setitem(sys.modules, "kiro_crew.acp.kas_parent_limits", None)
    for _ in _reset_kas_parent_notes():
        pass
    assert sys.modules["kiro_crew.acp.kas_parent_limits"] is None
