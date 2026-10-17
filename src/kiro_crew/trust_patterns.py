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
import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

ENV_ASSIGNMENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")

# The longest a timed command-scoped grant may last. "For the rest of today"
# is the widest timed choice the approval card offers, so a day bounds every
# honest request; a longer one is refused rather than clamped.
MAX_TIMED_GRANT_SECONDS = 24 * 60 * 60

_REDIRECT_PLACEHOLDER = "\x00REDIR\x00"
_REDIRECT_RE = re.compile(r"[0-9]*>&[0-9]*|&>>?")
# The blanks around a separator are bash's own: space and tab. `\s` would also
# eat U+00A0, `\r`, `\v` and `\f`, which bash keeps as part of the next or
# previous word, so a segment would read differently from what bash runs.
_COMMAND_SPLIT_RE = re.compile(r"[ \t]*(?:\|\||&&|;|&|\n|\|)[ \t]*")
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

    ``matches_trusted_pattern`` compares with ``str.lower()``.
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
    """Mask separators inside quotes before command segmentation.

    An escaped ``>`` is masked too: it is text for the splitter as well, so the
    redirect mask never reads it as the start of ``>&``, and the ``&`` after it
    separates commands.
    """
    out: list[str] = []
    restore: dict[str, str] = {}
    quote: str | None = None
    escaped = False
    n = 0
    for ch in text:
        if escaped:
            escaped = False
            if (mask_escaped and ch in "|&;\n") or ch == ">":
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


def redirect_outside_grant(command: str, patterns: set[str]) -> bool:
    """Whether ``command`` writes through a redirect no matching grant spelled.

    A base grant such as ``gh *`` vouches for a program and whatever arguments
    it is given. An output redirect is not one of those arguments: the shell
    opens the target before the program starts, so honouring ``gh *`` for
    ``gh pr view 42 > /tmp/x`` would let a grant for ``gh`` write any file.

    A segment that writes through a redirect is covered only by a pattern that
    matches it AND spells a write redirect itself (the exact-command grant for
    that line, or a pattern written with one), or by the broad ``*`` grant.
    Discard redirects (``2>/dev/null``, ``2>&1``) are not writes and need no
    such pattern, and neither is a ``>`` bash reads as text, inside quotes or
    after a backslash (:func:`_literal_gt_offsets`).

    ``False`` when the command cannot be segmented: such a command matches no
    pattern in the first place.
    """
    split = split_command_segments(command)
    if split is None:
        return False
    for segment, writes in _segment_writes(*split):
        if not writes:
            continue
        if not any(_covers_write_redirect(pattern, segment) for pattern in patterns):
            return True
    return False


# Constructs whose quoting :func:`_literal_gt_offsets` does not follow. A heredoc
# body (``<<``) and a comment (``#``) take quotes as plain characters, and the
# body can sit in a later segment than its ``<<``; ``$'...'`` lets a backslash
# escape a single quote; ``${...}`` opens a quoting context of its own inside
# double quotes. In a command carrying any of them every ``>`` stays a write.
_UNMODELLED_QUOTING = ("<<", "#", "$'", "${")


def _literal_gt_offsets(command: str) -> frozenset[int]:
    """Return where ``command`` holds a ``>`` that bash reads as literal text.

    A ``>`` inside single or double quotes, or right after a backslash outside
    single quotes, is part of a word, not a redirect operator; the splitter
    reads an escaped ``>`` the same way (:func:`_mask_quoted_separators`). The
    set is empty, so every ``>`` counts, when a quote is left open, when a
    construct of :data:`_UNMODELLED_QUOTING` appears, and on Windows, where the
    shell is PowerShell and a backslash escapes nothing.
    """
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS or any(c in command for c in _UNMODELLED_QUOTING):
        return frozenset()
    literal: set[int] = set()
    quote: str | None = None
    escaped = False
    for at, ch in enumerate(command):
        if escaped:
            escaped = False
            if ch == ">":
                literal.add(at)
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
            elif ch == ">":
                literal.add(at)
        elif ch in ("'", '"'):
            quote = ch
    return frozenset() if quote else frozenset(literal)


def _segment_writes(normalized: str, segments: list[str]) -> list[tuple[str, bool]]:
    """Pair each segment the grant checks read with whether it writes through a redirect.

    ``normalized`` and ``segments`` are what :func:`split_command_segments`
    returned. Quotes are read once, over all of ``normalized``: the splitter
    also cuts at a separator escaped inside double quotes, so a segment can
    start inside a quote that an earlier one opened. Each segment is cut
    verbatim and in order from ``normalized``; should one not be, it and every
    later one are read with every ``>`` counted.
    """
    literal = _literal_gt_offsets(normalized)
    pairs: list[tuple[str, bool]] = []
    at = 0
    for segment in segments if len(segments) > 1 else [normalized]:
        start = normalized.find(segment, at)
        if start < 0:
            literal, start = frozenset(), at
        at = start + len(segment)
        pairs.append((segment, _writes_through_redirect(segment, literal, start)))
    return pairs


def _writes_through_redirect(text: str, literal: frozenset[int], start: int = 0) -> bool:
    """Whether ``text`` writes through a redirect once its literal ``>`` are left out.

    ``literal`` holds :func:`_literal_gt_offsets` offsets into a command in
    which ``text`` starts at offset ``start``. What counts as a write is
    otherwise the read-only classifier's rule,
    :func:`kiro_crew.security.readonly_bash.writes_through_redirect`.
    """
    # Imported here: ``kiro_crew.security`` imports this module at package load.
    from kiro_crew.security.readonly_bash import writes_through_redirect

    kept = "".join(ch for at, ch in enumerate(text, start) if at not in literal)
    return writes_through_redirect(kept)


def _covers_write_redirect(pattern: str, segment: str) -> bool:
    """Whether ``pattern`` covers ``segment`` together with the write redirect in it.

    The rule :func:`redirect_outside_grant` applies: the broad ``*`` grant, or a
    pattern that matches the segment and spells a write redirect itself.
    """
    return pattern == "*" or (
        _writes_through_redirect(pattern, _literal_gt_offsets(pattern))
        and _tool_matches(pattern, segment)
    )


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


def parse_grant_ttl(value: object) -> int | None:
    """Return a timed grant's lifetime in whole seconds, or ``None`` if invalid.

    Only a plain integer from 1 to :data:`MAX_TIMED_GRANT_SECONDS` is a
    lifetime. A bool, a float, a numeric string or an out-of-range number is
    invalid, and the caller refuses the grant on ``None``: an unreadable
    lifetime never becomes a grant that lasts for the whole chat.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 1 <= value <= MAX_TIMED_GRANT_SECONDS:
        return None
    return value


class GrantDeadline(NamedTuple):
    """When a timed grant stops applying, on two clocks at once.

    ``wall`` is ``time.time()`` and keeps counting while the host is
    suspended, which is the clock the user's "until 1:45 AM" refers to.
    ``mono`` is ``time.monotonic()`` and cannot be moved back by a clock
    change. The grant ends when EITHER deadline passes, so neither a sleeping
    laptop nor a clock set backwards stretches it.
    """

    wall: float
    mono: float

    def ended(self, *, wall: float, mono: float) -> bool:
        """Return whether the deadline has passed at (``wall``, ``mono``)."""
        return wall >= self.wall or mono >= self.mono


# A deadline every reading of either clock has already passed.
_ENDED = GrantDeadline(wall=float("-inf"), mono=float("-inf"))


class GrantEnded(Exception):
    """The timed grant an approval rested on ended before the approval was sent.

    A transport raises it from ``approve_tool`` INSTEAD of answering: nothing
    was sent and the request is still pending, so the caller asks the person.
    """


def grant_deadline_guard(deadline: GrantDeadline, request_id: str | int) -> Callable[[], None]:
    """Return a check that raises :class:`GrantEnded` once ``deadline`` has passed.

    A transport runs it under its write lock, right before it writes the
    answer, so both clocks are read at the last moment before the answer leaves.
    """

    def _check() -> None:
        if deadline.ended(wall=time.time(), mono=time.monotonic()):
            raise GrantEnded(request_id)

    return _check


def grant_deadline(ttl: int, *, wall: float, mono: float) -> GrantDeadline:
    """Return the deadline of a grant made at (``wall``, ``mono``) for ``ttl`` seconds."""
    return GrantDeadline(wall=wall + ttl, mono=mono + ttl)


def record_trust_grant(
    patterns: set[str],
    timed: dict[str, GrantDeadline],
    granted: Iterable[str],
    deadline: GrantDeadline | None,
) -> None:
    """Record a chat's grant of ``granted``, for the chat or until ``deadline``.

    A chat-long grant goes into ``patterns``, the store every per-chat writer
    shares, and clears any timed entry it now outlasts. A timed grant goes into
    ``timed`` ONLY: it never adds to or removes from ``patterns``, so a pattern
    another writer holds there (a worker lease, a chat-long grant) keeps its
    own lifetime whatever the timed entry does. The newest timed grant of a
    pattern sets its deadline.
    """
    for pattern in granted:
        if deadline is None:
            patterns.add(pattern)
            timed.pop(pattern, None)
        else:
            timed[pattern] = deadline


def live_trusted_patterns(
    patterns: set[str], timed: dict[str, GrantDeadline], *, wall: float, mono: float
) -> set[str]:
    """Return the patterns that may approve a call now, dropping expired timed ones.

    Called at decision time, right before a call is matched, so an expired
    grant never approves anything: the call prompts again exactly as if the
    grant had never been made. Only ``timed`` is pruned; ``patterns`` is
    returned unchanged, and is returned itself when no timed grant is live.
    """
    for pattern, deadline in list(timed.items()):
        if deadline.ended(wall=wall, mono=mono):
            del timed[pattern]
    if not timed:
        return patterns
    return patterns | timed.keys()


def timed_grant_deadline(
    command: str, patterns: set[str], timed: dict[str, GrantDeadline]
) -> GrantDeadline | None:
    """Return when the timed grants an approval of ``command`` rests on end.

    ``None`` means the chat-long ``patterns`` cover every part of ``command``
    on their own: the approval rests on no timed grant and carries no deadline.
    Otherwise each part they leave uncovered takes the timed grant covering it
    that ends last, and the result is the earliest of those on each clock, so
    the approval stands only while every timed grant it needs is live. A part
    nothing covers returns a deadline that has already passed: the caller
    prompts rather than send an approval no grant backs.

    A part that writes through a redirect is covered only as
    :func:`redirect_outside_grant` covers it, so a timed grant that spells the
    redirect lends its deadline even when a chat-long base grant matches the
    rest of the part.
    """
    split = split_command_segments(command)
    if split is None:
        return _ENDED
    needed: list[GrantDeadline] = []
    for part, writes in _segment_writes(*split):
        covers = _covers_write_redirect if writes else _tool_matches
        if any(covers(p, part) for p in patterns):
            continue
        covering = [d for p, d in timed.items() if covers(p, part)]
        if not covering:
            return _ENDED
        needed.append(max(covering))
    if not needed:
        return None
    return GrantDeadline(wall=min(d.wall for d in needed), mono=min(d.mono for d in needed))
