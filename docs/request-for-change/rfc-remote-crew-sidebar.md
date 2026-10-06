---
title: Remote crews in the chat sidebar — per-machine groups and session ownership
status: partial
author: zejiangg
created: 2026-10-03
last-audited: 2026-10-06
audited-at: 9bf943b0f9
doc-pr: 16489
implementation-prs: [16756, 16757]
tracking-issues: [6180, 7445, 10618, 10826, 14585]
supersedes: []
superseded-by: []
---

# RFC: Remote crews in the chat sidebar — per-machine groups and session ownership

**Status:** `accepted` on 2026-10-03 by maintainer iamwhatever (see Open
questions); now `partial`. Waves 2 and 3 are built: #16756 groups the sidebar
per machine and #16757 opens a crew row as a window onto the peer's slot.
Waves 4 and 5 are not. The Current state section below records main at
`e698e8ca7a`, before either wave.

## Summary

Two linked decisions about sessions on a connected crew (a "peer"):

1. **Sidebar shape.** Replace the merged list (local and peer rows interleaved,
   told apart by a badge) with **one group per machine**: `Local` first, then
   one collapsible group per connected crew carrying a connection badge. A
   disconnected crew's rows stay visible, dimmed, as cached state.
2. **Session ownership.** A session on a crew is **owned by the peer**. The hub
   shows it through a window onto the peer's own chat API and keeps no second
   transcript. The relay-into-a-local-slot model is frozen and then retired.

Decision 1 depends on decision 2: per-machine groups only read honestly when a
row in a crew's group IS that crew's session, not a local copy of it.

## Motivation

### Current state

**Sidebar.** `website/src/hooks/useInstanceSessions.ts` (header comment
*WHY MERGED AND NOT SECTIONED*) merges a peer's live sessions into the local
recency list; origin is a property of a row, rendered as a badge.
`website/src/pages/chat-sidebar/sessionSources.ts` (`useSessionSources`)
assembles local slots, peer rows and the federated Older Sessions search into
that one list. Peer rows come from the hub route
`GET /api/instances/{id}/chat-slots` (`read_peer_slots` in
`src/kiro_crew/dashboard/handlers_instances.py`), behind the
`PREVIEW_INSTANCE_SESSIONS` flag (`website/src/utils/previewFlags.ts`).

**Ownership.** "New chat on crew" (`create_peer_slot`) and adopt
(`src/kiro_crew/dashboard/remote_adopt.py`) both create a **local** slot stamped
`executor="remote"`, `instance_id`, `remote_slot`. Each send goes through
`relay_remote_turn` (`src/kiro_crew/dashboard/remote_relay.py`), which POSTs
the turn to the peer, reads its SSE stream and replays every row into the
local transcript. So one conversation has **two transcripts**: the peer's
(authoritative, it ran the turn) and the hub's (a mirror). Adopt backfills the
peer's history into the local copy once, at birth.

### Problems

Every defect below except #7445 is a place the two transcripts, or the two
slots, disagree:

| Defect | Where it breaks |
|---|---|
| Peer approvals unanswerable — [#10618](https://github.com/kirodotdev/KiroCrew/issues/10618) | The approval card renders from the LOCAL slot projection (`pending_approval` / `approval_id`); a relayed turn never fills it, and `api_chat_slot_approve` has no local future to resolve. Recorded as *KNOWN GAP* in `relay_remote_turn`'s docstring. |
| No resume-attach — [#14585](https://github.com/kirodotdev/KiroCrew/issues/14585) | A hub restart drops the relay reader; the peer keeps running. `selectTurnInterrupted` reads the local transcript's shape, so a live peer turn shows *Turn interrupted*. Deferred in `remote_relay.py`'s *What is deliberately NOT here*. |
| Continue / regenerate / rewind / todo refused with 409 | `remote_bound_refusal` (`remote_relay.py`), called from `chat_handlers.py`, `chat_regenerate.py`, `chat_rewind.py`, `chat_todo.py`. Each would otherwise run the turn locally and fork the transcripts. |
| Executor lost on restore — [#10826](https://github.com/kirodotdev/KiroCrew/issues/10826) | `_apply_recent_session` (`src/kiro_crew/dashboard/chat_persistence.py`) never restores `executor` / `instance_id` / `remote_slot`; only the full rehydrate path does (`_read_executor` in `src/kiro_crew/dashboard/slot_persistence/metadata_codec.py`, which the `RECENT` purpose skips). The next send runs locally. |
| Peer-dispatched workers need hub-side parent rewriting — [#14907](https://github.com/kirodotdev/KiroCrew/pull/14907) nested federated workers under their remote conductor, and [#15653](https://github.com/kirodotdev/KiroCrew/pull/15653) (both merged) added the rewrite | A worker the peer opened cites the peer's slot key, but the hub shows the LOCAL relay slot, so `_clean_peer_parent` (`handlers_instances.py`) must map peer keys to hub keys (`hub_key`). |
| Older peer sessions not browsable — [#7445](https://github.com/kirodotdev/KiroCrew/issues/7445) | They live under the peer's `/api/sessions`, outside `_PROXY_ALLOWED_PREFIXES` (`api/chat`, `api/stream`) in `handlers_instances.py`. |
| Duplicate rows | `read_peer_slots` must filter peer slots the hub drives, because the peer lists its half of every relayed pair. Only the gateway can correlate them. |

Six of the seven exist only because a local slot stands in for a peer
session. Patching each one adds a mirror for one more piece of peer state
(approvals, in-flight tail, action routing, lineage, restore fields).

## Goals

- A user can tell at a glance which machine owns a session and whether that
  machine is reachable.
- Every action on a crew session (send, stop, approve, continue, regenerate,
  rewind) acts on the crew's own session, so no 409 refusal is needed.
- One transcript per conversation.

## Non-goals

- A new transport. The SSH/SSM tunnel, the Fargate turn API, and
  [`rfc-outbound-instance-transport`](https://github.com/kirodotdev/KiroCrew/pull/13031)
  stay as they are; this RFC only consumes "the peer is connected".
- Syncing memory, skills or config between machines.
- Running a crew's turn with the hub's tools or credentials. That is
  `executor: 'remote'` subagent placement ([#12821](https://github.com/kirodotdev/KiroCrew/pull/12821)),
  a different direction of travel, and unaffected.

## Design

### 1. Sidebar: one group per machine

Behaviours from herdr's [Connecting machines](https://herdr.dev/docs/connecting-machines),
mapped to Kiro Crew parts:

| herdr behaviour | Kiro Crew part |
|---|---|
| `Local` opens at once, never waits for SSH | Local group renders from local slots; peer groups load from `['instances']` and `/api/instances/{id}/chat-slots` independently (`useSessionSources`). |
| One collapsible row per machine; collapsing never switches selection | New group header per instance in the chat sidebar; collapse state kept per instance id. |
| Status badge: online / reconnecting / `! auth` / `! error` | Derived from `TunnelStatus.state` (`src/kiro_crew/instances/ssh_tunnel_manager.py`, `TunnelState`): `connected` → online, `connecting` → reconnecting, `error` → error. **Needs auth is not derivable today:** an auth failure and an unreachable host both diagnose as `ssh_unreachable`, and `DiagnosisResult` (`src/kiro_crew/instances/diagnostics.py`) carries no fix command. Wave 2 adds one structured auth outcome to the diagnosis; until then the badge shows error. |
| Clicking a badge shows the last error and the fix command; no credential popup | Badge opens the existing instance card with `TunnelStatus.error` / `diagnosis`; it never prompts for credentials. A fix command is shown only once the diagnosis carries one (see the row above). |
| Lost connection: last state stays visible, dimmed, input disabled | Keep the last `chat-slots` answer per instance in the browser's query cache (memory only, so a reload clears it); render it dimmed and read-only while not `connected`. Today the send path (`api_chat` in `chat_handlers.py`, via `peer_is_connected`) refuses a relay slot's send while the tunnel is down; that guard keys on `slot.is_remote`, so a windowed peer row (wave 3) needs its own disable. |
| Each machine keeps its own sessions; IDs scoped per machine | A row's identity is `(instance_id, slot_key)` (`sessionRowIdentity`); no key rewriting across machines. |
| Agent rows show a machine token only when several machines exist | No per-row badge inside a machine group; the group header carries origin. With no connected crew there is no group header at all, so the single-machine sidebar is unchanged. |
| Disable/remove a machine leaves its remote sessions running | Disconnecting or removing an instance drops its group from the hub only; nothing is stopped on the peer. |

Folders, pins and the conductor lane apply **within** a group. A peer's
conductor nests its own workers inside that peer's group by the peer's own
`parent.key`, so the `hub_key` rewrite is no longer needed for rendering.

Federated Older Sessions search keeps its rank-interleaved result list: search
is a query over all machines, and a result row keeps its machine badge there.

This reverses the *WHY MERGED AND NOT SECTIONED* rationale. That rationale held
while a crew session could be a local row (a relay slot); under decision 2 it
cannot, so "origin" stops being a row property and becomes a container.

### 2. Ownership: compare

| | (a) Local-owned + relay (today) | (b) Peer-owned, local is a window |
|---|---|---|
| Transcripts | Two; hub mirrors the peer | One, on the peer |
| Approvals (#10618) | New mechanism: mirror peer `pending_approval` onto the local projection, forward the decision back | Peer's own `POST /api/chat/slots/{slot}/approve`, already under the proxied `api/chat` prefix |
| Resume-attach (#14585) | New mechanism: fetch the peer's in-flight tail, splice into local transcript | Read the peer's slot state and stream directly; `running` is the peer's own flag |
| continue / regenerate / rewind | Relay each of four endpoints, keep both transcripts consistent across truncation | Call the peer's own routes, all under `api/chat`; `remote_bound_refusal` deleted |
| Restore (#10826) | Restore three more fields on every restore path | Nothing local to restore; the group re-reads the peer |
| Worker lineage (#14907, #15653) | `hub_key` rewrite in `_clean_peer_parent` (#15653) | Peer's own `parent.key`; rewrite deleted |
| Older sessions (#7445) | Still needs a new read path | Still needs a new read path (same in both) |
| Dedupe in `read_peer_slots` | Required | Deleted: no hub slot drives a peer slot |
| Hub-side history / search of crew chats | Free (local transcript) | Through federated search (exists) |
| Works offline (peer down) | Local mirror readable | Cached, dimmed, read-only while the page is open; after a browser reload a disconnected crew's group is empty until it reconnects (design §1) |
| Trust boundary | Peer text is redacted by `redact_peer_text`, then stored on the hub | Peer text is rendered, not stored. `api_instances_proxy` does not redact today, so wave 3 must apply `redact_peer_text` on the read path |

(a) reaches parity only by mirroring each piece of peer state one at a time;
every row above is a new mechanism. (b) removes mechanisms: the peer already
serves every needed action under the prefixes the proxy admits
(`_PROXY_ALLOWED_PREFIXES`). This is the direction issue
[#6180](https://github.com/kirodotdev/KiroCrew/issues/6180) set out ("nothing is
persisted locally").

## Recommendation

Adopt **(b)** and the per-machine sidebar together.

## Migration plan

Each wave is one PR-sized change, independently shippable.

| Wave | Change | Exit criteria | What happens to the follow-up work |
|---|---|---|---|
| 0 | Accept this RFC (status → `accepted`). | First Principles lane reads it from base. | — |
| 1 | Freeze (a): no new relay mirrors. | No PR adds a relayed endpoint or mirrored peer field to `remote_relay.py`. | #10618 approval-mirror and #14585 tail-splice are closed as superseded by wave 3, not built. |
| 2 | Per-machine groups in the sidebar, behind `PREVIEW_INSTANCE_SESSIONS`. Badge from `TunnelState`; cached-and-dimmed rows. | With one connected crew: two groups, collapse keeps selection, disconnect dims rows and blocks send. With none: sidebar unchanged (snapshot test). | Relay slots still render, inside the crew's group, until wave 4. |
| 3 | Window view: opening a crew row drives the peer's slot through `/api/instances/{id}/proxy/api/chat/...` and `api/stream`. Send, stop, approve, continue, regenerate, rewind all go to the peer. | Approve on a crew session resolves the peer's pending approval (#10618). Reloading the hub mid-turn shows the peer's turn running, not *Turn interrupted* (#14585). Continue/regenerate/rewind return the peer's answer, never 409. A credential planted in peer text renders redacted in the window. | "New chat on crew" mints on the peer and opens a window; it no longer creates a local slot. |
| 4 | Retire (a): existing relay slots migrate. On first load each `executor="remote"` slot becomes a pointer to `(instance_id, remote_slot)`; its local transcript is kept read-only under Local as an archive. | No new slot is created with `executor="remote"`. `remote_bound_refusal`, adopt backfill and the `read_peer_slots` dedupe have no callers and are deleted. | #10826 closes (nothing to restore). #15653's `hub_key` rewrite is deleted. |
| 5 | Older peer sessions browsable (#7445): one GET-only, read-only route for the peer's session list. | A crew group shows *Older* rows without admitting `DELETE /api/sessions` or other mutating session routes. | Independent of waves 3–4; blocked on open question 2. |

Waves 2 and 3 can ship in either order; 4 needs 3.

## Backward compatibility

Relay slots keep working until wave 4. Wave 4 keeps each old local transcript
readable; nothing on the peer changes. A hub talking to an older peer works
as long as the peer serves `api/chat` and `api/stream`, which (a) already
requires; `ensure_version_parity` keeps gating the pair.

## Security considerations

(b) adds no new proxy prefix: every call it makes is already admitted by
`_PROXY_ALLOWED_PREFIXES`, and the route stays owner-only. It does make the
proxy the main read path, which changes two things:

- **Redaction.** `relay_remote_turn` redacts peer text (`redact_peer_text`)
  before the hub stores or shows it. `api_instances_proxy` forwards peer bodies
  without redaction. Wave 3 must redact on the read path before the window
  renders peer text; its exit criteria include that test.
- **Volume.** The `api/stream` row admits the peer's whole broadcast feed, not
  only the session on screen (the comment above `_PROXY_ALLOWED_PREFIXES`). The
  window should subscribe only while a crew session is open.

It narrows one surface: peer text is no longer written into hub storage. Wave 5
adds the one new peer read and must stay GET-only.

## Open questions

1. **Hub-side search of crew chats.** Under (b) a crew conversation is not in
   local history. Is federated search (peer answers live) enough, or must a
   disconnected crew's chats stay searchable on the hub? Proposed: federated
   only, cached rows for display.
2. **Wave 5 route shape.** A new named, GET-only prefix row (e.g.
   `api/sessions` list only) or a hub route like `chat-slots` that reads and
   filters? Proposed: a hub route, matching `/chat-slots`.
3. **Window placement.** Render the window in the existing chat pane (local
   theme, #6180) or reuse the same-origin remote crew pane
   ([#15720](https://github.com/kirodotdev/KiroCrew/pull/15720))? Proposed: the
   chat pane; the remote pane stays for the crew's full dashboard.
4. **Interaction with the docked sidebar** ([#16052](https://github.com/kirodotdev/KiroCrew/pull/16052),
   `rfc-dashboard-chrome-shell`). Groups live inside the docked list; no
   change to the chrome proposed here.
5. **Project / workspace on a crew chat.** What should a crew chat show, and
   let the user pick, for project and workspace? On main the chat renders no
   named-workspace picker (`website/src/components/WorkspacePicker.tsx` is
   never rendered; `api.chatSlotWorkspace` in
   `website/src/api/client/chatSlotSettings.ts` has no non-test caller). The
   composer shows only `ProjectPicker` (`website/src/pages/ChatPage.tsx`), a
   browser of THIS machine's directories. `api_chat_slot_workspace`
   (`chat_handlers.py`) leaves `slot.project` local for a remote slot on
   purpose and forwards the workspace name to the peer (`_apply_remote_pick`).
   The peer's `capabilities.workspaces` is parsed in `handlers_instances.py`
   but read by no frontend code. Proposed under (b): the peer decides, and the
   window shows the peer's project and workspace read-only, with no local
   directory picker.

**Decided 2026-10-03 by iamwhatever (maintainer): this design is accepted.**
Option (b) is adopted with the per-machine sidebar: the peer owns a crew
session, and the local dashboard is only a window onto it. The migration
waves above are the plan. Open questions 1 to 5 are not decided here; each
is settled in the wave PR that meets it.
