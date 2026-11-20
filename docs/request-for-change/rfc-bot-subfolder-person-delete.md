---
title: The person may delete an empty folder inside a Bot's home folder, and the empty-folder cleanup offers it
status: implemented
author: buluoray (drafted with an agent session)
created: 2026-11-16
last-audited: 2026-11-20
audited-at: 0c5d8bd5f3
doc-pr: 38152
implementation-prs: [38808]
tracking-issues: [37434]
supersedes: []
superseded-by: []
---

# RFC: The person may delete an empty folder inside a Bot's home folder

Status: implemented. A maintainer (bolichen97) approved and merged this RFC in
[#38152](https://github.com/kirodotdev/KiroCrew/pull/38152), and
[#38808](https://github.com/kirodotdev/KiroCrew/pull/38808) (merged 2026-11-18)
built it: `delete_empty_bot_subfolder` in
`src/kiro_crew/dashboard/mate_subfolder_delete.py`, called from
`api_chat_folder_delete` in `dashboard/chat_folders_routes.py`, and the cleanup
gate `is_person_browser` in `dashboard/chat_folder_cleanup.py`. Checked at
`fc89562d69`. It
answers [#37434](https://github.com/kirodotdev/KiroCrew/issues/37434), which a
maintainer triaged as a design-level change to the rule that a Bot's home
folder belongs to the Bot. Every code reference below was read at
`574320d20fc`.

## Summary

A Bot files every folder it makes under its own home folder. When the work in
one of those sub-folders ends, the folder stays in the person's sidebar for
good: the person cannot delete, move or hide it, and "Clean up empty folders" skips
it. This RFC proposes one narrow opening: **the person** may delete a folder
**inside** a Bot's home when it is **empty**, and the cleanup offers such
folders. The home folder itself, and every other rule of the home, stay as
they are.

## Motivation

### Current state

`docs/system-specs/modules/session-control.md` states the rule: the home "is
also closed to everyone else, the person included", and nothing outside the
Bot may "edit or delete a folder inside it". The code that applies it:

| Path | What happens today | Where |
|---|---|---|
| Person deletes a sub-folder | `mate_home_locked` (403) | `api_chat_folder_delete` in `dashboard/chat_folders_routes.py` (`is_mate_home(target) or refuses_write_into(...)`) |
| An app deletes a sub-folder | `mate_home_locked` (403), from the same test | `api_chat_folder_delete` |
| The owning Bot deletes a sub-folder | refused before the handler: `DELETE` is not a verb a member is admitted for | `_MEMBER_CHAT_FOLDER_ID_METHODS` in `dashboard/handlers/_shared_route_guards.py` |
| Person or Bot moves it out | `mate_home_locked` / `mate_folder_scope` (403) | `api_chat_folder_update` |
| Person hides it | `mate_home_locked` (403): `hidden` is not in `LAYOUT_ONLY_FIELDS` | `LAYOUT_ONLY_FIELDS` in `dashboard/chat_folders_records.py` |
| "Clean up empty folders" | skips every id in `mate_home_subtree_ids` | `removable_folder_ids` in `dashboard/chat_folder_cleanup.py` |

The only ways out are deleting the whole Bot (`release_mate_home` then hands
the folders back) or editing `folders.json` by hand.

### Problem

A busy Bot makes many short-lived sub-folders, one per task. Users report the
sidebar filling with empty Bot sub-folders they cannot remove (#37434). The
cleanup action's label promises to clear empty folders and
silently does not.

## Goals

1. The person can delete an empty folder inside a Bot's home from that
   folder's menu in the sidebar.
2. "Clean up empty folders" offers the same empty sub-folders, by the same
   rule, except one that carries a `project_dir` (§2).
3. The Bot keeps placing its chats and folders exactly as today.

## Non-goals

- The home folder itself: still no rename, move, recolor, project or delete,
  from anyone.
- Deleting a sub-folder that is not empty. A non-empty one is refused
  `folder_not_empty` (409) and stays.
- Moving, renaming or hiding a sub-folder.
- Letting a Bot (or any app or crew member caller) delete folders. The reason
  in `chat_folders_routes.py` for refusing that stays true.

## Design

### 1. Single delete

In `api_chat_folder_delete`, a folder that `refuses_write_into` refuses, and
that is not itself a home (`is_mate_home`), is deletable when all of these
hold:

- the caller is the person's browser: no effective app
  (`_effective_request_app`), no member principal
  (`MEMBER_CHAT_PRINCIPAL_KEY`), and no internal secret
  (`request_origin(...)[0] == "dashboard"`). The last test keeps out an agent
  in the person's own session: it reaches this route through the
  `chat_folder_delete` MCP tool, on the internal transport;
- it has no child folder and no live session, checked inside the
  folder-store callback that removes the row, the same check the existing
  `if_empty` branch runs in `_remove_if_empty`;
- no archived session is filed in it (`_folder_history_counts`, counted
  first, as the `if_empty` branch does);
- no saved cron job files its tab into it (`state.crons.chat_folder_ids_async`,
  the read the cleanup already uses). It runs in a `prepare=` callback of
  `mutate_folders`, under the folder-store lock, as the cleanup's `_prepare`
  does. A job save that names a folder checks and persists it while holding
  the same lock (`_persist_holding_folder`), so it lands either before the
  check, and keeps the folder, or after the removal, and finds it gone.

A failed check answers `folder_not_empty` (409), or `mate_home_locked` (403)
for a non-person caller, so the refusal says why. The `CREATED_BY_SESSION`
test of the agent `if_empty` branch does not apply: the person is not an
agent cleaning up after itself.

The sidebar's folder menu, closed today on every Bot folder (`isMateManaged`
in `website/src/utils/mateFolders.ts`), opens on a folder inside a Bot's home
with one item, Delete folder, when the sidebar shows it empty: no session filed
in it, no archived session (`history_count` 0) and no child folder. The home's
menu stays closed. The server checks above are the authority; a folder that
fills between the menu and the click is refused with `folder_not_empty`.

### 2. Cleanup

`removable_folder_ids` keeps the home rows themselves (`is_mate_home`) instead
of the whole `mate_home_subtree_ids` set. A home is never removable, so its
empty sub-folders are offered one by one and the home stays. Every other
cleanup rule (live session, cron job, default folder, channel folder) still
keeps a folder. A folder inside a Bot's home is also kept while it holds an
archived session (`_folder_history_counts`). The dry run reads those counts
already. The confirm path reads them again in its `_prepare` step, under the
folder-store lock and off the event loop through `discovery_executor()`, and
the synchronous `_prune` callback uses the counts `_prepare` stored, so a
folder that gains an archived session after the preview is kept at confirm. For
a leaf folder without a `project_dir`, the cleanup and the single delete then
give the same answer.
The cleanup also removes a whole empty branch at once, while the single delete
refuses a folder that still has a child folder, so the person deletes a branch
from the menu leaf by leaf. For any folder outside a Bot's home, archived
sessions still do not keep it.

The rule that keeps a folder carrying a setting the person chose
(`_KEEP_FIELDS`) applies inside a Bot's home only through `project_dir`. A Bot
sub-folder made from a person's repo folder carries that folder's
`project_dir` (`home_create_target`), and the next worker filed there runs in
that repository; the cleanup keeps such a folder, so a bulk cleanup never
moves a Bot's later work out of its repository. The other fields (icon, color,
tags and the rest) were set by the Bot, not the person, so they do not keep a
folder inside a Bot's home.

The cleanup refuses app and member callers today, but has no dashboard-origin
test. One shared predicate, "the person's browser" (no effective app, no
member principal, no internal secret), decides both paths: the single delete
refuses any other caller on a Bot sub-folder, and the cleanup keeps every
folder inside a Bot's home for any other caller, exactly as it does today.

A cleanup scoped to one folder (`root`, the menu's "Close sessions and
delete") closes the sessions in that tree and drops the setting rule. Inside a
Bot's home that would end the Bot's live work and remove a sub-folder that
carries a `project_dir`, so a `root` inside a Bot's home keeps every folder in
that home, exactly as it does today.

### 3. Bot placement

The code paths do not change. A Bot's conductor that names a folder id that no longer
exists files in its home (`mate_home_filing`: `in_mate_home` is false, so
`ensure_mate_home` is used), and a Bot that needs the sub-folder again creates
it again inside its home (`home_parent_for_create`).

One outcome follows from that. When the person deletes a Bot sub-folder that
carried a `project_dir`, the next session the conductor files under that id
lands in the home, which usually has no project directory, so that worker runs
outside the repository until the Bot makes the sub-folder again. The person
chose that delete from the menu; the bulk cleanup never makes it for them
(§2).

### 4. Relation to the accepted path B plan

Path B (`rfc-crewmate-workers-under-the-crewmate.md`, accepted 2026-11-03 in
`docs/decisions/2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md`)
moves a Bot's sessions out of the person's sidebar, and its migration PR 7
stops conductors from filing into folders. Its §6 row "Mate home folders that
already exist" keeps those homes locked until the Bot is deleted. This RFC
narrows that row for one case and leaves the rest of it standing: the home and
every non-empty sub-folder stay locked, and an empty sub-folder can be
deleted by the person. The opening is permanent, not interim. Before PR 7 it
clears the sub-folders a Bot keeps making; after PR 7 it is how a person
clears the empty ones left behind. It does not answer Open question 1 of
`rfc-mate-sessions-folder-or-lineage.md` (whether a whole existing home is
released when path B turns on); that stays open.

## Migration plan

One phase, one code PR.

- Exit criteria:
  - a person's `DELETE /api/chat/folders/{id}` on an empty Bot sub-folder
    answers 200 and removes it;
  - the same call on a sub-folder holding a live, archived or cron-filed
    session or a child folder answers 409 `folder_not_empty`, and on a Bot's
    home answers 403 `mate_home_locked`; neither removes anything;
  - an app caller on a Bot sub-folder answers 403 `mate_home_locked` as
    today, a member caller is refused by the member route gate as today, and
    an internal (MCP) caller in the person's own session answers 403
    `mate_home_locked` too;
  - the folder menu offers Delete on an empty Bot sub-folder only, never on
    the home;
  - the cleanup dry run lists an empty Bot sub-folder and never the home, and
    keeps a Bot sub-folder that holds an archived session;
  - the cleanup dry run lists an empty Bot sub-folder that carries an icon or
    color, and keeps one that carries a `project_dir`, which the menu's
    single delete still removes;
  - the cleanup confirm keeps a Bot sub-folder that gained an archived session
    after the preview;
  - a cron job saved during a person's single delete either keeps the folder
    or is refused, and neither is lost (an event-gated ordering test);
  - the cleanup from a caller that is not the person's browser keeps every
    folder inside a Bot's home;
  - a cleanup whose `root` is a Bot's home or a folder inside one removes
    nothing;
  - a Bot conductor that names the deleted sub-folder files its next session
    in its home (`mate_home_filing`);
  - the spec paragraph in `session-control.md` names the exception;
  - the §6 row "Mate home folders that already exist" in
    `rfc-crewmate-workers-under-the-crewmate.md` says this RFC narrows it.

## Backward compatibility

No data migration and no API shape change. A call refused today with
`mate_home_locked` can now succeed for the person on an empty sub-folder.

## Security considerations

The opening is limited to the person's browser, and the person can already
delete every other folder in the sidebar, including full ones. An agent gains
nothing: app and member callers are refused before the new branch, and an
internal request (the MCP transport every agent tool call uses) is refused by
the `request_origin` test. A request from the agent's shell to the loopback
port with no internal secret is not "the person's browser" either: the auth
middleware (`token_auth.py`) requires a valid dashboard token on every
request, loopback included, so such a request is refused unless it carries the
person's own token. The emptiness check runs
under the folder-store lock, like `if_empty`, so a session filed between the
check and the removal keeps its folder. The one window the code comment in
`api_chat_folder_delete` names, an archived session closed during the archive
scan, leaves that session unfiled, the same outcome the person's ordinary
delete and the cleanup already accept. Cron jobs leave no such window: their
folder ids are read under the same lock as the removal (§1).

## Alternatives considered

- **Let the Bot delete an empty folder it made** (the second comment on
  #37434). Rejected here: `api_chat_folder_delete` refuses every app caller
  on purpose, because an agent's empty check cannot be atomic with the
  session archive. It also does not help a person whose Bot is idle.
- **Allow `hidden` on Bot sub-folders.** Smaller code, but it hides clutter
  instead of removing it, and the cleanup docstring calls that out as the
  wrong answer.
- **Hide empty folders in the view** (the open setting in #31417). It hides
  empty folders, Bot sub-folders included, but removes none, and the cleanup
  still skips them: the same reason `hidden` is rejected above.
- **Only explain the skip in the cleanup dialog.** Honest, but leaves the
  folders with no way out short of deleting the Bot.

## Open questions

None. Two were answered in review:

1. Deleting a non-empty Bot sub-folder: no. Unfiling a Bot's live or archived
   sessions moves its work out from under it, and the Bot would file new work
   into a fresh sub-folder anyway.
2. Delete in the sidebar folder menu: yes, in this phase (section 1).
