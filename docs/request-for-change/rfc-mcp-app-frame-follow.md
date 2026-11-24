---
title: A growing MCP App frame stops carrying a following reader past its top
status: draft
author: christopher-h-johnson
created: 2026-11-24
last-audited: 2026-11-24
audited-at: 54caa49074
doc-pr: null
implementation-prs: []
tracking-issues: [39933]
supersedes: []
superseded-by: []
---
# RFC: MCP App frame growth and transcript follow

Status: draft. Nothing is built and this document changes no code. It answers
[#39933](https://github.com/kirodotdev/KiroCrew/issues/39933), where a
maintainer replied that every option changes follow behaviour and therefore
needs design sign-off before implementation. Acceptance is requested from a
maintainer, who records it on this Status line.

Measured by reading main at `54caa49074`. The reported cause is consistent with
the code and with the issue's two code-reading triages, but has not yet been
confirmed by instrumentation. Phase 0 records that verdict before code is
written.

## 1. Summary

When a transcript-hosted MCP App frame renders while an agent run is active and
the reader is following the bottom, growth reports from the app resize its
containing transcript row. The transcript follows that tail growth to the new
bottom, carrying the reader past the app's header and first content.

This RFC proposes the issue's recommended design: every transcript-hosted
inline app frame gets one opportunity, on its first accepted
`ui/notifications/size-changed` report, to release follow and align the actual
frame top with the transcript viewport top. Later size reports from that mounted
frame may resize it but do not move the reader. Follow re-arms through the
existing paths: the reader returns to the bottom, uses Jump to latest, or sends
a message while the existing Jump to the bottom when you send setting is on.

The rule has no per-app opt-in. It applies only while the frame is in the current
tail display row and follow is armed. A side-panel frame, an older transcript
row, a frame whose first report arrives after follow was released, and the
`wide` and `overlay` presentations do not invoke it.

## 2. Motivation

### 2.1 Current behaviour

At `54caa49074`:

- `DEFAULT_HEIGHT` and `MAX_HEIGHT` in
  `website/src/components/McpAppFrame.tsx` give an inline frame a 480 px
  initial height and cap its height at 1200 px. The app supplies the requested
  height; the host validates and caps the result.
- The `N_SIZE_CHANGED` branch in `website/src/components/McpAppFrame.tsx`
  applies every positive, finite `ui/notifications/size-changed` height. A
  report may grow, shrink or leave the rounded height unchanged. The handler does not debounce reports.
- `followResizeBatch` in
  `website/src/hooks/virtualizer/followPolicy.ts` classifies a genuine tail-row
  resize as followable growth and calls `pinAuto`. Its comment names
  streaming and widget growth deliberately.
- `pinAuto` in `website/src/hooks/virtualizer/followPolicy.ts` leaves an armed,
  stationary reader followed and targets the live bottom. Frame growth itself
  does not release follow.
- The module invariant at the top of
  `website/src/hooks/virtualizer/followPolicy.ts` requires every programmatic
  scroll write to pass through `writeScrollTop` so the follow
  guard can distinguish host motion from reader motion.
- `releaseFollowBaseline` in
  `website/src/hooks/virtualizer/followPolicy.ts` clears the recorded pin and
  restores native anchoring; it does not set `stickRef` to false. Existing automatic releases assign the
  new stick state first and then call it
  (the `pinAuto` and `prePaintRepin` release branches in that module).
- A later height-cache commit has a second bottom-follow path:
  the height-sync consumer in
  `website/src/hooks/virtualizer/shiftCompensation.ts` calls `prePaintRepin`
  whenever follow remains armed. Suppressing only
  `followResizeBatch` therefore does not suppress every re-pin.

The transcript does know which content contains an app. `appToolCallIds` keeps
app-bearing tool calls visible (the `isMcpAppItem` path in
`website/src/pages/chat/TurnBlock.tsx`),
promotes their loose rows into a stable turn
(`useTranscriptRows` in
`website/src/pages/chat/page/transcriptRows.ts`), and gives that turn a
stable virtualizer key (`useStableRowKeys` in the same module).
The missing seam is narrower: neither `ResizeBatch` nor `followPolicy` receives
the app identity, the nested frame element, or a request to release and align.

### 2.2 Reported effect

The reporter recorded a phone-width viewport (1080x1920) with the
`progettai-commerce:progettai-bridge` app showing 133 products. The frame mounts
as an empty box, cards and image placeholders arrive, images load, and the app
reports larger heights. The view ends in the middle or at the bottom of the
frame. The business-message banner, result count and first products are above
the viewport. The reader scrolls up manually at about 00:08 in the 10 s
recording. Frequency on other apps is untested.

Following a growing tail row to its bottom is appropriate for streaming text,
where the newest line is at the bottom. It is wrong for an app whose reading
order begins at its top.

### 2.3 Issue triage

[Issue comment 7057497993](https://github.com/kirodotdev/KiroCrew/issues/39933#issuecomment-7057497993),
from a maintainer-operated auto-pipeline, checked the cause by reading main at
`10a7144441` and recommended this RFC's design. It specifically preferred one
release for every inline frame over an app opt-in, and rejected merely ignoring
frame resizes because that can leave follow state and scroll position out of
step. It also required the release to honor `writeScrollTop` and
`releaseFollowBaseline`, with Jump to latest making the reported tall-frame
state visible.

[Issue comment 7123061085](https://github.com/kirodotdev/KiroCrew/issues/39933#issuecomment-7123061085)
rechecked the cause on main, said that changing follow behaviour still requires
a maintainer's design sign-off, and directed a proposal to this process. Neither
comment records acceptance of this RFC.

### 2.4 Related work

- [#10810](https://github.com/kirodotdev/KiroCrew/issues/10810) fixed movement
  for a reader already inside a straddling streaming row. Its lasting rule is
  relevant here: growth appended below the reader must not be mistaken for a
  reprice distributed through the row.
- [#13092](https://github.com/kirodotdev/KiroCrew/issues/13092) and
  [#13355](https://github.com/kirodotdev/KiroCrew/issues/13355) are closed
  scroll-position defects involving width changes and visibility return.
- [#15170](https://github.com/kirodotdev/KiroCrew/issues/15170) tracks WebKit
  verification of table resize compensation. Phase 0 checks that this design
  does not reverse the compensation contract it protects.
- `docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md`
  keeps the transcript scroller behind the composer. “Viewport top” below means
  the top of that transcript scroller, not the browser window or composer.

## 3. Goals and non-goals

Goals:

1. The first accepted size report from an eligible frame aligns that frame's
   actual wrapper top with the transcript scroller's top and releases follow
   before the reported height commits.
2. Later size reports from that mounted frame leave the aligned frame top fixed
   until the reader moves it.
3. The release updates `stickRef`, the follow baseline, native anchoring and the
   programmatic-write accounting as one operation.
4. Existing re-engagement paths continue to work: reader travel to the bottom,
   Jump to latest, and a send that is configured to jump to the bottom.
5. Streaming text, tables, trailing chrome, ordinary widgets and non-app rows
   retain their current follow and resize-compensation behaviour.
6. The first accepted report produces no painted frame in which the new app
   height is visible while the viewport remains at the frame's bottom.

Non-goals:

- Changing `DEFAULT_HEIGHT`, `MAX_HEIGHT` or the MCP Apps size-report protocol.
- Adding an MCP tool/result metadata field or an app-specific opt-in.
- Changing the existing Jump to the bottom when you send setting.
- Changing `wide` or `overlay` expansion, collapse, or viewport-resize
  behaviour. Those presentations use a viewport-derived display height
  (`displayHeight` in `website/src/components/McpAppFrame.tsx`) rather than the
  reported inline height; any one-step transcript movement they cause is follow-up work.
- Changing follow for an app hosted in the side panel.
- Adding a new user setting.

## 4. Design

### 4.1 Eligible first report

“First accepted size report” means the first message in one mounted iframe
document whose `height` passes the existing positive, finite validation. A ref
owned by that `McpAppFrame` instance consumes the opportunity whether or not the
frame is eligible at that moment. A later report must never wait for the reader
to re-arm follow and then move them.

The report is eligible only when all of these are true before `setHeight`:

- This `McpAppFrame` was mounted by the transcript path in
  the `ToolCallLine` MCP App branch, not `McpAppTabBody` in
  `website/src/pages/chat/SidePanelTabBodies.tsx`.
- Its presentation is `inline`.
- Its containing display row is the current last display row. This prevents a
  delayed report from an older overscanned app from moving a reader backward.
- The virtualizer reports follow armed. If the reader already owns the position,
  the report changes height and nothing else.
- The scroller, row and frame wrapper are still connected and the wrapper is a
  descendant of that row.

A virtualizer remount creates a new iframe document and therefore one new
opportunity. This is intentional: the initial bottom placement of a newly
mounted tail can otherwise recreate the same symptom. The eligibility checks
bound the action to a followed tail, so remounting history while the reader is
scrolling does not move them. Multiple frames in one tail row each get one
opportunity; after the first eligible frame releases follow, another frame's
first report sees follow off and cannot move the reader.

### 4.2 Transcript-scoped integration seam

The transcript row host supplies an internal callback through its row-rendering
context. The callback carries the containing display index and the actual
`McpAppFrame` wrapper element. The side-panel host supplies no callback. This
keeps protocol mode (`inline`) separate from placement (transcript versus side
panel) and avoids a window-global event.

The virtualizer adds one imperative operation for that callback. It validates
the conditions in §4.1 against live refs rather than trusting a stale render.
The operation returns whether it performed the release, which lets tests prove
that the first opportunity was consumed without exposing follow internals to
`McpAppFrame`.

The operation aligns the nested frame element, not the display-row top. An
app-bearing display row can contain the prompt, reasoning, tool calls and more
than one frame, so those coordinates are not interchangeable.

### 4.3 Atomic release and alignment

The first eligible report performs this sequence synchronously, before applying
the reported height:

1. Read the frame wrapper and scroller rectangles.
2. Compute the scroller-coordinate target as current `scrollTop` plus the
   wrapper top minus the scroller top, clamped to the scroller's valid range.
3. Set `stickRef.current = false`.
4. Call `releaseFollowBaseline(scroller)`.
5. Call `writeScrollTop(scroller, target, 'auto', 'release',
   'mcp-app-first-size')`.
6. Apply the validated, rounded and capped frame height through the existing
   `setHeight` path.

Steps 3–5 are one virtualizer-owned operation. `McpAppFrame` does not mutate
follow refs or write `scrollTop` itself. Setting `stickRef` before the write is
load-bearing because `writeScrollTop` derives native `overflow-anchor` from the
live stick state.

### 4.4 Later app growth is an append below the aligned point

Releasing follow prevents `pinAuto` and `prePaintRepin`, but it is not sufficient
by itself. A released reader is protected by above-fold resize compensation,
and an app nested inside a grouped turn can make that turn straddle the fold.
Without an app-specific signal, the measurement path can mistake growth at the
frame's bottom for a reprice distributed through the whole row and compensate
`scrollTop`.

Before every accepted inline size change in a transcript row, the host marks the
frame wrapper through the existing `noteInPlaceResize` seam in
`website/src/hooks/virtualizer/inPlaceResize.ts`. The marker says that the
change occurs at or below the frame top. The row measurement then treats the
change like content appended at the bottom, preserving the aligned frame top,
while unrelated reprices in the same row keep their existing compensation.
This marker records geometry only; it performs no scroll write and does not
release or re-arm follow.

Phase 0 verifies that this compensation path is reachable in the reported
sequence. If instrumentation proves it is not, the implementation may omit the
marker, but the verdict and resulting simplification must be recorded in this
RFC before the implementation PR merges.

### 4.5 Re-engagement

No new re-engagement mechanism is added.

- Reader movement back to the bottom re-arms follow through
  `onFollowScroll`/`resolveUserScrollStick`.
- Jump to latest calls the existing `scrollToBottom`, which force-pins and
  re-arms follow.
- A send re-arms follow only when `scrollOnSend` is true and the existing send
  path calls `scrollBottom` (the `scrollOnSend` branch in
  `website/src/pages/chat/page/composerSend.ts`).
  With the setting off, sending leaves both position and released follow state
  alone, matching its documented contract
  (the `ChatConfig.scrollOnSend` contract in
  `website/src/pages/chat/ChatSettings.tsx`).

The Jump to latest button remains geometry-driven (`!isAtBottom`), not
follow-state-driven. It is expected to appear in the reported tall-frame case,
but this RFC does not claim that every released frame position shows it.

## 5. Implementation plan

### Phase 0: confirm the mechanism and amend this RFC

Instrument a development build while reproducing with the reporter's app or an
equivalent app that emits several growth reports. Record, for the initial mount
and each report:

- app identity, containing display index and current tail index;
- presentation and transcript-versus-panel host;
- requested and displayed height;
- frame top relative to the transcript scroller;
- `scrollTop`, `scrollHeight`, `clientHeight` and follow state;
- writes attributed to `followResizeBatch`, `prePaintRepin` and above-fold
  compensation.

Exit criteria: this RFC records the observed path in §2.1, including whether the
first report is a `firstMount` or genuine resize, whether a trailing “Thinking…”
footer participates, and whether released-reader compensation would move the
frame. Temporary instrumentation is removed. The related decisions in §2.4 are
checked. No implementation starts until the amendment is reviewable.

### Phase 1: implementation, tests and real-machine proof

One implementation PR adds the scoped callback and virtualizer operation,
marks later app growth as in-place where Phase 0 says it is necessary, and adds
tests before changing behaviour. Tests and implementation ship together; no PR
lands with an intentionally failing test.

Exit criteria:

- A protocol/component test proves only the first valid report from one mounted
  iframe can request alignment and invalid reports do not consume it.
- A transcript/virtualizer integration test uses an app nested inside a grouped
  tail row and proves that the first report aligns the frame top, releases
  follow and records a release write.
- Subsequent grow, shrink and repeated-height reports leave the frame top fixed.
- Already-released, non-tail, disconnected, side-panel, `wide` and `overlay`
  cases do not move the transcript.
- Two app frames in one tail row cannot each move the reader without explicit
  re-engagement between them.
- Reader return, Jump to latest, send with `scrollOnSend: true`, and send with
  `scrollOnSend: false` obey §4.5.
- Existing streaming, table, footer, first-mount, scroll-up, width-change and
  visibility-return suites pass unchanged.
- The real-machine check reproduces the bug on base and verifies the PR head on
  the same machine, app and phone-width viewport. Evidence shows the frame top
  after every size report and no bottom-then-top painted flash. The machine is
  cleaned up and the PR records the before, after and cleanup results.

## 6. Backward compatibility

The chosen design changes only transient transcript position and follow state.
It adds no persisted data, config key, MCP field or app opt-in, and a git revert
fully restores the previous behaviour.

Every existing app receives the new transcript behaviour without republishing.
Side-panel apps and expanded presentations are unchanged. The
`scrollOnSend: false` contract remains unchanged: a send does not move the
transcript or re-arm follow when that setting is off.

The intentional behaviour change is that a reader who was following no longer
sees text streamed after the frame automatically until they return to the
bottom, use Jump to latest, or make a send that is configured to jump there.

## 7. Security considerations

The app iframe is untrusted even though the bridge authenticates its window.
The app controls whether and when it sends its first valid size report, so the
host must not let that report target an arbitrary position.

The live checks in §4.1 limit the effect to one host-computed alignment per
mounted, transcript-hosted inline frame while its row is the followed tail. The
target comes from the connected wrapper's actual geometry and is clamped by the
host. A delayed older frame, side-panel frame or already-released transcript
cannot request a write. Later reports cause no follow write, so flooding them
does not amplify into repeated scroll writes beyond the rendering work the
current handler already accepts.

No capability, origin, CSP, iframe sandbox or app-to-gateway permission changes.

## 8. Alternatives considered

### App opt-in through `_meta.ui`

Apply the same release only when MCP tool-definition or tool-result metadata
opts in. This is not a manifest field: `_meta.ui.resourceUri` currently belongs
to tool metadata (the “Associate it with the tool” section of
`src/kiro_crew/docs/mcp-apps.md`). A new flag would be
a public MCP-facing contract and would have to be harvested and propagated into
the render payload. Existing apps would retain the defect until republished.
The issue triage preferred consistent host behaviour, so this RFC rejects the
opt-in.

### Ignore app resizes while follow is armed

Filtering app growth out of `followResizeBatch` avoids one immediate pin but
leaves follow armed and does not by itself stop the height-commit
`prePaintRepin` path. Suppressing both paths would leave the reader away from the
bottom while follow still claims ownership, with no reliable visible signal.
This is the mismatch the issue triage rejected.

### Cap follow at the frame top

Keep follow armed but cap automatic targets at the app. This does not fit the
actual row structure: the virtualizer owns a grouped display row, while the
frame is a nested child whose top can be far below the row top, and one row can
hold multiple frames. A cap also has no unambiguous release point when later
text is appended in the same tall row. It changes the central target calculation
in both `pinAuto` and `prePaintRepin` for no benefit over the chosen explicit
release, so this RFC rejects it.

### Debounce size reports

This reduces the number of pins but leaves the final pin aimed at the bottom.
It does not solve the reading-order error.

### Mount at the final height

The host cannot know the app's final height before its content and images load.
The size protocol exists to supply that information after mount.

### Leave the reader to scroll up

This is the reported behaviour and does not meet the goal.

## 9. Open decisions for acceptance

- **D1.** Does the maintainer accept the proposed transcript-hosted inline rule,
  with no per-app opt-in and expanded presentations out of scope?
- **D2.** Does the maintainer accept that text streamed after the frame remains
  below the viewport until an existing re-engagement path in §4.5 runs?

Phase 1 is blocked on both decisions. Phase 0 may proceed because it changes no
product behaviour and its verdict can narrow or withdraw this proposal.
