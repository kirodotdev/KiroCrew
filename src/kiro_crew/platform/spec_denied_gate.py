"""The gate's half of a KAS spec grant that holds only while Crew checks its denies.

``ToolCall.spec_denied_grant`` (:mod:`kiro_crew.platform.spec_denied_grant`)
carries grants a spec's ``deniedCommands`` / write ``deniedPaths`` held back, with
those denies. :func:`spec_denies_refusal` backs the ``spec-denies`` deny row: it refuses
a call a deny names, on every approval policy. :func:`denied_grant_applies` is
asked by the ``spec-builtin-grant`` row: it grants only a call a deny check
cleared, and treats every unknown (a command it cannot split, a target it cannot
read or resolve) as no grant, so the call asks.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from kiro_crew.platform import allowed_commands, allowed_paths
from kiro_crew.platform.tool_paths import edit_target_candidates, is_edit_call

if TYPE_CHECKING:
    from kiro_crew.hooks import ToolCall
    from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant

#: kiro-cli's shell and write names, as :func:`gate_tiers._spec_builtin_names` gives them.
_SHELL_NAMES = frozenset({"execute_bash", "execute_pwsh"})
_WRITE_NAMES = frozenset({"fs_write", "fs_append", "str_replace"})


def _path_denied(target: str, patterns: Sequence[str], cwd: str) -> bool | None:
    """Whether a deny in *patterns* covers the write *target*, or ``None`` (unknown).

    Read wider than :func:`gate_tiers._write_target_uncovered` reads an allowlist:
    the target is denied when ANY of its spellings (as written, normalized, or
    symlink-resolved) is covered, or covered by an entry whose directory part is
    resolved through a link. A relative target with no session directory, or a
    resolution that stalls or fails, is unknown.
    """
    from kiro_crew.security.paths_resolver import PathResolutionStalled, _resolved_forms_bounded

    expanded = os.path.expanduser(target)
    if not os.path.isabs(expanded):
        if not os.path.isabs(cwd):
            return None
        expanded = os.path.join(cwd, expanded)
    try:
        resolved = _resolved_forms_bounded(expanded)
    except PathResolutionStalled:
        return None
    if not resolved:
        return None
    forms = [os.path.normpath(expanded), *sorted(resolved)]
    every: list[str] = list(patterns)
    for pattern in patterns:
        prefix = allowed_paths.literal_prefix_dir(pattern)
        try:
            reals = _resolved_forms_bounded(prefix)
        except PathResolutionStalled:
            return None
        rest = pattern[len(prefix) :].lstrip("/")
        for real in reals:
            real = allowed_paths.to_glob_path(real)
            if real == prefix or not allowed_paths.is_absolute(real):
                continue
            twin = allowed_paths.anchor(allowed_paths.join(real, rest) if rest else real, "", "")
            if twin is not None:
                every.append(twin)
    return any(allowed_paths.covered(every, form) for form in forms)


def _write_denied(call: ToolCall, grant: SpecDeniedGrant) -> bool | None:
    """Whether a write deny covers any target of *call*, or ``None`` (unknown)."""
    candidates = edit_target_candidates(call.raw_params, call.diff_path, tool_kind=call.kind)
    if candidates.truncated or candidates.unanchored or not candidates:
        return None
    answers = [_path_denied(t, grant.denied_write_paths, grant.cwd) for t in candidates]
    if any(a is True for a in answers):
        return True
    return None if any(a is None for a in answers) else False


def _is_write(call: ToolCall, names: tuple[str, ...]) -> bool:
    return is_edit_call(call.kind, call.diff_path) or bool(_WRITE_NAMES.intersection(names))


def spec_denies_refusal(call: ToolCall, names: tuple[str, ...]) -> str | None:
    """Why the ``spec-denies`` row refuses *call*, or ``None``: its spec's checked
    ``deniedCommands`` or write ``deniedPaths`` names it. A ``deny_policy``: the
    refusal is the spec's state, as for ``spec-shell-allowlist``."""
    grant = call.spec_denied_grant
    if grant is None or call.mcp_server:
        return None
    prefix = "Blocked by this agent's spec: "
    if grant.denied_commands and call.is_shell and _SHELL_NAMES.intersection(names):
        if (
            allowed_commands.denied_match(grant.denied_commands, call.command, word_bounded=True)
            is True
        ):
            return prefix + "the command matches its deniedCommands"
    if grant.denied_write_paths and _is_write(call, names):
        if _write_denied(call, grant) is True:
            return prefix + "the write target is in its deniedPaths"
    return None


def denied_grant_applies(call: ToolCall, names: tuple[str, ...]) -> bool:
    """Whether *call*'s ``spec_denied_grant`` grants it: a name it lifted (or a
    command one of its ``allowedCommands`` matches), and every deny that applies to
    the call checked and clear. Anything unknown answers False."""
    grant = call.spec_denied_grant
    if grant is None or call.mcp_server:
        return False
    shell = call.is_shell and bool(_SHELL_NAMES.intersection(names))
    by_name = bool(grant.builtins.intersection(names))
    by_command = (
        shell
        and allowed_commands.plain_command(call.command)
        and allowed_commands.matches_any(grant.commands, str(call.command))
    )
    if not (by_name or by_command):
        return False
    if _SHELL_NAMES.intersection(names) and not shell:
        return False
    if shell and grant.denied_commands:
        if allowed_commands.denied_match(grant.denied_commands, call.command) is not False:
            return False
    if _is_write(call, names) and grant.denied_write_paths:
        if _write_denied(call, grant) is not False:
            return False
    return True
