import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'

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
    const pinEl = el.querySelector(`[data-display-index="${pinIdx}"]`) as HTMLElement | null
    // The pinned row is `visibility: hidden` while the card stands in for it, so
    // it keeps its layout box and stays measurable — which is what makes the
    // progressive fold possible.
    //
    // The BUBBLE, not the row, for both the ceiling and the fold's bottom edge. A
    // user row is not just padding around its bubble: UserMessage puts an action
    // row (copy / copy-link / pin / edit / timestamp) beneath it, so the row's
    // box overshoots the bubble by that strip's height (26px measured) plus its
    // 4px gap. The card stands in for the bubble alone — the strip is re-shown in
    // place under the hidden row (index.css `[data-pinned-standin]`), so a card
    // folding down to the ROW's bottom would cover exactly the controls that
    // hand-off leaves visible. `.message-bubble` is the theming contract's hook
    // for the bubble box (website/docs/theming-contract.md), carried by the
    // plain `.user-bubble` and by a steer's accent bubble alike, so a pinned
    // steer measures its bubble too instead of falling back to the row. Falls
    // back to the row's content box when a host renders no bubble node.
    const pinBubble = pinEl?.querySelector('.message-bubble') as HTMLElement | null
    // The row's own user root: UserMessage's `div[data-role="user"]`, the parent
    // of the bubble and of the action strip. Reached from the bubble by walking
    // UP, never by searching the row down, because the rendered message body is
    // inside the bubble and MarkdownRenderer admits `data-*` on it — a
    // `<div data-role="user">` minted by a prompt would be found first by a
    // `querySelector`, but can never be an ANCESTOR of the bubble it sits in.
    // The two markers below are read off this root and its direct children for
    // the same reason: a `[data-message-…]` search over the whole row subtree
    // put a forged marker inside the bubble ahead of the real one.
    const userRoot = pinBubble?.closest('[data-role="user"]') ?? null
    // A row being edited is never the stand-in. Editing swaps the bubble for a
    // textarea + Cancel/Send tree (UserMessage's `if (editing)` render), and the
    // stand-in state hides the whole row: an edit opened BEFORE the row reached
    // the fold would then carry on inside an invisible field — keystrokes landing
    // where nothing can be seen, and a Send below the viewport that scrolling can
    // never reveal. A card over an editor would also be a copy of text the reader
    // is changing. Editing state lives inside UserMessage, so the hook reads its
    // `data-message-editing` marker off the DOM it already measures. The editing
    // render has NO bubble, so there is no root to walk up from; the fallback
    // searches the row for a user root that carries the marker, which is safe
    // only because MarkdownRenderer reserves `data-message-*` (isAllowedAttr) —
    // a body can mint `data-role`, but never that pair.
    const editing = userRoot
      ? userRoot.hasAttribute('data-message-editing')
      : !!pinEl?.querySelector('[data-role="user"][data-message-editing]')
    if (editing) { setPinned(null); return }
    const pinRect = pinEl?.getBoundingClientRect()
    const bubbleRect = pinBubble?.getBoundingClientRect()
    // The fold's ceiling: the card's NATURAL height, i.e. from its own top to the
    // bubble's bottom at the hand-off frame. The card's top sits ROW_PAD_Y under
    // the row's top (both put that padding above their content), so this is the
    // bubble's height for a plain bubble — and more for a steer, whose accent
    // bubble starts under its badge (20px line + 4px gap): a ceiling of the
    // bubble's own height there stopped the card 24px short of the bubble's
    // bottom, a blank band above the re-shown strip that only closed once the
    // reader had scrolled that far. Measured from the row rather than assumed,
    // so whatever a host renders above its bubble is accounted for.
    const standinH = (bubbleRect && pinRect)
      ? bubbleRect.bottom - pinRect.top - ROW_PAD_Y
      : (pinRect ? Math.max(0, pinRect.height - ROW_PAD_Y * 2) : null)
    // The edge the fold tracks: the bubble's own bottom (the strip and the reply
    // begin below it), or the row's content-box bottom in the no-bubble fallback.
    const bubbleBottom = bubbleRect
      ? bubbleRect.bottom
      : (pinRect ? pinRect.bottom - ROW_PAD_Y : null)
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
    const liveRaw = (bubbleBottom != null && standinH != null)
      ? computeLiveCardH(bubbleBottom - foldY, restingH, standinH)
      : undefined
    const liveH = liveRaw != null && liveRaw > restingH + 0.5 ? liveRaw : undefined
    // Whether the row's re-shown action strip is still UNCOVERED — any of it
    // below the card's bottom, i.e. on screen. This is what the hosts write as
    // the `folding` value of the row's `data-pinned-standin` marker (the value
    // index.css re-shows the strip on), and it is derived from occlusion, NOT
    // from the fold: the fold ends when the card reaches its clamp, but the
    // strip hangs under the bubble's bottom, so at that frame the whole strip is
    // still on screen below the card and slides under it over the next
    // strip's-height of scroll (its `mt-1` gap plus its buttons). Keyed on
    // `liveH` the marker dropped at the clamp — copy / copy-link / pin vanished
    // under the pointer and that band went blank for those last pixels. The
    // strip's OWN rect, so whatever a host renders there is measured rather than
    // assumed. A row without a strip falls back to the fold itself. The strip is
    // a direct child of the user root (UserMessage renders it beside the
    // bubble), never something found inside the bubble — see `userRoot`.
    const strip = userRoot
      ? Array.from(userRoot.children).find(c => c.hasAttribute('data-message-actions')) ?? null
      : null
    const stripBottom = strip ? strip.getBoundingClientRect().bottom : null
    // Against the card's LIVE bottom — the one read of the live card this
    // geometry makes. The resting-height rule below exists because the card's
    // size must not feed the geometry that SETS the card's size; this flag only
    // decides the strip's visibility and feeds neither the fold nor the push
    // (both keep reading pinCollapsedHRef), so the peek/push oscillation
    // documented there cannot run through it. The live bottom is what keeps the
    // flag honest at rest: the hover peek and the expansion grow the card down
    // over the strip's last pixels — growth the row cannot show, and an opaque
    // card over a strip left `visible` is a Tab stop nobody can see and a click
    // meant for Copy swallowed by the card — while a card with nothing to reveal
    // does not grow, so a host's `expanded` flag outliving the prompt it was set
    // on (hosts reset it per session, not per prompt) cannot hide a shorter
    // prompt's still-uncovered strip. While folding, the bottom is the height
    // THIS frame sets, which sits on the bubble's bottom by construction: the
    // card's rect still carries the previous frame's fold, and a downward scroll
    // faster than the strip's height per frame would read the strip as under a
    // card that is about to shrink past it. The growth at rest arrives with no
    // scroll to run this recompute, so the card is observed for size below.
    const cardBottom = liveH != null
      ? foldY + ROW_PAD_Y + liveH
      : (pinCardRef.current?.getBoundingClientRect().bottom ?? (foldY + ROW_PAD_Y + restingH))
    const stripUncovered = stripBottom != null
      ? stripBottom > cardBottom + 0.5
      : liveH != null
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
      stripUncovered,
    }))
  }, [requiresMountedHandoff, scrollerRef])
  // The card grows at rest with no scroll to run the recompute — the hover peek
  // and the expansion — and the strip test above reads the card's live bottom,
  // so it has to re-run when that bottom moves. Observe the card's size for as
  // long as one is pinned: the observer fires once per committed size change
  // (each frame of the 150ms morph, then quiet), and a recompute that changes
  // nothing returns the same state, so it never re-enters itself. Keyed on the
  // pinned message because the card mounts and unmounts with it, and the ref is
  // attached before this effect runs. This replaces a callback the card would
  // otherwise have to report through every host; an environment without
  // ResizeObserver keeps the scroll-driven recompute alone.
  useEffect(() => {
    const card = pinCardRef.current
    if (!card || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => { updatePinnedPrompt() })
    observer.observe(card)
    return () => observer.disconnect()
  }, [pinned?.ts, updatePinnedPrompt])
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
