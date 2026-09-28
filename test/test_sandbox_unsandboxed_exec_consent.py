"""The ``kirocrew setup`` step that surfaces the unsandboxed-exec decision on a
host with no sandbox backend.

The step asks in whichever direction matches what an UNDECLARED key resolves to on
this platform (``sandbox.unsandboxed_exec_platform_default``): it offers the
opt-IN where the default is fail-closed, and states the exposure and offers the
opt-OUT where the default is allow. Every test here PINS that platform verdict —
see ``_run_consent``'s ``platform_allows`` — so the suite proves the same thing on
Linux CI and on a Windows dev box.

Covered: it asks only when it applies, it writes only on an explicit yes in either
direction, a decline leaves the key undeclared so a later change to the default
still reaches the host, and the audit is audit-or-deny for a grant but
best-effort for a lockdown (refusing to record a restriction because the audit log
is broken would leave the host unconfined).
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
    platform_allows: bool = False,
    prompts: list | None = None,
) -> dict | None:
    """Drive ``_setup_sandbox_consent`` and return the resulting config dict.

    ``None`` means the file was never created. ``existing`` seeds config.json as
    JSON; ``raw`` seeds it verbatim so a non-object document can be exercised;
    ``overlay`` seeds the ``config.local.json`` deep-merge overlay. ``audit``
    collects the kwargs of every SEL event the step emits. ``parse=False`` skips
    reading the result back, for a seed that is not valid JSON.

    ``platform_allows`` pins what an UNDECLARED key resolves to, which is what
    decides the direction the step asks in. It is pinned rather than inherited
    because the real value is ``sys.platform``-derived: left unpatched, every test
    in this module would assert the opt-IN branch on Linux CI and the opt-OUT
    branch on a Windows dev box, and the same suite would prove different things
    on the two hosts. Default ``False`` keeps the fail-closed direction, so a test
    that says nothing about the platform is testing the opt-in.

    ``prompts`` collects the text of every question asked. It is a separate seam
    because the prompt is an ARGUMENT to ``_input_or_skip``, which is stubbed here
    and so never reaches stdout — asserting the question via ``capsys`` would pass
    whatever the step asked, including nothing at all.
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

    def _locked_write(path, *, mutate, **kwargs):
        """Stand-in for ``update_config_locked`` with the same contract.

        The step now routes its read-modify-write through the advisory-locked
        primitive, so the seam moves there. The fake keeps the part these tests
        depend on: the mutate callback is handed the CURRENT document and the
        file is written only when it returns one.
        """
        if write_raises:
            raise OSError("config is locked by another process")
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        result = mutate(existing)
        if result is None:
            return existing
        path.write_text(json.dumps(result), encoding="utf-8")
        return result

    def _declared_stub() -> bool:
        """Presence of the key in either seeded file.

        The step now asks the loader's shared reader rather than carrying its own
        copy, so the seam moves here. Reading the two tmp files keeps the fixture's
        `existing` / `overlay` arguments meaningful without pointing the real
        loader's module-level paths at them.
        """
        for path in (cfg_file, local_file):
            if not path.exists():
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            agent = doc.get("agent") if isinstance(doc, dict) else None
            if isinstance(agent, dict) and "sandbox_allow_unsandboxed_exec" in agent:
                return True
        return False

    monkeypatch.setattr(cli_setup, "unavailable_kind", lambda *a, **k: kind)
    monkeypatch.setattr(cli_setup, "unsandboxed_exec_declared", _declared_stub)
    monkeypatch.setattr(
        cli_setup, "unsandboxed_exec_platform_default", lambda *a, **k: platform_allows
    )
    monkeypatch.setattr(cli_setup.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(cli_setup.sys.stdout, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(cli_setup, "sel", lambda: _FakeSel())
    monkeypatch.setattr(cli_setup, "config_path", lambda: cfg_file)
    monkeypatch.setattr(cli_setup, "update_config_locked", _locked_write)

    def _ask(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return answer

    monkeypatch.setattr(cli_setup, "_input_or_skip", _ask)

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


class TestSetupSandboxConsentOnADefaultAllowPlatform:
    """Where an undeclared key resolves to ALLOW, the step inverts.

    It informs and offers the lockdown instead of asking for permission it no
    longer needs. Everything structural is shared with the opt-in direction, so
    what these pin is the polarity: which question is asked, which value a yes
    writes, and that a decline still writes nothing.
    """

    def test_prompt_offers_the_refusal_not_the_grant(
        self, tmp_path, monkeypatch
    ) -> None:
        """Asking "allow?" where it is already allowed would misdescribe the host."""
        prompts: list = []
        _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="n",
            platform_allows=True,
            prompts=prompts,
        )
        assert len(prompts) == 1
        assert "Refuse unsandboxed execution on this host?" in prompts[0]
        assert "Allow unsandboxed execution?" not in prompts[0]

    def test_the_other_direction_still_asks_to_allow(
        self, tmp_path, monkeypatch
    ) -> None:
        """The polarity is the platform's, not a rewrite: a fail-closed default must
        still ask for permission rather than offer a lockdown that already holds."""
        prompts: list = []
        _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="n",
            platform_allows=False,
            prompts=prompts,
        )
        assert len(prompts) == 1
        assert "Allow unsandboxed execution?" in prompts[0]

    def test_prompt_states_the_same_concrete_risk(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """A default that describes itself in softer words than the opt-in did is
        the silent change this branch exists to avoid."""
        _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="n", platform_allows=True
        )
        out = capsys.readouterr().out
        assert "~/.aws" in out
        assert "~/.ssh" in out
        assert "BY DEFAULT" in out

    def test_explicit_yes_writes_the_lockdown(self, tmp_path, monkeypatch) -> None:
        """A yes here RESTRICTS, so it must persist false — not true."""
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="y", platform_allows=True
        )
        assert result == {"agent": {"sandbox_allow_unsandboxed_exec": False}}

    def test_declining_writes_nothing(self, tmp_path, monkeypatch) -> None:
        """Writing the current default on a decline would freeze it silently, so a
        later change to that default could never reach this host."""
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="n", platform_allows=True
        )
        assert result is None

    def test_empty_answer_declines(self, tmp_path, monkeypatch) -> None:
        """A bare Enter is not consent to change anything, in either direction."""
        result = _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="", platform_allows=True
        )
        assert result is None

    def test_declining_names_the_key_that_would_refuse(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The reader was just told they are unconfined; leaving without naming the
        opt-out would leave them no way to act on that."""
        _run_consent(
            tmp_path, monkeypatch, kind="no_backend", answer="n", platform_allows=True
        )
        out = capsys.readouterr().out
        assert "sandbox_allow_unsandboxed_exec=false" in out
        assert str(tmp_path / "config.json") in out

    def test_non_interactive_still_prints_the_notice(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """An unattended install is the case with NO consent behind it, so it is the
        one that most needs telling. It still must not ask."""
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            tty=False,
            platform_allows=True,
        )
        assert result is None
        out = capsys.readouterr().out
        assert "WITHOUT credential isolation" in out
        assert "Refuse unsandboxed execution on this host?" not in out

    def test_lockdown_is_audited(self, tmp_path, monkeypatch) -> None:
        """A restriction belongs in the tamper-evident log too, as its own outcome."""
        audit: list = []
        _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            audit=audit,
            platform_allows=True,
        )
        assert len(audit) == 1
        assert audit[0]["outcome"] == "denied"
        assert audit[0]["metadata"]["reason"] == "operator_lockdown_at_setup"

    def test_lockdown_audit_is_not_critical(self, tmp_path, monkeypatch) -> None:
        """Audit-or-deny is for GRANTS. Refusing to fail-close a host because the
        audit log is broken would leave it unconfined — the worse outcome."""
        audit: list = []
        _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            audit=audit,
            platform_allows=True,
        )
        assert audit[0]["critical"] is False

    def test_audit_failure_still_records_the_lockdown(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The write proceeds and the audit gap is reported, rather than the host
        being left unconfined because SEL is unwritable."""
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            sel_raises=True,
            platform_allows=True,
        )
        assert result == {"agent": {"sandbox_allow_unsandboxed_exec": False}}
        assert "Recording the restriction anyway" in capsys.readouterr().out

    def test_write_failure_names_the_posture_that_actually_remains(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """Nothing was written, so the host is still UNCONFINED. Reporting "stays
        fail-closed" here would tell an operator they are protected when they are
        not."""
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            write_raises=True,
            platform_allows=True,
        )
        assert result is None
        out = capsys.readouterr().out
        assert "keep running unconfined" in out
        assert "stays fail-closed" not in out

    def test_declared_false_still_suppresses_the_prompt(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """The operator already locked this host down; re-offering the lockdown
        would ask them to decide something they decided."""
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="no_backend",
            answer="y",
            existing={"agent": {"sandbox_allow_unsandboxed_exec": False}},
            platform_allows=True,
        )
        assert result == {"agent": {"sandbox_allow_unsandboxed_exec": False}}
        assert "Sandbox" not in capsys.readouterr().out

    def test_transient_offers_nothing_here_either(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """A self-healing probe failure is not a platform verdict, so no platform
        default applies to it."""
        result = _run_consent(
            tmp_path,
            monkeypatch,
            kind="transient",
            answer="y",
            platform_allows=True,
        )
        assert result is None
        assert "Refuse unsandboxed execution" not in capsys.readouterr().out
