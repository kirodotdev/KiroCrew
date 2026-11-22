import { type MutableRefObject, useCallback, useEffect, useRef, useState } from 'react'

import type { useScrollManager } from './useScrollManager'
import { usePinnedPrompt } from './usePinnedPrompt'
import { jumpAnchorIdx } from '../../utils/pinnedPrompt'
import { attachUserScrollIntent } from '../../utils/searchScroll'
import { useNavToDisplayIndex } from './useNavToDisplayIndex'
import { glideDurationMs, runConvergingGlide } from '../../utils/convergingGlide'
import { releaseReadingPosition } from '../../utils/panelSlide'

export interface UseChatPageTranscriptEarlyControllerOptions {
  activeTip: unknown
  mountIndexRef: MutableRefObject<(index: number, opts?: { unionOnly?: boolean }) => boolean>
  /** The virtualizer's height-index estimate of a row's top, for steering a
   *  jump toward a row that is not mounted yet. */
  estimateRowTopRef: MutableRefObject<(index: number) => number | null>
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
  estimateRowTopRef,
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
  //
  // The same observer marks the band while it OVERFLOWS its `max-h` cap
  // (`data-overflowing`, read by the `.dock-inert` rule in index.css). The band
  // is a `dock-inert` box: it passes input through so the strip beside its
  // column reaches the transcript, which also drops its own scrollbar as a hit
  // target — and a scrollbar belongs to the box, not to a child. The band
  // crosses the cap only by changing height, so every crossing is a resize
  // this observer already sees; while it sits at the cap, the mark holds.
  const composerBandObserverRef = useRef<ResizeObserver | null>(null)
  const composerBandRef = useCallback((el: HTMLDivElement | null) => {
    composerBandObserverRef.current?.disconnect()
    composerBandObserverRef.current = null
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      el.dataset.overflowing = el.scrollHeight > el.clientHeight ? 'true' : 'false'
      if (autoFollowAllowed()) scrollBottom(true)
    })
    ro.observe(el)
    composerBandObserverRef.current = ro
  }, [scrollBottom, autoFollowAllowed])

  // The jump to a (possibly off-window) display index lives in the shared
  // useNavToDisplayIndex, which a Bot chat's Find in chat also uses. The
  // pinned-prompt glide below shares its in-flight handles, so either jump
  // cancels the other.
  const mountIndex = useCallback(
    (index: number, opts?: { unionOnly?: boolean }) => mountIndexRef.current(index, opts),
    [mountIndexRef],
  )
  const { navToDisplayIndex, cancelNavScroll, navScrollRafRef, navPollCancelRef } = useNavToDisplayIndex({
    scrollerRef,
    mountIndex,
    scrollToDisplayIndex,
  })

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
    scrollTranscriptBy,
    updatePinnedPrompt,
    onScrollPin,
    pinnedJumpChrome,
  } = usePinnedPrompt({ scrollerRef, requiresMountedHandoff: true })
  /** Jump the transcript back to the pinned prompt, landing it just below the
   *  banner so the prompt is read in context — which also un-pins the banner,
   *  since its prompt is no longer above the fold.
   *
   *  One converging glide for every distance (utils/convergingGlide): smooth
   *  travel to a destination re-derived every frame, then convergence until
   *  the destination holds still. What differs by distance is only how the
   *  destination is read before the target row is mounted. */
  const scrollToPinnedPrompt = useCallback((target: number) => {
    cancelAnimationFrame(navScrollRafRef.current)
    navPollCancelRef.current?.()
    const sc0 = scrollerRef.current
    if (!sc0) return
    // The jump lands at the head of the target's consecutive prompt run — a
    // steer sent before any output, a double-send — so the row on the hand-off
    // line is a non-prompt and the previous turn's banner survives the landing.
    // Rationale and near/far interaction: see jumpAnchorIdx's docblock
    // (utils/pinnedPrompt.ts).
    const anchor = jumpAnchorIdx(displayItemsRef.current, target)
    // NEAR: the window is unioned up to the target, so every row on the path
    // mounts and measures before the glide starts and the destination is exact
    // from the first frame. FAR: nothing is mounted — replacing the window at
    // the target would blank the rows under the reader for a frame — and the
    // glide steers by the height index's estimate instead, the window following
    // each write as under a fling. The estimate refines as rows measure in, and
    // the goal is re-read every frame, so the glide converges on the true
    // position rather than the estimate. A teleport was the previous far path;
    // it read as the banner "jumping to the top", and a far landing that then
    // shifted was never corrected.
    const far = mountIndexRef.current(anchor, { unionOnly: true })
    window.dispatchEvent(new Event('mc-chat-scroll-jump'))
    const rowEl = (): HTMLElement | null =>
      (scrollerRef.current?.querySelector(`[data-display-index="${anchor}"]`) as HTMLElement | null)
    // Live destination: the row's top from the DOM once it is mounted, the
    // height index's estimate until then, less the chrome the previous turn's
    // banner needs to pin completely at the landing.
    const goal = (): number | null => {
      const sc = scrollerRef.current
      if (!sc) return null
      const row = rowEl()
      const rowTop = row
        ? sc.scrollTop + (row.getBoundingClientRect().top - sc.getBoundingClientRect().top)
        : estimateRowTopRef.current(anchor)
      if (rowTop == null) return null
      return Math.max(0, Math.min(sc.scrollHeight - sc.clientHeight, rowTop - pinnedJumpChrome()))
    }
    const reduced = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let cancelled = false
    // This jump owns the position from here (utils/panelSlide).
    releaseReadingPosition(sc0)
    let detach = attachUserScrollIntent(sc0, () => { cancelled = true; navPollCancelRef.current?.() })
    const startGlide = () => {
      const first = goal()
      if (first == null) { detach(); navPollCancelRef.current = null; return }
      const cancelGlide = runConvergingGlide({
        goal,
        read: () => scrollerRef.current?.scrollTop ?? 0,
        write: (top) => { const sc = scrollerRef.current; if (sc) sc.scrollTop = top },
        durationMs: glideDurationMs(first - sc0.scrollTop),
        reduced,
        raf: (cb) => (navScrollRafRef.current = requestAnimationFrame(cb)),
        cancelRaf: (id) => cancelAnimationFrame(id),
        onEnd: () => { detach(); navPollCancelRef.current = null },
      })
      navPollCancelRef.current = () => { cancelled = true; cancelGlide() }
    }
    if (far) { startGlide(); return }
    // NEAR: wait the few frames the unioned rows take to commit and measure
    // (reading, not scrolling) so the travel's endpoint is exact from its first
    // frame. 2 stable frames is enough: rows measure synchronously on mount via
    // measureRef; the wait only covers React committing the window. The frame
    // cap (~0.5s) guarantees the glide still happens if some row never stops
    // moving (an animated widget) — convergence absorbs whatever is left.
    let lastH: number | null = null
    let stable = 0
    let frames = 0
    navPollCancelRef.current = () => { cancelled = true; detach() }
    const tick = () => {
      if (cancelled) { detach(); return }
      const el = rowEl()
      const h = el ? el.getBoundingClientRect().height : null
      if (h != null && lastH != null && Math.abs(h - lastH) < 1) stable += 1
      else stable = 0
      lastH = h
      frames += 1
      if ((h != null && stable >= 2) || frames >= 30) {
        // Hand the abort listener to the glide: the wait's is detached here and
        // a fresh one is attached so the glide's own cancel owns it.
        detach()
        detach = attachUserScrollIntent(scrollerRef.current ?? undefined, () => {
          cancelled = true
          navPollCancelRef.current?.()
        })
        startGlide()
        return
      }
      navScrollRafRef.current = requestAnimationFrame(tick)
    }
    navScrollRafRef.current = requestAnimationFrame(tick)
  }, [pinnedJumpChrome, scrollerRef, mountIndexRef, estimateRowTopRef, displayItemsRef, navScrollRafRef, navPollCancelRef])

  return {
    scrollBottom,
    autoFollowAllowed,
    handleSurveyLayoutChange,
    composerBandRef,
    navToDisplayIndex,
    cancelNavScroll,
    displayItemsRef,
    pinFoldRef,
    pinCardRef,
    pinEnabledRef,
    pinned,
    setPinned,
    pinExpanded,
    setPinExpanded,
    onPinCollapsedHeight,
    scrollTranscriptBy,
    updatePinnedPrompt,
    onScrollPin,
    pinnedJumpChrome,
    scrollToPinnedPrompt,
  }
}
