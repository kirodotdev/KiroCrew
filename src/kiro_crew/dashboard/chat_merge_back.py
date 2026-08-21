"""Merge a forked session back into its parent.

The mirror image of :mod:`chat_fork`: where a fork copies a slice of a parent's
transcript into a fresh slot, a merge-back takes what a fork *added* and folds a
summary of it into the parent, then archives the fork so it can no longer be
continued. The maintainer rulings this implements (issue #3816):

* The result is a **visible marked block** appended to the parent — a distinct
  ``merged_summary`` message the parent's transcript renders as its own card,
  not a silent metadata edit.
* The block carries a **summary only**, produced by the same on-demand
  summarizer the panel uses (:func:`~kiro_crew.dashboard.chat_summary.generate_session_summary`).
  There is no raw-message copy mode.
* The fork is **archived** after the merge (marked ``merged`` + read-only), so
  it stays readable from History but can neither be continued nor merged again.
* The block is **appended at the parent's tail**, with a gap note when the
  parent advanced past the fork point since the fork was taken.

The summarizer is whole-transcript, not range-scoped (see the module-level note
on :func:`_post_fork_range`): for a tail-fork the fork's transcript already IS
the post-fork content, so the summary is exact; for a head-fork it also covers
the copied parent prefix, and the block says so. The longest-common-prefix scan
is used only to populate the human message count and the gap note, never to
re-scope the model call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.channel_slots import note_slot_closed
from kiro_crew.dashboard.chat_handlers import _subagents_attached_response
from kiro_crew.dashboard.chat_persistence import (
    rehydrate_slot_from_history_async,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_summary import generate_session_summary
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger(__name__)

# The role/cls the appended block carries. A dedicated role (rather than a
# tagged ``system`` row) is what lets the frontend route it to its own renderer
# and keep it always-visible; ``merged-summary`` in the cls mirrors the
# compaction notice's ``kind`` marker so a history reload re-derives the card.
_MERGED_ROLE = "merged_summary"
_MERGED_CLS = "msg msg-a merged-summary"


def _iso_now() -> str:
    """UTC timestamp in the same ISO shape the append path stamps rows with."""
    return datetime.now(tz=timezone.utc).isoformat()


def _visible(messages: list[dict]) -> list[dict]:
    """The user/assistant rows of a transcript — the only ones a fork copies.

    Mirrors :mod:`chat_fork`'s ``visible`` filter exactly, so the longest-common
    -prefix scan below compares like against like: the fork's stored transcript
    holds only these roles for the copied prefix, so anchoring the fork point on
    anything else would never line up.
    """
    return [m for m in messages if m.get("role") in ("user", "assistant")]


def _msg_identity(m: dict) -> tuple:
    """A comparison key for fork-point detection, preferring the per-row ``mid``.

    The ``mid`` is the row's delivery identity: ``_ChatSlot.append`` mints one
    per row and :func:`carry_provenance`/the copy loop preserve the parent's
    values into the fork, so two rows with the same ``mid`` ARE the same message
    (Risk 4). It is the stronger key because ``(role, content)`` alone mis-scans
    when a parent has duplicate consecutive rows. Fall back to ``(role,
    content)`` for a row minted before ``mid`` existed, or one whose ``mid`` did
    not survive an older restore.
    """
    mid = (m.get("meta") or {}).get("mid") if isinstance(m.get("meta"), dict) else None
    if isinstance(mid, str) and mid:
        return ("mid", mid)
    return ("rc", m.get("role", ""), m.get("content", ""))


def _common_prefix_len(parent: list[dict], fork: list[dict]) -> int:
    """How many leading messages the fork shares with the parent.

    The fork point: a head-fork copies the parent's prefix verbatim, so the
    shared run is exactly what the fork inherited and everything after it in the
    fork is post-fork work. A tail-fork shares nothing (its transcript is only
    the divergent tail), so this returns 0 and the whole fork counts as
    post-fork — which is correct.
    """
    n = 0
    for pm, fm in zip(parent, fork):
        if _msg_identity(pm) == _msg_identity(fm):
            n += 1
        else:
            break
    return n


def _post_fork_range(parent_visible: list[dict], fork_visible: list[dict]) -> tuple[int, int]:
    """The ``[start, end)`` span of *fork_visible* that is post-fork work.

    ``start`` is the fork point (the shared-prefix length); ``end`` is the
    fork's visible length. Used ONLY to populate the block's human message count
    and the gap note — NOT to re-scope the summarizer, which has no range
    parameter and summarizes the whole fork transcript. That whole-transcript
    behaviour is the accepted v1 (Risk 1): exact for a tail-fork, and labelled
    "covers the full fork session" for a head-fork whose prefix the summary also
    describes.
    """
    start = _common_prefix_len(parent_visible, fork_visible)
    return start, len(fork_visible)


# Versioned domain separator for the merge identity. Bump the version if the
# canonical snapshot shape below ever changes, so old receipts cannot be
# misread as covering a snapshot they never hashed.
_MERGE_KEY_DOMAIN = "merge-back:v1"


def _source_sig(fork_visible: list[dict]) -> str:
    """SHA-256 over the canonical ordered fork snapshot.

    The merge's identity is WHAT WAS SUMMARIZED, never where the block landed
    (GPT review, restructure round: positional identity — count, range end —
    was broken three different ways across three rounds). The canonical form
    is each visible row's identity key (mid-preferred, the same key the
    fork-point scan uses), role and content, in order. Timestamps and delivery
    metadata are excluded: they churn on rewrite without changing what a
    summary would say. A same-length rewrite changes content, so it changes
    this signature; duplicated content in a different order changes the
    sequence, so it changes this signature too.
    """
    canonical = [
        [list(_msg_identity(m)), m.get("role", ""), m.get("content", "")] for m in fork_visible
    ]
    blob = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _merge_key(fork_session_key: str, source_sig: str) -> str:
    """The merge's durable identity: this fork, at exactly this snapshot."""
    blob = f"{_MERGE_KEY_DOMAIN}\0{fork_session_key}\0{source_sig}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _render_summary_markdown(payload: dict) -> str:
    """Render an intent-summary payload to the markdown the block displays.

    Reads the same payload shape :func:`~kiro_crew.history.ConversationLog.read_intent_summary`
    returns — a list of ``intents`` (each a goal with a status and progress) plus
    session-scoped ``constraints``. Rendered as headed bullet sections rather
    than raw JSON so the parent's reader sees a runbook, not a data structure.
    Every field is redacted on the way in: the payload is model-derived text
    being folded into a different session's persisted transcript, an egress
    boundary the summarizer itself does not cross.
    """

    def _clean(text: str) -> str:
        text, _ = redact_exfiltration_urls(str(text))
        text, _ = redact_credentials(text)
        return text.strip()

    lines: list[str] = []
    for intent in payload.get("intents", []):
        if not isinstance(intent, dict):
            continue
        title = _clean(intent.get("title", "")) or "Untitled goal"
        status = _clean(intent.get("status", ""))
        header = f"**{title}**"
        if status:
            header += f" — _{status}_"
        lines.append(header)
        for step in intent.get("progress", []) or []:
            step_text = _clean(step)
            if step_text:
                lines.append(f"- {step_text}")
        lines.append("")
    constraints = [c for c in (_clean(c) for c in payload.get("constraints", []) or []) if c]
    if constraints:
        lines.append("**Constraints**")
        for c in constraints:
            lines.append(f"- {c}")
    return "\n".join(lines).strip()


def _resolve_parent(state: DashboardState, fork_session_key: str) -> "_ChatSlot | None":
    """The open parent slot for *fork_session_key*, or ``None`` if none is open.

    ``forked_from`` stores :func:`effective_session_key` of the parent, which is
    ``dashboard:<name>`` for a dashboard parent but the channel's own key
    (``slack:<ts>``) for a channel-born one. Rather than strip a ``dashboard:``
    prefix that a channel parent never had (Risk 5), scan the open slots for the
    one whose ``effective_session_key`` matches — the same handles-both-flavours
    approach :func:`~kiro_crew.dashboard.chat_utils.slack_options_slot` uses.
    Returns the live slot when a tab is open; the caller rehydrates from disk
    when this answers ``None``.
    """
    for candidate in state._slots.values():
        if effective_session_key(candidate) == fork_session_key:
            return candidate
    return None


async def api_chat_slot_merge_back(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/merge-back — fold a fork's summary into its parent.

    *slot* is the FORK. Resolves its parent from ``forked_from``, summarizes the
    fork with the on-demand summarizer, appends a visible ``merged_summary``
    block to the parent at its tail (with a gap note when the parent advanced),
    archives the fork (``merged`` + read-only), and returns the parent key.

    Body: ``{}`` (summary only — there is no raw-copy option).

    Responses:
      - 200 ``{ok, parent_key}``
      - 409 ``not_a_fork`` — the slot was not created by a fork
      - 409 ``already_merged`` — the fork has already been merged back
      - 409 ``summary_unavailable`` — the summarizer produced nothing, or the
        stored summary is stale against newer persisted turns
      - 409 ``summary_turn_running`` — the fork has a turn in flight
      - 409 ``parent_busy`` — the parent has a turn in flight, queued work, or
        is itself reserved by another merge; retry when it clears (checked
        before the summarization spend)
      - 409 ``nothing_to_merge`` — the fork's visible transcript is entirely
        the copied parent prefix; there is no post-fork work to fold back
      - 503 ``fork_flush_failed`` — the fork's dirty tail could not be
        persisted before snapshotting; retry
      - 404 ``parent_missing`` — the parent session no longer exists
      - 404 ``not found`` — the fork slot is unknown / owned by another app
      - 400 — the fork is a non-persistent session
      - 503 — the parent block could not be persisted; retry re-runs the merge
      - 503 ``archive_failed`` — the block IS merged but the fork could not be
        archived; retry re-runs only the archive step
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)

    # App ownership check (App Kit §5.2), identical to the fork endpoint: an
    # app-scoped caller may only act on a slot its own app owns, and a slot it
    # does not own answers 404 (not 403) so the isolation boundary cannot be
    # enumerated (CWE-204). The true reason is recorded server-side via SEL.
    if request_app:
        if not slot._app or slot._app != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.slot_merge_back",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app does not own this slot",
            )
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

    if not slot.forked_from:
        # Merge-back is only meaningful for a session that HAS a parent to merge
        # into. A root session has nowhere to go, so this is a 409 (a conflict
        # with the resource's state) rather than a validation 400.
        return web.json_response(
            {"error": "this session is not a fork", "code": "not_a_fork"},
            status=409,
        )
    if slot.memory_mode != "persistent":
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_merge_back",
            outcome="denied",
            source="dashboard",
            resources=f"slot={name},memory_mode={slot.memory_mode}",
            error="non-persistent slot",
        )
        return web.json_response(
            {
                "error": "cannot merge back a non-persistent session",
                "code": "non_persistent_session",
            },
            status=400,
        )
    # A turn in flight has no boundary worth summarizing, and the summarizer
    # would decline it anyway; saying so here as a distinct code (Risk 6) lets
    # the caller treat it as wait-and-retry rather than a hard failure — the
    # same distinction api_chat_slot_summary_generate draws.
    if getattr(slot, "running", False):
        return web.json_response(
            {"error": "this fork has a turn in progress", "code": "summary_turn_running"},
            status=409,
        )

    # Serialise the transition on the same per-slot lock the fork endpoint
    # uses: two concurrent merge-back POSTs (double-submit, two open tabs) must
    # not both pass the ``_merged`` gate and each append a block to the parent.
    async with slot._fork_lock:
        # Hold the fork read-only for the ENTIRE transition (GPT review): the
        # summarize/append awaits below yield the loop, and a turn starting
        # mid-merge would be omitted from the summary yet archived with the
        # fork. chat_send rejects new turns while this is set; the re-check at
        # the top of the locked body catches work already running or queued.
        slot._merging = True
        try:
            return await _merge_back_locked(state, slot, name, request_app)
        finally:
            slot._merging = False


async def _merge_back_locked(
    state: "DashboardState",
    slot,
    name: str,
    request_app: str,
) -> web.Response:
    """The merge transition proper. The caller holds ``slot._fork_lock``."""
    if getattr(slot, "_merged", False):
        # Idempotency guard: a fork carries its parent link forever. One case
        # is retryable: a prior call persisted the parent block but the archive
        # save failed (503) — ``_archive_pending`` marks it — and the retry
        # must re-attempt ONLY the archive, never the append, so the parent can
        # never gain a duplicate block. Every other merged fork (completed
        # merge, History-resumed archive) is done: 409.
        if getattr(slot, "_archive_pending", False):
            parent_session_key = slot.forked_from or ""
            failure = await _archive_fork(state, slot, name, parent_session_key, request_app)
            if failure is not None:
                return failure
            parent_slot = _resolve_parent(state, parent_session_key)
            # The retry archived the fork for real this time: refresh the
            # sessions board like the main path does, or the fork keeps
            # showing as open until an unrelated update.
            _sync_dashboard_slots(state)
            state.push_slots_update()
            # Same shape as the main path (First Principles review: the
            # client's onSuccess reads ``ok`` + ``parent_key`` only; a
            # which-step-retried marker had no consumer).
            return web.json_response(
                {
                    "ok": True,
                    "parent_key": parent_slot.key if parent_slot else parent_session_key,
                }
            )
        return web.json_response(
            {"error": "this fork has already been merged back", "code": "already_merged"},
            status=409,
        )

    # Re-check under the lock (GPT review, TOCTOU): a turn can start or a
    # message can queue between the handler's pre-lock check and lock
    # acquisition. Queued work counts too — the caller's ``_merging`` guard
    # stops NEW sends, but work already staged would be archived unsummarized.
    if getattr(slot, "running", False) or getattr(slot, "_queue", []):
        return web.json_response(
            {"error": "this fork has a turn in progress", "code": "summary_turn_running"},
            status=409,
        )
    # Sub-agent children are work in flight too (GPT review): an idle fork with
    # a running/queued child — or a completion whose delivery injection is
    # still landing — would summarize without the result and archive it unread.
    # Reuses the reload/continue guard, which fails closed on unreadable
    # probes. New spawns mid-merge are impossible: they ride a turn, and
    # ``_merging`` blocks turns.
    children_409 = _subagents_attached_response(
        state, slot, effective_session_key(slot), "merge_back"
    )
    if children_409 is not None:
        return children_409

    fork_session_key = effective_session_key(slot)
    parent_session_key = slot.forked_from

    # Resolve the parent. Prefer the open slot (so the block renders live into an
    # open parent tab); otherwise rehydrate from disk. A parent that is gone from
    # both — deleted, or closed without adopt_closed — is unmergeable.
    parent_slot = _resolve_parent(state, parent_session_key)
    if parent_slot is None:
        parent_name = parent_session_key.removeprefix("dashboard:")
        try:
            parent_slot = await rehydrate_slot_from_history_async(state, parent_name)
        except Exception:
            logger.warning(
                "chat_merge_back: rehydrating parent %s failed",
                parent_name,
                exc_info=True,
            )
            parent_slot = None
    if parent_slot is None:
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation="chat.slot_merge_back",
            outcome="denied",
            source="dashboard",
            resources=f"fork={fork_session_key},parent={parent_session_key}",
            error="parent session no longer exists",
        )
        return web.json_response(
            {
                "error": "the parent session is closed or no longer exists; "
                "reopen it from History first",
                "code": "parent_missing",
            },
            status=404,
        )

    # PARENT RESERVATION (GPT review B1, restructure round): the merged row
    # must never land inside the parent's streamed turn — trailing chunks and
    # the finalized answer would both persist around it. So the parent must be
    # quiescent BEFORE the token-spending summarization starts (rejecting
    # after a 10-30s summarize would burn the pass just to fail), and it must
    # STAY quiescent for the whole transition. ``_merging`` on the parent is
    # that reservation: chat_send / continue / regenerate / rewind answer 409
    # ``merge_in_progress`` through the shared gate, ``_run_chat``'s entry
    # check stops queued dispatch, and the append-level write gate fails any
    # unenumerated writer closed. Cleared in the ``finally`` on every exit.
    #
    # The reservation is EXCLUSIVE (round 5): a parent already reserved by a
    # sibling fork's merge — or itself a merged fork — is rejected too, or two
    # overlapping child merges could interleave snapshot/save and one summary
    # would overwrite the other. Check and set are await-free, so on the
    # single event loop the reservation is atomic.
    if (
        getattr(parent_slot, "running", False)
        or getattr(parent_slot, "_queue", [])
        or getattr(parent_slot, "_merging", False)
        or getattr(parent_slot, "_merged", False)
    ):
        return web.json_response(
            {
                "error": "the parent session has a turn in progress; retry when it finishes",
                "code": "parent_busy",
            },
            status=409,
        )
    parent_slot._merging = True
    # Register both participants' history keys so deletion paths (slot DELETE,
    # History delete) refuse to unlink a transcript this transition is about
    # to durably save — the save would otherwise resurrect the deletion.
    _reserved = {slot_history_key(slot), slot_history_key(parent_slot)}
    if not isinstance(state.merge_reserved_keys, set):
        state.merge_reserved_keys = set()
    state.merge_reserved_keys |= _reserved
    try:
        return await _merge_back_reserved(
            state,
            slot,
            name,
            request_app,
            parent_slot,
            fork_session_key,
            parent_session_key,
        )
    finally:
        parent_slot._merging = False
        state.merge_reserved_keys -= _reserved


async def _merge_back_reserved(
    state: "DashboardState",
    slot,
    name: str,
    request_app: str,
    parent_slot,
    fork_session_key: str,
    parent_session_key: str,
) -> web.Response:
    """The transition body. Fork lock held; both slots hold ``_merging``."""
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "could not summarize the fork", "code": "summary_unavailable"},
            status=409,
        )

    # Freeze ONE fork snapshot and derive the merge identity from it before
    # anything model-shaped runs. Both slots are held quiescent (``_merging``
    # + the write gates), so the snapshot cannot move under the summarizer.
    #
    # FLUSH FIRST (GPT round 5): a recently finished turn can still be dirty
    # in memory, and ``read_messages_chained`` reads the DISK transcript — an
    # unflushed tail would be omitted from the identity snapshot while the
    # summarizer's own flush later includes it, so a retry after an archive
    # failure + restart would mismatch the receipt and append a duplicate
    # block. Durable (best_effort=False): a failed flush means the snapshot
    # below would be stale, so refuse rather than hash the wrong bytes.
    fork_key = slot_history_key(slot)
    try:
        await save_slot_off_loop(state, slot, force=True, best_effort=False)
    except Exception:
        logger.warning(
            "chat_merge_back: pre-snapshot flush of fork %s failed", slot.key, exc_info=True
        )
        return web.json_response(
            {
                "error": "could not persist the fork before summarizing; please retry",
                "code": "fork_flush_failed",
            },
            status=503,
        )
    fork_visible = _visible(await asyncio.to_thread(log.read_messages_chained, fork_key))
    parent_key = slot_history_key(parent_slot)
    parent_all = await asyncio.to_thread(log.read_messages_chained, parent_key)
    parent_visible = _visible(parent_all)
    start, end = _post_fork_range(parent_visible, fork_visible)
    fork_point_full = start == len(fork_visible)  # whole fork WAS the parent prefix
    head_fork = start > 0  # shared a prefix → the summary also covers it
    advanced = max(0, len(parent_visible) - start)

    source_sig = _source_sig(fork_visible)
    merge_key = _merge_key(fork_session_key, source_sig)

    # A fork whose visible transcript is ENTIRELY the copied parent prefix has
    # nothing post-fork to fold back (Design review): the summary would only
    # restate content the parent already holds. Refuse before the summary
    # spend with a distinct retryable-after-work code.
    if fork_point_full:
        return web.json_response(
            {
                "error": "this fork has no work of its own to merge back yet",
                "code": "nothing_to_merge",
            },
            status=409,
        )

    # RECEIPT SCAN (GPT review B2, restructure round): the parent may already
    # carry this exact merge — a prior call persisted the block, the archive
    # failed, and a restart lost the in-memory retry marker. Identity is the
    # signed ``merge_key`` (this fork, at exactly this snapshot), never the
    # positional count/range that a same-length rewrite defeats. Scan the full
    # on-disk chained transcript (the live window is bounded) plus the live
    # window (a just-appended block may not be flushed). A receipt for a
    # DIFFERENT snapshot of this fork, or a legacy block with no ``merge_key``,
    # is history — it proves nothing about the current snapshot and never
    # suppresses a fresh merge.
    receipt_exists = False
    for m in list(parent_all) + list(parent_slot.messages):
        mm = m.get("meta") or {}
        if not isinstance(mm, dict):
            continue
        if mm.get("kind") != "merged_summary" or mm.get("merged_from") != fork_session_key:
            continue
        if mm.get("merge_key") == merge_key:
            receipt_exists = True
            break

    payload: dict = {}
    if not receipt_exists:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        # The ambient summarizer defaults to DISABLED (it spends tokens nobody
        # asked for), and ``force`` deliberately does not lift that off switch —
        # so on a default install the merge action could never succeed (GPT
        # review). A merge-back click IS an explicit request to spend one
        # summarization pass, the same consent that lets ``force`` lift the
        # clean-stop and cadence gates. Lift it for this call only; the ambient
        # path and every other gate (in_flight, memory_mode, running,
        # too_few_turns) are untouched.
        if not cfg.session_summary.enabled:
            cfg = dataclasses.replace(
                cfg, session_summary=dataclasses.replace(cfg.session_summary, enabled=True)
            )

        # Summarize the WHOLE fork transcript with the on-demand summarizer
        # (Risk 1: whole-transcript, not range-scoped). ``force=True`` lifts the
        # clean-stop and cadence gates the way an explicit panel click does; it
        # still refuses a running turn (already handled above) or a too-few-turns
        # fork.
        await generate_session_summary(state, slot, cfg=cfg, force=True)

        # Read the summary back rather than trusting the generator's bool: a
        # forced pass returns False both when it produced nothing AND when a
        # current summary already existed, and only the payload tells those
        # apart.
        raw_payload, stale = await asyncio.to_thread(log.read_intent_summary, fork_key)
        # ``stale`` after the forced generate above means generation was refused
        # or failed while newer turns are already on disk — merging that payload
        # would archive the fork behind an incomplete summary (GPT review), so
        # refuse.
        if raw_payload is None or stale or not raw_payload.get("intents"):
            return web.json_response(
                {"error": "could not summarize the fork", "code": "summary_unavailable"},
                status=409,
            )
        payload = raw_payload
        summary_md = _render_summary_markdown(payload)
        if not summary_md:
            return web.json_response(
                {"error": "could not summarize the fork", "code": "summary_unavailable"},
                status=409,
            )

        # Empty when untitled: the card falls back to its own localized label
        # (UX review — a persisted English "Untitled" would render verbatim
        # beside i18n'd chrome in 12 languages).
        fork_title = slot.title if slot._titled else ""
        fork_title, _ = redact_exfiltration_urls(fork_title)
        fork_title, _ = redact_credentials(fork_title)

        meta: dict = {
            "kind": "merged_summary",
            "merged_from": fork_session_key,
            "merged_from_title": fork_title,
            # The merge's durable identity: this fork, at exactly the snapshot
            # summarized (hash over the canonical ordered fork transcript,
            # domain-versioned by _MERGE_KEY_DOMAIN). The ONLY receipt field —
            # source_sig is already folded into it, and a schema/range twin
            # would have zero readers (First Principles review).
            "merge_key": merge_key,
            "ts": _iso_now(),
        }
        if head_fork and advanced > 0:
            # Structured count; the card renders a localized note from it
            # client-side (UX review — no persisted English twin: this code
            # always writes ``advanced`` under the same condition a fallback
            # string would cover, so no shipped block can lack it).
            meta["advanced"] = advanced
        if head_fork and not fork_point_full:
            # A head-fork's summary describes the copied parent prefix too, so
            # label the block honestly rather than implying it is post-fork
            # only.
            meta["covers_full_fork"] = True

        # Append WITHOUT broadcasting, persist, then broadcast: an open parent
        # tab must only render the block once it is durably on disk — pushing it
        # first would show a row that vanishes on reload if the save fails.
        parent_slot.append(
            _MERGED_ROLE, summary_md, _MERGED_CLS, ts=meta["ts"], meta=meta, broadcast=False
        )
        block_msg = parent_slot.messages[-1]
        # ``append`` enqueues to ``_pending`` unconditionally (delivery and
        # broadcast are separate channels). Remove only OUR copy: a full
        # ``drain()`` would also clear another turn's undelivered chunks and
        # truncate an attached SSE/OpenAI-compat reader mid-response (GPT
        # review) — rare for a dashboard parent (ws=1 leaves ``_pending`` as
        # dead weight) but the narrow removal costs nothing.
        try:
            parent_slot._pending.remove(block_msg)
        except ValueError:
            pass
        try:
            await save_slot_off_loop(state, parent_slot, force=True, best_effort=False)
        except Exception:
            # Roll back the in-memory append — by identity, since another append
            # may have landed on the parent since — so a retry does not double
            # it. The fork is untouched (not yet archived): safe to retry.
            try:
                parent_slot.messages.remove(block_msg)
                # ``append`` incremented the lifetime counter; a rollback that
                # leaves it inflated makes activity probes (e.g. Slack backfill
                # liveness) see a message that no longer exists (GPT review).
                parent_slot.total_messages = max(0, parent_slot.total_messages - 1)
            except ValueError:
                pass
            except Exception:
                logger.debug("chat_merge_back: block rollback failed", exc_info=True)
            logger.warning(
                "chat_merge_back: durable save of parent %s failed; aborting merge",
                parent_slot.key,
                exc_info=True,
            )
            return web.json_response(
                {
                    "error": "could not persist the merged block into the parent; please retry",
                    "code": "parent_save_failed",
                },
                status=503,
            )
        # Durable — now render it live into an open parent tab, mirroring the
        # broadcast gate ``append`` applies to a non-user role.
        if parent_slot._on_message and not parent_slot._has_reader:
            try:
                parent_slot._on_message(parent_slot.key, block_msg)  # type: ignore[operator]
            except Exception:
                logger.debug("chat_merge_back: merged block broadcast failed", exc_info=True)

    failure = await _archive_fork(state, slot, name, parent_session_key, request_app)
    if failure is not None:
        return failure

    sel().log_api_access(
        caller=request_app or "dashboard",
        operation="chat.slot_merge_back",
        outcome="allowed",
        source="dashboard",
        resources=(
            f"fork={fork_session_key},parent={parent_session_key},"
            f"range=[{start}:{end}],intents={len(payload.get('intents', []))},"
            f"head_fork={head_fork},advanced={advanced},"
            f"receipt_reused={receipt_exists}"
        ),
    )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    # Only what the client consumes (First Principles review): the mutation's
    # onSuccess reads ``ok`` + ``parent_key``; range/intent detail lives in the
    # block's persisted meta where it has real readers.
    return web.json_response(
        {
            "ok": True,
            "parent_key": parent_slot.key,
        }
    )


async def _archive_fork(
    state: "DashboardState",
    slot,
    name: str,
    parent_session_key: str,
    request_app: str,
) -> web.Response | None:
    """Archive a merged fork; returns an error response, or None on success.

    Marks the fork merged (which implies closed, so restore paths skip it) and
    read-only. ``note_slot_closed`` records the synchronous tombstone and
    returns the close instant, persisted as merged_at/closed_at.

    On a durable-save failure the merge FACT stands — the parent block is
    already persisted — so ``_merged`` stays True and ``_archive_pending`` marks
    the slot: the handler's retry branch then re-attempts ONLY this archive
    step. (Resetting ``_merged`` here would let the retry re-run the whole
    merge and append a duplicate block to the parent.)
    """
    slot._merged = True
    merged_at = note_slot_closed(state, name)
    try:
        await save_slot_off_loop(
            state,
            slot,
            merged=True,
            merged_at=merged_at,
            force=True,
            best_effort=False,
        )
    except Exception:
        logger.warning(
            "chat_merge_back: durable archive of fork %s failed; parent block persisted",
            slot.key,
            exc_info=True,
        )
        slot._archive_pending = True
        return web.json_response(
            {
                "error": "the summary was merged but the fork could not be archived; "
                "retry to re-archive",
                "code": "archive_failed",
            },
            status=503,
        )
    slot._archive_pending = False
    # Drop the archived fork from the open slots, like the tab-close handler.
    state._slots.pop(name, None)
    return None
