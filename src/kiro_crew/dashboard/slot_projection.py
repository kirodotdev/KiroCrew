"""Read-only source-link indexing and wire projection for dashboard chat slots."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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

        # The allowlist generation belongs in the cache key: a cold self-managed
        # GitLab miss must be retried after the allowlist finishes loading.
        cache_key = (slot._source_links_revision, gitlab_hosts_generation())
        if slot._source_links_cache and slot._source_links_cache[0] == cache_key:
            return slot._source_links_cache[1]

        # Asked of the provider registry rather than hard-coded here, so a
        # registered provider's own path marker is honoured by the pre-parse
        # filter instead of being dropped before ``parse_source_url`` sees it.
        path_markers = source_link_path_markers()
        stop_chars = set(" \t\n<>()[]{}\"'")
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
        for msg in reversed(slot.messages):
            if len(found) >= max_links or parse_budget <= 0:
                break
            if not isinstance(msg, dict) or msg.get("role") in non_durable_roles:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or "https://" not in content:
                continue

            # Bound each candidate by the next occurrence.  Without that bound,
            # repeated ``https://`` prefixes make the backwards scan quadratic.
            search_end = len(content)
            while len(found) < max_links and parse_budget > 0:
                idx = content.rfind("https://", 0, search_end)
                if idx == -1:
                    break
                token_limit = search_end
                search_end = idx
                end = idx
                while end < token_limit and content[end] not in stop_chars:
                    end += 1
                candidate = content[idx:end].rstrip(".,!?;:*_~`")
                if not any(marker in candidate for marker in path_markers):
                    continue
                parse_budget -= 1
                try:
                    ref = parse_source_url(candidate)
                except ValueError:
                    continue
                identity = ref.identity
                if identity in found:
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
        redact: Callable[[str], str],
        parse_options: Callable[[str], list[str]],
        has_option_actions: Callable[[str], bool],
        strip_options: Callable[[str], str],
        parse_cls_meta: Callable[[str], dict | None],
        is_turn_interrupted: Callable[[list[dict]], bool],
        is_system_notice: Callable[[str, dict], bool],
        latest_transcript_ts: Callable[..., str | None],
        strip_markdown_preview: Callable[[str], str],
        resolve_effective_agent: Callable[[str, str | None], str],
        budget_source_links: Callable[[list[dict]], list[dict]],
        project_source_links: Callable[[list[dict], bool], list[dict]],
    ) -> dict:
        """Serialize the ordered public slot summary without owning slot state."""
        last_ts = slot.messages[-1].get("ts", "") if slot.messages else ""
        last_msg = ""
        has_options = False
        options: list[str] = []
        prompt_preview = ""
        last_conv_role = ""
        last_activity_ts = ""
        found_conv = False
        for message in reversed(slot.messages):
            role = message.get("role")
            msg_meta = message.get("meta") or {}
            notice = is_system_notice(role, msg_meta)
            if (
                not last_activity_ts
                and role in ("tool_call", "tool_result", "assistant")
                and not notice
            ):
                last_activity_ts = message.get("ts") or ""
            if role in ("user", "assistant") and not notice:
                text = message.get("content") or ""
                if text:
                    if not found_conv:
                        found_conv = True
                        last_conv_role = role
                        if role == "assistant":
                            options = parse_options(text)
                            # ``has_options`` is the STATUS fact (choices are on
                            # screen) and gates ``waiting_for_input``; ``options``
                            # is the PAYLOAD channel button builders consume. An
                            # action-only row raises the former alone, or the row
                            # reads as unattended while it is in fact asking.
                            has_options = bool(options) or has_option_actions(text)
                            if has_options:
                                stripped = redact(strip_options(text))
                                prompt_preview = (
                                    stripped[:240] + "…" if len(stripped) > 240 else stripped
                                )
                    if not last_msg:
                        # Strip before redaction so markdown cannot split a
                        # credential signature and then rejoin it on the wire.
                        redacted = redact(strip_markdown_preview(text))
                        last_msg = redacted[:80] + "…" if len(redacted) > 80 else redacted
            if found_conv and last_msg and last_activity_ts:
                break

        pending_approval = any(not future.done() for future in slot._approval_futures.values())
        last_turn_ts = last_ts
        if slot.running:
            prompt_ts = next(
                (
                    message.get("ts") or ""
                    for message in reversed(slot.messages)
                    if message.get("role") in prompt_roles
                ),
                "",
            )
            queued_ts = slot._last_enqueue_ts if slot._queue else ""
            last_turn_ts = prompt_ts
            if queued_ts:
                last_turn_ts = latest_transcript_ts(prompt_ts, queued_ts) or queued_ts

        waiting_for_input = (
            not slot.running
            and not has_options
            and not pending_approval
            and bool(slot.messages)
            and last_conv_role == "assistant"
        )
        needs_input = bool(slot._question_pending)
        interrupted = not slot.running and is_turn_interrupted(slot.messages)

        pending_approval_info: dict[str, str] | None = None
        if pending_approval:
            for message in reversed(slot.messages):
                if message.get("role") != "permission":
                    continue
                meta = parse_cls_meta(message.get("cls") or "") or {}
                if meta.get("resolved"):
                    continue
                pending_approval_info = {
                    "tool": redact(message.get("content") or ""),
                    "tool_input": redact(meta.get("tool_input", "")),
                    "tool_kind": redact(meta.get("tool_kind", "")),
                    "request_id": redact(meta.get("approval_id", meta.get("request_id", ""))),
                }
                break

        return {
            "key": slot.key,
            "title": redact(slot.display_title),
            "agent": slot.agent,
            "effective_agent": resolve_effective_agent(slot.agent, slot.project or None),
            "model": slot.model,
            # The backend's own withhold verdict for `model`: true = the account
            # cannot run the pin (this session is on the backend default), false
            # = it can, null = not known yet. Carried so the frontend reads the
            # answer instead of inferring it from whether the pin appears in
            # `GET /api/models` -- a list every unrelated filter (deprecation,
            # curation) narrows, which silently turned those filters into
            # entitlement signals (#1819). DISPLAY only; never a write source.
            "model_withheld": slot.model_withheld,
            "reasoning_effort": slot.reasoning_effort,
            "mode": slot.mode,
            "surface": slot.mode,
            "workspace": slot.workspace,
            "project": slot.project,
            # Remote-execution binding. Shipped on every slot (not just remote
            # ones) so the frontend can branch on a field that is always
            # present: an absent key and "runs locally" would be the same
            # reading, and a stale client would then render a peer session as
            # local. The binding's third field, `remote_slot`, is deliberately
            # NOT projected: it is the PEER's slot key, meaningful only inside a
            # request routed back through that instance, and no browser code has
            # any use for it — these two carry every branch the frontend makes.
            "executor": slot.executor,
            "instance_id": slot.instance_id,
            "artifact": slot._artifact,
            "messages": len(slot.messages),
            "running": slot.running,
            "orchestrating": slot._in_stage_execution,
            "queue_depth": slot.queue_depth,
            "stopping": slot._stopping,
            "pending_approval": pending_approval,
            "pending_approval_info": pending_approval_info,
            "last_activity_ts": last_activity_ts,
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
            "options": [redact(option) for option in options],
            "prompt_preview": prompt_preview,
            "trust": slot._trust,
            "trust_reads": slot._trust_reads,
            "trusted_patterns_count": len(slot._trusted_patterns),
            "slack_linked": slot._slack_linked,
            "slack_channel": slot._slack_channel,
            "slack_thread_ts": slot._slack_thread_ts,
            "folder_id": slot.folder_id,
            "pinned": slot.pinned,
            "tags": list(slot.tags),
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
        }
