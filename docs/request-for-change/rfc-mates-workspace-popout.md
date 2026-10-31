---
title: Mates workspace pop-out
status: draft
author: solnikhil
created: 2026-10-31
last-audited: 2026-10-31
audited-at: 83544ff4a7fb533c5292d10a7d4584bc4bd1d601
doc-pr:
implementation-prs: []
tracking-issues: [26375]
supersedes: []
superseded-by: []
---
# RFC: Mates workspace pop-out

- Status: draft — design proposal only; no implementation in this change.
- Author: solnikhil
- Tracking: [#26375](https://github.com/kirodotdev/KiroCrew/issues/26375).
- Related: [app-page pop-outs, #22886](https://github.com/kirodotdev/KiroCrew/issues/22886).
- Decision requested: accept the workspace boundary, state-transfer contract and
  reuse of existing Mates presentation before implementation begins.

## Summary

Let the user detach the complete Mates workspace into one coordinated window at
`/popout/members?member=<name>`, keep working in the dashboard, and return without
losing the selected mate or unfinished work. Preserve the existing Mates UI and
the active theme. This adds window placement, not a second implementation of Mates.

The tracking issue is parked and classified `tier:T3`. This RFC requests design
agreement; it does not assume roadmap priority or authorization to implement.

## Motivation and current state

Mates have standing conversations and operational context worth keeping visible
beside ordinary chat. Switching pages hides one workspace; another full dashboard
duplicates navigation without expressing which window owns the Mates view.

The following evidence was read from upstream commit
`83544ff4a7fb533c5292d10a7d4584bc4bd1d601`, rather than inferred from an older checkout:

- `website/src/App.tsx`, `App` mounts the pop-out route branch and registers
  chat, artifact and terminal frames. That branch has no Mates frame.
- `website/src/pages/members/MembersPage.tsx`, `MembersPage` composes the existing
  Mates surface. `useMembersPageNavigation.ts`, `activate` opens a member and
  remembers its exact name. Use that identity, not an assumed slug or new identifier.
- `website/src/pages/members/useMembersPageLayout.ts`, `useMembersPageLayout`
  folds the roster into the switcher and retains `ROSTER_PINNED_KEY` in per-tab
  session storage. A new window needs an explicit layout handoff rather than
  assumed sharing.
- `website/src/pages/members/MembersPageSidePanel.tsx`, `useMembersPageSidePanel`
  owns standing Dashboard/Files views; Profile and schedule editing have their own
  existing surfaces and guards. A pop-out must retain these entry points, not
  replace them with the mockup's illustrative four-tab arrangement.
- `website/src/pages/members/CrewComposer.tsx`, `CrewComposer` reuses `ChatInput`,
  omitting session chrome. The pop-out must keep that composition.
- `website/src/utils/popoutController.ts`, `openPopout`, `bringBack` and
  `forwardToMain` own window opening, return and navigation forwarding. The current
  bring-back path can close a direct handle before any feature-specific draft
  handoff is acknowledged. Its lifecycle cannot alone guarantee lossless transfer.
- `website/electron/window-state.js`, `sanitizeWindowState` sanitizes persisted
  main-window bounds. This is useful geometry logic, not evidence that Mates child
  bounds persist.

## Goals

- Keep Mates and ordinary dashboard work visible in separate windows.
- Offer one Mates workspace per gateway origin and browser profile, with explicit
  mate selection and a reliable route back to one dashboard.
- Preserve drafts, attachments, unsaved-edit guards, selection and layout.
- Share lifecycle infrastructure with other pop-outs, including the app-page
  proposal, while keeping Mates rendering and state owned by Mates.

## Non-goals

No new mate runtime, launch-page default, autonomous-work controls, notification
system, theme, mandatory three-column layout or single-mate-per-window mode.
No broader permissions, automatic prompt submission or automatic reopening on app
startup. General app-page support remains separately reviewable under #22886.

## Design

### Presentation and navigation boundary

Reuse `MembersPage` and its shared components beneath a shell-free Mates frame.
Keep the mate switcher, optional pinned roster, conversation, composer, existing
Profile entry points and side panel. Remove dashboard-wide navigation and chrome.
Add a labelled Return to main control beside existing header actions. Follow the
current responsive layout, focus management, translations and theme tokens.

![Illustrative Mates pop-out using the current black theme](https://github.com/user-attachments/assets/5d836418-ae75-4e06-b2bc-105c8d92bd48)

This AI-generated concept uses the running app as a visual reference. It is not
an implemented screen or a new theme. Its panel tabs/content are illustrative;
the implementation retains current information architecture. Black reflects the
selected theme only. The roster is collapsed in this example and remains pinnable.

Changing mates, opening Profile, editing a mate's goals/schedules, viewing its
Dashboard/Files and using existing Mates-local dialogs stay in the workspace.
Worker sessions, global Settings, global schedule pages and artifact detail pages
go to one main dashboard. Existing external-link rules remain authoritative.

Forward exits through the shared navigation-intent mechanism, preserving path,
query, fragment, session selection and staged composer text without submitting it.
Handle imperative navigation, links and browser history through the same boundary;
a stray route must not mount dashboard chrome inside the pop-out.

### Identity and ownership

Use one stable feature/controller identity for the workspace, separately from
the selected mate. A generic open action focuses the live window. An explicit
open-on-mate action selects the requested name after the destination's existing
unsaved-edit guard permits it. Guard cancellation leaves its selection unchanged.

Prefer the opener as return destination while it is available. Otherwise use the
existing claim handshake to choose one dashboard. The main Mates route displays
Popped out, Focus Mates window and Bring back to main, leaving global navigation
usable. Other dashboards show the same liveness state without becoming additional
editable owners automatically.

Named-window deduplication and discovery are hints, not a cross-window lock.
Concurrent opens must converge through a claim/generation handshake: one frame
owns the workspace; a losing frame cannot overwrite its drafts and offers focus
or return. Missing or unsupported coordination leaves the original page usable
with an explicit recovery action rather than pretending transfer succeeded.

### Lossless handoff

Treat detach and return as acknowledged transfers, with a unique transfer id,
source/destination ids and an ownership generation. Keep all transfer state
scoped to the gateway origin and current authenticated client context.

1. Run existing dirty-edit guards. Save, discard or cancel through their existing
   contracts. Do not serialize component internals or bypass schedule guards.
2. Persist the outgoing composer draft through its existing owning draft store.
   Transfer a versioned reference/snapshot for selection, draft revision, active
   panel and layout, using exact mate/session identities.
3. Let the destination restore that generation and acknowledge it. Repeated
   delivery of a transfer id is idempotent; stale generations cannot replace a
   newer edit. Resolve conflicting drafts visibly instead of last-writer-wins.
4. Only after acknowledgement may the source release its editable view or close.
   On timeout, keep the source usable with retry/cancel. Do not invoke the current
   immediate-close bring-back path before the Mates guard and transfer complete.

Draft text and attachment bytes never travel in URLs. Reuse shared attachment
handles where valid; attachments held only in source memory must be made durable
through the existing attachment path before acknowledging, or block transfer with
an explanation. If there is no suitable store/ownership seam, Phase 0 must record
the gap and proposed minimal extension here before implementation proceeds.

Restore scroll position when it does not override an intentional destination
scroll. Persist drafts and safe UI state before unload; OS close cannot wait for
an asynchronous transfer. Existing save/discard protections apply to dirty forms.
Closing a window never cancels backend work. Reconnect/reload restores from the
owning stores without replaying sends, creating schedules or starting runs.

### Recovery and desktop

Copied/reloaded pop-out URLs retain their shell. Resolve selection through the
existing member-navigation rules and gone-member notice; do not silently send a
recovered draft to a substitute mate. With no main window, an exit opens its
destination in a full dashboard. A blocked fallback offers an explicit link.
Return either reaches a dashboard or visibly offers the full Mates view when
focus/close is refused. Do not auto-submit recovered or staged text.

Reuse liveness discovery and its background/sleep handling. Clear stale indicators
when closure is established, and rediscover after resume. Use existing event and
notification delivery paths; moving a view must not create a second producer or
duplicate a notification. No new event bus is proposed.

Electron must allow only the intended same-gateway pop-out route under existing
window security rules. Preserve the current refusal for desktop remote crew panes
(`popoutController.ts`, `openPopout` checks `isDesktopAppPane`); do not open a tokenless tunnel URL in a system browser.
Persist the workspace's normal bounds under a separate feature key, reusing the
existing geometry sanitizer and correcting off-screen positions after display
changes. Do not promise browser-controlled size/position persistence. Native
close/quit follows current app conventions without stopping backend work.

## Migration plan

Implementation waits for maintainer acceptance of this RFC. Each phase can be
reviewed separately and does not require shipping later phases.

| Phase | Scope | Exit criteria |
|---|---|---|
| 0: verify seams | Audit draft/attachment ownership, dirty guards, navigation exits, current app-page work and Electron allowlists at the implementation base. Record findings and exact seams in this RFC. | The handoff store and conflict policy are identified; no speculative persistence or notification rewrite remains. Any required contract change is agreed before phase 1. |
| 1: web workspace | Mates frame, stable identity, guarded state transfer, navigation forwarding, liveness, theme/layout reuse and recovery. Add only the shared-controller hooks required by this flow, with defaults preserving other pop-outs. | Browser journey detaches/returns without losing work; simultaneous opens converge; refused operations remain usable; existing pop-outs pass regression checks. |
| 2: desktop placement | Register the native workspace window and persist/sanitize its geometry. | Reopen restores bounds; a disconnected monitor cannot hide the window; route and remote-pane restrictions remain enforced on macOS/Linux/Windows. |

Before the entry control ships on any surface, its full applicable lifecycle must
work there. Do not expose a web-only path in Electron before phase 2 validates it.
Update owning Mates, window-lifecycle and layout documentation alongside code and
update this RFC's status/implementation references as work lands.

## Backward compatibility

The dashboard remains the default experience. Existing `/members` links retain
their selection/landing behavior. Add `/popout/members` without replacing routes,
changing session identity, modifying backend agent configuration or changing
existing chat/artifact/terminal controller defaults. No transcript migration or
provider change is required by this proposal.

## Security considerations

Window coordination is not authentication. Validate message types, same-origin
destinations, member identity and authenticated context; authorize mutations at
the existing server boundaries. Clear sensitive in-flight state on logout or
context change and scope persisted drafts using the existing draft-store policy.
Do not put credentials, draft content or attachment data in URLs or logs. Preserve
Electron isolation, opener/external-link restrictions and existing approval gates.

## Validation

Controller tests must cover simultaneous opens, owner disappearance, stale and
repeated acknowledgements, conflict cancellation and dirty-form refusal. Component
tests cover per-mate drafts, attachments, roster/panel state and unavailable mates.
Browser E2E covers detach, edits, return, main-window close, forwarded exits, reload,
history, popup refusal and recovery. Assert single-send/single-schedule effects.
Check existing pop-outs, theme/language switches, keyboard/focus behavior and the
supported narrow layout. Verify Chrome/Firefox/Safari/Edge and desktop lifecycle on
macOS/Linux/Windows; use deterministic lifecycle events rather than sleep-based
tests. Follow the repository's testing and browser E2E conventions.

## Alternatives considered

- Another dashboard tab works today, but duplicates global UI and has no workspace
  handoff. It remains a fallback, not the proposed coordinated experience.
- A single-mate chat window omits roster and operational context.
- A Chat/Mates mode switch still replaces one workspace with the other.
- Split view is separately useful but does not serve second-monitor placement.
- A universal page framework up front risks coupling this change to every app's
  dirty state. Share the lifecycle seam with #22886 without making all pages adopt
  a Mates-specific state model or delaying this proposal for a platform rewrite.

## Open questions for maintainers

1. Accept one workspace identity and the existing selector/pinnable roster, rather
   than independent mate windows or a redesigned mandatory roster column?
2. Accept acknowledged handoff as the prerequisite for lossless return, with the
   exact draft-store seam recorded by phase 0 before coding it?
3. Is #22886 building a shared page/window seam that this should consume? Phase 0
   must settle ownership before adding a competing abstraction.
4. Accept separate web and desktop implementation PRs, keeping the desktop entry
   hidden until native lifecycle verification is complete?
