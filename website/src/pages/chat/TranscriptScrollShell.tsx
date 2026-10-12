/**
 * TranscriptScrollShell — the transcript scroller SKELETON for a virtualized
 * chat surface: the scroll container's full style contract, the virtualizer's
 * sentinel/spacer wiring, and the older-messages loading affordance.
 *
 * Extracted from the main chat page as one unit because these pieces only
 * work as a set: the sentinels drive the window, the spacers reserve the
 * unmounted height the sentinels are measured against, and the scroller's
 * style contract is what keeps all of it scrollable, anchored, and contained.
 * Page-owned content threads through as slots — `aboveRows` (paging bar),
 * `children` (the mounted rows), `belowRows` (footer / survey / tail spacer) —
 * so page state never leaks in here and the shell stays reusable by any host
 * that runs useVirtualChat.
 *
 * The characterization net (ChatPage.scrollShell.recipe.test.tsx, the golden
 * frames, and the mutation harness) pins this file's tokens byte-for-byte;
 * dockClearance geometry stays with the page, which supplies the floating
 * dock's clearance padding via `scrollerStyle`.
 */
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Loader } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { afterSlide, forgetAfterSlide, holdReadingPosition, markSlideSettling, onReleaseReadingPosition, slideHeld, watchPanelSlide } from '../../utils/panelSlide'
import { attachUserScrollIntent } from '../../utils/searchScroll'

/** Inherited custom property carrying the scroller's usable width in px, so a
 *  descendant that may outgrow the reading column (a top-level assistant table,
 *  see `.markdown-table` in index.css) can size against the pane it is in.
 *
 *  Invariant: publish the width WITHOUT making the scroller a containing block
 *  that can trap `position: fixed` descendants — so no query container or
 *  `contain` on it. McpAppFrame promotes its full-screen sheet IN PLACE (it
 *  must not portal: reparenting the iframe reloads the app), and a scroller
 *  that contained it would centre the sheet on itself and clip its backdrop. */
export const PANE_WIDTH_PROPERTY = '--chat-pane-width'

/** Publish the scroller's `clientWidth` as PANE_WIDTH_PROPERTY on itself.
 *  `clientWidth` nets out the reserved scrollbar gutter, so it is the width a
 *  child can actually paint into. The measure farm renders inside this same
 *  element, so off-screen rows inherit the identical width. Undebounced: the
 *  tables must reflow with the pane, like the prose column does; the hosts'
 *  own 200ms-settled width buckets scope the height cache, not layout. */
function usePaneWidthProperty(scrollerRef: React.RefObject<HTMLDivElement | null>): void {
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const publish = () => { el.style.setProperty(PANE_WIDTH_PROPERTY, `${el.clientWidth}px`) }
    publish()
    if (typeof ResizeObserver === 'undefined') return
    // While a side panel slides (panelSlide), only the final width is
    // published: each change restyles the whole transcript.
    const observer = new ResizeObserver(() => { if (slideHeld()) afterSlide(publish); else publish() })
    observer.observe(el)
    return () => {
      observer.disconnect()
      forgetAfterSlide(publish)
    }
  }, [scrollerRef])
}

/** The blocks a reading position is anchored to: leaves only (a block holding
 *  another is skipped), so the anchor is the line the reader sees, not the top
 *  of a long reply that reflows under it. */
const anchorBlockSelector = ['p', 'li', 'tr', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'img'].join(', ')
/** Where in the viewport the anchored line is, as a share of its height. */
const ANCHOR_LINE = 0.3
/** Within this of the bottom, the transcript follows the bottom instead. */
const ANCHOR_AT_BOTTOM_PX = 4
/** How long after a slide ends the anchor still holds: the pane width, the
 *  tables and the virtualizer's settled heights land in this window. */
const ANCHOR_AFTER_SLIDE_MS = 600

function pickSlideAnchor(el: HTMLElement): { node: Element; want: number } | null {
  const top = el.getBoundingClientRect().top
  const line = top + el.clientHeight * ANCHOR_LINE
  for (const b of el.querySelectorAll(anchorBlockSelector)) {
    if (b.querySelector(anchorBlockSelector)) continue
    const r = b.getBoundingClientRect()
    if (r.height === 0 || r.bottom <= line) continue
    return { node: b, want: r.top - top }
  }
  return null
}

/**
 * Keep the line the reader is looking at in place while a side panel slides
 * (utils/panelSlide). The column changes width on every frame, so every block
 * above that line rewraps (tables most of all) and, with the scroll offset
 * left alone, the text under the reader's eyes moved up and down with the
 * drag. Not at the bottom only: there the transcript follows the bottom.
 *
 * The correction runs in a ResizeObserver on the scroller, after the frame's
 * layout and before its paint, so the reflowed frame is drawn already
 * corrected; a scroll offset change needs no new layout. After the slide it
 * holds a little longer, frame by frame, while the held width work lands. The
 * reader's own scroll input ends it at once (`attachUserScrollIntent`: a
 * wheel the page's swipe did not cancel, a scrollbar press, a scrolling key, a
 * touch).
 */
function useSlideAnchor(
  scrollerRef: React.RefObject<HTMLDivElement | null>,
  liveEdgeAtTop = false,
  holdScrollTop?: (el: HTMLDivElement, top: number) => void,
): void {
  // Read at slide start, so a layout toggle does not re-arm the observers.
  const liveEdgeAtTopRef = useRef(liveEdgeAtTop)
  liveEdgeAtTopRef.current = liveEdgeAtTop
  // Read at write time, for the same reason.
  const holdRef = useRef(holdScrollTop)
  holdRef.current = holdScrollTop
  useEffect(() => {
    const el = scrollerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let anchor: { node: Element; want: number } | null = null
    // At the top in the top-down layout, where the newest turn is: held at 0.
    // A block anchor would raise scrollTop as the rows rewrap, and the
    // top-edge follower reads that as the reader leaving the live edge.
    let top = false
    // At the bottom: the transcript stays pinned there. The follow logic pins
    // too, but on its own schedule; when the slide lands, the held width
    // rewraps the rows in that same commit, and a pin a frame later let one
    // frame paint the transcript a message's height off (Ray's 120fps
    // recording: a single-frame flash at the top, the bottom pinned).
    let bottom = false
    let raf = 0
    let until = 0
    const correct = () => {
      // In top-down the virtualizer's top-edge follow reads every scroll it
      // did not write as the reader's. A raw write that lands a reader in the
      // re-engage band arms follow for them, and a raw write away from 0
      // releases a follower. There, these writes go through the virtualizer's
      // write chokepoint (`holdScrollTop`). The bottom layout keeps its raw
      // writes.
      const hold = liveEdgeAtTopRef.current ? holdRef.current : undefined
      if (top) {
        if (el.scrollTop >= 0.5) {
          if (hold) hold(el, 0)
          else el.scrollTop = 0
        }
        return
      }
      if (bottom) {
        const top = el.scrollHeight - el.clientHeight
        if (Math.abs(el.scrollTop - top) >= 0.5) el.scrollTop = top
        return
      }
      if (!anchor || !anchor.node.isConnected) return
      const d = anchor.node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.want
      if (Math.abs(d) >= 0.5) {
        if (hold) hold(el, el.scrollTop + d)
        else el.scrollTop += d
      }
    }
    const ro = new ResizeObserver(correct)
    // The browser's own scroll anchoring picks its own node and would pull
    // against this one every frame: off while this anchor holds
    // (`data-slide-anchor`, chat-transcript.css; the follow logic writes the
    // inline value, so a stylesheet rule wins over it).
    const drop = () => {
      anchor = null
      bottom = false
      top = false
      el.removeAttribute('data-slide-anchor')
      holdReadingPosition(el, false)
      onReleaseReadingPosition(el, null)
      markSlideSettling(el, false)
      ro.disconnect()
      if (raf) cancelAnimationFrame(raf)
      raf = 0
    }
    // The reader's own input (a wheel the page's swipe did not claim, a
    // scrollbar press, a scrolling key, a touch) ends the hold at once: the
    // next correction would undo their scroll.
    // A downward input (a trackpad's momentum tail) agrees with the bottom pin, so it keeps it.
    const detachIntent = attachUserScrollIntent(el, (dir) => {
      if (bottom && dir === 'down') return
      if (anchor || bottom || top) drop()
    })
    const unwatch = watchPanelSlide((sliding, retired) => {
      if (sliding) {
        drop()
        if (liveEdgeAtTopRef.current && el.scrollTop <= ANCHOR_AT_BOTTOM_PX) {
          top = true
          onReleaseReadingPosition(el, drop)
        } else if (!liveEdgeAtTopRef.current && el.scrollHeight - el.scrollTop - el.clientHeight <= ANCHOR_AT_BOTTOM_PX) {
          // The bottom layout only. In top-down the bottom is the end of the
          // loaded history and nothing follows it: an older page landing in
          // the hold would move a pin there past the turn being read, so that
          // reader takes the block anchor below. scrollTop 0 is top-down's only
          // edge pin.
          bottom = true
          onReleaseReadingPosition(el, drop)
        } else {
          anchor = pickSlideAnchor(el)
          if (!anchor) return
          el.setAttribute('data-slide-anchor', '')
          holdReadingPosition(el, true, drop)
        }
        markSlideSettling(el, true)
        // The scroller resizes on every frame of the slide; its children (the
        // rows, the virtualizer's spacers) also change height when it rests
        // and the held width lands, with the scroller's own size unchanged.
        // A ResizeObserver runs after layout and before paint, so the
        // corrected position is the one painted.
        ro.observe(el)
        for (const child of el.children) ro.observe(child)
        return
      }
      // Dropped rather than landed (a session switch): the scroller may show
      // another session now, whose own restore owns the position.
      if (retired) { drop(); return }
      if (!anchor && !bottom && !top) return
      until = performance.now() + ANCHOR_AFTER_SLIDE_MS
      const tick = () => {
        correct()
        if (performance.now() < until) raf = requestAnimationFrame(tick)
        else drop()
      }
      correct()
      raf = requestAnimationFrame(tick)
    })
    return () => {
      unwatch()
      drop()
      detachIntent()
    }
  }, [scrollerRef])
}

const WIDTH_BUCKET_STEP = 16
const WIDTH_SETTLE_MS = 200
const bucketWidth = (px: number) => Math.round(px / WIDTH_BUCKET_STEP) * WIDTH_BUCKET_STEP

/** One width scope for both transcript hosts. Layout stays live during a drag,
 * but a height may enter the settled cache only when BOTH the pane and its
 * published table width belong to that scope. Reading both at write time makes
 * this independent of which ResizeObserver callback the browser delivers first.
 *
 * Element-bound, not ref-bound: a host can mount before its shell (the main
 * page's welcome hero) and replace the shell on the same stable ref, so the
 * shell binds its live element here on mount and unbinds on unmount. */
export function useTranscriptWidth() {
  const [scroller, bindScroller] = useState<HTMLDivElement | null>(null)
  const [widthBucket, setWidthBucket] = useState(() =>
    bucketWidth(typeof window !== 'undefined' ? window.innerWidth : 944))
  useLayoutEffect(() => {
    if (!scroller) return
    const measure = () => setWidthBucket(bucketWidth(scroller.clientWidth))
    measure()
    if (typeof ResizeObserver === 'undefined') return
    let timer: ReturnType<typeof setTimeout> | undefined
    const observer = new ResizeObserver(() => {
      clearTimeout(timer)
      timer = setTimeout(measure, WIDTH_SETTLE_MS)
    })
    observer.observe(scroller)
    return () => {
      observer.disconnect()
      clearTimeout(timer)
    }
  }, [scroller])
  // No bound element means no settled scope to measure against.
  const canMeasure = useCallback(() => {
    if (!scroller || scroller.clientWidth <= 0) return false
    const published = Number.parseFloat(scroller.style.getPropertyValue(PANE_WIDTH_PROPERTY))
    return bucketWidth(scroller.clientWidth) === widthBucket && bucketWidth(published) === widthBucket
  }, [scroller, widthBucket])
  return { widthBucket, canMeasure, bindScroller }
}

export interface TranscriptVirtWiring {
  topSentinelRef: React.MutableRefObject<HTMLDivElement | null>
  bottomSentinelRef: React.MutableRefObject<HTMLDivElement | null>
  /** The virtualizer's ref for the wrapper around `belowRows`: its
   *  ResizeObserver watches that wrapper so chrome mounting or growing below
   *  the rows (the working footer under a reply gone quiet) is followed like
   *  tail growth. Optional so a wiring that predates it (tests, a host with no
   *  trailing chrome) still type-checks; the wrapper renders either way. */
  trailingRef?: React.MutableRefObject<HTMLDivElement | null>
  /** The virtualizer's ref for the wrapper around the chrome that renders
   *  BEFORE the rows in the top-down layout (the footer, preparing caption and
   *  survey there). Its ResizeObserver watches the wrapper so a change in that
   *  chrome above a released reader reaches the measured residual. The wrapper
   *  renders only with `liveEdgeAtTop`; the bottom layout's DOM has none. */
  leadingRef?: React.MutableRefObject<HTMLDivElement | null>
  /** The virtualizer's write for a hold that keeps the reader's content still
   *  (useVirtualChat's `holdScrollTop`). The top-down layout's dock padding
   *  shift and slide hold write through it, so the follow guard reads their
   *  scroll events as its own. Optional for a wiring that predates it; the
   *  shell then writes scrollTop itself. */
  holdScrollTop?: (el: HTMLDivElement, top: number) => void
  offsetBefore: number
  offsetAfter: number
}

export default function TranscriptScrollShell({
  scrollerRef,
  onScroll,
  virt,
  loadingOlder,
  spinnerNearTop,
  historyAtBottom = false,
  liveEdgeAtTop = historyAtBottom,
  scrollerStyle,
  headerSpacer = true,
  aboveRows: aboveSlot,
  belowRows: belowSlot,
  onScrollerElement,
  children,
}: {
  /** The single scroll controller's element ref (owned by the host's virtualizer). */
  scrollerRef: React.MutableRefObject<HTMLDivElement | null>
  onScroll: () => void
  virt: TranscriptVirtWiring
  loadingOlder: boolean
  /** Whether the reader is still near the top, where the header-pinned loading
   *  overlay belongs. Omitted (undefined) means "always show it" — the overlay
   *  has zero layout footprint, so a host that does not track this cannot be
   *  hurt by it. */
  spinnerNearTop?: boolean
  /** Older history loads at the BOTTOM (the top-down chat layout), so the
   *  loading overlay sits at the bottom edge instead of under the header, and
   *  `spinnerNearTop` means "near that edge". */
  historyAtBottom?: boolean
  /** The live edge is scrollTop 0 (the top-down layout), so a side-panel slide
   *  holds a reader there. Defaults to `historyAtBottom`; a host that places
   *  its own paging bar sets this alone. */
  liveEdgeAtTop?: boolean
  /** Host-owned geometry merged onto the scroller (e.g. the fade-band clearance padding). */
  scrollerStyle?: React.CSSProperties
  /** Reserve the main chat's overlay-header band (h-16) above the rows. On by
   *  default — the page contract. A host whose scroller sits directly under
   *  its own title bar (a split pane, the side panel, an embed) turns it off;
   *  its padding travels through `scrollerStyle` instead. */
  headerSpacer?: boolean
  /** Page content above the rows (the earlier-messages paging bar). */
  aboveRows?: React.ReactNode
  /** Page content below the rows (footer, survey, tail spacer). */
  belowRows?: React.ReactNode
  /** Receives this shell's scroller element on mount and `null` on unmount
   *  (`useTranscriptWidth`'s `bindScroller`), after the pane width is published. */
  onScrollerElement?: (el: HTMLDivElement | null) => void
  children: React.ReactNode
}) {
  const hold = virt.holdScrollTop
  usePaneWidthProperty(scrollerRef)
  useSlideAnchor(scrollerRef, liveEdgeAtTop, hold)
  // History at the bottom (the top-down layout): the two slots trade ends, so
  // the host's live chrome (footer, survey) sits above the newest row and its
  // paging bar below the oldest. The props keep their host-facing meaning.
  const aboveRows = historyAtBottom ? belowSlot : aboveSlot
  const belowRows = historyAtBottom ? aboveSlot : belowSlot
  // A top-edge dock changes the leading padding under a reader who is off
  // scrollTop 0. Write the padding and the same scrollTop shift before paint.
  // The shift goes through the virtualizer's `holdScrollTop`: the top-edge
  // follower reads a scroll it did not write as the reader's, so a raw shift
  // into the re-engage band armed follow for a released reader, and a raw
  // shift away from 0 released a follower. No reading-position hold: the
  // virtualizer's only held path (compensateAboveFold) acts on row reprices,
  // and a padding change on the scroller resizes no row. The slide anchor
  // stays the only owner of that hold.
  const { paddingTop, ...scrollerStyleWithoutTopPadding } = scrollerStyle ?? {}
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    if (!liveEdgeAtTop) {
      // The effect owns top padding in top-down mode. Clear its value when
      // the bottom-layout host does not supply a replacement.
      if (paddingTop === undefined && el.style.paddingTop) el.style.paddingTop = ''
      return
    }
    const oldScrollTop = el.scrollTop
    const oldPadding = Number.parseFloat(el.style.paddingTop) || 0
    el.style.paddingTop = typeof paddingTop === 'number' ? `${paddingTop}px` : paddingTop ?? ''
    if (oldScrollTop <= 0.5) return
    const newPadding = Number.parseFloat(el.style.paddingTop) || 0
    // An unchanged padding moves nothing, and a hold write would still record
    // the position as the virtualizer's own.
    if (newPadding === oldPadding) return
    const next = Math.max(0, oldScrollTop + newPadding - oldPadding)
    if (hold) hold(el, next)
    else el.scrollTop = next
  }, [liveEdgeAtTop, paddingTop, scrollerRef, hold])
  useLayoutEffect(() => {
    if (!onScrollerElement) return
    onScrollerElement(scrollerRef.current)
    return () => onScrollerElement(null)
  }, [scrollerRef, onScrollerElement])
  return (
    <div
      ref={scrollerRef}
      // -1 so the bar can hand focus here on unmount without adding a tab stop.
      tabIndex={-1}
      // stable theming hook 'chat-container' — see website/docs/theming-contract.md
      className="chat-container"
      style={{
        // Host geometry merges FIRST so the shell's own tokens below always
        // win: the scroll contract (overflow axes, anchoring, containment,
        // gutter, and the flex-fill that makes the scroller consume its column
        // — every current host mounts it in a flex column) is this component's
        // whole reason to exist, and a consumer
        // overriding e.g. overflowX would silently reinstate the horizontal
        // scrollbar bug documented below. Hosts may only ADD properties the
        // shell does not claim (like the fade-band clearance padding).
        ...(liveEdgeAtTop ? scrollerStyleWithoutTopPadding : scrollerStyle),
        flex: 1,
        overflowY: 'auto',
        // overflow-x must be pinned, not left to default `visible`: with
        // overflowY `auto`, CSS forces the `visible` axis to compute to
        // `auto`, so one over-wide child (a long path, a wide code block,
        // a widget) gives the whole list a draggable horizontal scrollbar
        // above the composer. The conversation never pans sideways —
        // wide children scroll within themselves.
        overflowX: 'hidden',
        // Reserve a stable scrollbar gutter so the 6px scrollbar always
        // occupies the same right-edge column the title overlay is inset
        // from — keeps the thumb visible and grabbable at the top instead
        // of hidden behind the header.
        scrollbarGutter: 'stable',
        // Native scroll anchoring: when items above the viewport
        // resize (e.g. widget iframes loading async), the browser
        // adjusts scrollTop to keep the user's content stable.
        // This is more precise than item-level anchoring because
        // it works at the DOM-element granularity.
        overflowAnchor: 'auto',
        // Keep wheel/touch momentum inside the message list. Without
        // this, a delta that arrives at the top or bottom edge chains
        // to the nearest scrollable ancestor — the document, which
        // `body{overflow-y:auto}` leaves scrollable — and drags the
        // whole app shell by however many pixels of slack exist
        // (a browser-extension node parked past the shell is enough).
        overscrollBehavior: 'contain',
      } as React.CSSProperties}
      aria-label={i18nT('pages.chatPage.chat_messages')}
      aria-live="polite"
      onScroll={onScroll}
    >
      {/* Header spacer */}
      {headerSpacer && <div className="h-16" />}
      {/* Leading chrome in the top-down layout, in one block so the
          virtualizer can OBSERVE it: the footer, preparing caption and survey
          change height here when a turn starts, ends or goes quiet, above a
          reader in history, with no row resize to announce it. WebKit has no
          scroll anchoring, so the measured residual is what holds that reader.
          Only with the live edge at the top: the bottom layout's DOM is
          unchanged. A plain block wrapper, like the trailing one. */}
      {liveEdgeAtTop ? <div ref={virt.leadingRef} data-vc-leading="">{aboveRows}</div> : aboveRows}
      {/* Top sentinel: drives upward window expansion via virtualizer's IO. */}
      <div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />
      {/* top-16 matches the h-16 header spacer above, so the spinner clears the
          overlay header instead of sitting under it.
          overflow-anchor:none so appearing/vanishing here cannot become the
          browser's scroll anchor and jump the list mid-fetch. */}
      {loadingOlder && spinnerNearTop !== false && (
        /* ABSOLUTE overlay, not sticky: a sticky element still owns flow
           space, so mounting/unmounting it on every loadingOlder flip
           inserted/removed its own ~32px above the content — measured on the
           momentum rig as ±32px content twitches for a reader parked at the
           top, once per landing. An absolute box has zero layout footprint;
           the transcript never moves. The badge keeps its own background so
           the glyph stays legible over transcript text it now overlaps.
           `spinnerNearTop` lets the page hide it once the reader has scrolled
           away from the top, where an overlay pinned to the header would
           otherwise float over unrelated content. */
        <div className="absolute top-16 inset-x-0 z-[1] flex justify-center py-2 pointer-events-none" data-testid="older-messages-loading" role="status" aria-label={i18nT('pages.chatPage.loading_earlier_messages')} style={{ overflowAnchor: 'none', ...(historyAtBottom ? { top: 'auto', bottom: 8 } : null) }}>
          <span className="rounded-full px-2 py-1" style={{ background: 'var(--bg)' }}>
            <Loader size={16} className="animate-spin text-muted" />
          </span>
        </div>
      )}
      {/* Top spacer — reserves the height of all items above the mounted
          window so the scrollbar stays accurate while only the window
          renders real DOM (keeps fast scroll cheap — O(window) nodes).
          overflow-anchor:none so the browser anchors on real content,
          not on this spacer (which resizes as the window moves).
          `vc-spacer-skeleton` paints placeholder bars at the transcript's own
          column width: an unmounted region used to read as a blank void,
          which looks like content that vanished rather than content not yet
          drawn.
          Its height is rounded to a whole pixel. The measure farm re-prices
          unmounted rows above a reader who is sitting still, and
          Chromium's scroll anchoring moves scrollTop only by whole pixels, so
          a fractional change would leave the text shifted by the remainder,
          each line snapping to its own pixel row. The bottom spacer needs no
          rounding: a change below the reader moves nothing on screen. */}
      <div aria-hidden className="vc-spacer-skeleton mx-auto w-full" style={{ height: Math.round(virt.offsetBefore), maxWidth: 'var(--mc-content-width, 900px)', overflowAnchor: 'none' }} />
      {children}
      {/* Bottom spacer — reserves the height of all items below the
          mounted window. overflow-anchor:none (see top spacer). */}
      <div aria-hidden className="vc-spacer-skeleton mx-auto w-full" style={{ height: virt.offsetAfter, maxWidth: 'var(--mc-content-width, 900px)', overflowAnchor: 'none' }} />
      {/* Bottom sentinel: drives downward window expansion when in jump mode. */}
      <div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />
      {/* Trailing chrome, in one block so the virtualizer can OBSERVE it: the
          footer that mounts here when a reply goes quiet grows the content
          under a bottom-pinned reader with no row resize and no viewport
          change to announce it. A plain block wrapper, no class of its own,
          so the slot content keeps its width and the theming contract. */}
      <div ref={virt.trailingRef} data-vc-trailing="">
        {belowRows}
      </div>
    </div>
  )
}
