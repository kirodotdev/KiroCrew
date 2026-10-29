"""The lesson routes: ``POST``, ``DELETE`` and ``GET /api/lessons``.

Each route resolves the lesson store its caller may reach, then writes, deletes or
pages the lessons there; the create path embeds the rule within a bounded wait
and hands the contradiction sweep to :mod:`.cron_lesson_supersede`.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.memory_sections import memory_writes_off_key
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers.cron_caller_session import _recognize_session
from kiro_crew.dashboard.handlers.cron_lesson_supersede import _resolve_and_supersede
from kiro_crew.dashboard.handlers.cron_shared import _sel
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import is_incognito_transcript
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ON_TOPIC,
    LESSON_APPLIES_UNSTATED,
    LESSON_APPLIES_VALUES,
    LESSON_REFUSED_AT_CAPACITY,
)
from kiro_crew.lesson_validation import authored_lesson_applies as _authored_lesson_applies
from kiro_crew.lesson_validation import (
    contains_volatile_lesson_fact,
)
from kiro_crew.project_scope import (
    canonical_scope,
    scope_is_admissible,
    scope_selector_is_inadmissible,
)
from kiro_crew.validation import (
    ALLOWED_LESSON_SCOPES,
    LEARN_ADD_SCHEMA,
    LESSON_LIST_LIMIT,
    LESSON_LIST_LIMIT_MAX,
    WORKSPACE_NAME_RE,
    ValidationError,
    clamp_query_offset,
    normalize_lesson_category,
    validate_tool_args,
)

from ._shared import (
    _blocks_reads_session,
    _get_active_workspace,
    _get_lessons,
    _get_memory,
    _is_restricted_session,
    _redact_memory_field,
    guard_owner_surface_routes,
    read_bounded_json,
    resolve_lesson_memory_store,
)

if TYPE_CHECKING:
    from kiro_crew.learn import Lesson, LessonStore

logger = logging.getLogger("kiro_crew.dashboard.handlers.cron")


# Budget for embedding a submitted lesson rule before it is written. The shared
# inference thread serves one job at a time and cannot preempt a running one, so
# a short rule can wait behind a long prompt embed for longer than the embedder's
# own default wait -- which equals the MCP client's read timeout
# (``mcp_core._post``), so the write was never reached. Well under that timeout,
# so an expired embed still leaves time to write the row with a NULL vector for
# the standing repair sweep to fill.
_LESSON_EMBED_DEADLINE_SECS = 5.0


def _embed_lesson_bounded(vs: Any, rule: str) -> list[float] | None:
    """Embed *rule* for a lesson write within ``_LESSON_EMBED_DEADLINE_SECS``.

    Synchronous; run it via ``asyncio.to_thread``. Returns ``None`` when the
    embed could not finish in time, the same value as any other unavailable
    embedding, so the caller writes the lesson without a vector.
    """
    from kiro_crew.embeddings import PRIORITY_NORMAL, EmbeddingWork, embedding_work

    inherited = embedding_work.get()
    deadline = time.monotonic() + _LESSON_EMBED_DEADLINE_SECS
    if inherited is not None:
        deadline = min(deadline, inherited.deadline)
    work = EmbeddingWork(
        deadline=deadline,
        cancelled=inherited.cancelled if inherited is not None else threading.Event(),
        priority=PRIORITY_NORMAL,
    )
    token = embedding_work.set(work)
    try:
        return vs.embed_lesson(rule)
    finally:
        embedding_work.reset(token)


def _candidate_applies(candidate: object) -> str:
    """The authored tier of a contradiction candidate, or ``unstated``.

    The candidate rows this sweep receives are shaped by
    ``find_contradiction_candidates``, so the tier may arrive already decoded or
    still inside ``value_json``. Both are read, and anything unreadable answers
    ``unstated`` -- the protected side, so a row this cannot classify is never
    deleted by a finding.
    """
    if not isinstance(candidate, dict):
        return LESSON_APPLIES_UNSTATED
    direct = candidate.get("applies")
    if isinstance(direct, str) and direct.strip().lower() in LESSON_APPLIES_VALUES:
        return direct.strip().lower()
    raw = candidate.get("value_json")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return LESSON_APPLIES_UNSTATED
        if isinstance(decoded, dict):
            nested = decoded.get("applies")
            if isinstance(nested, str) and nested.strip().lower() in LESSON_APPLIES_VALUES:
                return nested.strip().lower()
    return LESSON_APPLIES_UNSTATED


def _lesson_jsonl_store(
    state: DashboardState,
    silo: str,
    scope: str = "global",
    workspace: str | None = None,
    session_key: str = "",
) -> LessonStore:
    """Return only a V1 JSONL learning tier using the recorded store binding.

    Member V2 callers use their SQLite handle even when it contains no lessons.
    Workspace selection applies only to Global V1, preserving its existing
    global/workspace fallback and list union. A workspace-scoped write that
    names no workspace resolves to the REQUESTING session's workspace
    (*session_key*), never to another session's.
    """
    if silo:
        return ContextBuilder.get_lessons_for(memory_store=silo)
    if scope == "workspace":
        return _get_lessons(state, workspace, session_key=session_key)
    return state.lessons


def _load_workspace_lessons(state: DashboardState, workspace: str) -> list[Lesson]:
    """Every row in *workspace*'s JSONL lessons file."""
    return _get_lessons(state, workspace).load_all()


async def _prepare_member_lesson_store(store: str) -> web.Response | None:
    """Prepare V2 before synchronous lesson readers can borrow a handle."""
    # The member database is opened through the shim, so its errors are the shim's
    # classes; the stdlib ``sqlite3.Error`` would let them escape as a 500.
    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.memory_stores import memory_store_version

    try:
        if store and await asyncio.to_thread(memory_store_version, store) == 2:
            if await ContextBuilder.ensure_store(store) is None:
                raise ValueError("Member memory database is unavailable")
    except (ValueError, OSError, sqlite3.Error) as exc:
        return web.json_response({"error": str(exc), "code": "store_unavailable"}, status=503)
    return None


async def _surface_lesson_saved(
    request: web.Request, state: DashboardState, sk: str, rule: str, outcome: str
) -> None:
    """Draw a lesson the AGENT just saved in the conversation it saved it from.

    Only the MCP ``learn_add`` path qualifies: it is the one caller that posts with
    the internal secret, and a person pressing the Memory tab's Add (``dashboard:ui``)
    needs no note about their own click. A key that maps to no live slot -- a
    sub-agent, a cron or workflow turn, a channel thread -- has no chat to draw in,
    so it is skipped. Best effort: the write already happened and a failure here
    must never change the route's answer.
    """
    try:
        if request.get("internal_auth") is not True or not sk or sk == "dashboard:ui":
            return
        from kiro_crew.dashboard import chat_cards
        from kiro_crew.dashboard.session_control import caller_slot_key

        slot_key = caller_slot_key(state, sk)
        if not slot_key:
            return
        await chat_cards.load_conversation_window(state, {"slot_key": slot_key})
        chat_cards.record_lesson_saved(state, slot_key, rule, outcome)
    except Exception:
        logger.warning("could not draw a saved lesson in its conversation", exc_info=True)


async def api_lessons_create(request: web.Request) -> web.Response:
    """POST /api/lessons — add a lesson (vector store or JSONL fallback)."""
    from kiro_crew.learn import Lesson, lesson_contains_injection  # noqa: F811
    from kiro_crew.vector_memory_runtime.write_outcomes import SemanticRejectCode

    state: DashboardState = request.app["state"]
    # Default cap: lesson fields are short strings (MAX_SHORT_STRING-bounded).
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Block lesson writes from restricted (incognito/temporary/guest) sessions.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state,
        sk,
        "learn_add",
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        logger.warning("Blocked learn_add from restricted session %s", sk)
        _sel().log_api_access(
            caller=sk,
            operation="learn_add",
            outcome="denied",
            source="dashboard",
            resources="restricted_session_block",
            error="Memory writes are not allowed in this session mode.",
        )
        return web.json_response(
            {"error": "Memory writes are not allowed in this session mode."},
            status=403,
        )
    # Global persistence switch (memory.persistence_enabled) and the
    # writes-only switch (memory.writes_enabled).
    # Enforced on the route rather than in the learn_add MCP handler so every
    # transport that posts here (MCP tool, dashboard, direct HTTP) is covered
    # by the one check. Reads and deletions stay available — the right to
    # forget survives the switch.
    writes_off = memory_writes_off_key(KiroCrewConfig.load().memory)
    if writes_off is not None:
        code = (
            "persistence_disabled"
            if writes_off == "memory.persistence_enabled"
            else "writes_disabled"
        )
        _sel().log_api_access(
            caller=sk,
            operation="learn_add",
            outcome="denied",
            source="dashboard",
            resources=code,
            error=f"Persistent memory writes are disabled ({writes_off}).",
        )
        return web.json_response(
            {
                "error": "Lesson was NOT saved: persistent memory writes are disabled "
                f"({writes_off} is false). Re-enable them with "
                f"`kirocrew config set {writes_off} true` to save "
                "lessons again.",
                "code": code,
            },
            status=403,
        )
    # Validate body fields against the SAME schema the learn_add MCP tool uses
    # (LEARN_ADD_SCHEMA), so REST and tool paths share one source of truth:
    # rule must be a string (bounded to MAX_SHORT_STRING), category/scope are
    # enum-restricted, workspace is pattern-checked. A non-string
    # rule (array/dict) would otherwise raise AttributeError on .strip() -> HTTP 500, and
    # category/length would be unbounded. Only schema-known keys are validated so
    # unrelated body fields don't trip unknown-field rejection.
    known = {f.name for f in LEARN_ADD_SCHEMA.fields}
    try:
        cleaned = validate_tool_args(
            {k: v for k, v in body.items() if k in known}, LEARN_ADD_SCHEMA
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    rule = cleaned["rule"]
    if not rule:
        return web.json_response({"error": "rule is required"}, status=400)
    category = cleaned.get("category", "knowledge")
    scope = cleaned.get("scope", "global")
    # LEARN_ADD_SCHEMA accepts and validates ``negative``, but both write paths
    # below discarded it -- write_lesson got a literal None and the JSONL Lesson
    # omitted the kwarg -- so every NOT-clause sent to this route, from the
    # learn_add MCP tool, the dashboard, or the CLI, was silently lost.
    negative = cleaned.get("negative") or None
    # Restricts the lesson to one repository; absent means it applies everywhere.
    # Both write paths carry it, so the JSONL fallback store gates identically to
    # the vector store rather than injecting a scoped lesson the other withholds.
    repo_scope = cleaned.get("repo_scope") or None
    # Which startup tier this correction belongs to, as STATED by the caller (the
    # learn_add tool, the dashboard, the CLI). Absent leaves the row unstated,
    # which the context builder serves as a standing rule. Both write paths carry
    # it so the JSONL fallback tiers identically to the vector store.
    applies = cleaned.get("applies") or None
    # Write to vector store if available, else JSONL
    # THE CALLER'S silo, not the global store. This is the agent's only durable
    # memory-write surface, so writing globally let a crew bound to one silo steer
    # every other crew's turns -- and, in the other direction, the crew's own
    # context injects only its silo's lessons, so its correction never reached its
    # own later turns. Falls back to the global store when the session names none,
    # which is where every install wrote before silos existed.
    _lesson_silo, refusal = await resolve_lesson_memory_store(request, state, "lessons.create")
    if refusal is not None:
        return refusal
    memory_refusal = await _prepare_member_lesson_store(_lesson_silo)
    if memory_refusal is not None:
        return memory_refusal
    _lesson_mem = (
        await asyncio.to_thread(ContextBuilder.get_memory_for, memory_store=_lesson_silo)
        if _lesson_silo
        else _get_memory(state)
    )
    vs = _lesson_mem.vector_store
    if vs:
        # Embed the rule once off the event loop and reuse it for both the
        # contradiction scan and write_lesson's own dedup pass — the store
        # methods otherwise each perform a blocking in-process embed of the same
        # text. find_contradiction_candidates and write_lesson are synchronous
        # (blocking embed + O(N) cosine scan), so run them via to_thread to
        # avoid stalling concurrent dashboard/Slack requests.
        # Read the space generation BEFORE embedding: write_lesson cannot infer the
        # space of a vector computed out here, and a model swap landing between this
        # embed and the write would otherwise commit it into the wrong space.
        rule_emb_generation = vs.space_generation
        # Bounded: a saturated inference queue yields None here rather than holding
        # the request past the client timeout, and the lesson still lands below.
        rule_emb = await asyncio.to_thread(_embed_lesson_bounded, vs, rule)
        # Persist the lesson immediately so the request returns fast. The
        # contradiction sweep below makes a per-candidate LLM call (~27s each);
        # running it inline would exceed the MCP client's 30s timeout while the
        # write still completed server-side, so the caller would see a "timeout"
        # for a lesson that was actually saved (and re-saved on every retry).
        # Writing first, then sweeping in the background, keeps the slow LLM call
        # off the request path.
        result = await asyncio.to_thread(
            vs.write_lesson,
            rule,
            category,
            negative,
            "user_explicit",
            rule_emb,
            rule_emb_generation,
            repo_scope,
            applies=applies,
            # The embed above was attempted; a None from it must not trigger a
            # second, unbounded embed of the same text inside the write.
            rule_emb_resolved=True,
            # Stored lessons that still lack a vector are the standing repair
            # sweep's to fill. Embedding them here is PRIORITY_BULK work with no
            # deadline, queued behind every other bulk job, so it can hold this
            # request past the MCP client's read timeout (``mcp_core._post``) while
            # the write still lands later. Until the sweep fills such a row, both
            # semantic checks (dedup here, the contradiction scan below) pass it
            # over; the exact-rule, substring and keyword-overlap rules still apply
            # to it.
            defer_backfills=True,
        )
        # Sweep ONLY when the lesson actually landed. The write declines for a value
        # its preflight refuses (reachable because ``negative`` is forwarded here) and
        # for a dedup refusal. Discarding the result would let a refused write still
        # run the sweep below, where _resolve_and_supersede would delete_semantic an
        # older contradicted lesson whose "replacement" was never stored -- destroying
        # a lesson on a request that persisted nothing, under HTTP 200. Superseding on
        # the authority of a write that did not happen is wrong for every declining
        # outcome, so gate on ``wrote`` rather than on the cause.
        outcome = result.outcome.value
        reason = result.reason
        stored = result.stored
        # Redacted through the SAME chain this handler already applies to a lesson's
        # rule and category below (`_redact_memory_field`, which walks a list). A
        # superseded rule is stored user text leaving the process, so a credential or
        # an exfiltration URL that a user once put in a lesson must not be handed back
        # in a response -- and this path is worse than a read, because the row is being
        # deleted, so this response is the one place that text is echoed at all.
        # Redacting HERE covers both readers: the dashboard and the ``learn_add`` tool
        # each see only what this route sends.
        superseded = _redact_memory_field(list(result.superseded))
        # The stored lesson a ``deduped`` write lost to, redacted on the same chain
        # for the same reason: it is stored user text leaving the process. Naming it
        # lets a caller act on the winner directly instead of paging the list route.
        winner = _redact_memory_field(result.winner) if result.winner else None
        # The scope goes out as the ``DELETE /api/lessons`` selector that names the
        # winner's row: ``""`` for a global row, the fragment for a scoped one, and
        # ``None`` when the fragment is unusable or carries a credential shape, so a
        # stored scope never leaves this route unredacted.
        winner_repo_scope = (
            _lesson_scope_selector(result.winner_repo_scope) if result.winner else None
        )
        # Avoid even scanning or scheduling the model-based sweep for V2;
        # explicit corrections remain available through the owner review flow.
        # Skipped without a rule vector too: the scan would embed the rule again
        # on the embedder's default wait, behind the same full queue, and hold
        # this request past the client timeout. That lesson gets no sweep.
        if result.wrote and rule_emb is not None and getattr(vs, "algorithm_version", "v1") != "v2":
            candidates = await asyncio.to_thread(
                vs.find_contradiction_candidates, rule, 0.4, 0.85, rule_emb, repo_scope
            )
            # A second deletion route, and it ends in delete_semantic on a one-word
            # LLM verdict. The invariant is on the CANDIDATE, not on the submission:
            # a model-guessed contradiction may retire only a finding (on_topic). A
            # standing (always) candidate is always protected, and an unstated
            # candidate is protected too -- injection serves it AS a standing rule,
            # and on a store predating the field every row is unstated.
            #
            # So the filter is unconditional and reads the CANDIDATE's persisted tier
            # via _candidate_applies, which fails safe to unstated (protected). The
            # submission's tier is deliberately not consulted: a model verdict is not
            # authority to delete a standing rule the user filed, whichever tier the
            # submission carries. Gating on the submission (its old form,
            # `if result.applies == on_topic`) fired only for a finding submission,
            # so a standing or unstated submission skipped the guard and the sweep
            # could tombstone a standing candidate the model called contradictory --
            # with no recovery, since the "self-heals on the next learn_add" note
            # below covers a MISSED sweep, not a wrong deletion. This is stricter
            # than write_lesson's deterministic dedup scan, which may let a standing
            # submission retire a finding, because that scan decides on text while
            # this one decides on a guess.
            candidates = [
                candidate
                for candidate in candidates
                if _candidate_applies(candidate) == LESSON_APPLIES_ON_TOPIC
            ]
            if candidates:
                # Fire-and-forget via this module's _background_tasks
                # pattern. The sweep only supersedes OTHER (older) lessons, never
                # the one just written (self-match scores ~1.0, above the 0.85
                # candidate ceiling), so deferring it is safe. No retry/queue: a
                # missed sweep self-heals on the next learn_add touching the topic.
                task = asyncio.create_task(_resolve_and_supersede(state, sk, rule, candidates, vs))
                state._background_tasks.add(task)
                task.add_done_callback(state._background_tasks.discard)
    else:
        # A workspace-scoped write that names no workspace takes the requesting
        # session's. When that resolves to "default" -- no key, or a key with no
        # live slot -- the write would land in the GLOBAL file, which every
        # workspace reads. Refuse it, like the delete route refuses
        # scope='workspace' without a name, rather than widen its reach.
        if (
            not _lesson_silo
            and scope == "workspace"
            and not cleaned.get("workspace")
            and _get_active_workspace(state, sk) == "default"
        ):
            return web.json_response(
                {
                    "error": "scope='workspace' requires a workspace name: this "
                    "session has no workspace of its own; use scope='global' or "
                    "name the workspace",
                    "code": "workspace_required",
                },
                status=400,
            )
        lesson = Lesson(
            rule=rule,
            category=category,
            negative=negative,
            repo_scope=repo_scope,
            applies=applies,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        store = _lesson_jsonl_store(
            state, _lesson_silo, scope, cleaned.get("workspace"), session_key=sk
        )
        # save_or_enrich, not save: a re-submit of a stored rule carrying a new
        # NOT-clause has to attach it rather than be skipped as a duplicate.
        # Off the loop because it reads the file and rewrites it whole -- the
        # same reason dashboard/ws.py offloads load_all.
        #
        # Every arm of _lesson_jsonl_store shares the vector store's volatile-text
        # predicate and otherwise answers with its original three outcomes.
        # ``refused`` means neither field was persisted; this is the one content
        # refusal the fallback owns, and its string outcome matches
        # LessonWriteOutcome on the wire.
        outcome = await asyncio.to_thread(store.save_or_enrich, lesson)
        # This store has exactly THREE refusal paths -- the volatile-text
        # predicate, the blocked-content screen and the row cap -- and all answer
        # with the bare ``refused`` string, so the cause has to be re-derived here.
        # Re-running the predicates is exact rather than a guess: they are the same
        # pure-text calls the store made, and ``_lesson_withheld`` above re-derives
        # the first the same way for its own surface. Reporting every refusal as
        # ``volatile_session_fact`` told a user at the row cap to reword a rule
        # whose wording was never the problem. The blocked-content reason is the
        # vector store's own code, so both stores answer one text alike.
        reason = None
        if outcome == "refused":
            if contains_volatile_lesson_fact(rule, negative):
                reason = "volatile_session_fact"
            elif lesson_contains_injection(rule, negative):
                reason = SemanticRejectCode.INJECTION.value
            else:
                reason = LESSON_REFUSED_AT_CAPACITY
        stored = outcome != "refused"
        # A genuine empty, not an unfilled field. ``_insert_or_enrich`` has no dedup
        # rule that supersedes: it matches on exact rule text plus scope and either
        # attaches a clause or reports ``unchanged``, appending every other record
        # untouched -- so this store keeps both a general rule and the narrower rule
        # containing it, which the vector store does not. (It can still drop the
        # oldest record to the ``_MAX_LESSONS_TOTAL`` cap, but that is an eviction,
        # not this write superseding a rule it overlaps.)
        superseded = []
        # This store never answers ``deduped``, so there is no winner to name.
        winner = None
        winner_repo_scope = None
    # Refreshed unconditionally, and deliberately so. Gating the push on the
    # write having landed would be wrong: a DECLINING
    # outcome can still have mutated the store. ``write_lesson``'s second pass
    # DELETES a row it supersedes and keeps scanning, so with a containment chain
    # (A inside R inside B) whose rows are visited A-first -- and the scan order is
    # effectively random, since get_lessons orders by updated_at DESC, then by md5
    # key within one stamp -- A is removed and the call then returns ``deduped`` for
    # B. The store changed while ``wrote`` is False, so gating on it would leave
    # connected dashboards showing a lesson that is gone.
    # Reporting mutation separately would buy nothing over refreshing always: an extra
    # refresh on a no-op re-submit costs a redundant list fetch, a missed one shows
    # deleted data.
    state.push_refresh("lessons")
    # One chat row per write that changed the store; the recorder drops every
    # other outcome, and a caller with no chat of its own draws nothing.
    await _surface_lesson_saved(request, state, sk, rule, outcome)
    # ``ok`` answers the question the caller actually asked -- is the lesson I
    # submitted in the store -- so it stays true for a no-op re-submit (it is stored,
    # there was simply nothing to write) and turns false when a dedup rule or
    # validation kept it out. An unconditional true would tell the caller its
    # lesson was saved even when the store refused the value, and the ``learn_add``
    # tool and the CLI would both report "Saved" on that response.
    # ``outcome`` and ``reason`` are additive, so a client that only reads ``ok``
    # keeps working. ``superseded`` is additive for the same reason, and it is the
    # only channel that can carry the rules this write DELETED: they are tombstoned,
    # so a client that re-reads /api/lessons after this response cannot see what it
    # lost. Always present, empty when nothing was superseded, so a client does not
    # have to tell "no deletions" from "this gateway is too old to say".
    response: dict[str, Any] = {
        "ok": stored,
        "outcome": outcome,
        "reason": reason,
        "superseded": superseded,
    }
    # ``winner`` is additive too, and sent only on the outcome that has one: the
    # stored lesson a ``deduped`` write lost to, with that row's own scope.
    if winner:
        response["winner"] = winner
        response["winner_repo_scope"] = winner_repo_scope
    return web.json_response(response)


async def api_lessons_delete(request: web.Request) -> web.Response:
    """DELETE /api/lessons — remove lessons by substring."""
    state: DashboardState = request.app["state"]
    # Require the SAME session recognition as ``api_lessons_create``, via the
    # shared ``_recognize_session`` gate. Before this gate, deleting a lesson
    # was LESS protected than adding one: a key that create rejects with HTTP
    # 400 "unknown session" (forged, or a fresh background session whose
    # transcript hasn't flushed yet) could still substring-delete any durable
    # lesson. That asymmetry also breaks the remove-then-re-add consolidation
    # pattern non-atomically — the destructive remove succeeds, then the
    # re-add is refused, and the lesson is lost. Gating delete the same way
    # makes the pattern fail closed at step one.
    #
    # Every restricted mode forbids induced persistent writes.
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state,
        sk,
        "lessons.delete",
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        _sel().log_api_access(
            caller=sk,
            operation="lessons.delete",
            outcome="denied",
            source="dashboard",
            resources=sk,
        )
        return web.json_response(
            {"error": "Memory writes are not allowed in this session mode."}, status=403
        )
    # Default cap: the body is a rule substring plus scope/workspace selectors.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    rule_sub = body.get("rule", "").strip()
    if not rule_sub:
        return web.json_response({"error": "rule substring required"}, status=400)
    scope = body.get("scope", "global")
    # The JSONL tier selector pair. ``scope`` picks the file and ``workspace``
    # names it, so the two are validated together: a workspace-tier delete with
    # no name would fall through to the ACTIVE workspace's file (or the global
    # one) and remove a matching row the caller never pointed at, and a name
    # without the tier would be silently ignored. Both are refused with 400.
    # ``isinstance`` first: a non-string (a list, a dict) is unhashable, and the
    # membership test alone would turn a malformed body into a 500.
    if not isinstance(scope, str) or scope not in ALLOWED_LESSON_SCOPES:
        return web.json_response(
            {"error": "scope must be 'global' or 'workspace'", "code": "scope_not_allowed"},
            status=400,
        )
    workspace = body.get("workspace")
    if workspace is not None and (
        not isinstance(workspace, str) or not WORKSPACE_NAME_RE.match(workspace)
    ):
        return web.json_response(
            {"error": "workspace is not a valid workspace name", "code": "workspace_invalid"},
            status=400,
        )
    if scope == "workspace" and not workspace:
        return web.json_response(
            {
                "error": "scope='workspace' requires a workspace name",
                "code": "workspace_required",
            },
            status=400,
        )
    # "default" is the reserved name of the GLOBAL file: ``_get_lessons`` maps it
    # to ``state.lessons``, so accepting it under scope='workspace' would route a
    # workspace-tier delete onto the global rows.
    if scope == "workspace" and workspace == "default":
        return web.json_response(
            {
                "error": "'default' is the global lessons file; use scope='global'",
                "code": "workspace_reserved",
            },
            status=400,
        )
    # A name the configured workspace map does not hold is refused too:
    # ``workspace_dir_for`` resolves an unmapped name to the DEFAULT workspace
    # directory (logged, never raised), so a typo would land the delete on the
    # default workspace's lessons file rather than on an empty one of its own.
    if scope == "workspace":
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if workspace not in cfg.workspaces:
            return web.json_response(
                {
                    "error": f"workspace '{workspace}' is not configured",
                    "code": "workspace_unknown",
                },
                status=400,
            )
    if workspace and scope != "workspace":
        return web.json_response(
            {
                "error": "workspace is only meaningful with scope='workspace'",
                "code": "workspace_without_scope",
            },
            status=400,
        )
    # Optional repo_scope discriminator. A lesson's identity is the pair
    # ``(rule, repo_scope)``: a scoped row and a same-rule global row are two
    # distinct lessons, and this selector decides which of them the delete
    # reaches. This is a DIFFERENT axis from the legacy ``scope`` selector
    # above (global/workspace tier), so it is a distinct body field.
    #
    # Absent key -> not selective: scope stays out of the match and every
    # substring hit is removed, so an existing client is unaffected and no
    # stored row migrates. Present key -> selective, including an empty or
    # whitespace-only string, which targets the unscoped (global) rows.
    # ``sentinel`` distinguishes the two: ``body.get(..., sentinel)`` cannot
    # collapse a present empty string into "absent" the way ``or None`` would.
    #
    # A present value is refused unless it is a string: coercing a JSON null
    # to "" would silently turn "no selector" into "delete the global rows".
    # A nonempty selector the write surface would refuse (a bare "/", an
    # absolute path, a dot segment) is refused for the mirror reason -- no
    # admissibly stored row carries it, so canonical folding would land the
    # delete on rows the caller never named.
    _no_scope_key = object()
    _rs = body.get("repo_scope", _no_scope_key)
    if _rs is _no_scope_key:
        repo_scope = None
    elif not isinstance(_rs, str):
        return web.json_response(
            {"error": "repo_scope must be a string", "code": "repo_scope_not_string"},
            status=400,
        )
    elif scope_selector_is_inadmissible(_rs):
        return web.json_response(
            {
                "error": "repo_scope does not name a usable scope",
                "code": "repo_scope_inadmissible",
            },
            status=400,
        )
    else:
        repo_scope = _rs
    # Optional exact-match mode. The rule selector is a SUBSTRING by default --
    # the CLI and MCP callers target a lesson by a fragment -- so a caller that
    # holds the whole rule and means exactly that row (a table row's Delete
    # button) says so, or "use tabs" would also take "always use tabs". Only a
    # JSON boolean is accepted: a truthy string such as "false" must not turn
    # exact on, and a falsy one must not silently widen the delete.
    exact = body.get("exact", False)
    if not isinstance(exact, bool):
        return web.json_response(
            {"error": "exact must be a boolean", "code": "exact_not_bool"}, status=400
        )
    # Optional keep-one mode: delete only the extra copies of identical lessons
    # among the matches, keeping the oldest of each, so duplicates can be
    # collapsed without deleting the lesson and re-adding it. JSON boolean only,
    # for the same reason as ``exact``.
    keep_one = body.get("keep_one", False)
    if not isinstance(keep_one, bool):
        return web.json_response(
            {"error": "keep_one must be a boolean", "code": "keep_one_not_bool"}, status=400
        )
    # Delete from the vector store if active; fall through to JSONL (V1) below.
    # THE CALLER'S silo, not the global store. This is the agent's only durable
    # memory-write surface, so writing globally let a crew bound to one silo steer
    # every other crew's turns -- and, in the other direction, the crew's own
    # context injects only its silo's lessons, so its correction never reached its
    # own later turns. Falls back to the global store when the session names none,
    # which is where every install wrote before silos existed.
    _lesson_silo, refusal = await resolve_lesson_memory_store(request, state, "lessons.delete")
    if refusal is not None:
        return refusal
    memory_refusal = await _prepare_member_lesson_store(_lesson_silo)
    if memory_refusal is not None:
        return memory_refusal
    _lesson_mem = (
        await asyncio.to_thread(ContextBuilder.get_memory_for, memory_store=_lesson_silo)
        if _lesson_silo
        else _get_memory(state)
    )
    vs = _lesson_mem.vector_store
    vs_lessons = await asyncio.to_thread(vs.get_lessons) if vs else None
    # `vs and` rather than `vs_lessons` alone: the rows do not narrow the store,
    # and the store is a real union now that it is resolved per caller instead of
    # arriving untyped from the global getter.
    ok = False
    if vs and (vs_lessons or vs.algorithm_version == "v2"):
        ok = await asyncio.to_thread(
            vs.delete_lesson, rule_sub, repo_scope, exact=exact, keep_one=keep_one
        )
    # V1 only: when the vector store matched nothing, try the JSONL tier, as
    # ``kirocrew learn remove`` does. Injection falls back to that file when every
    # vector row is withheld, so a lesson active in every turn must stay
    # removable. A V2 member's database is its only lesson store, so it stops here.
    if not ok and not (vs is not None and vs.algorithm_version == "v2"):
        store = _lesson_jsonl_store(state, _lesson_silo, scope, workspace)
        # Off the loop. remove() now takes the store's shared lock, which a worker
        # thread can be holding across file I/O for a concurrent save_or_enrich --
        # so calling it inline would let one lessons write stall every task on the
        # event loop. Same reason api_lessons_create offloads its write.
        ok = await asyncio.to_thread(
            store.remove, rule_sub, repo_scope, exact=exact, keep_one=keep_one
        )
    if ok:
        state.push_refresh("lessons")
    return web.json_response({"ok": ok})


# ``LESSON_LIST_LIMIT`` / ``LESSON_LIST_LIMIT_MAX`` (the ``GET /api/lessons``
# window) are imported from ``validation.py`` beside the tool schema that
# advertises them. Both branches of the handler apply the window, so the
# vector-store and JSONL tiers cannot disagree about the bound.


def _lesson_list_page(query: Mapping[str, str]) -> tuple[int, int] | web.Response:
    """``(limit, offset)`` from the ``GET /api/lessons`` query, or the 400.

    ``limit`` is clamped into ``[1, LESSON_LIST_LIMIT_MAX]`` and ``offset`` by
    the shared ``clamp_query_offset`` rather than refused, because the body
    echoes both effective values back and the clamp is therefore never silent.
    The offset ceiling matters: the vector tier binds the offset as a SQLite
    parameter, and a value past the 64-bit range raises there instead of
    yielding an empty page. A non-integer is refused: the caller asked for a
    page it did not get, and defaulting would return the first page under a
    shape the caller cannot distinguish from the one it wanted.
    """
    try:
        limit = int(query.get("limit", str(LESSON_LIST_LIMIT)))
        offset = int(query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "limit/offset must be integers", "code": "invalid_pagination"}, status=400
        )
    return (max(1, min(limit, LESSON_LIST_LIMIT_MAX)), clamp_query_offset(offset))


def _lesson_scope_selector(stored: object) -> str | None:
    """The ``DELETE /api/lessons`` ``repo_scope`` selector that names ONE row.

    A lesson's identity is the pair ``(rule, repo_scope)``, so a list that
    omits the scope shows two same-rule rows in two scopes as indistinguishable
    duplicates -- and a delete sent without the selector removes both. This
    answers, per row, the selector the delete route defines:

    * ``""`` for an unscoped (global) row -- the route's explicit-global
      selector, so a delete of the global row leaves a same-rule scoped row.
    * the canonical fragment for a scoped row -- the delete compares
      canonically on both sides, so the folded form round-trips onto the row
      and only that row.
    * ``None`` for a row whose stored scope is PRESENT but unusable (an
      imported ``/``, a blank string, a non-string). Both stores classify such
      a row as scoped-but-broken and a scope-selective delete never claims it,
      while the route refuses the raw value as a selector -- so echoing it
      would make the row undeletable from the UI. ``None`` tells the client to
      send no selector, which is the unselective path both stores keep open so
      junk rows stay deletable.

    The classification is the stores' own: ``None`` is global (``learn.py``
    ``remove`` guards on ``is not None``; the vector store's
    ``_lesson_scope_unusable`` answers False for a null), and anything else is
    judged by :func:`scope_is_admissible`, the same predicate both stores use.

    The selector must round-trip byte-exact to name its row, so it cannot be
    rewritten -- but it is still a stored string leaving through this handler,
    and every such string goes through the shared redaction chain. A fragment
    the chain would alter carries a credential shape, and echoing it raw to
    make the row selectable is the one trade this surface must not make: it is
    withheld (``None``) instead, so the row reads as unusable and stays
    reachable only through the unselective delete, exactly like a broken
    scope. A fragment the chain leaves alone is emitted as-is.
    """
    if stored is None:
        return ""
    if not scope_is_admissible(stored):
        return None
    selector = canonical_scope(stored)
    if selector is None:
        return None
    # A control character (an escape sequence, a C0/C1 byte) inside the
    # fragment is refused outright: the redaction chain scans the text as
    # stored, so a credential split by an embedded escape would pass it whole
    # and be reassembled by any terminal or renderer that strips the controls.
    # Nothing a user names a repository by contains one, so withholding costs
    # no real row.
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in selector):
        return None
    if _redact_memory_field(selector) != selector:
        return None
    return selector


async def api_lessons(request: web.Request) -> web.Response:
    """GET /api/lessons — one bounded window of lessons, oldest-first, plus its size.

    Query: ``limit`` (default ``LESSON_LIST_LIMIT``, at most
    ``LESSON_LIST_LIMIT_MAX``) and ``offset`` (rows skipped from the NEWEST
    end, default 0). Body: ``lessons`` (the window), ``total`` (every live
    lesson in the store this caller is bound to), ``truncated`` (``True`` when
    the body does not carry every one of them), and the effective ``limit`` /
    ``offset``. The counts exist because this is the only lesson surface that
    ever omits rows without a reader that could notice: injection reports its
    omissions inline, the memory graph reads the population, the CLI is
    unbounded -- and ``learn_list`` renders this body verbatim, so a store past
    the cap showed the model a subset with nothing to say so. ``learn_add``'s
    ``deduped`` outcome sends the model here to find the stored wording it
    lost to, and an older dedup winner sits exactly outside the newest window.

    The vector tier selects its newest rows, so ``offset`` walks back in time.
    The JSONL tier appends workspace rows after global rows before taking the
    window, so a workspace union is not strictly the newest rows across both
    stores.
    """
    state: DashboardState = request.app["state"]
    # Parsed before any store is resolved: a 400 should not have paid for a
    # silo's first ``init()``.
    page = _lesson_list_page(request.query)
    if isinstance(page, web.Response):
        return page
    limit, offset = page

    def _page_body(data: list[dict], total: int) -> web.Response:
        return web.json_response(
            {
                "lessons": data,
                "total": total,
                "truncated": len(data) < total,
                "limit": limit,
                "offset": offset,
            }
        )

    # Block lesson reads only for temporary sessions (blocks_reads=True).
    # Incognito sessions can read lessons (memory context is already injected).
    if _blocks_reads_session(state, request):
        sk = request.headers.get("X-Session-Key", "")
        _sel().log_api_access(
            caller=sk,
            operation="lessons.list",
            outcome="denied",
            source="dashboard",
            resources=sk,
        )
        return _page_body([], 0)
    workspace = request.query.get("workspace")

    def _safe_lesson(
        rule: object,
        category: object,
        ts: object,
        negative: object = None,
        repo_scope: object = None,
        *,
        tier: tuple[str, str | None] | None = None,
        applies: object = None,
        source: object = None,
    ) -> dict:
        """One sanitization chokepoint for every branch of this endpoint.

        *source* is the row's write origin (``user_explicit`` for a lesson saved
        on purpose through ``learn_add``, the dashboard or the CLI,
        ``consolidation`` for one extracted from chat history, and so on). It is
        reported only when the store recorded one, so a reader can tell a lesson
        saved on purpose from one the extractor inferred.

        *tier* names the JSONL file a row was read from -- ``("global", None)``
        or ``("workspace", <name>)`` -- and is emitted as the ``scope`` /
        ``workspace`` selectors ``DELETE /api/lessons`` uses to pick that file.
        The JSONL list is a UNION of the global file and the active workspace's,
        while the delete defaults to the global file, so a workspace row deleted
        without its tier would leave the row and remove a same-text global one
        instead. Vector rows pass no tier: the delete reaches the vector store
        whatever ``scope`` says, so there is nothing to select.

        Lesson rows can carry consolidation (LLM) or import output: normalize
        the category through the shared helper (display policy, strict=False)
        so this surface cannot drift from the write-path rules, and redact
        BOTH prose fields via the shared chain like every other agent-derived
        string this handler returns -- an imported row can carry a credential
        in either field. The JSONL store loads ``rule`` without type
        validation, so a malformed row can carry a non-string here; stringify
        before the redaction rather than crashing the endpoint.
        """
        if not isinstance(rule, str):
            rule = str(rule)
        normalized_category = normalize_lesson_category(category, strict=False)
        safe_rule = _redact_memory_field(rule)
        safe_category = _redact_memory_field(normalized_category)
        result = {
            "rule": safe_rule,
            "category": safe_category,
            "ts": ts,
            "repo_scope": _lesson_scope_selector(repo_scope),
        }
        if tier is not None:
            result["scope"] = tier[0]
            if tier[1] is not None:
                result["workspace"] = tier[1]
        # Only an AUTHORED tier is reported. A row nobody tiered is served as a
        # standing rule, so emitting a value for it would name a distinction the
        # injection path does not make, and the key's absence is what says "this
        # row carries no author's answer". This is the surface every overflow
        # notice sends the reader to, so a row filed as a finding has to be
        # visible HERE -- without it a rule misfiled as on_topic silently stops
        # arriving and the listing that is supposed to explain it shows nothing.
        authored_applies = _authored_lesson_applies(applies)
        if authored_applies is not None:
            result["applies"] = authored_applies
        if isinstance(source, str) and source.strip():
            result["source"] = _redact_memory_field(source.strip())[:64]
        if contains_volatile_lesson_fact(rule, negative):
            result["withheld_reason"] = "volatile_session_fact"
        return result

    # Read from vector store if it has lessons, else JSONL
    # THE CALLER'S silo, not the global store. This is the agent's only durable
    # memory-write surface, so writing globally let a crew bound to one silo steer
    # every other crew's turns -- and, in the other direction, the crew's own
    # context injects only its silo's lessons, so its correction never reached its
    # own later turns. Falls back to the global store when the session names none,
    # which is where every install wrote before silos existed.
    _lesson_silo, refusal = await resolve_lesson_memory_store(request, state, "lessons.list")
    if refusal is not None:
        return refusal
    memory_refusal = await _prepare_member_lesson_store(_lesson_silo)
    if memory_refusal is not None:
        return memory_refusal
    _lesson_mem = (
        await asyncio.to_thread(ContextBuilder.get_memory_for, memory_store=_lesson_silo)
        if _lesson_silo
        else _get_memory(state)
    )
    vs = _lesson_mem.vector_store
    # Bounded in SQL rather than sliced afterwards. ``get_lessons()`` orders
    # ``updated_at DESC``, so the tail-slice idiom the JSONL branch below uses --
    # correct there, because ``load_all()`` returns file append order -- selected
    # the OLDEST rows here and hid every recent lesson: a lesson saved through
    # ``learn_add`` was absent from the very next ``learn_list``, which reads as a
    # silently failed write. Passing the window to the store keeps the ordering
    # and the bound in one place and stops the read from materializing every
    # lesson row (embedding blobs included) to discard all but one page.
    vs_lessons: list[dict] = await asyncio.to_thread(vs.get_lessons, limit, offset) if vs else []
    # ``count_lessons()`` is the raw row count; it sizes ``total`` and nothing
    # else. The tier fallback below is keyed on the vector POPULATION, not on
    # the page, so an empty page past the end of a populated vector store still
    # answers from the vector tier -- with its true total -- and never falls
    # through to the JSONL file, whose rows are a different (superseded) tier.
    vs_total = await asyncio.to_thread(vs.count_lessons) if vs else 0
    if vs_lessons:
        # Deferred import: ``vector_memory`` pulls snowballstemmer plus the
        # optional numpy/faiss imports, and this helper is the handler's only
        # use of it, on one dashboard read path.
        from kiro_crew.vector_memory import _lesson_display_text, _lesson_fields_for_row

    data: list[dict] = []
    # Oldest-first, so both branches of this endpoint answer in the same
    # order. Consumers rely on it: the Memory tab takes its recent rows from
    # the TAIL of this list, so a newest-first response would show the oldest
    # of the capped window there.
    for e in reversed(vs_lessons):
        try:
            decoded = json.loads(e["value_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        # Rendered text for either storage shape: mapping-shaped rows
        # (write_lesson's format and the onboarding import's) would otherwise
        # ship a nested object where the dashboard expects a string. A row with
        # no lesson shape falls back to str() rather than being dropped, so it
        # stays listed and therefore deletable -- delete_lesson needs a
        # substring, and this list is the only surface that can show it. The
        # memory graph applies the same policy for the same reason.
        rule = _lesson_display_text(decoded) or str(decoded)
        fields = _lesson_fields_for_row(decoded, e["key"])
        negative = fields[1] if fields is not None else None
        raw_category = decoded.get("category") if isinstance(decoded, dict) else None
        # The RAW stored value, key-present or not: a legacy string row has
        # nowhere to carry a scope and reads as global, exactly as the
        # store's own ``_lesson_scope`` / ``_lesson_scope_unusable`` read it.
        raw_scope = decoded.get("repo_scope") if isinstance(decoded, dict) else None
        raw_applies = decoded.get("applies") if isinstance(decoded, dict) else None
        data.append(
            _safe_lesson(
                rule,
                raw_category,
                e.get("updated_at", ""),
                negative,
                raw_scope,
                applies=raw_applies,
                source=e.get("source"),
            )
        )
    # The population is measured by the rows THIS list renders: everything
    # that decodes (legacy strings and rule-less mappings included, marked
    # withheld), which is what ``has_any_decodable_lesson()`` asks. A page with
    # a rendered row settles it without a scan; a page with none (past the
    # end, or every row on it undecodable) asks the store, so a vector store
    # holding only rows an import or legacy migration left undecodable does
    # not silence the JSONL file the caller's valid corrections still live in.
    vs_populated = bool(data) or (
        vs is not None and vs_total > 0 and await asyncio.to_thread(vs.has_any_decodable_lesson)
    )
    # A member's SQLite database remains its only learned authority even
    # when empty or undecodable; only V1 has a JSONL fallback tier.
    if (vs is not None and vs.algorithm_version == "v2") or vs_populated:
        total = vs_total
    else:
        # The JSONL tier of the store this caller is BOUND to, which for a silo is its
        # own file and never the operator's -- an empty silo answers "no lessons", not
        # "here are the global ones". A silo also takes no workspace union: the two are
        # separate namespaces, so another target's rows are not this store's to show.
        rows = await asyncio.to_thread(lambda: _lesson_jsonl_store(state, _lesson_silo).load_all())
        # Each row keeps the tier it came from, so its delete can be sent back
        # to the same file (see ``_safe_lesson``).
        tiered: list[tuple[Lesson, tuple[str, str | None]]] = [
            (le, ("global", None)) for le in rows
        ]
        if not _lesson_silo:
            # Merge global + workspace-scoped lessons. A named workspace wins; a
            # session sees only its own slot's workspace. The all-workspaces
            # union -- every configured workspace's rows, so each one stays
            # visible and deletable from the operator's Memory tab -- is for the
            # operator's own BROWSER surface ONLY: no key (or the shared
            # ``dashboard:ui`` key, which names no slot) AND positively
            # authenticated as the dashboard user.
            #
            # ``is_dashboard_user`` is set by the token-auth middleware to
            # ``not app`` on every path that reaches a mixed-internal route
            # like this one, so it is ``True`` only for a pure cookie/session
            # browser caller (the operator) and ``False`` for an app token.
            # Gating on it (rather than on ``internal_auth is not True``) means
            # a keyless APP TOKEN -- which carries no ``internal_auth`` but is
            # not the operator either -- falls through to the per-session branch
            # and, with no resolvable slot, sees the global rows only. A keyless
            # internal-secret agent (``internal_auth`` set) likewise never
            # reaches the union. That keeps workspace data isolated: only the
            # operator's own browser sees every workspace.
            sk = request.headers.get("X-Session-Key", "")
            if workspace:
                union = [workspace]
            elif sk in ("", "dashboard:ui") and request.get("is_dashboard_user") is True:
                cfg = await asyncio.to_thread(KiroCrewConfig.load)
                union = sorted(cfg.workspaces)
            else:
                union = [_get_active_workspace(state, sk)]
            for ws in union:
                if ws == "default":
                    continue
                # Every workspace row is listed, a same-text global row
                # notwithstanding: the tier fields tell the two apart, and a row
                # this list hides is a row the UI can never delete.
                ws_lessons = await asyncio.to_thread(_load_workspace_lessons, state, ws)
                tiered.extend((le, ("workspace", ws)) for le in ws_lessons)
        total = len(tiered)
        # ``load_all()`` is file append order, so the newest rows are at the
        # TAIL: the window ends ``offset`` rows before it, mirroring the vector
        # tier where ``offset`` also counts back from the newest row.
        end = max(0, total - offset)
        data = [
            _safe_lesson(
                le.rule,
                le.category,
                le.ts,
                le.negative,
                le.repo_scope,
                tier=tier,
                # ``getattr``, not ``le.applies``: this branch renders whatever
                # the JSONL loader produced, and a row from an older file (or a
                # caller passing a lighter row shape) carries no tier attribute at
                # all. Absent reads the same as unstated, which is the tier every
                # row written before the field existed is in -- the same policy
                # the rule/scope reads above already apply to a malformed row.
                applies=getattr(le, "applies", None),
                source=getattr(le, "source", None),
            )
            for le, tier in tiered[max(0, end - limit) : end]
        ]
    return _page_body(data, total)


# Every other api_* handler in this module is an owner surface, so a private
# member's internal call is refused before it runs. These verify and scope
# their own caller instead.
guard_owner_surface_routes(
    globals(), member_scoped=frozenset({"api_lessons", "api_lessons_create", "api_lessons_delete"})
)
