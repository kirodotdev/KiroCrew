"""HTTP handlers for /side: ephemeral Q&A attached to a parent slot.

Sidecar buffer on ``slot._side``; isolated ``side:{slot.key}`` LLM session;
tool calls hard-rejected via ``REJECT_ALL``. Side messages never enter
``slot.messages`` or any persistent store.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.agent_discovery import warm_project_agent_names
from kiro_crew.config.loader import (
    KiroCrewConfig,
    refresh_materialized_agents,
    resolve_agent_bindings,
)
from kiro_crew.config.paths import CWD_CLEARED
from kiro_crew.dashboard.side_context import build_side_message
from kiro_crew.dashboard.side_state import (
    MAX_SIDE_QUEUE,
    STEER_CONSUMED,
    STEER_PENDING,
    STEER_REQUEUED,
    SideState,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.ws import broadcast_side_queue, broadcast_side_result
from kiro_crew.executors import subprocess_executor
from kiro_crew.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    stream_and_collect,
)
from kiro_crew.security import StreamRedactor, redact
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_QUESTION_BYTES = 32_768


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _side_session_key(slot_key: str) -> str:
    """Return the isolated ACP session key for a side turn."""
    return f"side:{slot_key}"


def _dispatch_side_turn(state: DashboardState, slot, question: str) -> str:
    """Start a side turn for *question* and return its run_id.

    Callers must have established that no side turn is in flight; there is no
    await between the caller's check and the state mutation here, so the
    single-threaded event loop makes the pair atomic.
    """
    side = slot._side
    run_id = uuid.uuid4().hex
    is_first_turn = not any(m.get("role") == "assistant" for m in side.messages)
    side.last_run_id = run_id
    side.is_complete = False
    side.append_user(question)

    broadcast_side_result(
        state,
        slot_key=slot.key,
        run_id=run_id,
        role="user",
        content=question,
    )

    task = asyncio.create_task(
        _run_side_turn(
            state,
            slot,
            run_id,
            question,
            is_first_turn=is_first_turn,
        )
    )
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return run_id


def _requeue_unconsumed_side_steers(
    state: DashboardState, slot, run_id: str, side_at_start: Any = None
) -> None:
    """Degrade steers the backend never confirmed into visible queue cards.

    A steer is a fire-and-forget write, so ``steer()`` returning True only proves
    the bytes left; the ``steering_consumed`` echo is the authoritative signal.
    Anything still pending when the turn ends reached no generation and would
    otherwise vanish having been reported as delivered.

    Requeued at the HEAD, preserving relative order — a steer was meant to be
    injected before anything the user queued after it. Each becomes an ordinary
    card, so a user who no longer wants it cancels it with one click.

    Gated on ``side_at_start`` identity as well as ``run_id``: the run id alone
    would let a superseded turn write onto a sidecar a close+reopen replaced.
    """
    side = slot._side
    if side is None or not side.open or side.last_run_id != run_id:
        return
    if side_at_start is not None and side is not side_at_start:
        return
    orphaned = side.steer_pending()
    if not orphaned:
        return
    for entry in orphaned:
        side.steer_mark(entry["id"], STEER_REQUEUED)
    for entry in reversed(orphaned):
        message = entry["text"]
        qid = side.queue_insert_front(message)
        if qid is None:
            # Acceptance reserves a slot per in-flight steer, so this should be
            # unreachable. Log loudly rather than broadcasting a null id: a
            # phantom card the user can neither cancel nor run is worse than a
            # recorded gap.
            logger.error(
                "Side steer requeue found no room: slot=%s depth=%d",
                slot.key,
                len(side.queue),
            )
            continue
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=message,
            depth=len(side.queue),
            front=True,
            # Name the steer this card came from. The card's id is brand new, so a
            # client holding the raw text of its own steer has no other way to
            # recognise it — and the content on the wire is redacted, which is a
            # permanently corrupted restore for a credential-bearing question.
            steer_id=entry["id"],
        )
    logger.info(
        "Requeued %d unconsumed side steer(s): slot=%s run_id=%s",
        len(orphaned),
        slot.key,
        run_id,
    )


def _drain_side_queue(state: DashboardState, slot, run_id: str) -> None:
    """Promote the oldest queued message to the next side turn.

    ``run_id`` is identity-checked against the sidecar's current run so a stale
    task from a closed-and-reopened side can never dispatch onto the new state.
    No-op when the side is closed, still busy, or the queue is empty.
    """
    side = slot._side
    if side is None or not side.open:
        return
    if side.last_run_id != run_id or not side.is_complete:
        return
    entry = side.queue_pop()
    if entry is None:
        return
    broadcast_side_queue(
        state,
        slot_key=slot.key,
        action="drain",
        queue_id=entry["id"],
        depth=len(side.queue),
    )
    try:
        _dispatch_side_turn(state, slot, entry["content"])
    except Exception:
        # The card is already retired on the client, so put the text back at the
        # head rather than only reporting the failure: "a submit is never
        # dropped" has to hold on this path too.
        logger.exception("Side queue drain failed: slot=%s", slot.key)
        # Room is guaranteed here: this path just popped the entry it is putting
        # back. Still handled, so a future change cannot silently broadcast null.
        qid = side.queue_insert_front(entry["content"])
        if qid is None:
            logger.error("Side re-insert found no room: slot=%s", slot.key)
            return
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=entry["content"],
            depth=len(side.queue),
            front=True,
        )
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=entry["id"],
            role="assistant",
            content="(queued side question could not be started — it is back in the queue)",
            is_error=True,
            final=True,
        )


async def _try_side_steer(state: DashboardState, slot, question: str) -> str | None:
    """Inject *question* into the side turn already streaming on this slot.

    Returns the ledger id the attempt was recorded under, or None when it was
    never attempted (no session, no steer support, no live turn) or the write
    failed — every one of which must fall through to the queue rather than drop
    the user's text.

    The entry is registered BEFORE the RPC suspends. ``steer()`` returning True
    only proves the bytes left; the ``steering_consumed`` echo is what proves
    injection. Registering first is what makes the ordering safe: if the turn's
    ``finally`` runs while this is suspended, it already sees the entry and
    requeues it. The caller then reads its OWN entry's state, so it never has to
    infer an outcome from an absence.
    """
    provider = state.sessions.get_provider(_side_session_key(slot.key))
    if provider is None or not getattr(provider, "supports_steer", False):
        return None
    steer = getattr(provider, "steer", None)
    if steer is None:
        return None
    # ``has_active_turn`` gates on a GENUINELY live prompt: the provider stays
    # registered through post-turn bookkeeping, and kiro-cli silently swallows a
    # steer for a prompt that already ended. Absent the probe, attempt the steer
    # and let its boolean decide.
    has_active = getattr(provider, "has_active_turn", None)
    if has_active is not None and not has_active():
        return None
    side = slot._side
    if side is None:
        return None
    if not side.steer_can_accept():
        # No room to requeue this steer if the turn ends without consuming it, and
        # a requeue that cannot land would drop a question already reported as
        # delivered. Decline here instead: the caller falls through to the queue,
        # which either takes it or answers 429 — both visible, both recoverable.
        return None
    steer_id = side.steer_register(question)
    if steer_id is None:
        # Every ledger slot is an in-flight steer. Fall through to the queue
        # rather than accept a steer whose outcome we could not record.
        return None
    try:
        delivered = bool(await steer(question))
    except Exception as exc:
        logger.warning("Side steer failed for slot %s: %s", slot.key, exc)
        delivered = False
    if not delivered:
        # The write did not land. If the entry is still pending it is ours to
        # discard; if the echo or the finally already resolved it, leave their
        # outcome alone and let the caller read it.
        if side.steer_state(steer_id) == STEER_PENDING:
            side.steers[:] = [e for e in side.steers if e["id"] != steer_id]
            return None
    return steer_id


async def _run_side_turn(
    state: DashboardState,
    slot,
    run_id: str,
    question: str,
    *,
    is_first_turn: bool,
) -> None:
    """Background task: drive one side turn and broadcast chunks over WS."""
    # Local import: chat_runner imports this package, so a module-level import
    # would close a cycle. Bound here rather than inside the try below because
    # the ``except`` clause has to name the exception.
    from kiro_crew.dashboard.chat_runner import (
        _AppAgentNotLoaded,
        _recover_app_agent_binding,
    )

    side_key = _side_session_key(slot.key)
    # The sidecar this turn belongs to. Every later mutation is gated on the slot
    # still pointing at THIS object, so a close+reopen part-way through can never
    # have the old turn settle or requeue onto the replacement's state.
    side_at_start = slot._side
    message = build_side_message(slot, question, is_first_turn=is_first_turn)
    chunks: list[str] = []
    # Rolling-buffer redactor for the live side stream. broadcast_side_result
    # already redacts each frame, but per-frame redaction alone misses a secret
    # split across streaming chunk boundaries; StreamRedactor withholds a
    # trailing credential-class run until it's confirmed safe. Mirrors
    # chat_runner._wsred so /side has the same protection as the main chat.
    _wsred = StreamRedactor()

    def _on_chunk(text: str) -> None:
        chunks.append(text)
        safe = _wsred.feed(text)
        if safe:
            broadcast_side_result(
                state,
                slot_key=slot.key,
                run_id=run_id,
                role="assistant",
                content=safe,
            )

    def _on_steer_consumed(snapshot: str) -> None:
        """Settle the steers this echo proves the backend injected.

        Bound to ``side_at_start``: an echo belongs to the turn that produced it,
        so a close+reopen mid-turn must not let it settle the REPLACEMENT
        sidecar's own pending steer — that steer would then never be requeued and
        the question would be lost silently.

        Whatever stays pending never reached a generation, and the finally below
        turns it back into a visible queue card.
        """
        if side_at_start is None or slot._side is not side_at_start:
            return
        # Proof of injection is the ONLY thing that may put a steer in the
        # transcript, so this callback is the single writer of the chip. The
        # submitting request cannot do it: when its RPC returns, consumption is
        # still unproven, and committing then duplicated any question the model
        # never consumed (the finally requeues it and the queue runs it for real).
        for entry in side_at_start.steer_settle(snapshot):
            side_at_start.append_user(entry["text"], steer=True)
            broadcast_side_result(
                state,
                slot_key=slot.key,
                run_id=side_at_start.last_run_id or "",
                role="user",
                content=entry["text"],
                steer=True,
            )

    provider = None
    acquired_key = ""
    auth_required = False
    try:
        # Resolve the KiroCrew slot agent name (e.g. "default") to the real
        # kiro-cli agent (e.g. "kirocrew") before creating the session. Passing
        # the raw slot name straight through to get_or_create -> create_session
        # -> set_mode fails with "Mode '<name>' not found" because there is no
        # matching ~/.kiro/agents/<name>.json. Mirrors chat_runner._run_chat and
        # chat_handlers, which resolve bindings for the same reason. Best-effort:
        # a config-load failure degrades to the raw slot.agent rather than
        # crashing the turn.
        kiro_agent: str | None = None
        # An app-owned slot whose agent never resolved (see the fail-loud guard
        # after this block). Captured inside the try so the raise lives OUTSIDE
        # it and is not swallowed by the resolve except.
        _app_agent_unresolved = False
        try:
            cfg = KiroCrewConfig.load()
            # Warm off-loop, resolve inline — same reasoning as the main chat path
            # (offloading the resolver itself would swallow a StopIteration into a
            # Future and hang the await).
            await warm_project_agent_names(slot.project, operation="side_panel", source="dashboard")
            bindings = resolve_agent_bindings(cfg, slot.agent or None, slot.project or None)
            kiro_agent = bindings.kiro_agent
            # SELF-HEAL an app-owned slot whose agent did not resolve, on the same
            # two escalating rungs `_run_chat` uses, for the same reason: an app's
            # agents live only in ``~/.kiro/agents/<app>--<agent>.json``, so the
            # resolver can honor them only through the materialized-agent snapshot,
            # which is COLD on the event loop until a warm lands. A cold read falls
            # back to the default agent with ``requested_resolved=False``.
            #
            # A side turn reaches this with its OWN session, so it cannot ride on
            # the main chat having healed first — a user can open the side panel on
            # an app slot that has never taken a turn. Guarded so the common path
            # (no ``_app``, or already resolved) does zero extra work and no I/O.
            if slot._app and not bindings.requested_resolved:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(), refresh_materialized_agents
                    )
                except Exception:  # noqa: BLE001 — a warm failure only costs the fail-loud
                    logger.warning(
                        "Side turn: failed to warm materialized agents for app slot %s",
                        slot.key,
                        exc_info=True,
                    )
                bindings = resolve_agent_bindings(cfg, slot.agent or None, slot.project or None)
                kiro_agent = bindings.kiro_agent
                if not bindings.requested_resolved:
                    bindings = await _recover_app_agent_binding(
                        cfg, slot, project=slot.project or None
                    )
                    kiro_agent = bindings.kiro_agent
            _app_agent_unresolved = bool(slot._app) and not bindings.requested_resolved
        except Exception:
            logger.warning(
                "Side turn: failed to resolve agent bindings for slot=%s; "
                "falling back to raw slot.agent",
                slot.key,
                exc_info=True,
            )

        # FAIL-LOUD, exactly as the main chat does: an app-owned slot whose agent
        # STILL did not resolve must NOT run the default agent. Substituting it
        # here is worse than the main chat's version of the same bug, because a
        # side answer carries no turn card the user would scrutinise — it reads as
        # this app's assistant answering, while actually being the generic default
        # with none of the app's MCP tools. Raised before get_or_create, so the
        # turn aborts without ever creating a session or dispatching an agent.
        if _app_agent_unresolved:
            raise _AppAgentNotLoaded(
                f"The app agent '{slot.agent or ''}' isn't loaded yet — "
                "try again in a moment, or restart the gateway."
            )

        provider, _is_new, _resumed = await state.sessions.get_or_create(
            side_key,
            agent=kiro_agent or slot.agent or None,
            # The session must run in the slot's project directory for a
            # project-scope agent resolved above to be loadable at all:
            # kiro-cli resolves --agent against $PWD/.kiro/agents, so creating
            # the side session without this cwd has set_mode reject the very
            # name resolve_agent_bindings just returned. Mirrors
            # chat_runner._run_chat, which passes the same cwd for the same
            # reason.
            cwd=slot.project or CWD_CLEARED,
        )
        acquired_key = side_key
        try:
            response_text = await stream_and_collect(
                provider,
                message,
                approval_policy=ToolApprovalPolicy.REJECT_ALL,
                on_chunk=_on_chunk,
                on_steer_consumed=_on_steer_consumed,
            )
            # Redact the assembled text before it is stored/broadcast as the
            # terminal frame (which replaces the streamed deltas). Never trust
            # LLM output on an external surface. redact() applies BOTH passes —
            # redact_exfiltration_urls() then redact_credentials() (security.py)
            # — so exfil URLs and credentials are both scrubbed here.
            response_text = redact(response_text)
        except PromptBusyExhaustedError:
            logger.warning(
                "Side turn aborted (prompt busy exhausted): slot=%s run_id=%s",
                slot.key,
                run_id,
            )
            response_text = redact("".join(chunks))
            if slot._side is not None and slot._side.open and slot._side.last_run_id == run_id:
                slot._side.append_assistant(response_text)
            broadcast_side_result(
                state,
                slot_key=slot.key,
                run_id=run_id,
                role="assistant",
                content="(side conversation interrupted — please retry)",
                is_error=True,
                final=True,
            )
            return

        if not chunks:
            response_text = (
                "Tool and MCP execution is intentionally unavailable in Side Chat. "
                "Ask in the main chat if you want me to use tools or take action."
            )
            logger.info(
                "Side turn produced no text (tool rejection): " "slot=%s run_id=%s",
                slot.key,
                run_id,
            )

        if slot._side is not None and slot._side.open and slot._side.last_run_id == run_id:
            slot._side.append_assistant(response_text)

        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content=response_text,
            ts=time.time(),
            final=True,
        )
    except asyncio.CancelledError:
        raise
    except AcpAuthRequired as exc:
        # A signed-out CLI is actionable, so surface its own message rather than
        # the generic failure below — the side panel has no other channel to tell
        # the user what to do. Latch the service signed-out too, so the
        # fail-closed gates stop trusting a stale ready value.
        logger.warning("Side turn auth required: slot=%s run_id=%s", slot.key, run_id)
        # Non-retryable: every queued question would hit the same wall. Draining
        # would spend the whole queue on identical failures and leave nothing to
        # resume after signing in, so the queue is held intact below. Mirrors the
        # main chat, which already makes this call in `_run_chat`.
        auth_required = True
        # Local import: chat_runner imports from this package, so a module-level
        # import would close a cycle.
        from kiro_crew.dashboard.chat_runner import _mark_kiro_signed_out

        _mark_kiro_signed_out(state)
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content=str(exc),
            is_error=True,
            final=True,
        )
    except _AppAgentNotLoaded as exc:
        # Terminal for this turn and deliberately not a generic failure: the
        # message names the agent and says what to do, which the catch-all below
        # would replace with "see server logs". No session was created, so there
        # is nothing to release. The queue is NOT held (unlike the auth arm
        # above): the self-heal rungs re-run per turn, so the next queued
        # question is a real retry rather than a repeat of a settled refusal.
        logger.warning(
            "Side turn: app agent not loaded for slot=%s run_id=%s: %s",
            slot.key,
            run_id,
            exc,
        )
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content=str(exc),
            is_error=True,
            final=True,
        )
    except Exception:
        logger.exception(
            "Side turn failed: slot=%s run_id=%s",
            slot.key,
            run_id,
        )
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content="(side conversation failed — see server logs)",
            is_error=True,
            final=True,
        )
    finally:
        # Identity-check run_id so a stale task from a closed-and-reopened
        # side never flips is_complete on the new state's in-flight turn.
        if slot._side is not None and slot._side.last_run_id == run_id:
            slot._side.is_complete = True
        if acquired_key:
            try:
                state.sessions.release(acquired_key)
            except Exception:
                logger.debug(
                    "Failed to release side session %s",
                    acquired_key,
                    exc_info=True,
                )
        # Release first, then drain: the next turn re-acquires the same isolated
        # session, so dispatching before the release would leave it held twice.
        _requeue_unconsumed_side_steers(state, slot, run_id, side_at_start)
        # Unconsumed steers are still requeued above — they were never delivered
        # either, and the head of the queue is where a resume should find them.
        # Only the DISPATCH is withheld, so nothing is lost, merely paused.
        if not auth_required:
            _drain_side_queue(state, slot, run_id)


def _check_slot_ownership(
    request: web.Request,
    slot,
    operation: str,
) -> web.Response | None:
    """Return 403 if the request app can't access ``slot``; mirrors ``api_chat``.

    App Kit §5.2: dashboard users (empty ``request_app``) can access everything.
    The auth gate is upstream in ``token_auth_middleware``; this is the
    app-vs-dashboard scope check, matching ``chat_handlers.py`` and ``chat_fork.py``.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None
    if not slot._app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="app cannot access unscoped slots",
        )
        return web.json_response(
            {"error": "not found"},
            status=404,
        )
    if slot._app != request_app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error="app does not own this slot",
        )
        # 404 (not 403) so a foreign/unscoped slot is indistinguishable from a
        # missing one — anti-enumeration (CWE-204); true reason logged via SEL.
        return web.json_response(
            {"error": "not found"},
            status=404,
        )
    return None


async def api_side_open(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/open — idempotent sidecar init."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    own = _check_slot_ownership(request, slot, "chat.side_open")
    if own is not None:
        return own

    if slot._side is None or not slot._side.open:
        slot._side = SideState(open=True, created_at=_now_iso())
        outcome = "opened"
    else:
        outcome = "reopened"

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_open",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},result={outcome}",
    )
    return web.json_response(
        {
            "ok": True,
            "open": True,
            "messages": len(slot._side.messages),
            "last_run_id": slot._side.last_run_id,
            "created_at": slot._side.created_at,
        }
    )


async def api_side_turn(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/turn — body ``{"question": str, "steer"?: bool}``.

    Three outcomes, all returning immediately:

    - idle → starts a turn, ``{ok, run_id}``; the stream runs as a background
      task and chunks are broadcast on ``chat.side_result``.
    - in flight + ``steer`` → injects into the RUNNING turn, ``{ok, steered}``.
    - in flight (or a steer that could not be delivered) → queues behind the
      turn, ``{ok, queued, queue_id, depth}``.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    own = _check_slot_ownership(request, slot, "chat.side_turn")
    if own is not None:
        return own

    if not request.body_exists:
        return web.json_response({"error": "missing JSON body"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object"},
            status=400,
        )

    question = body.get("question")
    if not isinstance(question, str):
        return web.json_response(
            {"error": "question must be a string"},
            status=400,
        )
    question = question.strip()
    if not question:
        return web.json_response(
            {"error": "question must not be empty"},
            status=400,
        )
    if len(question.encode("utf-8")) > _MAX_QUESTION_BYTES:
        return web.json_response(
            {"error": f"question too long (max {_MAX_QUESTION_BYTES} bytes)"},
            status=400,
        )

    # Check after body parse: ``api_side_close`` may have landed during
    # ``await request.json()``.
    if slot._side is None or not slot._side.open:
        return web.json_response(
            {"error": "side conversation is not open"},
            status=409,
        )

    if slot._side.last_run_id and not slot._side.is_complete:
        # A turn is in flight. Mirror the main chat: steer injects into the
        # RUNNING turn, queue defers it to the next one, and an unavailable
        # steer falls through to the queue so the text is NEVER dropped.
        #
        # Bind the commit to the sidecar OBJECT and run the steer was aimed at,
        # captured before the RPC suspends. Re-reading them afterwards would
        # attribute a steer to whatever is live now: a close+reopen swaps in a
        # fresh SideState that is also ``open``, and a completed turn's drain can
        # already have started the next run.
        side_before = slot._side
        run_before = side_before.last_run_id
        if body.get("steer"):
            steer_id = await _try_side_steer(state, slot, question)
            # Identity is checked on BOTH outcomes: a close+reopen during the RPC
            # leaves ``slot._side`` pointing at a fresh, also-``open`` sidecar, and
            # queueing this question onto it would put it in a side conversation
            # that never asked for it.
            if slot._side is not side_before or not side_before.open:
                return web.json_response(
                    {
                        "error": "side conversation is not open",
                        "code": "side_not_open",
                    },
                    status=409,
                )
            if steer_id is not None:
                ledger = side_before.steer_state(steer_id)
                # CONSUMED is the backend's proof of injection, and it outranks
                # turn completion: the question reached a generation and was
                # answered, so it belongs in the transcript even though the turn
                # has since ended. The echo callback has ALREADY appended and
                # broadcast it — this branch only reports the outcome, because two
                # writers would render the chip twice.
                if ledger == STEER_CONSUMED:
                    sel().log_api_access(
                        caller=request.get("app", "") or "dashboard",
                        operation="chat.side_turn",
                        outcome="allowed",
                        source="dashboard",
                        resources=(
                            f"slot={slot.key},run_id={run_before},"
                            f"mode=steer,ledger={ledger},"
                            f"question_len={len(question)}"
                        ),
                    )
                    return web.json_response(
                        {"ok": True, "steered": True, "run_id": run_before}
                    )
                if ledger == STEER_PENDING and (
                    side_before.last_run_id == run_before
                    and not side_before.is_complete
                ):
                    # Genuinely in flight, consumption unproven. Report that
                    # instead of claiming delivery: the outcome arrives as a frame
                    # — the chip if the echo proves it, a queue card if the turn's
                    # finally requeues it. The ledger holds the text either way.
                    #
                    # Pending but the run has MOVED ON is a different case and must
                    # keep falling through below: nobody is going to consume it, so
                    # it becomes a queue card now rather than waiting on a frame
                    # that will never come.
                    return web.json_response(
                        {
                            "ok": True,
                            "steered": False,
                            "pending": True,
                            "run_id": run_before,
                            # So the submitter can recognise the queue card this
                            # steer becomes if it is never consumed, and restore
                            # its own RAW text rather than the redacted broadcast.
                            "steer_id": steer_id,
                        }
                    )
                if ledger in (STEER_REQUEUED, None):
                    # REQUEUED: the turn's finally already made a card.
                    # None: the entry aged out of the ledger, so its outcome was
                    # decided by someone else. Either way a second entry here
                    # would ask the same question twice, which is the one
                    # direction worse than a duplicate-looking response.
                    return web.json_response(
                        {
                            "ok": True,
                            "queued": True,
                            "demoted": True,
                            "depth": len(side_before.queue),
                            # The card already exists — the turn's finally made it
                            # while the steer RPC was awaiting — so this response is
                            # the submitter's only chance to learn a handle for it.
                            # Its queue id is unknown here; the ledger id is what the
                            # requeue broadcast carries, so it is what correlates.
                            "steer_id": steer_id,
                        }
                    )
                # Still pending but the run moved on: nobody else has claimed it,
                # so take it and queue it below.
                side_before.steer_mark(steer_id, STEER_REQUEUED)

        side = slot._side
        if side is None or not side.open:
            return web.json_response(
                {"error": "side conversation is not open", "code": "side_not_open"},
                status=409,
            )
        qid = side.queue_append(question)
        if qid is None:
            sel().log_api_access(
                caller=request.get("app", "") or "dashboard",
                operation="chat.side_turn",
                outcome="denied",
                source="dashboard",
                resources=f"slot={slot.key},depth={len(side.queue)}",
                error="side queue full",
            )
            return web.json_response(
                {
                    "error": (
                        f"side queue is full (max {MAX_SIDE_QUEUE}) — "
                        "wait for the current turn or cancel a queued message"
                    ),
                    "code": "side_queue_full",
                    "depth": len(side.queue),
                },
                status=429,
            )
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=question,
            depth=len(side.queue),
        )
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="chat.side_turn",
            outcome="allowed",
            source="dashboard",
            resources=(
                f"slot={slot.key},mode=queue,queue_id={qid},"
                f"depth={len(side.queue)},question_len={len(question)}"
            ),
        )
        # The turn may have finished during the steer attempt above. The drain
        # hook only runs inside a turn's finally, so an entry queued after that
        # point would sit forever — kick the drain here.
        if side.is_complete:
            _drain_side_queue(state, slot, side.last_run_id)
        return web.json_response(
            {
                "ok": True,
                "queued": True,
                "queue_id": qid,
                # Whether the entry is STILL queued as of this response. A turn
                # that finished during the request can have drained it already,
                # and a client that materialised a card for it would show one the
                # server no longer has — cancelling that card 404s.
                "still_queued": any(e["id"] == qid for e in side.queue),
                "depth": len(side.queue),
                # Tell a user who pressed "Steer" that the turn ended under them,
                # so the queue card is not the only hint that the mode changed.
                **({"demoted": True} if body.get("steer") else {}),
            }
        )

    idle_side = slot._side
    if idle_side is not None and idle_side.queue:
        # Idle, but questions are already waiting — the auth hold leaves exactly
        # this state. Dispatching now would run the newest question first and leave
        # the earlier ones behind it, so join the tail and let the head go next.
        # This is also what restarts a held queue: the drain below moves it.
        qid = idle_side.queue_append(question)
        if qid is None:
            return web.json_response(
                {
                    "error": (
                        f"side queue is full (max {MAX_SIDE_QUEUE}) — "
                        "wait for the current turn or cancel a queued message"
                    ),
                    "code": "side_queue_full",
                    "depth": len(idle_side.queue),
                },
                status=429,
            )
        broadcast_side_queue(
            state,
            slot_key=slot.key,
            action="push",
            queue_id=qid,
            content=question,
            depth=len(idle_side.queue),
        )
        _drain_side_queue(state, slot, idle_side.last_run_id)
        return web.json_response(
            {
                "ok": True,
                "queued": True,
                "queue_id": qid,
                "still_queued": any(e["id"] == qid for e in idle_side.queue),
                "depth": len(idle_side.queue),
            }
        )

    run_id = _dispatch_side_turn(state, slot, question)

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_turn",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"slot={slot.key},run_id={run_id},"
            f"messages={len(slot._side.messages)},"
            f"question_len={len(question)}"
        ),
    )
    return web.json_response(
        {
            "ok": True,
            "run_id": run_id,
            "messages": len(slot._side.messages),
        }
    )


async def _read_side_queue_body(request: web.Request) -> str | web.Response:
    """Parse ``{"content": str}`` from a side-queue edit. Returns the trimmed
    content, or the error response to send."""
    if not request.body_exists:
        return web.json_response(
            {"error": "missing JSON body", "code": "missing_body"}, status=400
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body", "code": "invalid_body"}, status=400
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"},
            status=400,
        )
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"},
            status=400,
        )
    content = content.strip()
    if not content:
        return web.json_response(
            {"error": "content must not be empty", "code": "empty_content"},
            status=400,
        )
    if len(content.encode("utf-8")) > _MAX_QUESTION_BYTES:
        return web.json_response(
            {
                "error": f"content too long (max {_MAX_QUESTION_BYTES} bytes)",
                "code": "content_too_long",
            },
            status=400,
        )
    return content


def _resolve_side_queue_slot(
    request: web.Request, operation: str
) -> tuple[Any, web.Response | None]:
    """Shared lookup for the side-queue mutation endpoints.

    Returns ``(slot, None)`` on success or ``(None, response)`` to send.
    """
    state: DashboardState = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if not slot:
        return None, web.json_response(
            {"error": "not found", "code": "slot_not_found"}, status=404
        )
    own = _check_slot_ownership(request, slot, operation)
    if own is not None:
        return None, own
    if slot._side is None or not slot._side.open:
        return None, web.json_response(
            {"error": "side conversation is not open", "code": "side_not_open"},
            status=409,
        )
    return slot, None


async def api_side_queue_cancel(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/side/queue/{queue_id} — drop one entry.

    The frontend moves the cancelled text back into its composer, so the content
    is echoed back (redacted) rather than discarded silently.
    """
    state: DashboardState = request.app["state"]
    # Read the body FIRST: this is the only await in the handler, and everything after it
    # depends on `_side` still existing. A concurrent `/side/close` clears `_side`, so an await
    # placed after the entry is removed would raise with the entry already gone and no
    # broadcast sent — losing the question outright.
    #
    # The tab id lets other tabs drop the card without also pasting the question into their own
    # composer. Absent or malformed is fine: consumers then fall back to releasing.
    origin_client = ""
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("client"), str):
            origin_client = body["client"][:128]
    except Exception:
        origin_client = ""
    slot, err = _resolve_side_queue_slot(request, "chat.side_queue_cancel")
    if err is not None:
        return err
    # Bound to a local so that adding an await below could not silently reintroduce the crash
    # through `slot._side` being replaced or cleared underneath.
    side = slot._side
    queue_id = request.match_info["queue_id"]
    content = side.queue_remove(queue_id)
    if content is None:
        return web.json_response(
            {"error": "queue entry not found", "code": "queue_entry_not_found"},
            status=404,
        )
    broadcast_side_queue(
        state,
        slot_key=slot.key,
        action="cancel",
        queue_id=queue_id,
        content=content,
        depth=len(side.queue),
        origin_client=origin_client,
    )
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_queue_cancel",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},queue_id={queue_id},depth={len(slot._side.queue)}",
    )
    return web.json_response(
        {"ok": True, "content": redact(content), "depth": len(slot._side.queue)}
    )


async def api_side_queue_edit(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/side/queue/{queue_id} — body ``{"content"}``.

    Rewrites one queued entry in place; queue order is preserved.
    """
    state: DashboardState = request.app["state"]
    # Body first: `_side` must not be resolved across a suspension point, because a concurrent
    # `/side/close` clears it and every use below would raise.
    parsed = await _read_side_queue_body(request)
    if isinstance(parsed, web.Response):
        return parsed
    slot, err = _resolve_side_queue_slot(request, "chat.side_queue_edit")
    if err is not None:
        return err
    # Re-read after the body await: a close (or a drain) may have landed.
    side = slot._side
    if side is None or not side.open:
        return web.json_response(
            {"error": "side conversation is not open", "code": "side_not_open"},
            status=409,
        )
    queue_id = request.match_info["queue_id"]
    if not side.queue_edit(queue_id, parsed):
        return web.json_response(
            {"error": "queue entry not found", "code": "queue_entry_not_found"},
            status=404,
        )
    broadcast_side_queue(
        state,
        slot_key=slot.key,
        action="edit",
        queue_id=queue_id,
        content=parsed,
        depth=len(side.queue),
    )
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_queue_edit",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},queue_id={queue_id},content_len={len(parsed)}",
    )
    return web.json_response({"ok": True, "depth": len(side.queue)})


async def api_side_close(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/side/close — drop sidecar + destroy session."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    own = _check_slot_ownership(request, slot, "chat.side_close")
    if own is not None:
        return own

    was_open = slot._side is not None and slot._side.open
    slot._side = None

    side_key = _side_session_key(slot.key)
    try:
        await state.sessions.destroy(side_key)
    except Exception:
        logger.debug(
            "Failed to destroy side session %s",
            side_key,
            exc_info=True,
        )

    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="chat.side_close",
        outcome="allowed",
        source="dashboard",
        resources=f"slot={slot.key},was_open={was_open}",
    )
    return web.json_response({"ok": True, "was_open": was_open})
