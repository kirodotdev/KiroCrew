"""Shared command-scoped trust parsing and matching.

This module is deliberately outside ``dashboard``.  A trust grant is an
authorization decision, so chat, channels, and any future approval surface must
derive and enforce its scope with the same command-shaped helpers rather than
reaching into a UI runner or synthesizing display titles.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import re

ENV_ASSIGNMENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")

_REDIRECT_PLACEHOLDER = "\x00REDIR\x00"
_REDIRECT_RE = re.compile(r"[0-9]*>&[0-9]*|&>>?")
_COMMAND_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|&|\n|\|)\s*")
_GRANT_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")


def extract_bash_command(tool_input: str) -> str:
    """Extract the command string from shell ``tool_input`` (JSON or raw)."""
    try:
        data = json.loads(tool_input)
        if isinstance(data, dict):
            command = data.get("command", "")
            return command if isinstance(command, str) else ""
    except (json.JSONDecodeError, TypeError):
        pass
    return tool_input if isinstance(tool_input, str) else ""


def canonical_non_shell_tool(mcp_server_name: str, tool_name: str) -> str:
    """Return the ACP-compatible MCP display identity, or ``""`` on a miss.

    Both values originate in the preceding ``tool_call`` frame's
    ``_meta.kiro`` object and are recovered by ``toolCallId`` for the later
    permission frame.  A display title is deliberately not accepted here: it
    may be model-authored and two different tools may reuse the same prose.

    This wire-shaped value is intentionally DISPLAY ONLY.  ``__`` can occur in
    either component, so concatenating the pair this way is not injective and
    must never be stored or matched as durable trust authority.
    """
    if not mcp_server_name or not tool_name:
        return ""
    return f"mcp__{mcp_server_name}__{tool_name}"


def _trust_identity_component(value: str) -> str:
    """Encode one identity component for case-insensitive durable matching.

    ``matches_trusted_pattern`` historically compares with ``str.lower()``.
    Normalize with that exact operation before encoding (not ``casefold()``,
    which would silently widen the established equivalence classes), then use
    UTF-8 ``surrogatepass`` + lowercase hex.  Hex contains no delimiter, shell
    separator, or fnmatch metacharacter and remains stable under another
    ``lower()`` in the matcher.
    """
    return value.lower().encode("utf-8", "surrogatepass").hex()


def canonical_non_shell_trust_key(mcp_server_name: str, tool_name: str) -> str:
    """Return an unambiguous internal durable-trust key for one MCP tool.

    The versioned, component-encoded form is injective after the existing
    case-insensitive normalization.  In particular, ``("github",
    "repo__delete")`` cannot collide with ``("github__repo", "delete")`` as
    both did in the display/wire form ``mcp__github__repo__delete``.
    """
    if not mcp_server_name or not tool_name:
        return ""
    server = _trust_identity_component(mcp_server_name)
    tool = _trust_identity_component(tool_name)
    return f"mcp-trust:v1:{server}:{tool}"


def approval_command(
    tool_input: str,
    *,
    is_shell: bool,
    tool_name: str = "",
    mcp_server_name: str = "",
    raw_tool_params: dict | None = None,
) -> str:
    """Return the server-authoritative INTERNAL key a trust click may bind.

    Shell scope comes only from the provider's structured ``tool_input``.  A
    non-shell event is grantable only when it has no structured input and both
    halves of its ACP-cached server/tool identity are present.  Both the rendered
    ``tool_input`` and the structured params are checked; the structured params
    persist across a same-call re-prompt even if a provider omits rendered input.
    Display text is intentionally absent from this API so it cannot become
    authority later.
    """
    if is_shell:
        return extract_bash_command(tool_input) if tool_input else ""
    if not tool_input and not isinstance(raw_tool_params, dict):
        return canonical_non_shell_trust_key(mcp_server_name, tool_name)
    return ""


def approval_display_command(
    tool_input: str,
    *,
    is_shell: bool,
    tool_name: str = "",
    mcp_server_name: str = "",
    raw_tool_params: dict | None = None,
) -> str:
    """Return the user-visible consent label for :func:`approval_command`.

    Grantability is deliberately identical, but non-shell display retains the
    ACP-compatible ``mcp__server__tool`` spelling.  The pending-card handler
    stores the separately derived internal key, so UI compatibility never
    becomes authorization authority.
    """
    trust_key = approval_command(
        tool_input,
        is_shell=is_shell,
        tool_name=tool_name,
        mcp_server_name=mcp_server_name,
        raw_tool_params=raw_tool_params,
    )
    if not trust_key:
        return ""
    if is_shell:
        return trust_key
    return canonical_non_shell_tool(mcp_server_name, tool_name)


def _tool_matches(pattern: str, tool_name: str) -> bool:
    """Match the existing case-insensitive fnmatch trust language."""
    if pattern == "*":
        return True
    return fnmatch.fnmatch(tool_name.lower(), pattern.lower())


def _mask_quoted_separators(text: str, *, mask_escaped: bool = False) -> tuple[str, dict[str, str]]:
    """Mask separators inside quotes before command segmentation."""
    out: list[str] = []
    restore: dict[str, str] = {}
    quote: str | None = None
    escaped = False
    n = 0
    for ch in text:
        if escaped:
            escaped = False
            if mask_escaped and ch in "|&;\n":
                placeholder = f"\x00SEP{n}\x00"
                n += 1
                restore[placeholder] = ch
                out.append(placeholder)
            else:
                out.append(ch)
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            out.append(ch)
            continue
        if quote:
            if ch == quote:
                quote = None
            elif ch in "|&;\n":
                placeholder = f"\x00SEP{n}\x00"
                n += 1
                restore[placeholder] = ch
                out.append(placeholder)
                continue
        elif ch in ("'", '"'):
            quote = ch
        out.append(ch)
    return "".join(out), restore


def split_command_segments(
    command: str,
    split_re: re.Pattern[str] | None = None,
    mask_escaped: bool = False,
) -> tuple[str, list[str]] | None:
    """Split a command into unquoted shell segments, failing closed."""
    # ``command`` is the canonical value from structured ``tool_input``.  A
    # shell can legitimately invoke an executable named ``Reading`` or
    # ``Running:``, so presentation-prefix removal here would alias two distinct
    # commands at the authorization boundary.
    normalized = command
    if _COMMAND_SUBSTITUTION_RE.search(normalized) or "\x00" in normalized:
        return None
    quote_masked, separator_restore = _mask_quoted_separators(normalized, mask_escaped=mask_escaped)
    redirects: list[str] = []

    def _mask_redirect(match: re.Match[str]) -> str:
        redirects.append(match.group())
        return _REDIRECT_PLACEHOLDER

    masked = _REDIRECT_RE.sub(_mask_redirect, quote_masked)
    parts = (split_re or _COMMAND_SPLIT_RE).split(masked)
    redirect_iter = iter(redirects)
    segments: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        restored = part
        while _REDIRECT_PLACEHOLDER in restored:
            restored = restored.replace(_REDIRECT_PLACEHOLDER, next(redirect_iter), 1)
        for placeholder, separator in separator_restore.items():
            if placeholder in restored:
                restored = restored.replace(placeholder, separator)
        segments.append(restored)
    return normalized, segments


def matches_trusted_pattern(command: str, patterns: set[str]) -> str | None:
    """Return the trusted fnmatch pattern covering ``command``, if any.

    The input is the canonical command from ``tool_input``.  Presentation
    prefixes are never stripped here: they are ordinary executable text at this
    boundary, not UI decoration.
    """
    split = split_command_segments(command)
    if split is None:
        return None
    normalized, segments = split
    if len(segments) > 1:
        matched_patterns: list[str] = []
        for segment in segments:
            match = next((p for p in patterns if _tool_matches(p, segment)), None)
            if match is None:
                return None
            matched_patterns.append(match)
        return ",".join(matched_patterns)
    return next((p for p in patterns if _tool_matches(p, normalized)), None)


def extract_base_command(command: str) -> str:
    """Return comma-joined base binaries for a grant, or ``""`` if unsafe.

    Environment-assignment prefixes are refused instead of being turned into a
    broad ``NAME=value *`` grant.  Substitution and forged placeholders already
    fail closed in :func:`split_command_segments`.
    """
    split = split_command_segments(command, split_re=_GRANT_SPLIT_RE, mask_escaped=True)
    if split is None:
        return ""
    normalized, segments = split
    bases: list[str] = []
    for segment in segments:
        stripped = segment.strip()
        parts = stripped.split(None, 1)
        if not parts or ENV_ASSIGNMENT_RE.match(parts[0]):
            return ""
        base = parts[0]
        # The comma is our multi-command delimiter.  Quotes or escapes mean the
        # first whitespace-delimited token is not necessarily the executable;
        # shell expansion characters can likewise select a different binary at
        # execution time.  A lossy base grant would then authorize a command the
        # user never saw, so these shapes remain allow-once only.
        if any(ch in base for ch in "'\"\\,$`*?[]{}()<>!") or base.startswith(("~", "#")):
            return ""
        bases.append(base)
    return ",".join(dict.fromkeys(bases)) if bases else normalized


def extract_full_command(command: str) -> str:
    """Return the canonical command/tool identity unchanged."""
    return command


def base_consent_pattern(base_command: str) -> str:
    """Return the client-visible consent string for a base grant."""
    return ",".join(f"{base.strip()} *" for base in base_command.split(",") if base.strip())


def exact_trust_pattern(command: str) -> str:
    """Encode an exact command as an fnmatch pattern with no wildcard power."""
    return glob.escape(command)


def base_trust_patterns(base_command: str) -> set[str]:
    """Encode base binaries as literal bare/argument-bearing fnmatch patterns."""
    patterns: set[str] = set()
    for base in (part.strip() for part in base_command.split(",")):
        if not base or ENV_ASSIGNMENT_RE.match(base):
            continue
        literal = glob.escape(base)
        patterns.update((literal, f"{literal} *"))
    return patterns
