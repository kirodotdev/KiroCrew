"""Tests for v1c-B -- per-conversation state on the session entry.

Covers the additive ``SessionMap`` backing store that will replace the
``slack/handler.py`` module-global thread-state dicts:

* boolean flags (``temporary`` / ``incognito``)
* agent + project overrides

Key properties asserted:
* state survives a reload (it is the point of moving off in-memory globals);
* setting per-conversation state never clobbers ``sid`` / Slack-link fields;
* bare ``thread_ts`` and ``slack:`` keys resolve to the SAME entry
  (canonicalization), so a not-yet-migrated caller and a migrated one agree.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.session_map import SessionMap


def _make_kiro_session(kiro_dir, sid: str) -> None:
    kiro_dir.mkdir(parents=True, exist_ok=True)
    (kiro_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
    (kiro_dir / f"{sid}.jsonl").write_text('{"x":1}\n{"y":2}\n', encoding="utf-8")


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    kiro = tmp_path / "kiro"
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", kiro)
    return tmp_path, kiro


class TestFlags:
    def test_flag_defaults_false(self, patched):
        sm = SessionMap()
        assert sm.get_flag("slack:1.2", "temporary") is False
        assert sm.get_flag("missing", "incognito") is False

    def test_set_and_get_flag(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        assert sm.get_flag("slack:1.2", "temporary") is True
        # other flags remain independent
        assert sm.get_flag("slack:1.2", "incognito") is False

    def test_flag_persists_across_reload(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "incognito", True)
        # fresh instance == reload from disk
        assert SessionMap().get_flag("slack:1.2", "incognito") is True

    def test_clear_flag_removes_it(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        sm.set_flag("slack:1.2", "temporary", False)
        assert sm.get_flag("slack:1.2", "temporary") is False
        # clearing the last flag drops the sub-dict entirely (no accretion)
        raw = json.loads((patched[0] / "session_map.json").read_text())
        assert "flags" not in raw["slack:1.2"]

    def test_clear_flag_on_missing_key_creates_no_entry(self, patched):
        # Clearing a flag on a key that was never stored is a no-op and must
        # NOT materialize a blank entry on disk (phantom-entry accretion).
        sm = SessionMap()
        sm.set_flag("slack:never.stored", "temporary", False)
        assert sm.get_flag("slack:never.stored", "temporary") is False
        path = patched[0] / "session_map.json"
        if path.exists():
            raw = json.loads(path.read_text())
            assert "slack:never.stored" not in raw

    def test_two_flags_coexist(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        sm.set_flag("slack:1.2", "incognito", True)
        sm.set_flag("slack:1.2", "temporary", False)
        # clearing one leaves the other
        assert sm.get_flag("slack:1.2", "temporary") is False
        assert sm.get_flag("slack:1.2", "incognito") is True

    def test_bare_and_namespaced_key_same_entry(self, patched):
        sm = SessionMap()
        sm.set_flag("1.2", "temporary", True)  # bare thread_ts
        assert sm.get_flag("slack:1.2", "temporary") is True  # namespaced read


class TestOverrides:
    def test_agent_override_round_trip(self, patched):
        sm = SessionMap()
        sm.set_agent_override("slack:1.2", "researcher")
        assert sm.get_agent_override("slack:1.2") == "researcher"
        assert SessionMap().get_agent_override("slack:1.2") == "researcher"

    def test_agent_override_clear(self, patched):
        sm = SessionMap()
        sm.set_agent_override("slack:1.2", "researcher")
        sm.set_agent_override("slack:1.2", None)
        assert sm.get_agent_override("slack:1.2") is None

    def test_project_override_round_trip(self, patched):
        sm = SessionMap()
        sm.set_project_override("slack:1.2", "/home/u/proj")
        assert sm.get_project_override("slack:1.2") == "/home/u/proj"
        assert SessionMap().get_project_override("slack:1.2") == "/home/u/proj"

    def test_missing_override_is_none(self, patched):
        sm = SessionMap()
        assert sm.get_agent_override("nope") is None
        assert sm.get_project_override("nope") is None


class TestNoClobber:
    def test_flag_preserves_sid(self, patched):
        tmp, kiro = patched
        _make_kiro_session(kiro, "sid-abc")
        sm = SessionMap()
        sm.set("slack:1.2", "sid-abc")
        sm.set_flag("slack:1.2", "temporary", True)
        # reload: both the live sid and the flag survive together
        sm2 = SessionMap()
        assert sm2.get("slack:1.2") == "sid-abc"
        assert sm2.get_flag("slack:1.2", "temporary") is True

    def test_flag_preserves_slack_link(self, patched):
        sm = SessionMap()
        sm.set_slack_link("slack:1.2", "1.2", "C1")
        sm.set_agent_override("slack:1.2", "researcher")
        sm2 = SessionMap()
        assert sm2.get_slack_link("slack:1.2") == ("1.2", "C1")
        assert sm2.get_agent_override("slack:1.2") == "researcher"
        # reverse index for challenge-redirect resume is intact
        assert sm2.get_session_for_thread("1.2") == "slack:1.2"
