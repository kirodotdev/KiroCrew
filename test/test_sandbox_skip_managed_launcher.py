"""An edition's managed launcher is skipped on every spawn no Crew wrap confines.

An edition may install ``kiro-cli`` as a managed launcher that runs its own sandbox
before the real executable, and resolve it to that executable through the
``AgentExecutableResolver`` seam. Crew's namespace and Seatbelt wraps already ran the
seam. Every other branch of ``wrap_argv`` ran the launcher. The rule here:

* Crew's namespace and Seatbelt wraps: the resolved executable runs, as before;
* every other branch (inside a Crew sandbox, sandbox ``off``, no backend on an
  opted-in host, or a spawn delegated to kiro-cli's own sandbox): the resolved
  executable runs only for a tool-less child. A child with tools keeps the
  launcher, whose sandbox may be the only one or part of the delegated one.

One case per host row, plus the client that hands the tool-less verdict to the wrap.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_launch_capture import capture

from kiro_crew import sandbox
from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.sandbox import wrap_argv, wrap_argv_async

LAUNCHER = "/opt/edition/launcher/kiro-cli"
DIRECT = "/opt/edition/bundle/kiro-cli"


class _EditionResolver:
    """An edition seam that knows one launcher; identity for anything else."""

    def resolve_executable(self, executable: str) -> str:
        return DIRECT if executable == LAUNCHER else executable


class _EditionContext:
    """The live platform context with the edition's resolver in place of the public one."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.agent_executable = _EditionResolver()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def edition(monkeypatch: pytest.MonkeyPatch) -> None:
    real = sandbox.current_context()
    monkeypatch.setattr("kiro_crew.sandbox.current_context", lambda: _EditionContext(real))
    # Ungoverned host, outside any Crew sandbox, unless a case says otherwise.
    monkeypatch.setattr("kiro_crew.sandbox._governance_sandbox_floor", lambda: None)
    monkeypatch.setattr("kiro_crew.sandbox._inside_kirocrew_sandbox", lambda: False)


def _executable(argv: list[str]) -> str:
    """The command the wrap runs: the first argv entry naming a kiro-cli."""
    return next(part for part in argv if part.endswith("/kiro-cli"))


# ── Nothing confines the child: only a tool-less one skips the launcher ──────


@pytest.mark.parametrize(("tool_less", "expected"), [(False, LAUNCHER), (True, DIRECT)])
def test_sandbox_off(
    edition: None, monkeypatch: pytest.MonkeyPatch, tool_less: bool, expected: str
) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    argv, cleanup = wrap_argv([LAUNCHER, "acp"], mode="off", is_kiro_cli=True, tool_less=tool_less)
    assert argv == [expected, "acp"]
    assert cleanup is None


@pytest.mark.parametrize(("tool_less", "expected"), [(False, LAUNCHER), (True, DIRECT)])
def test_no_backend_on_an_opted_in_host(
    edition: None, monkeypatch: pytest.MonkeyPatch, tool_less: bool, expected: str
) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: True)
    with (
        patch("kiro_crew.sandbox.detect_backend", return_value="none"),
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
    ):
        argv, cleanup = wrap_argv(
            [LAUNCHER, "acp"], mode="auto", is_kiro_cli=True, tool_less=tool_less
        )
    assert argv == [expected, "acp"]
    assert cleanup is None


@pytest.mark.parametrize("tool_less", [False, True])
def test_no_backend_first_party_passthrough_keeps_what_it_was_given(
    edition: None, monkeypatch: pytest.MonkeyPatch, tool_less: bool
) -> None:
    """The fixed-argv probes are never tool-less spawns; the seam does not run there."""
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: False)
    monkeypatch.setattr("kiro_crew.sandbox._classify_unavailable", lambda transient: "no_backend")
    with (
        patch("kiro_crew.sandbox.detect_backend", return_value="none"),
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
    ):
        argv, _cleanup = wrap_argv(
            [LAUNCHER, "--version"],
            mode="auto",
            first_party_fixed_argv=True,
            tool_less=tool_less,
        )
    assert _executable(argv) == LAUNCHER


# ── Delegated to kiro-cli's own sandbox: only a tool-less child skips it ─────
# A managed launcher may be part of the sandbox the delegation relies on
# (docs/architecture/security-deep-dive.md), so a child with tools keeps it.


def _kiro_internal_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = tmp_path / "kiro-settings.json"
    settings.write_text('{"sandbox": true}')
    monkeypatch.setattr("kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH", str(settings))
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "darwin")


@pytest.mark.parametrize(("tool_less", "expected"), [(False, LAUNCHER), (True, DIRECT)])
def test_macos_delegated_to_kiro_clis_own_sandbox(
    edition: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_less: bool, expected: str
) -> None:
    """Red before the fix for a tool-less child: it ran the launcher."""
    _kiro_internal_sandbox(monkeypatch, tmp_path)
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        argv, cleanup = wrap_argv(
            [LAUNCHER, "acp"], mode="auto", is_kiro_cli=True, tool_less=tool_less
        )
    assert argv[-2:] == [expected, "acp"]
    assert cleanup is None


@pytest.mark.parametrize(("tool_less", "expected"), [(False, LAUNCHER), (True, DIRECT)])
def test_macos_sandbox_off_with_kiro_clis_own_sandbox(
    edition: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_less: bool, expected: str
) -> None:
    _kiro_internal_sandbox(monkeypatch, tmp_path)
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        argv, _cleanup = wrap_argv(
            [LAUNCHER, "acp"], mode="off", is_kiro_cli=True, tool_less=tool_less
        )
    assert argv[-2:] == [expected, "acp"]


@pytest.mark.parametrize(("tool_less", "noted"), [(False, False), (True, True)])
def test_the_mode_off_delegated_audit_record_names_what_ran(
    edition: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_less: bool, noted: bool
) -> None:
    """Red before the fix: the record was written from the launcher argv, with no note."""
    _kiro_internal_sandbox(monkeypatch, tmp_path)
    audit = MagicMock()
    with patch("kiro_crew.sel.sel", return_value=audit):
        wrap_argv([LAUNCHER, "acp"], mode="off", is_kiro_cli=True, tool_less=tool_less)
    call = audit.log_tool_invocation.call_args.kwargs
    assert ("edition launcher was skipped" in call["resources"]) is noted


@pytest.mark.parametrize(("tool_less", "noted"), [(False, False), (True, True)])
def test_the_no_backend_audit_record_names_a_skipped_launcher(
    edition: None, monkeypatch: pytest.MonkeyPatch, tool_less: bool, noted: bool
) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    monkeypatch.setattr("kiro_crew.sandbox._allow_unsandboxed_exec", lambda: True)
    audit = MagicMock()
    with (
        patch("kiro_crew.sandbox.detect_backend", return_value="none"),
        patch("kiro_crew.sel.sel", return_value=audit),
    ):
        wrap_argv([LAUNCHER, "acp"], mode="auto", is_kiro_cli=True, tool_less=tool_less)
    resources = audit.log_tool_invocation.call_args.kwargs["resources"]
    assert ("edition launcher was skipped" in resources) is noted


@pytest.mark.parametrize("tool_less", [False, True])
def test_the_nested_passthrough_audit_record_says_whether_the_launcher_was_skipped(
    edition: None, monkeypatch: pytest.MonkeyPatch, tool_less: bool
) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    monkeypatch.setattr("kiro_crew.sandbox._inside_kirocrew_sandbox", lambda: True)
    monkeypatch.setattr("kiro_crew.sandbox._macos_sandbox_state", lambda: None)
    audit = MagicMock()
    with patch("kiro_crew.sel.sel", return_value=audit):
        wrap_argv([LAUNCHER, "acp"], mode="auto", is_kiro_cli=True, tool_less=tool_less)
    assert audit.log_tool_invocation.call_args.kwargs["metadata"]["launcher_skipped"] is tool_less


# ── Inside a Crew sandbox, and Crew's own wraps ──────────────────────────────


@pytest.mark.parametrize(("tool_less", "expected"), [(False, LAUNCHER), (True, DIRECT)])
def test_inside_a_crew_sandbox(
    edition: None, monkeypatch: pytest.MonkeyPatch, tool_less: bool, expected: str
) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    monkeypatch.setattr("kiro_crew.sandbox._inside_kirocrew_sandbox", lambda: True)
    monkeypatch.setattr("kiro_crew.sandbox._macos_sandbox_state", lambda: None)
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        argv, _cleanup = wrap_argv(
            [LAUNCHER, "acp"], mode="auto", is_kiro_cli=True, tool_less=tool_less
        )
    assert _executable(argv) == expected


@pytest.mark.parametrize(("tool_less", "noted"), [(False, False), (True, True)])
def test_the_delegated_audit_record_says_when_the_launcher_was_skipped(
    edition: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_less: bool, noted: bool
) -> None:
    _kiro_internal_sandbox(monkeypatch, tmp_path)
    audit = MagicMock()
    with patch("kiro_crew.sel.sel", return_value=audit):
        wrap_argv([LAUNCHER, "acp"], mode="auto", is_kiro_cli=True, tool_less=tool_less)
    resources = audit.log_tool_invocation.call_args.kwargs["resources"]
    assert ("edition launcher was skipped" in resources) is noted


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace launcher is Linux")
def test_linux_namespace_wrap(edition: None) -> None:
    """Unchanged: the namespace wrap resolved the launcher before this change too."""
    with patch("kiro_crew.sandbox.detect_backend", return_value="namespace"):
        argv, cleanup = wrap_argv([LAUNCHER, "acp"], mode="strict")
    try:
        assert _executable(argv) == DIRECT
        assert LAUNCHER not in argv
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


# ── What the seam does not recognise is left alone ───────────────────────────


def test_an_executable_the_edition_does_not_know_is_unchanged(
    edition: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    argv, _cleanup = wrap_argv(["/usr/bin/kiro-cli", "acp"], mode="off", tool_less=True)
    assert argv == ["/usr/bin/kiro-cli", "acp"]


def test_the_public_seam_is_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "linux")
    monkeypatch.setattr("kiro_crew.sandbox._governance_sandbox_floor", lambda: None)
    argv, _cleanup = wrap_argv([LAUNCHER, "acp"], mode="off", tool_less=True)
    assert argv == [LAUNCHER, "acp"]
    assert sandbox._direct_executable_argv([], tool_less=True) == []


# ── The client hands the tool-less verdict to the wrap ───────────────────────


def _client_wrap_kwargs(
    tmp_path: Path, *, authored: bool, launched: bool
) -> tuple[dict[str, Any], list[bool]]:
    """Spawn a kiro-cli client whose authored spec and launched spec answer as given.

    *launched* stands for ``effective_spec_declares_zero_tools``: the prepared view
    and its published alias when a projection names one. Returns the wrap's keyword
    arguments and each ``authored_before_spawn`` the client passed to that check.
    """
    wrap_kwargs: dict[str, Any] = {}
    passed: list[bool] = []

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        wrap_kwargs.update(kwargs)
        return list(argv), None

    def _effective(self: AcpClient, *, authored_before_spawn: bool = False) -> bool:
        passed.append(authored_before_spawn)
        return launched

    capture(
        ACP_BACKEND_KIRO,
        tmp_path,
        extra_patches=[
            patch.object(
                client_mod, "_resolve_kiro_bin_for_spawn", new=AsyncMock(return_value=LAUNCHER)
            ),
            patch.object(client_mod, "wrap_argv_async", side_effect=_wrap),
            patch("kiro_crew.kiro_cli.kiro_acp_engine_args", return_value=[]),
            patch.object(AcpClient, "authored_spec_declares_zero_tools", lambda self: authored),
            patch.object(AcpClient, "effective_spec_declares_zero_tools", _effective),
        ],
    )
    return wrap_kwargs, passed


@pytest.mark.parametrize("zero_tools", [True, False])
def test_the_client_tells_the_wrap_whether_its_spec_mounts_a_tool(
    tmp_path: Path, zero_tools: bool
) -> None:
    """Red before the fix: the wrap was never told, so a tool-less client kept the launcher."""
    wrap_kwargs, passed = _client_wrap_kwargs(tmp_path, authored=zero_tools, launched=zero_tools)
    assert wrap_kwargs["tool_less"] is zero_tools
    assert passed and passed[0] is zero_tools


@pytest.mark.parametrize(("authored", "launched"), [(True, False), (False, True)])
def test_the_client_judges_the_spec_it_launches_not_the_authored_one(
    tmp_path: Path, authored: bool, launched: bool
) -> None:
    """The launched spec decides, in both directions.

    A view can add a tool the authored spec lacks (skill search for ``skill://``
    resources): that child keeps the launcher. An authored file can keep a
    registered injection while the alias launched is clean: that child skips it.
    """
    wrap_kwargs, _passed = _client_wrap_kwargs(tmp_path, authored=authored, launched=launched)
    assert wrap_kwargs["tool_less"] is launched


def test_a_client_on_a_host_with_its_own_launch_is_never_tool_less(tmp_path: Path) -> None:
    """Only the client's own kiro-cli arm loads one agent's spec with ``--agent``."""
    wrap_kwargs: dict[str, Any] = {}

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        wrap_kwargs.update(kwargs)
        return list(argv), None

    capture(
        ACP_BACKEND_CLAUDE,
        tmp_path,
        extra_patches=[
            patch.object(client_mod, "wrap_argv_async", side_effect=_wrap),
            patch.object(AcpClient, "authored_spec_declares_zero_tools", lambda self: True),
        ],
    )
    assert wrap_kwargs["tool_less"] is False


@pytest.mark.parametrize("tool_less", [True, False])
def test_the_async_wrap_hands_the_verdict_to_the_wrap(tool_less: bool) -> None:
    seen: dict[str, Any] = {}

    def _prepare(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        seen.update(kwargs)
        return list(argv), None

    asyncio.run(
        wrap_argv_async([LAUNCHER, "acp"], mode="off", tool_less=tool_less, _prepare=_prepare)
    )
    assert seen.get("tool_less", False) is tool_less
