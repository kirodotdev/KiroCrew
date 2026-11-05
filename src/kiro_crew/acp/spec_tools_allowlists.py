"""A direct-client session's ``toolsSettings`` shell and write allowlists.

kiro-cli enforces an agent spec's ``toolsSettings.shell.denyByDefault`` (only the
``allowedCommands`` run) and ``toolsSettings.fs_write.fallbackAction: "deny"``
(only the ``allowedPaths`` are written) itself. A harness in
``ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS`` never reads either, so the
direct ``AcpClient`` reads them here from the spec it projects, stamps them on the
session's own permission requests, and the gate's ``spec-shell-allowlist`` and
``spec-write-allowlist`` rows refuse what they do not cover, on every approval
policy. The readers are the ones the KAS projection uses
(``kas_permissions.spec_shell_allowlist``, ``kas_permissions.spec_write_allowlist``
and ``kas_write_allowlist.anchor_write_allowlist``), so both transports refuse the
same calls.

The gate only sees a call the harness asks about. :func:`claude_ask_rules` names
the claude ``permissions.ask`` rules the settings seed adds while either allowlist
is set, so the shell and the file-write tools raise a permission request even where
claude would approve the call on its own (a read-only command, an edit under
``acceptEdits``, an allow rule in another settings file: on claude an ``ask`` rule
outranks an ``allow``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.acp.kas_permissions import spec_shell_allowlist, spec_write_allowlist
from kiro_crew.acp.kas_write_allowlist import anchor_write_allowlist
from kiro_crew.acp.session_mcp import _session_spec_and_snapshot_for

#: claude's shell tool, asked about while the spec's shell allowlist is set.
CLAUDE_SHELL_ASK_RULES: tuple[str, ...] = ("Bash",)

#: claude's file-write tools, asked about while the spec's write allowlist is set.
CLAUDE_WRITE_ASK_RULES: tuple[str, ...] = ("Edit", "MultiEdit", "NotebookEdit", "Write")


def session_allowlist_spec(agent: str, work_dir: str | Path | None) -> dict[str, Any] | None:
    """The spec a mirrored session's allowlists are read from, ``None`` when unreadable.

    The mirrored read the session's MCP projection resolves (``mirrored_session``):
    an untrusted checkout's same-named spec yields the user-level spec with only the
    checkout's switch-off keys, so a cloned repository cannot drop the allowlists the
    user wrote. The settings seed's ask rules and the gate's stamps both come from it,
    so one never covers a call the other does not.
    """
    return _session_spec_and_snapshot_for(agent, work_dir)[0]


def _tools_settings(spec: Any) -> tuple[Any, Any]:
    """The spec's ``toolsSettings`` and ``allowedTools``, ``None`` for a non-dict spec."""
    if not isinstance(spec, dict):
        return None, None
    return spec.get("toolsSettings"), spec.get("allowedTools")


def spec_shell_patterns(spec: Any) -> tuple[str, ...] | None:
    """The only shell commands *spec* lets run, or ``None`` when it sets no allowlist.

    No KAS policy is passed: on this transport no ``deniedCommands`` entry rides a
    harness deny rule, so an ``allowedTools`` that names the shell lifts
    ``denyByDefault`` only when the spec denies no command at all
    (``kas_permissions.shell_denies_carried``), as kiro-cli lifts it.
    """
    tools_settings, allowed_tools = _tools_settings(spec)
    patterns = spec_shell_allowlist(tools_settings, allowed_tools, None)
    return None if patterns is None else tuple(patterns)


def spec_write_globs(spec: Any, cwd: str) -> tuple[str, ...] | None:
    """The absolute globs *spec*'s write tool may touch, anchored at *cwd*, or ``None``."""
    tools_settings, _ = _tools_settings(spec)
    patterns = spec_write_allowlist(tools_settings)
    return None if patterns is None else anchor_write_allowlist(patterns, cwd)


def claude_ask_rules_for_allowlists(shell: Any, write: Any) -> list[str]:
    """The claude ``permissions.ask`` rules for a shell and a write allowlist.

    ``None`` for either means that list is not set. Empty when neither is, so a
    session whose spec asks for no restriction prompts exactly as before.
    """
    rules: list[str] = []
    if shell is not None:
        rules.extend(CLAUDE_SHELL_ASK_RULES)
    if write is not None:
        rules.extend(CLAUDE_WRITE_ASK_RULES)
    return rules


def claude_ask_rules(spec: Any) -> list[str]:
    """The claude ``permissions.ask`` rules that make *spec*'s allowlisted tools ask."""
    tools_settings, _ = _tools_settings(spec)
    return claude_ask_rules_for_allowlists(
        spec_shell_patterns(spec), spec_write_allowlist(tools_settings)
    )
