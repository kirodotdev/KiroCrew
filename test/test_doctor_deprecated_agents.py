"""Tests for the `kirocrew doctor` deprecated-agent-spec check.

Guards the retirement contract from rfc-conductor-work-ledger.md ("What
retired means for the name"): a config surface that persists an agent name --
a cron job, a crew binding, a chat slot -- and still names a deprecated spec
is reported with its replacement, while a config naming only current specs
stays silent. The check is what makes deleting a deprecated alias safe: the
one-release window warns the owners instead of ending in a silent
"Mode not found" at dispatch time.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import cli_doctor, cron
from kiro_crew.agent import DEPRECATED_AGENT_SPECS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.config.sections import KiroCrewAgentConfig

DEPRECATED = "kirocrew-ledger-conductor"
REPLACEMENT = "kirocrew-conductor"


@pytest.fixture()
def no_other_surfaces(monkeypatch):
    """Silence the cron and slot surfaces so a test exercises one at a time."""
    monkeypatch.setattr(cron, "job_agent_names_from_disk", lambda: [])
    monkeypatch.setattr(cli_doctor, "_open_slot_agent_names", lambda: [])


class TestDeprecatedTable:
    def test_ledger_conductor_row_present(self) -> None:
        # The alias installer and this row retire together; the doctor notice
        # is the precondition for deleting the alias.
        assert DEPRECATED_AGENT_SPECS[DEPRECATED] == REPLACEMENT

    def test_values_are_not_themselves_deprecated(self) -> None:
        # A replacement that is itself a key would send a migrating user to a
        # name that is also going away.
        for replacement in DEPRECATED_AGENT_SPECS.values():
            assert replacement not in DEPRECATED_AGENT_SPECS


class TestJobAgentNamesFromDisk:
    def _write_store(self, jobs: list[dict]) -> None:
        path = config_dir() / "crons.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "jobs": jobs}), encoding="utf-8")

    def test_reads_agent_id_and_sequence(self) -> None:
        self._write_store(
            [
                {"id": "a1", "name": "nightly", "message": "x", "agent_id": DEPRECATED},
                {"id": "b2", "name": "relay", "message": "x", "agent_sequence": ["one", "two"]},
                {"id": "c3", "name": "plain", "message": "x"},
            ]
        )
        assert cron.job_agent_names_from_disk() == [
            ("nightly", DEPRECATED),
            ("relay", "one"),
            ("relay", "two"),
        ]

    def test_unnamed_job_uses_placeholder_holder(self) -> None:
        self._write_store([{"id": "", "message": "x", "agent_id": "spec"}])
        assert cron.job_agent_names_from_disk() == [("<unnamed job>", "spec")]

    def test_dispatching_sequence_makes_agent_id_dormant(self) -> None:
        # A sequence of more than one agent is what dispatch runs; agent_id is
        # dormant and must not fail doctor.
        self._write_store(
            [
                {
                    "id": "m1",
                    "name": "multi",
                    "message": "x",
                    "agent_id": DEPRECATED,
                    "agent_sequence": ["one", "two"],
                }
            ]
        )
        assert cron.job_agent_names_from_disk() == [("multi", "one"), ("multi", "two")]

    def test_one_entry_sequence_is_dormant(self) -> None:
        # Dispatch runs a sequence only when it holds more than one agent; a
        # one-entry sequence never dispatches, so its name must not fail
        # doctor.
        self._write_store(
            [{"id": "a1", "name": "solo", "message": "x", "agent_sequence": ["dormant"]}]
        )
        assert cron.job_agent_names_from_disk() == []

    def test_loader_rejected_record_contributes_nothing(self) -> None:
        # The scheduler's own loader rejects a record whose agent_sequence
        # holds a non-string; a job that never loads dispatches nothing.
        self._write_store(
            [
                {"id": "", "message": "x", "agent_id": "spec", "agent_sequence": [1, "ok", "x"]},
                {"id": "b", "name": "bad-id", "message": "x", "agent_id": ["list"]},
            ]
        )
        assert cron.job_agent_names_from_disk() == []

    def test_script_and_command_jobs_are_skipped(self) -> None:
        # A script or command cron bypasses agent dispatch entirely
        # (gateway runs it with no LLM), so its agent fields are dormant.
        self._write_store(
            [
                {
                    "id": "s1",
                    "name": "scripted",
                    "message": "x",
                    "agent_id": DEPRECATED,
                    "script": "~/.kiro/crew/crons/x.py:run",
                },
                {
                    "id": "c1",
                    "name": "cmd",
                    "message": "x",
                    "agent_id": DEPRECATED,
                    "command": "echo hi",
                },
            ]
        )
        assert cron.job_agent_names_from_disk() == []

    def test_gate_predicate_is_shared_with_dispatch(self) -> None:
        # The reader, session-key stability, and the Slack dispatch path must
        # all read the same gate.
        from kiro_crew.slack import gateway

        assert gateway.agent_sequence_dispatches is cron.agent_sequence_dispatches
        assert not cron.agent_sequence_dispatches(["one"])
        assert cron.agent_sequence_dispatches(["one", "two"])

    def test_missing_store_is_empty(self) -> None:
        assert cron.job_agent_names_from_disk() == []

    def test_malformed_store_is_empty(self) -> None:
        path = config_dir() / "crons.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert cron.job_agent_names_from_disk() == []


class TestOpenSlotAgentNames:
    def _write_slot(self, slot_key: str, agent: str) -> None:
        from kiro_crew.dashboard.chat_utils import slot_transcript_key
        from kiro_crew.history import _safe_key, _sessions_dir

        sessions = _sessions_dir()
        sessions.mkdir(parents=True, exist_ok=True)
        meta = {"_type": "metadata", "agent": agent}
        name = _safe_key(slot_transcript_key(slot_key))
        (sessions / f"{name}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")

    def _write_open_slots(self, keys: list[str]) -> None:
        path = config_dir() / "open_slots.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"keys": keys}), encoding="utf-8")

    def test_reads_open_slot_agent(self) -> None:
        self._write_open_slots(["chat-1"])
        self._write_slot("chat-1", DEPRECATED)
        assert cli_doctor._open_slot_agent_names() == [("chat-1", DEPRECATED)]

    def test_channel_born_slot_is_read_from_its_own_transcript(self) -> None:
        # A channel-born tab's slot key (slack_<ts>) already addresses its
        # transcript; a dashboard: prefix would read a nonexistent file and
        # silently skip the tab.
        self._write_open_slots(["slack_1785370133.085469"])
        self._write_slot("slack_1785370133.085469", DEPRECATED)
        assert cli_doctor._open_slot_agent_names() == [("slack_1785370133.085469", DEPRECATED)]

    def test_slot_without_agent_metadata_is_skipped(self) -> None:
        self._write_open_slots(["chat-1"])
        self._write_slot("chat-1", "")
        assert cli_doctor._open_slot_agent_names() == []

    def test_path_separator_key_is_rejected(self) -> None:
        # open_slots.json is attacker-writable; a smuggled key must not reach
        # path construction.
        self._write_open_slots(["../../etc/passwd"])
        assert cli_doctor._open_slot_agent_names() == []

    def test_missing_file_is_empty(self) -> None:
        assert cli_doctor._open_slot_agent_names() == []


class TestDoctorDeprecatedAgentSpecs:
    def test_crew_binding_naming_deprecated_is_flagged(self, no_other_surfaces, capsys) -> None:
        cfg = KiroCrewConfig()
        cfg.agents = {"myteam": KiroCrewAgentConfig(kiro_agent=DEPRECATED)}
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(cfg, issues)
        out = capsys.readouterr().out
        assert "Deprecated Agent Specs" in out
        assert "'myteam'" in out
        assert DEPRECATED in out
        assert REPLACEMENT in out
        assert issues == ["a config names a deprecated agent spec"]

    def test_cron_job_naming_deprecated_is_flagged(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cron, "job_agent_names_from_disk", lambda: [("nightly", DEPRECATED)])
        monkeypatch.setattr(cli_doctor, "_open_slot_agent_names", lambda: [])
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(KiroCrewConfig(), issues)
        out = capsys.readouterr().out
        assert "cron job 'nightly'" in out
        assert REPLACEMENT in out
        assert issues == ["a config names a deprecated agent spec"]

    def test_chat_slot_naming_deprecated_is_flagged(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cron, "job_agent_names_from_disk", lambda: [])
        monkeypatch.setattr(cli_doctor, "_open_slot_agent_names", lambda: [("chat-3", DEPRECATED)])
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(KiroCrewConfig(), issues)
        out = capsys.readouterr().out
        assert "chat slot 'chat-3'" in out
        assert REPLACEMENT in out
        assert issues == ["a config names a deprecated agent spec"]

    def test_job_naming_a_crew_is_reported_once_via_the_crew(self, monkeypatch, capsys) -> None:
        # The job names the CREW; the crew binds the deprecated spec. One
        # finding (the crew's), not two -- fixing the binding fixes the job.
        cfg = KiroCrewConfig()
        cfg.agents = {"myteam": KiroCrewAgentConfig(kiro_agent=DEPRECATED)}
        monkeypatch.setattr(cron, "job_agent_names_from_disk", lambda: [("nightly", "myteam")])
        monkeypatch.setattr(cli_doctor, "_open_slot_agent_names", lambda: [])
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(cfg, issues)
        out = capsys.readouterr().out
        assert out.count("names deprecated agent spec") == 1
        assert "crew 'myteam'" in out
        assert "nightly" not in out

    def test_config_selectors_naming_deprecated_are_flagged(
        self, no_other_surfaces, capsys
    ) -> None:
        from kiro_crew.config.sections import ChannelConfig

        cfg = KiroCrewConfig()
        cfg.agent.default_agent = DEPRECATED
        cfg.session.pool_agent = DEPRECATED
        cfg.slack_channels = {"C123": ChannelConfig(agent=DEPRECATED)}
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(cfg, issues)
        out = capsys.readouterr().out
        assert "agent.default_agent" in out
        assert "session.pool_agent" in out
        assert "slack channel 'C123'" in out
        assert out.count("names deprecated agent spec") == 3
        assert issues == ["a config names a deprecated agent spec"]

    def test_default_agent_naming_a_crew_is_reported_once_via_the_crew(
        self, no_other_surfaces, capsys
    ) -> None:
        cfg = KiroCrewConfig()
        cfg.agents = {"myteam": KiroCrewAgentConfig(kiro_agent=DEPRECATED)}
        cfg.agent.default_agent = "myteam"
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(cfg, issues)
        out = capsys.readouterr().out
        assert out.count("names deprecated agent spec") == 1
        assert "crew 'myteam'" in out

    def test_malformed_selector_values_do_not_crash_doctor(self, no_other_surfaces, capsys) -> None:
        # config.json is hand-editable and the loader preserves kiro_agent
        # verbatim, so a list can arrive here; dict.get on an unhashable value
        # would raise TypeError. Doctor must diagnose a malformed config, not
        # crash on it.
        from kiro_crew.config.sections import ChannelConfig

        cfg = KiroCrewConfig()
        cfg.agents = {"broken": KiroCrewAgentConfig(kiro_agent=[DEPRECATED])}  # type: ignore[arg-type]
        cfg.agent.default_agent = [DEPRECATED]  # type: ignore[assignment]
        cfg.session.pool_agent = {"agent": DEPRECATED}  # type: ignore[assignment]
        channel = ChannelConfig()
        channel.agent = [DEPRECATED]  # type: ignore[assignment]
        cfg.slack_channels = {"C123": channel}
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(cfg, issues)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_current_names_are_silent(self, monkeypatch, capsys) -> None:
        cfg = KiroCrewConfig()
        cfg.agents = {"myteam": KiroCrewAgentConfig(kiro_agent=REPLACEMENT)}
        monkeypatch.setattr(cron, "job_agent_names_from_disk", lambda: [("nightly", REPLACEMENT)])
        monkeypatch.setattr(cli_doctor, "_open_slot_agent_names", lambda: [("chat-1", "kirocrew")])
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(cfg, issues)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_holder_text_is_rendered_inert(self, monkeypatch, capsys) -> None:
        # Crew names, job names and slot keys are read off disk; a control
        # sequence in one must print escaped, not drive the terminal.
        hostile = "evil\x1b]0;pwned\x07"
        monkeypatch.setattr(cron, "job_agent_names_from_disk", lambda: [(hostile, DEPRECATED)])
        monkeypatch.setattr(cli_doctor, "_open_slot_agent_names", lambda: [])
        issues: list[str] = []
        cli_doctor._doctor_deprecated_agent_specs(KiroCrewConfig(), issues)
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\\x1b" in out
