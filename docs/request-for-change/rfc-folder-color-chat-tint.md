---
title: A chat pane takes a faint wash of its folder's color
status: draft
author: gurumaia
created: 2026-11-12
last-audited: 2026-11-12
audited-at: d9839eee5d
doc-pr: null
implementation-prs: [35932]
tracking-issues: [35870]
supersedes: []
superseded-by: []
---

# RFC: A chat pane takes a faint wash of its folder's color

- Status: draft. Nothing is on main. This one-pager is the design write-up issue
  [#35870](https://github.com/kirodotdev/KiroCrew/issues/35870) (`tier:T3`) asks for
  before building.
- Tracking issue: [#35870](https://github.com/kirodotdev/KiroCrew/issues/35870)
- Reference implementation: [#35932](https://github.com/kirodotdev/KiroCrew/pull/35932)
  (draft). It is there to make the proposal concrete; it does not merge before this
  document is decided.

## Problem

People who run several projects at once keep one sidebar folder per project. Once a
chat is open, nothing in the chat pane says which project it belongs to: every pane
looks the same, so it is easy to answer in the wrong project's session and hand the
wrong context to the wrong agent.

A folder already has a color. The person picks it in Folder settings, and today it
shows only on folder glyphs (the sidebar, the board, menus, Folder settings), never
across the chat pane.

## Current behavior

Verified at `d9839eee5d`:

1. A chat folder stores an optional `color`, one of the 12 values in
   `FOLDER_COLOR_PALETTE` (`website/src/components/folderColorCatalog.tsx`). The
   gateway refuses any other value (`_FOLDER_COLOR_PALETTE` and
   `_is_valid_folder_color` in `src/kiro_crew/dashboard/chat_folders_icons.py`).
2. The color paints folder glyphs only, through `folderColorStroke` and
   `folderColorWash` (`website/src/components/folderColorPaint.ts`), whose one caller
   is `FolderGlyph`: the sidebar rows, the board, the move-to-folder menu
   (`FolderMoveSubmenu`) and its undo bar (`MoveUndoBar`), and Folder settings. The
   folder chip a chat message shows for a folder path (`FolderChip`) draws a plain,
   uncolored icon.
3. Inside the chat pane, a folder appears only in short-lived notices above the
   composer: the `FolderSuggestionCard` and the `folder-project-hint` that says the
   session's project differs from the folder's (`useComposerAboveBand` in
   `website/src/pages/chat/page/composerDock.tsx`). Nothing marks the pane as a whole
   with its folder. The pane's one lasting identity color is the per-session color on
   its header band (`headerBandStyle`, used in `website/src/pages/ChatPage.tsx` and
   `website/src/components/ChatPane.tsx`).
4. In Split View (`dashboard.session_grid`, read as `splitFeatureEnabled` in
   `website/src/pages/chat/page/pageEnvironment.ts`), `SessionGridView` renders one
   `ChatPane` per session inside the single-chat pane of `ChatPage.tsx`.

## Proposal

| Session filed in | Chat pane background |
|---|---|
| a colored folder | the theme's `--bg` with a small share of the folder's color mixed in |
| a subfolder with no color | the same, using the nearest colored ancestor |
| an uncolored folder chain, no folder, or a Bot-managed folder | the theme's `--bg`, unchanged |

1. The tint is mixed from the theme's own background (`color-mix` of the folder color
   into `--bg`), so every theme, built-in or installed, and both polarities keep their
   own canvas, and text keeps the contrast the theme was designed for.
2. The pane redefines `--bg` for its subtree, so the header, the composer's fade and
   every `bg-bg` surface inside the pane sit on the same tinted canvas.
3. In Split View each pane shows only its own session's folder color.
4. The per-session header color stays as it is. A session color says which chat; the
   folder tint says which project.
5. It reads the folder list the sidebar already loads, so it adds no request, no new
   field and no stored data. Recoloring a folder re-tints its open chats at once.
6. The reference implementation uses a 16% share.

## What this changes for people

Anyone who has already colored a folder sees every chat in it change color after
upgrading, without having asked for it. That is a change to how the chat pane looks
by default, which is why it needs a maintainer's decision. See open question 1.

## Alternatives considered

- **A per-folder background image.** Needs storage, upload limits, a security review
  of the upload path and a readability overlay. A color answers "which project am I
  in" without any of that.
- **Tint only the chat header.** The header already carries the per-session color
  band, and a thin strip is easy to miss.
- **Do nothing; rely on the sidebar.** The sidebar is often collapsed, and in Split
  View several projects are on screen at once with no sidebar row beside each pane.

## Open questions

1. **On by default, or opt-in?** The reference implementation is always on, with no
   setting. An opt-in switch (for example Settings > Display, "Tint chats with their
   folder's color", off by default) would leave every existing install looking
   exactly as it does today, at the cost of a settings row and its translations. The
   author leans toward opt-in if maintainers see the default change as a concern.
2. **How strong?** 16% reads clearly on light and dark built-in themes. A lower share
   is safer for high-contrast and installed theme packs, which have not been checked.
3. **Translucent panels.** With Settings > Display > Translucent panels on, the
   composer's glass lets the tint show through, so the composer takes the color too.
   With it off, the composer stays opaque like the sent messages. Is the translucent
   behavior acceptable, or should the composer always stay neutral?
4. **Bot-managed folders.** The proposal leaves them untinted, because the gateway
   manages them for the person. Should a Bot's home folder be tinted after all?

## Risks

- A theme or pack that draws something behind the chat pane would have it covered
  by a tinted pane. Nothing in core does that today.
- Installed theme packs and high-contrast themes are unverified at the proposed
  strength.

## Rollout

1. This document. Exit: on main with `status: accepted`, recording the answers to
   the open questions.
2. [#35932](https://github.com/kirodotdev/KiroCrew/pull/35932), adjusted to those
   answers. Exit: a chat in a colored folder, a subfolder with no color, an unfiled
   chat and a Bot-managed folder each get the background the table above names; in
   Split View two panes from different folders each show their own; each case is
   covered by a test.
