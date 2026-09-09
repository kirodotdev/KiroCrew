"""The no-backend execution policy: who permits it, and what gets audited.

Two things can permit a spawn on a host with no sandbox backend: an explicit
``agent.sandbox_allow_unsandboxed_exec=true``, and the platform default — allow on
Windows, where nothing installable can ever produce a backend, and fail-closed
everywhere else, where a missing backend is broken or one AppArmor profile away
from working.

That makes the DECLARATION load-bearing in a way the dataclass cannot express: a
deliberate ``false`` and an absent key both arrive as ``False``, and only the first
may outrank the platform. This module pins the resulting matrix, and pins that
every unconfined spawn is audited with the permitting party named — a platform
grant has no config file standing as its record, so without that event it would
leave no trace at all.

Every test PINS the platform verdict rather than inheriting it, so the suite proves
the same thing on Linux CI and on a Windows dev box.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.config import loader as loader_mod
from kiro_crew.sandbox import (
    UNSANDBOXED_BY_OPERATOR,
    UNSANDBOXED_BY_PLATFORM,
    SandboxUnavailableError,
    reset_backend,
    wrap_argv,
)

_ARGV = ["kirocrew-test-binary", "--flag"]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Neutralize host interference and per-process latches.

    The kiro internal-sandbox settings path is pointed at nothing so the
    delegation branch never preempts the policy under test, and the one-shot
    warning latch is cleared so each test sees its own warning.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.delenv("KIROCREW_ALLOW_UNSANDBOXED", raising=False)
    monkeypatch.setattr(
        sandbox_mod,
        "_KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    for attr in ("_warned", "_nested_passthrough_logged"):
        if hasattr(sandbox_mod.wrap_argv, attr):
            delattr(sandbox_mod.wrap_argv, attr)
    reset_backend()
    yield
    for attr in ("_warned", "_nested_passthrough_logged"):
        if hasattr(sandbox_mod.wrap_argv, attr):
            delattr(sandbox_mod.wrap_argv, attr)
    reset_backend()


def _pin_config(monkeypatch, *, value: bool, declared: bool) -> None:
    """Pin the two inputs the spawn path reads.

    ``value`` is the RESOLVED policy the dataclass carries — the loader has already
    folded the platform default into it, so there is no platform seam here — and
    ``declared`` only labels the audit event. Stubbing ``KiroCrewConfig.load`` keeps
    this focused on the spawn path rather than the loader's validation machinery;
    the platform resolution itself is covered in ``test_config_loader.py``.
    """
    monkeypatch.setattr(
        loader_mod.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(agent=SimpleNamespace(sandbox_allow_unsandboxed_exec=value))
        ),
    )
    monkeypatch.setattr(loader_mod, "unsandboxed_exec_declared", lambda: declared)


class TestUnsandboxedExecDeclaredReadsPresenceNotValue:
    """``unsandboxed_exec_declared`` answers "did the operator decide", not "what".

    DIAGNOSTIC ONLY: it labels an audit event and picks message wording, and must
    never gate execution. A full-document ``save()`` materializes the key, so a host
    that never answered can read as declared afterwards — which is exactly why the
    effective policy lives in the resolved VALUE instead.
    """

    @pytest.fixture
    def cfg(self, tmp_path, monkeypatch):
        main = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        monkeypatch.setattr(loader_mod, "config_path", lambda: main)
        monkeypatch.setattr(loader_mod, "config_local_path", lambda: local)
        return main, local

    def test_absent_key_is_not_declared(self, cfg) -> None:
        main, _ = cfg
        main.write_text(json.dumps({"agent": {"approval_mode": "auto"}}), encoding="utf-8")
        assert loader_mod.unsandboxed_exec_declared() is False

    def test_declared_true_counts(self, cfg) -> None:
        main, _ = cfg
        main.write_text(
            json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": True}}), encoding="utf-8"
        )
        assert loader_mod.unsandboxed_exec_declared() is True

    def test_declared_false_also_counts(self, cfg) -> None:
        """The whole point: a deliberate false is a DECISION, not an absence."""
        main, _ = cfg
        main.write_text(
            json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": False}}), encoding="utf-8"
        )
        assert loader_mod.unsandboxed_exec_declared() is True

    def test_overlay_only_declaration_counts(self, cfg) -> None:
        """The overlay wins at load time, so a decision recorded only there is
        still a decision — ignoring it would let the platform default overrule it."""
        main, local = cfg
        main.write_text(json.dumps({"agent": {"approval_mode": "auto"}}), encoding="utf-8")
        local.write_text(
            json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": False}}), encoding="utf-8"
        )
        assert loader_mod.unsandboxed_exec_declared() is True

    def test_missing_files_are_not_a_declaration(self, cfg) -> None:
        assert loader_mod.unsandboxed_exec_declared() is False

    def test_unreadable_document_is_not_a_declaration(self, cfg) -> None:
        """A corrupt config is not consent to anything."""
        main, _ = cfg
        main.write_text("{not json", encoding="utf-8")
        assert loader_mod.unsandboxed_exec_declared() is False

    def test_non_object_agent_section_is_not_a_declaration(self, cfg) -> None:
        main, _ = cfg
        main.write_text(json.dumps({"agent": ["not", "a", "dict"]}), encoding="utf-8")
        assert loader_mod.unsandboxed_exec_declared() is False


class TestTheGateReadsTheResolvedValue:
    """The spawn gate is a plain read, and that is the point.

    The platform default is folded into the field by the loader (its matrix lives in
    ``test_config_loader.py``), so nothing here re-derives policy from whether the
    key was declared. Keying the gate on presence would let a full-document
    ``KiroCrewConfig.save()`` — which publishes ``asdict(self.agent)`` — turn "never
    decided" into a declared lockdown and re-brick every spawn on a platform with no
    installable backend.
    """

    def test_a_permitting_value_permits(self, monkeypatch) -> None:
        _pin_config(monkeypatch, value=True, declared=True)
        assert sandbox_mod._allow_unsandboxed_exec() is True

    def test_a_permitting_value_permits_even_undeclared(self, monkeypatch) -> None:
        """This is the shape a Windows host takes: the loader resolved an absent key
        to True, and the gate must not second-guess it by looking for a declaration."""
        _pin_config(monkeypatch, value=True, declared=False)
        assert sandbox_mod._allow_unsandboxed_exec() is True

    def test_a_refusing_value_refuses(self, monkeypatch) -> None:
        _pin_config(monkeypatch, value=False, declared=True)
        assert sandbox_mod._allow_unsandboxed_exec() is False

    def test_a_refusing_value_refuses_even_undeclared(self, monkeypatch) -> None:
        _pin_config(monkeypatch, value=False, declared=False)
        assert sandbox_mod._allow_unsandboxed_exec() is False

    def test_unreadable_config_refuses(self, monkeypatch) -> None:
        """A broken config must never buy a LOOSER sandbox than was configured."""

        def _boom(cls):
            raise OSError("config unreadable")

        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", classmethod(_boom))
        assert sandbox_mod._allow_unsandboxed_exec() is False


class TestPermittedByNamesTheSource:
    """The audit log has to distinguish an accepted risk from a default."""

    def test_operator_declaration(self, monkeypatch) -> None:
        _pin_config(monkeypatch, value=True, declared=True)
        assert sandbox_mod.unsandboxed_exec_permitted_by() == UNSANDBOXED_BY_OPERATOR

    def test_platform_default(self, monkeypatch) -> None:
        _pin_config(monkeypatch, value=True, declared=False)
        assert sandbox_mod.unsandboxed_exec_permitted_by() == UNSANDBOXED_BY_PLATFORM

    def test_refused_names_nobody(self, monkeypatch) -> None:
        _pin_config(monkeypatch, value=False, declared=True)
        assert sandbox_mod.unsandboxed_exec_permitted_by() == ""

    def test_source_follows_a_patched_gate(self, monkeypatch) -> None:
        """The source is DERIVED from the gate's verdict, so a test that pins the
        gate cannot end up with an audit string describing the real host instead."""
        monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)
        assert sandbox_mod.unsandboxed_exec_permitted_by() == ""


class TestWrapArgvOnABackendlessHost:
    """What the spawn path actually does, and what it records."""

    @pytest.fixture
    def no_backend(self, monkeypatch):
        monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox_mod, "_last_unshare_failure", (False, "not Linux", ""))
        monkeypatch.setattr(sandbox_mod, "_inside_macos_sandbox", lambda: False)
        monkeypatch.setattr(sandbox_mod, "_governance_sandbox_floor", lambda: "")

    def test_platform_default_passes_through_and_audits_unconfined(
        self, no_backend, monkeypatch
    ) -> None:
        _pin_config(monkeypatch, value=True, declared=False)
        sel_instance = MagicMock()
        with patch("kiro_crew.sel.sel", return_value=sel_instance):
            wrapped, cleanup = wrap_argv(list(_ARGV), mode="standard")
        assert wrapped == _ARGV
        assert cleanup is None
        events = sel_instance.log_tool_invocation.call_args_list
        assert [c.kwargs["outcome"] for c in events] == ["unconfined"]
        assert "platform default" in events[0].kwargs["resources"]

    def test_operator_grant_audits_its_own_source(self, no_backend, monkeypatch) -> None:
        _pin_config(monkeypatch, value=True, declared=True)
        sel_instance = MagicMock()
        with patch("kiro_crew.sel.sel", return_value=sel_instance):
            wrap_argv(list(_ARGV), mode="standard")
        event = sel_instance.log_tool_invocation.call_args_list[0].kwargs
        assert event["outcome"] == "unconfined"
        assert "operator declared" in event["resources"]

    def test_audit_failure_does_not_block_the_spawn(self, no_backend, monkeypatch) -> None:
        """Denying the spawn on a SEL hiccup would brick every agent subprocess on
        the one platform this path exists to serve."""
        _pin_config(monkeypatch, value=True, declared=False)
        failing = MagicMock()
        failing.log_tool_invocation.side_effect = OSError("audit log unwritable")
        with patch("kiro_crew.sel.sel", return_value=failing):
            wrapped, _ = wrap_argv(list(_ARGV), mode="standard")
        assert wrapped == _ARGV

    def test_declared_false_still_raises_and_says_so(self, no_backend, monkeypatch) -> None:
        """The refusal must name the operator's own decision, not tell them to set a
        key they already set."""
        _pin_config(monkeypatch, value=False, declared=True)
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as excinfo:
                wrap_argv(list(_ARGV), mode="standard")
        assert "is set to false" in str(excinfo.value)
        assert "allow_unsandboxed_exec is not set" not in str(excinfo.value)

    def test_governance_floor_outranks_the_platform_default(self, no_backend, monkeypatch) -> None:
        """``config.json`` is not policy, and neither is the platform: a managed
        fleet keeps fail-closing on Windows too."""
        _pin_config(monkeypatch, value=True, declared=False)
        monkeypatch.setattr(sandbox_mod, "_governance_sandbox_floor", lambda: "strict")
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError):
                wrap_argv(list(_ARGV), mode="standard")

    def test_transient_probe_failure_follows_the_opt_in_semantics(
        self, no_backend, monkeypatch
    ) -> None:
        """A permitted spawn does not re-classify the probe failure, and the platform
        default is deliberately no different from the operator opt-in here.

        The "a transient failure still raises" rule belongs to the FIRST-PARTY
        CARVE-OUT, which requires a ``no_backend`` classification; the broad
        permission has always passed through without consulting the class. Keeping
        the platform default identical to the opt-in means one set of semantics to
        reason about, and it costs nothing real: the Windows probe records
        ``(False, "not Linux", "")``, so a Windows host never classifies transient,
        and ``foreign_sandbox`` requires macOS. Pinned so that if the permitted path
        ever DOES start classifying, it is a deliberate change and not a drift.
        """
        _pin_config(monkeypatch, value=True, declared=False)
        monkeypatch.setattr(sandbox_mod, "_last_unshare_failure", (True, "fork EAGAIN", "retry"))
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            platform_wrapped, _ = wrap_argv(list(_ARGV), mode="standard")

        delattr(sandbox_mod.wrap_argv, "_warned")
        _pin_config(monkeypatch, value=True, declared=True)
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            operator_wrapped, _ = wrap_argv(list(_ARGV), mode="standard")

        assert platform_wrapped == operator_wrapped == _ARGV

    def test_a_refusing_host_still_classifies_transient(self, no_backend, monkeypatch) -> None:
        """The classification still matters where it decides something: on the
        REFUSAL path, where it picks the guidance and gates the carve-out."""
        _pin_config(monkeypatch, value=False, declared=False)
        monkeypatch.setattr(sandbox_mod, "_last_unshare_failure", (True, "fork EAGAIN", "retry"))
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as excinfo:
                wrap_argv(list(_ARGV), mode="standard")
        assert excinfo.value.kind == "transient"

    def test_foreign_sandbox_still_refuses_a_host_that_declared_nothing(
        self, no_backend, monkeypatch
    ) -> None:
        """A foreign outer sandbox means the host's sandbox is FINE. No platform
        grants this: the platform default is false on macOS, which is the only
        platform where a foreign Seatbelt can be detected."""
        _pin_config(monkeypatch, value=False, declared=False)
        monkeypatch.setattr(sandbox_mod, "_inside_macos_sandbox", lambda: True)
        with patch("kiro_crew.sel.sel", return_value=MagicMock()):
            with pytest.raises(SandboxUnavailableError) as excinfo:
                wrap_argv(list(_ARGV), mode="standard")
        assert excinfo.value.kind == "foreign_sandbox"

    def test_warning_on_a_platform_grant_names_the_lockdown(
        self, no_backend, monkeypatch, caplog
    ) -> None:
        """ "Install a sandbox backend" is unactionable where none exists, and a
        warning whose only suggestion is impossible trains readers to ignore it."""
        _pin_config(monkeypatch, value=True, declared=False)
        with caplog.at_level("WARNING"):
            with patch("kiro_crew.sel.sel", return_value=MagicMock()):
                wrap_argv(list(_ARGV), mode="standard")
        security = [r.getMessage() for r in caplog.records if "SECURITY" in r.getMessage()]
        assert len(security) == 1
        assert "sandbox_allow_unsandboxed_exec=false" in security[0]
        assert "Install a supported sandbox" not in security[0]
