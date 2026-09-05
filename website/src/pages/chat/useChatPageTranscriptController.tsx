import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type Dispatch,
  type MutableRefObject,
  type ReactNode,
  type SetStateAction,
} from 'react'
import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'

import { api } from '../../api/client'
import type { AutoNudgeLoop } from '../../components/AutoNudgePopover'
import type { FileChangeEntry } from '../../components/FileChangeChips'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import type { ChatMessage, ChatSlot } from '../../types'
import { useChatNavigation } from '../../hooks/useChatNavigation'
import { useChatPins } from '../../hooks/useChatPins'
import type { useMessageSearch } from '../../hooks/useMessageSearch'
import { openPanelView } from '../../hooks/usePanelTabs'
import { MessageSearchScope } from '../../hooks/SearchHighlightContext'
import { useSlotDeferredValue } from '../../hooks/useSlotDeferredValue'
import { useVirtualChat } from '../../hooks/virtualizer/useVirtualChat'
import { turnHadPolicyBlock } from '../../app-sdk/turnPolicyBlock'
import {
  mergeRenderers,
  resolveRenderer,
  type MessageRenderer,
  type MessageRenderContext,
} from '../../app-sdk/messageRenderers'
import { createTranscriptRenderers } from './transcriptRenderers'
import { devLog, inspectorOn } from '../../dev/scrollInspector'
import { prevUserTextFor } from './share/shareSupport'
import { parseOptions } from '../../app-sdk/protocol'
import { isNoteRow } from '../../lib/noteContract'
import { i18nT } from '../../i18n/t'
import { store, useAppSelector, type AppDispatch } from '../../store'
import {
  abortActiveOlderFetch,
  isSupersededPagingRejection,
  loadOlderMessages,
  openActivityPanel,
} from '../../store/chatSlice'
import {
  attachUserScrollIntent,
  glideOnceStep,
  pickSearchScrollBehavior,
  pollRowSettled,
  scrollCurrentMatchIntoView,
} from '../../utils/searchScroll'
import { resolveMsgIndex } from '../../utils/shareUrl'
import { isHiddenInvisibleAssistantRow } from '../../utils/invisibleText'
import type { PasteBlock } from '../../utils/pasteTokens'
import {
  DEFAULT_PINNED_CARD_H,
  computePinPush,
  findNextPromptIdx,
  findPinnedPromptIdx,
  jumpAnchorIdx,
  nextPinnedPromptState,
  pinHandoffY,
  pinPushTravel,
  type PinnedPromptState,
} from '../../utils/pinnedPrompt'
import type { TurnStats } from './AssistantMessage'
import { loadChatConfig, saveChatConfig, type ChatConfig } from './ChatSettings'
import CollapsibleToolGroup from './CollapsibleToolGroup'
import {
  applyRunningState,
  createTurnGrouper,
  REASONING_ROLES,
} from './groupDisplayItems'
import { renderMcpOAuthMessage } from './McpOAuthBanner'
import { fmtMessageTime, fmtMessageTimeFull } from './messageTime'
import NoticeCard from './NoticeCard'
import { nudgeLabel, parseNudgeMessage } from './NudgeCard'
import {
  canForkAtWindow,
  earlierAffordanceInView,
  EARLIER_BAR_SELECTOR,
  OLDER_WALK_MAX_PAGES_PER_INPUT,
  shouldContinueOlderWalk,
  shouldPaginateOlder,
} from './pagination'
import StopEventCard from './StopEventCard'
import { headline as subagentHeadline } from './SubagentCompletionCard'
import {
  parseSubagentCompletionMessage,
  type ParsedSubagentCompletion,
} from './subagentCompletion'
import TurnBlock from './TurnBlock'
import type { DisplayItem, TurnItem } from './types'
import { AssistantMessage, UserMessage } from '.'
import { useStreamIdle } from './ChatFooter'
import type { useScrollManager } from './useScrollManager'
import { useBubbleVanishProbe } from './useBubbleVanishProbe'
import {
  anchorAltIdFor,
  messageRowKey,
  msgIdentityKey,
  renderUserContent,
  stableAnchorIdFor,
  turnLeadKey,
  uniqueRowKeys,
  virtualKeyFor,
} from './ChatPageMessageContent'

/** Min gap between scroll-gesture-driven retries of a failed older-history
 * page. One request per gesture on a dead link, not one per scroll event. */
const OLDER_RETRY_COOLDOWN_MS = 1500
/** Cadence of the level-triggered top-parked pagination poll. Slow on purpose:
 * it is the backstop that guarantees progress while the reader holds the top,
 * not the fast path (the sentinel/crossing triggers still fire first). */
const OLDER_TOP_POLL_MS = 700
/**
 * How long after the reader's last scroll the top-of-transcript walk keeps
 * paging. Past this they have stopped climbing, and a page landing then is
 * movement they did not ask for.
 */
const OLDER_WALK_ACTIVE_MS = 1500
// Idle prefetch: quiet time required before background pages load, and the
// poll cadence. Quiet > the farm's deep-idle threshold is deliberate — the
// farm gets first claim on idle time; prefetch only runs once it is caught up.
// A page may LAND only after the scroller has been still this long: landing
// compensation writes scrollTop, and mid-momentum writes fight the fling.
const OLDER_LANDING_SETTLE_MS = 400
/** A transcript at least this much taller than its viewport is scrollable
 *  in earnest: the reader can climb to ask for history, so nothing fetches
 *  it for them. Below it, auto-fill is load-bearing (no scrollbar exists). */
const OLDER_FILL_SLACK_PX = 120

/** Whether a top-sentinel fire may auto-fetch history for a reader who did
 *  not climb. Exported for tests: this single predicate is what separates
 *  the load-bearing short-transcript fill (no scrollbar exists, so nothing
 *  else CAN load history) from the boot-transient page chain that walked
 *  megabytes over a parked phone after every refresh.
 *
 *  Keyed on INPUT, not on follow: a boot that restores a saved scroll
 *  anchor releases follow before the reader has touched anything, and a
 *  follow-based gate read that as a climbing reader -- the replica probe
 *  showed the walk running at one page per ~8s through that door with
 *  zero input events. Real wheel/touch input is the only signal that a
 *  fetch is reader-initiated; the sole inputless exception is a transcript
 *  too short to scroll, where the fill is load-bearing (no scrollbar
 *  exists, so nothing else could ever load its history). */
export function shouldAutoFillOlder(g: { scrollHeight: number; clientHeight: number; sawInput: boolean }): boolean {
  if (g.scrollHeight <= g.clientHeight + OLDER_FILL_SLACK_PX) return true
  return g.sawInput
}
/** How long one real gesture authorizes an automatic older-history fetch.
 *
 *  It is a WINDOW and not a latch because the latch was the defect. A landing's
 *  own compensation writes scrollTop, which fires a `scroll` event, so with
 *  permanent authorization the automatic doors ran land -> quiet -> land at a
 *  steady beat over a reader who was not asking for any of it. Only `wheel` and
 *  `touchmove` refresh this stamp, and our own writes produce neither, so the
 *  window ages out on its own: a gesture burst buys a bounded run of pages, not
 *  the rest of the session. */
const REAL_GESTURE_AUTH_MS = 20000
/** ChatPage's tool rows derive their auto-denied state inside ToolCallLine;
 *  the registry default that reads this set is never reached here. Frozen and
 *  shared so the per-row context does not allocate. */
const NO_AUTO_DENIED = new Set<string>()
// Hold-down for the display-layer running latch: a slots broadcast that
// catches the agent between tool calls flaps `running` false for well under
// a second; only a false that persists longer reflects the turn ending.
const RUNNING_LATCH_MS = 2500

/**
 * Where a jump-to-message came from, because the three entry points owe the
 * reader different copy when the target cannot be found.
 *
 *  - `pin`     the pins list, so pin wording is accurate;
 *  - `earlier` the earlier-messages control, which has its own paging copy;
 *  - `link`    a `?msg=` share link, minted by copy-link-to-message for ANY
 *              message. That reader may never have pinned anything, so naming a
 *              pin would report an action they did not take.
 */
export type PendingJumpOrigin = 'pin' | 'earlier' | 'link'

/** SINGLE writer for the not-found copy, so a new origin cannot reach the reader
 *  wearing another origin's wording. */
const jumpUnavailableNotice = (origin: PendingJumpOrigin): string =>
  origin === 'earlier' ? i18nT('components.chatPane.earlier_messages_unavailable')
    : origin === 'link' ? i18nT('pages.chat.deepLink.message_unavailable')
      : i18nT('pages.chat.pins.message_unavailable')

export interface UseChatPageTranscriptEarlyControllerOptions {
  activeTip: unknown
  mountIndexRef: MutableRefObject<(index: number) => boolean>
  pinPromptMinimized: boolean
  scrollerRef: ReturnType<typeof useScrollManager>['scrollerRef']
  scrollToDisplayIndex: ReturnType<typeof useScrollManager>['scrollToDisplayIndex']
  /** Live mirror of `chat.slotRunning`. A ref, not the value: `autoFollowAllowed`
   *  is a stable callback several observers depend on, and taking the value as a
   *  dependency would re-attach them on every turn boundary. */
  slotRunningRef: MutableRefObject<boolean>
  vGetFollowRef: MutableRefObject<() => boolean>
  vScrollToBottomRef: MutableRefObject<(behavior?: ScrollBehavior) => void>
}

export function useChatPageTranscriptEarlyController({
  activeTip,
  mountIndexRef,
  pinPromptMinimized,
  scrollerRef,
  scrollToDisplayIndex,
  slotRunningRef,
  vGetFollowRef,
  vScrollToBottomRef,
}: UseChatPageTranscriptEarlyControllerOptions) {
  // Scroll to bottom helper — delegates to the virtualizer (single controller).
  // Distance-aware: a smooth glide is for SHORT hops. Sending from deep in
  // history used to smooth-scroll through tens of thousands of estimate-priced
  // pixels — every frame mounted, measured and repriced a fresh window, so the
  // trip itself took seconds and arrived at a still-mounting tail. Beyond a few
  // viewports, teleport (the industry norm: message send lands at the bottom
  // instantly; smooth motion is reserved for distances the eye can follow).
  const scrollBottom = useCallback((instant: boolean = false) => {
    const el = scrollerRef.current
    const far = el ? el.scrollHeight - el.scrollTop - el.clientHeight > el.clientHeight * 3 : false
    vScrollToBottomRef.current(instant || far ? 'auto' : 'smooth')
  }, [scrollerRef, vScrollToBottomRef])

  /**
   * Whether an AUTOMATIC bottom pin is allowed: the follow flag AND live
   * geometry must agree.
   *
   * The flag alone is not enough for anything the reader did not ask for. A turn
   * can start on its own -- a subagent completion, a cron notification, an
   * auto-nudge cycle -- and an in-flow band can resize at any time; with a stale
   * armed flag either one teleports a reader who is deep in history to the
   * bottom. The distance cannot be stale, so requiring it makes that impossible.
   * Explicit intent (the send path, the jump-to-bottom pill) does NOT go through
   * here: there the reader asked to be at the bottom.
   */
  const autoFollowLastChRef = useRef(0)
  const autoFollowAllowed = useCallback(() => {
    if (!vGetFollowRef.current()) return false
    // Nothing running means there is no output to follow, so an automatic pin is
    // a yank with no cause. The bands this gates (the tip/survey card and the
    // composer status stack) mount and resize on their own schedule, which is
    // how a reader who had scrolled up got sent back to the bottom with nothing
    // streaming. Explicit intent -- sending, the jump-to-bottom pill -- does not
    // come through here.
    if (!slotRunningRef.current) return false
    const el = scrollerRef.current
    if (!el) return true
    // A SHRINKING viewport is the composer growing under the reader's own
    // typing. Chasing it walks the transcript up a line every few characters,
    // which is the "picture keeps moving while I type" report -- so follow is
    // frozen for that direction here too, not only in the virtualizer's own
    // viewport branch. Growth (composer collapsing, keyboard closing) still
    // pins: that space is being given back.
    const prevCh = autoFollowLastChRef.current
    autoFollowLastChRef.current = el.clientHeight
    if (prevCh > 0 && el.clientHeight < prevCh) return false
    return el.scrollHeight - el.scrollTop - el.clientHeight <= el.clientHeight
  }, [scrollerRef, slotRunningRef, vGetFollowRef])

  // Scroll compensation for two in-flow bands that render outside the
  // virtualizer's measured rows: the tip card and the session-pulse survey
  // card. Mounting or resizing either shrinks the scroll viewport without the
  // virtualizer re-anchoring, so when the user is parked at the bottom of a
  // streaming turn the last line gets clipped, or a new turn renders behind the
  // card instead of pushing it out of view. Re-anchor whenever the tip changes
  // OR the survey reports a height change (double rAF: let the band's layout
  // commit before measuring).
  //
  // `surveyLayoutTick` is a counter, not a boolean: the card can report the
  // same "still visible" state across several distinct height changes
  // (mount/unmount, expand/collapse, the post-submit thank-you collapse), and
  // this effect only cares that SOMETHING changed, not the value.
  const [surveyLayoutTick, setSurveyLayoutTick] = useState(0)
  const handleSurveyLayoutChange = useCallback(() => setSurveyLayoutTick((t) => t + 1), [])
  useEffect(() => {
    // Gate on FOLLOW, not the 100px at-bottom band: a reader parked a little
    // above the bottom has released follow, and re-anchoring for a tip/survey
    // band would yank them (and replace the mounted window under them).
    if (!vGetFollowRef.current()) return
    const raf = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (autoFollowAllowed()) scrollBottom(true)
      })
    })
    return () => cancelAnimationFrame(raf)
  }, [activeTip, surveyLayoutTick, scrollBottom, autoFollowAllowed, vGetFollowRef])

  // Same compensation for the composer status stack (progress bars, sub-agent
  // delivery line, queue stack). The virtualizer's own viewport branch DOES
  // re-pin when the band shrinks the scroller's box — but a queued send is a
  // message-array append too, and the regroup remounts tail rows while the
  // band's spring animates the viewport, so that re-pin can land on interior
  // heights that are still settling. Measured frame-by-frame on the pre-fix
  // build: `scrollTop - clientHeight` math reports "at bottom" while the
  // content sits a card-height (~21px) low, and whether it recovers depends
  // on which re-render lands last — the defect reads as intermittent. This
  // observer re-anchors AFTER every layout step of the band (ResizeObserver
  // fires post-layout), so the final write always follows the last height
  // change instead of racing it. Effect deps cannot do that: a one-shot
  // re-anchor at mount time measures a half-grown band. Gated on FOLLOW for
  // the same reason as the tip/survey effect above. A callback ref (not
  // useRef + effect) so the observer re-attaches when the chat column
  // unmounts and remounts.
  const composerBandObserverRef = useRef<ResizeObserver | null>(null)
  const composerBandRef = useCallback((el: HTMLDivElement | null) => {
    composerBandObserverRef.current?.disconnect()
    composerBandObserverRef.current = null
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      if (autoFollowAllowed()) scrollBottom(true)
    })
    ro.observe(el)
    composerBandObserverRef.current = ro
  }, [scrollBottom, autoFollowAllowed])

  // Navigate to a (possibly off-window) display index: mount it first via the
  // virtualizer so the DOM-based scroll can find it, then scroll next frame.
  // Tracks the in-flight row-mount poll (below) so a newer navigation cancels
  // the previous one. Without this, an earlier far-jump loop whose target
  // finally mounts would scroll to that stale destination, yanking away from
  // the newer target (rapid stepping / click-then-click). cancelAnimationFrame(0)
  // is a no-op, so 0 is a safe initial value.
  const navScrollRafRef = useRef(0)
  // Cancel handle for the in-flight settle poll, so a newer navigation or an
  // unmount terminates it rather than letting it run to the wall-clock backstop.
  const navPollCancelRef = useRef<(() => void) | null>(null)
  const navToDisplayIndex = useCallback((
    idx: number,
    opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number },
  ) => {
    cancelAnimationFrame(navScrollRafRef.current)
    // Signal WidgetFrames that a jump is starting so the span of widgets
    // mountIndex is about to union doesn't all build their iframes in one
    // frame (see PROGRAMMATIC_BUILD_DELAY_MS in WidgetFrame).
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const jumpedFar = mountIndexRef.current(idx)
    // A FAR jump replaces the window, so the rows between the old viewport and
    // the target are NOT mounted — a smooth glide would scrub the scroller
    // through blank spacer (the "occasional flicker" on the ↑/jump pills when
    // the target is past a long turn). Teleport instantly instead: the target
    // block is already mounted so it shows immediately, and overflow-anchor
    // keeps it stable as its rows measure. NEAR jumps keep their smooth glide
    // (mountIndex unioned the whole path, so there's nothing blank to scrub).
    const behavior: ScrollBehavior = jumpedFar ? 'auto' : (opts?.behavior ?? 'smooth')
    // mountIndex queues a React state update (the virtualizer's window range).
    // A FAR jump REPLACES the window, so the target row is NOT painted into the
    // DOM within a single frame — one rAF then a DOM query misses it. Poll for
    // the row and scroll once it mounts, then keep re-scrolling (re-reading the
    // live offset each frame) until the row's measured height SETTLES — a far
    // row must mount + measure, and a widget target keeps growing for ~450ms as
    // its iframe builds (PROGRAMMATIC_BUILD_DELAY_MS). A fixed frame-count
    // ceiling (~0.5s) gives up before the widget settles, so the jump silently
    // no-ops and only works on a second click once cached. Condition-based
    // instead: retry until the target reports a stable (non-estimated) height,
    // with a ~2s wall-clock backstop so a genuinely unreachable target still
    // terminates instead of spinning. While the row is missing we do NOTHING —
    // we never teleport to top (the "far jump jumps to top, second click works"
    // bug). navScrollRafRef holds the in-flight frame so a newer navigation
    // cancels this loop (rapid stepping / click-then-click).
    const rowEl = (): HTMLElement | null =>
      (scrollerRef.current?.querySelector(`[data-display-index="${idx}"]`) as HTMLElement | null) ?? null
    navPollCancelRef.current?.()
    // The poll re-scrolls every frame for up to CONVERGE_MAX_MS (~2s). If the
    // user tries to scroll during that window, continuing to step would drag
    // the viewport back to the target and fight their input — so user scroll
    // ABORTS the convergence, exactly as scrollCurrentMatchIntoView does. (A
    // fixed frame-count ceiling short enough (~0.5s) masks this; the
    // longer, condition-based window makes it reachable.) The shared
    // attachUserScrollIntent covers scrollbar drag and keyboard scrolling too,
    // not just wheel/touch.
    const scrollEl = scrollerRef.current
    const onUserScroll = () => { navPollCancelRef.current?.() }
    const detachUserScroll = attachUserScrollIntent(scrollEl ?? undefined, onUserScroll)
    navPollCancelRef.current = pollRowSettled({
      measure: () => {
        const el = rowEl()
        return el ? el.getBoundingClientRect().height : null
      },
      // Only the FIRST step may glide — see glideOnceStep. Re-issuing a smooth
      // scroll cancels and restarts the animation, so stepping every frame
      // through the quiet window would leave a NEAR jump stuttering until the
      // poll ends (the same restart trap removed from the streaming pin).
      step: glideOnceStep(
        (b) => { scrollToDisplayIndex(idx, { ...opts, behavior: b }) },
        behavior,
      ),
      raf: (cb) => (navScrollRafRef.current = requestAnimationFrame(cb)),
      now: () =>
        typeof performance !== 'undefined' && typeof performance.now === 'function'
          ? performance.now()
          : Date.now(),
      onEnd: () => { detachUserScroll(); navPollCancelRef.current = null },
    })
  }, [scrollToDisplayIndex, scrollerRef, mountIndexRef])

  // Stop any in-flight settle poll on unmount. Without this the loop keeps
  // ticking rAFs against a null scroller until the ~2s backstop (harmless but
  // pointless work after the page is gone).
  useEffect(() => () => {
    navPollCancelRef.current?.()
    navPollCancelRef.current = null
    cancelAnimationFrame(navScrollRafRef.current)
  }, [])

  const displayItemsRef = useRef<DisplayItem[]>([])
  // Pinned-prompt banner. `pinFoldRef` is a zero-height sentinel sitting
  // directly under the title row: its top edge is the fold line the banner
  // sticks to, and it is always mounted so the fold stays measurable even when
  // nothing is pinned yet. `pinCardRef` is measured for the push geometry.
  const pinFoldRef = useRef<HTMLDivElement | null>(null)
  const pinCardRef = useRef<HTMLDivElement | null>(null)
  const pinEnabledRef = useRef(true)
  // Read by updatePinnedPrompt, which keeps stable deps and so reaches config
  // through refs rather than closing over it.
  const pinMinimizedRef = useRef(false)
  // Which control to focus after the next minimize/restore. Set only by the click
  // handler, so a chip that mounts from SCROLLING never steals focus.
  const pinFocusWantRef = useRef<'chip' | 'minimize' | null>(null)
  const [pinned, setPinned] = useState<PinnedPromptState | null>(null)
  const [pinExpanded, setPinExpanded] = useState(false)
  /**
   * Minimized-to-chip state, held in the shared chat config rather than local
   * state so it survives a slot switch and a reload — the point of the control is
   * that a narrow screen stays uncluttered without being re-told every session.
   * `saveChatConfig` dispatches `mc-config-changed`, which this page's own listener
   * and every other open pane already consume, so one write settles it everywhere.
   */
  const pinMinimized = pinPromptMinimized
  const setPinMinimized = useCallback((next: boolean) => {
    // The click unmounts the focused button, dropping focus to <body>. Name the
    // successor; the effect below lands on it once the swap commits.
    pinFocusWantRef.current = next ? 'chip' : 'minimize'
    // Re-read, so a field another pane wrote since this render is not reverted by
    // spreading a stale snapshot.
    saveChatConfig({ ...loadChatConfig(), pinPromptMinimized: next })
  }, [])
  // Collapsed card height — the hand-off line is derived from it, so it must be
  // known even while nothing is pinned (no card mounted to measure). Seeded with
  // the computed default and then reported by PinnedPrompt itself, which is the
  // only place the SETTLED height is knowable: measuring the card from here would
  // sample the expand/collapse morph mid-flight and drag the line with it.
  const pinCollapsedHRef = useRef(DEFAULT_PINNED_CARD_H)
  const onPinCollapsedHeight = useCallback((h: number) => {
    if (h > 0) pinCollapsedHRef.current = h
  }, [])
  // Recompute which prompt is pinned, and how far the incoming prompt has
  // pushed it out, from the current scroll position.
  const updatePinnedPrompt = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    // Measure with getBoundingClientRect (viewport-relative) so the origin
    // matches the scroller regardless of which ancestor is the items'
    // offsetParent — consistent with useScrollManager, which also deliberately
    // avoids offsetTop. The fold sits BELOW the scroller's top edge (under the
    // title row), which is what the sentinel gives us.
    const items = el.querySelectorAll('[data-display-index]')
    const foldY = pinFoldRef.current?.getBoundingClientRect().top
      ?? el.getBoundingClientRect().top
    // A prompt hands over to the banner only once it is entirely behind the band
    // (bottom edge at or above the band's bottom), so a prompt taller than the
    // band scrolls away line by line instead of collapsing the moment it is sent.
    const handoffY = pinHandoffY(foldY, pinCollapsedHRef.current)
    // First row whose bottom is still below that line = the topmost row not yet
    // fully scrolled behind the band.
    let handoffIdx = -1
    for (const item of items) {
      const htmlItem = item as HTMLElement
      if (htmlItem.getBoundingClientRect().bottom > handoffY) {
        handoffIdx = parseInt(htmlItem.getAttribute('data-display-index') || '0', 10)
        break
      }
    }

    if (!pinEnabledRef.current || handoffIdx < 0) { setPinned(null); return }
    const list = displayItemsRef.current
    const pinIdx = findPinnedPromptIdx(list, handoffIdx)
    const pinItem = pinIdx >= 0 ? list[pinIdx] : undefined
    if (!pinItem || pinItem.kind !== 'single') { setPinned(null); return }
    // The incoming prompt pushes the banner out; when its row is not mounted it
    // is still far below the fold, so there is nothing to push against yet. Its
    // TOP edge against the fold drives the push (see computePinPush) — an earlier
    // line than the hand-off, so a tall prompt shoves the card fully out while it
    // scrolls in, and only takes the pin once its own bottom clears the band.
    const nextIdx = findNextPromptIdx(list, pinIdx)
    const nextEl = nextIdx >= 0
      ? el.querySelector(`[data-display-index="${nextIdx}"]`) as HTMLElement | null
      : null
    const nextTop = nextEl ? nextEl.getBoundingClientRect().top : null
    // Measure the live card when it is mounted, and otherwise fall back to the
    // last SETTLED collapsed height PinnedPrompt reported: the push threshold
    // below has to be decidable even while nothing is mounted, or dropping the
    // banner would zero the height, zero the push, re-mount it, and oscillate at
    // frame rate.
    // Neither push nor drop describes a chip, whose band is a fraction of the
    // height this math uses. A no-op while not minimized, so the card is unchanged.
    const minimized = pinMinimizedRef.current
    const measured = minimized ? 0 : (pinCardRef.current?.getBoundingClientRect().height ?? 0)
    const bannerH = measured > 0 ? measured : pinCollapsedHRef.current
    const push = minimized ? 0 : computePinPush(bannerH, foldY, nextTop)
    // Fully pushed out: DROP the banner instead of rendering it clipped to
    // nothing. A tall incoming prompt holds this state for its whole length (it
    // takes the pin only once its own bottom clears the band), and a card clipped
    // to zero still shows a hairline of its bottom edge under sub-pixel rounding
    // and browser zoom — a bubble fragment parked over the prompt being read.
    if (!minimized && push >= pinPushTravel(bannerH)) { setPinned(null); return }
    const full = pinItem.msg.content
    // A nudge's content is a machine-facing instruction payload behind an
    // `[auto-nudge cycle N]` tag, and a subagent completion's is a header block
    // plus digest. Quoting either verbatim would park kilobytes of machine text
    // over the transcript, so both reuse the compact label their transcript card
    // already shows and keep the body for the expanded state.
    const nudge = pinItem.msg.role === 'nudge' ? parseNudgeMessage(pinItem.msg) : null
    // Detected by PARSING, not by role: the same completion event reaches the
    // transcript under `subagent`, `assistant` (delivery-timeout variant) and
    // `user` (older scrollback), and the parser already tolerates all three.
    // Matching on the role here would both miss those variants and duplicate
    // dispatch knowledge this file has no business holding.
    const sub = nudge ? null : parseSubagentCompletionMessage(pinItem.msg)
    const machineLabel = nudge
      ? nudgeLabel(nudge.cycle)
      : sub
        ? subagentHeadline(sub)
        : null
    // Stored content is COLLAPSED (recollapsePastes), so a big paste is a
    // `[ Paste #N ]` token; the reducer unwraps it and decides whether to derive.
    setPinned(prev => nextPinnedPromptState(prev, {
      idx: pinIdx,
      ts: pinItem.msg.ts,
      raw: full,
      pastes: (pinItem.msg.meta?.pastes as PasteBlock[] | undefined) || [],
      machineLabel,
      machineBody: nudge ? nudge.body : (sub ? full : undefined),
      push,
      bannerH,
    }))
  }, [scrollerRef])
  // rAF-throttle the per-scroll recompute: updatePinnedPrompt does a
  // querySelectorAll + getBoundingClientRect loop (a forced layout read), and a
  // fling fires scroll dozens of times/sec. Coalesce to at most once per frame,
  // mirroring the virtualizer's own scroll-listener throttle so this handler
  // doesn't reintroduce scroll-time main-thread cost.
  // Cancel-and-reschedule, never latch-on-pending: a handle whose callback
  // never fires (bfcache-dropped frame) would block every later signal
  // permanently (frameSchedulerLatch guard). Coalesces identically.
  const pinRafRef = useRef(0)
  const onScrollPin = useCallback(() => {
    if (pinRafRef.current) cancelAnimationFrame(pinRafRef.current)
    pinRafRef.current = requestAnimationFrame(() => {
      pinRafRef.current = 0
      updatePinnedPrompt()
    })
  }, [updatePinnedPrompt])
  /** Jump the transcript back to the pinned prompt, landing it just below the
   *  banner so the prompt is read in context — which also un-pins the banner,
   *  since its prompt is no longer above the fold. */
  /** Landing inset for a pinned-prompt jump, solved from the banner's own
   *  push geometry so the PREVIOUS turn's banner pins COMPLETELY at the
   *  landing — the chained-jump flow: click the banner, land on the prompt's
   *  start, the previous prompt's banner is already fully formed above it,
   *  click again to keep walking back. computePinPush returns 0 (no push, no
   *  clipping) iff the landed row's top clears the fold by at least
   *  pinPushTravel(bannerH). The incoming banner's height is unknowable until
   *  it pins (different prompt, different wrap), so reserve for the SETTLED
   *  collapsed height (pinCollapsedHRef, what a clamped card measures) with a
   *  slack margin absorbing wrap variance and mid-glide shifts — over-reserving
   *  only shows a little more of the turn above; under-reserving clips the
   *  banner and breaks the chain. */
  const PINNED_JUMP_SLACK_PX = 24
  const pinnedJumpChrome = useCallback(() => {
    const el = scrollerRef.current
    const foldTop = pinFoldRef.current?.getBoundingClientRect().top
    const srTop = el?.getBoundingClientRect().top
    const fold = (foldTop != null && srTop != null) ? (foldTop - srTop) : 48
    // The banner that must fit is the PREVIOUS turn's, which pins mid-glide —
    // its height is unknowable at launch (different prompt, different wrap:
    // measured 69.5-92.3px across the same session). Read the LIVE card when
    // one is pinned (after the mid-glide swap that is already the incoming
    // banner), floored by the settled collapsed height for the gap while
    // nothing is pinned. The converging glide re-reads this every frame, so
    // the reserve tracks the swap instead of freezing at the old banner.
    const live = pinCardRef.current?.getBoundingClientRect().height ?? 0
    const bannerH = Math.max(live, pinCollapsedHRef.current)
    return fold + pinPushTravel(bannerH) + PINNED_JUMP_SLACK_PX
  }, [scrollerRef])
  const scrollToPinnedPrompt = useCallback((target: number) => {
    const chrome = pinnedJumpChrome()
    cancelAnimationFrame(navScrollRafRef.current)
    navPollCancelRef.current?.()
    // The jump lands at the head of the target's consecutive prompt run — a
    // steer pair, a subagent fan-out, an unanswered nudge run — so the row on
    // the hand-off line is a non-prompt and the previous turn's banner
    // survives the landing. Rationale and near/far interaction: see
    // jumpAnchorIdx's docblock (utils/pinnedPrompt.ts).
    const anchor = jumpAnchorIdx(displayItemsRef.current, target)
    const jumpedFar = mountIndexRef.current(anchor)
    if (jumpedFar) {
      // Far target: the window was REPLACED, the path between is unmounted
      // spacer — a glide would scrub blank. Teleport via the convergence
      // path, same as every other far jump.
      navToDisplayIndex(anchor, { behavior: 'auto', align: 'start', offset: -chrome })
      return
    }
    // NEAR jump — the common case: the pinned prompt is the previous turn.
    // mountIndex UNIONED the whole path above, so every row between here and
    // the target is now mounting. Wait the few frames those rows take to
    // measure (reading, not scrolling), then compute the distance ONCE from
    // live geometry and glide in a single smooth scroll. Measuring first is
    // what makes the one glide land exactly (no estimatedHeight rows left on
    // the path); gliding once is what keeps it a real scroll — a convergence
    // poll's per-frame auto writes would cancel the animation and read as a
    // teleport. A user scroll or a newer navigation aborts the wait.
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const rowEl = (): HTMLElement | null =>
      (scrollerRef.current?.querySelector(`[data-display-index="${anchor}"]`) as HTMLElement | null)
    let lastH: number | null = null
    let stable = 0
    let frames = 0
    let cancelled = false
    let detach2: (() => void) | null = null
    const detach = attachUserScrollIntent(scrollerRef.current ?? undefined, () => { cancelled = true })
    navPollCancelRef.current = () => { cancelled = true; detach() }
    const tick = () => {
      if (cancelled) { detach(); return }
      const el = rowEl()
      const h = el ? el.getBoundingClientRect().height : null
      if (h != null && lastH != null && Math.abs(h - lastH) < 1) stable += 1
      else stable = 0
      lastH = h
      frames += 1
      // 2 stable frames is enough: rows measure synchronously on mount via
      // measureRef; the wait only covers React committing the unioned window.
      // The frame cap (~0.5s) guarantees the glide still happens if some row
      // never stops moving (e.g. an animated widget).
      if ((h != null && stable >= 2) || frames >= 30) {
        // SELF-DRIVEN converging glide, not a native smooth scroll. A native
        // animation is cancelled by ANY other scrollTop write — and writes DO
        // land mid-glide: the upward window expansion's anchor compensation,
        // the height-sync compensation, a re-measuring row. Each cancellation
        // strands the scroll wherever the write happened (the probe showed
        // landings at 34-61px with the banner clipped or dropped — the exact
        // "some fixed spots never reach the previous message" report). Owning
        // every frame's write makes the glide uncancellable, and re-deriving
        // the destination each frame from LIVE geometry (row rect + the
        // banner currently pinned) absorbs those same mid-flight shifts —
        // mid-glide image loads and the banner swap included — so the glide
        // CONVERGES on the true landing instead of a stale one. One motion,
        // no post-landing correction. User scroll intent still aborts.
        detach()
        detach2 = attachUserScrollIntent(scrollerRef.current ?? undefined, () => { cancelled = true })
        navPollCancelRef.current = () => { cancelled = true; detach2?.() }
        const GLIDE_MS = 450
        const t0 = performance.now()
        const sc0 = scrollerRef.current
        const from = sc0 ? sc0.scrollTop : 0
        const reduced = typeof window.matchMedia === 'function'
          && window.matchMedia('(prefers-reduced-motion: reduce)').matches
        const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3)
        const glide = () => {
          if (cancelled) { detach2?.(); return }
          const sc = scrollerRef.current
          const row = rowEl()
          if (!sc || !row) { detach2?.(); navPollCancelRef.current = null; return }
          const liveTarget = sc.scrollTop
            + (row.getBoundingClientRect().top - sc.getBoundingClientRect().top)
            - pinnedJumpChrome()
          const goal = Math.max(0, Math.min(sc.scrollHeight - sc.clientHeight, liveTarget))
          const t = reduced ? 1 : Math.min(1, (performance.now() - t0) / GLIDE_MS)
          sc.scrollTop = from + (goal - from) * easeOutCubic(t)
          if (t >= 1) { detach2?.(); navPollCancelRef.current = null; return }
          navScrollRafRef.current = requestAnimationFrame(glide)
        }
        navScrollRafRef.current = requestAnimationFrame(glide)
        return
      }
      navScrollRafRef.current = requestAnimationFrame(tick)
    }
    navScrollRafRef.current = requestAnimationFrame(tick)
  }, [navToDisplayIndex, pinnedJumpChrome, scrollerRef, mountIndexRef])
  return {
    vGetFollowRef,
    mountIndexRef,
    scrollerRef,
    scrollToDisplayIndex,
    vScrollToBottomRef,
    scrollBottom,
    autoFollowAllowed,
    surveyLayoutTick,
    handleSurveyLayoutChange,
    composerBandRef,
    navScrollRafRef,
    navPollCancelRef,
    navToDisplayIndex,
    displayItemsRef,
    pinFoldRef,
    pinCardRef,
    pinEnabledRef,
    pinMinimizedRef,
    pinFocusWantRef,
    pinned,
    setPinned,
    pinExpanded,
    setPinExpanded,
    pinMinimized,
    setPinMinimized,
    pinCollapsedHRef,
    onPinCollapsedHeight,
    updatePinnedPrompt,
    onScrollPin,
    pinnedJumpChrome,
    scrollToPinnedPrompt,
  }
}

export type ChatPageTranscriptEarlyController = ReturnType<typeof useChatPageTranscriptEarlyController>

type AssistantMessageProps = ComponentProps<typeof AssistantMessage>

export interface UseChatPageTranscriptControllerOptions {
  activeSlot: string | null
  activeViewIsBoundedPage: boolean
  activityOpen: boolean
  /** Tool-call ids whose app opened in the side panel. Needed here (not only in
   *  the view) because the measure farm renders a TurnBlock off-screen and its
   *  height must equal the mounted row's, which reads this set. */
  appToolCallIds: ReadonlySet<string>
  approve: NonNullable<ComponentProps<typeof CollapsibleToolGroup>['onApprove']>
  autoNudgeLoop: AutoNudgeLoop | null
  chatConfig: ChatConfig
  connected: boolean
  connectionsUiOn: boolean
  continuing: boolean
  continuable: boolean
  cursorIsForActiveSlot: boolean
  dismissApproval: (approvalId: string, decision?: string) => void
  dispatch: AppDispatch
  early: ChatPageTranscriptEarlyController
  /** Fork / Plan-from-here are withheld inside an embedded, non-popout pane. */
  embedded?: boolean
  filteredSlots: ChatSlot[]
  handleApplyPlan: NonNullable<AssistantMessageProps['onApplyPlan']>
  handleArtifactOpen: NonNullable<AssistantMessageProps['onArtifactOpen']>
  handleAsk: NonNullable<AssistantMessageProps['onAsk']>
  handleContinue: () => void
  handleEditResend: NonNullable<ComponentProps<typeof UserMessage>['onEditResend']>
  handleFileOpen: (path: string, opts?: { line?: number; endLine?: number }) => void
  handleFolderOpen: (path: string) => void
  handleFork: NonNullable<AssistantMessageProps['onFork']>
  handleOpenDiff: NonNullable<AssistantMessageProps['onOpenDiff']>
  handlePlanFromHere: NonNullable<AssistantMessageProps['onPlanFromHere']>
  handleQuote: NonNullable<AssistantMessageProps['onQuote']>
  handleRegenerate: NonNullable<AssistantMessageProps['onRegenerate']>
  handleSpeak: NonNullable<AssistantMessageProps['onSpeak']>
  handleSubagentPanelOpen: (parsed: ParsedSubagentCompletion) => void
  highlightTs: string | null
  initialMidRef: MutableRefObject<string | null>
  initialMsgRef: MutableRefObject<string | null>
  initialSidRef: MutableRefObject<string | null>
  interrupted: boolean
  isMobile: boolean
  isStreaming: boolean
  lastTextIdx: number
  linkPreviewsOn: boolean
  loadingOlder: boolean
  mcpAppPanel: boolean
  messages: ChatMessage[]
  messagesRef: MutableRefObject<ChatMessage[]>
  mode?: string
  planTaskId: string
  /** A popped-out pane is a real window, so it keeps Fork / Plan-from-here. */
  popout?: boolean
  regenerating: boolean
  revealAppInPanel: (toolCallId: string) => void
  search: ReturnType<typeof useMessageSearch>
  selectSessionTab: (key: string) => void
  setAutoNudgeOpen: Dispatch<SetStateAction<boolean>>
  setHighlightTs: Dispatch<SetStateAction<string | null>>
  setToolDisclosureFor: (key: string, expanded: boolean) => void
  showRefusedPress: (action: 'continue' | 'regenerate' | 'switch_variant', error: unknown) => void
  slotHasMore: boolean
  slotOldestIndex: number
  /** Governance-gated: `capabilities.social_share`, resolved server-side and
   *  reported by `GET /api/dashboard/config`. Not a preference — the entry stays
   *  hidden until the endpoint says true. */
  socialShareOn: boolean
  slotRunning: boolean
  slotState: string
  toApiDecision: (action: string) => 'approve' | 'reject'
  toggleAct: () => void
  toolDisclosure: Record<string, boolean>
}

export function useChatPageTranscriptController({
  activeSlot,
  activeViewIsBoundedPage,
  activityOpen,
  appToolCallIds,
  approve,
  autoNudgeLoop,
  chatConfig,
  connected,
  connectionsUiOn,
  continuing,
  continuable,
  cursorIsForActiveSlot,
  dismissApproval,
  dispatch,
  early,
  embedded,
  filteredSlots,
  handleApplyPlan,
  handleArtifactOpen,
  handleAsk,
  handleContinue,
  handleEditResend,
  handleFileOpen,
  handleFolderOpen,
  handleFork,
  handleOpenDiff,
  handlePlanFromHere,
  handleQuote,
  handleRegenerate,
  handleSpeak,
  handleSubagentPanelOpen,
  highlightTs,
  initialMidRef,
  initialMsgRef,
  initialSidRef,
  interrupted,
  isMobile,
  isStreaming,
  lastTextIdx,
  linkPreviewsOn,
  loadingOlder,
  mcpAppPanel,
  messages,
  messagesRef,
  mode,
  planTaskId,
  popout,
  regenerating,
  revealAppInPanel,
  search,
  selectSessionTab,
  setAutoNudgeOpen,
  setHighlightTs,
  setToolDisclosureFor,
  showRefusedPress,
  slotHasMore,
  slotOldestIndex,
  slotRunning,
  socialShareOn,
  slotState,
  toApiDecision,
  toggleAct,
  toolDisclosure,
}: UseChatPageTranscriptControllerOptions) {
  const {
    displayItemsRef,
    vGetFollowRef,
    mountIndexRef,
    navToDisplayIndex,
    pinEnabledRef,
    pinFocusWantRef,
    pinMinimizedRef,
    setPinned,
    setPinExpanded,
    scrollerRef,
    updatePinnedPrompt,
    vScrollToBottomRef,
  } = early
  const searchCtxValue = useMemo(() => ({
    term: search.term,
    caseSensitive: search.caseSensitive,
    currentMessageIdx: search.currentMessageIdx,
    currentOccurrenceIdx: search.currentOccurrenceIdx,
  }), [search.term, search.caseSensitive, search.currentMessageIdx, search.currentOccurrenceIdx])

  const lastRole = messages[messages.length - 1]?.role ?? ''
  // Advances with every streamed chunk, so ChatFooter can tell "text is arriving"
  // apart from "the stream went quiet mid-turn" (the model generating a tool call,
  // or a tool group holding the trailing 'streaming' message open). 0 whenever no
  // streaming message is in flight.
  const streamTick = lastRole === 'streaming' ? (messages[messages.length - 1]?.content.length ?? 0) : 0
  // Transcript heat: advances on ANY transcript mutation (streamed chunk, tool
  // row, thinking burst), so useStreamIdle can tell a high-frequency burst from
  // a quiet running turn. Render-phase ref bump, guarded on the identity change
  // — the same pattern as a lazy initializer, so it is StrictMode-safe.
  const heatMessagesRef = useRef<ChatMessage[] | null>(null)
  const heatTickRef = useRef(0)
  if (heatMessagesRef.current !== messages) { heatMessagesRef.current = messages; heatTickRef.current++ }
  // Hot while the slot runs and mutations landed within the idle window. The
  // state update inside useStreamIdle commits AFTER the render that delivered a
  // mutation, so a row mounting on the first mutation after a quiet spell still
  // reads idle=true (hot=false) and keeps its entrance ease; only rows mounting
  // inside a burst (a second mutation within 700ms) snap. Passed down to
  // ToolCallLine to gate its height animations — see `transcriptHot` there.
  const transcriptIdle = useStreamIdle(heatTickRef.current, slotRunning)
  const transcriptHot = slotRunning && !transcriptIdle

  // Grouping depends ONLY on `messages`; `slotRunning` decides one boolean on the
  // trailing turn. Bundling both in one memo re-ran the whole O(N) grouping pass on
  // every turn start/stop just to flip that flag, and the new identity cascaded into
  // messageToDisplayIdx / visibleIndexMap / the virtualizer. Split: group once, then
  // apply the flag in O(1).
  //
  // The grouper is the per-page identity cache (see createTurnGrouper): each
  // streaming flush replaces `messages`, so this memo re-runs per flush — the
  // grouper reconciles against the previous result so settled turns keep their
  // object identity and memo(TurnBlock) / mergeTurnThinking bail out.
  const groupTurns = useMemo(() => createTurnGrouper(), [])
  const groupedTurns = useMemo(() => groupTurns(messages), [groupTurns, messages])

  // LATCHED running for the DISPLAY layer only. The raw flag is derived
  // from slots broadcasts that catch the agent momentarily idle BETWEEN
  // tool calls, so mid-turn it flaps false for a beat and back. Each flap
  // marks the trailing turn complete: TurnBlock auto-collapses it, the
  // next broadcast re-expands it, and on a long-running turn (hundreds of
  // steps) that is a multi-thousand-px accordion right above a reader
  // parked at the bottom -- the field-reported self-bounce, reproduced on
  // the bottom rig as a ~2Hz scrollHeight oscillation. TRUE applies
  // immediately (a new turn must render live), FALSE only after holding
  // steady past the flap window.
  const [runningLatched, setRunningLatched] = useState(slotRunning)
  useEffect(() => {
    if (slotRunning) { setRunningLatched(true); return }
    const timer = setTimeout(() => setRunningLatched(false), RUNNING_LATCH_MS)
    return () => clearTimeout(timer)
  }, [slotRunning])
  const displayItems = useMemo<DisplayItem[]>(
    () => applyRunningState(groupedTurns, runningLatched),
    [groupedTurns, runningLatched],
  )
  // The transcript render is the page's heaviest tree (a landed page regroups
  // 1500+ messages and remounts a window of rich rows), and rendering it at
  // urgent priority is what freezes composer input and every main-thread
  // animation during a landing. Deferring the VIRTUALIZER's input marks that
  // whole subtree as interruptible: urgent updates (typing, button states,
  // spinners) commit against the previous list, and the regrouped list renders
  // when the main thread has room. Everything that must agree with the
  // RENDERED rows (the DOM-index ref, row keys, the prefetch index) reads the
  // deferred value, so index spaces stay consistent.
  //
  // Scoped to the active slot: a plain useDeferredValue keeps returning the
  // PREVIOUS list until the background render lands, and under the page's
  // urgent churn that is hundreds of ms -- long enough that a session switch
  // painted the outgoing tab's transcript under the incoming tab's URL, and a
  // new chat's first send briefly showed the previous session's messages
  // (#8526). Only same-slot updates (streaming flushes, history landings) are
  // deferred; a switch renders the right transcript in its first commit.
  const renderedDisplayItems = useSlotDeferredValue(activeSlot, displayItems)

  // Keep the ref in sync so handleRangeChanged / updatePinnedPrompt
  // read the latest displayItems. useLayoutEffect (not useEffect): the DOM's
  // `data-display-index` attributes are updated at commit, but a scroll rAF can
  // fire before React flushes a PASSIVE effect — so with useEffect the pin
  // recompute could read fresh DOM indices against a stale list, mis-deriving
  // `pinned.idx` by one row (the row-hide is identity-keyed as a second guard,
  // see below). A layout effect runs in the commit phase, before that rAF, so
  // the ref is caught up by the time the recompute reads it. Still a passive
  // side effect, not render-body mutation, so React's rules of render hold.
  useLayoutEffect(() => { displayItemsRef.current = renderedDisplayItems }, [renderedDisplayItems, displayItemsRef])

  // Opt-in #7045 diagnostic: log store-vs-render counts whenever the number of
  // mounted transcript rows drops (see useBubbleVanishProbe). Off (and free)
  // unless the localStorage flag is set.
  const messagesLenRef = useRef(0)
  useLayoutEffect(() => { messagesLenRef.current = messages.length }, [messages])
  const bubbleProbeCounts = useCallback(
    () => ({ store: messagesLenRef.current, display: displayItemsRef.current.length }),
    [displayItemsRef],
  )
  useBubbleVanishProbe(scrollerRef, bubbleProbeCounts, activeSlot)

  // Pinned prompt: keep the enablement ref in sync (updatePinnedPrompt is declared
  // above chatConfig and reads it through a ref), and recompute after the list
  // changes — a new turn shifts geometry with no scroll event of its own.
  useEffect(() => {
    pinEnabledRef.current = chatConfig.pinLastPrompt
    if (!chatConfig.pinLastPrompt) setPinned(null)
  }, [chatConfig.pinLastPrompt, pinEnabledRef, setPinned])
  // Recompute on the flip too: the band's height changes with it, so the push and
  // drop verdicts computed against the old one are stale the moment it toggles.
  useEffect(() => {
    pinMinimizedRef.current = chatConfig.pinPromptMinimized
    // Keyed on the SHARED state, not the toggle that wrote it: the toggle runs only
    // in the clicked pane, so a second pane came back expanded on restore.
    if (chatConfig.pinPromptMinimized) setPinExpanded(false)
    updatePinnedPrompt()
  }, [chatConfig.pinPromptMinimized, pinMinimizedRef, setPinExpanded, updatePinnedPrompt])
  // Runs after commit so the successor exists; the ref gate means only a user
  // toggle focuses, never a scroll-driven mount.
  useEffect(() => {
    const want = pinFocusWantRef.current
    if (!want) return
    pinFocusWantRef.current = null
    const sel = want === 'chip'
      ? '[data-testid="pinned-prompt-pill"]'
      : '[data-testid="pinned-prompt-minimize"]'
    ;(document.querySelector(sel) as HTMLElement | null)?.focus()
  }, [chatConfig.pinPromptMinimized, pinFocusWantRef])
  useEffect(() => { updatePinnedPrompt() }, [renderedDisplayItems, updatePinnedPrompt])
  // Expanded state PERSISTS as the pinned prompt is replaced by the next one
  // while scrolling — the user asked for a sticky "keep it open" behaviour, so we
  // do NOT collapse on `pinned.idx` change. It still resets on slot switch below
  // (a different session should start collapsed).

  // Virtualized display — only mounts items in the viewport window. The
  // virtualizer shares `scrollerRef` with useScrollManager so the legacy
  // scroll APIs (scrollToDisplayIndex, scrollToBottom) operate on the
  // same DOM element. Its own follow-output handles streaming auto-pin
  // and append-pin, so the legacy useStreamingScroll/useFollowOutput
  // calls below are no-ops in this configuration but are kept invoked
  // for hook-call stability.
  // Per-message identity used to derive BOTH the inner bubble key (renderMessage,
  // ~line 2848) AND the virtualizer/HeightCache key (virtualKey, below). Keeping
  // them on the SAME identity means the steer-bubble stability fix protects
  // the virtualizer + HeightCache layer too, not just the bubble:
  //   1. Prefer meta.clientTs — the steer_push echo overwrites `ts` (client→
  //      server) mid-stream; keying on `ts` alone would flip the key, orphan the
  //      cached height, revert the row to the estimate, and lurch the viewport.
  //   2. Fall back to `ts` for ordinary messages.
  //   3. For ts-less messages (e.g. an error appended on the send-failure path)
  //      DON'T fall back to the array index: truncateAfterIndex / regenerate
  //      would shift the key of every following row → mass remount + a large
  //      scroll swing. Mint a per-message-instance id instead. Object identity
  //      is stable across renders under Immer's structural sharing, and survives
  //      truncation of *later* rows, so the key is stable for the message's life.
  //      (A durable id stamped in the reducer at append would also survive a full
  //      refetch/replace.)
  const msgIdSeq = useRef(0)
  const msgIds = useRef(new WeakMap<ChatMessage, string>())
  const stableMsgKey = useCallback((m: ChatMessage): string => {
    const explicit = (m.meta?.clientTs as string | undefined) || m.ts
    if (explicit) return explicit
    let id = msgIds.current.get(m)
    if (!id) { id = `mid-${msgIdSeq.current++}`; msgIds.current.set(m, id) }
    return id
  }, [])
  // Index lookup into the deduped list, so this getKey prices an item
  // correctly ONLY against the displayItems of its own render. Live consumers
  // pair getKeyRef with itemsRef from the same tick; the one stale-ITEMS
  // consumer — the prepend anchor capture — snapshots getKey ALONGSIDE the
  // previous items (see prependPrevRef in useVirtualChat). The window-shift /
  // tail-append captures read previous-commit DOM indices through the current
  // render, which stays correct in the shapes they fire on (indices before
  // the change point keep both item and bare key). The fallback covers only
  // an out-of-range probe.
  const stableAnchorId = useCallback(
    (it: DisplayItem, index: number) => stableAnchorIdFor(it, index, stableMsgKey),
    [stableMsgKey],
  )
  const anchorAltId = useCallback(
    (it: DisplayItem, index: number) => anchorAltIdFor(it, index, stableMsgKey),
    [stableMsgKey],
  )
  // The prefetch contract, verbatim from the user: "start loading while I am
  // still two USER MESSAGES from the top" — messages they sent, not any two
  // display rows (a row can be a nudge, a tool group, a lone card). Resolve
  // the display index holding the SECOND user-authored message from the top of
  // the loaded transcript; the virtualizer fires the older-history fetch on
  // the downward crossing of that index.
  const prefetchStartIndex = useMemo(() => {
    const holdsUser = (t: TurnItem): boolean =>
      t.kind === 'single' ? t.msg.role === 'user' : t.msgs.some((m) => m.role === 'user')
    let seen = 0
    for (let i = 0; i < renderedDisplayItems.length; i++) {
      const it = renderedDisplayItems[i]
      const has = it.kind === 'turn' ? it.items.some(holdsUser) : holdsUser(it)
      if (has && ++seen === 2) return i
    }
    return undefined
  }, [renderedDisplayItems])
  const rowKeys = useMemo(
    () => uniqueRowKeys(renderedDisplayItems, stableMsgKey),
    [renderedDisplayItems, stableMsgKey],
  )
  const virtualKey = useCallback(
    (it: DisplayItem, i: number) => rowKeys[i] ?? virtualKeyFor(it, i, stableMsgKey),
    [rowKeys, stableMsgKey],
  )

  // (Sticky widget detection removed — widgets now unmount with the
  // window like any other item. See useVirtualChat call below for the
  // memory-vs-flicker trade-off rationale.)

  // Width bucket for the height cache's scope (see heightScopeKey below).
  // Quantized to 16px; capped at 944 because the content column maxes out at
  // 900px + 32px row padding, so all wider scrollers share one bucket.
  // Initialized from innerWidth (the scroller is not mounted yet on first
  // render) and corrected from the real clientWidth in the layout effect.
  const [scrollerWidthBucket, setScrollerWidthBucket] = useState(() =>
    Math.min(typeof window !== 'undefined' ? Math.round(window.innerWidth / 16) * 16 : 944, 944))
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const compute = () => setScrollerWidthBucket(Math.min(Math.round(el.clientWidth / 16) * 16, 944))
    compute()
    if (typeof ResizeObserver === 'undefined') return
    // Debounced: mid-drag resize storms must not thrash the height index.
    let t: ReturnType<typeof setTimeout> | undefined
    const ro = new ResizeObserver(() => { clearTimeout(t); t = setTimeout(compute, 200) })
    ro.observe(el)
    return () => { ro.disconnect(); clearTimeout(t) }
  }, [scrollerRef])

  // Live scroller element for gates that run before `virt` exists in scope
  // (handleTopReached is a dependency of the useVirtualChat call itself).
  const vScrollerElRef = useRef<HTMLElement | null>(null)
  // Timestamp of the last REAL gesture (wheel/touchmove/keydown/pointerdown) on
  // the scroller. Programmatic scrolls (landing compensation, bottom pins) fire
  // scroll events but none of these, so the stamp genuinely means "the reader
  // drove". Read by every self-issued history-fetch gate.
  const lastRealInputAtRef = useRef(0)
  // Pages the SENTINEL door has issued since the last real gesture. The walk
  // poll bounds itself the same way (OLDER_WALK_MAX_PAGES_PER_INPUT) because an
  // unbounded authorization once walked a whole multi-megabyte transcript over a
  // parked reader; this door needs its own counter because the poll's lives
  // inside its interval effect, out of reach here.
  const sentinelPagesSinceInputRef = useRef(0)
  // The walk poll's own budget and last-gesture stamp. Refs so neither survives
  // only as long as the effect that reads them -- see the note at their use.
  const walkPagesSinceInputRef = useRef(0)
  const walkLastInputAtRef = useRef(Number.NEGATIVE_INFINITY)
  // Entering a session is not a request for history, so a session must never
  // INHERIT authorization. Every one of these is written in exactly one place --
  // `noteInput`, on a real wheel/touchmove over the transcript -- and that
  // listener's effect is not keyed on the slot, so without this clear a gesture
  // in the session you just left still authorizes the automatic doors in the one
  // you just opened. Reader intent belongs to the session it happened in.
  //
  // A one-way "has this session ever seen input" latch used to sit here too. It
  // is gone rather than reset: an authorization that can only ever turn ON is not
  // an authorization, and both automatic doors now read the same EXPIRING window,
  // so leaving the slot simply lets it age out.
  useEffect(() => {
    lastRealInputAtRef.current = 0
    walkPagesSinceInputRef.current = 0
    walkLastInputAtRef.current = Number.NEGATIVE_INFINITY
    sentinelPagesSinceInputRef.current = 0
  }, [activeSlot])
  // Mirrored so the early handlers (declared above `virt`) can refuse to page on
  // unsettled geometry, the same gate the walk poll and idle prefetch apply.
  const vFarmIsMeasuredRef = useRef<((i: number) => boolean) | null>(null)
  // Mirrored for the interval bodies, which must not re-arm per render.
  const earlierBarInViewRef = useRef<() => boolean>(() => false)

  // THE admission rule for every AUTOMATIC older fetch: the control that offers
  // history must be on the reader's screen. See earlierAffordanceInView for why
  // this replaces the per-trigger geometry/latch proxies, each of which had a
  // second cause that was not the reader.
  const earlierBarInView = useCallback(() => {
    const el = vScrollerElRef.current
    if (!el) return false
    const bar = el.querySelector(EARLIER_BAR_SELECTOR)
    const vr = el.getBoundingClientRect()
    const br = bar?.getBoundingClientRect()
    return earlierAffordanceInView(
      br ? { top: br.top, bottom: br.bottom } : null,
      { top: vr.top, bottom: vr.bottom },
    )
  }, [])

  // Reaching the top of a resumed transcript fetches the history behind the loaded slice.
  /**
   * Has the active session finished ARRIVING? Every automatic history fetch is
   * shut until it has.
   *
   * This gates the door itself rather than feeding shouldAutoFillOlder, because
   * an empty transcript satisfies that predicate's "too short to scroll" branch
   * on GEOMETRY alone -- the one path no gesture requirement can close, since
   * the branch returns before it ever reads `sawInput`. A switch installs an
   * empty list, restores cursor ownership on fulfilment, and leaves the earlier
   * bar sitting in view with nothing above it: three conditions that together
   * read as "the reader is at the top asking for history" while the reader has
   * done nothing at all.
   */
  const handleTopReached = useCallback(() => {
    const chat = store.getState().chat
    if (!shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })) return
    // A bottom-FOLLOWED reader on a SCROLLABLE transcript did not ask for
    // this: the top sentinel only reaches them through boot/measurement
    // transients (estimate-priced rows keep total height under a viewport
    // for a beat; a spacer collapse pulls the top within reach), and each
    // self-issued landing re-fires the transition -- a page chain over a
    // parked reader, felt as 'it starts loading previous the moment I
    // refresh, then jumps'. When the transcript cannot scroll at all the
    // fill is load-bearing (a short resumed session has no scrollbar to
    // climb), so it stays; the moment it is scrollable, further history
    // is reader-initiated (climb releases follow, sentinel then serves).
    if (!earlierBarInView()) return
    const el = vScrollerElRef.current
    // The "too short to scroll" branch of shouldAutoFillOlder fires on GEOMETRY,
    // so it also fires on a geometry TRANSIENT — and the comment above names the
    // two that produce one. What it did not account for is that the composer's
    // text is ChatPage state, so every keystroke re-renders this tree and gives
    // the virtualizer another chance to be caught mid-measurement. Reported from
    // a phone as history loading while TYPING. Both the walk poll and the idle
    // prefetch already refuse to page on unsettled geometry; this door did not,
    // and it is the one the sentinel comes through.
    // An empty transcript is NOT "too short to scroll" -- it is "not loaded yet".
    // shouldAutoFillOlder cannot tell the two apart: both satisfy its geometry
    // branch, which returns before it ever reads `sawInput`, so no authorization
    // requirement can close that path. Separating the two states is what stops a
    // session ENTRY from reading as a reader parked at the top asking for history.
    // The measured-rows loop below cannot do it either: over zero rows it checks
    // nothing and falls straight through.
    if (displayItemsRef.current.length === 0) return
    const nRows = displayItemsRef.current.length
    for (let i = 0; i < nRows; i++) { if (!vFarmIsMeasuredRef.current?.(i)) return }
    // Neither of the two obvious signals can key this. A one-way input latch is
    // one-way latch, so on a scrollable transcript one touch leaves it open for
    // the rest of the mount — it cannot mean "this session". And `!follow` is the
    // design shouldAutoFillOlder's own contract names as falsified: follow is
    // released with no reader input at all, both by an anchor restore and at slot
    // entry, where `lastWriteTop` resets to -1 so the idle branch's self-check
    // cannot rescue it. Either one turns an entry geometry transient into a fetch
    // nobody asked for.
    // What survives both is the EXPIRING form of the real-gesture record, which
    // the automatic doors already apply: a timestamp cannot latch open, and it is
    // silent on a slot the reader has not touched.
    if (el && !shouldAutoFillOlder({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      sawInput: Date.now() - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
    })) return
    // A gesture authorizes a BOUNDED run of pages, not the whole authorization
    // window. Authorization alone is what let one flick chain prepends until the
    // transcript ran out and left the reader at the very start of history.
    // The short-transcript fill is exempt because it bounds itself: every page
    // makes the transcript taller, so the geometry branch stops admitting once it
    // outgrows the viewport, and bounding it would strand a transcript that is
    // still too short to offer a scrollbar.
    if (el && el.scrollHeight > el.clientHeight + OLDER_FILL_SLACK_PX) {
      if (sentinelPagesSinceInputRef.current >= OLDER_WALK_MAX_PAGES_PER_INPUT) return
      sentinelPagesSinceInputRef.current += 1
    }
    if (inspectorOn()) devLog('OLDER', 'sentinel')
    void dispatch(loadOlderMessages())
  }, [dispatch, earlierBarInView, displayItemsRef])
  /**
   * The click path needs no gate beyond the in-flight check the thunk already makes.
   *
   * Also the remedy for affordances NOT adjacent to it: the unavailable fork/plan items
   * and the partial-scope search count both name "load earlier history" as the fix while
   * that control sits at the top of the transcript. Those callers page from where the
   * statement is read, and this deliberately does NOT scroll or move focus -- the reader
   * is mid-transcript at the message they mean to fork, or typing in the search field,
   * and satisfying the condition takes many pages, so relocating them on each one costs
   * more than the hunt it saves. Their in-flight cue is a spinner on the item instead.
   */
  const handleLoadEarlier = useCallback(() => {
    if (store.getState().chat.loadingOlder) return
    if (inspectorOn()) devLog('OLDER', 'manual-bar')
    void dispatch(loadOlderMessages())
  }, [dispatch])

  const virt = useVirtualChat<DisplayItem>({
    items: renderedDisplayItems,
    getKey: virtualKey,
    // Anchor resolution survives the per-landing key reshuffle by identifying
    // rows by their TAIL message — see stableAnchorIdFor.
    getStableId: stableAnchorId,
    getAltId: anchorAltId,
    prefetchStartIndex,
    sessionId: activeSlot ?? '__no_slot__',
    // Width-bucketed height scope: measured row heights are only valid for
    // the width they were measured at. The content column is capped at 900px
    // (+32px row padding), so every scroller wider than the cap shares ONE
    // bucket (desktop sidebar toggles do not re-measure); below the cap the
    // bucket quantizes to 16px so a phone, a rotated phone, and a narrow
    // desktop window each keep their own measured geometry.
    heightScopeKey: `${activeSlot ?? '__no_slot__'}@w${scrollerWidthBucket}`,
    estimatedHeight: 100,
    // Overscan tradeoff (experimental):
    //   smaller (3)   → least memory, frequent widget remounts on small scrolls
    //   medium  (12)  → screenful of buffer, ~290MB baseline / 450MB while scrolling
    //   larger  (25)  → fewer remounts but inflated RAM from warm iframe pool
    // Currently testing 6 — middle ground between memory and remount frequency.
    overscan: 6,
    // A first measurement lands in the offset tree immediately instead of
    // waiting out the height-sync debounce. Without this, a fast scroll or a
    // FAR jump mounts a streak of rows whose real heights sit outside the
    // spacer math for up to the debounce window; when they reconcile, content
    // shifts under the viewport. Chrome's native scroll anchoring absorbs
    // that shift, iOS Safari has none — measured 13-25px of post-jump drift
    // with anchoring disabled (the "jump lands off by a bit" report). First
    // measurements happen once per row, so they cannot be the oscillation the
    // debounce exists to smother.
    eagerFirstMeasure: true,
    // No isSticky: widget messages unmount along with everything else
    // when they leave the viewport window. Trade-off: scrolling back to
    // an old widget causes its iframe to reload (1-2 frames of flicker).
    // Memory benefit: only widgets in the active window are kept alive,
    // ~290MB baseline instead of 500MB+ with all-widgets-sticky.
    externalScrollerRef: scrollerRef,
    // The currently-streaming message is always the LAST message and
    // therefore always ends up in the LAST displayItems entry — whether
    // that entry is itself the streaming `single`, or a `turn`/`group`
    // that the streaming message got folded into (turns only close when a
    // new user/nudge message opens the next one, by which point the prior
    // streaming message has already finished). Passing its index lets the
    // virtualizer track that one row's growth every RO tick instead of
    // debouncing it into a stale-then-jump spacer (see the `streamingIndex`
    // option's doc and useVirtualChat.spacerLurch.test.tsx).
    streamingIndex: isStreaming && displayItems.length > 0 ? displayItems.length - 1 : undefined,
    // `slotRunning`, not `isStreaming`: a turn spends much of its life in tool
    // calls with no streaming row named, and follow has to keep working there.
    runActive: !!slotRunning,
    onTopReached: handleTopReached,
  })

  // Single scroll controller wiring: expose the virtualizer's follow API to
  // the early effects/handlers (declared above) via refs, and derive the
  // at-bottom state for the jump-to-bottom pill. The virtualizer owns slot
  // entry, streaming follow, and append-pin; ChatPage only triggers explicit
  // jumps (send, jump-to-latest pill) through these.
  const isAtBottom = virt.isAtBottom
  // Mirror the virtualizer's follow API into the refs the early effects/handlers
  // (declared above) read. Done in a layout effect rather than the render body
  // so a concurrent render React throws away can't write stale callbacks into
  // the refs. Layout effects run before passive effects, so the gating effects
  // that call vGetFollowRef.current() still see this commit's callback.
  useLayoutEffect(() => {
    vGetFollowRef.current = virt.getFollow
    vScrollerElRef.current = virt.scrollerRef?.current ?? vScrollerElRef.current
    vScrollToBottomRef.current = virt.scrollToBottom
    vFarmIsMeasuredRef.current = virt.farmIsMeasured
    earlierBarInViewRef.current = earlierBarInView
    mountIndexRef.current = virt.mountIndex
  })

  // Legacy aliases so the JSX below keeps reading the same names.
  const visibleDisplayItems = virt.virtualItems

  // A reader parked within one viewport of the absolute top while older
  // history remains is a STANDING request for more. Every edge-triggered
  // fire in this path has a death mode (measured on a pod rig — runs stalled
  // after 3/5/9/10 pages, nondeterministically): the top sentinel only fires
  // on intersection TRANSITIONS and a small landing never pushes it back out
  // of view; the window-start crossing needs a >lead→≤lead transition that a
  // top-pinned recompute skips; and a landing-edge chain races the scroll
  // compensation it reads. So this is deliberately LEVEL-triggered: a slow
  // poll, gated to near-top + hasMore + idle + no error. It cannot stack
  // requests (loadingOlder gates), cannot spin on a dead link (slotOlderError
  // gates; the scroll-retry below owns that path), and stops the moment the
  // reader leaves the top or history is exhausted.
  useEffect(() => {
    if (!slotHasMore) return
    const el = virt.scrollerRef?.current
    // The reader's own INPUT, not scroll position: a landing's compensation
    // moves scrollTop thousands of px without the reader touching anything,
    // which would otherwise end the walk after every single page — on desktop
    // (no rubber-band to hold the top) that reduced "load to the beginning"
    // to one page per manual climb. While the last fetch this poll issued is
    // newer than the last wheel/touch, the reader is still waiting on the
    // walk it started: keep going. Any input hands control back to the
    // near-top gate.
    // Both of these live in REFS, not in this effect's scope. As locals they were
    // re-created with the effect -- whose deps include `slotHasMore`, a flag that
    // history loading itself moves -- and each re-creation handed out a fresh full
    // budget plus a `lastInput` of "now", i.e. an authorization nobody gestured
    // for. A budget that a re-render can reissue is not a budget.
    // The walk needs the reader to have ACTUALLY climbed: a phone refresh
    // parks at the bottom with zero wheel/touch input ever, and boot-phase
    // transients (pre-layout scrollTop near 0, the giant-turn grouped list
    // briefly shorter than a viewport) can slip one self-issued page past
    // the near-top gate. With lastInput frozen at mount, that single
    // landing made `walking` TRUE FOREVER and the poll walked the entire
    // multi-megabyte transcript over a parked reader -- the field report
    // 'it just keeps loading previous after a refresh'. Requiring one real
    // input event this session before any poll-issued fetch turns the walk
    // back into what its own comment promises: reader-initiated.
    const noteInput = () => {
      walkLastInputAtRef.current = Date.now()
      walkPagesSinceInputRef.current = 0
      lastRealInputAtRef.current = Date.now()
      sentinelPagesSinceInputRef.current = 0
    }
    el?.addEventListener('wheel', noteInput, { passive: true })
    el?.addEventListener('touchmove', noteInput, { passive: true })
    // A wheel and a touch are not the only ways a human scrolls. PgUp/Home/space
    // and a scrollbar-thumb drag reach the top just as deliberately, and gating on
    // wheel/touchmove alone silences automatic older history for exactly the
    // readers who use them -- keyboard navigation being the accessibility path.
    // Both events are still unforgeable by us, which is the whole point of the
    // window: writing `scrollTop` fires neither, so an automatic scroll cannot
    // authorize itself. `keydown` is bound to the SCROLLER, not the document, so
    // typing in the composer is not mistaken for an intent to read history.
    el?.addEventListener('keydown', noteInput, { passive: true })
    el?.addEventListener('pointerdown', noteInput, { passive: true })
    // The walk is "in progress" when the newest older-page LANDING postdates
    // the newest user input — regardless of which trigger fired the fetch.
    // (An earlier draft keyed this on the poll's own fires and never engaged:
    // the sentinel/crossing triggers always win the race for the first page,
    // and its landing throws the reader off the near-top gate before the next
    // tick, so the poll never got the first fire it required.)
    let lastLanding = 0
    let prevLoading = false
    // Any scroll event — momentum coasting included, which fires no
    // wheel/touchmove — marks the transcript as still MOVING. A landing's
    // prepend compensation writes scrollTop, and on iOS a programmatic write
    // mid-momentum fights the fling's own curve: the reader sees the view
    // snap. Pages land only when the scroller is fully settled.
    let lastScrollEvt = 0
    const noteScroll = () => {
      lastScrollEvt = Date.now()
      // Motion kills any in-flight page: landings commit only while still.
      abortActiveOlderFetch()
    }
    el?.addEventListener('scroll', noteScroll, { passive: true })
    const t = setInterval(() => {
      const el2 = virt.scrollerRef?.current
      if (!el2 || el2.clientHeight <= 0) return
      const chat = store.getState().chat
      if (prevLoading && !chat.loadingOlder) lastLanding = Date.now()
      prevLoading = chat.loadingOlder
      const walking = lastLanding > walkLastInputAtRef.current
      // Authorization is a WINDOW, the same one the sentinel door uses -- not the
      // "has this session ever seen input" latch that used to gate this poll. The
      // latch could only ever turn ON: one flick authorized the walk for the rest
      // of the mount, `lastInput` froze at that flick, and since every landing
      // postdates it `walking` was true forever after. The page counter was then
      // the only brake, and it was reissued whenever this effect re-created. With
      // an expiring window the door is simply SHUT while nobody is scrolling,
      // which is what "reader-initiated" has to mean.
      if (!shouldContinueOlderWalk({
        sawRealInput: Date.now() - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
        nearTop: el2.scrollTop <= el2.clientHeight,
        walking,
        pagesSinceInput: walkPagesSinceInputRef.current,
      })) return
      // A bottom-followed reader is reading the LIVE end: the top-of-
      // transcript walk has nothing for them, and its landings are pure
      // disturbance budget. The sentinel/crossing triggers (reader-scroll
      // driven) still page history the moment they actually climb.
      if (vGetFollowRef.current()) return
      if (!earlierBarInViewRef.current()) return
      if (Date.now() - lastScrollEvt < OLDER_LANDING_SETTLE_MS) return
      // ...and stops entirely once they are no longer climbing. The settle gate
      // above only says "not mid-gesture", so on its own it made a reader who
      // STOPPED near the top the ideal candidate: they would sit still and watch
      // several pages land under them, one prepend each, which is felt as the
      // transcript starting to move on its own a second after they let go.
      // History still reaches back as far as they like -- it loads while they
      // climb, which is when they are asking for it.
      if (Date.now() - lastScrollEvt > OLDER_WALK_ACTIVE_MS) return
      if (!chat.slotHasMore || chat.loadingOlder || chat.slotOlderError) return
      if (chat.slotCursorKey !== chat.activeSlot) return
      // Same contract as the idle prefetch: a page lands only on FULLY
      // MEASURED geometry. Without this the walk outran the farm and piled
      // unmeasured rows over the parked reader -- every measurement landing
      // was an estimate correction under their eyes (reproduced on the rig
      // as per-landing twitches at the top). Turn-grouped pages measure in
      // ~1-2s, so the walk's pace barely changes.
      const nRows = displayItemsRef.current.length
      for (let i = 0; i < nRows; i++) { if (!virt.farmIsMeasured(i)) return }
      walkPagesSinceInputRef.current += 1
      if (inspectorOn()) devLog('OLDER', `walk p${walkPagesSinceInputRef.current}`)
      void dispatch(loadOlderMessages())
    }, OLDER_TOP_POLL_MS)
    return () => {
      clearInterval(t)
      el?.removeEventListener('scroll', noteScroll)
      el?.removeEventListener('wheel', noteInput)
      el?.removeEventListener('touchmove', noteInput)
      el?.removeEventListener('keydown', noteInput)
      el?.removeEventListener('pointerdown', noteInput)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- listeners re-arm on these triggers only; handlers read refs
  }, [slotHasMore, dispatch, virt.scrollerRef])

  // The sticky in-flight spinner is only meaningful where pages LAND — at the
  // top of the loaded transcript. `loadingOlder` is now true for the whole
  // automatic walk (a dozen pages back-to-back), so gating the spinner on the
  // fetch alone kept it pinned over the reader even mid-transcript. Track
  // "near the top" cheaply: the setState is value-stable away from the
  // threshold, so mid-scroll updates bail before rendering.
  const [spinnerNearTop, setSpinnerNearTop] = useState(true)
  useEffect(() => {
    const el = virt.scrollerRef?.current
    if (!el) return
    let raf = 0
    const onScroll = () => {
      // Cancel-and-reschedule, never latch-on-pending (frameSchedulerLatch
      // guard): a dropped frame handle must not wedge the near-top signal.
      if (raf) cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        raf = 0
        setSpinnerNearTop(el.scrollTop < el.clientHeight * 1.5)
      })
    }
    onScroll()
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => { el.removeEventListener('scroll', onScroll); if (raf) cancelAnimationFrame(raf) }
  }, [virt.scrollerRef, activeSlot])

  // A failed older-page fetch PARKS pagination: the top sentinel is already
  // inside the viewport, so no new crossing ever fires and automatic paging
  // never resumes — the only way forward is the retry bar, which on a phone
  // is easy to miss, so the transcript reads as "history ends here"
  // (reproduced: fail page 3 of 7 once, scrolling never fetches again).
  // Treat a FURTHER upward scroll as retry intent — the reader is still
  // asking for older content. Cooldown-gated so a dead link costs one
  // request per gesture, not one per scroll event; the thunk's own
  // loadingOlder gate covers the in-flight window.
  const olderRetryAtRef = useRef(0)
  useEffect(() => {
    const el = virt.scrollerRef?.current
    if (!el) return
    let prevTop = el.scrollTop
    const onScroll = () => {
      const st = el.scrollTop
      const up = st < prevTop
      prevTop = st
      if (!up) return
      const chat = store.getState().chat
      if (!chat.slotOlderError || chat.loadingOlder || !chat.slotHasMore) return
      const now = Date.now()
      if (now - olderRetryAtRef.current < OLDER_RETRY_COOLDOWN_MS) return
      olderRetryAtRef.current = now
      if (inspectorOn()) devLog('OLDER', 'error-retry')
      void dispatch(loadOlderMessages())
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [dispatch, activeSlot, virt.scrollerRef])
  // No "load more" pagination indicator with virtualization — the
  // windowing engine swaps mounted/placeholder automatically.

  // Reset scroll-navigation state on slot switch.
  useEffect(() => {
    setPinned(null)
    setPinExpanded(false)
  }, [activeSlot, setPinExpanded, setPinned])

  // Search: map message index → displayItems index for scroll-to-match
  const messageToDisplayIdx = useMemo(() => {
    const map = new Map<number, number>()
    displayItems.forEach((item, di) => {
      if (item.kind === 'turn') {
        for (const ti of item.items) {
          if (ti.kind === 'single') map.set(ti.idx, di)
          else if (ti.kind === 'group') ti.msgs.forEach((_, mi) => map.set(ti.startIdx + mi, di))
        }
      } else if (item.kind === 'single') map.set(item.idx, di)
      else if (item.kind === 'group') item.msgs.forEach((_, mi) => map.set(item.startIdx + mi, di))
    })
    return map
  }, [displayItems])

  const chatNav = useChatNavigation(messages, messageToDisplayIdx)

  // ── Chat Pins ──────────────────────────────────────────────────────────────
  const {
    pins: chatPins,
    loading: chatPinsLoading,
    error: chatPinsError,
    clearError: clearChatPinsError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
  } = useChatPins(activeSlot ?? undefined)
  const [pinNotice, setPinNotice] = useState<string | null>(null)
  const [pendingPinnedJump, setPendingPinnedJump] = useState<{
    slotKey: string
    messageTs: string
    mid?: string
    // Required, not optional: the entry points render different copy, and a new
    // caller that omitted it would silently show pin wording.
    origin: PendingJumpOrigin
  } | null>(null)
  // No arbitrary cap on pinned-jump page loads: the loop terminates when the
  // target message is found OR history is exhausted (!slotHasMore / null result).
  // The `cancelled` flag in the useEffect cleanup and the loadOlderMessages null
  // sentinel prevent infinite loops. A ref tracks loads for diagnostics only.
  const pinnedJumpPageLoadsRef = useRef(0)
  const jumpToLoadedPinnedMessage = useCallback((messageTs: string, mid?: string): boolean => {
    // Mid-based resolution when a mid is known; ts ONLY for legacy pins that carry none.
    // Falling through to ts with a mid in hand takes a same-tick twin, which is the wrong row.
    const msgIdx = mid
      ? messages.findIndex(m => (m.meta as Record<string, unknown> | undefined)?.mid === mid)
      : messages.findIndex(m => m.ts === messageTs)
    if (msgIdx < 0) return false
    const di = messageToDisplayIdxRef.current.get(msgIdx)
    if (di === undefined) return false
    setPinNotice(null)
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
    setHighlightTs(messageTs)
    setTimeout(() => setHighlightTs(null), 3000)
    return true
  }, [messages, navToDisplayIndex, setHighlightTs])
  const handleJumpToPinnedMessage = useCallback((messageTs: string, mid: string | undefined, { origin }: { origin: PendingJumpOrigin }) => {
    if (jumpToLoadedPinnedMessage(messageTs, mid)) return
    if (activeSlot && (!cursorIsForActiveSlot || (slotHasMore && slotOldestIndex > 0))) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(null)
      setPendingPinnedJump({ slotKey: activeSlot, messageTs, mid, origin })
      return
    }
    // Same writer as the async branch below, so the synchronous dead-link case
    // cannot drift into pin wording while the paging case reports the truth.
    setPinNotice(jumpUnavailableNotice(origin))
  }, [activeSlot, cursorIsForActiveSlot, jumpToLoadedPinnedMessage, slotHasMore, slotOldestIndex])
  // The pins list's own entry point, so pin copy is claimed HERE by a caller that
  // means it rather than inherited by one that passed nothing.
  const handleJumpToPin = useCallback((messageTs: string, mid?: string) => {
    handleJumpToPinnedMessage(messageTs, mid, { origin: 'pin' })
  }, [handleJumpToPinnedMessage])
  useEffect(() => {
    if (!pendingPinnedJump) return
    if (pendingPinnedJump.slotKey !== activeSlot) {
      pinnedJumpPageLoadsRef.current = 0
      setPendingPinnedJump(null)
      return
    }
    // Captured per effect run so the async branches below report the entry point
    // this jump came from, not whichever one ran last.
    const notFoundNotice = jumpUnavailableNotice(pendingPinnedJump.origin)
    // A fetch that errored is transient, so the not-found copy would tell the reader
    // their history is gone. `link` shares the retry copy: it makes no origin claim.
    const loadFailedNotice = pendingPinnedJump.origin === 'earlier' || pendingPinnedJump.origin === 'link'
      ? i18nT('components.chatPane.earlier_messages_load_failed')
      : notFoundNotice
    if (jumpToLoadedPinnedMessage(pendingPinnedJump.messageTs, pendingPinnedJump.mid)) {
      pinnedJumpPageLoadsRef.current = 0
      // A jump resolved against the bounded page is provisional: the full
      // transcript prepends older rows, so re-resolve once it has replaced it.
      if (!activeViewIsBoundedPage) setPendingPinnedJump(null)
      return
    }
    // The cursor still describes the chat we left; wait for the switch to settle
    // rather than read its has-more as this chat's.
    if (!cursorIsForActiveSlot) return
    if (!slotHasMore || slotOldestIndex <= 0) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(notFoundNotice)
      setPendingPinnedJump(null)
      return
    }
    if (loadingOlder) return

    // Counted for diagnostics only, deliberately NOT compared against a ceiling:
    // an arbitrary page cap is what made a distant pin in a resumed session report
    // itself "unavailable" when it simply needed more pages, and it was removed for
    // that reason (chatPins.test.ts, 'no arbitrary page-load cap'). The loop is
    // bounded by the history itself -- the target is found, `slotHasMore` goes
    // false, or `slotOldestIndex` reaches 0 -- and it is walking history the reader
    // ASKED for by tapping the pin, which is not the unasked loading this branch is
    // about. The real fix for a very distant pin is a server query that fetches
    // AROUND a message id; until that exists, paging is the honest behaviour.
    pinnedJumpPageLoadsRef.current += 1
    if (inspectorOn()) devLog('OLDER', `jump p${pinnedJumpPageLoadsRef.current}`)
    let cancelled = false
    void dispatch(loadOlderMessages()).unwrap().then(result => {
      if (!cancelled && result === null) {
        pinnedJumpPageLoadsRef.current = 0
        setPinNotice(notFoundNotice)
        setPendingPinnedJump(null)
      }
    }).catch(err => {
      // Cancelled or refused means the user switched chat, not that the pin is
      // unreachable.
      if (isSupersededPagingRejection(err)) return
      if (!cancelled) {
        pinnedJumpPageLoadsRef.current = 0
        setPinNotice(loadFailedNotice)
        setPendingPinnedJump(null)
      }
    })
    return () => { cancelled = true }
  }, [
    activeSlot,
    activeViewIsBoundedPage,
    cursorIsForActiveSlot,
    dispatch,
    jumpToLoadedPinnedMessage,
    loadingOlder,
    pendingPinnedJump,
    slotHasMore,
    slotOldestIndex,
  ])
  const handleTogglePinForMessage = useCallback((mid: string, messageTs: string, role: 'user' | 'assistant', content: string) => {
    if (isPinned(mid)) {
      void unpinMessage(mid).catch(() => {}) // useChatPins exposes the localized error state.
      return
    }
    // A session's FIRST pin opens the Pins tab, so the pin has a visible
    // destination -- the same shape as the Issues reveal, and for the same
    // reason: Pins is an on-demand view, so nothing would surface it otherwise.
    // A session pinned earlier reaches it through the + menu (Issues' zero
    // option for pre-existing links), which is what keeps this free of a
    // persisted reveal claim.
    // Read before the mutation so the optimistic insert has not landed yet.
    const isFirstPin = chatPins.length === 0
    void pinMessage({ mid, message_ts: messageTs, role, preview: content }).catch(() => {})
    if (isFirstPin && activeSlot) {
      // Addressed by slot, not through tabsCtl, for the same reason as the
      // source-reveal path: that binding can be a chat being left.
      openPanelView(activeSlot, 'pins')
      // Pinning is NOT a navigation request, so it must not cost the user state
      // they are mid-way through. Unlike the source-reveal path this does not
      // close the find pane: someone who searched the transcript to FIND the
      // message they are pinning would lose the pane and its results on the very
      // click that acts on a result. Below the mobile breakpoint the panel opens
      // full width, so opening it would navigate them off the chat entirely.
      // The tab is still created above -- it is revealed quietly instead.
      if (!search.isOpen && !isMobile) dispatch(openActivityPanel())
    }
  }, [activeSlot, chatPins.length, dispatch, isMobile, isPinned, pinMessage, search.isOpen, unpinMessage])
  const handleUnpinById = useCallback((id: string) => {
    void unpinById(id).catch(() => {})
  }, [unpinById])
  const pinStatus = pinNotice ?? (chatPinsError
    ? i18nT(chatPinsError === 'pin' ? 'pages.chat.pins.pin_failed' : chatPinsError === 'pin_limit' ? 'pages.chat.pins.pin_limit_reached' : 'pages.chat.pins.unpin_failed')
    : null)
  const dismissPinStatus = useCallback(() => {
    setPinNotice(null)
    clearChatPinsError()
  }, [clearChatPinsError])
  useEffect(() => {
    if (!pinStatus) return
    const timeout = window.setTimeout(dismissPinStatus, 8000)
    return () => window.clearTimeout(timeout)
  }, [pinStatus, dismissPinStatus])

  // Track the timestamp of the previous search-nav step so we can tell "user is
  // holding Enter through many matches" apart from "user landed on one match".
  // Rapid consecutive steps snap instantly (behavior:'auto') — a smooth glide
  // would be interrupted and restarted on every keypress, producing the stutter
  // of half-finished eased scrolls. A lone step (or the final one after a pause)
  // glides smoothly and centers. navToDisplayIndex still forces 'auto' for FAR
  // jumps regardless; this only governs NEAR jumps, which is where the queued-
  // animation jank lived.
  const lastSearchStepAtRef = useRef(0)
  // Set when the user clicks a row in the results panel (vs. Enter/Arrow
  // stepping). A click is a direct jump that's usually FAR and to an unmeasured
  // virtualized row — a smooth scroll animates to the *estimated* offset and
  // then visibly corrects once the row mounts. Snapping instantly collapses
  // that into one jump.
  const searchClickJumpRef = useRef(false)
  // Cancel handle for the re-click converge loop (below) so repeated re-clicks
  // of the same result don't stack concurrent loops + window listeners.
  const reclickScrollCancelRef = useRef<(() => void) | null>(null)
  // Read the display-index map via a ref so the scroll effect below does NOT
  // re-fire when the map is rebuilt (every new message / stream chunk rebuilds
  // it). Otherwise an open search pane would yank the chat back to the current
  // match each time the agent emits output. The effect should scroll only on
  // deliberate search navigation (currentIdx / currentMessageIdx change).
  const messageToDisplayIdxRef = useRef(messageToDisplayIdx)
  messageToDisplayIdxRef.current = messageToDisplayIdx
  const jumpToSearchResult = useCallback((i: number) => {
    // Re-clicking the already-selected result won't change currentIdx, so the
    // nav effect won't fire — scroll back to it imperatively so a click always
    // returns to the match even after the user has scrolled away from it.
    if (i === search.currentIdx) {
      const m = search.matches[i]
      const di = m ? messageToDisplayIdxRef.current.get(m.msgIdx) : undefined
      if (di !== undefined) {
        requestAnimationFrame(() => {
          navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
          // currentOcc is unchanged so the message's occurrence-scroll effect
          // won't re-run; converge-center the already-rendered active mark.
          reclickScrollCancelRef.current?.()
          reclickScrollCancelRef.current = scrollCurrentMatchIntoView()
        })
      }
      return
    }
    searchClickJumpRef.current = true
    search.goTo(i)
  }, [search, navToDisplayIndex])
  useEffect(() => {
    if (search.currentMessageIdx < 0) return
    const di = messageToDisplayIdxRef.current.get(search.currentMessageIdx)
    if (di === undefined) return
    const now = performance.now()
    const behavior = searchClickJumpRef.current
      ? 'auto'
      : pickSearchScrollBehavior(now, lastSearchStepAtRef.current)
    searchClickJumpRef.current = false
    lastSearchStepAtRef.current = now
    navToDisplayIndex(di, { behavior, align: 'center' })
  }, [search.currentMessageIdx, search.currentIdx, navToDisplayIndex])

  // "Show in chat" button on the approval bar dispatches openActivityToTool,
  // which sets `focusToolCallId`. Pulling a virtualised pill back into the DOM
  // requires Virtuoso's own scrollToIndex — direct DOM scrollIntoView fails
  // because the element doesn't exist. ToolCallLine's own effect then takes
  // over once it mounts: refines the scroll position and clears the focus.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (!focusToolCallId) return
    const msgIdx = messages.findIndex(m =>
      m.role === 'tool' && m.meta?.tool_call_id === focusToolCallId
    )
    if (msgIdx < 0) return
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
  }, [focusToolCallId, messages, messageToDisplayIdx, navToDisplayIndex])

  // Deep-link: scroll to ?msg= timestamp on cold load.
  // When ?mid= is also present (copied from a pinned-message link), resolve by
  // mid first (stable per-message identity) and fall back to ts for legacy links.
  // The scroll-to-bottom effect above is suppressed while initialMsgRef is set.
  // Safety net: clear both refs after 5s to restore scroll-to-bottom if deep-link fails.
  useEffect(() => {
    if (!initialMsgRef.current) return
    const timer = setTimeout(() => { initialMsgRef.current = null; initialMidRef.current = null }, 5000)
    return () => clearTimeout(timer)
  }, [initialMsgRef, initialMidRef])
  useEffect(() => {
    const targetTs = initialMsgRef.current
    const targetMid = initialMidRef.current
    if (!targetTs || messages.length === 0) return
    // `messages` can still be the chat being left while a ?sid= switch settles,
    // so decide only once this window is known to belong to the target chat.
    if (initialSidRef.current && initialSidRef.current !== activeSlot) return
    if (!cursorIsForActiveSlot) return
    // The captured pair predates the mount effect that dispatches `switchSlot`, whose
    // `pending` nulls the cursor key even on a same-key switch -- so read it live.
    const liveChat = store.getState().chat
    if (liveChat.slotCursorKey !== liveChat.activeSlot) return
    const resolved = resolveMsgIndex(messages, targetTs, targetMid)
    // A mid that is merely OFF-PAGE falls back to ts in the helper, and that is a
    // DIFFERENT row of the same tick -- treat it as unresolved so the hand-off runs.
    const msgIdx = targetMid && messages[resolved]?.meta?.mid !== targetMid ? -1 : resolved
    if (msgIdx < 0) {
      // A bounded first page need not contain the target; the jump path already
      // gates on the cursor and reports a dead link, so the decision lives there.
      initialMsgRef.current = null
      // Carries `targetMid`: paging back re-resolves, and ts alone would pick the
      // wrong message of a same-ts pair that the mid exists to disambiguate.
      handleJumpToPinnedMessage(targetTs, targetMid ?? undefined, { origin: 'link' })
      return
    }
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    initialMsgRef.current = null
    initialMidRef.current = null
    setTimeout(() => {
      navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
      setHighlightTs(targetTs)
      setTimeout(() => setHighlightTs(null), 3000)
    }, 500)
  }, [messages, messageToDisplayIdx, slotHasMore, slotOldestIndex, handleJumpToPinnedMessage, activeSlot, cursorIsForActiveSlot]) // eslint-disable-line react-hooks/exhaustive-deps

  // Precomputed O(n) map from message index → visible (user/assistant) index,
  // used by the fork button. Avoids a per-row O(i) filter that would make the
  // renderer O(n²) overall.
  const visibleIndexMap = useMemo(() => {
    const map = new Map<number, number>()
    let count = 0
    for (let idx = 0; idx < messages.length; idx++) {
      const r = messages[idx].role
      if (r === 'user' || r === 'assistant') {
        map.set(idx, count)
        count++
      }
    }
    return map
  }, [messages])

  const activeSlotTitle = filteredSlots.find(s => s.key === activeSlot)?.title

  // `filteredSlots`, not `slots`: a surface this page cannot render would chip to a
  // destination the switch clears. Signature because heartbeats remint slot objects.
  const sessionTitleSig = JSON.stringify(filteredSlots.map(s => [s.key, s.title || s.key]))
  const sessionTitles = useMemo(
    () => new Map(filteredSlots.map(s => [s.key, s.title || s.key] as const)),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the value-equal pair signature, not the slot objects (see above)
    [sessionTitleSig],
  )

  // Below `sessionTitles`, not up with the other render callbacks: the session
  // triple it forwards reads that map, and a dependency array is evaluated in
  // the render body, so declaring this earlier would read it before its
  // initializer runs.
  const renderUserContentCb = useCallback(
    // The session triple matches what the assistant / note rows hand
    // MarkdownRenderer (see the AssistantMessage and inject branches below),
    // so a `/chat?sid=…` link behaves identically across row kinds (#8253).
    (c: string, mt: Record<string, unknown> | undefined) => renderUserContent({
      content: c,
      meta: mt,
      onFileOpen: handleFileOpen,
      onFolderOpen: handleFolderOpen,
      linkPreviews: linkPreviewsOn,
      onSessionOpen: selectSessionTab,
      sessions: connected ? sessionTitles : undefined,
      activeSession: activeSlot || undefined,
    }),
    [handleFileOpen, handleFolderOpen, linkPreviewsOn, selectSessionTab, connected, sessionTitles, activeSlot]
  )

  // Session documents (in-session artifacts) for the active slot. Used only to
  // badge file-change rows that are tracked docs/artifacts (e.g. a generated
  // PR body) rather than source-file edits. Shares the ['session-artifacts',
  // slot] query key with the Artifacts tab so it's a single deduped fetch; the
  // memoized Set keeps AssistantMessage's memo stable across renders.
  const { data: sessionDocs } = useQuery({
    queryKey: ['session-artifacts', activeSlot],
    queryFn: () => api.artifactSessionDocs(activeSlot || undefined),
    enabled: !!activeSlot,
    staleTime: 15_000,
  })
  const artifactPaths = useMemo(
    () => new Set((sessionDocs?.docs || []).map(d => d.path)),
    [sessionDocs],
  )

  // Flush-volatile positional state is read through refs so a streaming flush
  // (which replaces `messages` and rebuilds the derived index/tail values)
  // does not mint a new renderMessage -> renderTurnItem identity and defeat
  // memo(TurnBlock) for every settled turn. The refs are synced per render, so
  // a callback invoked during THIS render's children sees current values.
  // UI-state deps (chatConfig, linkPreviewsOn, disclosure, pin state, ...)
  // deliberately STAY in the dep array: when they change, settled turns must
  // re-render with the new behavior, and the changed identity is what breaks
  // through the memo.
  const visibleIndexMapRef = useRef(visibleIndexMap); visibleIndexMapRef.current = visibleIndexMap
  const lastTextIdxRef = useRef(lastTextIdx); lastTextIdxRef.current = lastTextIdx
  const slotStateRef2 = useRef(slotState); slotStateRef2.current = slotState

  // ── Registry-driven row dispatch (chat-core P5-a) ──
  // Every transcript row on this page resolves through the SAME renderer
  // registry the other surfaces consume (app-sdk/messageRenderers), so a role
  // registered once renders everywhere -- the double-wiring defect class
  // (`mcp_oauth` shipped wired in app-sdk and raw in the main chat) is closed
  // structurally rather than by the parity test alone. This page's chrome (tool
  // disclosure state, fork/pin/footer, the error card's Continue, the nudge
  // card's Loop button, ...) rides as HOST ENTRIES that reuse the default ids
  // they replace, plus a few page-only shape entries. Order inside this array
  // is the page's precedence order, unchanged from the if-chain it replaces:
  // the shared dashboard set (sub-agent completion, launch cards, tool,
  // thinking, file, nudge, recovery inject, workflow completion, error), then
  // stop_event, notice, permission, undrawn, mcp_oauth, hidden invisible
  // assistant, and the conversational bubble. Roles none of these
  // claim fall to the registry defaults (`undrawn` for queued/system/done and
  // the reasoning roles; `tool_lifecycle` for raw wire shapes the store
  // normalizes away), and a role NOBODY claims renders as the bubble, which is
  // what the if-chain's fall-through did.
  //
  // Memoized with the deps the old renderMessage carried: UI-state deps
  // (chatConfig, linkPreviewsOn, disclosure, pin state, ...) deliberately STAY
  // in the array so settled turns re-render with the new behavior, and the
  // changed identity is what breaks through memo(TurnBlock).
  const { renderers: chatPageRenderers, fallback: bubbleRenderer } = useMemo<{ renderers: readonly MessageRenderer[]; fallback: MessageRenderer }>(() => {
    /** The conversational row: user / inject (cron & recovery prose) / assistant. */
    const bubble: MessageRenderer = {
      id: 'bubble',
      roles: ['user', 'assistant', 'streaming', 'inject'],
      render: (m, ctx) => {
        const i = ctx.index
        const key = ctx.key
    const isUser = m.role === 'user'
    const isStreaming = m.role === 'streaming'
    const isInject = m.role === 'inject'
    // Pass a stable handleFork (useCallback) + primitive index so memo()
    // on AssistantMessage can short-circuit when only unrelated state changes.
    // visibleIndexMap is O(1) per row.
    const messageId = typeof m.meta?.mid === 'string' && m.meta.mid ? m.meta.mid : undefined
    const canResolveOnServer = !!messageId && !isStreaming && !isInject
    const canFork = canResolveOnServer || canForkAtWindow({ isStreaming, isInject, slotHasMore, cursorIsForActiveSlot })
    const forkIndex = canFork ? visibleIndexMapRef.current.get(i) : undefined
    const msgTime = fmtMessageTime(m.ts)
    const msgTimeFull = fmtMessageTimeFull(m.ts)
    return (
      <MessageSearchScope key={key} messageIdx={i}>
      <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''} ${m.ts && m.ts === highlightTs ? 'animate-msg-highlight rounded-lg' : ''}`}>
        <div className={`flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${isUser ? 'items-end' : ''}`}>
          {isUser ? (
            <UserMessage
              content={m.content}
              meta={m.meta}
              timestamp={chatConfig.showTimestamps ? msgTime : undefined}
              timestampTitle={msgTimeFull}
              renderContent={renderUserContentCb}
              canEdit={!slotRunning && !regenerating && !!activeSlot}
              messageIndex={i}
              messageTs={m.ts || ''}
              onEditResend={handleEditResend}
              slotKey={activeSlot || undefined}
              slotTitle={activeSlotTitle}
              mode={mode}
              pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false}
              onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? () => handleTogglePinForMessage((m.meta as Record<string, unknown>).mid as string, m.ts!, 'user', m.content) : undefined}
            />
          ) : isInject ? (
            (() => {
              const cronLabel = (m.meta?.cronLabel as string) || ''
              // Strip wrapper tags — LLM needs them for context but user sees clean content
              const stripped = cronLabel
                ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
                : m.content
              // A note's marker is consumed into the pill row, so rendering it too would show
              // the same choices twice. Non-note inject rows keep it: there it is prose.
              const cleanContent = isNoteRow(m) ? parseOptions(stripped).text : stripped
              return <>
                {cronLabel && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1"><Clock className="lucide-inline" /> {cronLabel}</span>}
                {/* Same session wiring as the assistant branch. Without it `resolveSessionChip`
                    refuses at its first guard and a `/chat?sid=` link gains `target="_blank"`. */}
                <div className="msg-content px-4 py-3 text-sm leading-6 rounded-lg bg-warn-subtle text-text ring-1 ring-inset forced-colors:border ring-warn/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}><MessageErrorBoundary rawContent={cleanContent}><MarkdownRenderer content={cleanContent} onSessionOpen={selectSessionTab} sessions={connected ? sessionTitles : undefined} activeSession={activeSlot || undefined} softBreaks /></MessageErrorBoundary></div>
                {/* No `font-mono`: a formatted date is prose, and Tailwind's
                    `font-mono` pins `var(--mono)` — a token the Font Family
                    setting never writes, so it overrode the user's choice and
                    put JetBrains Mono (no CJK coverage) under a date that a
                    zh/ja dashboard renders WITH CJK characters. `tabular-nums`
                    keeps the digits fixed-width, which is the alignment the
                    mono was actually there for. */}
                {chatConfig.showTimestamps && msgTime && <span className="text-muted text-[12px] leading-4 tabular-nums px-1" title={msgTimeFull}>{msgTime}</span>}
              </>
            })()
          ) : (
            <div className="flex flex-col gap-0">
              <AssistantMessage suppressSteerAck={turnHadPolicyBlock(messagesRef.current, i)} prevUserText={prevUserTextFor(messagesRef.current, i)} shareEnabled={socialShareOn} linkPreviews={linkPreviewsOn} content={m.content} isStreaming={isStreaming} isRegenerating={regenerating && i === lastTextIdxRef.current} onFileOpen={handleFileOpen} onFolderOpen={handleFolderOpen} onArtifactOpen={handleArtifactOpen} onSessionOpen={selectSessionTab} sessions={connected ? sessionTitles : undefined} activeSession={activeSlot || undefined} onQuote={handleQuote} onAsk={handleAsk} slotRunning={slotRunning} planTaskId={planTaskId} timestamp={chatConfig.showTimestamps ? msgTime : undefined} timestampTitle={msgTimeFull} messageTs={m.ts} slotKey={activeSlot || undefined} slotTitle={activeSlotTitle} mode={mode} fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined} turnStats={chatConfig.showTurnStats ? (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined : undefined} onOpenDiff={handleOpenDiff} fileChipStyle={chatConfig.fileChipStyle} artifactPaths={artifactPaths} pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false} onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? () => handleTogglePinForMessage((m.meta as Record<string, unknown>).mid as string, m.ts!, 'assistant', m.content) : undefined} showFooter={(() => {
                // Show footer on the last assistant message of each completed turn
                if (isStreaming) return false
                // Find next message after this one that's assistant, user, or streaming
                for (let j = i + 1; j < messagesRef.current.length; j++) {
                  const later = messagesRef.current[j]
                  if (later.role === 'user') return true // end of turn — show footer
                  // A hidden invisible-only row draws nothing, so it cannot
                  // host the footer; pass over it to the row that renders.
                  if (isHiddenInvisibleAssistantRow(later)) continue
                  if (later.role === 'assistant' || later.role === 'streaming') return false // not last assistant in turn
                }
                // End of messages. A run still in progress has not produced this
                // turn's footer yet — but a message that already CARRIES turn
                // stats is a turn that finished, and a LATER run (a cron, a
                // monitor cycle, another tab, a background job) must not retract
                // the footer of a turn it has nothing to do with.
                //
                // Retracting it removed the stats line, the timestamp row and the
                // overflow trigger — measured frame by frame from a phone
                // recording: ~108px, gone for 3 frames at 60fps, at the very
                // bottom edge of the transcript. Content shrinking there makes
                // the engine clamp a bottom-parked reader down, and nothing ever
                // pushes them back up when the footer returns, so each flicker
                // cost the reader their position permanently.
                const stats = (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined
                if (stats && (stats.elapsed_ms ?? 0) > 0) return true
                return !slotRunning
              })()} onSpeak={handleSpeak} onRegenerate={i === lastTextIdxRef.current && !slotRunning && !regenerating && activeSlot ? handleRegenerate : undefined} variants={m.variants} variantIdx={m.variant_idx} onSwitchVariant={i === lastTextIdxRef.current && m.variants && m.variants.length > 1 && activeSlot ? (idx: number) => { api.switchVariant(activeSlot, idx).catch((e: unknown) => {
                showRefusedPress('switch_variant', e)
              }) } : undefined} onFork={embedded && !popout ? undefined : handleFork} onPlanFromHere={embedded && !popout ? undefined : handlePlanFromHere} forkIndex={forkIndex} forkMessageId={canResolveOnServer ? messageId : undefined} onLoadEarlier={cursorIsForActiveSlot ? handleLoadEarlier : undefined} loadingOlder={loadingOlder} earlierRemaining={slotOldestIndex} onApplyPlan={handleApplyPlan} />
            </div>
          )}
        </div>
      </div>
      </MessageSearchScope>
    )
      },
    }
    // The dashboard's shared row set (pages/chat/transcriptRenderers.tsx --
    // tool lines and launch cards, thinking block, nudge, recovery inject, the
    // two completion cards, the error card with Continue), wired with this
    // page's behaviours through its options; ChatPane calls the same factory
    // with fewer. Only rows that are genuinely page-specific follow it.
    const shared = createTranscriptRenderers({
      slot: activeSlot || undefined,
      // An unparseable file row has always fallen through to the bubble on
      // this page (a pane draws nothing for it); P5-b changes no row's output.
      renderUnparsedFile: (m, ctx) => bubble.render(m, ctx),
      onFileOpen: handleFileOpen,
      onFolderOpen: handleFolderOpen,
      onOpenSubagentPanel: handleSubagentPanelOpen,
      toolDisclosure,
      onToolDisclosureChange: setToolDisclosureFor,
      // Animate tools in the trailing group (after last assistant/streaming text).
      toolRunning: (_m, ctx) => slotStateRef2.current === 'tool_running' && ctx.index > lastTextIdxRef.current,
      transcriptHot,
      appInPanel: mcpAppPanel,
      onOpenApp: revealAppInPanel,
      // The Loop button is offered only when this row's own loop is the one
      // still bound to the slot, so a historical card never opens a successor
      // loop's controls (the match rule lives in the factory).
      activeNudgeLoopId: autoNudgeLoop?.id,
      onOpenNudgeLoop: () => setAutoNudgeOpen(true),
      continuable,
      interrupted,
      continuing,
      onContinue: handleContinue,
      onSessionOpen: selectSessionTab,
      sessions: connected ? sessionTitles : undefined,
      activeSession: activeSlot || undefined,
    })
    const renderers = mergeRenderers([
      ...shared,
      {
        id: 'stop_event',
        roles: ['*'],
        match: m => m.kind === 'stop_event' || m.meta?.kind === 'stop_event',
        render: (m, ctx) => <StopEventCard key={m.meta?.id as string ?? ctx.key} message={m} />,
      },
      { id: 'notice', roles: ['notice'], render: (m, ctx) => <NoticeCard key={ctx.key} content={m.content} /> },
      {
        // Approval flow: the permission cards own it; grouped, never a standalone row.
        id: 'permission',
        roles: ['permission'],
        render: () => null,
      },
      {
        // The page's undrawn set is NARROWER than the SDK default's: reasoning
        // roles without reasoning content (the old `isReasoningRole -> null`
        // arm) and the queue rail's rows draw nothing here, but `system` /
        // `done` -- lifecycle markers this store never carries -- are left
        // unclaimed on purpose, so they take the bubble fallback exactly as the
        // if-chain's fall-through did rather than vanishing.
        id: 'undrawn',
        roles: [...REASONING_ROLES, 'queued'],
        render: () => null,
      },
      {
        id: 'mcp_oauth',
        roles: ['mcp_oauth'],
        render: (m, ctx) => {
          const key = ctx.key
      const banner = renderMcpOAuthMessage(m, connectionsUiOn)
      return banner ? <div key={key}>{banner}</div> : null
        },
      },
      {
        // A quiet monitor-loop cycle replies with a bare zero-width space
        // (U+200B): the content is truthy but renders as nothing, so the row
        // would draw as an empty bubble -- one per quiet cycle, historical
        // transcripts included. Skip it; rows carrying file-change chips still
        // render (the chips are the content). Same skip as the app-sdk registry.
        id: 'hidden_invisible_assistant',
        roles: ['*'],
        match: isHiddenInvisibleAssistantRow,
        render: () => null,
      },
      bubble,
    ])
    return { renderers, fallback: bubble }
    // dispatch/navigate are stable; handleOpenDiff/handlePlanFromHere are
    // memoized callbacks; planTaskId is read when rendering the plan footer /
    // apply-plan handler, so it belongs here for correctness. approve/send/
    // dismissApproval are NOT referenced in this renderer (user/approval rows go
    // through renderUserContentCb), so they are omitted to keep it stable.
    // cursorIsForActiveSlot/slotOldestIndex/handleLoadEarlier belong here: a switch
    // back restores the cursor while changing no other dep, stranding Fork shut.
    // embedded/popout gate whether Fork and Plan-from-here are handed to the row at
    // all, so a stale copy would leave both live inside an embedded pane.
    //
    // revealAppInPanel is named here rather than excluded: it depends on the find
    // pane's `close` member (identity-stable) rather than the whole `search` object
    // useMessageSearch rebuilds as a fresh literal every render, so it holds one
    // identity and cannot churn this callback — or renderTurnItem below it — and
    // defeat memo(TurnBlock) for settled turns. Excluding it instead would leave it
    // captured across a render where the find pane opens, and the stale copy would
    // open an app tab behind the still-hidden dock.
  }, [slotRunning, handleFileOpen, handleFolderOpen, handleArtifactOpen, selectSessionTab, sessionTitles, connected, handleFork, handleQuote, handleAsk, chatConfig, activeSlot, regenerating, handleRegenerate, handleEditResend, slotHasMore, loadingOlder, cursorIsForActiveSlot, slotOldestIndex, handleLoadEarlier, renderUserContentCb, highlightTs, activeSlotTitle, mode, embedded, popout, handleOpenDiff, handlePlanFromHere, planTaskId, artifactPaths, autoNudgeLoop, setAutoNudgeOpen, toolDisclosure, setToolDisclosureFor, linkPreviewsOn, socialShareOn, handleSubagentPanelOpen, isPinned, handleTogglePinForMessage, connectionsUiOn, showRefusedPress, transcriptHot, mcpAppPanel, revealAppInPanel, continuable, interrupted, handleContinue, continuing, messagesRef, handleSpeak, handleApplyPlan])

  const renderMessage = useCallback((i: number, m: ChatMessage) => {
    // Key identity rules (clientTs preference + streaming->assistant role
    // normalization) live in messageRowKey -- see its doc comment.
    const key = messageRowKey(m, i)
    const ctx: MessageRenderContext = {
      index: i,
      messages: messagesRef.current,
      running: slotRunning,
      key,
      onFileOpen: handleFileOpen,
      hideCardOwnedOAuth: connectionsUiOn,
      autoDeniedIds: NO_AUTO_DENIED,
      // The shared row set returns `ctx.row(...)`; the row must be a KEYED
      // passthrough, not an element, so a tool line lands in the DOM exactly as
      // this page's own entry used to render it (the virtualizer measures the
      // row's component root). `wrapper` is reached only by a registry default
      // this page does not override (raw wire-shape tool rows).
      wrapper: (children) => <Fragment key={key}>{children}</Fragment>,
      row: (children) => <Fragment key={key}>{children}</Fragment>,
    }
    const entry = resolveRenderer(m, chatPageRenderers)
    // A role nobody claims renders as the conversational bubble -- what the
    // if-chain's fall-through did, so an unknown role is visible, never lost.
    // By reference: the merged list's tail is an SDK default, not the bubble.
    return (entry ?? bubbleRenderer).render(m, ctx)
  }, [chatPageRenderers, bubbleRenderer, slotRunning, handleFileOpen, connectionsUiOn, messagesRef])

  // Hoisted out of the row map so every TurnBlock receives the SAME function
  // identity per render — an inline closure there re-created it per row per
  // render and defeated memo(TurnBlock) even when the turn identity was stable
  // (see createTurnGrouper). It depends on nothing row-specific.
  const renderTurnItem = useCallback((it: TurnItem, _j: number) => {
    // Skip hidden tool messages (✅/🚫 completions) to avoid empty py-1 wrappers
    if (it.kind === 'single' && it.msg.role === 'tool' && !it.msg.content.startsWith('🔧')) return null
    // Same for hidden invisible-only assistant rows: renderMessage draws
    // nothing for them, and the bare wrapper would still stack py-1 spacers,
    // one per quiet monitor cycle.
    if (it.kind === 'single' && isHiddenInvisibleAssistantRow(it.msg)) return null
    return <div key={turnLeadKey(it, stableMsgKey)} className={`px-4 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      {it.kind === 'group' ? (() => {
        const unresolvedPerms = it.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
        // Skip group entirely if it only contains unresolved permissions (handled by ApprovalBar)
        if (it.msgs.every(m => m.role === 'permission')) return null
        return (
        <CollapsibleToolGroup
          count={it.msgs.filter(m => m.role !== 'permission').length}
          disclosureKey={`ctg-${turnLeadKey(it, stableMsgKey)}`}
          hasPermission={false}
          isRunning={false}
          permissionMeta={unresolvedPerms.at(-1)?.meta as Record<string, unknown> | undefined}
          pendingPermCount={unresolvedPerms.length}
          onApprove={(() => {
            const aid = unresolvedPerms.at(-1)?.meta?.approval_id as string | undefined
            if (!aid) return approve
            return async (action: string) => { await api.resolveApproval(aid, toApiDecision(action)); dismissApproval(aid) }
          })()}
          onViewActivity={toggleAct}
          activityOpen={activityOpen}
        >{it.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(it.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
      })() : renderMessage(it.idx, it.msg)}
    </div>
  }, [stableMsgKey, renderMessage, approve, toApiDecision, dismissApproval, toggleAct, activityOpen])

  // ---- Measure-farm wiring ----
  // The farm's renderItem must reproduce the transcript row wrappers EXACTLY
  // (same classes, same maxWidth, default disclosure) so an off-screen
  // measurement equals the height the row will really mount at. Group rows
  // whose members are all permissions render null in the transcript; the farm
  // mirrors that so their measured height is the wrapper's own (near-zero).
  const renderFarmItem = useCallback((i: number): ReactNode => {
    const item = renderedDisplayItems[i]
    if (!item) return null
    if (item.kind === 'turn') {
      return <TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} appToolCallIds={appToolCallIds} disclosure={undefined} disclosureKey={`farm-${i}`} onDisclosureChange={() => {}} />
    }
    return (
      <div className={`px-4 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        {item.kind === 'group' ? (() => {
          if (item.msgs.every(m => m.role === 'permission')) return null
          return (
            <CollapsibleToolGroup
              count={item.msgs.filter(m => m.role !== 'permission').length}
              disclosureKey={`farm-ctg-${i}`}
              hasPermission={false}
              isRunning={false}
              permissionMeta={undefined}
              pendingPermCount={0}
              onApprove={approve}
              onViewActivity={toggleAct}
              activityOpen={false}
            >{item.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>
          )
        })() : renderMessage(item.idx, item.msg)}
      </div>
    )
  }, [renderedDisplayItems, renderTurnItem, chatConfig.collapseAllSteps, appToolCallIds, approve, toggleAct, stableMsgKey, renderMessage])
  return {
    searchCtxValue,
    renderUserContentCb,
    lastRole,
    streamTick,
    transcriptIdle,
    transcriptHot,
    groupTurns,
    groupedTurns,
    displayItems,
    renderedDisplayItems,
    spinnerNearTop,
    stableMsgKey,
    virtualKey,
    handleTopReached,
    handleLoadEarlier,
    virt,
    isAtBottom,
    visibleDisplayItems,
    messageToDisplayIdx,
    chatNav,
    chatPins,
    chatPinsLoading,
    chatPinsError,
    clearChatPinsError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
    pinNotice,
    pendingPinnedJump,
    setPendingPinnedJump,
    jumpToLoadedPinnedMessage,
    handleJumpToPinnedMessage,
    handleJumpToPin,
    handleTogglePinForMessage,
    handleUnpinById,
    pinStatus,
    dismissPinStatus,
    jumpToSearchResult,
    visibleIndexMap,
    activeSlotTitle,
    artifactPaths,
    renderMessage,
    renderTurnItem,
    renderFarmItem,
  }
}

export type ChatPageTranscriptController = ReturnType<typeof useChatPageTranscriptController>
