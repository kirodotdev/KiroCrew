"""Doctor reporting for a non-default ACP backend.

One section, driven by the backend registry rather than by an if-chain per
backend, so registering a backend is what makes it reportable.

Kept out of ``cli_doctor`` itself because it needs the acp package, which pulls in
the client; ``cli_doctor`` is imported by the CLI on every invocation and should
not pay that cost to print rows nobody asked for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from kiro_crew.acp import backends as acp_backends
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)

#: Adapters whose binary resolution Kiro Crew owns, so the doctor can report a
#: real reading rather than a guess.
#:
#: A named set rather than an identity test (harness-parity H5): an inequality
#: against one backend hands these branches to every harness added later, which
#: is how a doctor row starts asserting a resolution ladder that does not exist.
#: Membership is a deliberate edit — an adapter joins when Kiro Crew actually
#: gains a ladder for it, not because it resembles one that has one.
_ADAPTERS_WITH_OWNED_LADDER = frozenset(
    {
        ACP_BACKEND_CODEX,
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_OPENCODE,
        ACP_BACKEND_PI,
    }
)

#: Credential-file reading is a narrower set than binary resolution. Claude,
#: goose, OpenCode and pi authenticate through a vendor CLI Kiro Crew never
#: reads, so a missing token file is not a doctor issue there — only Codex has
#: a leaf we can stat.
_ADAPTERS_WITH_OWNED_CREDENTIAL = frozenset({ACP_BACKEND_CODEX})

#: Capability key -> the literal safe to print in doctor output. CodeQL treats
#: the internal ``billing`` key as private data, so passing keys through to the
#: output creates a high-severity false positive. Reading a literal value from a
#: complete table keeps the diagnostic exhaustive without carrying that taint.
_CAPABILITY_NOTE_LABELS: dict[str, str] = {
    acp_backends.CAP_SESSION_SHARING: "session_sharing",
    acp_backends.CAP_REASONING_EFFORT: "reasoning_effort",
    acp_backends.CAP_TOOL_SEARCH: "mcp_tool_search",
    acp_backends.CAP_AGENT_PROFILES: "agent_profiles",
    acp_backends.CAP_SLASH_COMMANDS: "slash_commands",
    acp_backends.CAP_TURN_USAGE: "turn_usage",
    acp_backends.CAP_BILLING: "cost_reporting",
    acp_backends.CAP_NATIVE_RESUME: "native_resume",
    acp_backends.CAP_REGISTRY_MODEL_IDS: "registry_model_ids",
    acp_backends.CAP_MID_TURN_STEER: "mid_turn_steer",
}


def report(
    backend: str,
    work_dir: Path | str,
    *,
    allow_ungated: bool,
    emit: Callable[[str], None],
    issues: list[str],
) -> None:
    """Print the active backend's rows, appending real problems to ``issues``.

    Nothing is printed for the default backend: an installation that never opted
    in should see the doctor output it sees today.

    An ``issues`` entry means "a human must act". A capability row is a NOTE
    rather than an issue — degraded reasoning-effort support is a documented
    property of the backend, not a fault, and listing it as a problem would train
    the reader to ignore the section.
    """
    if not backend:
        return

    descriptor = acp_backends.descriptor_for(backend)
    emit("\nACP Backend")
    marker = " (experimental)" if descriptor.experimental else ""
    emit(f"  backend:     {descriptor.label}{marker} [agent.acp_backend={backend}]")

    _report_adapter(descriptor, emit, issues)
    _report_signin(descriptor, emit, issues)
    _report_routing(backend, work_dir, allow_ungated=allow_ungated, emit=emit, issues=issues)
    _report_capabilities(backend, emit)


def _report_adapter(
    descriptor: acp_backends.BackendDescriptor,
    emit: Callable[[str], None],
    issues: list[str],
) -> None:
    """Whether the adapter binary resolved, and the ladder when it did not."""
    # Positive membership (harness-parity H5). An inequality against one backend
    # would hand this branch to every harness added later; only adapters with a
    # Kiro-Crew-owned resolution ladder have anything to report here.
    if descriptor.id not in _ADAPTERS_WITH_OWNED_LADDER:
        return
    argv, missing = _resolve_owned_adapter(descriptor)
    if argv:
        emit(f"  adapter:     ✅ {' '.join(argv)}")
        return
    emit("  adapter:     ❌ not found")
    for line in missing.split(". "):
        if line.strip():
            emit(f"               {line.strip().rstrip('.')}.")
    issues.append(f"{descriptor.label}: ACP adapter not found")


def _resolve_owned_adapter(
    descriptor: acp_backends.BackendDescriptor,
) -> tuple[list[str] | None, str]:
    """Dispatch the owned resolver by backend id (harness-parity H5)."""
    if descriptor.id == ACP_BACKEND_CODEX:
        from kiro_crew.acp import codex

        return codex.resolve_argv_cached(), codex.missing_adapter_message()
    if descriptor.id == ACP_BACKEND_CLAUDE:
        from kiro_crew.acp.client import _resolve_claude_acp_bin_cached

        argv = _resolve_claude_acp_bin_cached()
        missing = (
            "claude-agent-acp not found. Install with "
            f"`{descriptor.install_command}`, or set CLAUDE_AGENT_ACP_BIN "
            "to its entry script."
        )
        return argv, missing
    if descriptor.id == ACP_BACKEND_GOOSE:
        from kiro_crew.acp import goose

        return goose.resolve_argv_cached(), goose.missing_adapter_message()
    if descriptor.id == ACP_BACKEND_OPENCODE:
        from kiro_crew.acp import opencode

        return opencode.resolve_argv_cached(), opencode.missing_adapter_message()
    if descriptor.id == ACP_BACKEND_PI:
        from kiro_crew.acp import pi

        return pi.resolve_argv_cached(), pi.missing_adapter_message()
    return None, ""


def _report_signin(
    descriptor: acp_backends.BackendDescriptor,
    emit: Callable[[str], None],
    issues: list[str],
) -> None:
    """Whether the vendor CLI has persisted credentials Kiro Crew never reads."""
    # Positive membership, same reason as _report_adapter: only an adapter whose
    # credential file Kiro Crew can locate gets a real sign-in reading. Everything
    # else is owned by its vendor CLI, which is the honest answer rather than a
    # guess dressed as a check. Narrower than the resolution ladder — Claude,
    # goose, OpenCode and pi resolve a binary we own but authenticate through a
    # CLI we do not read.
    if descriptor.id not in _ADAPTERS_WITH_OWNED_CREDENTIAL:
        emit(f"  sign-in:     ⏹ owned by the vendor CLI ({descriptor.signin_command})")
        return
    from kiro_crew.acp import codex

    if codex.signin_state() == codex.SIGNIN_PRESENT:
        emit(f"  sign-in:     ✅ {codex.auth_json_path()}")
        return
    # NOT an issue: a turn has been driven on a host where this file was absent,
    # so appending to `issues` here would fail the doctor over a working install
    # and send the operator to re-run a login they do not need.
    emit(f"  sign-in:     ❔ no persisted token at {codex.auth_json_path()}")
    emit("               The adapter has other credential channels, so this")
    emit("               is not proof it cannot authenticate.")
    emit(f"               If a turn fails on auth: {codex.signin_hint()}")


def _report_routing(
    backend: str,
    work_dir: Path | str,
    *,
    allow_ungated: bool,
    emit: Callable[[str], None],
    issues: list[str],
) -> None:
    """Whether tool calls reach Kiro Crew's PreToolUse gate.

    The most load-bearing row in the section: a bypassing or unestablishable
    verdict means the denied-command rules, the sensitive-path block and the
    governance ceiling are not consulted for the tools the backend self-approves.
    """
    from kiro_crew.acp import tool_gate

    routing = acp_backends.descriptor_for(backend).routing
    verdict, reason = tool_gate.resolve_verdict(backend, work_dir)
    if verdict is Verdict.ROUTED:
        emit(f"  tool gate:   ✅ {reason} — tool calls reach Kiro Crew's checks")
        # Scope the ✅ where it is narrower than the row implies. ACP v1 has no way
        # to make an adapter ask before a passive read, so a session-config route
        # covers commands and changes only. Saying so is what keeps the row from
        # reading as "every tool asks", which is the claim that sent an operator
        # looking for a prompt that was never going to arrive.
        if routing is acp_backends.Routing.SESSION_CONFIG:
            emit("               Covers commands and file changes. Passive reads run")
            emit("               without asking — ACP v1 cannot require a prompt for them.")
    else:
        emit(f"  tool gate:   ❌ {verdict.value}: {reason}")
        emit("               The denied-command rules, the sensitive-path block and")
        emit("               the governance ceiling are NOT consulted for tool calls")
        emit("               this backend approves on its own.")
        remedy = tool_gate.remediation_for(backend, work_dir)
        if remedy:
            emit(f"               Fix: {remedy}")
        issues.append(f"{backend}: tool calls bypass the PreToolUse gate")

    if allow_ungated:
        # An issue even when the verdict is ROUTED: the opt-out disarms the
        # refusal for every FUTURE session too, so the operator should know it is
        # on regardless of today's configuration.
        emit("  opt-out:     ⚠️  agent.acp_backend_allow_ungated_tools is ENABLED")
        emit("               Sessions start even when tool calls bypass the gate.")
        issues.append("agent.acp_backend_allow_ungated_tools is enabled")


def _report_capabilities(backend: str, emit: Callable[[str], None]) -> None:
    """One note per capability this backend does not fully support."""
    rows: list[str] = []
    for capability in acp_backends.ALL_CAPABILITIES:
        level = acp_backends.level(backend, capability)
        if level is acp_backends.Level.SUPPORTED:
            continue
        rows.append(f"{_CAPABILITY_NOTE_LABELS[capability]}={level.value}")
    if not rows:
        return
    emit(f"  capabilities: ⏹ {len(rows)} differ from kiro-cli or are unverified")
    for row in rows:
        emit(f"               - {row}")
    from kiro_crew.acp import spec_servers

    if spec_servers.crew_mcp_forwarding_unverified(backend):
        emit("  crew mcp:    ⏹ delivered on session/new; official pi-acp may")
        emit("               not forward those tools to the model, so spawn")
        emit("               and Crew tools can stay inert until it does.")


__all__ = ["report"]
