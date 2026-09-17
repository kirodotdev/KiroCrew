import { type RefObject, useCallback, useRef, useState } from 'react'

import type { DisplayItem } from './types'
import type { PasteBlock } from '../../utils/pasteTokens'
import {
  DEFAULT_PINNED_CARD_H,
  ROW_PAD_Y,
  computeLiveCardH,
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
import { glideDurationMs, runConvergingGlide } from '../../utils/convergingGlide'

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
 * Jumping back to the pinned prompt needs the host's virtualizer — the target
 * row may not be mounted — so the hook exposes `pinnedJumpChrome` (the landing
 * inset, solved from the live banner geometry) plus `jumpToPinnedPromptInPlace`,
 * which takes the host's `mountIndex` / `estimateRowTop` steer and drives one
 * converging glide from them.
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
  // Scroll the transcript on the card's behalf. The card is interactive so its text
  // stays selectable and its buttons keep working, but it sits in a
  // `pointer-events-none` overlay that is a SIBLING of this scroller, so a wheel
  // over it finds no scrollable ancestor and the transcript would not move at all.
  // The card reports the delta and this applies it, because the scroller is ours.
  const scrollTranscriptBy = useCallback((dy: number) => {
    const el = scrollerRef.current
    if (!el || !dy) return
    el.scrollTop += dy
  }, [scrollerRef])
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
    // A prompt hands over to the banner once its row TOP has risen above the
    // card's own resting top, so the bubble stops travelling at the pixel the
    // card occupies. Independent of the card's height — see pinHandoffY.
    const handoffY = pinHandoffY(foldY)
    // First row whose top has NOT yet reached that line = the topmost row still
    // below it. STRICT `>`, and that is load-bearing rather than a taste: the
    // outgoing card is dropped the moment the incoming row's top reaches the fold
    // (`push >= pinPushTravel`, below), so a row sitting exactly ON the line must
    // already be pinnable. With `>=` it was not, and the banner disappeared
    // entirely for the frames where the gap was zero — one hand-off replaced by a
    // blink. The two predicates are the same instant by construction.
    //
    // The row must also REACH the line: a far jump or fast upward fling can leave
    // unmounted spacer between the viewport and the first mounted row for one
    // commit, and treating that later row as the hand-off would select a prompt
    // below what the reader can see. With a top-edge rule that shows up as the
    // FIRST mounted row already sitting below the line — every contiguous case has
    // a mounted row above the boundary.
    let handoffIdx = -1
    let first = true
    for (const item of items) {
      const htmlItem = item as HTMLElement
      const rect = htmlItem.getBoundingClientRect()
      if (rect.top > handoffY) {
        if (requiresMountedHandoff && first) { setPinned(null); return }
        handoffIdx = parseInt(htmlItem.getAttribute('data-display-index') || '0', 10)
        break
      }
      first = false
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
    // The pinned row is `visibility: hidden` while the card stands in for it, so
    // it keeps its layout box and stays measurable — which is what makes the
    // progressive fold possible: the row's bottom edge is where the reply begins.
    //
    // The BUBBLE, not the row, for the ceiling. A user row is not just padding
    // around its bubble: UserMessage puts an action row (copy / copy-link / edit)
    // beneath it, so `rowH - ROW_PAD_Y * 2` overshoots the bubble by that strip's
    // height (32px measured) and the card would stand in for the bubble as a
    // taller box. `.user-bubble` is the class the bubble and the pinned card
    // already share (the theme hook both use), so it is the right handle for "the
    // box this card is a copy of". Falls back to the row's content box when a host
    // renders no bubble node.
    const pinEl = el.querySelector(`[data-display-index="${pinIdx}"]`) as HTMLElement | null
    const pinBubble = pinEl?.querySelector('.user-bubble') as HTMLElement | null
    const pinRect = pinEl?.getBoundingClientRect()
    const bubbleH = pinBubble
      ? pinBubble.getBoundingClientRect().height
      : (pinRect ? Math.max(0, pinRect.height - ROW_PAD_Y * 2) : null)
    // Height for THIS frame. Derived from the row and the settled resting height,
    // never from the live card, so the card's own size is not an input to the
    // geometry that sets it (see computeLiveCardH).
    //
    // Reported ONLY while it exceeds the resting height, i.e. while there is
    // actually a fold in progress. At rest it is left undefined so the card goes
    // back to being content-driven and its own expand / peek morph owns the height.
    // The threshold lives here because this is where the resting height lives;
    // duplicating it in the card would let the two disagree about "at rest".
    const restingH = pinCollapsedHRef.current
    const liveRaw = (pinRect && bubbleH != null)
      ? computeLiveCardH(pinRect.bottom - foldY, restingH, bubbleH)
      : undefined
    const liveH = liveRaw != null && liveRaw > restingH + 0.5 ? liveRaw : undefined
    // The SETTLED resting height, never the live card rect.
    //
    // The card grows past its resting size in two states — the hover peek
    // (PINNED_RESTING_LINES -> PINNED_PREVIEW_LINES) and the expansion — and
    // `peek` is itself gated on `pushUp <= 0`. Reading the live rect here
    // therefore closed a cycle: the peek grows the card, the taller card
    // lengthens `pinPushTravel`, the longer travel makes `push` positive for any
    // incoming prompt sitting between the resting and peeked travels, a positive
    // push closes the peek, the card shrinks back, `push` returns to 0, and the
    // peek reopens under the still-resting pointer. The card flipped between its
    // one-line and three-line heights for as long as the pointer stayed put.
    //
    // The resting height is what this geometry was always documented to use:
    // `pinHandoffY` above already derives from it, PinnedPrompt's own `peek`
    // comment says "`pinHandoffY` and `pinPushTravel` are derived from the
    // RESTING height", and PINNED_RESTING_LINES says a card growing after it
    // mounts "can never invalidate the pin that mounted it". This read was the
    // one place that was not true.
    //
    // No fallback is needed for an unmounted card: `pinCollapsedHRef` is seeded
    // with DEFAULT_PINNED_CARD_H and only ever written from PinnedPrompt's
    // `!expanded && !peek` report, so it is always known and always settled.
    const bannerH = pinCollapsedHRef.current
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
      liveH,
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
   * Jump back to the pinned prompt with one self-driven converging glide over
   * the host's virtualized transcript. `steer` is the host's virtualizer:
   * `mountIndex` preserves the current window for a far target so the glide
   * has rows to travel over, and `estimateRowTop` supplies the height-index
   * estimate until the row mounts. Every frame prefers live geometry, so row
   * measurement and the banner swap refine the landing; a frame with neither a
   * mounted row nor an estimate yields no goal (see runConvergingGlide). User
   * scroll intent aborts the glide, and owning each frame's write prevents
   * follow-controller writes from cancelling it.
   */
  const jumpCancelRef = useRef<(() => void) | null>(null)
  const jumpToPinnedPromptInPlace = useCallback((
    target: number,
    steer: {
      mountIndex: (index: number, opts?: { unionOnly?: boolean }) => boolean
      estimateRowTop: (index: number) => number | null
    },
  ) => {
    jumpCancelRef.current?.()
    const sc0 = scrollerRef.current
    if (!sc0) return
    // Land at the head of the target's consecutive prompt run (a steer sent
    // before any output, a double-send) so the row on the hand-off line is a
    // non-prompt and the previous turn's banner survives the landing — see
    // jumpAnchorIdx.
    const anchor = jumpAnchorIdx(displayItemsRef.current, target)
    steer.mountIndex(anchor, { unionOnly: true })
    const rowEl = (): HTMLElement | null =>
      scrollerRef.current?.querySelector(`[data-display-index="${anchor}"]`) as HTMLElement | null
    const goal = (): number | null => {
      const sc = scrollerRef.current
      if (!sc) return null
      const row = rowEl()
      const rowTop = row
        ? sc.scrollTop + (row.getBoundingClientRect().top - sc.getBoundingClientRect().top)
        : steer.estimateRowTop(anchor)
      if (rowTop == null) return null
      return Math.max(0, Math.min(sc.scrollHeight - sc.clientHeight, rowTop - pinnedJumpChrome()))
    }
    const first = goal()
    if (first == null) return
    const reduced = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const detach = attachUserScrollIntent(sc0, () => { jumpCancelRef.current?.() })
    const cancelGlide = runConvergingGlide({
      goal,
      read: () => scrollerRef.current?.scrollTop ?? 0,
      write: (top) => { const sc = scrollerRef.current; if (sc) sc.scrollTop = top },
      durationMs: glideDurationMs(first - sc0.scrollTop),
      reduced,
      onEnd: () => { detach(); jumpCancelRef.current = null },
    })
    jumpCancelRef.current = cancelGlide
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
    scrollTranscriptBy,
    updatePinnedPrompt,
    onScrollPin,
    pinnedJumpChrome,
    jumpToPinnedPromptInPlace,
  }
}
