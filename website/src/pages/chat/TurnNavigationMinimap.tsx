import { createPortal } from 'react-dom'
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent, type RefObject } from 'react'
import type { ChatSection } from '../../hooks/useChatNavigation'
import type { MinimapSide } from './ChatSettings'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import { stripMd } from '../../components/notifications/notifMeta'

const MIN_TURNS = 2
const MIN_GUTTER_PX = 40
const MIN_PANE_WIDTH_PX = 560
/** The rail's `hidden md:flex` CSS breakpoint. The measure pass must apply the
 *  same floor: below it the nav is display-hidden by CSS alone, so the measure
 *  loop and in-view tracking have nothing to feed. */
const MD_BREAKPOINT_PX = 768
const DEFAULT_CONTENT_WIDTH_PX = 900
const MARKER_STEP_PX = 9
/** Hard cap on rendered markers — past this, turns are evenly bucketed so the
 *  rail's footprint stays constant no matter how much history is loaded. */
export const MAX_TICKS = 45
const PREVIEW_WIDTH_PX = 264
const TITLE_PREVIEW_MAX_CHARS = 64
const RESPONSE_PREVIEW_MAX_CHARS = 112
const PREVIEW_GAP_PX = 10
const VIEWPORT_EDGE_PX = 12
/** Fisheye widths by distance from the scrubbed marker: hovered → ±1 → ±2 → ±3 → rest. */
const FISHEYE_W = [26, 20, 15, 12]
const REST_W = 10
/** Delay before the card first opens; pointer-down bypasses it. */
const HOVER_INTENT_MS = 150

export function shortenTurnPreview(value: string, maxChars: number): string {
  const compact = stripMd(value)
  if (compact.length <= maxChars) return compact
  const budget = Math.max(1, maxChars - 3)
  const candidate = compact.slice(0, budget + 1)
  const lastSpace = candidate.lastIndexOf(' ')
  const cut = lastSpace >= Math.floor(budget * 0.6) ? lastSpace : budget
  return `${candidate.slice(0, cut).trimEnd()}...`
}

export function markerPosition(index: number, count: number): number {
  if (count <= 1) return 0
  return index / (count - 1)
}

export function pointerToTurnIndex(pointerY: number, top: number, height: number, count: number): number {
  if (count <= 1 || height <= 0) return 0
  const fraction = Math.min(1, Math.max(0, (pointerY - top) / height))
  return Math.min(count - 1, Math.max(0, Math.round(fraction * (count - 1))))
}

/** A rail marker's span of turns. Under `MAX_TICKS` every bucket is one turn;
 *  past the cap, older history is evenly bucketed while the newest turn always
 *  keeps its own dedicated final bucket so it stays individually selectable. */
export interface TurnBucket {
  /** Index into `items` of the bucket's representative (first) turn. */
  first: number
  /** Number of turns this marker covers (1 when under the cap). */
  size: number
}

export function bucketTurns(count: number): TurnBucket[] {
  if (count <= MAX_TICKS) return Array.from({ length: count }, (_, index) => ({ first: index, size: 1 }))
  const bodyTicks = MAX_TICKS - 1
  const per = (count - 1) / bodyTicks
  const body = Array.from({ length: bodyTicks }, (_, b) => {
    const first = Math.floor(b * per)
    const end = b === bodyTicks - 1 ? count - 1 : Math.floor((b + 1) * per)
    return { first, size: Math.max(1, end - first) }
  })
  return [...body, { first: count - 1, size: 1 }]
}

/** The bucket whose span contains the given turn. Linear scan: capped at MAX_TICKS entries. */
function bucketOfTurn(buckets: TurnBucket[], turnIndex: number): number {
  let found = 0
  for (let b = 0; b < buckets.length; b++) if (buckets[b].first <= turnIndex) found = b
  return found
}

function constrainedContentRect(scroller: HTMLDivElement): DOMRect | null {
  // ChatPage stamps `data-content-column` on the row wrapper it constrains to
  // `--mc-content-width`; that is the deliberate contract this probe reads.
  const mountedRow = scroller.querySelector<HTMLElement>('[data-display-index]')
  const constrained = mountedRow?.matches('[data-content-column]')
    ? mountedRow
    : mountedRow?.querySelector<HTMLElement>('[data-content-column]')
  const rect = constrained?.getBoundingClientRect()
  if (rect && rect.width > 0) return rect

  const scrollerRect = scroller.getBoundingClientRect()
  const contentWidthValue = getComputedStyle(scroller.parentElement ?? scroller)
    .getPropertyValue('--mc-content-width').trim()
  const parsed = Number.parseFloat(contentWidthValue)
  const configuredWidth = !Number.isFinite(parsed) || parsed <= 0
    ? DEFAULT_CONTENT_WIDTH_PX
    : contentWidthValue.endsWith('%') ? scroller.clientWidth * parsed / 100 : parsed
  const width = Math.min(configuredWidth, scroller.clientWidth)
  const left = scrollerRect.left + (scroller.clientWidth - width) / 2
  return { ...scrollerRect, left, right: left + width, width } as DOMRect
}

/** Whether the hosting edge has free space between the pane edge and the
 *  content column. */
function hasSafeGutter(scroller: HTMLDivElement, side: MinimapSide): boolean {
  if (window.innerWidth < MD_BREAKPOINT_PX) return false
  if (scroller.clientWidth < MIN_PANE_WIDTH_PX) return false
  const scrollerRect = scroller.getBoundingClientRect()
  const contentRect = constrainedContentRect(scroller)
  if (!contentRect) return false
  return side === 'right'
    ? scrollerRect.left + scroller.clientWidth - contentRect.right >= MIN_GUTTER_PX
    : contentRect.left - scrollerRect.left >= MIN_GUTTER_PX
}

/** Marker length in px: the scrubbed marker grows and the neighbours taper
 *  off (radius 3), so the rail reads as a lens rather than a single lit tick. */
function markerWidth(index: number, selected: number | null): number {
  if (selected === null) return REST_W
  const distance = Math.abs(index - selected)
  return distance < FISHEYE_W.length ? FISHEYE_W[distance] : REST_W
}

/** At rest, markers for on-screen turns read as text and the rest is one
 *  gray. While scrubbing, only the lens is lit — the scrubbed marker reads as
 *  text with a muted rim on its immediate neighbours, and the in-view
 *  highlight yields so the wave is the single focus. */
function markerColor(index: number, selected: number | null, visible: ReadonlySet<number>): string {
  if (selected !== null) {
    if (index === selected) return 'var(--text)'
    if (Math.abs(index - selected) === 1) return 'var(--muted)'
    return 'var(--border)'
  }
  return visible.has(index) ? 'var(--text)' : 'var(--border)'
}

interface TurnNavigationMinimapProps {
  items: ChatSection[]
  scrollerRef: RefObject<HTMLDivElement | null>
  onNavigate: (displayIndex: number, opts?: { instant?: boolean }) => void
  /** True while the server still holds rows above the loaded window: the rail
   *  maps loaded turns only, and the aria label and meta row say so. */
  windowed?: boolean
  /** Hosting pane edge. The right-edge variant replaces the native scrollbar
   *  while the rail is shown. */
  side?: MinimapSide
}

export default function TurnNavigationMinimap({
  items,
  scrollerRef,
  onNavigate,
  windowed,
  side = 'left',
}: TurnNavigationMinimapProps) {
  const [hasGutter, setHasGutter] = useState(false)
  const [focused, setFocused] = useState(false)
  const [coarsePointer, setCoarsePointer] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // Buckets with a turn currently on screen — the in-view highlight, and where
  // keyboard browsing starts (at the last of them, the reading position).
  const [visible, setVisible] = useState<ReadonlySet<number>>(() => new Set())
  const [previewTop, setPreviewTop] = useState(0)
  const [previewLeft, setPreviewLeft] = useState(0)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const buckets = useMemo(() => bucketTurns(items.length), [items.length])
  const bucketsRef = useRef(buckets)
  bucketsRef.current = buckets

  // `selected` is a BUCKET index. Identity is carried by the representative
  // turn's id so selection survives prepends: after "load earlier" lands, the
  // same turn maps back to whichever bucket now contains it.
  const selected = useMemo(() => {
    if (selectedId === null) return null
    const turnIndex = items.findIndex(item => item.id === selectedId)
    return turnIndex >= 0 ? bucketOfTurn(buckets, turnIndex) : null
  }, [buckets, items, selectedId])
  const selectedRef = useRef<number | null>(selected)
  selectedRef.current = selected
  // `items` is rebuilt on every streamed token (its source memo keys on the
  // messages array), so geometry work keys on what actually changes the
  // rail: the ordered list of display indexes. Everything else reads the
  // latest items through a ref.
  const itemsRef = useRef(items)
  itemsRef.current = items
  const displayKey = items.map(item => item.displayIdx).join(',')
  const railHeight = useMemo(() => Math.max(24, (buckets.length - 1) * MARKER_STEP_PX), [buckets.length])
  const clearClose = useCallback(() => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = null
  }, [])
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingOpenIndex = useRef<number | null>(null)
  const clearOpen = useCallback(() => {
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = null
    pendingOpenIndex.current = null
  }, [])
  const scheduleClose = useCallback(() => {
    clearClose()
    closeTimer.current = setTimeout(() => setSelectedId(null), 120)
  }, [clearClose])

  // Whether the card was visible on the previous placement — gates the top
  // glide: the card GLIDES between markers while visible (same curve as the
  // fisheye, so they move as one) but SNAPS into place when it first appears,
  // otherwise it would swoop across the rail from wherever it last closed.
  const cardWasVisibleRef = useRef(false)

  const placePreview = useCallback((bucketIndex: number) => {
    const button = buttonRef.current
    const currentBuckets = bucketsRef.current
    const bucket = currentBuckets[bucketIndex]
    const item = bucket ? itemsRef.current[bucket.first] : undefined
    if (!button || !bucket || !item) return
    const rect = button.getBoundingClientRect()
    const markerY = rect.top + markerPosition(bucketIndex, currentBuckets.length) * rect.height
    // Rough content estimate for viewport clamping only: title row + optional
    // neighbours + optional response snippet + meta row.
    const neighbours = (bucket.first > 0 ? 17 : 0) + (bucket.first + 1 < itemsRef.current.length ? 17 : 0)
    const estimatedHeight = 40 + neighbours + (item.response ? 36 : 0) + 18
    setPreviewTop(Math.min(
      window.innerHeight - estimatedHeight - VIEWPORT_EDGE_PX,
      Math.max(VIEWPORT_EDGE_PX, markerY - estimatedHeight / 2),
    ))
    // The card floats on the content side of the rail.
    setPreviewLeft(side === 'right'
      ? Math.max(VIEWPORT_EDGE_PX, rect.left - PREVIEW_GAP_PX - PREVIEW_WIDTH_PX)
      : Math.min(window.innerWidth - PREVIEW_WIDTH_PX - VIEWPORT_EDGE_PX, rect.right + PREVIEW_GAP_PX))
  }, [side])

  const select = useCallback((bucketIndex: number | null) => {
    clearClose()
    const bucket = bucketIndex === null ? undefined : buckets[bucketIndex]
    const item = bucket ? items[bucket.first] : undefined
    setSelectedId(item?.id ?? null)
    if (item && bucketIndex !== null) placePreview(bucketIndex)
  }, [buckets, clearClose, items, placePreview])

  useEffect(() => {
    const query = window.matchMedia?.('(pointer: coarse)')
    const update = () => setCoarsePointer(!!query?.matches)
    update()
    query?.addEventListener?.('change', update)
    return () => query?.removeEventListener?.('change', update)
  }, [])

  useEffect(() => {
    if (selectedId !== null && !items.some(item => item.id === selectedId)) setSelectedId(null)
  }, [items, selectedId])

  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller || itemsRef.current.length < MIN_TURNS || coarsePointer) {
      setHasGutter(false)
      setVisible(prev => (prev.size === 0 ? prev : new Set()))
      return
    }
    // A turn owns every row from its prompt up to the next turn's prompt, so a
    // long reply keeps its marker current after the prompt row scrolls away.
    // Under bucketing the marker index is the bucket containing that turn.
    const turnStarts = itemsRef.current.map(item => item.displayIdx)
    const markerForRow = (displayIndex: number): number | undefined => {
      let lo = 0
      let hi = turnStarts.length - 1
      let found = -1
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (turnStarts[mid] <= displayIndex) { found = mid; lo = mid + 1 } else hi = mid - 1
      }
      return found >= 0 ? bucketOfTurn(bucketsRef.current, found) : undefined
    }
    let frame = 0
    let settleFrames = 6
    const measure = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const gutter = hasSafeGutter(scroller, side)
        setHasGutter(gutter)
        // Right edge only: the rail replaces the native scrollbar while shown.
        // `scrollbarWidth` is not a property TranscriptScrollShell claims, so
        // this stays inside its style contract.
        if (side === 'right') scroller.style.scrollbarWidth = gutter ? 'none' : ''
        if (!gutter) {
          setVisible(prev => (prev.size === 0 ? prev : new Set()))
          return
        }
        const viewport = scroller.getBoundingClientRect()
        const next = new Set<number>()
        const mountedRows = scroller.querySelectorAll<HTMLElement>('[data-display-index]')
        for (const row of mountedRows) {
          const displayIndex = Number(row.dataset.displayIndex)
          const markerIndex = markerForRow(displayIndex)
          if (markerIndex === undefined) continue
          const rect = row.getBoundingClientRect()
          if (rect.bottom > viewport.top && rect.top < viewport.bottom) next.add(markerIndex)
        }
        // Scroll fires this every frame; only an actual membership change may
        // re-render the rail.
        setVisible(prev => {
          if (prev.size === next.size && [...next].every(index => prev.has(index))) return prev
          return next
        })
        if (selectedRef.current !== null) placePreview(selectedRef.current)
        if (settleFrames-- > 0) measure()
      })
    }
    measure()
    scroller.addEventListener('scroll', measure, { passive: true })
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    resizeObserver?.observe(scroller)
    // Only a row appearing, disappearing, or being re-labelled by the
    // virtualizer changes the geometry this pass reads. Streaming text mutates
    // deep inside a row every frame; those records are skipped here.
    const rowMutation = (records: MutationRecord[]) => records.some(record => {
      if (record.type === 'attributes') return true
      for (const node of [...record.addedNodes, ...record.removedNodes]) {
        if (!(node instanceof HTMLElement)) continue
        if (node.hasAttribute('data-display-index') || node.querySelector('[data-display-index]')) return true
      }
      return false
    })
    const mutationObserver = typeof MutationObserver === 'undefined'
      ? null
      : new MutationObserver(records => { if (rowMutation(records)) measure() })
    mutationObserver?.observe(scroller, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-display-index'],
    })
    window.addEventListener('resize', measure)
    return () => {
      cancelAnimationFrame(frame)
      scroller.removeEventListener('scroll', measure)
      resizeObserver?.disconnect()
      mutationObserver?.disconnect()
      window.removeEventListener('resize', measure)
      scroller.style.scrollbarWidth = ''
    }
  }, [coarsePointer, displayKey, placePreview, scrollerRef, side])

  useEffect(() => () => { clearClose(); clearOpen() }, [clearClose, clearOpen])

  // ---- Drag-to-scrub: hold the pointer down and drag along the rail to
  // scroll the chat live. Pointer capture engages only once a drag actually
  // crosses a marker (capturing on pointerdown retargets the derived click
  // off the rail, so plain clicks would stop jumping).
  const draggingRef = useRef(false)
  // True once a drag crossed a marker — suppresses the trailing click (the
  // chat already followed the pointer; a smooth jump would fight it).
  const draggedRef = useRef(false)

  const bucketFromPointer = useCallback((clientY: number) => {
    const button = buttonRef.current
    if (!button) return -1
    const rect = button.getBoundingClientRect()
    return pointerToTurnIndex(clientY, rect.top, rect.height, bucketsRef.current.length)
  }, [])

  const onPointerDown = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return
    clearOpen()
    draggingRef.current = true
    draggedRef.current = false
    const bucketIndex = bucketFromPointer(event.clientY)
    if (bucketIndex >= 0) select(bucketIndex)
  }, [bucketFromPointer, clearOpen, select])

  const onPointerMove = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (!draggingRef.current) return
    const bucketIndex = bucketFromPointer(event.clientY)
    if (bucketIndex < 0 || bucketIndex === selectedRef.current) return
    if (!draggedRef.current) {
      // First marker crossing = this is a drag. Take capture now so the scrub
      // keeps tracking when the cursor drifts off the rail horizontally.
      // Guarded: not implemented in all environments (jsdom).
      try { buttonRef.current?.setPointerCapture(event.pointerId) } catch { /* noop */ }
    }
    draggedRef.current = true
    select(bucketIndex)
    const bucket = bucketsRef.current[bucketIndex]
    const item = bucket ? itemsRef.current[bucket.first] : undefined
    // Instant scroll — a smooth glide would lag the pointer and queue easings
    // on every marker crossing.
    if (item) onNavigate(item.displayIdx, { instant: true })
  }, [bucketFromPointer, onNavigate, select])

  const onPointerUp = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (!draggingRef.current) return
    draggingRef.current = false
    try { buttonRef.current?.releasePointerCapture(event.pointerId) } catch { /* noop */ }
  }, [])

  if (items.length < MIN_TURNS || !hasGutter || coarsePointer) return null
  const activeBucket = selected === null ? null : buckets[selected]
  const active = activeBucket ? items[activeBucket.first] : null
  const activeMeta = activeBucket
    ? activeBucket.size > 1
      ? i18nT(windowed ? 'pages.chatPage.turn_minimap_meta_grouped_windowed' : 'pages.chatPage.turn_minimap_meta_grouped', { first: activeBucket.first + 1, last: activeBucket.first + activeBucket.size, count: items.length })
      : i18nT(windowed ? 'pages.chatPage.turn_minimap_meta_position_windowed' : 'pages.chatPage.turn_minimap_meta_position', { n: activeBucket.first + 1, count: items.length })
    : null
  const label = active && activeMeta
    ? i18nT('pages.chatPage.turn_minimap_active', { position: activeMeta, prompt: active.label })
    : i18nT('pages.chatPage.turn_minimap')
  const prevItem = activeBucket && activeBucket.first > 0 ? items[activeBucket.first - 1] : null
  const nextItem = activeBucket ? items[activeBucket.first + 1] ?? null : null
  const activeTime = active?.ts ? fmtRelative(active.ts) : null
  // Gate the card's top glide on last frame's visibility (render-time update,
  // read before it is overwritten).
  const glideTop = cardWasVisibleRef.current && selected !== null
  cardWasVisibleRef.current = selected !== null

  const onMouseMove = (event: MouseEvent<HTMLButtonElement>) => {
    if (draggingRef.current) return
    const rect = event.currentTarget.getBoundingClientRect()
    const index = pointerToTurnIndex(event.clientY, rect.top, rect.height, buckets.length)
    if (selectedRef.current !== null) { select(index); return }
    // Delay the first open; movement retargets it without restarting.
    pendingOpenIndex.current = index
    if (openTimer.current === null) {
      openTimer.current = setTimeout(() => {
        openTimer.current = null
        const target = pendingOpenIndex.current
        pendingOpenIndex.current = null
        if (target !== null) select(target)
      }, HOVER_INTENT_MS)
    }
  }

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    // Where keyboard browsing starts with nothing selected: the reading
    // position — the last on-screen turn.
    let lastVisible = -1
    for (const index of visible) lastVisible = Math.max(lastVisible, index)
    const current = selected ?? (lastVisible >= 0 ? lastVisible : 0)
    let next = current
    // With nothing selected, the first Arrow reveals the starting turn (the
    // last one on screen) rather than stepping past it.
    if (event.key === 'ArrowDown') next = selected === null ? current : Math.min(buckets.length - 1, current + 1)
    else if (event.key === 'ArrowUp') next = selected === null ? current : Math.max(0, current - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = buckets.length - 1
    else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onNavigate(items[buckets[current].first].displayIdx)
      return
    } else if (event.key === 'Escape') {
      if (selectedId === null) return
      event.preventDefault()
      setSelectedId(null)
      return
    } else return
    event.preventDefault()
    select(next)
  }

  return (
    // Desktop-only by design: a hover-revealed edge rail has no useful
    // expression below the `md` breakpoint or on coarse pointers, and the
    // transcript scrolls exactly as before without it.
    <nav
      data-testid="turn-navigation-minimap"
      aria-label={i18nT('pages.chatPage.turn_minimap_landmark')}
      className={`absolute ${side === 'right' ? 'right-2 justify-end' : 'left-2 justify-start'} top-20 bottom-36 z-[3] hidden md:flex w-10 pointer-events-none items-center`}
    >
      {/* Accessible-name changes on an already-focused control are announced
          inconsistently across AT; this live region speaks the keyboard
          selection while the rail has focus and stays silent for hover. */}
      <span className="sr-only" aria-live="polite" aria-atomic="true">
        {focused && selected !== null ? label : ''}
      </span>
      <div className={`flex h-full flex-col ${side === 'right' ? 'items-end' : 'items-start'} justify-center gap-1.5`}>
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-describedby={active ? 'turn-navigation-preview' : undefined}
        className="relative w-7 cursor-pointer pointer-events-auto bg-transparent border-none p-0 touch-none select-none focus:outline-hidden focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)] rounded"
        style={{ height: `min(calc(100% - 8px), ${railHeight}px)` }}
        onMouseMove={onMouseMove}
        onMouseLeave={() => { clearOpen(); scheduleClose() }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        // Focus alone does not open the card: a Tab pass toward the composer
        // must not flash it. The first navigation key opens it (see onKeyDown).
        onFocus={() => setFocused(true)}
        onBlur={() => { setFocused(false); scheduleClose() }}
        onClick={(event) => {
          // A real drag already scrolled the chat live; the derived trailing
          // click must not fire a second (smooth) jump at the same target.
          if (draggedRef.current) {
            draggedRef.current = false
            return
          }
          const rect = event.currentTarget.getBoundingClientRect()
          const index = event.detail > 0
            ? pointerToTurnIndex(event.clientY, rect.top, rect.height, buckets.length)
            : (selected ?? 0)
          select(index)
          onNavigate(items[buckets[index].first].displayIdx)
        }}
        onKeyDown={onKeyDown}
      >
        {buckets.map((bucket, index) => (
          <span
            key={items[bucket.first].id}
            data-testid="turn-navigation-marker"
            data-target-display-index={items[bucket.first].displayIdx}
            data-in-view={visible.has(index) ? 'true' : 'false'}
            aria-hidden
            className={`absolute ${side === 'right' ? 'right-0' : 'left-0'} block rounded-full`}
            style={{
              top: `${markerPosition(index, buckets.length) * 100}%`,
              width: markerWidth(index, selected),
              height: index === selected ? 3 : 2.5,
              background: markerColor(index, selected, visible),
              transform: 'translateY(-50%)',
              transition: 'width .18s cubic-bezier(.32,.72,0,1), background .18s ease, height .18s ease',
            }}
          />
        ))}
      </button>
      </div>
      {active && activeBucket && createPortal(
        // The card floats beside the rail, toward the content: the scrubbed
        // turn in its
        // conversational neighbourhood (one faint neighbour title above and
        // below — three title lines max, by design), the reply snippet, and a
        // meta row. Its top glides on the fisheye's curve while visible.
        // The text is deliberately selectable; hover handlers only keep it
        // open while the pointer crosses from the rail; all navigation stays
        // on the single keyboard-accessible button above.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          id="turn-navigation-preview"
          role="tooltip"
          data-testid="turn-navigation-preview"
          className="fixed z-[100] max-w-[calc(100vw-5rem)] rounded-[10px] border border-border bg-bg-elevated px-3 py-2.5 text-left shadow-lg pointer-events-auto select-text"
          style={{
            top: previewTop,
            left: previewLeft,
            width: PREVIEW_WIDTH_PX,
            transition: glideTop ? 'top .18s cubic-bezier(.32,.72,0,1)' : undefined,
          }}
          onMouseEnter={clearClose}
          onMouseLeave={scheduleClose}
        >
          {prevItem && (
            <div data-testid="turn-navigation-preview-neighbor" className="truncate text-[10.5px] leading-[15px] text-text opacity-35">
              <span className="inline-block w-[26px] text-[10px] text-muted">#{activeBucket.first}</span>
              {shortenTurnPreview(prevItem.prompt, TITLE_PREVIEW_MAX_CHARS)}
            </div>
          )}
          <div className="truncate text-[12.5px] leading-normal font-semibold text-text">
            <span className="inline-block w-[26px] text-[10px] font-normal text-muted">#{activeBucket.first + 1}</span>
            {shortenTurnPreview(active.prompt, TITLE_PREVIEW_MAX_CHARS)}
          </div>
          {nextItem && (
            <div data-testid="turn-navigation-preview-neighbor" className="truncate text-[10.5px] leading-[15px] text-text opacity-35">
              <span className="inline-block w-[26px] text-[10px] text-muted">#{activeBucket.first + 2}</span>
              {shortenTurnPreview(nextItem.prompt, TITLE_PREVIEW_MAX_CHARS)}
            </div>
          )}
          {active.response && (
            <div className="mt-1.5 border-t border-border pt-1.5 text-[12px] leading-snug text-muted overflow-hidden" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
              {shortenTurnPreview(active.response, RESPONSE_PREVIEW_MAX_CHARS)}
            </div>
          )}
          <div data-testid="turn-navigation-preview-meta" className="mt-1.5 flex items-baseline justify-between gap-2 text-[10.5px] leading-[14px] text-muted opacity-80">
            <span className="truncate">{activeMeta}</span>
            {activeTime && <span className="shrink-0">{activeTime}</span>}
          </div>
        </div>,
        document.body,
      )}
    </nav>
  )
}
