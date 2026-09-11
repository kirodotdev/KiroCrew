import { type RefObject, useCallback, useRef, useState } from 'react'

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
import { attachUserScrollIntent } from '../../utils/searchScroll'

export interface UsePinnedPromptOptions {
  /** The transcript scroll container. Rows inside it carry `data-display-index`. */
  scrollerRef: RefObject<HTMLElement | null>
  /** A gap at the hand-off line is unmounted spacer, not transcript content. */
  requiresMountedHandoff?: boolean
}

/**
 * The pinned-prompt banner's DOM-driven geometry, shared by every transcript
 * host (the main chat page, split-view panes, the Crew Members DM thread).
 *
 * The hook owns WHAT is pinned and HOW FAR it has been pushed out; it does not
 * own how the transcript scrolls. It reads `[data-display-index]` rows inside
 * `scrollerRef` and the host-maintained `displayItemsRef` (the list those
 * indices point into), so any host that can (a) mark its rows and (b) keep the
 * ref current can wear the banner. `pinFoldRef` is a zero-height sentinel the
 * host mounts on the fold line the banner sticks to (directly under its header,
 * or the scroller's own top edge when there is no header); `pinCardRef` goes on
 * the rendered `PinnedPrompt` card so the push geometry can measure it.
 *
 * Jumping back to the pinned prompt is host-specific — a virtualized transcript
 * must mount the target first, an unvirtualized one can glide straight to the
 * row — so the hook exposes `pinnedJumpChrome` (the landing inset, solved from
 * the live banner geometry) plus `jumpToPinnedPromptInPlace`, the unvirtualized
 * glide, and lets a virtualized host supply its own jump.
 */
export function usePinnedPrompt({ scrollerRef, requiresMountedHandoff = false }: UsePinnedPromptOptions) {
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
    // fully scrolled behind the band. The row must also REACH the line. A far
    // jump or fast upward fling can leave unmounted spacer between the viewport
    // and the first mounted row for one commit; treating that later row as the
    // hand-off would select a prompt below what the reader can see.
    let handoffIdx = -1
    for (const item of items) {
      const htmlItem = item as HTMLElement
      const rect = htmlItem.getBoundingClientRect()
      if (rect.bottom > handoffY) {
        if (requiresMountedHandoff && rect.top > handoffY) { setPinned(null); return }
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
    // Only a user-authored prompt reaches here (isPrompt in utils/pinnedPrompt):
    // nudge and subagent rows are never pin candidates, so there is no machine
    // payload to substitute a label for.
    // Stored content is COLLAPSED (recollapsePastes), so a big paste is a
    // `[ Paste #N ]` token; the reducer unwraps it and decides whether to derive.
    setPinned(prev => nextPinnedPromptState(prev, {
      idx: pinIdx,
      ts: pinItem.msg.ts,
      raw: pinItem.msg.content,
      pastes: (pinItem.msg.meta?.pastes as PasteBlock[] | undefined) || [],
      push,
      bannerH,
    }))
  }, [requiresMountedHandoff, scrollerRef])
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

  /**
   * Jump back to the pinned prompt on an UNVIRTUALIZED transcript (every row
   * is mounted, so the target's rect is readable right away — split-view panes
   * and the Members DM thread). The same self-driven converging glide the main
   * chat uses for its near jump: each frame re-derives the destination from
   * LIVE geometry (row rect + the banner currently pinned), so the banner swap
   * mid-glide — the previous turn's card pinning as this one un-pins — moves
   * the landing instead of stranding it. A native smooth scroll would be
   * cancelled by the follow controller's re-pin writes; owning every frame's
   * write makes the glide uncancellable. User scroll intent aborts it.
   * A virtualized host (ChatPage) must not use this: its target row may be
   * unmounted spacer, which is why it keeps its own mount-then-scroll jump.
   */
  const jumpRafRef = useRef(0)
  const jumpCancelRef = useRef<(() => void) | null>(null)
  const jumpToPinnedPromptInPlace = useCallback((target: number) => {
    cancelAnimationFrame(jumpRafRef.current)
    jumpCancelRef.current?.()
    const sc0 = scrollerRef.current
    if (!sc0) return
    // Land at the head of the target's consecutive prompt run (a steer pair, a
    // subagent fan-out) so the row on the hand-off line is a non-prompt and the
    // previous turn's banner survives the landing — see jumpAnchorIdx.
    const anchor = jumpAnchorIdx(displayItemsRef.current, target)
    const rowEl = (): HTMLElement | null =>
      scrollerRef.current?.querySelector(`[data-display-index="${anchor}"]`) as HTMLElement | null
    if (!rowEl()) return
    let cancelled = false
    const detach = attachUserScrollIntent(sc0, () => { cancelled = true })
    jumpCancelRef.current = () => { cancelled = true; detach() }
    const GLIDE_MS = 450
    const t0 = performance.now()
    const from = sc0.scrollTop
    const reduced = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3)
    const glide = () => {
      if (cancelled) { detach(); return }
      const sc = scrollerRef.current
      const row = rowEl()
      if (!sc || !row) { detach(); jumpCancelRef.current = null; return }
      const liveTarget = sc.scrollTop
        + (row.getBoundingClientRect().top - sc.getBoundingClientRect().top)
        - pinnedJumpChrome()
      const goal = Math.max(0, Math.min(sc.scrollHeight - sc.clientHeight, liveTarget))
      const t = reduced ? 1 : Math.min(1, (performance.now() - t0) / GLIDE_MS)
      sc.scrollTop = from + (goal - from) * easeOutCubic(t)
      if (t >= 1) { detach(); jumpCancelRef.current = null; return }
      jumpRafRef.current = requestAnimationFrame(glide)
    }
    jumpRafRef.current = requestAnimationFrame(glide)
  }, [pinnedJumpChrome, scrollerRef])

  return {
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
    jumpToPinnedPromptInPlace,
  }
}
