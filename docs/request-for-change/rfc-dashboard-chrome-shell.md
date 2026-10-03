---
title: Dashboard chrome shell — one continuous surface with a fixed desktop rail
status: accepted
author: krewworker
created: 2026-10-02
last-audited: 2026-10-02
audited-at: fa37aa54f
doc-pr:
implementation-prs: [16052]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Dashboard chrome shell — one continuous surface with a fixed desktop rail

> **Status:** `accepted` on 2026-10-02 by maintainer dodgeblaster (see
> Acceptance). Nothing is on main yet; the document lands first because the
> fixed-collapsed desktop rail removes a user-facing capability and the First
> Principles review lane reads that decision off the base branch. The
> implementation is [#16052](https://github.com/kirodotdev/KiroCrew/pull/16052).

- Status: accepted — nothing implemented on main yet. The implementation is
  [#16052](https://github.com/kirodotdev/KiroCrew/pull/16052).
- Author: krewworker
- Related: `website/docs/page-layout.md` (the shell/panel layout this reshapes),
  `website/docs/theming-contract.md` (the stable class hooks it adds),
  `rfc-composable-layout-mechanism.md` (the layout data model this does not touch),
  `rfc-navigation-placement-seam.md` (nav placement, orthogonal to this)

## Summary

The desktop dashboard becomes one continuous **chrome** surface: the navigation
rail and the top bar share a single `--chrome` background, and the content area
is a bordered, top-rounded **inset panel** on top of it — one framed window
instead of three floating cards. Inside that frame the sessions sidebar docks
**flush** (square top corners, no gap below its footer, keeping only its
right-edge divider), the navigation rail leads with the **crew identity
switcher** (which replaces the old brand toggle), and the desktop rail is **fixed
to the collapsed icon rail** — the expandable rail is mobile-only.

## Screenshots

Before / after of the desktop shell are attached to this RFC's PR (dragged into
the PR description or an RFC comment in the web UI, which yields permanent
`https://github.com/user-attachments/assets/…` URLs), rather than committed into
the repo — following the mock-attachment precedent in `rfc-issue-radar-crews`.
Each `user-attachments` URL replaces one of the four slots below once the frames
are attached:

- **Shell — before:** floating cards, expandable rail, double identity mark.
- **Shell — after:** one chrome surface, fixed icon rail, single crew switcher.
- **Sessions sidebar — before:** floating rounded card with a gap below the footer.
- **Sessions sidebar — after:** docked flush, square corners, no bottom gap.

## Problem

The desktop shell reads as unfinished because its pieces do not belong to one
surface:

- The rail, the top bar, and the content area each sit on their own background,
  so there is no single framed window.
- The rail header shows a brand toggle **beside** the crew identity mark — two
  stacked avatars for one identity.
- The desktop rail is expandable, and the expand is reachable by accident: a
  nav-item click, or a Web-Preview teardown, could widen it, so selecting a page
  also moved the layout.
- The sessions sidebar floats as a rounded card with a visible gap below its
  "Older Sessions" footer, not docked into the frame.

## Decision

Adopt the chrome shell as the desktop default:

1. **One chrome surface.** The shell grid is `.grabber-shell` on the `--chrome`
   background; the content area is a bordered, top-rounded `.dashboard-surface`
   inset panel; the rail is `.dashboard-navigation` on the same chrome.
2. **Crew switcher leads the rail**, replacing the brand toggle (one identity,
   centred when collapsed). The product name moves to the window title.
3. **The desktop rail is fixed-collapsed.** The expandable rail is **mobile
   only** — on desktop the rail is always the icon rail, its toggle is removed,
   and a nav click (or a Web-Preview teardown) never widens it.
4. **The sessions sidebar docks flush** inside the surface: square resting
   corners, no bottom gap, keeping its right-edge divider.
5. **The top bar's glass pills keep their liquid-glass.** The readout capsule
   and Request-a-Feature pill wear the same Liquid Glass as the search trigger,
   so the three top-bar pills read as one consistent set on the chrome bar.

The product-shape part that needs a recorded decision is **3** — it removes a
user-facing capability (expanding the desktop rail). The rail's expand/collapse
machinery and its tests are retained for the mobile rail; only the desktop
default changes.

## Rollout

One frontend PR, [#16052](https://github.com/kirodotdev/KiroCrew/pull/16052):
the shell CSS and class hooks in `website/src/index.css`, the rail wiring in
`website/src/App.tsx` (fixed-collapsed off mobile, toggle removed, switcher in
the header), the switcher component `website/src/components/InstanceTabBar.tsx`
+ `website/src/components/CrewIdentityMark.tsx`, and the flush docked sidebar via
a `flush` prop on `website/src/components/OverlayDrawer.tsx` passed from
`website/src/pages/ChatPage.tsx`. Tests covering the previously-expandable
desktop rail are rewritten to the fixed-collapsed reality (collapsed-rail
role/aria assertions). No backend, agent, or data change.

## Alternatives considered

- **Keep the desktop rail expandable, only reskin the surface.** Rejected: the
  accidental-widen paths (nav click, preview teardown) are the main complaint,
  and an expandable rail on the fixed-width chrome frame reintroduces them.
- **A separate floating roster/sidebar card instead of docking flush.** Rejected
  by the maintainer during development: a floating card re-creates the
  "scatter of panels" look the chrome removes.
- **Hide the desktop rail labels behind a user preference rather than fixing
  collapsed.** Rejected: a per-user toggle is more surface for the same
  outcome, and the icon rail with hover labels already carries the names.

## Risks

- Desktop nav labels are hover-only (the icon rail). Mitigated: every rail item
  keeps an `aria-label`, and the collapsed rail is the established mobile shape.
- The one shared-component change (`OverlayDrawer`'s `flush` prop) could affect
  other callers. Mitigated: it defaults to the prior floating behaviour, so only
  the desktop sessions sidebar opts in.

## Acceptance

**Decided 2026-10-02 by dodgeblaster (maintainer): this design is accepted.**
The desktop nav rail is fixed to the collapsed icon rail for every user (expand
stays mobile-only), the brand toggle is replaced by the crew identity switcher,
the installed theme's rail logo gives way to the favicon, and Report-a-problem
leaves the rail (kept at Settings › About › Support and the top-bar feedback
pill). The top bar's readout capsule and Request-a-Feature pill keep their
Liquid Glass (decision 5) — reversing an earlier development iteration that
flattened them — so all three top-bar pills wear one material. This document
records the decision so the First Principles review lane can
read it off the base branch. The implementation is
[#16052](https://github.com/kirodotdev/KiroCrew/pull/16052).

