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

_TOOL_TITLE_PREFIXES = ("Running: ", "Reading ")
_REDIRECT_PLACEHOLDER = "\x00REDIR\x00"
_REDIRECT_RE = re.compile(r"[0-9]*>&[0-9]*|&>>?")
_COMMAND_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|&|\n|\|)\s*")
_GRANT_SPLIT_RE = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")


def normalize_tool_name(tool_name: str) -> str:
    """Strip presentation prefixes from a command or tool name."""
    for prefix in _TOOL_TITLE_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix) :]
    return tool_name


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


def approval_command(tool_title: str, tool_input: str, *, is_shell: bool) -> str:
    """Return the server-authoritative command/tool a trust click may bind.

    Shell scope comes only from the provider's structured ``tool_input``.  A
    non-shell event is grantable only when it has no structured input, matching
    the enforcement path where the provider-controlled title is the identity.
    """
    if is_shell:
        return extract_bash_command(tool_input) if tool_input else ""
    if not tool_input:
        return normalize_tool_name(tool_title)
    return ""


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
    normalized = normalize_tool_name(command)
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

    The input is command-shaped.  Callers must pass the canonical command from
    ``tool_input`` rather than wrapping it in a synthetic ``"Running: ..."``
    title.  Presentation-prefix normalization remains for compatibility with
    existing callers and tests, but it is not an authority source.
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
        parts = segment.strip().split(None, 1)
        if not parts or ENV_ASSIGNMENT_RE.match(parts[0]):
            return ""
        bases.append(parts[0])
    return ",".join(dict.fromkeys(bases)) if bases else normalized


def extract_full_command(command: str) -> str:
    """Return a command/tool name without a presentation prefix."""
    return normalize_tool_name(command)


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
