"""`kirocrew doctor` names the sandbox that actually confines a kiro-cli spawn.

On macOS with kiro-cli's internal sandbox enabled, Kiro Crew deliberately skips
its own seatbelt wrap for kiro-cli spawns (mutual exclusion: only one layer can
be active per spawn). A Sandbox section that answers with the backend alone and
returns leaves a host in exactly that configuration reading:

    Sandbox
      backend:     OK seatbelt

which describes a profile the session's tool backend never runs under. The
reachable-path consequence belongs to kiro-cli's profile instead, and its denial
of a pre-existing file outside the workspace surfaces as "Operation not
permitted" -- which looks like a macOS privacy (TCC) problem and is not one, so
the operator grants Full Disk Access and nothing changes.

These tests pin the diagnostic: report the delegation on every backend verdict,
name the exact switch, say Full Disk Access cannot help, keep the remedy safe
under Kiro Crew's own configured tier, and never count a working audited
delegation as a fault.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew import cli_doctor, sandbox


@pytest.fixture(autouse=True)
def healthy_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose own sandbox probe succeeds -- the reporter's configuration.

    This is the case the backend-only section cannot report: with
    ``unavailable_kind() == ""`` it takes the success branch and returns before
    anything else can print.
    """
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "")
    monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "seatbelt")


def _flat(capsys) -> str:
    """The section's detail text, with wrapping collapsed.

    ``_print_wrapped`` breaks the detail at 80 columns, so any asserted phrase
    longer than a few words straddles a newline in the raw capture. Collapsing
    runs of whitespace lets a test assert the sentence a reader sees rather than
    the column the wrapper happened to break at.
    """
    return " ".join(capsys.readouterr().out.split())


def _delegating(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: enabled)


class TestDelegationIsReported:
    """The note prints on a healthy backend, which is where it is needed."""

    def test_reports_that_kiro_cli_owns_confinement(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _delegating(monkeypatch, True)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "kiro-cli:" in out
        assert "confined by kiro-cli's own sandbox" in out

    def test_names_the_settings_file_and_key(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """The operator has to edit a file; naming only the symptom is useless."""
        _delegating(monkeypatch, True)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        settings_path, key = sandbox.kiro_internal_sandbox_switch()
        assert settings_path in out
        assert f'"{key}": true' in out

    def test_reads_the_switch_from_the_module_not_a_literal(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A repointed settings path must appear verbatim in the note.

        Pins the note against a hand-spelled copy of the path, which would keep
        passing the assertion above while sending a real operator to a file that
        is not the one being read.
        """
        _delegating(monkeypatch, True)
        monkeypatch.setattr(
            sandbox, "_KIRO_INTERNAL_SETTINGS_PATH", "/probe/kirocrew-test/settings.json"
        )
        monkeypatch.setattr(sandbox, "_KIRO_INTERNAL_SANDBOX_KEY", "probe_key")

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "/probe/kirocrew-test/settings.json" in out
        assert '"probe_key": true' in out
        assert "amazon-internal.json" not in out

    def test_says_full_disk_access_cannot_restore_the_reads(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The wrong remedy the reporter spent their time on, ruled out by name."""
        _delegating(monkeypatch, True)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "Full Disk Access" in out
        assert "Seatbelt profile, not macOS privacy" in out

    def test_names_the_denied_read_symptom(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """A reader arrives with the error string, so the note must carry it."""
        _delegating(monkeypatch, True)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "Operation not permitted" in out
        assert "PRE-EXISTING" in out

    def test_follows_the_backend_verdict(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """The note says "not the backend above", so it must print after it."""
        _delegating(monkeypatch, True)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert out.index("backend:") < out.index("kiro-cli:")

    def test_delegation_is_not_an_issue(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """Delegation is a working audited configuration, not a fault.

        Counting it would make ``kirocrew doctor`` exit non-zero on a correctly
        configured host and bury real faults in the summary.
        """
        _delegating(monkeypatch, True)

        issues: list[str] = []
        cli_doctor._doctor_sandbox(issues)

        capsys.readouterr()
        assert issues == []


class TestRemedyDependsOnKiroCrewsOwnTier:
    """The advice must never remove the only layer confining the spawn.

    With ``agent.sandbox="off"`` Kiro Crew builds no profile, so "disable
    kiro-cli's sandbox" leaves nothing confining a kiro-cli spawn and turns
    ``~/.aws`` and ``~/.ssh`` into readable files. The two settings correlate --
    ``"off"`` exists to defer isolation to kiro-cli -- so that is a reachable
    configuration, not a contrived one.
    """

    def _tier(self, monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
        _delegating(monkeypatch, True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: tier)
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)

    def test_recommends_disabling_when_our_own_profile_would_engage(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._tier(monkeypatch, "auto")

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert 'Set "sandbox" to false' in out
        assert 'agent.sandbox="auto"' in out
        assert "no OS confinement at all" not in out

    def test_refuses_to_recommend_disabling_when_our_tier_is_off(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        self._tier(monkeypatch, "off")

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert 'Do NOT just set "sandbox" to false' in out
        assert "no OS confinement at all" in out
        assert "~/.aws" in out
        assert 'Set agent.sandbox to "auto" first' in out

    def test_governance_clamp_decides_not_the_raw_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A governed host clamps a configured ``off`` upward, so a profile does
        engage and the plain recommendation is correct. Reading the raw config
        value would withhold the right advice on exactly those hosts."""
        _delegating(monkeypatch, True)
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: "strict")

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert 'Set "sandbox" to false' in out
        assert 'agent.sandbox="strict"' in out
        assert "Do NOT" not in out

    def test_unreadable_tier_gets_the_cautious_wording(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A tier read that raises must not produce the bare recommendation.

        ``effective_sandbox_mode`` propagates a PlatformCompositionError by
        design, so the failure direction has to be the cautious one.
        """
        _delegating(monkeypatch, True)

        def _boom(mode: str) -> str:
            raise RuntimeError("governance profile could not be composed")

        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "auto")
        monkeypatch.setattr(sandbox, "effective_sandbox_mode", _boom)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "check that agent.sandbox is not" in out
        assert "no OS confinement at all" in out
        assert 'Set "sandbox" to false to hand isolation' not in out


class TestSilentWhenNotDelegating:
    """No note where the claim would be false."""

    def test_no_note_when_the_internal_sandbox_is_off(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _delegating(monkeypatch, False)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "kiro-cli:" not in out
        assert "backend:" in out

    def test_no_note_off_darwin_even_with_the_setting_on(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The settings-driven delegation is macOS-only.

        Linux namespace isolation is unaffected by that file, so reporting it
        there would tell a Linux operator their agent is confined by kiro-cli
        when Kiro Crew's own launcher is what wraps the child.
        """
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "kiro-cli:" not in out

    def test_unreadable_setting_does_not_break_the_section(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The doctor must survive a probe that raises, as its other checks do."""
        monkeypatch.setattr(sys, "platform", "darwin")

        def _boom() -> bool:
            raise RuntimeError("home directory could not be resolved")

        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", _boom)

        cli_doctor._doctor_sandbox([])

        out = _flat(capsys)
        assert "backend:" in out
        assert "kiro-cli:" not in out
