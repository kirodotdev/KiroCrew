"""The redaction opt-out is a ceiling, so it does not live where the agent can write."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


class TestTheSwitchIsBeyondTheAgentsReach:
    def test_it_lives_inside_the_fenced_backup_directory(self, home: Path) -> None:
        switch = redact.redaction_switch_path()
        assert switch.parent.name == "backup", switch
        # The fence classifies the DIRECTORY, so the leaf inherits it by living there.
        assert switch.parent.parent == home
        assert switch.name == "redaction.json"

    def test_the_file_gate_refuses_it(self, home: Path) -> None:
        """The layer that holds on every platform: containment, not pattern matching."""
        assert security.is_sensitive_path(str(redact.redaction_switch_path()))

    def test_the_os_layer_fences_it_for_every_verb(self, home: Path) -> None:
        """The layer that binds a SUBPROCESS, which no text matcher can.

        This assertion used to run `is_sensitive_bash_command` over four shell forms
        (`cat`, `echo >`, `tee`, `rm`). That tested a layer which cannot hold for a
        spawned command: a shell reaches a file through an `open()` that never routes
        through the tool gate, so a path fenced only there is readable in any sandbox
        mode whatever the matcher recognises, and the set of spellings is unbounded --
        `sandbox.py` says so in as many words.

        `backup` carries the HIDDEN disposition, the strongest of the three: the whole
        directory is bind-masked in EVERY sandbox mode, so the switch is unreachable for
        read, write and delete alike. That is broader than the four verbs enumerated
        above and needs no platform skip, since it asserts the disposition rather than
        one platform's parsing of a data-home path.
        """
        from kiro_crew import sandbox

        assert "backup" in sandbox._CREW_HIDDEN_LEAVES
        assert redact.redaction_switch_path().parent.name == "backup"

    def test_the_shell_scan_treats_this_file_exactly_like_the_other_ceilings(
        self, home: Path
    ) -> None:
        """Pins the SHAPE of the coverage rather than one platform's answer.

        Whatever the shell scan does with the data home it is given, it must do the same
        for this switch as for the deny list and the computer-use enable. That keeps the
        skip above honest -- it records a shared-scanner limit rather than a hole specific
        to this file -- and it fails if this switch ever becomes the odd one out.
        """
        cfg = redact.redaction_switch_path().parent.parent
        peers = [cfg / "denied_commands.json", cfg / "computer_use.json"]
        mine = redact.redaction_switch_path()
        verdicts = {
            str(p): bool(security.is_sensitive_bash_command(f"cat {p}")) for p in [*peers, mine]
        }
        assert len(set(verdicts.values())) == 1, verdicts

    def test_it_is_not_a_config_field(self) -> None:
        """Two places to turn it off means the agent-writable one decides.

        The repo already keeps its other ceilings (the deny list, the computer-use enable)
        out of `config.json` for exactly this reason, and states that the config section
        must carry no enable field so there is only one place the thing can be switched.
        """
        assert not hasattr(KiroCrewConfig, "redact_backup_uploads")
        assert "redact_backup_uploads" not in inspect.getsource(snap)


class TestTheDefaultNeedsNoFile:
    """Redaction is opt-IN. The owner-only destination is what guards the bundle."""

    def _write(self, home: Path, payload: str) -> None:
        switch = redact.redaction_switch_path()
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text(payload, encoding="utf-8")

    def test_absent_means_no_rewriting(self, home: Path) -> None:
        assert redact.outbound_redaction_enabled() is False

    def test_an_explicit_opt_in_is_honoured(self, home: Path) -> None:
        self._write(home, json.dumps({"redact_uploads": True}))
        assert redact.outbound_redaction_enabled() is True

    def test_an_explicit_off_is_allowed_to_be_written_down(self, home: Path) -> None:
        self._write(home, json.dumps({"redact_uploads": False}))
        assert redact.outbound_redaction_enabled() is False

    def test_a_key_the_operator_did_not_set_is_off_not_an_error(self, home: Path) -> None:
        """An unrelated key is a file that simply does not opt in."""
        self._write(home, json.dumps({"other": True}))
        assert redact.outbound_redaction_enabled() is False
        self._write(home, json.dumps({}))
        assert redact.outbound_redaction_enabled() is False

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            "",
            "[]",
            json.dumps({"redact_uploads": "true"}),
            json.dumps({"redact_uploads": 1}),
            json.dumps({"redact_uploads": "false"}),
            json.dumps({"redact_uploads": 0}),
        ],
    )
    def test_a_file_that_states_neither_raises_rather_than_guessing(
        self, home: Path, payload: str
    ) -> None:
        """The operator wrote this on purpose; both silent answers betray a choice.

        Off would ignore a request to scrub, on would rewrite files they may not have meant
        to touch, so the upload refuses and names the file instead.
        """
        self._write(home, payload)
        with pytest.raises(redact.RedactionSwitchUnreadable):
            redact.outbound_redaction_enabled()

    def test_an_unreadable_file_raises_rather_than_defaulting(
        self, home: Path, monkeypatch
    ) -> None:
        self._write(home, json.dumps({"redact_uploads": True}))

        def deny(*a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", deny)
        with pytest.raises(redact.RedactionSwitchUnreadable):
            redact.outbound_redaction_enabled()

    def test_the_upload_asks_the_switch_not_the_config(self) -> None:
        src = inspect.getsource(snap._redacted_upload_copy)
        assert "outbound_redaction_enabled()" in src
        assert "KiroCrewConfig" not in src

    def test_an_unreadable_switch_refuses_the_upload(self, home: Path, monkeypatch) -> None:
        """Not a silent default in either direction: the upload stops and names the file."""
        self._write(home, "not json at all")
        src = inspect.getsource(snap._redacted_upload_copy)
        assert "RedactionSwitchUnreadable" in src
        assert "redact = True" not in src, "the old fail-closed default must be gone"
