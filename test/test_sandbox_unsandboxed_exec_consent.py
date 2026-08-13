"""The ``kirocrew setup`` consent step that offers the unsandboxed-exec opt-in
on a host with no sandbox backend.

Fail-closed is the shipped default (pinned in
``test_config_loader.py::test_sandbox_allow_unsandboxed_exec_loads_from_config``,
which also pins that no platform may flip it). This module covers the other half:
the wizard is the discoverable path to the opt-in, it asks only when it applies,
and it writes only on an explicit yes — so the decision stays operator-declared.
"""

from __future__ import annotations

import json
from pathlib import Path


def _run_consent(
    tmp_path: Path,
    monkeypatch,
    *,
    kind: str,
    answer: str | None,
    tty: bool = True,
    existing: dict | None = None,
    raw: str | None = None,
    sel_raises: bool = False,
    audit: list | None = None,
    parse: bool = True,
    overlay: dict | None = None,
    write_raises: bool = False,
) -> dict | None:
    """Drive ``_setup_sandbox_consent`` and return the resulting config dict.

    ``None`` means the file was never created. ``existing`` seeds config.json as
    JSON; ``raw`` seeds it verbatim so a non-object document can be exercised;
    ``overlay`` seeds the ``config.local.json`` deep-merge overlay. ``audit``
    collects the kwargs of every SEL event the step emits. ``parse=False`` skips
    reading the result back, for a seed that is not valid JSON.
    """
    from kiro_crew import cli_setup

    cfg_file = tmp_path / "config.json"
    local_file = tmp_path / "config.local.json"
    if raw is not None:
        cfg_file.write_text(raw, encoding="utf-8")
    elif existing is not None:
        cfg_file.write_text(json.dumps(existing), encoding="utf-8")
    if overlay is not None:
        local_file.write_text(json.dumps(overlay), encoding="utf-8")

    calls = audit if audit is not None else []

    class _FakeSel:
        def log_tool_invocation(self, **kwargs):
            calls.append(kwargs)
            if sel_raises:
                raise OSError("audit log unwritable")

    def _write(path, data, **kwargs):
        if write_raises:
            raise OSError("config is locked by another process")
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(cli_setup, "unavailable_kind", lambda *a, **k: kind)
    monkeypatch.setattr(cli_setup.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(cli_setup.sys.stdout, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(cli_setup, "sel", lambda: _FakeSel())
    monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)
    monkeypatch.setattr(cli_setup, "config_local_path", lambda: local_file)
    monkeypatch.setattr(cli_setup, "write_config_atomically", _write)
    monkeypatch.setattr(cli_setup, "_input_or_skip", lambda _prompt: answer)

    cli_setup._setup_sandbox_consent()

    if not cfg_file.exists():
        return None
    if not parse:
        return None
    return json.loads(cfg_file.read_text(encoding="utf-8"))


class TestSetupSandboxConsent:
    """The wizard asks only when it applies, and writes only on an explicit yes."""

    def test_no_prompt_when_a_backend_exists(self, tmp_path, monkeypatch, capsys) -> None:
        """The Linux/macOS norm: the step is invisible."""
        result = _run_consent(tmp_path, monkeypatch, kind="", answer="y")
        assert result is None
        assert "Sandbox" not in capsys.readouterr().out

    def test_no_prompt_when_the_key_is_already_declared_true(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """Never re-ask an operator who already decided."""
        existing = {"agent": {"sandbox_allow_unsandboxed_exec": True}}
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="n", existing=existing
        )
        assert result == existing
        assert "Sandbox" not in capsys.readouterr().out

    def test_no_prompt_when_the_key_is_already_declared_false(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """An explicit decline is a decision too — it must not be re-litigated."""
        existing = {"agent": {"sandbox_allow_unsandboxed_exec": False}}
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", existing=existing
        )
        assert result == existing
        assert "Sandbox" not in capsys.readouterr().out

    def test_explicit_yes_writes_the_opt_in(self, tmp_path, monkeypatch) -> None:
        result = _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="y")
        assert result is not None
        assert result["agent"]["sandbox_allow_unsandboxed_exec"] is True

    def test_yes_spelled_out_writes_the_opt_in(self, tmp_path, monkeypatch) -> None:
        result = _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="YES")
        assert result is not None
        assert result["agent"]["sandbox_allow_unsandboxed_exec"] is True

    def test_yes_preserves_unrelated_config(self, tmp_path, monkeypatch) -> None:
        existing = {"timezone": "Australia/Sydney", "agent": {"approval_mode": "auto"}}
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", existing=existing
        )
        assert result is not None
        assert result["timezone"] == "Australia/Sydney"
        assert result["agent"]["approval_mode"] == "auto"
        assert result["agent"]["sandbox_allow_unsandboxed_exec"] is True

    def test_declining_writes_nothing(self, tmp_path, monkeypatch) -> None:
        assert _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="n") is None

    def test_empty_answer_declines(self, tmp_path, monkeypatch) -> None:
        """``[y/N]`` — a bare Enter must not opt in."""
        assert _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="") is None

    def test_non_interactive_eof_declines(self, tmp_path, monkeypatch) -> None:
        """``_input_or_skip`` returns None on EOF; that must stay fail-closed."""
        assert _run_consent(tmp_path, monkeypatch, kind="no_backend", answer=None) is None

    def test_declining_names_the_config_key_and_path(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The remedy has to be actionable — that is the whole point of the step."""
        _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="n")
        out = capsys.readouterr().out
        assert "sandbox_allow_unsandboxed_exec=true" in out
        assert str(tmp_path / "config.json") in out

    def test_prompt_states_the_concrete_risk(self, tmp_path, monkeypatch, capsys) -> None:
        """A consent prompt that does not name what is exposed is not consent."""
        _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="n")
        out = capsys.readouterr().out
        assert "~/.aws" in out
        assert "~/.ssh" in out

    def test_non_dict_agent_section_is_left_untouched(self, tmp_path, monkeypatch) -> None:
        """Refuse to coerce a malformed config rather than clobbering it."""
        existing: dict = {"agent": "not-an-object"}
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", existing=existing
        )
        assert result == existing

    def test_non_object_config_document_is_skipped(self, tmp_path, monkeypatch) -> None:
        """A top-level ``[]`` must not raise AttributeError and abort the wizard."""
        result = _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="y", raw="[]")
        assert result == []

    def test_unreadable_config_is_skipped(self, tmp_path, monkeypatch) -> None:
        """Malformed JSON is reported, not repaired — and never rewritten."""
        _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            raw="{not json",
            parse=False,
        )
        assert (tmp_path / "config.json").read_text(encoding="utf-8") == "{not json"


class TestSetupSandboxConsentIsAudited:
    """Persisting an execution permission is a security event."""

    def test_grant_emits_a_sel_event_with_audit_or_deny(self, tmp_path, monkeypatch) -> None:
        audit: list = []
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", audit=audit
        )
        assert result is not None
        assert result["agent"]["sandbox_allow_unsandboxed_exec"] is True
        assert len(audit) == 1
        event = audit[0]
        assert event["tool_name"] == "sandbox_allow_unsandboxed_exec"
        assert event["outcome"] == "allowed"
        assert event["critical"] is True
        assert event["metadata"]["reason"] == "operator_consent_at_setup"

    def test_audit_failure_refuses_the_grant(self, tmp_path, monkeypatch, capsys) -> None:
        """Audit-or-deny: an unwritable SEL log must not yield an unaudited grant."""
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", sel_raises=True
        )
        assert result is None
        assert "Refusing to grant unsandboxed execution unaudited" in capsys.readouterr().out

    def test_decline_emits_no_grant_event(self, tmp_path, monkeypatch) -> None:
        audit: list = []
        _run_consent(tmp_path, monkeypatch, kind="no_backend", answer="n", audit=audit)
        assert audit == []

    def test_skip_when_backend_exists_emits_no_grant_event(
        self, tmp_path, monkeypatch
    ) -> None:
        audit: list = []
        _run_consent(tmp_path, monkeypatch, kind="", answer="y", audit=audit)
        assert audit == []


class TestSetupSandboxConsentRespectsTheOverlay:
    """``config.local.json`` deep-merges OVER ``config.json`` and wins at load."""

    def test_overlay_declaring_false_suppresses_the_prompt(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """Prompting here would report a grant the effective config contradicts."""
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            overlay={"agent": {"sandbox_allow_unsandboxed_exec": False}},
        )
        assert result is None
        assert "Sandbox" not in capsys.readouterr().out

    def test_overlay_declaring_true_suppresses_the_prompt(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            overlay={"agent": {"sandbox_allow_unsandboxed_exec": True}},
        )
        assert result is None
        assert "Sandbox" not in capsys.readouterr().out

    def test_unrelated_overlay_does_not_suppress_the_prompt(
        self, tmp_path, monkeypatch
    ) -> None:
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            overlay={"agent": {"approval_mode": "auto"}},
        )
        assert result is not None
        assert result["agent"]["sandbox_allow_unsandboxed_exec"] is True


class TestSetupSandboxConsentSurvivesAWriteFailure:
    """A locked config must not abort the wizard after the user answered."""

    def test_write_failure_is_reported_and_leaves_fail_closed(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", write_raises=True
        )
        assert result is None
        out = capsys.readouterr().out
        assert "Could not write" in out
        assert "stays fail-closed" in out
        assert "sandbox_allow_unsandboxed_exec=true" in out


class TestSetupSandboxConsentOnlyActsOnNoBackend:
    """A persistent opt-in may only be offered for a PERMANENT absence."""

    def test_transient_probe_failure_offers_no_opt_in(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """A momentary fork failure self-heals; it must not buy a permanent bypass."""
        result = _run_consent(tmp_path, monkeypatch, kind="transient", answer="y")
        assert result is None
        out = capsys.readouterr().out
        assert "TRANSIENT" in out
        # The sandbox layer's own guidance forbids steering a transient failure at
        # this flag, so the remedy must not be named here.
        assert "unsandboxed_exec" not in out

    def test_foreign_sandbox_offers_no_opt_in(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """This host's sandbox works; the remedy is elsewhere, not this flag."""
        result = _run_consent(tmp_path, monkeypatch, kind="foreign_sandbox", answer="y")
        assert result is None
        assert "Sandbox" not in capsys.readouterr().out

    def test_non_interactive_stdio_does_not_prompt(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """`kirocrew update` captures output; an unseen prompt would hang it."""
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", tty=False
        )
        assert result is None
        out = capsys.readouterr().out
        assert "from a terminal" in out
        assert "Allow unsandboxed execution?" not in out
