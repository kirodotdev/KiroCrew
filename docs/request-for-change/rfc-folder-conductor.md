---
title: Folder conductor — every sidebar folder is a conductor you can talk to
status: draft
author: zezhexu
created: 2026-10-19
last-audited: 2026-10-19
audited-at: 88914fd0d1
doc-pr: null
implementation-prs: []
tracking-issues: [18335, 22010]
supersedes: []
superseded-by: []
---

# RFC: Folder conductor

**Status:** draft. Nothing in this document is built. What is on main today is a
*derived* folder conductor ([#18438](https://github.com/kirodotdev/KiroCrew/pull/18438)):
the sidebar guesses which session in a folder opened the others and offers a way
to it. This RFC replaces the guess with a conductor that belongs to the folder.
Verified at `88914fd0d1`.

## Summary

A folder **is** a conductor. Talking to a folder means talking to its conductor,
and the person never sees the conductor as a separate session.

| A folder holds… | …and to the folder's conductor that is |
|---|---|
| a session | a session it conducts |
| a subfolder | a conductor it conducts, which conducts that subfolder in turn |

So the folder tree and the conducting tree are one tree. Filing a session into a
folder hands it to that folder's conductor; nesting a folder makes its conductor
answer to the parent's.

Once a folder manages its sessions, it is a **project**, and the last phase renames
it so (§6). This document says "folder" throughout because that is the name on
main until then.

The person sees three things change:

1. Every eligible folder row carries a **Chat with \<folder\>** button.
2. That chat is titled by the folder itself (`📂 wave2 · Folder chat · 4 chats`),
   and the conductor's own session never appears in the session list.
3. The first time the folder chat is empty, it explains in three animated beats
   what a folder chat is for.

The mocks for every surface below, rendered on a live dashboard, are in the description of the pull request that adds this document.

## Motivation

### Current state

A folder is a stored record with a name and a few settings, and no agent behind
it. The record `create_folder` writes (`src/kiro_crew/dashboard/chat_folders_records.py`)
carries `id`, `name`, `order`, `collapsed`, `hidden`, `parent_id`, `project_dir`,
`default_agent`, `created_at`, and optionally `color`, `icon`, `steering_dirs` and
`owner_app`. Nothing in it names a conductor.

The conductor that #18438 shipped is computed in the browser:

- `detectFolderConductor` (`website/src/pages/chat-sidebar/folderConductor.ts`)
  picks the root of the folder's own lineage that has at least one child filed in
  the same folder.
- `folderConductorOf` (`website/src/pages/chat-sidebar/folderBlocks.tsx`) feeds
  it the folder's whole membership, and the row button
  (`folderBlocks.tsx`, testid `folder-conductor-chat-row-<id>`) appears only
  when a conductor resolves.
- The pinned card (`folderBlocks.tsx`) is held behind the
  `mc-preview-folder-conductor` flag (`website/src/utils/previewFlags.ts`).

### Problems

1. **The guess rarely fires on real data.** On the author's own install, measured
   2026-10-19: 243 live sessions, 228 filed in a folder, 40 with a parent, and
   **0** whose parent was filed in the same folder. 33 of the 40 had a parent filed
   in no folder; 7 had a parent in a different folder. A conductor that files its
   workers into a goal folder and stays outside it, which is what conductors do,
   is never detected. On that install the feature does not exist.
2. **"Chat with the folder" opens someone else's log.** When the guess does fire,
   the button lands the person at the bottom of a session that has run for hours:
   in the demo, cycle 120 of a patrol loop. Nothing on screen says who this is or
   why talking to it means talking to the folder.
3. **The conductor reads as one more item in the folder.** The pinned card is a
   session card among session cards. The person's mental model is "I am talking
   to this project", and the screen says "I am talking to one of its chats".
4. **A conductor cannot reach what is filed under it.** Session control fences
   every agent-created caller to the sessions it created
   (`_caller_is_ownership_fenced`, `src/kiro_crew/dashboard/session_control_parts/caller_access.py`;
   enforced in `authorize_target`, `target_authorization.py`). A conductor
   that an agent created cannot reach a session the person files into a folder by
   hand. A conductor the person started in their own tab is not fenced at all: it
   reaches every session in its workspace, sibling folders included. Either way
   "this folder's conductor manages this folder" is not a rule the code holds,
   even when a conductor is detected.

## Goals

- G1. Every eligible folder has exactly one conductor, created on demand, stored
  with the folder, never guessed.
- G2. The folder chat reads as the folder: folder name as title, no visible
  conductor session, the folder row as the open item.
- G3. A conductor's reach is the folder's membership: the sessions filed directly
  in it, plus the conductors of its direct subfolders.
- G4. A person who opens a folder chat for the first time learns, without reading
  docs, why they would talk to a folder.
- G5. A folder nobody chats with costs nothing: no process, no context, no tokens.
- G6. One word per concept. The thing that manages sessions is a **Project**; the
  directory on disk a chat works in is its **Working folder**.

## Non-goals

- The folder-free session tree mode (#18335 part 2). Separate decision.
- A conductor for the unfiled lane or a root conductor above all folders.
- Reaching across workspaces. The workspace boundary in `authorize_target`
  (`target_authorization.py`) stays as it is.
- Conductors for channel folders, app-owned folders (`owner_app`) and crew-member
  folders (`member:<store>` principals). These keep today's behaviour.
- Changing what the `kirocrew-conductor` charter does beyond telling it which
  folder it is.
- A conductor that acts on its own without being asked. It acts on turns the
  person sends and on patrols the person arms, as any conductor does today.

## Design

### 1. The folder owns its conductor

The folder record gains one optional key:

```jsonc
"conductor": {
  "slot": "chat-…",           // the conductor's session key, minted once
  "agent": "kirocrew-conductor",
  "created_at": 1792368968.4
}
```

- **Minted on first open.** `POST /api/chat/folders/{id}/conductor` is idempotent:
  it returns the existing slot or mints one, stamps the record under the
  folder-store lock, and answers the same key on every later call. The slot is
  created like any new tab; its agent process starts on the first turn, as every
  tab's does, which is what makes G5 hold. Its reach is not a tab's: a person's own
  tab is unfenced and reaches the whole workspace, so the minted slot is fenced
  (§3) and starts from the reach of an agent-created caller.
- **One slot for the folder's life.** The work ledger is keyed by the conductor's
  slot key (`conductor_dir(slot_key)`, `src/kiro_crew/work_ledger_store.py`).
  A stable key keeps the ledger and the Dashboard tab across restarts. A fresh
  start uses the existing reset-conversation path, which keeps the slot and its
  ledger and drops only the model context.
- **The slot carries `folder_conductor_of: <folder id>`.** That mark is the only
  thing every surface reads to treat the slot as the folder, and the only thing
  the ownership fence reads to fence it (§3).
- **Lifecycle follows the folder.** Moving a folder moves nothing: the slot is not
  filed anywhere, and its reach is computed from the folder each time (§3).
  Deleting a folder archives its conductor slot, recoverably, as closing a tab does.
- **Eligibility.** Not offered for a channel folder, a folder with `owner_app`, or a
  folder owned by a `member:` principal. The route refuses these with 409
  `folder_conductor_ineligible`.

Only the person may mint a conductor. An app or crew-member principal is refused
403, the way a non-empty `steering_dirs` is refused today (`steering_dirs_forbidden`, `src/kiro_crew/dashboard/chat_folders_paths.py`), so an agent cannot give itself a
conductor.

### 2. The folder chat reads as the folder

| Surface | Today | Proposed |
|---|---|---|
| Folder row hover cluster | `⋯` · `MessageSquarePlus` · `CornerUpRight` (only when a conductor is guessed) | `⋯` · `Plus` · `MessageSquare`, always, on every eligible folder |
| Row button label | "Chat with {{folder}} folder" | "Chat with {{folder}}" |
| Menu item | two lines, the second explaining lineage | one line, same label, `MessageSquare` |
| Conductor's own card | a session card, pinned behind a preview flag | never rendered as a row |
| Folder count | counts the conductor | counts only the sessions it conducts |
| Open item when the chat is open | the conductor's card | the folder row itself |
| Chat header | the conductor session's title | `📂 <folder> · Folder chat · N chats` |
| Search, ⌘K, Older Sessions | the session's own title | `<folder> (folder chat)`, opening the folder chat |

The icon and label changes are the ones [#22010](https://github.com/kirodotdev/KiroCrew/issues/22010)
proposes for the derived conductor; this RFC carries them over unchanged. The
hover cluster stays at three controls, which is over the two-per-row cap by the
owner ruling already recorded on #18438; with a conductor on every folder the
third control is no longer conditional.

The folder name and the rows under it keep the one text column
`docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md` fixes. The
open-item highlight is a background on the existing row, so no text moves.

### 3. Reach is folder membership

`_caller_is_ownership_fenced` gains a fifth population: a slot that carries
`folder_conductor_of`. Without it the conductor would be an ordinary unfenced
caller (no `_created_by`, no channel link, no `member:` or cron prefix), the
`not_creator` fence in `authorize_target` would never run for it, and membership
would bound nothing. With the conductor fenced, `authorize_target` gains one
admission, evaluated before the ownership fence and only for a caller whose slot
carries `folder_conductor_of`:

```
caller is the conductor of folder F
  and target is filed directly in F            -> admit
caller is the conductor of folder F
  and target is the conductor of a direct
  subfolder of F                               -> admit
otherwise                                      -> the existing rules decide
```

- **Computed at call time, never stored.** Membership is read from the folder
  store each time. A session moved out of F is out of reach on the next call. No
  grant list exists to go stale, which is the same reason #18438 refused to store
  its conductor.
- **One level per hop.** F's conductor reaches a subfolder's sessions only by
  talking to that subfolder's conductor. The hierarchy is the delegation path, so
  the person can read each level's chat and see what was asked of it.
- **Only a filing the person made grants reach.** The folder assignment records who
  made it (`filed_by: owner | <slot key>`). A session the conductor itself created
  is in reach because the conductor created it, exactly as today. A session an
  *agent* filed into F, when that agent did not create it, does not enter F's
  reach. Without this rule, any agent that can move a session could hand the
  person's own tab to a conductor.
- **Excluded targets stay excluded.** Every refusal `authorize_target` already
  applies keeps applying first: self-target, live-target checks, channel mirrors,
  the workspace boundary. Added: an incognito or temporary session is never in a
  folder conductor's reach, because reading it would carry its content into a
  context that may be learned from.
- **Audited.** An admission by folder membership is written to the SEL with the
  folder id, so "why could this conductor read that chat" has an answer after the
  fact.

### 4. The conductor knows its folder

Each turn of a folder conductor carries a `[FOLDER CONDUCTOR]` block built by
context assembly: the folder's path, `project_dir`, and a list of what it conducts,
each entry being a session key with its title and state, or a subfolder with its
conductor's key. The block is DATA in the same sense the `[FOLDER]` line is today.
The charter is unchanged except for one rule: a session the conductor creates
with `session_create` is filed into its own folder unless it names another.

### 5. The first empty folder chat explains itself

When a folder chat has no messages, the transcript area shows a client-rendered
empty state instead of a blank page. It plays once, in three beats, between the
header and the composer, both of which stay visible.

The pull request that adds this document carries a recording of the three beats.

| Beat | What moves | What it says about the folder |
|---|---|---|
| 1 · You say it | one line types itself into a chat bubble | you speak to the folder, once |
| 2 · It opens chats | three chats slide in under the folder name, spin, then check | its conductor starts the work inside the folder |
| 3 · It pings you | a PR card marked "Needs you" appears; a bell rings three times | it watches, and interrupts only when you are needed |

Heading: **Say it once. \<folder\> takes it from there.** Subline: **No hunting
through chats. It splits the work, starts it, and keeps watch.** Three suggestion
chips follow; clicking one fills the composer and sends nothing.

- Built with Framer Motion. No CSS `@keyframes`.
- `prefers-reduced-motion` renders the final frame without motion.
- Every string is a catalog key; the mock above is the English catalog.
- Beat 2's chat names are illustrative, never the folder's real sessions, so an
  empty folder and a busy one show the same story.
- Shown whenever the folder chat is empty, which is the first open for every
  folder because the slot is kept (§1).

Because the empty state is the introduction, the conductor's own "Your Conductor"
introduction card would repeat it. A folder conductor's turn carries a marker that
makes it skip that card, the same way
`docs/decisions/2026-10-13-conductor-with-a-dashboard-tab-skips-its-intro-card.md`
skips it for a chat with a Dashboard tab. That is a change to a recorded behaviour
and needs its own decision entry (Phase 3 exit criteria).

### 6. Naming: Folder becomes Project, Project becomes Working folder

Today the two words point at the wrong things. "Folder" names the sidebar
container, and "Project" names the directory on disk a chat works in.

| Thing | What it does | Label on main (en / zh-CN) | Proposed (en / zh-CN) |
|---|---|---|---|
| The sidebar container | groups sessions; after §1–§4 it owns a conductor that manages them | Folder / 文件夹 | **Project / 项目** |
| The chat's directory picker | where the chat works, and whose `AGENTS.md` and `.kiro/steering` it reads | Project / 项目, Select project / 选择项目 | **Working folder / 工作目录**, Select working folder / 选择工作目录 |
| The container's `project_dir` setting | the default working folder for every chat in it | Project directory / 项目目录 | **Working folder / 工作目录**, shown in Project settings |
| A channel, app or crew-member folder (§1 ineligible) | groups sessions, has no conductor | Folder / 文件夹 | Folder / 文件夹, unchanged |

The relationship then reads the way it works:

```
Project            manages its sessions through its conductor
  └─ Working folder   a directory on this computer, chosen per project
       └─ each chat in the project works there by default, and may pick another
```

"Folder" goes back to meaning a folder on disk, which is what the word means in
every file manager. In zh-CN the two labels stay distinct: 项目 is the container,
工作目录 the directory. 工作目录 already appears in the zh-CN catalog for the same
sense, so the translation is not new.

Scope of the rename:

- **User-facing strings only.** Catalog keys, the code identifiers (`folder_id`,
  `project_dir`, `steering_dirs`), the REST routes under `/api/chat/folders` and the
  on-disk folder store keep their names. A rename there buys nothing a person can
  see and breaks every client and saved config.
- **Agent-facing tools keep their names.** `chat_folder_*` and `set_project` are
  API; renaming them breaks every prompt and skill that calls them. Their
  descriptions change to say what each now means ("move a session into a project",
  "set this chat's working folder").
- **Measured size, at `88914fd0d1`.** In `website/src/i18n/locales/en.manual.json`,
  197 lines mention "folder" and 78 mention "project". Not every "project" line is
  the directory sense (some name a GitLab project or a work-ledger project); the
  Phase 5 audit sorts them before anything changes.
- **A label a person may already have decided.** `docs/decisions/` has no entry for
  either word today. The rename lands with one.

## Migration plan

Each phase is one PR and can ship or be abandoned on its own.

### Phase 0 — accept this RFC

Exit: `status: accepted`, with the maintainer decision recorded here.

### Phase 1 — the folder owns a conductor, and it can reach the folder

Backend only. The `conductor` key, the idempotent mint route, the
`folder_conductor_of` slot mark, `filed_by` on folder assignment, the
`authorize_target` admission of §3 (direct sessions only, no subfolder hop yet),
the `[FOLDER CONDUCTOR]` context block, and the default filing rule of §4.

Exit criteria:
- Minting twice returns the same slot key; minting from an app or `member:`
  principal answers 403; minting on an ineligible folder answers 409.
- `_caller_is_ownership_fenced` answers True for a slot carrying
  `folder_conductor_of`, so `authorize_target` runs the `not_creator` fence for it.
- A folder conductor can `session_read_message` a session the person filed in its
  folder, and is refused `not_creator` on a session filed in a sibling folder.
- Moving that session out makes the next read fail; moving it back makes it pass.
- A session an agent filed into the folder, which the agent did not create, is
  refused.
- An incognito session filed in the folder is refused.
- Each admission writes one SEL record naming the folder id.

### Phase 2 — the sidebar and header read as the folder

Frontend. §2 in full, plus retiring `detectFolderConductor`, the pinned card and
the `mc-preview-folder-conductor` flag. This absorbs #22010.

Exit criteria:
- No session row renders for a slot with `folder_conductor_of`, in any lane,
  search or ⌘K result; search and ⌘K list it as `<folder> (folder chat)`.
- The folder count equals the number of rows the folder renders.
- With the folder chat open, the folder row carries the open-item state and no
  session row does.
- `folderConductor.ts` and its preview flag have no remaining callers.

### Phase 3 — the empty folder chat explains itself

Frontend, plus the intro-card skip and its decision entry.

Exit criteria:
- An empty folder chat renders the three beats and the chips; a non-empty one
  renders the transcript.
- Under `prefers-reduced-motion` the final frame renders with no running
  animation.
- Every string resolves through the catalog in all shipped locales.
- A folder conductor's first reply contains no introduction card, and
  `docs/decisions/` has the entry that records it.

### Phase 4 — subfolder conductors answer to their parent

The second admission of §3, and `session_status` for a folder conductor listing
the conductors of its direct subfolders beside its own sessions.

Exit criteria:
- F's conductor can `session_send` to the conductor of a direct subfolder and is
  refused on that subfolder's sessions.
- It is refused on a grandchild folder's conductor.
- Blocked on open question 1.

### Phase 5 — Folder becomes Project, Project becomes Working folder

§6, strings only. Independent of Phase 4; it may land any time after Phase 2,
because only then does the container behave like a project.

Exit criteria:
- Every catalog line that labels the sidebar container says Project / 项目, except
  channel, app and crew-member folders, which still say Folder / 文件夹.
- Every catalog line that labels the chat's directory says Working folder / 工作目录,
  and no user-visible string calls a directory a project.
- The audit list of the 78 "project" lines, each marked directory-sense or other,
  is linked from this document.
- `chat_folder_*` and `set_project` keep their names; their descriptions use the
  new words.
- `docs/decisions/` has the entry recording both labels.

## Backward compatibility

- Existing folders get nothing until someone opens their chat. No migration runs.
- A session that #18438's guess used to surface as the conductor becomes an
  ordinary session in its folder. Its transcript, ledger and Dashboard tab are
  untouched.
- The stale `mc-preview-folder-conductor` key in a browser's local storage is
  ignored once the flag is removed.
- The Phase 5 rename changes no stored data and no API. A person's existing
  folders show up as projects with everything in them; a chat's directory is the
  same directory under a new label.
- A folder record without `conductor` reads exactly as today. A record written by
  this version and read by an older one keeps an unknown key the older version
  does not use.

## Security considerations

- **Reach widens, on purpose, along one edge.** Today an agent-created caller
  reaches only what it created, and a conductor started in the person's own tab
  reaches every session in its workspace. A folder conductor is fenced like the
  first, so it does not get the second's reach. It additionally reaches what the
  person filed beside it. The widening is bounded by membership the person
  chose, recomputed per call, refused for agent-made filings, and audited.
- **Filing is a grant.** Dropping a running session into a folder hands it to that
  folder's conductor. Phase 2 shows that once on the drop:
  "\<folder\>'s conductor can now read and message this chat."
- **No new host access.** A conductor's file and tool permissions are the
  `kirocrew-conductor` agent's, unchanged. Folder membership grants session
  control, never filesystem paths; folder steering keeps its own person-only gate.
- **Fail closed.** An unreadable folder store, a missing `filed_by`, or a slot whose
  `folder_conductor_of` names a deleted folder all fall through to the existing
  ownership fence. The mark itself fences the slot, so falling through leaves
  the conductor with only what it created, never the workspace-wide reach of an
  unfenced tab.

## Alternatives considered

1. **Keep deriving the conductor (#18438).** Fails problem 1: zero detections on
   a real install with 40 parent links.
2. **Make the session tree the authority.** Adopt every filed session under the
   folder's conductor with `session_adopt`. That writes a second record of the
   same fact, and the two drift the moment a session moves folder.
3. **A conductor process per folder, always running.** Satisfies G1 and violates
   G5. Most folders are drawers nobody talks to.
4. **Keep the conductor visible as a pinned card.** What the preview flag ships.
   It says "a chat inside the folder", which is problem 3.
5. **Explain with a static card.** Tried in this RFC's own mock round. It names the
   conductor but does not show what talking to a folder does. The three-beat
   empty state shows it.

## Open questions

1. **May a conductor create subfolders?** If yes, a conductor can spawn
   sub-conductors and the hierarchy grows itself. If no, only the person shapes
   the tree. Phase 4 is blocked on this.
2. **Does a folder conductor arm its default patrol?** `rfc-conductor-default-patrol`
   arms a patrol on bind. A folder conductor that patrols by default spends tokens
   on folders the person only glanced at.
3. **Does `default_agent` choose the conductor's agent?** The proposal fixes it to
   `kirocrew-conductor`. A folder may want a different lead.
4. **Should a person's own new chat in the folder notify its conductor?** Useful
   for awareness, but it turns every new tab into a conductor turn.
