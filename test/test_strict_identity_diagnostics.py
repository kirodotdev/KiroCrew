"""Strict-identity diagnostics: the refusal names its cause, and doctor reports it.

Two surfaces are under test, and they exist for two different readers:

1. :func:`kiro_crew.mcp_core.strict_identity_diagnosis` — the reader who just
   had a tool refused. Every strict refusal already said WHAT was refused; none
   said why THIS install cannot answer "which session is calling", so there was
   no next step. The diagnosis is appended to the refusal text.
2. ``cli_doctor._doctor_strict_identity`` — the reader doing a checkup before
   hitting the wall.

Both are REPORTS. ``mcp_gateway.stub_servers`` is empty by default on purpose
(routing starts a broker plus a stub per server), so neither surface repairs
anything.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import cli_doctor, mcp_core


class TestStrictIdentityDiagnosis:
    """The machine-specific reason strict identity is unavailable."""

    def test_empty_when_identity_resolves(self, monkeypatch) -> None:
        """A caller appends this unconditionally, so a resolvable identity must
        produce nothing rather than a misleading explanation."""
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-7")
        assert mcp_core.strict_identity_diagnosis() == ""

    def test_names_the_routing_gap_and_the_fix(self, monkeypatch) -> None:
        """The no-channel case: no env identity and no gateway caller. The text
        must name the server, the config key, and why the backend cannot supply
        an env identity — that last part is what stops a reader concluding their
        session is broken."""
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        with patch.object(mcp_core, "current_caller", return_value=None):
            out = mcp_core.strict_identity_diagnosis("kirocrew-dashboard")
        assert "kirocrew-dashboard" in out
        assert "mcp_gateway.stub_servers" in out
        assert "session-unbound" in out
        assert "doctor" in out

    def test_a_declared_host_pid_points_at_signing_not_routing(self, monkeypatch) -> None:
        """When the launcher DID declare a host pid the channel exists and the
        sidecar is what failed, so advising the operator to route the server
        would send them to the wrong place."""
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        with (
            patch.object(mcp_core, "current_caller", return_value=None),
            patch.object(mcp_core, "_resolve_session_key_strict", return_value=""),
        ):
            out = mcp_core.strict_identity_diagnosis()
        assert "did not verify" in out
        assert "trust root" in out
        assert "mcp_gateway.stub_servers" not in out

    def test_signing_refusal_names_the_searched_mapping_path(self, monkeypatch, tmp_path) -> None:
        """The refusal must say WHERE the verifier looked.

        When an agent spec pins a foreign ``KIROCREW_HOME`` into the stub's
        environment, the mapping is searched in a home the real gateway never
        writes. Without the resolved path in the message, the operator is sent
        to ``kirocrew doctor`` on the real gateway — which reports a healthy
        trust root and points nowhere near the poisoned home.
        """
        from kiro_crew.config import paths as config_paths

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        poisoned = tmp_path / "poisoned-home"
        monkeypatch.setenv("KIROCREW_HOME", str(poisoned))
        mapping = poisoned.resolve() / "session_pid_4242.txt"
        with (
            patch.object(mcp_core, "current_caller", return_value=None),
            patch.object(mcp_core, "_resolve_session_key_strict", return_value=""),
        ):
            out = mcp_core.strict_identity_diagnosis()
            assert f"mapping searched: {mapping}" in out

            # The wording is existence-independent: a mapping that exists (but
            # fails the HMAC check) produces the identical suffix, still naming
            # the path.
            config_paths.config_dir().mkdir(parents=True, exist_ok=True)
            mapping.write_text("dashboard:chat-1\n", encoding="utf-8")
            out_present = mcp_core.strict_identity_diagnosis()
            assert f"mapping searched: {mapping}" in out_present
            assert out_present == out

    def test_signing_refusal_wording_is_not_an_existence_oracle(
        self, monkeypatch, tmp_path
    ) -> None:
        """The wording must not vary with what is at the mapping path.

        The mapping directory is same-uid agent-writable, so an agent can plant
        a symlink at the mapping path aimed at a guessed sensitive target. Any
        wording that differs between present/absent/planted would leak
        existence through the refusal text; the suffix only names the path the
        verifier searched.
        """
        from kiro_crew.config import paths as config_paths

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        home = tmp_path / "home"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        mapping = home.resolve() / "session_pid_4242.txt"
        with (
            patch.object(mcp_core, "current_caller", return_value=None),
            patch.object(mcp_core, "_resolve_session_key_strict", return_value=""),
        ):
            absent = mcp_core.strict_identity_diagnosis()
            config_paths.config_dir().mkdir(parents=True, exist_ok=True)
            # Dangling symlink at the mapping path, aimed at a guessed target.
            mapping.symlink_to(tmp_path / "guessed-secret-that-does-not-exist")
            planted = mcp_core.strict_identity_diagnosis()
        assert f"mapping searched: {mapping}" in absent
        assert planted == absent, "wording varied with filesystem state: existence oracle"

    def test_the_diagnostic_never_creates_the_home_directory(self, monkeypatch, tmp_path) -> None:
        """Naming a path must not materialize it.

        ``config_dir()`` mkdirs as start-of-process maintenance; a diagnostic
        that inherited that side effect would raise on an uncreatable home —
        replacing the denial it decorates with a crash — and would create
        directories from a message-formatting path.
        """
        from kiro_crew.session_pid_sig import session_pid_mapping_path

        home = tmp_path / "never-created"
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        mapping = session_pid_mapping_path("4242")
        assert mapping == home.resolve() / "session_pid_4242.txt"
        assert not home.exists(), "the diagnostic resolver must not mkdir the home"

    @pytest.mark.parametrize(
        "exc",
        [OSError("uncreatable home"), RuntimeError("no home directory"), ValueError("garbage")],
        ids=["oserror", "runtimeerror", "valueerror"],
    )
    def test_an_unresolvable_home_keeps_the_generic_denial(self, monkeypatch, exc) -> None:
        """A path-resolution error downgrades the message, never the denial.

        Strict tools append this diagnosis to their refusal; if resolving the
        mapping path raises, the tool would error instead of returning its
        denial. ``Path.home()`` raises RuntimeError (not OSError) when neither
        HOME nor USERPROFILE resolves, so every arm the except clause names is
        exercised. The generic wording is the fallback.
        """
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        with (
            patch.object(mcp_core, "current_caller", return_value=None),
            patch.object(mcp_core, "_resolve_session_key_strict", return_value=""),
            patch.object(
                mcp_core,
                "session_pid_mapping_path",
                side_effect=exc,
            ),
        ):
            out = mcp_core.strict_identity_diagnosis()
        assert "did not verify" in out
        assert "trust root" in out
        assert "mapping" not in out.split("did not verify")[1].split(".")[0]


class TestRefusalsCarryTheDiagnosis:
    """The tool-layer refusals that own strict-identity text append it.

    Asserted per call site rather than by grep: a refusal that silently loses
    the diagnosis is the regression this pins.
    """

    _MARKER = " [why: no identity channel]"

    def test_ledger_refusal_carries_it(self, monkeypatch) -> None:
        from kiro_crew.mcp_tools import ledger

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        monkeypatch.setattr(mcp_core, "strict_identity_diagnosis", lambda *a: self._MARKER)
        _sk, err = ledger._strict_session_key()
        assert err.startswith("Error:") and self._MARKER in err

    def test_crew_ledger_refusal_carries_it(self, monkeypatch) -> None:
        from kiro_crew.mcp_tools import apps

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        monkeypatch.setattr(mcp_core, "strict_identity_diagnosis", lambda *a: self._MARKER)
        _sk, err = apps._crew_session_key()
        assert err.startswith("Error:") and self._MARKER in err

    def test_every_strict_refusal_string_carries_it(self) -> None:
        """Source-level completeness check across the modules that own strict
        refusal text. A sibling refusal added later without the diagnosis is the
        exact regression that made this change necessary in the first place —
        five sites had it, four did not, and the four were invisible.
        """
        import re

        # For modules migrated to the shared reflexive-tool gate the
        # diagnosis is appended INSIDE mcp_core.require_strict_session_key, so
        # the marker to count is the gate call itself; mcp_cron composes its
        # refusal (and diagnosis) separately and keeps the direct token.
        roots = {
            "mcp_tools/ledger.py": "require_strict_session_key(",
            "mcp_tools/apps.py": "require_strict_session_key(",
            "mcp_tools/messaging.py": "require_strict_session_key(",
            "mcp_dashboard.py": "require_strict_session_key(",
            "mcp_cron.py": "strict_identity_diagnosis(",
        }
        src_root = Path(mcp_core.__file__).parent
        # Refusal text that means "I could not tell which session is calling".
        pattern = re.compile(
            r"cannot verify (which session|caller identity)"
            r"|could not be verified strictly"
            r"|cannot determine which session"
            r"|cannot be identified well enough"
            r"|needs a directly-identified dashboard session"
        )
        for rel, marker in roots.items():
            text = (src_root / rel).read_text(encoding="utf-8")
            refusals = len(pattern.findall(text))
            wired = text.count(marker)
            assert refusals > 0, f"{rel}: expected strict refusal text"
            assert wired >= refusals, (
                f"{rel}: {refusals} strict refusal(s) but only {wired} carry the "
                f"diagnosis — a refusal without it leaves the reader no next step"
            )


class TestDoctorStrictIdentity:
    """``kirocrew doctor`` answers the question without waiting for a refusal."""

    class _Cfg:
        class _GW:
            def __init__(self, routed):
                self.stub_servers = routed

        def __init__(self, routed):
            self.mcp_gateway = self._GW(routed)

    def _darwin(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Darwin")

    def test_all_routed_reads_healthy(self, monkeypatch, capsys) -> None:
        self._darwin(monkeypatch)
        cli_doctor._doctor_strict_identity(self._Cfg(list(cli_doctor._STRICT_IDENTITY_SERVERS)))
        out = capsys.readouterr().out
        assert "strict identity: ✅" in out and "per-call caller" in out

    def test_unrouted_names_the_servers_and_the_affected_tools(self, monkeypatch, capsys) -> None:
        self._darwin(monkeypatch)
        cli_doctor._doctor_strict_identity(self._Cfg(["aws-mcp"]))
        out = capsys.readouterr().out
        assert "no identity channel" in out
        assert "kirocrew-core" in out and "kirocrew-dashboard" in out
        assert "monitor_start" in out and "session_ledger" in out

    def test_it_never_makes_doctor_exit_nonzero(self, monkeypatch, capsys) -> None:
        """The line is a NOTE, not a problem. ``stub_servers`` is empty by
        default, so appending to doctor's ``issues`` would make a stock install
        exit 1 and break ``kirocrew doctor && kirocrew gateway`` — the failure
        the speech-to-text section is written to avoid. Signature-enforced: the
        function takes no ``issues`` list at all, so it structurally cannot.
        """
        import inspect

        params = inspect.signature(cli_doctor._doctor_strict_identity).parameters
        assert list(params) == ["cfg"], "no issues list may be threaded in"
        self._darwin(monkeypatch)
        cli_doctor._doctor_strict_identity(self._Cfg([]))
        out = capsys.readouterr().out
        # Neutral marker, not a warning glyph: doctor's ⚠ lines read as work to do.
        assert "⚠" not in out

    def test_it_reports_rather_than_prescribes(self, monkeypatch, capsys) -> None:
        """Routing is an opt-in topology change (a broker plus a stub per
        server), so the note must say that leaving it unrouted is valid —
        otherwise doctor reads as demanding a change the design made optional."""
        self._darwin(monkeypatch)
        cli_doctor._doctor_strict_identity(self._Cfg([]))
        out = capsys.readouterr().out
        assert "valid choice" in out
        assert "broker" in out

    def test_skipped_where_the_env_channel_exists(self, monkeypatch, capsys) -> None:
        """On Linux the sandbox launcher exports ``KIROCREW_HOST_PID``, so
        routing is not what decides whether strict identity resolves — warning
        there would be false."""
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Linux")
        cli_doctor._doctor_strict_identity(self._Cfg([]))
        assert capsys.readouterr().out == ""

    def test_a_malformed_config_does_not_raise(self, monkeypatch, capsys) -> None:
        """Doctor never fails on the object it was handed."""

        class _Broken:
            @property
            def mcp_gateway(self):  # pragma: no cover - raising getter is the point
                raise RuntimeError("no gateway block")

        self._darwin(monkeypatch)
        cli_doctor._doctor_strict_identity(_Broken())
        assert "no identity channel" in capsys.readouterr().out


def test_no_new_dependency_on_a_running_gateway() -> None:
    """The diagnosis is pure inspection of this process's own state: it must not
    reach the gateway, because it runs on the failure path of a tool that could
    not even identify its session."""
    src = Path(mcp_core.__file__).read_text(encoding="utf-8")
    body = src.split("def strict_identity_diagnosis(")[1].split("\ndef ")[0]
    for forbidden in ("_api_urlopen", "_get(", "_post(", "urlopen", "requests."):
        assert forbidden not in body, f"diagnosis must not call {forbidden}"
