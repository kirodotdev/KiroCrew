"""The chat-history search tool's helpers: incognito transcripts, output redaction,
date parsing, the caller's workspace bucket, and the snippet a match is shown with.

:mod:`kiro_crew.mcp_core` stays the import path and the patch surface; see
:mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kiro_crew.history import _SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW
from kiro_crew.history import is_incognito_transcript, snippet_needles
from kiro_crew.platform import redact_via_context as redact

# ── Chat-history search helpers (Phase 1: search_chat_history / get_chat_session) ──

_SNIPPET_RADIUS = 120  # chars of context kept on each side of a match


_SNIPPET_MAX_LEN = 320  # hard cap on a returned snippet


# Upper bound on ranked candidates pulled from the backend per search. Bound to
# the backend's own scan window (imported, not copied) so we consider every
# ranked match (bounded I/O) and post-filtering can't starve a caller whose hits
# rank past a small page — and the two can't silently drift apart.
_SEARCH_HISTORY_SCAN = SEARCH_SCAN_WINDOW


def _history_is_incognito(meta: dict) -> bool:
    """True if a session's memory_mode marks it private (never searchable).

    Dict-shaped convenience over :func:`kiro_crew.history.is_incognito_transcript`,
    the shared classifier — the predicate itself lives in history_derivation.py.
    """
    return is_incognito_transcript(meta.get("memory_mode"))


def _redact_history_output(text: str) -> str:
    """Apply the standard dual redaction to any chat-history tool output.

    Used on EVERY return path (including early-return error strings that echo an
    LLM-supplied session_key) so nothing reaches the dashboard unredacted.

    Routes through the context-aware :func:`redact` shim so the companion's extra
    credential patterns apply to verbatim chat-transcript egress; the Default
    ``CredentialPolicy`` delegates to ``security.redact`` (the same
    exfil-then-credential dual pass), so standalone is byte-for-byte unchanged.
    """
    return redact(text)


def _parse_iso_date_epoch(date_str: str) -> float | None:
    """Parse a YYYY-MM-DD string to a UTC midnight epoch. None on bad input."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _ws_bucket(meta_ws: object) -> str:
    """Normalize a session's workspace value to a comparable bucket.

    ``update_metadata`` accepts arbitrary JSON for ``workspace``; a non-string
    (or empty) value must bucket to "default" rather than compare unequal to a
    real workspace name and silently hide the session from its owner.
    """
    return meta_ws if isinstance(meta_ws, str) and meta_ws else "default"


def _caller_workspace(cl: "object", session_key: str) -> str:
    """Resolve the calling session's workspace bucket for scope filtering.

    Read from the caller's own session metadata (normalized via _ws_bucket).
    Known limitation: on a brand-new session whose metadata file has not been
    written yet, this returns "default". A multi-workspace caller in that narrow
    window is scoped to the default bucket (fail-CLOSED — they see fewer results,
    never another workspace's). Fully fixing it needs the gateway to carry the
    workspace in CallerContext (the register payload does not today), so it is
    tracked as a separate gateway change rather than papered over here.
    """
    if not session_key:
        return "default"
    return _ws_bucket(cl.get_metadata(session_key).get("workspace"))  # type: ignore[attr-defined]


_HISTORY_SNIPPET_ROLES = frozenset({"user", "assistant"})


def _casefold_match_span(text: str, needle_cf: str) -> tuple[int, int] | None:
    """Locate *needle_cf* (already casefolded) inside *text* using full casefolding.

    Returns ``(start, end)`` source indices into *text* for the first match, or
    ``None``. Unlike ``re.search(..., re.IGNORECASE)`` — which does only simple
    per-character case mapping — this mirrors ``str.casefold`` so multi-char
    folds (e.g. ``ß`` ↔ ``ss``, ``ﬃ`` ↔ ``ffi``) match, keeping the wrap matcher
    consistent with the ``str.casefold().find`` selection above. ``str.casefold``
    is a per-character homomorphism, so casefolded offsets map back to source
    character boundaries.
    """
    if not needle_cf:
        return None
    # bounds[k] = length of casefold(text[:k]); the running offset into cf_text
    # for each source char boundary, so a casefolded match offset maps back to
    # the source index whose bounds entry equals it.
    bounds = [0]
    for ch in text:
        bounds.append(bounds[-1] + len(ch.casefold()))
    cf_text = text.casefold()
    cf_start = cf_text.find(needle_cf)
    if cf_start < 0:
        return None
    cf_end = cf_start + len(needle_cf)
    # Map casefolded offsets to source char boundaries. A fold that expands
    # length can leave an offset mid-expansion (no exact boundary); fall back to
    # the enclosing boundary so the wrap never splits a source character.
    try:
        start = bounds.index(cf_start)
    except ValueError:
        start = next((k for k in range(len(bounds)) if bounds[k] > cf_start), 1) - 1
    try:
        end = bounds.index(cf_end)
    except ValueError:
        end = next((k for k in range(len(bounds)) if bounds[k] >= cf_end), len(bounds) - 1)
    return start, end


def _extract_history_snippet(messages: list[dict], needle: str) -> str:
    """Return a bounded snippet around the first user/assistant message matching *needle*.

    The matched substring is delimited with ``<<<...>>>``. Returns "" when no
    eligible message content contains the needle (e.g. it only matched the title).
    """
    # Defense-in-depth: an empty/whitespace needle makes str.find return 0 on
    # every message and would wrap meaningless text in <<<>>>. The query is
    # already validated non-empty upstream, but guard here too since this helper
    # is independently callable.
    if not needle.strip():
        return ""
    # Same parse as search_sessions: that call decides a session MATCHED on
    # scattered needles, so searching only the whole phrase here would return ""
    # and suppress the row's snippet for exactly the multi-word queries the
    # needle-wise match enables. Ordered highest-signal first (phrase, whole
    # terms and CJK bigrams, then lone characters) so the excerpt centers on
    # the most meaningful hit available.
    needles_cf = snippet_needles(needle)
    if not needles_cf:
        return ""
    for m in messages:
        # Only surface user/assistant content (mirror get_chat_session) so the
        # snippet is the human-facing context, not a tool/system trace blob.
        if str(m.get("role", "")).lower() not in _HISTORY_SNIPPET_ROLES:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        folded = content.casefold()
        for needle_cf in needles_cf:
            idx = folded.find(needle_cf)
            if idx < 0:
                continue
            start = max(0, idx - _SNIPPET_RADIUS)
            end = min(len(content), idx + len(needle_cf) + _SNIPPET_RADIUS)
            seg = content[start:end]
            # Redact BEFORE inserting <<<...>>> markers: marker insertion would
            # split a credential/URL token and defeat the contiguous-match
            # redactors, so a query that is a substring of a secret in stored
            # content could leak it.
            seg = _redact_history_output(seg)
            # Locate the match span in the (possibly redacted) original text using
            # the SAME full casefolding as the selection above — a case-insensitive
            # regex does only simple per-char mapping and would miss multi-char
            # folds (ß→ss), leaving a selected-but-unwrapped snippet with no
            # <<<...>>>.
            span = _casefold_match_span(seg, needle_cf)
            if span:
                s, e = span
                seg = seg[:s] + "<<<" + seg[s:e] + ">>>" + seg[e:]
            seg = ("…" if start > 0 else "") + seg + ("…" if end < len(content) else "")
            result = seg[:_SNIPPET_MAX_LEN]
            # If the hard cap sliced through the match delimiters (possible with a
            # long query), re-close so the consumer never sees a dangling "<<<".
            if "<<<" in result and ">>>" not in result:
                result = result[: _SNIPPET_MAX_LEN - 3] + ">>>"
            return result
    return ""
