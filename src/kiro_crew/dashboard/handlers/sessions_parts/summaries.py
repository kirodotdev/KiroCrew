"""One-line summaries of past sessions for the Older-sessions pane.

``POST /api/sessions/summarize`` and :func:`_summarize_one`, the ephemeral background turn
behind each summary: the bounded prompt and time limit, the filter that drops a reply that
narrates or refuses instead of summarising, and, for an app caller, the ownership re-check
inside each transcript read. New summary rules go here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import guard_owner_surface_routes
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import (
    _NOT_TRANSCRIPT_OWNER,
    _app_owns_transcript,
    _audit_app_allow,
    _audit_app_denial,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    TranscriptBusy,
    TranscriptWithheld,
    transcript_lock_stems,
    transcript_withholds_derivation,
)
from kiro_crew.label_guard import PROSE_OPENERS, is_verdict_reply, looks_like_prose
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


_SUMMARIZE_MAX_SESSIONS = 8  # bound cost/latency: only the top-N get an LLM pass
_SUMMARIZE_MODEL = "auto"  # inherit the governed default; a hardcoded id 400s where unavailable
_SUMMARIZE_MSG_LIMIT = 12  # messages fed to the summarizer per session
_SUMMARIZE_TIMEOUT_SECS = (
    30  # per-session deadline so one stalled prompt can't pin the shared _bg session
)
# Prose ceilings for the refusal guard, scaled to this prompt's 18-word contract
# the way the title's ceilings (12 words / 24 chars) sit above its 3-6 word
# contract: a legitimate long summary clears them, a refusal paragraph does not.
_SUMMARIZE_PROSE_MAX_WORDS = 36
_SUMMARIZE_PROSE_MAX_UNSPACED_CHARS = 72
# A summary DESCRIBES the conversation, so the openers that mark a title reply
# as narration ("the conversation covers ...", "based on the transcript ...")
# are its legitimate shape here; the refusal openers stay. Likewise the
# sentence-shape signals (a mid-line terminator, a Korean polite ending) tell a
# name from a sentence and cannot tell a summary from a refusal, so the guard
# runs without them on this path -- a false positive here is not free: the
# summary is lost AND the model is re-asked on every later list until the
# transcript changes, because "" is never cached.
_SUMMARIZE_NARRATION_PREFIXES = ("the conversation", "this conversation", "based on the")
_SUMMARIZE_PROSE_OPENERS = tuple(
    opener for opener in PROSE_OPENERS if opener not in _SUMMARIZE_NARRATION_PREFIXES
)
# The exemption above is for the AFFIRMATIVE summary shape only. A refusal can
# open the same way ("The conversation is too vague to summarize", "Based on
# the transcript, I cannot determine a topic"), so a line that starts with one
# of the exempted prefixes is still rejected when a refusal marker follows.
_SUMMARIZE_REFUSAL_MARKERS = re.compile(
    r"\b(?:cannot|can't|can not|unable|too (?:vague|short|brief|little)|not enough"
    r"|insufficient|unclear|no clear|nothing to summari[sz]e|not possible"
    r"|do(?:es)? not (?:contain|provide|have|include)|doesn't (?:contain|provide|have|include)"
    r"|don't have|i need|would need)\b",
    re.IGNORECASE,
)


def _summary_is_narrated_refusal(summary: str) -> bool:
    """True for a refusal that opens with an exempted narration prefix."""
    lowered = summary.strip().lower()
    return lowered.startswith(_SUMMARIZE_NARRATION_PREFIXES) and bool(
        _SUMMARIZE_REFUSAL_MARKERS.search(lowered)
    )


_SUMMARIZE_PROMPT = (
    "Summarize the following conversation in ONE terse line (max 18 words), "
    "describing what the user and assistant are working on. No preamble, no "
    "quotes, no trailing period. If the topic is unclear, reply exactly SKIP.\n\n"
    "===== CONVERSATION =====\n"
    "{transcript}\n"
    "===== END ====="
)


def _build_summary_prompt(messages: list[dict]) -> str | None:
    """Build a one-line-summary prompt from a session's recent messages."""
    lines: list[str] = []
    for m in messages[:_SUMMARIZE_MSG_LIMIT]:
        role = m.get("role", "")
        content = " ".join(str(m.get("content", "")).split())
        if role in ("user", "assistant") and content:
            lines.append(f"{role}: {content[:300]}")
    if not lines:
        return None
    return _SUMMARIZE_PROMPT.format(transcript="\n".join(lines))


async def _summarize_one(state: DashboardState, key: str, *, owner_app: str = "") -> str:
    """Generate a one-line LLM summary for a single session. "" on any failure.

    Mirrors dashboard.chat_title._generate_title_via_kiro: uses an ephemeral
    background session on the cheap/fast model and destroys it in a finally.
    Best-effort — every failure path returns "" so the caller falls back to the
    session's stored title.

    With *owner_app* set (an app caller), both transcript reads -- the cached
    summary and the rows -- re-judge :func:`_app_owns_transcript` inside their own
    lock hold, so a same-key delete and recreate after the caller's check yields
    "" instead of a summary of somebody else's transcript.
    """
    log = state.conversation_log
    if not log:
        return ""
    loop = asyncio.get_running_loop()

    def _read_cache_if_derivation_is_allowed() -> tuple[str | None, bool]:
        # The sidecar is derived from the transcript. Hold the same physical
        # lock as metadata writers while validating the line and reading the
        # cache, so a same-key restricted recreation cannot leave a stale
        # persistent summary readable. Unreadable fails closed.
        with log.derivation_hold(transcript_lock_stems(key)):
            if transcript_withholds_derivation(log, key):
                return None, False
            if owner_app and not _app_owns_transcript(log, owner_app, key):
                _audit_app_denial(
                    owner_app, "session_summarize", f"session={key}", _NOT_TRANSCRIPT_OWNER
                )
                return None, False
            return log.get_cached_summary(key), True

    try:
        cached, derivation_allowed = await loop.run_in_executor(
            None, _read_cache_if_derivation_is_allowed
        )
    except TranscriptBusy:
        return ""
    if not derivation_allowed:
        return ""
    if cached:
        return str(cached)
    # Cache: a summary persisted in a sidecar file is reusable as long as the
    # session file hasn't changed since it was generated. session_mtime advances
    # only on real message appends (preserved across metadata writes), so it is a
    # cheap, exact staleness signal — a repeat list_sessions(summarize=true) for
    # an unchanged session pays zero LLM cost. The cache lives in a sidecar
    # (never the session JSONL) so summarizing an *active* session never rewrites
    # its log and cannot lose a concurrently-appended message.
    sig = await loop.run_in_executor(None, log.session_mtime, key)
    # Captured WITH the signature: a rewrite during the model call below
    # preserves the mtime while advancing this counter, and stamping the new
    # content identity onto the older summary would bless it as fresh.
    generation = await loop.run_in_executor(None, log.rotation_generation, key)

    # Through the DERIVATION seam, not the plain ``recent``: the line checked
    # above is a snapshot, and a writer can tighten it before the rows are read
    # (a same-key hand-over landing a closed restricted tab's rows). The seam
    # validates the line with the rows under one lock hold and raises instead of
    # yielding rows a restricted (or unreadable) line governs.
    def _derive_rows() -> list[dict] | None:
        derive = functools.partial(
            log.derive_recent, key, max_messages=_SUMMARIZE_MSG_LIMIT, roles={"user", "assistant"}
        )
        if not owner_app:
            return derive()
        # derive_recent re-enters the same (reentrant) hold.
        with log.derivation_hold(transcript_lock_stems(key)):
            if not _app_owns_transcript(log, owner_app, key):
                _audit_app_denial(
                    owner_app, "session_summarize", f"session={key}", _NOT_TRANSCRIPT_OWNER
                )
                return None
            return derive()

    try:
        messages = await loop.run_in_executor(None, _derive_rows)
    except TranscriptWithheld:
        return ""
    if messages is None:
        return ""
    prompt = _build_summary_prompt(messages)
    if not prompt:
        return ""
    try:
        text = await run_bg_oneliner(
            state.sessions, prompt, model=_SUMMARIZE_MODEL, timeout=_SUMMARIZE_TIMEOUT_SECS
        )
    except Exception:
        logger.debug("Session summary generation failed for %s", key, exc_info=True)
        return ""
    # First line only: the prompt asks for ONE line, and a verdict followed by
    # an explanation ("SKIP\n\nThe topic is unclear.") must reduce to the bare
    # verdict rather than pass the checks below as a five-word "summary".
    summary = text.strip().split("\n", 1)[0].strip().strip('"').strip("'").strip(".")
    if not summary or is_verdict_reply(summary, ("SKIP",)):
        return ""
    summary, _ = redact_exfiltration_urls(summary)
    summary, _ = redact_credentials(summary)
    if _summary_is_narrated_refusal(summary) or looks_like_prose(
        summary,
        max_words=_SUMMARIZE_PROSE_MAX_WORDS,
        max_unspaced_chars=_SUMMARIZE_PROSE_MAX_UNSPACED_CHARS,
        openers=_SUMMARIZE_PROSE_OPENERS,
        sentence_shape=False,
    ):
        # The model refused or narrated instead of summarizing. Return "" so the
        # caller falls back to the stored title, and -- crucially -- do NOT reach
        # the sidecar write below: a cached refusal would be served on every
        # later list until the transcript changes.
        logger.info("Session summary reply is prose, discarding for %s", key)
        return ""
    summary = summary[:200]
    # Revalidate only after the model call has returned: model latency must never
    # block a transcript writer. Keep the hold through the sidecar write so a
    # same-key tightening cannot land between the privacy check and publication.
    if sig is not None:

        def _publish_if_derivation_is_allowed() -> None:
            with log.publication_hold(key):
                log.set_cached_summary(key, summary, sig, generation)

        try:
            await loop.run_in_executor(None, _publish_if_derivation_is_allowed)
        except TranscriptBusy:
            logger.debug(
                "Summary for %s withheld: the transcript lock was busy at publication",
                key,
            )
            return ""
        except TranscriptWithheld:
            logger.debug(
                "Discarding summary for %s: the transcript became restricted "
                "during summarisation",
                key,
            )
            return ""
        except Exception:
            logger.debug("Failed to persist summary cache for %s", key, exc_info=True)
    return summary


async def api_sessions_summarize(request: web.Request) -> web.Response:
    """POST /api/sessions/summarize — one-line LLM summaries for given sessions.

    Body: ``{"keys": ["<session_key>", ...]}``. Only the first
    ``_SUMMARIZE_MAX_SESSIONS`` keys are summarized (cost/latency bound); the
    rest are silently skipped and the caller falls back to their titles.
    Returns ``{"summaries": {key: one_line_summary}}`` — keys that produced no
    usable summary are omitted. Best-effort: a per-session failure never fails
    the whole request.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"summaries": {}})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        return web.json_response({"error": "keys must be a list"}, status=400)
    # Dedupe while preserving order, drop non-strings, then bound the count.
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keys:
        if isinstance(k, str) and k and k not in seen:
            seen.add(k)
            ordered.append(k)
    ordered = ordered[:_SUMMARIZE_MAX_SESSIONS]

    log = state.conversation_log
    request_app = str(request.get("app") or "")
    summaries: dict[str, str] = {}
    for key in ordered:
        if request_app:
            # Skipped like a missing key, so the reply cannot tell the two apart.
            if not await asyncio.to_thread(_app_owns_transcript, log, request_app, key):
                _audit_app_denial(
                    request_app, "session_summarize", f"session={key}", _NOT_TRANSCRIPT_OWNER
                )
                continue
            _audit_app_allow(request_app, "session_summarize", f"session={key}")
        elif not await asyncio.to_thread(log.has_log, key):
            continue
        summary = await _summarize_one(state, key, owner_app=request_app)
        if summary:
            summaries[key] = summary
    return web.json_response({"summaries": summaries})


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name). A
# summary is derived from the owner's own transcript, so a non-owner dashboard
# session (a channel allowlist link, same empty app claim) is refused too.
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(),
    dashboard_owner_only=True,
)
