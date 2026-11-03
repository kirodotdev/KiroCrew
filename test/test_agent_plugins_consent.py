"""The owner's on/off record for Agent Plugins, and the ``on`` state.

Covers the record's fail-closed reads, its by-name shape (including a record an
earlier build wrote with extra fields per row), how discovery turns the record
into ``on``, and where the sandbox puts ``agent-plugins.json``.
"""

from __future__ import annotations

import json
import os

import pytest
from agent_plugins_helpers import install

from kiro_crew import sandbox
from kiro_crew.agent_plugins import consent, discover_plugins
from kiro_crew.security import paths_lists


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


class TestRecord:
    def test_absent_means_nothing_on(self, crew_home):
        assert consent.enabled() == frozenset()

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "{}",
            "not json{",
            "[]",
            json.dumps({"version": 2, "enabled": {"p": {}}}),
            json.dumps({"version": 1, "enabled": ["p"]}),
            json.dumps({"version": 1, "enabled": {"p": True}}),
        ],
    )
    def test_empty_corrupt_or_malformed_means_nothing_on(self, crew_home, raw):
        consent.consent_path().write_text(raw, encoding="utf-8")
        assert consent.enabled() == frozenset()

    def test_round_trip_and_off(self, crew_home):
        consent.set_enabled("p", True)
        consent.set_enabled("q", True)
        assert consent.enabled() == {"p", "q"}
        consent.set_enabled("p", False)
        assert consent.enabled() == {"q"}

    def test_the_record_is_keyed_by_name_only(self, crew_home):
        consent.set_enabled("p", True)
        stored = json.loads(consent.consent_path().read_text(encoding="utf-8"))
        assert stored == {"version": 1, "enabled": {"p": {}}}

    def test_a_row_with_extra_fields_still_reads_as_on(self, crew_home):
        doc = {"version": 1, "enabled": {"p": {"note": "written by an earlier build"}}}
        consent.consent_path().write_text(json.dumps(doc), encoding="utf-8")
        assert consent.enabled() == {"p"}
        consent.set_enabled("q", True)
        stored = json.loads(consent.consent_path().read_text(encoding="utf-8"))
        assert stored == {"version": 1, "enabled": {"p": {}, "q": {}}}

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
    def test_the_record_is_owner_only(self, crew_home):
        consent.set_enabled("p", True)
        assert os.stat(consent.consent_path()).st_mode & 0o077 == 0

    def test_a_corrupt_record_is_kept_aside_before_a_write(self, crew_home):
        consent.consent_path().write_text("not json{", encoding="utf-8")
        consent.set_enabled("p", True)
        kept = [p.name for p in crew_home.iterdir() if ".corrupt-" in p.name]
        assert len(kept) == 1
        assert consent.enabled() == {"p"}


class TestDiscoveryStates:
    def test_unrecorded_plugin_stays_off(self, tmp_path, crew_home):
        install(tmp_path, "p")
        assert [r.state for r in discover_plugins(tmp_path)] == ["off"]

    def test_recorded_plugin_is_on(self, tmp_path, crew_home):
        install(tmp_path, "p")
        consent.set_enabled("p", True)
        assert [r.state for r in discover_plugins(tmp_path)] == ["on"]

    def test_an_edit_after_turning_on_keeps_it_on(self, tmp_path, crew_home):
        root = install(tmp_path, "p")
        consent.set_enabled("p", True)
        (root / "extra.txt").write_text("x", encoding="utf-8")
        (rec,) = discover_plugins(tmp_path)
        assert rec.state == "on" and rec.reason is None

    def test_an_invalid_plugin_never_becomes_on(self, tmp_path, crew_home):
        install(tmp_path, "p", manifest={"name": "p"})
        consent.set_enabled("p", True)
        assert [r.state for r in discover_plugins(tmp_path)] == ["invalid"]

    def test_discovery_does_not_write_the_record(self, tmp_path, crew_home):
        install(tmp_path, "p")
        discover_plugins(tmp_path)
        assert not consent.consent_path().exists()


class TestSandboxPlacement:
    """``agent-plugins.json`` sits in every list ``aws_service_consent.json`` does,
    except the harness-child read classification: the launcher reads it there."""

    LEAF = consent.CONSENT_LEAF

    def test_sealed_read_only_for_sandboxed_code(self):
        assert self.LEAF in sandbox._CREW_READONLY_LEAVES

    def test_precreated_so_an_absent_record_is_sealed_too(self):
        assert self.LEAF in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert self.LEAF not in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES

    def test_fenced_from_the_agent_file_tools(self):
        assert self.LEAF in paths_lists._CREW_SECRET_LEAVES

    def test_readable_by_a_harness_child_for_the_launcher(self):
        assert self.LEAF in sandbox._CREW_CHILD_READABLE_LEAVES
        assert self.LEAF not in sandbox._CREW_CHILD_WITHHELD_LEAVES

    def test_an_empty_precreated_record_reads_as_nothing_on(self, crew_home):
        consent.consent_path().write_bytes(sandbox._EMPTY_CEILING_DOCUMENT)
        assert consent.enabled() == frozenset()
