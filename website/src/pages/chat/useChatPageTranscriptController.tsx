import { type MutableRefObject, useCallback, useEffect, useRef, useState } from 'react'

import type { useScrollManager } from './useScrollManager'
import { usePinnedPrompt } from './usePinnedPrompt'
import { jumpAnchorIdx } from '../../utils/pinnedPrompt'
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

  // Pinned-prompt banner: geometry, expanded state and the fold/card refs live
  // in the shared usePinnedPrompt hook (also worn by ChatPane). Only the JUMP is
  // page-specific — a virtualized transcript must mount the target row before
  // it can scroll to it — so that stays here, on top of the hook's chrome math.
  const {
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
    pinnedJumpChrome,
  } = usePinnedPrompt({ scrollerRef, requiresMountedHandoff: true })
  /** Jump the transcript back to the pinned prompt, landing it just below the
   *  banner so the prompt is read in context — which also un-pins the banner,
   *  since its prompt is no longer above the fold. */
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
  }, [navToDisplayIndex, pinnedJumpChrome, scrollerRef, mountIndexRef, displayItemsRef])

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
