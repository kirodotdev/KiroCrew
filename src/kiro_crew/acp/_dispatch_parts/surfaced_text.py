"""LLM- and backend-authored text, made safe and readable before it is surfaced.

The one redaction policy for text bound for the dashboard or a log (the two-pass scrub,
the check that a result still fits after it, and length-safe backend ids), the unified
edit diff, the pill title and purpose line, and the JSON encode that degrades visibly
instead of raising.

:mod:`kiro_crew.acp._dispatch` stays the import path and the patch surface; see
:mod:`kiro_crew.acp._dispatch_parts`.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any

from kiro_crew.acp._dispatch_parts.tool_call_identity import is_shell_kind
from kiro_crew.acp.types import TOOL_PURPOSE_KEYS
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Appended when a diff is cut at ``max_len``. Uses the unified-diff escape
# convention ("\ ...", the same lead-in as "\ No newline at end of file"), so
# diff renderers skip the line while consumers counting +/- rows can detect
# that the counts understate the real change.
DIFF_TRUNCATION_MARK = "\\ diff truncated"

# Argument keys that carry the created file's content across edit-tool arg
# shapes; checked in order, the first non-empty string wins.
_EDIT_CONTENT_KEYS = ("fileText", "content", "text")


def derive_edit_diff(raw_input: object) -> str:
    """Derive a unified diff from a bare-JSON edit payload.

    Covers the edit-tool arg shapes that arrive WITHOUT a diff content block:
    a ``strReplace`` pair renders as a replace hunk; a ``create``'s content
    renders as a whole-file addition; an ``insert`` with a line number
    renders as an addition hunk AT that line — in all three the rendered
    lines ARE the change, exactly. An insert/append without a line number
    derives nothing (the hunk position would be a guess) and keeps its
    fold-proof trace via the file_changes snapshot channel. Deriving here
    (single, shared) rather than per-surface means every consumer of
    ``tool_input`` — the dashboard card, a future channel renderer — sees
    the same diff.
    """
    if not isinstance(raw_input, dict):
        return ""
    path = raw_input.get("path")
    if not isinstance(path, str) or not path:
        # A non-string path (numeric, dict) in malformed args must not reach
        # difflib — a TypeError here would abort the whole dispatch mid-turn.
        return ""
    command = raw_input.get("command")
    if command == "strReplace":
        old = raw_input.get("oldStr")
        new = raw_input.get("newStr")
        old = old if isinstance(old, str) else ""
        new = new if isinstance(new, str) else ""
        if old or new:
            return make_unified_diff(old, new, path)
        return ""
    if command == "create":
        for key in _EDIT_CONTENT_KEYS:
            value = raw_input.get(key)
            if isinstance(value, str) and value:
                return make_unified_diff("", value, path)
        return ""
    if command == "insert":
        insert_line = raw_input.get("insertLine")
        if not isinstance(insert_line, int) or insert_line < 0:
            return ""
        for key in _EDIT_CONTENT_KEYS:
            value = raw_input.get(key)
            if isinstance(value, str) and value:
                lines = value.rstrip("\n").split("\n")
                body = "\n".join(f"+{line}" for line in lines)
                # Pure-insertion hunk: zero old lines at insert_line, the
                # added lines starting on the following row (0-indexed
                # insertLine -> content lands as new line insert_line+1).
                header = f"@@ -{insert_line},0 +{insert_line + 1},{len(lines)} @@"
                return f"--- {path}\n+++ {path}\n{header}\n{body}"
        return ""
    return ""


def make_unified_diff(old: str, new: str, path: str, max_len: int = 65536) -> str:
    """Generate a unified diff string from old/new text, handling empty inputs.

    ``max_len`` bounds the live event payload; the default is sized so the
    dashboard's full-card range (a few hundred lines) is never cut. A longer
    diff is truncated at a LINE boundary and annotated with
    ``DIFF_TRUNCATION_MARK`` — a bare slice can cut mid-line and render a
    garbled half-row, and an unmarked cut silently understates +/- counts.
    """
    old_lines = (old if old.endswith("\n") else old + "\n").splitlines(keepends=True) if old else []
    new_lines = (new if new.endswith("\n") else new + "\n").splitlines(keepends=True) if new else []
    udiff = difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, n=3)
    text = "".join(udiff).rstrip()
    if len(text) <= max_len:
        return text
    budget = max(max_len - len(DIFF_TRUNCATION_MARK) - 1, 0)
    cut = text.rfind("\n", 0, budget)
    head = text[:cut] if cut > 0 else text[:budget]
    return head + "\n" + DIFF_TRUNCATION_MARK


def select_tool_title(
    title: object,
    raw_input: object,
    kind: object = None,
    *,
    is_shell: bool | None = None,
) -> str | None:
    """Pick the pill label, using ``description`` for shell and sub-agent calls.

    Some backends' Bash tool emits a ``description`` field alongside ``command``
    (e.g. "List Kiro Crew ACP module files" rather than ``ls <workspace>/...``).
    We surface it on the pill when supplied, then the literal shell command for
    a shell tool, and only then the SDK-provided ``title``. Used for both the
    initial ``tool_call`` and the second-phase ``tool_call_update`` refinement
    so the title rule stays consistent across both events.

    The command outranks ``title`` because backends disagree on what ``title``
    holds for a shell call: some send the invocation itself, others a generic
    kind label ("Run Command") that names no command at all. Reading the
    command yields the same pill for the first shape and an informative one for
    the second, and it is never the weaker choice — a genuinely human-readable
    label arrives as ``description``, which still wins.

    ``is_shell`` overrides the kind-derived classification for a caller holding
    a RESOLVED signal. A ``tool_call_update`` may omit ``kind`` entirely, and
    reading that absence as non-shell would put the generic title back on a
    pill the initial ``tool_call`` had already labelled with its command.
    """
    kind_str = kind if isinstance(kind, str) else None
    shell = is_shell_kind(kind_str) if is_shell is None else is_shell
    if isinstance(raw_input, dict):
        # Sub-agent calls label their task; other non-shell tools can use
        # description as a functional argument such as an issue body.
        subagent_type = raw_input.get("subagent_type")
        subagent = isinstance(subagent_type, str) and bool(subagent_type.strip())
        desc = raw_input.get("description")
        if (shell or subagent) and isinstance(desc, str) and desc.strip():
            return desc
        cmd = raw_input.get("command")
        if shell and isinstance(cmd, str) and cmd.strip():
            return cmd
    # The flat title field defaults to an "unknown" sentinel when a backend
    # omits it; treat that (and blanks) as absent rather than surfacing it.
    if isinstance(title, str) and title and title != "unknown":
        return title
    return None


def is_tool_purpose_key(key: object) -> bool:
    """True when ``key`` names the reserved tool-purpose argument.

    Matched by SHAPE rather than by an allowlist of literals: any *reserved*
    (dunder-prefixed) argument whose name ends in ``purpose`` once separators
    and case are normalized away. The declared spelling is
    ``__tool_use_purpose``, but the argument reaches us as whatever the model
    actually emitted, and models paraphrase the name — ``__purpose``,
    ``__thinking_purpose`` and ``__woohoo_purpose`` all occur in real
    transcripts. An exact allowlist silently drops every one of them.

    The ``__`` prefix is load-bearing: it keeps a *functional* argument that
    happens to be called ``purpose`` (a tool legitimately taking a purpose
    string) out of the match, because only dunder names are reserved. A tool
    declaring its own dunder ``…purpose`` argument would be read as the purpose
    line, which is the desired reading anyway — and harmless either way, since
    this only picks the label and never rewrites the arguments sent to the tool.
    """
    if not isinstance(key, str) or not key.startswith("__"):
        return False
    return re.sub(r"[^a-z0-9]", "", key.lower()).endswith("purpose")


def extract_tool_purpose(raw_input: object) -> str:
    """Pull the agent-authored purpose line out of a tool call's raw params.

    The canonical spellings in ``TOOL_PURPOSE_KEYS`` are preferred (kiro-cli
    echoes the reserved argument back as either the declared snake_case name or
    a camelCased variant), then any other key matching
    ``is_tool_purpose_key()``. Reading a fixed set of literals drops the purpose
    for every paraphrased spelling, which shows up as the dashboard's concise
    tool pill falling back to the literal command line.

    Off-canonical keys are scanned in sorted order so the choice is
    deterministic when a call somehow carries more than one.
    """
    if not isinstance(raw_input, dict):
        return ""
    for key in TOOL_PURPOSE_KEYS:
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in sorted(k for k in raw_input if is_tool_purpose_key(k)):
        value = raw_input[key]
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _redact(text: str) -> str:
    """Run a string through both redactors (URL exfil + credentials)."""
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def redact_text(text: str) -> str:
    """Public single-source redaction for LLM-influenced text (sub-agent
    streamed output, tool/sub-agent titles) before it is surfaced on the
    dashboard/Slack. Never trust LLM output — scrub exfil URLs + credentials.
    Both AcpClient and AcpRuntime call this so the two paths stay identical."""
    return _redact(text)


def fits_tool_result(text: str, limit: int) -> bool:
    """Whether *text* stays within *limit* after the tool-row scrub, so it lands uncut."""
    return len(_redact(text)) <= limit


# Display cap for a backend-authored JSON-RPC id in a log line / exception
# text. Applied AFTER redaction (redact-before-bound): a slice taken before the
# redactor ran could sever a credential at the cut and leak the fragment.
_REQUEST_ID_LOG_CAP = 256
# Cap on the text handed to the redactor. A frame is bounded only by the 10 MB
# stdout line limit, and the credential scan slides a window byte-by-byte over
# base64-alphabet runs, so an unbounded id could hold the event loop for a
# value that is then cut to 256 chars anyway. An id over the cap is NOT
# truncated -- a cut can sever a credential into a fragment no pattern matches,
# and an earlier redaction (a collapsed URL) can pull that fragment back inside
# the display cap -- it is replaced by a fixed marker carrying only its length.
_REQUEST_ID_REDACT_INPUT_CAP = 16 * _REQUEST_ID_LOG_CAP


def _loggable_request_id(request_id: object) -> str:
    """A JSON-RPC id from the backend, made safe for a log line or error text.

    The id is backend-authored: ``repr`` keeps control characters and newlines
    out of the line, the shared ``redact_text`` scrub (exfil URLs, then
    credentials) keeps a URL- or credential-shaped id out of the gateway log,
    ``/api/logs`` and any session card an exception message rides into, and the
    result is length-capped after redaction. An id over
    ``_REQUEST_ID_REDACT_INPUT_CAP`` is replaced by a length-only marker rather
    than truncated, so a multi-MB id cannot stall the loop and a cut can never
    hand the redactor a severed secret. ``repr`` also makes a non-string id (a
    numeric ``toolCallId``) safe for the regexes. Every ACP log line that
    carries a backend-authored id -- JSON-RPC request ids and tool-call ids, in
    client, runtime, session handle and dispatch -- goes through it.
    """
    return redact_backend_text(repr(request_id))[:_REQUEST_ID_LOG_CAP]


def redact_backend_text(text: str) -> str:
    """Redact one backend-authored string whole, or refuse it by length.

    The single policy behind every retained or logged backend string (ids,
    session ids, methods): the text is handed to ``redact_text`` UNCUT, so no
    credential is ever severed at a slice before the redactor sees it. A string
    over ``_REQUEST_ID_REDACT_INPUT_CAP`` is not truncated -- a cut can leave a
    fragment no pattern matches -- it is replaced by a marker carrying only its
    length. Callers apply their own display or retention cap to the RESULT.
    """
    if len(text) > _REQUEST_ID_REDACT_INPUT_CAP:
        # No content survives: a truncation could hand the redactor a severed
        # secret that matches none of its patterns. Only the size is kept.
        return f"<id too long: {len(text)} chars>"
    return redact_text(text)


#: Stands in for a payload the JSON encoder refuses at any dispatch-path encode
#: (see :func:`_dumps_degraded`). Visible in the
#: transcript on purpose: the user can see that detail is missing, which is the
#: whole difference between this and dropping the field.
UNSERIALISABLE_SIBLING_VALUE = "[[value omitted: nested too deeply to serialise]]"


def _dumps_degraded(payload: Any, **kwargs: Any) -> str:
    """``json.dumps`` that degrades to readable text instead of raising.

    Every encode on the dispatch path serialises a payload whose shape the agent
    backend chooses, so the encoder can always be pushed past its ceiling:
    ``json.dumps`` raises ``RecursionError`` on a sufficiently nested structure
    -- a ``RuntimeError``, which the ``(TypeError, ValueError)`` arm that guards
    one of these sites does not catch and the others do not guard at all, so the
    raise escapes frame rendering and aborts the whole agent turn. The intended
    cost of an unrenderable frame is that ONE frame, degraded visibly, never the
    turn. Every encode on this path gets the same refusal posture.

    A ``TypeError``/``ValueError`` refusal degrades to ``str(payload)`` -- the
    payload still has a repr, and this preserves byte-identically the arm that
    :func:`_build_tool_refinement_event` already carried. A ``RecursionError``
    refusal cannot count on that (``repr`` recurses too, just with a different
    ceiling than the encoder's), so it degrades to the
    :data:`UNSERIALISABLE_SIBLING_VALUE` placeholder, and the ``str`` fallback
    keeps its own arm for the payload that is both deep and unencodable.
    """
    try:
        return json.dumps(payload, **kwargs)
    except (TypeError, ValueError):
        try:
            return str(payload)
        except RecursionError:
            return UNSERIALISABLE_SIBLING_VALUE
    except RecursionError:
        return UNSERIALISABLE_SIBLING_VALUE
