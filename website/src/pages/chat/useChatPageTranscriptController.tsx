import { type MutableRefObject, useCallback, useEffect, useRef, useState } from 'react'

import type { useScrollManager } from './useScrollManager'
import { parseNudgeMessage, nudgeLabel } from './NudgeCard'
import { parseSubagentCompletionMessage } from './subagentCompletion'
import { headline as subagentHeadline } from './SubagentCompletionCard'
import type { DisplayItem } from './types'
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
import { attachUserScrollIntent, glideOnceStep, pollRowSettled } from '../../utils/searchScroll'

export interface UseChatPageTranscriptEarlyControllerOptions {
  activeTip: unknown
  mountIndexRef: MutableRefObject<(index: number) => boolean>
  scrollerRef: ReturnType<typeof useScrollManager>['scrollerRef']
  scrollToDisplayIndex: ReturnType<typeof useScrollManager>['scrollToDisplayIndex']
  slotRunningRef: MutableRefObject<boolean>
  vGetFollowRef: MutableRefObject<() => boolean>
  vScrollToBottomRef: MutableRefObject<(behavior?: ScrollBehavior) => void>
}

/** The transcript state that must exist before the virtualizer is created:
 *  scroll-to-bottom, auto-follow gating, the composer-band observer, nav
 *  scrolling and the pinned-prompt banner. The page passes the refs the
 *  virtualizer later fills; the hook creates the rest. */
export function useChatPageTranscriptEarlyController({
  activeTip,
  mountIndexRef,
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
  const [pinned, setPinned] = useState<PinnedPromptState | null>(null)
  const [pinExpanded, setPinExpanded] = useState(false)
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
    const measured = pinCardRef.current?.getBoundingClientRect().height ?? 0
    const bannerH = measured > 0 ? measured : pinCollapsedHRef.current
    const push = computePinPush(bannerH, foldY, nextTop)
    // Fully pushed out: DROP the banner instead of rendering it clipped to
    // nothing. A tall incoming prompt holds this state for its whole length (it
    // takes the pin only once its own bottom clears the band), and a card clipped
    // to zero still shows a hairline of its bottom edge under sub-pixel rounding
    // and browser zoom — a bubble fragment parked over the prompt being read.
    if (push >= pinPushTravel(bannerH)) { setPinned(null); return }
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
    scrollBottom,
    autoFollowAllowed,
    handleSurveyLayoutChange,
    composerBandRef,
    navToDisplayIndex,
    displayItemsRef,
    pinFoldRef,
    pinCardRef,
    pinEnabledRef,
    pinned,
    setPinned,
    pinExpanded,
    setPinExpanded,
    onPinCollapsedHeight,
    updatePinnedPrompt,
    onScrollPin,
    scrollToPinnedPrompt,
  }
}
