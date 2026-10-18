"""The deny checks a tool call's title and input run through before anything approves it.

Extracting the strings of a tool call's input, the scan ceiling, the exempt command
text, and the title, edit-target and first-input denials the permission resolver asks
first.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import json

from kiro_crew.hooks import _normalize_tool_name
from kiro_crew.platform.tool_paths import edit_target_candidates
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    is_denied,
    is_sensitive_bash_command,
    is_unverifiable_path_refusal,
    sensitive_path_refusal,
    sensitive_write_path_refusal,
)


def _extract_tool_input_strings(tool_input: str) -> list[str]:
    """Extract all string values from a JSON tool_input for security scanning.

    Recursively walks nested dicts and lists to find all string values,
    ensuring sensitive paths in nested structures like
    ``{"args": {"path": "~/.aws/credentials"}}`` are not missed.

    Handles dict, list, plain-string, and malformed JSON gracefully. On
    parse failure, returns the raw string itself as the single candidate.
    """
    if not tool_input:
        return []
    try:
        parsed = json.loads(tool_input)
    except ValueError:
        # Not JSON — treat the raw string as a path/command candidate
        return [tool_input]
    if isinstance(parsed, str):
        return [parsed]

    results: list[str] = []

    def _collect(obj: object) -> None:
        if isinstance(obj, str) and obj:
            results.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(parsed)
    return results


# Longest single string the tool_input scan will attempt.
#
# The scan runs on a worker thread, but CPython's ``re`` HOLDS the GIL for the
# whole of one match call (measured: a 5-8 s ``search`` on a worker left the
# main thread exactly one tick on 3.10 and 3.12), so the hop yields between
# strings, never within one. The ceiling is therefore the liveness bound for a
# single string as well as the WORKER's: an unbounded string parks the loop and
# the thread for as long as the scan takes, and the permission request behind
# it -- the turn -- waits with it.
#
# The anchor rewrite in this change cut the constant but NOT the growth -- the
# scan is still superlinear in the length of one line. Measured on one dev box:
#
#     4 KiB 0.16s | 8 KiB 0.31s | 16 KiB 1.14s | 20 KiB 1.52s | 32 KiB 3.79s
#
# 16->32 KiB is 3.3x for 2x the input (~n^1.7), so extrapolation is the wrong
# instinct here and a ceiling has to be set from the measured curve. A CI runner
# under parallel load came in roughly an order of magnitude slower than this box,
# which is what sets the margin: 20 KiB is ~1.5s here and ~15s there, under the
# 25s loop watchdog on both.
#
# Exceeding it is FAIL-CLOSED: the call is denied, never skipped. A skip would
# convert a liveness bug into a security hole by letting unscanned input through
# the deny surface; a denial only refuses input we cannot prove safe, and is
# strictly better than the alternative it replaces, which was crashing the
# gateway and losing the whole turn.
#
# Known cost of that trade: a permission-gated write of a benign file larger
# than this lands its whole content in ``tool_input`` and is refused WHEN the
# frame's provenance is unknown. An edit with trusted provenance is judged by
# its target path instead (``_edit_target_denial``), and every other non-shell
# tool with trusted provenance has its document-body fields skipped
# (``platform.tool_paths.command_shaped_strings``), so neither reaches this
# ceiling on a body; the residual is the unclassified frame, which keeps the
# full scan by design. Tracked in https://github.com/kirodotdev/KiroCrew/issues/8053.
#
# One number for both tiers: the shell gate refuses a command above
# ``security.MAX_SCANNABLE_COMMAND_CHARS`` on its own (every caller, not only
# this one), so aliasing it here is what keeps "too long for the tool_input
# scan" and "too long for the command gate" the same size.
_MAX_SCANNABLE_TOOL_INPUT_CHARS = MAX_SCANNABLE_COMMAND_CHARS


def _path_tier_exempt(event: object) -> str | None:
    """The one string of *event* the path tier does not resolve, or ``None``.

    The path tier reads a PATH; a shell tool's COMMAND is command text, which the
    gate does not match paths in because the OS sandbox holds the credential
    stores away from the shell. Only the recovered command text
    (``AcpEvent.shell_command``) is exempt, and only when the client classified
    the frame as shell AND no MCP server serves it: kiro-cli can classify an
    execute-kind frame as shell while also naming an MCP server
    (``classify_tool_call`` carries the identity and keeps the shell verdict),
    and an MCP-served tool runs outside the sandbox. Every OTHER string of a
    shell frame stays path-gated -- a shell-kind tool with structured
    parameters (kiro-cli ``use_aws``) can carry a discrete credential path as an
    argument, and in ``standard`` sandbox mode ``~/.aws`` is visible to the
    shell, so the path tier over that argument is the control there, not the
    sandbox. Same condition as ``hooks.on_tool_call``; both read the client's
    own classification, never the payload's.
    """
    if not bool(getattr(event, "is_shell", False)):
        return None
    if getattr(event, "mcp_server_name", "") or "":
        return None
    command = getattr(event, "shell_command", None)
    return command if isinstance(command, str) and command else None


def _is_exempt_command_text(text: str, exempt_command: str | None) -> bool:
    """*text* is the exempt command, with or without a display prefix."""
    if exempt_command is None:
        return False
    return text == exempt_command or _normalize_tool_name(text) == exempt_command


def _title_denial(
    title: str,
    denied_regexes: list[str] | None,
    *,
    exempt_command: str | None = None,
    identity_exempt: bool = False,
    bash_subject: str | None = None,
) -> tuple[str, str] | None:
    """Return the always-enforced denial for the tool *title*, or ``None``.

    The title is the request's primary subject -- for a shell tool it IS the
    command -- and it goes through the same three predicates as every
    tool_input string. Pure and synchronous like :func:`_first_tool_input_denial`,
    and run on the same worker hop: the field crash this exists for was the
    sensitive-path gate scanning a ~9 KB TITLE for 25 s on the event loop, so
    a hop that offloaded only the tool_input strings left the crash path in
    place. The tuple is ``(kind, reason)`` with *kind* ``"path"`` / ``"bash"`` /
    ``"regex"``; the reasons are the exact strings the on-loop checks produced.

    *bash_subject*, when given, is what the bash tier scans instead of *title*:
    a title rebuilt around the whole command (``untruncated_shell_title``) is
    scanned there without its display prefix, so the prefix cannot carry a
    command that fits the size ceiling past it.
    """
    # The path tier reads a PATH. A shell tool's recovered COMMAND is command text,
    # which the gate deliberately does not match paths in (``hooks.on_tool_call``
    # makes the same exemption): resolving ``cd /x && grep ...`` as a filename
    # never matched, but it spent a resolver round-trip per call and, under a
    # stall, refused the command as a sensitive path. ``exempt_command`` is
    # :func:`_path_tier_exempt`'s answer -- the one string that is that text; a
    # title that is not the command stays gated.
    #
    # ``identity_exempt`` is the same reasoning for a tool NAME: the caller sets
    # it only when the title is exactly the call's own verified ``@server/tool``
    # identity (``title_is_trusted_mcp_identity``), which names a tool, not a
    # file. The bash and regex tiers below still read it.
    path_refusal = (
        None
        if identity_exempt or _is_exempt_command_text(title, exempt_command)
        else sensitive_path_refusal(title)
    )
    if path_refusal:
        # A stall is passed through as worded (recognised by its fixed prefix, which
        # the deny guidance classifies by); a match keeps this producer's wording.
        if is_unverifiable_path_refusal(path_refusal):
            return ("path", path_refusal)
        return ("path", f"Blocked: sensitive path: {title}")
    bash_reason = is_sensitive_bash_command(title if bash_subject is None else bash_subject)
    if bash_reason:
        return ("bash", bash_reason)
    deny_reason = is_denied(title, denied_regexes=denied_regexes)
    if deny_reason:
        return ("regex", deny_reason)
    return None


def _edit_target_denial(
    raw_params: dict | None, diff_path: str = "", tool_kind: str = ""
) -> tuple[str, str, str] | None:
    """The always-enforced denial for a file-EDIT tool call, or ``None``.

    An edit's ``tool_input`` is a DOCUMENT: ``_dispatch.derive_edit_diff`` renders
    the file's new content (or a strReplace pair) as a unified diff, and that text
    is what ``event.tool_input`` carries. Handing it to :func:`_first_tool_input_denial`
    read the document as a shell command line, so writing a Markdown page that says
    ``git push origin main``, a docstring that says ``kirocrew restart``, or prose
    that names ``~/.ssh`` was refused -- and a body over
    :data:`_MAX_SCANNABLE_TOOL_INPUT_CHARS` was refused for its LENGTH (the
    tool-input length-cap defect). That is the same class the cron-script body
    gate rework closed: a document is not the shell gate's subject.

    What an edit can actually do is decided by WHERE it writes, so the gate for an
    edit is the resolved target path, exactly as ``hooks.on_tool_call`` decides it:
    every accepted path spelling in the params (``target_paths``) goes through
    :func:`is_sensitive_write_path`, which is the read+write keystone PLUS the
    write-only tier (config, the agents dir). A walk that hit its work cap is
    denied as unverifiable, mirroring the hook gate's fail-closed shape. The
    title-tier scan of the request still runs before this, unchanged.

    The target set is the UNION of the params' path spellings and *diff_path*,
    the path the tool_call's ``{"type": "diff"}`` content block named. A backend
    may stream trusted params that carry no path key at all and name the file
    only in that block (``_dispatch`` caches it per toolCallId onto the
    permission event as ``diff_path``), so judging the params alone would judge
    nothing. Two unverifiable shapes fail closed: an EMPTY union (an edit whose
    params and content block together name no target has no proven target to
    judge, and the document scan is not a fallback here -- a document that
    happens to contain no denied text is not evidence that the write is safe),
    and an UNANCHORED diff path (relative after ``~``/env expansion, which
    resolves against the gateway CWD rather than the agent workspace, so its
    sensitivity cannot be established -- see ``edit_target_candidates``). The
    caller reaches this on trusted provenance (see ``_resolve_permission``) or
    on a client-cached diff block, which is write-plane evidence on its own;
    a call with neither trusted params nor a diff block never gets here and
    keeps the document scan.
    """
    candidates = edit_target_candidates(raw_params, diff_path, tool_kind=tool_kind)
    if candidates.truncated:
        return (
            "path",
            "Blocked: tool arguments too large to verify for sensitive paths " "(deny-by-default)",
            "",
        )
    if candidates.unanchored:
        return (
            "path",
            "Blocked: file edit names a relative target path that cannot be "
            "verified (deny-by-default)",
            "",
        )
    if not candidates:
        return (
            "path",
            "Blocked: file edit names no target path to verify (deny-by-default)",
            "",
        )
    for path in candidates:
        # ``sensitive_write_path_refusal`` is ``is_sensitive_write_path`` as a
        # reason: a stall still refuses, worded as a timed-out check.
        if reason := sensitive_write_path_refusal(path):
            return ("path", reason, path)
    return None


def _first_tool_input_denial(
    strings: list[str],
    denied_regexes: list[str] | None,
    *,
    exempt_command: str | None = None,
    command_rules: bool = True,
) -> tuple[str, str, str] | None:
    """Return the first tool_input denial among *strings*, or ``None``.

    Pure, synchronous, and blocking: the three predicates are regex-heavy and
    ``_extract_tool_input_strings`` hands over EVERY string in the payload, so a
    single long document body can occupy the calling thread for seconds. It
    therefore runs on a worker thread (one hop for the whole loop, not one per
    string). CPython's ``re`` HOLDS the GIL for the whole of one match call
    (measured: a 5-8 s ``search`` on a worker left the main thread a single
    tick on 3.10 and 3.12), so the hop keeps the loop live BETWEEN strings,
    not within one; within one string the only liveness guarantees are the
    linear patterns and the length check against
    :data:`_MAX_SCANNABLE_TOOL_INPUT_CHARS`, which also bounds the worker's
    own wall clock -- a denied oversized string is recoverable, a worker parked
    for minutes on a pathological payload is not -- and an oversized one is
    denied rather than scanned or skipped.

    ``command_rules=False`` applies the size ceiling and the path tier only:
    the caller passes it for a named MCP document body
    (``platform.tool_paths.MCP_DOCUMENT_BODY_FIELDS``), which is stored text
    and not a command line.

    The tuple is ``(kind, reason, matched_string)`` where *kind* is
    ``"path"`` / ``"bash"`` / ``"regex"`` / ``"oversize"``. Mechanism
    classification stays with the caller on the event loop, because it consults
    the HookManager.
    """
    for s in strings:
        if len(s) > _MAX_SCANNABLE_TOOL_INPUT_CHARS:
            # Fail closed: too long to scan inside the loop's liveness budget,
            # so it cannot be shown safe and is refused.
            return (
                "oversize",
                (
                    "Blocked: a tool_input string is too large to security-scan "
                    f"({len(s)} chars > {_MAX_SCANNABLE_TOOL_INPUT_CHARS} limit); "
                    "refused rather than left unscanned"
                ),
                s[:64],
            )
        # Same exemption as ``_title_denial``: only the recovered command text is
        # command text. A shell frame's OTHER payload strings (a structured
        # ``use_aws`` argument naming a path) stay path-gated -- see
        # :func:`_path_tier_exempt`.
        path_refusal = (
            None if _is_exempt_command_text(s, exempt_command) else sensitive_path_refusal(s)
        )
        if path_refusal:
            if is_unverifiable_path_refusal(path_refusal):
                return ("path", path_refusal, s)
            return ("path", f"Blocked: sensitive path in tool_input: {s}", s)
        if not command_rules:
            continue
        _input_bash = is_sensitive_bash_command(s)
        if _input_bash:
            return ("bash", _input_bash, s)
        _input_deny = is_denied(s, denied_regexes=denied_regexes)
        if _input_deny:
            return ("regex", _input_deny, s)
    return None
