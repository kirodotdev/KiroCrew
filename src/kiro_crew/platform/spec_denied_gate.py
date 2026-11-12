"""The gate's half of a KAS spec grant that holds only while Crew checks its denies.

``ToolCall.spec_denied_grant`` (:mod:`kiro_crew.platform.spec_denied_grant`)
carries grants a spec's ``deniedCommands`` / write ``deniedPaths`` held back, with
those denies. :func:`spec_denies_refusal` backs the ``spec-denies`` deny row: it refuses
a call a deny names, on every approval policy. :func:`denied_grant_applies` is
asked by the ``spec-builtin-grant`` row: it grants only a call a deny check
cleared, and treats every unknown (a command it cannot split, a target it cannot
read or resolve) as no grant, so the call asks. A ``web_fetch`` is granted by a
plain ``web_fetch.trusted`` URL only, never refused here (:func:`url_literal`).
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
_WEB_FETCH = "web_fetch"


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
    refusal is the spec's state, as for ``spec-shell-allowlist``.

    A chain is judged one command at a time (``allowed_commands.chain_commands``)
    and refused when a deny names any of them. A command that does not split there
    (a quote, ``#`` or ``[[`` in a chain, a redirect, ``$(...)``, a backtick)
    is not refused here."""
    grant = call.spec_denied_grant
    if grant is None or call.mcp_server:
        return None
    prefix = "Blocked by this agent's spec: "
    if grant.denied_commands and call.is_shell and _SHELL_NAMES.intersection(names):
        pieces = allowed_commands.chain_commands(call.command) or ()
        # A ``#`` can start a comment that holds a separator, so a piece after it
        # may never run; inside a ``[[ ... ]]`` test a separator is part of the
        # test, not a new command. Such a chain is not split here.
        command = str(call.command)
        if len(pieces) > 1 and ("#" in command or "[[" in command):
            pieces = ()
        if any(
            allowed_commands.denied_match(grant.denied_commands, piece, word_bounded=True) is True
            for piece in pieces
        ):
            return prefix + "the command matches its deniedCommands"
    if grant.denied_write_paths and _is_write(call, names):
        if _write_denied(call, grant) is True:
            return prefix + "the write target is in its deniedPaths"
    return None


#: Characters that make a kiro-cli regex more than a literal.
_URL_REGEX_SYNTAX = frozenset(".^$*+?()[]{}|\\")


def url_literal(raw: object) -> tuple[str, bool] | None:
    """A ``web_fetch.blocked`` / ``trusted`` entry as ``(literal, is_prefix)``, or
    ``None`` when it is not a plain URL or a plain URL followed by ``.*``.

    kiro-cli anchors an entry with ``^`` and ``$`` unless it already starts or ends
    with one, then matches the whole URL. Read here with string work only, never
    as a regex: an optional leading ``^`` or ``\\A``, an optional trailing ``$`` or
    ``\\z``, then literal characters (a backslash before ASCII punctuation is that
    character), and at most a final ``.*``. ``\\<`` / ``\\>`` (word boundaries in
    Rust) and a trailing ``\\$`` (which kiro-cli leaves unanchored) are refused.
    """
    if not isinstance(raw, str) or not raw:
        return None
    body = raw.removeprefix("\\A") if raw.startswith("\\A") else raw.removeprefix("^")
    if body.endswith("\\$"):
        return None
    if body.endswith("\\z") and not body.endswith("\\\\z"):
        body = body[:-2]
    elif body.endswith("$"):
        body = body[:-1]
    prefix = body.endswith(".*") and not body.endswith("\\.*")
    if prefix:
        body = body[:-2]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if not (nxt.isascii() and not nxt.isalnum() and nxt.isprintable()) or nxt in "<> ":
                return None
            out.append(nxt)
            i += 2
        elif ch in _URL_REGEX_SYNTAX:
            return None
        else:
            out.append(ch)
            i += 1
    return "".join(out), prefix


def _url_matches(entry: tuple[str, bool], url: str) -> bool:
    literal, prefix = entry
    return url.startswith(literal) if prefix else url == literal


def _web_fetch_granted(call: ToolCall, grant: SpecDeniedGrant) -> bool:
    """Whether a ``web_fetch`` call is granted: only by a ``trusted`` entry that is a
    plain URL (equal) or a plain URL plus ``.*`` (prefix), with every ``blocked``
    entry of that shape and not matching. Anything else keeps the prompt KAS asked
    for. A URL with a control character is never granted: Rust's ``.`` does not
    match a newline, so a prefix test would read the entry wider than kiro-cli."""
    if not grant.web_fetch_blocked_complete or not grant.web_fetch_trusted:
        return False
    url = call.raw_params.get("url") if isinstance(call.raw_params, dict) else None
    if not isinstance(url, str) or not url or any(ch < " " or ch == "\x7f" for ch in url):
        return False
    blocked = [url_literal(raw) for raw in grant.web_fetch_blocked]
    trusted = [url_literal(raw) for raw in grant.web_fetch_trusted]
    if any(entry is None or _url_matches(entry, url) for entry in blocked):
        return False
    return any(entry is not None and _url_matches(entry, url) for entry in trusted)


def denied_grant_applies(call: ToolCall, names: tuple[str, ...]) -> bool:
    """Whether *call*'s ``spec_denied_grant`` grants it: a name it lifted (or a
    command one of its ``allowedCommands`` matches), and every deny that applies to
    the call checked and clear. Anything unknown answers False."""
    grant = call.spec_denied_grant
    if grant is None or call.mcp_server:
        return False
    shell = call.is_shell and bool(_SHELL_NAMES.intersection(names))
    by_name = bool(grant.builtins.intersection(names))
    if _WEB_FETCH in names:
        return _web_fetch_granted(call, grant)
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
