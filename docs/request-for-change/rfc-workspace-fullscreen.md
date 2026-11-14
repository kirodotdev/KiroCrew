---
title: Workspace fullscreen — one fullscreen for the workspace panel and the files it hosts
status: draft
author: Skymore
created: 2026-09-30
last-audited: 2026-11-14
audited-at: a92dde1a6c
doc-pr: 15942
implementation-prs: []
tracking-issues: [10722]
supersedes: []
superseded-by: []
---

# RFC: Workspace Fullscreen

> **Status:** `draft`. Acceptance requested from a maintainer. The direction is maintainer CrysisDeu's, recorded in [#10619](https://github.com/kirodotdev/KiroCrew/issues/10619) on 2026-09-13 when #10119 was reverted: "The parts that were justified in the review (workspace fullscreen, the single fullscreen concept, PTY-Escape handling, hairline split dividers, the title-row z-index fix) are welcome back as a separate PR backed by an issue and screenshots of every shipped state." The shape below is the one proposed in [#10722](https://github.com/kirodotdev/KiroCrew/issues/10722), including that a fullscreen workspace removes the need for a separate file fullscreen. This document writes that shape down so the First Principles lane can read it from the base branch; a maintainer accepting it confirms the shape. Nothing is on main yet. Verified at `a92dde1a6c`: `MarkdownPanel` passes `onFullscreen` to its `OverflowMenu` unconditionally, so every file open in the SidePanel offers a file-only Full screen, and its Escape handler exits that fullscreen or closes the file; a browser tab's `WebPreviewPanel` has its own Expand (`toggleExpand`), which hides the nav rail and the sessions list; `WorkspaceFullscreenContext` does not exist and nothing expands the `#activity-bar-slot` host that `App.tsx` renders on desktop; and the nav rail's Terminal row (`app.terminal`) is the pointer entry to the docked terminal. No implementation is open: [#10757](https://github.com/kirodotdev/KiroCrew/pull/10757) was closed unmerged on 2026-11-01.

## Summary

On the desktop chat page the workspace panel (the SidePanel) gets a Fullscreen control that expands the panel over the dashboard's content area with every tab still mounted. A file hosted in that panel stops offering its own file-only fullscreen, so each surface has one fullscreen action. The nav rail's Terminal row stays.

## Motivation

The workspace panel holds live work: editor drafts, browser pages and terminal sessions. The panel itself cannot fullscreen today. A file preview can, through its ⋯ menu, but that shows one file and hides every other tab. A browser tab's Expand only hides the nav rail and the sessions list, and a terminal tab has no fullscreen at all. The only ways to give the workspace more room are resizing the split or popping work out into another window.

Adding panel fullscreen beside the file's own fullscreen would put two actions named Full screen, with different scope, a few pixels apart, and would let them nest. Escape would then have three plausible meanings inside the panel: exit the file's fullscreen, exit the workspace's, or close the file. #10119 chose one fullscreen concept for this reason, and #10619 kept that choice when it reverted the rest of #10119.

## Goals

- Fullscreen the whole workspace panel in place, without remounting any tab, draft, browser frame or terminal it holds.
- One fullscreen action per surface: the panel's where the panel can fullscreen, the file's own where it cannot.
- A focused terminal keeps Escape for its running program; nested dialogs, menus, editors and annotation composers keep Escape precedence.
- The docked terminal stays reachable by pointer from every route and on the phone.

## Non-goals

- #10119's pill tabs and flattened panel frame. The fused browser-style tabs and half-card frame stay, as #10619 decided.
- Workspace fullscreen on phone viewports, and on routes other than chat. The Crew Members page mounts the same SidePanel; it gets no fullscreen context.
- An artifact open in the workspace. `ArtifactPanel` keeps its own fullscreen; this design does not change it (see Open questions).
- A browser tab's Expand. `WebPreviewPanel` keeps it; it widens the panel inside the normal layout by hiding the nav rail and the sessions list rather than covering the content area, and this design does not change it (see Open questions).
- Removing the nav rail's Terminal row.
- Moving the panel icon family (`components/icons/panels.tsx`) to Lucide.

## Design

### 1. Workspace fullscreen

The chat route on a desktop viewport provides a `WorkspaceFullscreenContext` to the SidePanel. Its Fullscreen control expands the existing panel host across the content rows; the panel's subtree is not moved or re-created, so tab order, drafts, browser state and terminal scrollback survive both directions. The 42px topbar stays visible and keeps taking input. The covered content (chat, sessions sidebar, rail) is `inert`. In Focus Mode the topbar, the rail overlay and their peek triggers stay above the fullscreen layer and interactive, and modals, sheets and banners stay above them.

Fullscreen exits through its own control, and also when the panel closes, when conversation search takes the dock, when the viewport narrows to phone width, or when the user leaves the chat route. In the bottom dock, Fullscreen is a direct button rather than a dock-menu item.

### 2. One fullscreen concept

A `MarkdownPanel` rendered under the context omits its file-only Full screen action. Where there is no context (a phone viewport, the Crew Members page) a file keeps its own full-screen action, because there is no panel fullscreen to use instead. While the workspace is fullscreen, Escape in a file does not close the file.

### 3. Escape

Nested dialogs, menus, editors and annotation composers handle Escape first. A focused terminal consumes Escape before the workspace does, so a program running in the shell keeps the key; the visible control is the exit. An Escape nothing else claims exits workspace fullscreen.

### 4. Docked terminal

The nav rail's Terminal row stays; it is the only pointer entry to the docked terminal on the phone and on every non-chat route, and the `terminal` chord ships unbound. Every docked-terminal entry exits workspace fullscreen before it toggles, so the docked terminal never opens behind the fullscreen layer.

The shape of the panel controls (the open panel's Close button, any Side-panel or Bottom-panel toggle and their names) is out of scope here and belongs in its own RFC or issue.

## Migration plan

### Phase 1: workspace fullscreen

Not started. [#10757](https://github.com/kirodotdev/KiroCrew/pull/10757), the earlier frontend-only attempt, was closed unmerged on 2026-11-01.

**Exit criteria:**

- Entering and leaving workspace fullscreen keeps the same editor, browser and terminal DOM nodes mounted; a draft typed before entering is present after leaving.
- On the desktop chat route a workspace-hosted file's ⋯ menu has no Full screen item; on a phone viewport and on the Crew Members page it does.
- Escape in a focused terminal reaches the PTY and fullscreen stays; Escape in an open menu or dialog closes only that; an unclaimed Escape exits fullscreen; Escape in a file during workspace fullscreen does not close the file.
- In fullscreen the topbar is not `inert` and accepts a click; the content grid is `inert`; in Focus Mode the peek triggers and rail overlay accept input.
- Closing the panel, opening conversation search, narrowing to phone width and leaving the chat route each clear fullscreen.
- Every docked-terminal entry (the nav rail Terminal row and the `terminal` chord) exits fullscreen and then opens the docked terminal.

## Backward compatibility

Compatible. The change is frontend only and touches no stored state, API or configuration. The one removed entry, a workspace-hosted file's Full screen on the desktop chat route, is replaced by the panel's Fullscreen in the same panel header.

## Security considerations

None beyond accessibility hygiene: fullscreen marks the covered content `inert`, so keyboard and screen-reader focus cannot reach controls the user cannot see. No new network, storage or permission path.

## Alternatives considered

- **Keep the file-only fullscreen inside the workspace as well.** Two actions named Full screen with different scope, a nested fullscreen, and an Escape with three meanings. #10119 rejected this and #10619 kept that decision.
- **Remove the file-only fullscreen everywhere.** Leaves the phone and the Crew Members page, where the panel cannot fullscreen, with no fullscreen at all.
- **Fullscreen by moving the panel to its own window.** A new window is a new document, so editors, browser frames and terminal views would remount there, and unsaved drafts would have to be carried across.
- **Remove the nav rail Terminal row.** Tried in an earlier revision of #10757 (closed unmerged). The phone and every non-chat route lost pointer access to the docked terminal, because the `terminal` chord ships unbound.

## Open questions

1. An artifact open in the workspace keeps `ArtifactPanel`'s own fullscreen, so the one-fullscreen rule covers file previews only. Folding the artifact viewer into the panel fullscreen is left for a follow-up; this document does not decide it.
2. A browser tab keeps its Expand beside the panel's Fullscreen. Whether it should give way to the panel fullscreen where the context is provided, as a file's Full screen does, is left for a follow-up; this document does not decide it.

**Direction from CrysisDeu (maintainer) in #10619, 2026-09-13:** workspace fullscreen, the single fullscreen concept and PTY-Escape handling are welcome back as a separate PR backed by an issue and screenshots of every shipped state. #10722 is that issue. Acceptance of this shape is requested from a maintainer.
