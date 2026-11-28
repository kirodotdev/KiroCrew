---
title: "Sessions badge folder opt-out: a per-folder flag that keeps a folder's sessions out of the Sessions rail badge"
status: draft
author: neilieva
created: 2026-11-23
last-audited: 2026-11-23
audited-at: 06eb5cc15c
doc-pr: null
implementation-prs: []
tracking-issues: [38262]
supersedes: []
superseded-by: []
---

# RFC: Sessions badge folder opt-out

**Status:** `draft`. Nothing here is built. Measured against main `06eb5cc15c`.

## 1. Summary

This RFC adds one boolean to a sidebar folder, `skip_sessions_badge`, off by default. When it is on, an unread session filed in that folder or any of its subfolders is left out of the Sessions badge on the nav rail. The browser tab title and the Instances hub chip count the same unread sessions, so this RFC drops it from those too. A session in the folder that is waiting on a tool approval or a question still counts. Nothing else changes: the session keeps its unread dot, the collapsed folder keeps its unread dot, and chimes, toasts and the bell work as today. Only the person can set the flag.

## 2. Motivation

### Current state (at `06eb5cc15c`)

**The Sessions badge.** The rail item is the `chat` surface with `slotMode: ''` (`BUILTIN_SURFACE_NAV.chat` in `website/src/surfaces/surfaceData.ts`, registered in `website/src/surfaces/builtins.tsx`). `NavBadge` (`website/src/shell/nav/navRow.tsx`) reads `selectSurfaceBadgeCount('chat')`, which calls `selectUnreadByMode('')` (`website/src/surfaces/registry.ts`). The count itself is `countUnreadByMode` in `website/src/store/dashboardSlice.ts`. It walks `unreadSlots` and skips two kinds of key:

- Held orphans: keys with no row that no reconcile will drain (`heldOrphanTest` in `website/src/store/dashboard/unread.ts`).
- The keys `crewMemberKeys` returns (same file). These are Crew Mode members whose root is listed, sessions whose mate thread is live, and rows `slackRowHidden` leaves out.

**Two other counts use the same number.** The browser tab title sums every surface through `selectAllSurfacesAttention` (`registry.ts`, read as `builtinAttention` in `website/src/shell/appShell.tsx`), which calls the same `selectUnreadByMode('')`. The count an embedded instance sends to the Instances hub is `relayedUnreadKeys` (`unread.ts`), which applies the same two skips.

**There is already a "waiting on you" exemption for a skipped row.** `slackRowHidden` (`website/src/lib/crewMode.ts`) leaves out a row the server stamped `slack_list_hidden`, except the open session and a row with `pending_approval` or `needs_input`. The server stamps that field once per slot-list pass in `_build_rows` (`src/kiro_crew/dashboard/state_parts/slots_frame.py`), through `slack_row_stamper` (`src/kiro_crew/dashboard/slack_session_list.py`). The peer row mapper in `src/kiro_crew/dashboard/handlers_instances_peer.py` forwards it.

**Folder unread state.** A collapsed folder shows an unread dot when any session in its subtree is unread (`folderTreeHasUnread` in `website/src/pages/chat-sidebar/folderBlocks.tsx`). The number beside it is the folder's session count, not an unread count. Each session card has its own dot, which `dashboard.show_unread_dot` can hide (`showUnreadDot` in `website/src/pages/chat-sidebar/rows.ts`).

**Folder settings.** Folders are stored on the gateway and sent in every slots frame. `PATCH /api/chat/folders/{id}` is `api_chat_folder_update` (`src/kiro_crew/dashboard/chat_folders_routes.py`). The closest field is the folder pin. It is a strict boolean (a string `"false"` is refused with `pinned_invalid`). It is the person's only: an app caller gets `403 pinned_forbidden`, and the refusal is audited as `chat.folder_pinned`. Unpinning deletes the key, so absent means off. The route pushes a fresh slot list on every change (`push_slots_update`). A mate's home folder takes only `LAYOUT_ONLY_FIELDS` (`src/kiro_crew/dashboard/chat_folders_records.py`). The client type is `ChatFolder` (`website/src/types/index.chat.ts`), and the pin's menu item is in `folderBlocks.tsx`. The agent's `chat_folder_update` tool (`src/kiro_crew/mcp_dashboard.py`) takes only name, icon and color. The pin shipped as a plain PR (#17084), tested in `test/test_chat_folder_pinned.py`.

**What mute does.** A muted session never becomes unread from its own activity. It gets no row dot, no folder dot, and nothing in the nav, tab or hub counts, except for a turn that ended in a terminal error. The gate is `isSlotMuted` (`website/src/hooks/sessionMute.ts`), read before `markSlotUnread` in `website/src/hooks/websocket/turnCompletion.ts`, `chatStream.ts` and `serverReads.ts`.

### Problems

A person who files scheduled and background sessions (cron watchers, polling jobs) into their own folders sees each run mark them unread. The Sessions badge then sits at 30 or more all day, and it stops saying whether a chat they care about has something new (#38262).

The existing controls each miss:

- "Mark sessions unread only when they need you" (`unread_only_when_done_or_waiting` in Settings, Notifications) still marks a session unread when its agent finishes, even when the person has nothing to act on.
- Mute keeps the session from ever being marked unread by its own activity (only a terminal error still marks it). The person loses the card dot and the folder dot, which are how they see that a watcher posted something. Mute is also per session, so a new session filed into the folder starts unmuted.
- "Show unread dot on session cards" hides only the card dot. Folder and aggregate counts don't change.

## 3. Goals

- A person can keep one folder's sessions out of the Sessions badge and still see their unread state in the folder.
- A session waiting on the person always counts.
- The rail badge, the tab title and the Instances hub chip keep agreeing on how many sessions are unread.
- Nothing changes for anyone who doesn't set the flag.

## 4. Non-goals

- Changing what mute does, or tying this to mute.
- A new badge kind, a dimmed badge or a second count.
- Changing card dots, folder dots, the Unread filter chip, chimes, toasts or the bell.
- Hiding sessions from the list.
- Letting an app, crew member or agent set the flag.
- A per-session version of the flag.

## 5. Design

### 5.1 The folder field

`skip_sessions_badge` is an optional boolean on a folder. `api_chat_folder_update` handles it the way it handles `pinned`:

- Strict boolean. Any other type answers `400 skip_sessions_badge_invalid`.
- The person's only. An app or crew member caller gets `403 skip_sessions_badge_forbidden`, audited as `chat.folder_skip_sessions_badge`, the same shape as `chat.folder_pinned`. An app can't take its own sessions off the person's badge.
- Absent means off. Turning it off deletes the key.
- Not in `LAYOUT_ONLY_FIELDS`, so a mate's home folder refuses it through the existing check.
- Not added to the agent's `chat_folder_update` tool.

The route already pushes a slot list after the write, so every client re-reads the stamp in §5.2.

### 5.2 The row stamp

Once per slot-list pass, beside `stamp_slack` in `_build_rows`, the gateway builds the set of folder ids that skip the badge: the subtree (`chat_folder_subtree_ids`) of every folder with the flag. A row whose `folder_id` is in that set gets `sessions_badge_skipped: true`. Other rows get no key. The stamp is added after `serialize_slot`, as `slack_list_hidden` is, so the per-slot row memo doesn't cache it.

The peer row mapper forwards it the way it forwards `slack_list_hidden`: only a real `true`.

**Nested folders.** A folder skips the badge when it or any ancestor has the flag. A subfolder can't opt back in. A subfolder's own stored value is kept while an ancestor has the flag, and it takes effect again if the ancestor is turned off. In the ⋯ menu, a subfolder under a flagged ancestor shows the item disabled, and the item's own text names the ancestor ("Left out of Sessions badge by <ancestor>"). The text is in the label because the existing menu code notes that a disabled item can't rely on its title. Moving or deleting a folder goes through the folder routes, which already push a full slot list.

**Moving a session.** `api_chat_slot_folder` (`src/kiro_crew/dashboard/chat_folders.py`) pushes a one-field patch, `push_slot_patch(slot.key, ("folder_id",))`, when the folders generation is unchanged. The stamp can't travel in that patch: `_SLOT_PATCH_FIELDS` (`state_parts/slots_push.py`) doesn't list it, and this design doesn't add it, because the stamp is built outside `serialize_slot`. So when the move changes whether the row is stamped, the route calls `push_slots_update` instead. Every other move keeps the patch.

### 5.3 The count

One predicate in `unread.ts`, beside `crewMemberKeys`:

```ts
export const badgeSkipped = (row: { sessions_badge_skipped?: boolean; pending_approval?: boolean; needs_input?: boolean }): boolean =>
  row.sessions_badge_skipped === true && !row.pending_approval && !row.needs_input
```

`countUnreadByMode` and `relayedUnreadKeys` skip a key whose row matches it, next to their existing `crewMembers.has(key)` check. `pending_approval` and `needs_input` are the same two `ChatSlot` fields `slackRowHidden` reads. A waiting session counts exactly as it does today, so if it is unread it is in the badge. Once the person answers, the row leaves the badge even if it is still unread. A turn that ended in an error is not exempt: it keeps its dot but stays out of the badge (open question 3). The client merges a slot patch into the existing row (`sseSlotPatch` in `dashboardSlice.ts` assigns only the patched fields), so a later title, pin or mute patch keeps the stamp. The predicate stays separate from `crewMemberKeys` because that set is also what the person's lists leave out, and these rows stay listed.

Each count changes like this:

- The rail badge leaves the row out.
- The tab title leaves it out too, because `selectAllSurfacesAttention` calls the same selector. If the tab title still counted it, the person would see a number the rail doesn't explain.
- The Instances hub chip leaves it out through `relayedUnreadKeys`, for the same reason.
- A key with no row has no stamp, so it counts as it does today.
- The card dot, the folder dot and the Unread chip read the sidebar's own unread set (`folderTreeHasUnread`, and `makeStatusMatches` in `rows.ts`), not either function, so they stay as they are.

### 5.4 The menu item

One item in the folder ⋯ menu, beside Pin folder: "Don't count in Sessions badge", and "Count in Sessions badge again" when the flag is on. It calls `updateFolderMutation.mutate({ id, body: { skip_sessions_badge: !on } })`, as the pin item does. It is hidden where the pin item is hidden (`columnId !== undefined`), not shown on a folder `isMateManaged` reports (the route refuses it there), and disabled offline the same way. Its title says what the flag does: unread sessions here stay marked in the sidebar, and only ones waiting on your answer or approval count in the Sessions badge.

`ChatFolder` gets `skip_sessions_badge?: boolean` and `ChatSlot` gets `sessions_badge_skipped?: boolean`. Two label keys and a title key are added to every locale, as for the pin.

## 6. Migration plan

One phase, one PR.

**Phase 1: field, stamp, count, menu item.** Exit criteria:

- `PATCH /api/chat/folders/{id}` with `{"skip_sessions_badge": true}` from the person stores it. `false` deletes the key. A string answers `400 skip_sessions_badge_invalid`. An app or crew member caller answers `403 skip_sessions_badge_forbidden` and writes a `chat.folder_skip_sessions_badge` audit row with outcome `denied`. A mate's home folder refuses it.
- With the flag on, an unread session in that folder isn't in the Sessions badge, the tab title count or the hub relay count. Its card dot and the collapsed folder's dot are still shown.
- The same session with `pending_approval` or `needs_input` counts.
- A session in a subfolder of a flagged folder is left out. The subfolder's menu item is disabled and names the flagged ancestor.
- Moving an unread session into a flagged folder takes it out of the badge, and moving it out puts it back, without a reload.
- Turning the flag off puts the folder's unread sessions back in the badge on every open client.
- With no folder flagged, slot rows carry no `sessions_badge_skipped` key.
- A peer instance's stamped row keeps `sessions_badge_skipped` through the peer row mapper, and a non-boolean value is dropped.

- A move that changes whether a row is stamped sends a full slot list, and a move that doesn't still sends the one-field patch.

Tests go beside the existing ones: `test/test_chat_folder_pinned.py` for the route, `test/test_slack_session_list.py` for the stamp and the peer forward, a slot-folder route test for the move push, the `slack_list_hidden` case in `website/src/test/dashboardSlice.test.ts` for `countUnreadByMode` and `relayedUnreadKeys`, `website/src/test/surfaces.test.tsx` for the tab title sum, and a folder menu test for the item and its disabled state.

## 7. Backward compatibility

The change is additive. An absent field reads as off, so every existing folder keeps today's count. The folder store keeps whole rows on read (`read_rows` in `src/kiro_crew/dashboard/folder_repository.py`), so an older build that loads the file keeps the flag and ignores it. An older client ignores the stamp and counts the row as today. A newer client on an older gateway sees no stamp and also counts as today.

## 8. Security considerations

- **Only the person sets the flag.** It is refused to apps and crew members (`folder_principal` in `dashboard/token_auth_callers.py` names both), and it isn't in the agent's folder tool.
- **An agent can still file its own session into a flagged folder.** `api_chat_slot_folder` lets an app or crew member file a session it owns into any folder except a mate's home (`refuses_write_into` in `dashboard/mate_folders.py`). Such a session leaves the badge. This is accepted: the person chose to quiet that folder, the session keeps its card dot and the folder's dot, and if it waits on an approval or a question it counts again. Fencing filing would change who can file sessions, which is outside this RFC.
- **What needs the person stays visible.** Approvals and questions always count. The flag never hides a session from the list, and the bell and approval paths don't read it.

## 9. Alternatives considered

- **Use mute.** Mute takes the session out of every unread signal, including the card and folder dots. The ask is to keep those.
- **A per-session flag.** A cron or polling job that opens new sessions would need setting again for each one. The folder is where the person already sorts these.
- **Compute it on the client.** The client has the folder list in the `['chat-folders']` query, but the badge is a Redux selector over `state.dashboard`, which doesn't hold folders. Copying folders into Redux adds a second store. The stamp follows `slack_list_hidden`, which is already a skip with the same exemption.
- **Leave the tab title alone.** The tab title would then count sessions the rail badge doesn't, and the person couldn't find what to read.
- **Use `slotNeedsMe` for the exemption** (`website/src/pages/chat-sidebar/needsMeCard.ts`). It also counts a finished reply that offers choices. That reply is the "agent finished" case the person is filtering out, so the narrower `slackRowHidden` rule fits.
- **A subfolder that can opt back in.** That needs a three-state field (inherit, on, off). Nobody has asked for it, and it can be added later without breaking the boolean.

### Earlier requests

The maintainer noted that #13406 and #17139 asked to leave sessions out of aggregate counts and were turned down. Neither was closed on the merits of leaving sessions out of a count. One related design choice was rejected, in #13395, and it is covered below.

- **#13406** asked for conductor workers to get a dimmed badge and to stay out of aggregate counts, folder totals included. It was closed as a duplicate of #13395. That issue shipped in #17700, and its muted workers are left out of every unread count. So leaving a session out of the aggregate count is already on main. This proposal differs in that the session keeps its own dot and folder dot, and only the person's folder choice drives it.
- **#13395** itself rejected muting by folder, because a conductor files into the same goal folder as its workers and each goal makes a new folder. That objection still holds for goal folders, and this RFC doesn't replace the lineage-based mute for conductor workers. If someone flags a goal folder, the conductor's own unread finishes leave the badge too, though its approvals and questions still count. The case here is different: long-lived folders the person made for background work, set once.
- **#17139** proposed an attention-tiered sidebar that changes the default look for everyone. It was closed after 30 days with nobody else asking. This proposal changes no default and touches one count.
- **#17109** (via PR #25757) kept folder and aggregate counts unchanged as a non-goal of that PR, not as a decision against changing them.

## 10. Open questions

1. Should the tab title and the hub chip follow the rail badge (§5.3)? This RFC says yes, so the three counts agree. If a maintainer wants only the rail badge to change, `countUnreadByMode` needs a second entry point and the tab title keeps counting these rows.
2. Is the menu label right? "Don't count in Sessions badge" is plain but long. A shorter label with the full rule in the title is an option.
3. Should a turn that ended in an error count, the way mute lets it through? This RFC says no, because the ask is to count only what waits on the person. Counting it would add `unread_failed` to the exemption.
