"""Read-only source-link indexing and wire projection for dashboard chat slots."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from kiro_crew.dashboard.slot_preparing import is_preparing
from kiro_crew.safety_override import safety_override, yolo_policy_permits
from kiro_crew.session_lifecycle import STOP_DECLINED_ESCALATION_SECS


def summary_rows(slot: Any) -> list[Any]:
    """The rows a slot's sidebar summary reads, without loading a pending window.

    A restored row whose transcript window is still pending answers with the
    bounded tail read at restore (``_ChatSlot.sidebar_messages``); every other
    slot, including a duck-typed one, answers with its window.
    """
    if getattr(slot, "window_pending", False) is True:
        return slot.sidebar_messages()
    return slot.messages


def stop_declined_armed(slot: Any, now: float | None = None) -> bool:
    """Whether a recent declined Stop makes the next press a force stop.

    The window is ``session_lifecycle.STOP_DECLINED_ESCALATION_SECS``, the same
    one the channels' second-press hatch uses: long enough for a human to read
    the card and decide, short enough that a press an hour later is a fresh
    first press.
    """
    at = float(getattr(slot, "_stop_declined_at", 0.0) or 0.0)
    if at <= 0.0:
        return False
    current = time.monotonic() if now is None else now
    return current - at < STOP_DECLINED_ESCALATION_SECS


def resolved_row_identity(slot: Any) -> str:
    """The identity the sidebar renders this slot under: its own local key.

    A relay archive is a local row too, so it never shares an identity with the
    peer crew's own row for the same conversation.
    """
    return str(getattr(slot, "key", "") or "")


def live_trust_scope(slot: Any) -> str:
    """The slot's ``SafetyOverride`` scoped-grant key while that grant is live, else "".

    Display only. It answers whether the scope the slot carries still has time on
    it, through ``scope_remaining_secs`` -- a pure read -- because this runs on
    every slots poll and must never expire a grant or write a SEL record. It
    applies the same policy mask ``is_scope_active`` applies first, so a grant
    the approval ceiling denies never shows as Trust. The approval paths decide
    through ``is_scope_active``; nothing reads this value back into ``_trust`` or
    into a stored approval policy.
    """
    scope = str(getattr(slot, "_trust_scope", "") or "")
    if not scope:
        return ""
    if not yolo_policy_permits():
        return ""
    return scope if safety_override().scope_remaining_secs(scope) > 0 else ""


class SlotProjection:
    """Build cached source links and the public summary of a slot.

    The component is deliberately stateless.  Every operation receives the slot
    facade and reads its current containers, because replay and cleanup paths may
    replace those containers wholesale.
    """

    @staticmethod
    def source_links(
        slot: Any,
        *,
        max_links: int,
        non_durable_roles: frozenset[str],
    ) -> list[dict]:
        """Return source links ordered by their most recent mention."""
        from kiro_crew.dashboard.handlers.source_providers import (
            gitlab_hosts_generation,
            parse_source_url,
            source_link_path_markers,
            source_ref_label,
        )
        from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
        from kiro_crew.dashboard.source_providers.links import iter_source_url_candidates

        # Identities the user has explicitly unlinked from this session. The set
        # is keyed on the serialized identity, so a dismissed change stays gone
        # no matter which URL shape re-mentions it. Snapshotting it as a frozenset
        # keeps the loop's membership test cheap and immune to a concurrent mutate.
        # Read defensively: this scanner is a staticmethod designed to run against
        # a bare ``object.__new__``-built slot that supplies only the fields the
        # walk reaches, so a slot that never ran ``__init__`` (and thus has no
        # dismissed set) must read as "nothing dismissed" rather than raise.
        dismissed = frozenset(getattr(slot, "_dismissed_source_links", ()) or ())
        # Keys added under an in-flight (uncommitted) unlink transaction are NOT
        # yet suppressed: publishing them here would let a concurrent broadcast
        # show a chip removed before its guarded write commits (and possibly
        # rolled back). Subtract them so the chip stays visible until commit.
        # Read defensively for the same bare-slot reason as ``dismissed`` above.
        _txn_pending = frozenset(getattr(slot, "_dismissed_txn_pending", ()) or ())
        if _txn_pending:
            dismissed = dismissed - _txn_pending

        # The allowlist generation belongs in the cache key: a cold self-managed
        # GitLab miss must be retried after the allowlist finishes loading. The
        # dismissed-set revision belongs there too: unlinking a chip bumps it via
        # ``invalidate_source_links``, but a set that gained and then lost the
        # same key across two edits would leave the revision unmoved, so its
        # current contents are folded in directly rather than trusting the count.
        cache_key = (
            slot._source_links_revision,
            gitlab_hosts_generation(),
            hash(dismissed),
        )
        if slot._source_links_cache and slot._source_links_cache[0] == cache_key:
            return slot._source_links_cache[1]

        # Asked of the provider registry rather than hard-coded here, so a
        # registered provider's own path marker is honoured by the pre-parse
        # filter instead of being dropped before ``parse_source_url`` sees it.
        path_markers = source_link_path_markers()
        # Keyed on the ref's identity, not on ``ref.url``: a registered
        # provider whose URL grammar accepts more than one shape for the same
        # change (e.g. an optional revision pin kept in the canonical URL)
        # would otherwise render one chip per shape -- identical label,
        # identical status. Built-in parsers emit exactly one canonical URL
        # per change, so for them the two keys are equivalent. What the
        # identity contains (and Jira's instance-context exception) is
        # ``SourceRef.identity``'s contract.
        found: dict[tuple, dict] = {}
        # Charge every parse attempt, including rejected and duplicate URLs, so
        # one accepted oversized message cannot monopolize the event loop.
        parse_budget = max_links * 64
        for msg in reversed(summary_rows(slot)):
            if len(found) >= max_links or parse_budget <= 0:
                break
            if not isinstance(msg, dict) or msg.get("role") in non_durable_roles:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or "https://" not in content:
                continue

            # ``iter_source_url_candidates`` is the shared token grammar (the
            # unlink-authorization predicate ``_ChatSlot.mentions_source_identity``
            # walks the same one, so the two cannot drift). It yields front to
            # back; we need newest-first (reversed) to keep this scanner's "newest
            # mention wins" contract -- combined with the outer
            # ``reversed(messages)`` walk, the first writer into ``found`` is the
            # most recent mention, so the chip links to the newest URL. Collect
            # into a ``deque(maxlen=parse_budget)`` rather than an unbounded
            # ``list``: at most ``parse_budget`` candidates are ever resident, so
            # a message dense in marker-carrying URLs cannot allocate an unbounded
            # candidate list before the budget check stops the scan. maxlen keeps
            # the LAST budget candidates (the message tail = its most recent
            # content), and reversing them yields newest-first within the cap.
            recent = deque(iter_source_url_candidates(content, path_markers), maxlen=parse_budget)
            for candidate in reversed(recent):
                if len(found) >= max_links or parse_budget <= 0:
                    break
                parse_budget -= 1
                try:
                    ref = parse_source_url(candidate)
                except ValueError:
                    continue
                identity = ref.identity
                if identity in found:
                    continue
                # Budget is already charged above, so an unlinked chip costs the
                # same parse work it did before dismissal -- cost accounting is
                # unchanged, only the surfaced result set shrinks. Keyed on the
                # serialized identity so the suppression matches the object, not
                # the particular URL that re-mentioned it.
                if source_ref_identity_key(identity) in dismissed:
                    continue
                # First writer wins, and because the walk is backwards the
                # first writer IS the most recent mention -- so the newest
                # mention's URL (and any sub-path pin it carries) is the one
                # the chip links to.
                found[identity] = {
                    "provider": ref.provider,
                    "number": ref.number,
                    "url": ref.url,
                    "kind": ref.kind,
                    "label": source_ref_label(ref),
                    # The serialized identity travels to the client so an unlink
                    # affordance can name this exact object back to the DELETE
                    # endpoint without the client having to re-parse the URL. It
                    # is the SAME key the dismissed-set filter above tests, so a
                    # round-trip through the wire cannot drift from what the
                    # backend suppresses.
                    "identity": source_ref_identity_key(identity),
                }

        links = list(found.values())
        slot._source_links_cache = (cache_key, links)
        return links

    @staticmethod
    def to_dict(
        slot: Any,
        *,
        include_check_status: bool,
        source_links: list[dict],
        prompt_roles: frozenset[str],
        transient_roles: frozenset[str],
        redact: Callable[[str], str],
        parse_options: Callable[[str], list[str]],
        strip_options: Callable[[str], str],
        parse_cls_meta: Callable[[str], dict | None],
        is_turn_interrupted: Callable[[list[dict]], bool],
        is_system_notice: Callable[[str, dict], bool],
        latest_transcript_ts: Callable[..., str | None],
        last_message_preview: Callable[[str], str],
        resolve_effective_agent: Callable[[str, str | None], str],
        budget_source_links: Callable[[list[dict]], list[dict]],
        project_source_links: Callable[[list[dict], bool], list[dict]],
        coordinator_pending: Sequence[dict] = (),
    ) -> dict:
        """Serialize the ordered public slot summary without owning slot state.

        ``coordinator_pending`` is the list of live ``ApprovalCoordinator``
        records whose ``slot`` is this slot -- a sub-agent spawn gate or a tool
        approval raised inside a running sub-agent. Their futures live on the
        state-level registry, not on ``slot._approval_futures``, so without this
        input the slot reads as idle while its owner is parked on an approval.
        Oldest first; the projection reads only the first one for the card.
        """
        # Local import avoids the state -> slot_projection -> chat_utils -> state
        # cycle at module import time. The value is process-local and cheap to
        # read; it pairs the slot's restarting turn counter with its gateway.
        from kiro_crew.dashboard.chat_utils import chunk_generation

        # The newest DURABLE row: a transient row (the turn-end ``done`` row
        # among them) is never persisted, so a slot rebuilt from disk after a
        # gateway restart lacks it. Projecting from one would move ``last_ts``
        # backwards across the restart, and the dashboard stores ``last_ts`` as
        # the unread watermark it can only clear with a covering ``last_ts``.
        rows = summary_rows(slot)
        last_ts = next(
            (
                message.get("ts", "")
                for message in reversed(rows)
                if message.get("role") not in transient_roles
            ),
            "",
        )
        last_msg = ""
        has_options = False
        options_ts = ""
        options: list[str] = []
        prompt_preview = ""
        last_conv_role = ""
        # When the session last moved: the newest durable row of any role that is
        # not a system notice (a reload card is not activity). The scan runs
        # newest-first, so the first match IS the newest row by position; no
        # timestamp text is compared (rows mix naive and offset-aware formats).
        last_activity_ts = ""
        # When the AGENT last acted: the newest assistant / tool row. Kept apart
        # for readers that ask "did the agent act after X", which a user prompt
        # must not answer (the artifact freshness nudge).
        last_agent_ts = ""
        found_conv = False
        for message in reversed(rows):
            role = message.get("role")
            msg_meta = message.get("meta") or {}
            notice = is_system_notice(role, msg_meta)
            if not last_activity_ts and role not in transient_roles and not notice:
                last_activity_ts = message.get("ts") or ""
            if (
                not last_agent_ts
                and role in ("tool_call", "tool_result", "assistant")
                and not notice
            ):
                last_agent_ts = message.get("ts") or ""
            if role in ("user", "assistant") and not notice:
                text = message.get("content") or ""
                if text:
                    if not found_conv:
                        found_conv = True
                        last_conv_role = role
                        if role == "assistant":
                            options = parse_options(text)
                            has_options = bool(options)
                            if has_options:
                                options_ts = str(message.get("ts") or "")
                                stripped = redact(strip_options(text))
                                prompt_preview = (
                                    stripped[:240] + "…" if len(stripped) > 240 else stripped
                                )
                    if not last_msg:
                        # Stripped, redacted and cut by the caller, which strips
                        # before redacting so markdown cannot split a credential
                        # signature and then rejoin it on the wire.
                        last_msg = last_message_preview(text)
            # An agent row is durable and not a notice, so once `last_agent_ts`
            # is set `last_activity_ts` is too.
            if found_conv and last_msg and last_agent_ts:
                break

        # A send queued behind a busy turn is user input with no transcript row
        # yet. The dashboard bumps `last_activity_ts` on `queue_push`, so the
        # snapshot folds the same instant in, or the next frame would undo it.
        # `latest_transcript_ts` compares instants, not text.
        queued_activity_ts = slot._last_enqueue_ts if slot._queue else ""
        if queued_activity_ts:
            last_activity_ts = (
                latest_transcript_ts(last_activity_ts, queued_activity_ts) or last_activity_ts
            )

        slot_pending = any(not future.done() for future in slot._approval_futures.values())
        pending_approval = slot_pending or bool(coordinator_pending)
        # A turn is in flight when this slot's own task runs it OR when another
        # path (a channel dispatcher answering in the Slack thread this slot is
        # bound to) holds the linked session's turn lease. The second is not a
        # dashboard turn, so it has no task, no generation and no queue here;
        # it only stops the summary from reading its trailing user row as an
        # interruption, and reports the row as running like any other turn.
        turn_in_flight = slot.turn_running or getattr(slot, "linked_turn_running", False) is True
        # A turn running only on the linked session has no number here, so the
        # row names none, as the slot detail does: naming the slot's last
        # dashboard turn would tell the open pane, which already saw that turn
        # end, that this running row is stale, and the pane would stay idle --
        # no Stop, and Resume offered on a reply still being written.
        turn_identity: dict[str, object] = (
            {}
            if turn_in_flight and not slot.turn_running
            else {"turn": slot._turn_generation, "turn_gen": chunk_generation()}
        )
        last_turn_ts = last_ts
        if turn_in_flight:
            prompt_ts = next(
                (
                    message.get("ts") or ""
                    for message in reversed(rows)
                    if message.get("role") in prompt_roles
                ),
                "",
            )
            queued_ts = slot._last_enqueue_ts if slot._queue else ""
            last_turn_ts = prompt_ts
            if queued_ts:
                last_turn_ts = latest_transcript_ts(prompt_ts, queued_ts) or queued_ts

        waiting_for_input = (
            not turn_in_flight
            and not has_options
            and not pending_approval
            and bool(rows)
            and last_conv_role == "assistant"
        )
        needs_input = bool(slot._question_pending)
        interrupted = not turn_in_flight and is_turn_interrupted(rows)

        pending_approval_info: dict[str, str] | None = None
        if slot_pending:
            # The transcript row is consulted only for a SLOT-registry future:
            # a coordinator approval writes no row, and a stale unresolved row
            # from an earlier turn must not describe it.
            for message in reversed(rows):
                if message.get("role") != "permission":
                    continue
                meta = parse_cls_meta(message.get("cls") or "") or {}
                if meta.get("resolved"):
                    continue
                request_id = meta.get("approval_id", meta.get("request_id", ""))
                if not isinstance(request_id, str):
                    continue
                future = slot._approval_futures.get(request_id)
                if future is None or future.done():
                    continue
                request_mid = slot.approval_instance(request_id, message)
                if not request_mid:
                    continue
                pending_approval_info = {
                    "origin": "native",
                    "tool": redact(message.get("content") or ""),
                    "tool_input": redact(meta.get("tool_input", "")),
                    "tool_kind": redact(meta.get("tool_kind", "")),
                    "request_id": redact(request_id),
                    "request_mid": request_mid,
                }
                if meta.get("tool_purpose"):
                    pending_approval_info["tool_purpose"] = redact(meta["tool_purpose"])
                break
        if pending_approval_info is None and coordinator_pending:
            # No unresolved permission row supplied the card: the pending
            # approval is a coordinator one, whose record never reaches the
            # transcript. Its fields were redacted at registration; the redact
            # here keeps this branch on the same wire contract as the row above.
            record = coordinator_pending[0]
            approval_id = str(record.get("id") or "")
            pending_approval_info = {
                "origin": "coordinator",
                "tool": redact(str(record.get("tool") or "")),
                "tool_input": redact(str(record.get("tool_input") or "")),
                "tool_kind": "spawn" if approval_id.startswith("spawn:") else "",
                "request_id": redact(approval_id),
            }
            if record.get("tool_purpose"):
                # Import after state initialization: chat_utils itself imports state.
                from kiro_crew.dashboard.chat_utils import _MAX_TOOL_PURPOSE, _redact_tool_field

                # Redact the full source before the display cap. Native metadata
                # is already capped upstream; recapping would split its notice.
                pending_approval_info["tool_purpose"] = _redact_tool_field(
                    redact(str(record["tool_purpose"])), limit=_MAX_TOOL_PURPOSE
                )

        return {
            "key": slot.key,
            "title": redact(slot.display_title),
            "agent": slot.agent,
            "agent_kind": getattr(slot, "agent_kind", ""),
            # The agent Crew Mode turned on with, "" when it never did
            # (``crew_mode.stamp_rows`` and the dashboard's ``runsCrewMode`` read it).
            "crew_mode_agent": redact(getattr(slot, "crew_mode_agent", "") or ""),
            "effective_agent": resolve_effective_agent(slot.agent, slot.project or None),
            "model": slot.model,
            # Whether this session's turns ask Jev which model tier to run on
            # (the picker's "Auto (Jev)" entry). Shipped on every slot, not only
            # the routed ones, so the picker branches on a field that is always
            # present: an absent key and "the owner picked a model by hand" would
            # otherwise be the same reading, and a stale client would show a
            # routed session as pinned.
            "jev_route": bool(getattr(slot, "jev_route", False)),
            # The backend's own withhold verdict for `model`: true = the account
            # cannot run the pin (this session is on the backend default), false
            # = it can, null = not known yet. Carried so the frontend reads the
            # answer instead of inferring it from whether the pin appears in
            # `GET /api/models` -- a list every unrelated filter (deprecation,
            # curation) narrows, which would silently turn those filters into
            # entitlement signals. DISPLAY only; never a write source.
            "model_withheld": slot.model_withheld,
            # The model the live session actually resolved to, so a slot that
            # inherits (no pin, or a withheld one) can be NAMED rather than
            # shown as "auto". "" = not known. DISPLAY only, like the verdict
            # above: never a write source.
            "served_model": slot.served_model,
            "reasoning_effort": slot.reasoning_effort,
            "mode": slot.mode,
            "surface": slot.mode,
            "workspace": slot.workspace,
            "project": slot.project,
            # Relay-archive marker. Shipped on every slot so the frontend can
            # branch on a field that is always present: an absent key and "runs
            # locally" would be the same reading. The binding's third field,
            # `remote_slot`, is NOT projected: it is the PEER's slot key, and
            # shipping it to a browser buys nothing.
            "executor": slot.executor,
            "instance_id": slot.instance_id,
            "row_identity": resolved_row_identity(slot),
            "artifact": slot._artifact,
            "messages": len(rows),
            "running": turn_in_flight,
            # A loop wake is opening this running turn and its row is not in the
            # transcript yet: a structured monitor writes that row only once the
            # runner accepts the turn. Rides the same frame as `running`, so a
            # reader can tell the wake from a turn the user started before the
            # row lands.
            "wake_starting": bool(
                slot.turn_running and getattr(slot, "_wake_starting_task", None) is slot.task
            ),
            **turn_identity,
            # An automatic compaction in flight on this session. Separate from
            # `running` because it is NOT a dashboard turn: the composer reads
            # idle while it holds the session, which without this field looks
            # like a stall worth pressing Stop on.
            "compacting": bool(getattr(slot, "_compacting", False)),
            # A background assistant message is being prepared outside any
            # turn (``slot_preparing``). Separate from `running` for the same
            # reason as `compacting`: it must not make the slot look busy to
            # sends or queueing, only show the chat's working indicator.
            "preparing": is_preparing(slot),
            # A cooperative Stop was declined moments ago (the session was
            # compacting) and the next press escalates to a force stop. Read
            # with the same window the stop route uses, so the button's hint and
            # the backend's answer cannot disagree.
            "stop_declined": stop_declined_armed(slot),
            "queue_depth": slot.queue_depth,
            "stopping": slot._stopping,
            "pending_approval": pending_approval,
            "pending_approval_info": pending_approval_info,
            "last_activity_ts": last_activity_ts,
            "last_agent_ts": last_agent_ts,
            "waiting_for_input": waiting_for_input,
            "needs_input": needs_input,
            "interrupted": interrupted,
            "stop_state": slot._stop_state,
            "wait_state": slot._wait_state,
            "created": slot.created_at,
            "last_ts": last_ts,
            "last_turn_ts": last_turn_ts,
            "last_message": last_msg,
            "source_links": project_source_links(
                budget_source_links(source_links), include_check_status
            ),
            "source_links_total": len(source_links),
            "todo": slot.todo_payload(),
            # The session's OWN MCP report, deliberately alongside "todo" rather
            # than merged into any host-level MCP payload: /api/mcp/active and
            # /api/mcp/probe answer questions about the host, this answers one
            # about this session, and conflating them is what let a dashboard
            # look like it had confirmed a server the session never mounted.
            "mcp_report": slot.mcp_report_payload(),
            "has_options": has_options,
            "options_ts": options_ts,
            "options": [redact(option) for option in options],
            "prompt_preview": prompt_preview,
            "trust": slot._trust,
            "trust_scope": live_trust_scope(slot),
            "trust_reads": slot._trust_reads,
            "trusted_patterns_count": len(slot._trusted_patterns),
            "slack_linked": slot._slack_linked,
            "slack_channel": slot._slack_channel,
            "slack_thread_ts": slot._slack_thread_ts,
            "folder_id": slot.folder_id,
            "pinned": slot.pinned,
            "mutes_opened": slot.mutes_opened,
            "muted": slot.muted,
            "tags": list(slot.tags),
            "tags_revision": getattr(slot, "tags_revision", ""),
            "color_index": slot.color_index,
            "color_hex": slot.color_hex,
            "color_theme": slot.color_theme,
            "theme_consent": slot.theme_consent,
            "theme_consent_sha": slot.theme_consent_sha,
            "memory_mode": slot.memory_mode,
            "forked_from": slot.forked_from,
            "linked_session_key": slot.linked_session_key,
            "app": slot._app,
            "origin": slot._origin,
            # Creator attribution: the slot key of the session that asked for
            # this one via the session-control create verb ("" for a person's
            # own tab, a fork, a restore). Written at birth and rehydrated, so
            # it is the one durable link from a crew member's DM thread to the
            # workers it drives -- the Crew Members drawer filters the live
            # ``slots`` frames on it. A member caller is ownership-fenced to the
            # slots it created (``authorize_target``), so created == driven.
            "created_by": getattr(slot, "_created_by", ""),
            # Whether THIS gateway process stamped ``created_by`` at mint, as
            # opposed to rehydrating it from the transcript. The field above is
            # restored from a metadata line an agent's file tools can edit, so on
            # its own it is a claim; this says whether it is the gateway's own
            # witness. Carried onto the row because the one reader that DECIDES
            # on it -- the sidebar's lineage join -- gets rows and no slots, and
            # a join that trusted the claim alone would nest a session under
            # whoever a transcript happened to name.
            "lineage_minted": bool(getattr(slot, "_lineage_minted", False)),
        }
