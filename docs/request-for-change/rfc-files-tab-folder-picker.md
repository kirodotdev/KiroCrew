---
title: Files tab folder picker — Project plus the folders this chat worked in
status: draft
author: bellingman
created: 2026-11-06
last-audited: 2026-11-06
audited-at: 39717a0a
doc-pr:
implementation-prs: []
tracking-issues: [11892]
supersedes: []
superseded-by: []
---

# RFC: Files tab folder picker

The side panel's **Files** tab always shows the chat's project folder, names it
only by its last segment, and cannot show any other folder. Most chats start in
the shared crew workspace and then work in repositories elsewhere, so the Files
tab shows the one folder the user is least likely to want.

This RFC adds a folder chip to the **Files** tab header, right of the title. It
shows the current folder with the leaf always visible. Clicking it shows the
full path and two groups to pick from: **Project** (the chat's project folder,
the default) and **Changes** (the folders this chat worked in).

Picking a folder only changes what the Files tab shows. It never changes the chat's
project folder. It is browsing only.

It replaces the closed feature request #11892, which a maintainer routed here
for design sign-off.

## 1. Summary

- **Files tab:** `Files  📁 …/workspace/my-repo ▾  ⋯`. The chip fills the
  empty header space between the title and `⋯`.
- **Popover:** the full path (read-only, with copy), then **Project**, then
  **Changes**. Nothing else: no Recent, Favorites or Browse, and no typed path.
- **Changes group:** every git repo this chat's tool calls worked in (edited a
  file, ran a command in, `cd`'d into, `git -C`'d), whether or not it has
  changed yet, plus the folder of each file this chat edited outside any repo.
- **Changes tab:** unchanged. It already lists every repo this chat worked in.
- **Server:** the Files tree route admits the folders the Git routes already
  admit, plus the folders of edited non-repo files. No user-supplied path is
  ever admitted.
- Ships as one change, not phases.

## 2. Motivation

### 2.1 Current state (measured at `39717a0a`)

- The Files tab's root is the slot's project folder: `SidePanel` passes
  `projectDir={projectDir ?? ''}` to `FilesHomePanel`
  (`website/src/pages/chat/SidePanel.tsx`).
- Its header shows only `basename(projectDir)`; the full path is only a `title`
  tooltip (`website/src/pages/chat/FilesHomePanel.tsx`). In the common case it
  reads `Files  workspace`.
- The server already tracks the repos a chat works in.
  `src/kiro_crew/dashboard/git_repo_touches.py` records the repo root of every
  file the agent writes, every shell command's working directory, and every
  leading `cd <dir>` / `git -C <dir>`, on the slot as `_git_repos` (capped by
  `MAX_SLOT_GIT_REPOS`). `resolve_repo_roots` never lists home or `/`, and
  drops sensitive roots.
- `GET /api/project/git/repos?slot=` (`api_project_git_repos` in
  `src/kiro_crew/dashboard/file_api/git_panel.py`) serves that list, Project
  first. The **Changes** tab renders one collapsible section per repo from it
  (`website/src/components/GitReposPanel.tsx`).
- The Git routes admit those roots through `_slot_git_repo_snapshot`
  (`src/kiro_crew/dashboard/file_api/project_dirs.py`): "server-derived from
  each session's own tool calls, never from a request, so they carry the same
  trust as a project directory the agent set itself."
- The Files tree route does **not**: `api_project_tree`
  (`src/kiro_crew/dashboard/file_api/project_tree.py`) matches only
  `_slot_project_snapshot` plus recent projects, and answers
  `403 unknown_project_dir` for anything else.

So the Changes tab can already show a repo the Files tab cannot open.

### 2.2 Problems

1. **The root is unlabeled.** `workspace` does not say which `workspace`.
2. **The folder the chat is working in is out of reach.** A chat rooted at
   `~/.kiro/crew/workspace` that works in `~/workspace/<ws>/src/<pkg>` cannot
   show `<pkg>` in the Files tab. Its Changed segment says "No changed files"
   while the transcript shows diffs. This is the main use case.
3. **There is no way to switch.** The only route is `set_project`, which
   applies at the next turn, resets context and swaps project steering. That is
   far too heavy for "let me look at the other repo".
4. **Two tabs disagree.** The Changes tab lists a repo; the Files tab beside it
   cannot open it.

## 3. Goals

- G1. The Files tab always shows which folder it shows, leaf visible at any
  width, full path one click away.
- G2. One click takes the Files tab to any folder this chat worked in.
- G3. The tab's folder changes without changing the chat's project folder.
- G4. No new trust: the Files tab admits exactly what the server already
  derives from the chat's own tool calls.
- G5. The header stays within the `max-two-buttons-per-row` rule
  (`website/AUTOSDE.yaml`).

## 4. Non-goals

- Changing the chat's project folder, steering or agent working directory.
- Any change to the **Changes** tab. It already shows one section per repo this
  chat worked in.
- Recent, Favorites, Browse, or typing a path.
- Multi-root workspaces (#7837).
- The `.kiro` / dot-folder skip set (#10225).
- Remote sessions: `git/repos` already answers empty for them
  (`session_runs_remotely` in `api_project_git_repos`), so they get Project only.

## 5. Design

### 5.1 Files tab: placement right of "Files"

| Row | Today | After |
|---|---|---|
| Header (38px) | `Files` · `workspace` · *empty* · Reveal · `⋯` | `Files` · **chip** (fills the gap) · `⋯` |
| Edited/Browse (36px) | view toggle | unchanged |
| Toolbar (40px) | All/Changed · Filter · Hidden · Refresh | unchanged |

With Reveal the header would hold three buttons. To stay at two, **Reveal
moves into `⋯`** beside Open terminal; `⋯` then renders whenever a folder is
set. The chip takes `flex-1 min-w-0` and shrinks; the row never wraps.

### 5.2 The chip

- Folder icon, then the path with the **leaf pinned**: the leaf is a `shrink-0`
  span, the leading segments a separate span that truncates at its start
  (`…/parent/`). At very narrow widths only `leaf ▾` remains. No
  `direction: rtl`, which reorders punctuation in paths.
- Home shows as `~`.
- When the folder is not the Project folder, the chip gets the accent border.
- `aria-label`: "Folder: <full path>. Change folder".

### 5.3 The popover

Opens under the chip, panel width; Escape or an outside click closes it.

1. **Full path**, selectable, with a copy control. Read-only: there is no typed
   path, because only server-known folders can be opened (G4).
2. **Project** — always first, always shown. The chat's project folder, with a
   check mark when current. Picking it is the reset.
3. **Changes** — shown when non-empty. One row per folder this chat worked in,
   most recent first, excluding the Project folder. Each row: leaf name, muted
   full path, git branch when it is a repo.

Keys: Up/Down move, Enter picks, Escape closes. Rows follow the Files app
`PathBar` suggestion rows (`apps/file-explorer/PathBar.tsx`) for look and
keyboard handling.

### 5.4 The Changes group

- Source: `GET /api/project/git/repos?slot=`, already used by the Changes tab,
  so both tabs list the same repos in the same order.
- Plus **edited non-repo folders**: when a file the agent edited is in no repo,
  its parent folder is listed. Only edits count here; a command run or `cd`
  into a non-repo folder adds nothing. Today `resolve_repo_roots` drops these.
  They are recorded on the slot beside `_git_repos` (`_edited_dirs`), under the
  same rules: home and `/` never, sensitive paths never, same cap.
- `git/repos` gains a `dirs` field for them, so one request feeds the group.
  The Changes tab ignores the field.
- The list updates as the agent works, the same way the Changes tab does.

### 5.5 What follows the picked folder (Files tab)

| Follows the tab folder | Stays on the Project folder |
|---|---|
| Tree | The chat's project folder |
| Changed segment and count | Project steering |
| Name filter and content search | Agent working directory |
| Reveal, Open terminal (in `⋯`) | `set_project` |
| Drop-to-upload target | **Edited** view (per chat, not per folder) |

### 5.6 State

- Remembered per chat slot in `localStorage` (`kc.filesPanelRoot.<slot>`).
  Survives reload; two chats do not share it.
- Cleared when the chat's project folder changes.
- A remembered folder that is no longer listed (gateway restart before the
  transcript reseeds, or the cap evicted it) falls back to Project with a
  one-line notice, never a blank tree.

### 5.7 Server change

- `api_project_tree` takes its allow-list from `_slot_git_repo_snapshot`
  plus the new `_edited_dirs`, instead of `_slot_project_snapshot`. The Git
  routes are unchanged.
- `git_repo_touches` keeps an edited file's parent folder when no repo is
  found (edit touches only, via `is_edit_call`), under the existing home, `/`,
  sensitive and length rules.
- `git/repos` returns `dirs` beside `repos`.

### 5.8 Empty state

The Changed segment's empty state names the folder: "No changed files in
`~/.kiro/crew/workspace`". When the Changes group has entries, it adds:
"This chat worked in `my-repo` — show it".

## 6. Implementation and exit criteria

One PR (frontend and server).

- At 280px panel width the Files header shows the leaf name in full; the
  header has at most two buttons.
- Clicking the chip shows the absolute path and a working copy control.
- Project is always the first row; with no touches it is the only row.
- After the agent runs `cd R && ls` in repo R (no edit), R appears under
  Changes within one `git/repos` refresh.
- After the agent edits `W/notes.md` in non-repo folder W, W appears under
  Changes. After it only runs `cd W && ls`, W does not.
- The Changes tab renders exactly as before.
- Picking R shows R's tree and R's git changes in the Changed segment;
  `slot.project` is unchanged before and after.
- A slot that never touched R gets `403 unknown_project_dir` for R from
  `/api/project/tree`.
- Home, `/` and sensitive folders never appear and are refused.
- Reload keeps the pick; changing the chat's project folder clears it.

## 7. Backward compatibility

- With no pick, the Files tab shows what it shows today.
- Reveal moves one click deeper, into `⋯`. This is the one change to behavior
  users rely on.
- `/api/project/tree` admits more folders; no response shape changes.
  `git/repos` gains one field, which the Changes tab ignores.

## 8. Alternatives considered

1. **Show the full path only, no picker.** Fixes problem 1, not 2 or 3.
2. **Picker in the toolbar row.** The row is full; it would need a third button.
3. **Chip on its own row.** Costs ~28px of tree while the header space right of
   "Files" sits empty.
4. **Recent, Favorites, Browse, typed path.** Dropped: the list is small, so
   Recent is mostly clutter, and Favorites/Browse/typing need a new
   user-recorded allow-list (a trust change tracked by #11879 and #7837).
5. **Derive roots on the client from `session-files`.** Misses repos the agent
   worked in without editing, and duplicates `git_repo_touches`.

## 9. Security considerations

- No request-supplied path is admitted. Every new folder comes from the chat's
  own tool calls on the server, the trust `_slot_git_repo_snapshot` already
  grants the Git routes.
- New exposure: a recursive file listing (not just git status) for repos the
  agent touched, and for the folders of non-repo files it edited. The agent
  already had access there, and file contents were already readable through
  `/api/browse-files`.
- Non-repo folders come from edits only, so running a command somewhere never
  makes that folder listable. Home and `/` stay excluded, sensitive paths stay
  refused, and the tree walk's existing caps apply.
- App tokens see only the folders of slots they own, as today
  (`app_owns_slot_session`).

## 10. Open questions

- **OQ-1.** Is moving Reveal into `⋯` acceptable to keep the header at two
  buttons?
