---
title: A small game on the screens where the local dashboard waits for its gateway
status: draft
author: jeeshofone (drafted with an agent session)
created: 2026-11-19
last-audited: 2026-11-19
audited-at: 301f9abd8b0
doc-pr: null
implementation-prs: [39420]
tracking-issues: [39419]
supersedes: []
superseded-by: []
---

# RFC: A wait-screen game for the local dashboard

Status: draft. Nothing is built on main and this document changes no code. It answers [#39419](https://github.com/kirodotdev/KiroCrew/issues/39419) and the First Principles block on [#39420](https://github.com/kirodotdev/KiroCrew/pull/39420#issuecomment-7005098912), which implements it. Acceptance is requested from a maintainer, who records it on this Status line. Code references are to main at `301f9abd8b0` and to #39420's head `fb70369f5b`.

## Summary

When the dashboard on this machine waits for its gateway, it shows a spinner and a few lines of status text until the gateway answers. #39420 adds a small laundry-folding game below that text. The game appears on its own after 30 seconds, or at once when the person presses Space while no control has focus. It goes away when the wait ends.

Showing new content on a wait screen without anyone opting in changes a default, and `CONTRIBUTING.md` sends a changed default to an RFC first. This document asks a maintainer to decide whether the game is on by default, and proposes a setting to turn it off.

## Motivation

### Current state

These screens wait on the gateway of a loopback dashboard:

- The host-offline cover in `website/src/App.tsx` ("The gateway isn't running on this machine."). It shows while the gateway is stopped, for as long as it stays stopped.
- The update overlay's reconnect step (`UpdateOverlay` in `App.tsx`), shown while an in-app update restarts the gateway.
- The update dialog's restart wait (`website/src/components/UpdateFoundModal.tsx`).

Each shows its status text and a spinner, with nothing else on the page. A restart takes seconds. A stopped gateway can keep the cover up for minutes or until the person acts.

### Problem

A long wait on a static page looks like a hang. That is when a person is most likely to close the tab or restart things by hand, which can interrupt an update that was about to finish. The status text already says what is happening. The page offers nothing to do while it stays true. Browsers handle the same moment on their offline page with a small game.

## Goals

- Give the person something to do during a long wait on this machine's own dashboard.
- Change nothing for a short wait: a restart that finishes inside 30 seconds looks exactly as it does today.
- Keep each screen's status text and actions where they are, above the game.
- Add no request, storage, telemetry or new dependency.

## Non-goals

- Dashboards reached through a tunnel, a relay Crew window, or any non-loopback origin. They get no game, as they get no host-offline cover today.
- The desktop app's own splash page (`website/electron/loading.html`). It is a separate `file://` page outside the web bundle; it could get the game in a later PR with its own review.
- The first load of a dashboard whose gateway is already down with nothing cached. The browser's own error page shows, as today.
- Any other game, a score that persists, or a game anywhere outside a gateway wait.

## Design (as #39420 builds it)

- **Component.** `WaitGame` (`website/src/components/laundry/WaitGame.tsx`) renders inside each of the three screens, below their text. Before it appears it renders nothing and starts no game loop.
- **When it appears.** After `WAIT_GAME_DELAY_MS` (30 000 ms), or at once on Space. Space counts only when focus is on the page body, not on a control or editable field, so it never presses, types into or scrolls something the person is using. The key listener runs in the capture phase, because the update dialog stops key events from bubbling.
- **Where it appears.** Only when `isLoopbackHost(window.location.hostname)` is true. That covers a browser on localhost and the desktop app, which loads the dashboard from a loopback origin.
- **What it is.** A canvas game (`engine.ts`, `geometry.ts`, `laundry.css`): drag clothes to the table, tap to fold, stack them on the pile. Now and then the folded sheet at the bottom of the pile turns into the Kiro ghost, read from `assets/kiro-ghost-mark.svg`, and floats away. A **Start over** control (a `<Btn>`, also R) resets the count. It runs its own frame loop, like `MissionControlScene`, so it does not use Framer Motion.
- **Bundling.** The game ships in the main bundle. While the gateway is down a lazily loaded chunk could not be fetched, and a failed chunk load makes the dashboard reload itself.
- **Isolation.** It makes no fetch, XHR or WebSocket call and reads or writes no storage. A test checks both the calls at runtime and the source for their names.
- **Text.** 25 catalog keys (`app.laundry_*`) in every locale under `website/src/i18n/locales/`.
- **Changes to the screens around it.** The update overlay becomes a modal that keeps focus and page shortcuts inside it. The overlay, the update dialog and the host-offline cover scroll on a short window so the game never pushes their text or actions off screen.
- **Ending.** The game unmounts with its screen. `stop()` cancels the frame loop and removes every window listener.

## The decision asked of a maintainer

Should the game appear on its own after 30 seconds, with no opt-in?

This RFC recommends **yes, on by default after 30 seconds, with an opt-out setting added to #39420 before it merges** (option 1 under Opt-out setting below).

The reasons for on by default:

- The people who most need it are the ones who do not know it exists. An opt-in setting would be found only by people who had already sat through a long wait.
- 30 seconds is well past a normal update restart, so the common case is unchanged.
- It shows only on the person's own machine, never to someone viewing a shared or tunnelled dashboard.
- It adds content below the status text and removes nothing, so the screen still says what is wrong.

## Alternatives considered

- **Opt-in only (a setting, default off).** Rejected: almost nobody turns on a setting for a screen they hope never to see, so the game would rarely appear where it helps. The opt-out below gives the people who do not want it the same control.
- **A visible "Play while you wait" button, with no auto-show.** The strongest alternative: nothing appears unasked, and a person sees the offer. Rejected as the default because the button is new chrome on every wait, including short ones, where today there is none, and it still needs a click from someone who may have stepped away. If the maintainer prefers no unasked content, this is the shape to choose, and #39420 can switch to it by replacing the timer with a button; the game itself does not change.
- **Space only, no timer.** Rejected: a hidden key nobody is told about is the same as not shipping it.
- **No game.** Keeps the wait screens as they are. Rejected because the problem above stays, but it is a reasonable answer if the maintainers want wait screens to carry status only.

## Opt-out setting

Proposal: one dashboard setting, **Show a game while the dashboard waits for its gateway**, default on, in Settings (the follow-up card's setting sits in `website/src/pages/settings/ChatPanel.tsx`). When it is off, `WaitGame` renders nothing, the timer does not start and Space does nothing.

The setting must be readable while the gateway is down, so it lives in the browser's `localStorage`, the way the follow-up card's setting does (`FOLLOWUP_START_SENDS_KEY` in `website/src/pages/chat/followupStartSends.ts`). That is a read of one key by the wait component, not by the game, so the game's own no-storage rule still holds.

#39420's head `fb70369f5b` has no such setting. The maintainer is asked to pick one of these, and this RFC recommends the first:

1. **Accept, with the setting.** The setting is added to #39420 before it merges, and #39420 does not merge without it. Recommended: the First Principles finding names the missing off switch, and an on-by-default feature should arrive with its off switch.
2. **Accept without an off switch.** The maintainer records on the Status line that the game ships on by default with no setting, and #39420 merges as it is.
3. **Choose the button alternative.** #39420 replaces the timer with a "Play while you wait" button before it merges.

Whichever is picked, the maintainer records it on the Status line, and the front matter `status` and the README row move from `draft` to `accepted` when this document merges, so the First Principles lane can read the decision off the base branch.

## Accessibility

- **Keyboard.** Invisible buttons that the game loop keeps over each drawn item let a keyboard user reach each item with Tab and pick it up with Space. The arrow keys then choose the basket, table or pile, Enter folds and Escape cancels. R starts over. A keyboard-only game can fold, stack and reach the ghost (tested).
- **Screen readers.** The canvas is `aria-hidden`. The game is a labelled group with a help line, each item button has a spoken name from the catalog, and results go to one polite live region. The host-offline cover itself stays a non-live region, so the game does not make the cover announce.
- **Reduced motion.** Under `prefers-reduced-motion: reduce` an idle scene requests no frames and the message toast does not animate.
- **Focus.** Space does not start the game from a focused control, so it never takes a key press meant for something else.

## Tests

From #39420:

- `components/laundry/WaitGame.test.tsx`: nothing renders and the engine does not start before 30 s; it starts at 30 s or on Space; Space on a focused control is ignored; no game on a non-loopback host; unmount stops the engine; labels come from the catalog.
- `components/laundry/engine.test.ts`: reduced motion requests no idle frames; `stop()` removes every listener; a keyboard game folds, stacks, counts and reaches the ghost; a screen reader's plain click works; no network or storage call, at runtime or named in the source.
- `test/App.offlinePill.test.tsx`, `test/AppRootCoverage.test.tsx`, `components/UpdateFoundModal.test.tsx`: the game on each of the three screens, below their text, gone on reconnect; the update overlay's focus trap.
- `shell/RelayReconnectingView.test.tsx`: the relay window gets no game.

The opt-out setting, if accepted, adds one test per screen that the game stays absent with the setting off.

## Rollout and reversibility

The game ships in the next release once #39420 merges. It persists nothing on users' machines (the opt-out writes one `localStorage` key that an older build ignores) and changes no API, config file or data format. A `git revert` of #39420 removes it fully, so this is a two-way door. Changing the 30-second delay, or switching to the button alternative, is a one-line or one-component change.

## Security considerations

None new. The game runs only in the page, makes no request, reads no user data and is shown only on loopback origins. The update overlay's new focus trap stops page shortcuts from reaching the dashboard behind it during an update, which narrows what a key press can do.

## Open questions

- On by default after 30 s, the button alternative, or neither?
- Which of the options under Opt-out setting: with the setting (recommended), without one, or the button?
- Should the desktop app's splash page get the game later, under this decision or a new one?
