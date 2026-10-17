"""Auto-skill extraction: which sessions qualify, and how a skill is staged or published.

:meth:`_run_skill_detection` asks the model whether a consolidated session holds a
reusable procedure, after the eligibility helpers here rule out sessions with too few
tool calls or one that touched a sensitive path. :meth:`_process_auto_skills` dedupes a
proposal against the live and pending skills (:meth:`_dedupe_candidate`, judged by
:meth:`_dedupe_judge`), then stages it for approval or publishes it, and
:meth:`_stage_skill_update` stages a refinement of a live skill whose body
:meth:`_merge_skill_update` merges. Every LLM-authored description, trigger list,
procedure and generated script is redacted before ``SkillsLoader`` writes it, and every publication runs under
:meth:`_skill_publication_guard`. The ``_facade_*`` helpers read ``kiro_crew.history`` at
call time, so a patch of ``kiro_crew.history.sel`` or ``stream_and_collect`` reaches them.

Each function here whose first parameter is ``self`` is a ``HistoryConsolidator`` method:
the class binds it under the same name (``history_consolidation.py``), so it reads the
consolidator's own fields and calls its siblings through ``self``.

Instance fields read: ``_approval_required``, ``_archive_after_days``,
``_auto_min_tool_calls``, ``_auto_refine_enabled``, ``_auto_similarity_threshold``,
``_event_loop``, ``_generate_scripts``, ``_judge_model``, ``_last_skillgen_marker``,
``_log``, ``_max_auto_skills``, ``_sessions``, ``_skills_loader``, ``_stale_after_days``.

Instance fields written: ``_last_skillgen_marker``.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports only ``history_consolidation_span``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.frontmatter import SKILL_UPDATE, frontmatter_value
from kiro_crew.history_consolidation_span import (
    _ConsolidationNotDispatched,
    _fmt_message,
    _prompt_rows,
    _RunCommitState,
)
from kiro_crew.llm_helpers import ToolApprovalPolicy, background_turn
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS, AutoSkillProvenance, ClaimRefusal
from kiro_crew.skills_dedupe import VERDICT_DUP, VERDICT_NEW, VERDICT_UPDATE
from kiro_crew.skills_script_validator import validate_skill_script

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator


_SKILL_DETECTION_WINDOW = 200


def _facade_sel() -> Any:
    from kiro_crew import history as history_facade

    return history_facade.sel()


def _facade_stream_and_collect(*args: Any, **kwargs: Any) -> Awaitable[str | None]:
    from kiro_crew import history as history_facade

    return history_facade.stream_and_collect(*args, **kwargs)


def _facade_metadata_dedupe_verdict(
    candidate: dict,
    existing: list[dict],
    judge: Callable[[str], str],
) -> tuple[str, str | None]:
    from kiro_crew import history as history_facade

    return history_facade.metadata_dedupe_verdict(candidate, existing, judge)


# ── Module-level helpers for auto skill eligibility ──
#
# Kept at module level so they're trivially unit-testable without
# instantiating HistoryConsolidator.

# Canonical tool titles that indicate a read targeting a sensitive path.
# Supplements is_sensitive_path() and is_sensitive_bash_command() which
# handle the actual runtime blocking — this is a second-layer defense
# that refuses to extract a skill if the session tried to access a
# sensitive path, even when the attempt was denied at hook time.
_SENSITIVE_TOOL_PATTERNS: tuple[str, ...] = (
    ".aws/",
    ".ssh/",
    ".gnupg/",
    ".gpg/",
    ".docker/config",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # Kiro Crew's own credential file. The data home moved to ~/.kiro/crew, so the
    # LIVE secret is ~/.kiro/crew/.env; cover the pre-move legacy home too
    # (substring match, so bare "/.env"-suffixed forms).
    ".kiro/crew/.env",
    ".kirocrew/.env",
    "169.254.169.254",  # IMDS
)


_TOOL_ROLES: frozenset[str] = frozenset({"tool", "tool_call", "tool_result"})


def _frontmatter_value(text: str | None, key: str) -> str:
    """Return *key*'s frontmatter value from a SKILL.md body, or "".

    Values resolve the way ``SkillsLoader._parse_frontmatter`` resolves them:
    only a column-0 key is a field, and a bare block-scalar indicator
    (``>``/``|``, optionally chomped) folds the indented lines that follow.
    The auto-skill update path carries the live skill's ``description`` and
    ``triggers`` through this reader into a staged candidate that overwrites
    the live skill on approval — reading the indicator verbatim would collapse
    a block-scalar description to ``""`` and inject a bogus ``>`` trigger on
    that round-trip. The grammar (plus the leading-whitespace opener
    tolerance, verbatim plain values, and first-duplicate-wins lookup) is
    pinned as ``frontmatter.SKILL_UPDATE``.
    """
    if not text:
        return ""
    return frontmatter_value(text, key, SKILL_UPDATE)


def _merge_trigger_lists(live: str, candidate: str, *, cap: int = 12) -> str:
    """Union two comma-separated trigger lists, live first, case-insensitively
    deduped and capped.

    Triggers are the skill's ACTIVATION surface. An update proposes triggers for
    the new requirement only, so replacing the live list would stop the skill
    firing on every phrasing it already answered — a silent regression the diff
    shows but nobody reads as a behavior change. Union instead, and cap so
    repeated updates cannot grow the list without bound.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for raw in (live or "").split(",") + (candidate or "").split(","):
        t = re.sub(r"\s+", " ", raw).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)
        if len(merged) >= cap:
            break
    return ", ".join(merged)


def _strip_skill_frontmatter(text: str | None) -> str:
    """Return *text* with a leading ``---`` frontmatter block removed.

    A skill body read off disk carries its frontmatter header; only the prose
    below it may be fed to (or accepted from) the update-merge turn, because
    ``stage_skill_candidate`` re-emits frontmatter of its own. Text without a
    leading block is returned unchanged (stripped). A fence LOCATOR, not a
    field parser — deliberately outside ``kiro_crew.frontmatter``; editing
    its grammar means revisiting ``_frontmatter_value``'s dialect too. Like
    that dialect's fence, an optional carriage return before each fence
    newline is tolerated, so the locator strips exactly the block the field
    parser reads.
    """
    if not text:
        return ""
    m = re.match(r"^\s*---\r?\n.*?\r?\n---\r?\n?(.*)$", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _strip_code_fence(text: str) -> str:
    """Unwrap a single outer ```/```markdown fence, if the model emitted one."""
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    lines = s.split("\n")
    if len(lines) < 2:
        return s
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _count_tool_call_messages(messages: list[dict]) -> int:
    """Count messages that represent tool invocations under either schema.

    Two recording formats exist:
    - Legacy (Slack pipeline): assistant messages carry a ``tools`` list field.
    - Dashboard pipeline: separate messages with ``role`` in {"tool", "tool_call",
      "tool_result"} and the tool name embedded in ``content``.

    A message matching EITHER condition counts once (no double-counting).
    """
    count = 0
    for msg in messages:
        tools = msg.get("tools")
        if isinstance(tools, list) and tools:
            count += 1
        elif msg.get("role") in _TOOL_ROLES:
            count += 1
    return count


def _session_touched_sensitive(messages: list[dict]) -> bool:
    """Return True if any tool call in the session referenced a sensitive path.

    Checks both recording schemas:
    - Legacy: substring match over each entry in ``msg["tools"]`` list.
    - Dashboard: substring match over ``content`` when ``role`` indicates a tool event.

    Designed to be conservative — a false positive just means we skip
    auto-creation for this session.
    """
    for msg in messages:
        # Legacy schema: tools list on assistant messages
        tools = msg.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, str):
                    continue
                lower = tool.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
        # Dashboard schema: role="tool" with tool info in content
        if msg.get("role") in _TOOL_ROLES:
            content = msg.get("content", "")
            if isinstance(content, str):
                lower = content.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
    return False


async def _run_skill_detection(
    self: HistoryConsolidator, key: str, commit_state: _RunCommitState | None = None
) -> None:
    """Detect a reusable skill from the FULL session (bounded window).

    Unlike history/semantic/lesson extraction — which correctly runs on the
    incremental unconsolidated tail — skill detection judges the last
    ``_SKILL_DETECTION_WINDOW`` messages of the WHOLE session, decoupled
    from the consolidation offset. A reusable procedure usually spans a
    session rather than the slice since the last consolidation, so a
    tail-only view systematically misses skills in any session consolidated
    more than once. The skill need only be demonstrated by PART of the
    window; the pass does not have to cover the whole session.

    Runs as its own LLM call so the consolidation prompt stays tail-scoped
    (widening THAT prompt would re-summarize already-consolidated messages
    into duplicate history/semantic entries). A per-session
    (rotation_generation, count) guard skips re-running when nothing new has
    been appended since the last pass, yet still forces a fresh pass after a
    transcript rotation (which swaps the window's content); genuine repeats
    are still caught by the dedupe verdict in ``_process_auto_skills``.

    The prompt gates on RECURRENCE, not effort. A session can be long,
    difficult, and rich in tool calls while still being one-off — a single
    bug's fix, a one-time audit of one component, a probe answering a
    question that is now answered — and the tool-call floor
    (``auto_min_tool_calls``) cannot tell those apart from a repeatable
    method. So the prompt makes the model name the future session and the
    DIFFERENT target that would reuse the procedure, and return null when
    the only honest answer reuses this session's own artifact. It also
    prefers null under uncertainty: an unreusable candidate is not free,
    because it spends the human's review attention on every later proposal.
    """
    if self._skills_loader is None:
        return
    # circular import: kiro_crew.history re-exports this module
    from kiro_crew.history import TranscriptWithheld

    # Through the derivation seam: a third read of the transcript, so the line
    # is validated with THESE rows under the lock (the consolidation snapshots
    # above vouched for their own rows, not these).
    try:
        all_messages = await asyncio.to_thread(self._log.derive_messages, key)
    except TranscriptWithheld:
        return
    if not all_messages:
        return
    # Key the guard on (rotation generation, message count), NOT count
    # alone. The transcript rotates at _SESSION_MAX_BYTES / _SESSION_KEEP_LINES:
    # a rotation bumps rotation_generation and replaces the window with fresh
    # messages even when the resulting count matches a prior value, so a
    # count-only guard would wrongly treat a rotated session as unchanged and
    # never propose its skill. Comparing the pair re-detects after any
    # rotation while still skipping a genuinely unchanged session.
    generation = await asyncio.to_thread(
        lambda: int(self._log._read_metadata(key).get("rotation_generation", 0) or 0)
    )
    marker = (generation, len(all_messages))
    if self._last_skillgen_marker.get(key) == marker:
        return
    window = all_messages[-_SKILL_DETECTION_WINDOW:]
    if _count_tool_call_messages(window) < self._auto_min_tool_calls:
        return
    if _session_touched_sensitive(window):
        return

    scripts_field = ""
    if self._generate_scripts:
        scripts_field = (
            ', "scripts": (optional array, part of THIS new_skill '
            "object) ONLY when the procedure includes a "
            "DETERMINISTIC, always-identical step sequence worth "
            "running verbatim (a fixed command chain, a set API "
            "sequence, a predictable file transform). Each item: "
            '{"filename": "<name>.py", "language": "python", '
            '"content": "<self-contained Python, no network to '
            "unknown hosts, no credential access, no destructive "
            'commands, <=4KB>"}. Python ONLY (must run on Windows). '
            "Omit for judgment-based / context-dependent procedures. "
            "Scripts always require human approval"
        )
    skill_keys = [
        '"new_skill": Object or null. Return an object ONLY if this '
        "session demonstrated a procedure that will RECUR — one a future "
        "session, working on a DIFFERENT target, would run again "
        "substantially unchanged (e.g. a repeatable debugging method for a "
        "class of error, a fixed command/API sequence, a verification "
        "technique). The procedure may be demonstrated by only PART of the "
        "excerpt below — you do NOT need to cover the whole session. "
        "Shape: "
        '{"slug": "<kebab-case-4-to-60-chars>", '
        '"description": "<=150 chars, starts with verb>", '
        '"triggers": "<3-8 comma-separated keywords/phrases>", '
        '"procedure_md": "<concise markdown body with '
        "## When to use / ## Steps / ## Gotchas sections, "
        '<=8000 chars>"' + scripts_field + "}. "
        "## The recurrence test (apply BEFORE returning an object)\n"
        "Name the future session that would load this skill and the "
        "DIFFERENT target it would run against. If the only honest answer "
        "reuses this session's specific artifact — this bug, this file, "
        "this component, this one question — the procedure does not recur "
        "and you MUST return null. Effort is not evidence of recurrence: a "
        "long, many-step, genuinely difficult session is still one-off if "
        "its steps were chosen for one target.\n"
        "Return null for: a task done once and now finished (a specific "
        "bug's fix, a one-time audit/trace of one component, a migration, "
        "a probe run to answer a question that is now answered); a design "
        "or planning discussion; a narrative of what happened in this "
        "session; a procedure whose steps only make sense against the "
        "exact artifact at hand; a trivial or single-shot answer; a "
        "one-off failure with no reusable takeaway; anything touching "
        "sensitive paths. Prefer null when uncertain — an unreusable "
        "candidate costs the user review effort on every future proposal, "
        "so silence is cheaper than a plausible-looking one-off. "
        "Do NOT include absolute paths, credentials, tokens, or user PII "
        "in the procedure body."
    ]
    if self._auto_refine_enabled:
        skill_keys.append(
            '"refined_skill": Object or null. If an existing '
            '"auto/..." skill was loaded during this session AND '
            "the agent found a better procedure than the one "
            "documented in that skill, return: "
            '{"name": "auto/<existing-slug>", '
            '"description": "<updated>", "triggers": "<updated>", '
            '"procedure_md": "<refined markdown>"}. Return null '
            "if nothing was refined. Do not fabricate refinements."
        )
    numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(skill_keys))
    conversation = "\n".join(_fmt_message(m) for m in _prompt_rows(window))
    prompt = (
        "You are a skill-extraction agent. Review this session excerpt and "
        "return a JSON object with these keys:\n\n"
        + numbered
        + "\n\n## Session excerpt\n"
        + conversation
        + "\n\nRespond with ONLY valid JSON, no markdown fences."
    )
    try:
        result = await self._call_llm(prompt)
    except _ConsolidationNotDispatched:
        # Skill detection is best-effort and owns no retry accounting, so an
        # unreachable provider is simply no detection this pass. The marker
        # below is still recorded, matching the existing failed-turn path.
        result = None
    # Record the (generation, count) marker regardless of outcome so an
    # unchanged session isn't re-evaluated on every subsequent
    # consolidation, but a rotation still forces a fresh pass.
    self._last_skillgen_marker[key] = marker
    if not result:
        return
    # Log the verdict, not just the proposals. The prompt's default is null,
    # so silence is the common outcome, and the staging log in
    # ``_process_auto_skills`` only fires when a candidate is produced --
    # which would leave the queue showing the false-POSITIVE rate while the
    # false-negative rate had no signal at all.
    self._logger.debug(
        "Skill detection verdict for %s: %s",
        key,
        "candidate proposed" if result.get("new_skill") else "no recurring procedure",
    )
    # _event_loop was captured by our caller (_consolidate) so the
    # thread-offloaded dedupe judge can marshal back onto the gateway loop.
    refusal = ClaimRefusal()
    await asyncio.to_thread(
        self._process_auto_skills,
        result,
        key,
        guard_publication=True,
        commit_state=commit_state,
        refusal=refusal,
    )
    if refusal.retryable:
        # A claim path refused because the slug claim lock was unavailable --
        # a property of the moment, not of the candidate. The marker recorded
        # above would otherwise skip this session until a further message
        # changed the count or a restart cleared it, so a session that goes
        # quiet right after the stall would lose the candidate. Retracting it
        # makes the retry the lock helper documents actually happen on the
        # next pass. The consolidation offset is deliberately NOT held back:
        # history, semantic and lesson extraction share it, so rewinding it
        # would re-summarize an already-consolidated tail into duplicates,
        # which is why skill detection was decoupled from that offset.
        self._last_skillgen_marker.pop(key, None)


def _dedupe_candidate(
    self: HistoryConsolidator, slug: str, description: str, triggers: str
) -> "tuple[str, str | None]":
    """Classify a candidate against existing auto-skills.

    Returns ``(verdict, key)`` where ``verdict`` is one of ``VERDICT_NEW``
    (stage as a new candidate), ``VERDICT_DUP`` (drop — pure re-detection),
    or ``VERDICT_UPDATE`` (stage a pending update to ``key``). ``key`` is the
    matched/target existing-skill key for DUP/UPDATE, else ``None``.

    Primary: a single tri-state metadata-judge call comparing the candidate
    against ALL existing auto-skills at once (bounded set, no embeddings).
    Lexical ``find_similar`` runs as a fallback when the judge is unavailable
    (no ``judge_model``, no captured event loop, or no existing skills) AND
    as a safety net when the judge returns ``VERDICT_NEW`` — so a judge
    *failure* (which fails open to "new") can't silently skip dedup and let
    a near-identical skill through. A lexical hit is treated as a DUP.
    """
    loader = self._skills_loader
    if loader is None:
        return (VERDICT_NEW, None)
    existing = list(loader.list_auto_skills())
    # Include already-staged (pending) candidates so repeated sessions don't
    # queue a duplicate of something still awaiting review (list_auto_skills
    # only enumerates LIVE skills — .pending is pruned from discovery).
    try:
        for p in loader.list_pending_skills():
            existing.append(
                {
                    "key": f"auto/{p.get('slug', '')}",
                    "description": p.get("description", ""),
                    "triggers": p.get("triggers", ""),
                }
            )
    except Exception:
        pass
    loop = self._event_loop

    def _lexical() -> "tuple[str, str | None]":
        # ``loader`` is not None here: mypy checks this owner before the class that
        # binds it, so the narrowing above does not reach this closure.
        hit = loader.find_similar(description, threshold=self._auto_similarity_threshold)  # type: ignore[union-attr]
        return (VERDICT_DUP, hit) if hit else (VERDICT_NEW, None)

    if self._judge_model and existing and loop is not None:

        def _judge_fn(prompt: str) -> str:
            try:
                # ``loop`` is not None here: mypy checks this owner before the class that
                # binds it, so the narrowing above does not reach this closure.
                fut = asyncio.run_coroutine_threadsafe(self._dedupe_judge(prompt), loop)  # type: ignore[arg-type]
                return fut.result(timeout=60) or ""
            except Exception:
                return ""

        candidate = {
            "key": f"auto/{slug}",
            "description": description,
            "triggers": triggers,
        }
        verdict, key = _facade_metadata_dedupe_verdict(candidate, existing, _judge_fn)
        # VERDICT_NEW means "new" OR a judge error (the verdict API fails open
        # to new). Either way, confirm with the cheap lexical check before
        # concluding the candidate is unique.
        if verdict == VERDICT_NEW:
            return _lexical()
        return (verdict, key)
    return _lexical()


async def _dedupe_judge(self: HistoryConsolidator, prompt: str) -> str:
    """One cheap metadata-dedupe judge turn on the shared background session.
    Runs on that session's existing model — no per-turn
    ``set_model`` switch, because the ``BACKGROUND_KEY`` session is shared
    with other lightweight jobs and a switch would leak into later
    turns when recycling doesn't fire. Fail-open (returns "" on any error)."""
    if not self._sessions:
        return ""
    try:
        async with background_turn(
            self._sessions, task="skill_dedupe", agent="kirocrew-lite"
        ) as client:
            text = await _facade_stream_and_collect(
                client,
                prompt,
                approval_policy=ToolApprovalPolicy.REJECT_ALL,
                allow_image=False,
            )
        return text or ""
    except Exception:
        self._logger.debug("Skill dedupe judge failed", exc_info=True)
        return ""


async def _merge_skill_update(
    self: HistoryConsolidator, live_body: str, description: str, triggers: str, procedure_md: str
) -> "str | None":
    """Merge an existing live skill body with a new candidate into ONE
    updated markdown body — a single text turn on the shared background
    session. Mirrors ``_dedupe_judge`` exactly. Fail-open (returns ``None`` on
    any error) so the caller can fall back to a plain replacement proposal."""
    if not self._sessions:
        return None
    prompt = (
        "You are updating an existing auto-generated agent skill with a newly "
        "learned requirement. Merge the EXISTING skill body and the NEW "
        "requirement into ONE updated markdown skill body — fold the new "
        "requirement in, do NOT blindly replace the existing content. Keep "
        "the '## When to use', '## Steps', and '## Gotchas' sections. Keep "
        "the result under 8000 characters. Output ONLY the updated markdown "
        "body — no preamble, no explanation, no code fences.\n\n"
        f"EXISTING skill body:\n{live_body}\n\n"
        f"NEW requirement — description: {description}\n"
        f"NEW requirement — triggers: {triggers}\n"
        f"NEW requirement — procedure:\n{procedure_md}\n"
    )
    try:
        async with background_turn(
            self._sessions, task="skill_merge", agent="kirocrew-lite"
        ) as client:
            text = await _facade_stream_and_collect(
                client,
                prompt,
                approval_policy=ToolApprovalPolicy.REJECT_ALL,
                allow_image=False,
            )
        return text or None
    except Exception:
        self._logger.debug("Skill update merge failed", exc_info=True)
        return None


@contextlib.contextmanager
def _skill_publication_guard(
    self: HistoryConsolidator,
    key: str,
    *,
    enabled: bool,
    commit_state: _RunCommitState | None = None,
):
    """Hold the transcript contract stable across one final skill write."""
    state = commit_state or _RunCommitState()
    if not enabled:
        yield state
        return
    # Circular import: kiro_crew.history re-exports this module. The lock is
    # acquired only after every model call has returned: model latency must
    # never block a transcript writer. Keeping it through the final staging
    # or publication call closes the check-to-write race instead.
    from kiro_crew.history import TranscriptBusy, TranscriptWithheld

    entered = False
    try:
        with self._publication_hold_checked(key, state) as publication:
            entered = True
            yield publication
    except TranscriptBusy:
        # Only an acquisition refusal maps to the existing no-publication arm;
        # do not swallow an unrelated busy error from the guarded write body.
        if entered:
            raise
        self._logger.debug(
            "Discarding skill detection result for %s: the transcript was busy "
            "during extraction",
            key,
        )
        yield None
    except TranscriptWithheld:
        if entered:
            raise
        self._logger.debug(
            "Discarding skill detection result for %s: the transcript became "
            "restricted during extraction",
            key,
        )
        yield None


def _stage_skill_update(
    self: HistoryConsolidator,
    *,
    key: str,
    target_key: str,
    description: str,
    triggers: str,
    procedure_md: str,
    scripts: "list[dict] | None" = None,
    guard_publication: bool = False,
    commit_state: _RunCommitState | None = None,
    refusal: ClaimRefusal | None = None,
) -> None:
    """Stage a pending UPDATE candidate for an existing auto-skill.

    (a) read the target's current live body; (b) LLM-merge it with the new
    requirement (bridged from this worker thread onto the captured loop,
    90s, fail-open); (c) use the redacted merge as the proposed body, else
    fall back to the candidate's own procedure (also on oversize); (d) stage
    under ``<target-slug>-update`` with ``kind='update'`` metadata; (e) SEL
    audit with outcome ``staged_update``."""
    loader = self._skills_loader
    if loader is None:
        return

    def _redact(text: object) -> str:
        if not isinstance(text, str):
            return ""
        safe, _ = redact_exfiltration_urls(text)
        safe, _ = redact_credentials(safe)
        return safe

    target_slug = target_key.split("/", 1)[-1]
    # Capture the base version BEFORE reading the body it describes. The merge
    # turn below can take up to 90s, and an approval landing in that window
    # advances live — sampling the version afterwards would record the NEW
    # version against a body merged from the OLD one, and
    # ``approve_pending_update_checked``'s staleness guard would then see base ==
    # current and let the stale body overwrite the intervening update. Reading
    # it first fails safe in the other direction: if live advances after this
    # point the recorded base is behind, the guard fires, and the candidate is
    # refused rather than silently applied.
    try:
        base_version = loader.get_auto_skill_version(target_key)
    except Exception:
        base_version = 1
    try:
        live_body = loader.read_auto_skill_body(target_key)
    except Exception:
        live_body = None
    if not live_body:
        # ``_dedupe_candidate`` deliberately includes already-PENDING
        # candidates in the judge's ``existing`` set (so repeated sessions
        # don't queue duplicates), which means the judge can answer
        # ``UPDATE auto/<pending-slug>`` — a target that is not live.
        # ``approve_pending_update_checked`` requires a live target, so staging that
        # would queue a candidate the user can never approve. Drop it
        # instead, audited so the loss is visible.
        self._logger.info(
            "Skill update skipped: target '%s' is not a live auto skill",
            target_key,
        )
        _facade_sel().log_tool_invocation(
            session_key=key,
            tool_name="auto_skill_create",
            tool_kind="skills",
            outcome="rejected",
            metadata={"target": target_key, "reason": "target_not_live"},
        )
        return
    # ``read_auto_skill_body`` returns the FULL SKILL.md (frontmatter
    # included). Only the prose body may be merged: ``stage_skill_candidate``
    # re-wraps the result in its own frontmatter, so feeding the header in
    # invites the merge to echo it back and nest a second ``---`` block
    # inside the procedure.
    # Redact before the merge prompt. The read path already refuses symlinks
    # into credential storage, but a credential can also be typed straight
    # INTO a skill body via the dashboard editor — that file legitimately
    # lives in the skills tree, so no path guard catches it. The candidate's
    # own description/triggers/procedure are redacted upstream; this was the
    # one input reaching the model raw. (Redaction also runs on the merge
    # OUTPUT, which is too late to protect the prompt.)
    live_prose = _redact(_strip_skill_frontmatter(live_body))

    merged: "str | None" = None
    if live_prose and self._event_loop is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._merge_skill_update(live_prose, description, triggers, procedure_md),
                self._event_loop,
            )
            merged = fut.result(timeout=90)
        except Exception:
            merged = None

    used_merge = False
    body = procedure_md
    if merged:
        # Defensive sanitize: the prompt forbids fences/frontmatter, but a
        # model may still emit them — strip both so the staged candidate's
        # procedure is pure markdown prose.
        red = _redact(_strip_skill_frontmatter(_strip_code_fence(merged)))
        if red and len(red) <= AUTO_SKILL_MAX_PROCEDURE_CHARS:
            body = red
            used_merge = True

    provenance = AutoSkillProvenance(session_key=key, created_at=AutoSkillProvenance.now_iso())
    # The slug pattern caps at 64 chars, and our own generation prompt permits
    # up to 60, so `<target>-update` can overflow and be REJECTED by staging —
    # silently dropping the learning, because consolidation advances its
    # message offset regardless of candidate outcome. Reserve room for
    # "-update" (7) plus the "-2".."-50" collision suffix (3).
    _update_slug = f"{target_slug[:54].rstrip('-')}-update"
    # Approval writes the candidate's frontmatter over the live skill, so the
    # candidate must carry the MERGED metadata, not just its own. The body is
    # merged by the LLM turn above; description/triggers were not, and the
    # candidate only proposes triggers for the NEW requirement — replacing the
    # live list would stop the skill activating on everything it already
    # answered. Union the triggers and keep the live description when the
    # candidate did not supply one.
    _live_triggers = _frontmatter_value(live_body, "triggers")
    _live_description = _frontmatter_value(live_body, "description")
    _staged_triggers = _merge_trigger_lists(_live_triggers, triggers)
    _staged_description = description or _live_description
    with self._skill_publication_guard(
        key, enabled=guard_publication, commit_state=commit_state
    ) as publication:
        if publication is None:
            return
        name = loader.stage_skill_candidate(
            _update_slug,
            description=_staged_description,
            triggers=_staged_triggers,
            procedure_md=body,
            provenance=provenance,
            scripts=scripts or None,
            kind="update",
            target=target_key,
            base_version=base_version,
            refusal=refusal,
        )
        if name:
            publication.mark_committed()
    if name:
        self._logger.info(
            "Staged skill update %s (target %s) from session %s",
            name,
            target_key,
            key,
        )
        _facade_sel().log_tool_invocation(
            session_key=key,
            tool_name="auto_skill_create",
            tool_kind="skills",
            outcome="staged_update",
            metadata={
                "name": name,
                "target": target_key,
                "base_version": base_version,
                "merged": used_merge,
            },
        )
    else:
        self._logger.info("Skill update staging rejected for target '%s'", target_key)
        _facade_sel().log_tool_invocation(
            session_key=key,
            tool_name="auto_skill_create",
            tool_kind="skills",
            outcome="rejected",
            metadata={"slug": _update_slug, "reason": "creation_failed"},
        )


def _process_auto_skills(
    self: HistoryConsolidator,
    result: dict,
    key: str,
    *,
    guard_publication: bool = False,
    commit_state: _RunCommitState | None = None,
    refusal: ClaimRefusal | None = None,
) -> None:
    """Extract + write auto-generated skills from the consolidation result.

    Handles both ``new_skill`` and ``refined_skill`` result keys.  Each
    is validated, redacted via ``security.redact_*``, then deduped
    against existing skills (for new creation) before being written
    through ``SkillsLoader``.  Every successful write emits a SEL audit
    event via ``_facade_sel().log_tool_invocation``.

    ``refusal``, when supplied, is filled in by whichever claim path was
    refused because the slug claim lock was unavailable. That is the one
    not-staged outcome worth another pass, and the caller uses it to retract
    this session's detection marker; every other rejection is a property of
    the candidate and stays final. It is an out-parameter, not a return value,
    so a test that patches this method away cannot accidentally report a
    refusal that never happened.
    """
    if self._skills_loader is None:
        return

    def _redact(text: object) -> str:
        """Run the same two-pass redaction used for Slack/dashboard output."""
        if not isinstance(text, str):
            return ""
        safe, _ = redact_exfiltration_urls(text)
        safe, _ = redact_credentials(safe)
        return safe

    # Create path
    new_skill = result.get("new_skill")
    if isinstance(new_skill, dict):
        slug = str(new_skill.get("slug", "")).strip()
        description = _redact(new_skill.get("description", ""))
        triggers = _redact(new_skill.get("triggers", ""))
        procedure_md = _redact(new_skill.get("procedure_md", ""))
        # Extract + statically validate any generated scripts. Scripts are
        # redacted, then each is checked by the always-on static validator;
        # only individually-clean scripts survive. A script-bearing
        # candidate ALWAYS routes to approval (never auto-published).
        valid_scripts: list[dict] = []
        scripts_supplied = False
        if self._generate_scripts:
            raw_scripts = new_skill.get("scripts")
            if isinstance(raw_scripts, list) and raw_scripts:
                scripts_supplied = True
                for s in raw_scripts:
                    if not isinstance(s, dict):
                        continue
                    fn = _redact(s.get("filename", "")).strip()
                    body = _redact(s.get("content", ""))
                    ok, _findings = validate_skill_script(fn, body)
                    if ok:
                        valid_scripts.append({"filename": fn, "content": body})
                    else:
                        self._logger.info(
                            "Auto-skill script %r rejected by validator: %s",
                            fn,
                            "; ".join(_findings),
                        )
        if not (slug and description and procedure_md):
            # Required fields missing (or stripped empty by redaction).
            # Audit the rejection so operators can see that a create
            # attempt happened but lacked the minimum inputs.
            self._logger.info(
                "Auto-skill create skipped: empty slug/description/procedure "
                "after redaction (slug=%r)",
                slug,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="rejected",
                metadata={
                    "slug": slug or "(empty)",
                    "reason": "empty_after_redaction",
                },
            )
        else:
            verdict, target = self._dedupe_candidate(slug, description, triggers)
            # ``_dedupe_candidate`` deliberately shows the judge already-PENDING
            # candidates too (so repeat sessions don't queue duplicates), which
            # means an UPDATE verdict can name a target that is not LIVE. Such a
            # target cannot be updated — but the requirement is genuinely new
            # relative to the live skill set, and consolidation advances its
            # message offset regardless, so dropping it would lose the learning
            # for good. Downgrade to a NEW candidate instead: it only overlaps
            # another *proposal*, which the human reviews side by side anyway.
            if verdict == VERDICT_UPDATE and target:
                try:
                    _target_is_live = self._skills_loader.read_auto_skill_body(target) is not None
                except Exception:
                    _target_is_live = False
                if not _target_is_live:
                    self._logger.info(
                        "Auto-skill UPDATE target '%s' is not live (pending candidate); "
                        "staging '%s' as a new candidate instead of dropping it",
                        target,
                        slug,
                    )
                    verdict = VERDICT_NEW
            if verdict == VERDICT_DUP:
                self._logger.info(
                    "Auto-skill synthesis skipped: '%s' overlaps existing skill '%s'",
                    slug,
                    target,
                )
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_create",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={
                        "slug": slug,
                        "reason": "similar_exists",
                        "existing": target,
                    },
                )
            elif verdict == VERDICT_UPDATE and target:
                # Same skill, new requirements worth folding in — stage a
                # pending UPDATE candidate rather than dropping the learning.
                self._stage_skill_update(
                    key=key,
                    target_key=target,
                    description=description,
                    triggers=triggers,
                    procedure_md=procedure_md,
                    scripts=valid_scripts or None,
                    guard_publication=guard_publication,
                    commit_state=commit_state,
                    refusal=refusal,
                )
            else:
                provenance = AutoSkillProvenance(
                    session_key=key,
                    created_at=AutoSkillProvenance.now_iso(),
                )
                if scripts_supplied and not valid_scripts and not self._approval_required:
                    # The user opted out of prose review, but this candidate
                    # attempted to add executable content and every script
                    # failed validation. Do not disguise it as a prose-only
                    # skill, and do not create an approval request the user
                    # explicitly disabled: reject the candidate as a whole.
                    self._logger.info(
                        "Auto-skill candidate %s rejected: all supplied scripts failed validation",
                        slug,
                    )
                    _facade_sel().log_tool_invocation(
                        session_key=key,
                        tool_name="auto_skill_create",
                        tool_kind="skills",
                        outcome="rejected",
                        metadata={"slug": slug, "reason": "all_scripts_rejected"},
                    )
                elif self._approval_required or valid_scripts:
                    # Stage when review is enabled or the candidate retained
                    # a validator-passed script. A mixed candidate keeps only
                    # the scripts that passed validation; with review enabled,
                    # an all-rejected candidate can still be inspected as
                    # prose. (An all-invalid candidate with approval disabled
                    # is consumed by the reject branch above, so a bare
                    # scripts_supplied never decides this branch.)
                    with self._skill_publication_guard(
                        key,
                        enabled=guard_publication,
                        commit_state=commit_state,
                    ) as publication:
                        if publication is None:
                            return
                        name = self._skills_loader.stage_skill_candidate(
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                            scripts=valid_scripts or None,
                            refusal=refusal,
                        )
                        if name:
                            publication.mark_committed()
                    if name:
                        self._logger.info("Staged skill candidate %s from session %s", name, key)
                        _facade_sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="staged",
                            metadata={"name": name, "scripts": len(valid_scripts)},
                        )
                    else:
                        self._logger.info("Skill staging rejected for slug '%s'", slug)
                        _facade_sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="rejected",
                            metadata={"slug": slug, "reason": "creation_failed"},
                        )
                else:
                    with self._skill_publication_guard(
                        key,
                        enabled=guard_publication,
                        commit_state=commit_state,
                    ) as publication:
                        if publication is None:
                            return
                        name = self._skills_loader.create_auto_skill(
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                            refusal=refusal,
                        )
                        if name:
                            publication.mark_committed()
                    if name:
                        self._logger.info("Auto-created skill %s from session %s", name, key)
                        _facade_sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="invoked",
                            metadata={"name": name},
                        )
                        # Bound the live auto-skill set after a live create
                        # (auto-approve path). Best-effort; never break
                        # consolidation on a lifecycle hiccup.
                        try:
                            self._skills_loader.run_skill_lifecycle(
                                max_auto_skills=self._max_auto_skills,
                                stale_after_days=self._stale_after_days,
                                archive_after_days=self._archive_after_days,
                            )
                        except Exception:  # pragma: no cover - defensive
                            self._logger.debug("Skill lifecycle pass failed", exc_info=True)
                    else:
                        # create_auto_skill returned None: invalid slug,
                        # oversized procedure, or directory already exists.
                        # Audit the rejection so operators can see why.
                        self._logger.info(
                            "Auto-skill creation rejected for slug '%s' (creation_failed)",
                            slug,
                        )
                        _facade_sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="rejected",
                            metadata={
                                "slug": slug,
                                "reason": "creation_failed",
                            },
                        )
    else:
        # Eligible session ran the skill-gen prompt, but the model returned
        # no new-skill candidate. Emit a lightweight audit trail so
        # operators can distinguish "asked, model declined" from "never
        # asked" — with no event or log line here the audit log cannot show
        # whether skill generation was attempted during a consolidation.
        self._logger.info(
            "Auto-skill: model proposed no skill candidate for session %s",
            key,
        )
        _facade_sel().log_tool_invocation(
            session_key=key,
            tool_name="auto_skill_create",
            tool_kind="skills",
            outcome="skipped",
            metadata={"reason": "no_candidate_proposed"},
        )

    # Refine path (only if explicitly enabled)
    if not self._auto_refine_enabled:
        return
    refined = result.get("refined_skill")
    if isinstance(refined, dict):
        name = str(refined.get("name", "")).strip()
        if not self._skills_loader.is_auto_generated(name):
            self._logger.info("Auto-skill refine rejected for %s: not in auto namespace", name)
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_refine",
                tool_kind="skills",
                outcome="rejected",
                metadata={"name": name, "reason": "not_auto_namespace"},
            )
            return
        description = _redact(refined.get("description", ""))
        triggers = _redact(refined.get("triggers", ""))
        procedure_md = _redact(refined.get("procedure_md", ""))
        if not description or not procedure_md:
            self._logger.info(
                "Auto-skill refine skipped for %s: empty description/procedure " "after redaction",
                name,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_refine",
                tool_kind="skills",
                outcome="rejected",
                metadata={"name": name, "reason": "empty_after_redaction"},
            )
            return
        provenance = AutoSkillProvenance(
            session_key=key,
            created_at=AutoSkillProvenance.now_iso(),
            refined_at=AutoSkillProvenance.now_iso(),
        )
        with self._skill_publication_guard(
            key,
            enabled=guard_publication,
            commit_state=commit_state,
        ) as publication:
            if publication is None:
                return
            ok = self._skills_loader.update_auto_skill(
                name,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            if ok:
                publication.mark_committed()
        if ok:
            self._logger.info("Auto-refined skill %s from session %s", name, key)
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_refine",
                tool_kind="skills",
                outcome="invoked",
                metadata={"name": name},
            )
        else:
            # update_auto_skill returned False: oversized procedure,
            # file missing, or other internal rejection.  Audit it so
            # operators can trace why a refine was proposed but not
            # applied.
            self._logger.info("Auto-skill refine rejected for %s (update_failed)", name)
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_refine",
                tool_kind="skills",
                outcome="rejected",
                metadata={"name": name, "reason": "update_failed"},
            )
