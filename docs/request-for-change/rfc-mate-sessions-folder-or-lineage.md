---
title: Where a crewmate's sessions live — the mate home folder, or lineage stamps with no dispatcher folders
status: accepted
author: iamwhatever
created: 2026-11-02
last-audited: 2026-11-02
audited-at: 764aade2c2
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Where a crewmate's sessions live

**Status:** accepted 2026-11-03. The maintainer Joe Guo (@iamwhatever) merged it in [#27837](https://github.com/kirodotdev/KiroCrew/pull/27837) and picked path B; the pick is recorded in [2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md](../decisions/2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md). This document changes no code and supersedes nothing. Every "on main" claim below was checked at main `764aade2c2` (2026-11-02); citations name files and symbols at that commit.

## The question

Two changes about the same thing landed one day apart, in different directions:

| | [#25994](https://github.com/kirodotdev/KiroCrew/pull/25994) | [#26643](https://github.com/kirodotdev/KiroCrew/pull/26643) |
|---|---|---|
| Kind | Code | RFC only ([rfc-crewmate-workers-under-the-crewmate.md](rfc-crewmate-workers-under-the-crewmate.md)) |
| Author | @CrysisDeu | @iamwhatever |
| Merged | 2026-10-30 21:41Z, `b7b76f09c2` | 2026-10-31 12:00Z, `9a0719d6cd` |
| Status on main | Live, no flag | `draft`: nothing built, not accepted |
| Groups a mate's sessions by | One locked sidebar folder per mate | Who-created-whom (lineage), out of the sidebar |
| Folders afterwards | The mate files into its home folder | Dispatchers lose folder tools and mate-home filing |

#26643 knew of #25994: its section 6 keeps existing home folders and stops new mate-home filing only behind its preview flag. But the two set different end states. The first step of the #26643 path is open as [#26952](https://github.com/kirodotdev/KiroCrew/pull/26952) (checks passing, awaiting review, not merged). Before more is built, the owners should pick one.

**Decision asked:** do a crewmate's sessions keep living in the mate home folder (path A), or do we move to lineage stamps and remove dispatcher folder tools (path B)?

## The two paths

| | Path A: keep folders | Path B: lineage stamps |
|---|---|---|
| Do | Keep #25994 as is; withdraw #26643 steps 3 to 9 | Merge #26952, then build #26643 steps 3 to 9 |
| Grouping key | Folder `owner_app == "member:<store>"` and `mate_home` | Slot stamps `mate_root` and `mate_dispatched_by` |
| Where the person sees them | The mate's folder in the sidebar | The mate's Sessions tab, grouped like the Crew panel |
| Mate-home filing | Stays | Stays while the preview flag is off; stops with it on (#26643 step 7); the flag turns on by default at step 9 |
| Recorded decision | Matches [2026-10-17](../decisions/2026-10-17-crewmate-chat-hands-off-to-worker-sessions.md) ("appears in the sidebar") | Supersedes that sidebar clause (#26643 step 9) |

#26952 is additive and changes no list or filing, so under A it can still merge if a grouped status view is wanted later. Closing it is a scope choice, not a technical need.

### What path A is, on main

`src/kiro_crew/dashboard/mate_folders.py` (447 lines, from #25994):

- `ensure_mate_home` makes one top-level folder per mate, marked `MATE_HOME_KEY` (`"mate_home"`) and owned by `mate_principal(store)` (`"member:<store>"`). It is made the first time the mate opens a chat or folder, and renamed when the mate is renamed (`follow_display_name`, called from `crew_update.py`).
- `mate_home_filing`, called from `session_create.py`, files every session the mate's own conductor chat opens into the home, or a folder inside it. That chat is any slot running as the mate on the mate conductor (`_conducts_for_a_worker`, which reads the run record through `_dispatching_crewmate`).
- `refuses_refile` and `refuses_write_into` fence the home against everyone but the mate. `_refuse_mate_move_out` in `chat_folders.py` answers 403 `mate_home_locked` to those callers, and 403 `mate_folder_scope` to the mate when it files a chat outside its home.
- `chat_folder_cleanup.py` never removes a home or its subtree (`mate_home_subtree_ids`). `hand_back_home` drops the marker only when the mate is deleted (`crew_removal.py`, `installed_agents.py`).

### What path B has so far

#26952 at head `4bf644a704` adds `MATE_ROOT_FIELD` and `MATE_DISPATCHER_FIELD` to `stamp_rows` in `src/kiro_crew/crew_mode.py`. A slot whose `mode` is `"member"` and that has no creator is a mate root. The walk stamps every session below it, at any depth, and stops at the nearest root of either kind. `slots_push.py` sends the two stamps on patch frames. Nothing reads them yet. #26643 steps 3 to 6 add the reader (grouped route, Sessions tab, approvals in the mate chat, hiding behind a flag); steps 7 and 8 drop mate-tree filing and dispatcher folder grants.

## Cases

Each row was checked in code at `764aade2c2` (path A) or at #26952's head (path B).

| Case | Path A: mate home folder | Path B: lineage stamps |
|---|---|---|
| Session moved out of the folder | The person gets 403 `mate_home_locked`. The mate can move chats only within its home. Grouping cannot break, but the person cannot reorganize a mate's chats | Not a case. Stamps read `created_by` and the session tree, not folders |
| Folder deleted | The person is refused (`mate_home_locked`) for the home and every folder inside it; the mate may delete folders inside its own home, not the home. Cleanup skips them. Released only when the mate is deleted | Not a case. No folder holds the group |
| Folder cap `MAX_CHAT_FOLDERS` (500) reached | Only for a mate with no home yet: `ensure_mate_home` raises `MateFolderError`, and the conductor's `session_create` fails with `folder_cap_reached` until a folder is freed | Not a case. No folder is made |
| A conductor the mate opened, and its workers | The mate chat's direct children go home. Their own children are filed where the child names (any folder it may write, a person's included), or in no folder, at the sidebar's top level | Every depth gets `mate_root`. The walk stops only at another root, so a sub-conductor and its workers stay under the mate |
| A normal chat running as the mate | Filed home, like the mate thread | Not stamped: only `mode == "member"` threads are mate roots, so its sessions stay in the sidebar (#26643 section 1 leaves this out of scope) |
| Mate chat closed | Nothing changes. The home and its chats stay until the mate is deleted | Its sessions keep `mate_root` while this gateway process remembers the mate (`_known_mate_roots`, in memory only). After a restart, until the mate thread is reopened, they lose the stamp and would show in the sidebar. Crew Mode roots do not have this gap: `learn_closed_roots` in `crew_mode_migration.py` reloads them from saved metadata |
| Sidebar clutter | One top-level folder per mate. It can be collapsed and reordered only (`LAYOUT_ONLY_FIELDS`); it cannot be hidden, renamed or moved | With the step 6 flag on: no stamped mate session in the sidebar, Recents or grid. Until then, nothing changes |
| Hidden work | None hidden | Sessions the person no longer sees rely on step 5 routing their approvals and questions to the mate chat |
| Code removed | None. #26952 (+96 source, +239 test lines) is closed or left for later | Folder grants, folder guidance and `chat_folder_file_self` for dispatchers (#26643 section 6). `mate_folders.py` and its fences stay, because existing homes stay locked and flag-off filing stays. Deleting them is a later proposal |
| Code kept | `mate_folders.py` and its callers: 18 backend files that name it, 9 frontend files that use `mateFolders.ts` or `MateFolderFace`, plus tests | The same, plus the person's folders and `chat_folder_*` tools for ordinary agents |
| Work still to build | None required. Optional: the grandchild and cap fixes in open questions 3 and 4 | #26643 steps 3 to 9, each under about 600 lines, plus the restart fix in open question 2 |

## Owner decision: a click shows the recap in place

The owner chose this, for both paths. In a mate's profile, on its Sessions tab, clicking a session expands that row and shows the session's recap there. It does not go to another page, and it opens no side panel or drawer. Claims in this section were checked at main `84ea89db7c`.

Today the click leaves the page. The row (`crew-profile-driving-row` in `website/src/pages/members/MembersPageProfile.tsx`) calls `openSessionGuarded`, which asks first when a Schedules draft is unsaved, then `openSessionOnChatPage` (`MembersPageSidePanel.tsx`) navigates to `/chat?sid=<key>`. The tab lists `drivingSessions`, the live slots whose `created_by` is the mate.

**Source: the crew log only, no model call.** The recap is a plain render of records the crew log already holds. It does not use the model-written intent summary (`read_cached_intent_summary` in `src/kiro_crew/dashboard/chat_summary.py`), so it costs no model call.

| Recap line | Crew log record |
|---|---|
| Title | The work item's `title` (`create`), else the slot title |
| Status | The item's latest `status` and `reason` (`report`), and its `state` (`close`) |
| Last report | The item's latest `summary` (`report`) |
| PR link | The item's `pr` and `artifacts` (`report`) |
| Conductor's decision | The item's `decision` (`decide`) |
| Last update | The item's `last_report_at`, else the session's newest `turn/completed` |
| Open approvals | The `pending` entries of the session's existing approvals fold (`_approvals_step` in `src/kiro_crew/crew_log/projection_side_panel.py`, from `approval/requested` and `approval/decided`) |
| Open questions | The session's open `ask_question` calls in the same file's tools fold (`_tools_step`): a `tool/called` with no `tool/completed` yet |

The item fields are `work/recorded` entries (`WORK_ENTRY_TYPE` in `src/kiro_crew/crew_log/entry_types_ledgers.py`), written for each `work_ledger_record` and `work_report`. The board files are a cache of them (`rebuild_from_projection` in `src/kiro_crew/work_ledger.py`). The Crew panel already reads that board for its worker line, through `work_ledger_board.task_records`, and the recap reads it the same way (`src/kiro_crew/dashboard/handlers/crew_workers.py`). `_ledger_items(root)` reads only the root's own ledger; `_crew_ledger_items(root, members)` adds each member's, so a sub-conductor's workers show their reports too. As there, the server checks the session key against the mate's rows from its own stamps and refuses any other. The log holds no question text or answer: `ask_question` leaves a `tool/called` entry and, once answered, a `tool/completed` with a result digest only, so the recap shows a count. `dashboard/waiting_changed` lists only the mate's own waits, so it is not used.

**When the crew log has nothing.** A session bound to no work item, or with no report yet, shows its title and "no report yet". Open approvals and questions still show if any are open.

**What the person can do without leaving.** The recap is read-only. Approvals and question cards from the mate's sessions are answered in the mate chat (#26643 section 4, step 5).

**The full transcript.** It is not reached from this click. It stays reachable from History and search (`api_sessions_search` in `src/kiro_crew/dashboard/handlers/sessions_parts/history_reads.py`). #26643 section 2 says a hidden worker opens from the Sessions tab with a breadcrumb back to the mate. With this decision the Sessions tab no longer opens it; the breadcrumb stays for History, search and links in the mate chat.

**Both paths.** The recap reads work boards and the session's own crew log, not folders or stamps, so its lines are the same under A and B. The rows and boards differ. Under A, the rows are the sessions the mate created, and the mate's own board (`_ledger_items`) holds their items. Under B, the rows are every session stamped `mate_root` for the mate (#26643 section 3), so the recap needs `_crew_ledger_items` to reach a sub-conductor's board.

## Recommendation

Path B, with #25994's filing kept until the step 6 flag is on by default (step 9).

1. Grouping by who-created-whom reaches every depth. Auto-filing reaches only the mate chat's direct children.
2. B does not fail at the folder cap. A blocks a new mate's first session at 500 folders.
3. The person's sidebar holds no mate sessions at all. Under A each mate adds a folder there that the person cannot hide.
4. B's next step, #26952, is inert and ready. It changes no list.
5. A costs nothing now, works today, and matches the recorded decision. If the owners want no more work here, A is the right pick.

## Open questions

1. **Existing home folders under B.** Keep them locked until the mate is deleted (what #26643 says now), release them to the person when the flag turns on, or move their chats out and delete them?
2. **Restart gap under B.** Load closed mate threads from saved metadata at start, the way `learn_closed_roots` does for Crew Mode roots, or accept that reopening the mate thread is enough?
3. **Under A, the grandchild gap.** Should a session opened by a mate's worker also be filed in the home?
4. **Under A, the cap.** Should a mate's first session succeed unfiled when the folder cap is reached, instead of failing?
5. **A normal chat running as the mate under B.** Stamp it as a mate root too, or leave its sessions in the sidebar?
6. **Record.** Answered 2026-11-03: yes. The path B pick is the decision entry [2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md](../decisions/2026-11-03-a-crewmates-sessions-leave-the-sidebar-for-its-sessions-tab.md), and this RFC and #26643 moved to `accepted`. That entry does not change the flag default. The 2026-10-17 sidebar clause gives way once the #26643 step 6 flag is on by default (step 9).
