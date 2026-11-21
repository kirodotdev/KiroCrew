---
title: Fold-aware dashboard layout from the Viewport Segments and Device Posture APIs
status: draft
author: jeeshofone
created: 2026-11-03
last-audited: 2026-11-20
audited-at: cdb601611f
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Fold-aware dashboard layout from the Viewport Segments and Device Posture APIs

- Status: draft. Nothing is built. This document proposes how the web dashboard keeps content and controls off the fold of a book-style foldable phone, and asks a maintainer to accept or change the design before any of it is written.
- Tracking issue: the issue this document's PR names, which the four-edge safe-area fix closes.
- Depends on: that fix. It makes every fixed surface inset all four edges and pins that the shell keeps its state when a fold resizes the viewport, so this document only has to deal with the fold itself.

## Summary

Safari 27.1 on iPhone Duo, Apple's book-style foldable phone, ships two APIs as a developer preview behind feature flags. The **Device Posture API** reports `folded` (partly open, held like a book or a laptop) or `continuous` (flat, fully open or fully closed), through the `device-posture` media query and `navigator.devicePosture`. The **Viewport Segments API** describes the regions on either side of the fold, through the `horizontal-viewport-segments` and `vertical-viewport-segments` media queries, six `env(viewport-segment-*)` variables and `window.viewport.segments`. Chrome 138 and later ship the Viewport Segments API without a flag, Android included, so a book-style foldable running Chrome reports a fold today.

This RFC proposes using them only where they are present, to do three things: keep a centred dialog or a sheet inside one segment, split the chat page's panes at the fold (with the side panel open, the panel takes one segment and the transcript the other), and leave everything else as it is. Where the APIs are absent, nothing changes.

## Motivation

### Current state (measured at `ca98797739`)

- Nothing under `website/src/` reads either API: no match for `device-posture`, `devicePosture`, `viewport-segment`, `horizontal-viewport-segments` or `window.viewport`.
- Centred dialogs sit at the viewport centre. `Modal` (`components/Modal.tsx`) centres its dialog in a `fixed inset-0 … flex items-center justify-center` row, and `DialogContent` (`components/ui/dialog.tsx`) places its content at `left-1/2 top-1/2` with a translate. On a folded inner display the viewport centre is the fold, so both put the dialog across it.
- `SideSheet` (`components/SideSheet.tsx`) pins its sheet to the right edge with `maxWidth` from the caller. Its left edge lands wherever that width ends, which can be just past the fold.
- On the inner display in landscape (about 950px wide) the shell is in its desktop form (the `dashboard-shell` grid in `App.tsx`): nav rail, then the sessions sidebar (`SIDEBAR_MIN` in `pages/chat/sidebarWidth.ts`, 180px), then the transcript. The boundary between sidebar and transcript is wherever the user last dragged it, so a folded device can show the transcript's first column of text inside the hinge.

### Problems

1. A dialog's title, buttons or text field sit in the folding region, which Apple's design guidance describes as excluded from the usable area while the device is partly open.
2. The chat page's two panes do not line up with the two halves of the display, so one pane bends across the hinge.

## Goals

- When the page reports two side-by-side segments, a centred dialog or a sheet sits entirely inside one segment.
- When the page reports two side-by-side segments and the chat page is in its two-pane form with the side panel closed, the sessions pane ends at the left edge of the fold and the transcript starts at its right edge.
- With the side panel open beside a fold, the transcript holds the leading segment and the panel the trailing one, each inside its segment.
- Every fold rule is gated on the feature query. Where neither API is present the computed layout is byte-identical to today's. (The form-switch continuity described under [Continuity](#continuity) needs no fold API and ships in the safe-area PR; only its fold-aware resting edge is gated here.)
- One module owns the detection, so a change to the flagged API shape is one edit.

## Non-goals

- Two vertically stacked segments (the laptop pose). Phase 0 records what it reports; a later RFC can propose a transcript-above, composer-below layout.
- A layout per pose. Apple's guidance is to let the existing layout expand and to avoid large rearrangements as the device folds, and this follows it. "Book pose with the side panel open" keeps the desktop layout's panes and their order (rail, sessions, transcript, panel). The fold decides which pane has a segment, sets the panel's width, holds the dock at the right and withholds the controls a fixed width makes moot. The panel stays mounted across a fold, and the stored pin, list width and dock are kept, so flattening the device restores the layout it had before the fold.
- User-agent or device detection of any kind.
- Native-app work. This is the web dashboard only.

## Design

### Detection

CSS first, because the layout changes are CSS and a media query re-evaluates on every fold without script.

- `src/tailwind-theme.css` gains `@custom-variant fold-x (@media (horizontal-viewport-segments: 2));`, so a class reads `fold-x:…` and compiles to nothing a browser without the API applies.
- `src/styles/design-tokens.css` gains, inside that media query only:

  ```css
  @media (horizontal-viewport-segments: 2) {
    :root {
      --fold-start: env(viewport-segment-right 0 0);
      --fold-end: env(viewport-segment-left 1 0);
      --segment-0-width: env(viewport-segment-width 0 0);
      --segment-1-width: env(viewport-segment-width 1 0);
    }
  }
  ```

  The tokens are undefined outside it, so every use must carry a fallback (`var(--fold-start, 50%)`) or sit inside a `fold-x:` class.
- A hook, `useViewportSegments()`, for the few places that position by script (the shell's grid template is an inline style). It subscribes to the media query list's `change`, to `resize`, and to `navigator.devicePosture`'s `change` when present, the same way `useIsMobile` does, and returns `null` when `window.viewport?.segments` is missing or has fewer than two entries.

Device Posture alone says the device is folded but not where the fold is, so it is only a wake signal for the hook. Every layout decision reads segments.

### Dialogs and sheets

- `Modal` and `ui/dialog`: under `fold-x:` the dialog's horizontal centre becomes the centre of the leading segment (segment `0 0` in a left-to-right UI, `1 0` in right-to-left), and its width is capped at that segment's width less the existing gutter. Vertical placement does not change.
- The App-root dialogs that centre their own panel in a `fixed inset-0 flex` row (the update dialogs, the changelog) get the same rule through one shared class rather than one patch each.
- `SideSheet`: under `fold-x:` its width becomes `--segment-1-width`, so it covers exactly the trailing segment.

### The chat page split

Edges below are written for a left-to-right document. In a right-to-left one the leading segment is `1 0`: the sidebar takes the right-hand segment, `SideSheet` the left-hand one (`--segment-0-width`), and the transcript's fold padding goes on its right.

When `useViewportSegments()` reports two horizontal segments and the shell is in its desktop form, the sessions sidebar's width is set to `fold-start` minus the nav rail's width, and the transcript gets a left padding of `fold-end - fold-start` so its text starts past the hinge. The user's dragged width is kept and comes back when the device is flattened. With the sessions sidebar closed, nothing moves.

### Book pose with the side panel open

The chat page has three panes (sessions list, transcript, side panel) and a folded inner display has two segments. They are allocated in this order:

1. The transcript always keeps a segment.
2. When the side panel is on screen, it takes the other segment at that segment's reported width (`--segment-1-width` in a left-to-right document, `--segment-0-width` in a right-to-left one), and the sessions list steps aside. The list's pin and dragged width are not touched, so the list comes back when the panel closes, and flattening the device restores the layout it had before the fold. The transcript now holds the leading segment beside the nav rail, and the rail stays compact wherever an expanded rail would leave the transcript less than the chat pane's 320px minimum.
3. When the side panel is closed, [the chat page split](#the-chat-page-split) applies: the sessions list in the leading segment, the transcript in the trailing one.

So opening the panel moves the transcript from the trailing segment to the leading one, and closing it moves the transcript back. The move runs inside the same view transition as the form switch (`kc-transcript`, `kc-composer` and `kc-sessions`), and snaps under `prefers-reduced-motion: reduce`. A fold or an unfold with the panel open is a viewport resize, and keeps the form switch's own motion.

While a fold is reported:

- The panel never fills the chat column. On a narrow desktop window today the panel takes over the chat column when the list leaves too little room beside it. Beside a fold that would hide the transcript, which is the pane the allocation keeps on screen.
- The panel docks right. A bottom dock would be a row across both segments. The stored dock choice (`mc-side-panel-dock`) is read and never written, the dock toggle is not offered, and the stored choice comes back when the device is flattened. This is the same shape as the sessions list's width while folded: the fold holds the value and the stored preference is left alone.
- The panel's resize handle is not offered: the segment sets its width. Panel tabs get no fold rule of their own. Each lays out in the width the panel gives it. The web preview's expand control is not offered either, since expanding could not change the width; a preview already expanded keeps its collapse control.
- A fold gap the device reports between the segments is kept clear before the panel, so neither the panel nor the transcript lies under the hinge.
- Asking for the sessions list (the sessions toggle, the rail's chat item, the keyboard shortcut, "show in list", or a folder picked in the command bar) while the panel holds the other segment closes the panel and pins the list. The list then takes the leading segment back. Pinning a list that has no segment to go to would do nothing on screen.

Every rule above needs a reported fold. With no segments the panel, the list, the dock and the composer behave exactly as today, and the only stylesheet rule this adds sits inside the fold media query. Segment heights and the lower segment's bottom are never read. The Safari preview on iPhone Duo reports them larger than the viewport.

This follows the platform owners' guidance for a secondary pane. Apple's [Strike a pose with adaptive layouts on iPhone Duo](https://developer.apple.com/videos/play/tech-talks/111463/) splits Podcasts' Now Playing and its transcript at the fold, and when the transcript closes Now Playing stays in its own region instead of re-centring, so its controls do not move. Microsoft's [companion pane](https://learn.microsoft.com/en-us/dual-screen/introduction#companion-pane) puts the content on one screen and the tools that act on it on the other, and suggests the tools sit on the right or at the bottom.

### Continuity

Unfolding or folding resizes the viewport with the dashboard open. When the resize crosses the chat shell's two-pane threshold, the shell switches between its phone form (one column) and its desktop form (nav, sessions sidebar, chat). The [four-edge safe-area PR](https://github.com/kirodotdev/KiroCrew/pull/29144), now merged, makes that switch continuous without any fold API. On `main` the threshold is Tailwind's `md` (768px), and it stays 768px. This design adds a fold-gated two-pane form below 768px (see the last item below) and builds on the rest as shipped:

- **What moves.** The switch runs inside a same-document view transition (`document.startViewTransition`). The open chat's transcript carries `view-transition-name: kc-transcript` and the composer dock `kc-composer`. Each travels from its old box to its new one without being rescaled: it becomes the trailing pane on unfold and the whole column on fold. The sessions sidebar carries `kc-sessions`. It exists only in the desktop form, so it slides in from the leading edge on unfold and out to that edge on fold. The custom keyframes move transform and opacity only, over 300ms, ease-out.
- **What is kept.** Keyboard focus, the composer draft and the open chat stay as they were. On unfold the reading row stays in place. On fold the reading position can still move, because the transcript virtualizer's height bookkeeping collapses when the form changes (in a 40-row test chat the transcript's total height fell from about 12,200px to 4,300px with no scroll correction). That bug is on `main` today, is not caused by the transition, and gets its own fix in a separate pull request.
- **When it runs.** Only for a form change that a live resize caused (the breakpoint media query's `change`, `resize`, `orientationchange`) while the page is visible. It never runs on first load, a route change or a sidebar toggle, or for a change noticed on `pageshow` or `visibilitychange`. Where the API is missing the switch is instant, as it was before.
- **Reduced motion.** Under `prefers-reduced-motion: reduce` there is no transition at all, and the stylesheet sets `animation: none` on every transition pseudo-element in case one starts.
- **Right-to-left.** The leading edge is the inline-start edge: left in a left-to-right document, right in a right-to-left one. The script reads the document direction and the stylesheet picks the keyframe for that edge.
- **With viewport segments.** When `useViewportSegments()` reports two horizontal segments, the sidebar's resting edge is `fold-start` and the transcript starts at `fold-end`, as in [The chat page split](#the-chat-page-split): the slide ends where Phase 3 places the sidebar, and with the side panel closed the transcript's morph target is the trailing segment (with it open, the leading one; see [Book pose with the side panel open](#book-pose-with-the-side-panel-open)). #33546 places the sidebar there; the transcript's fold padding waits on Phase 3.
- **Which poses switch.** The global two-pane breakpoint stays Tailwind's `md` (768px) for the chat shell and every other component. Below 768px the chat shell takes its two-pane form only when the fold query matches two horizontal segments (`(horizontal-viewport-segments: 2)`), so only a folded inner display gets it. Unfolding to the inner display in landscape (about 951px) switches and animates at the existing breakpoint. Unfolding to the inner display in portrait (about 669px wide) switches and animates only where the fold query reports two horizontal segments, and in that form the nav rail stays compact so the sessions sidebar and a usable transcript fit. The outer display and each window of a split view (about 382px) stay in the phone form.
- **Windows without a fold.** A window 640px to 767px wide on a device with no foldable, such as a narrow desktop window, a tablet in portrait or a split view, is unchanged: it keeps today's one-column phone form. Lowering the global breakpoint is not part of this design; if it is wanted, it needs its own proposal and UX review.

## Migration plan

Each phase is planned as one PR, and each can be abandoned without undoing the ones before it; #33546 below combines parts of two.

They merge in order: Phase 0's table (#33463), then #33546, then Phase 3b. #33546 carries Phase 1's hook together with the fold two-pane form and the list half of Phase 3 (the sessions list ending at the fold), which is the panel-closed state Phase 3b relies on. The Phase 3b PR carries #33546's commit, so merging it would land #33546 too; it waits for #33463 and #33546 to merge first. Both code PRs were opened before the Phase 0 table landed, and they wait on it to merge. Accepting this RFC, and with it changing `status`, `implementation-prs` and the "Nothing is built" line above, is the maintainers' step, so this document keeps `status: draft` until they take it.

### Phase 0: probe (no code on main)

On the foldable simulator, with both feature flags on, record for every pose: the two media queries, the six `env()` values for each segment, `window.viewport.segments`, and `navigator.devicePosture.type`. Write the table into this document.

Exit criteria: the table is in this document, and Open question 1 is answered from it. No Phase 1 code is opened until the table has landed in this document.

### Phase 1: detection primitives

The custom variant, the tokens, the hook and their tests. No surface uses them yet.

Exit criteria:

- A unit test with `matchMedia('(horizontal-viewport-segments: 2)')` mocked to match, and `window.viewport.segments` mocked to two rects, gets those rects from the hook. With either mock absent the hook returns `null`.
- A static guard fails any `env(viewport-segment-` read outside the media query block, and any `--fold-*` or `--segment-*` read with no fallback outside a `fold-x:` class.
- The compiled CSS of a build without the flag is unchanged apart from the new `@media` block.

### Phase 2: dialogs and sheets stay in one segment

Exit criteria: a test per surface (Modal, ui/dialog, SideSheet, one App-root dialog) asserts the `fold-x:` classes are present, and the PR carries simulator screenshots of each, folded and flat.

### Phase 3: chat page split at the fold

Exit criteria: with the hook mocked to two segments, the sessions sidebar's computed width equals `fold-start` minus the rail width and the transcript's left padding equals the fold width; with the hook returning `null` both equal today's values, and a 640px to 767px window keeps the phone form. Simulator screenshots, folded and flat.

### Phase 3b: the side panel in book pose

Exit criteria: with the hook mocked to two segments, an open panel's width equals the trailing segment's (both halves of a Safari tab, and the web app's unequal halves, in both directions), the sessions list is off screen with its pin unchanged, the fill width is not used and the dock reads right with the stored dock unwritten; with the hook returning `null` all of them equal today's values. A browser test with an emulated fold asserts that the transcript and the panel each sit inside one segment and that nothing crosses the fold. Simulator screenshots in book pose with the panel open and closed, in a Safari tab and in the Home Screen web app.

## Backward compatibility

None of this applies unless the media query matches. Chrome 138 and later match it on a foldable without a flag, Android included; Safari needs its feature flags. The flagged Safari APIs may still change. Keeping detection in one variant, one token block and one hook is what limits a change to a single place.

## Security considerations

None. The APIs report geometry only. Nothing is stored, sent or logged.

## Alternatives considered

- **Do nothing until the APIs ship unflagged everywhere.** The four-edge fix already makes the dashboard usable on the device. This RFC can wait on Phase 0 for as long as needed; writing it now records the shape so the probe answers specific questions.
- **Device Posture only.** It says the device is folded, not where the fold is or how wide it is, so it cannot place anything.
- **Script-only, from `window.viewport.segments`.** Every dialog would have to measure and re-measure on resize. A media query does the same with no script and no layout pass.
- **Keep the transcript in the trailing segment when the panel opens, and swap the sessions list for the panel in the leading one.** The transcript would not move, but the panel would sit left of the chat, the reverse of the desktop column order (rail, sessions, transcript, panel), and flattening the device would then move the panel across the chat. The allocation above keeps that order, the panel stays on the right as Microsoft's guidance suggests for tools, and the transcript's move uses view-transition names that already exist.
- **Treat a folded inner display as two phone viewports.** That is the large rearrangement Apple's guidance tells apps to avoid, and it would change what is on screen every time the hinge moves. The book-pose allocation does not do this: there is one page and one desktop layout, and a segment is a width that layout's existing panes are given. The panel stays mounted across a fold; the list steps aside and comes back with its pin. While the panel holds the other segment, its resize handle and the preview's expand control are not offered (a preview already expanded keeps its collapse control) The dock toggle is not offered beside any fold, because a bottom dock would span both segments.

## Open questions

1. Does `env(viewport-segment-*)` report the fold as a gap between segment `0 0` and `1 0`, or do the segments meet at the centre line? Phase 0 answers it; the transcript padding in Phase 3 depends on it.
2. Which segment should a dialog use: the leading one, or the one holding the control that opened it? The leading one is proposed because it needs no knowledge of the opener.
3. Should a dialog opened while flat move when the device folds, or stay put until it closes? Apple's system alerts move; moving a dialog under the user's finger may be worse.
4. Does the dashboard want anything for the laptop pose (two stacked segments), for example the composer on the lower half?
5. How does the page's view transition line up with Safari's own window resize on iPhone Duo? The page sees the new viewport only once Safari has resized it, so the transition starts from the old form already laid out at the new width. Whether Safari animates its window change, and whether the two motions read as one or as two steps, is to be answered from a simulator capture of the unfold and the fold.
