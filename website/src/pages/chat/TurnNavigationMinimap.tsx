import { createPortal } from 'react-dom'
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type MouseEvent, type RefObject } from 'react'
import type { ChatSection } from '../../hooks/useChatNavigation'
import { i18nT } from '../../i18n/t'
import { stripMd } from '../../components/notifications/notifMeta'

const MIN_TURNS = 2
const MIN_GUTTER_PX = 40
const MIN_PANE_WIDTH_PX = 560
const DEFAULT_CONTENT_WIDTH_PX = 900
const MARKER_STEP_PX = 9
/** First hover waits this long before opening, so a pointer crossing the gutter does not flash the card. */
const OPEN_DELAY_MS = 150
const PREVIEW_WIDTH_PX = 288
const PROMPT_PREVIEW_MAX_CHARS = 64
const RESPONSE_PREVIEW_MAX_CHARS = 112
const PREVIEW_GAP_PX = 10
const VIEWPORT_EDGE_PX = 12

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

function hasSafeLeftGutter(scroller: HTMLDivElement): boolean {
  if (scroller.clientWidth < MIN_PANE_WIDTH_PX) return false
  const scrollerRect = scroller.getBoundingClientRect()
  const contentRect = constrainedContentRect(scroller)
  return !!contentRect && contentRect.left - scrollerRect.left >= MIN_GUTTER_PX
}

function markerWidth(index: number, selected: number | null): number {
  if (selected === null) return 14
  const distance = Math.abs(index - selected)
  if (distance === 0) return 26
  if (distance === 1) return 20
  if (distance === 2) return 16
  return 14
}

/** Older history the rail does not map yet. Present only while the server
 *  still holds rows above the loaded window (`slotHasMore`). */
export interface EarlierHistory {
  loading: boolean
  onLoad: () => void
}

interface TurnNavigationMinimapProps {
  items: ChatSection[]
  scrollerRef: RefObject<HTMLDivElement | null>
  onNavigate: (displayIndex: number) => void
  /** When set, the rail is a window: an end-cap above the first marker says so and loads older history on click. */
  earlier?: EarlierHistory
}

export default function TurnNavigationMinimap({
  items,
  scrollerRef,
  earlier,
  onNavigate,
}: TurnNavigationMinimapProps) {
  const [hasGutter, setHasGutter] = useState(false)
  const [focused, setFocused] = useState(false)
  const [coarsePointer, setCoarsePointer] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [previewTop, setPreviewTop] = useState(0)
  const [previewLeft, setPreviewLeft] = useState(0)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const markerRefs = useRef<Array<HTMLSpanElement | null>>([])
  const visibleMarkerIndexes = useRef(new Set<number>())
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const selected = useMemo(() => {
    if (selectedId === null) return null
    const index = items.findIndex(item => item.id === selectedId)
    return index >= 0 ? index : null
  }, [items, selectedId])
  const selectedRef = useRef<number | null>(selected)
  selectedRef.current = selected
  // `items` is rebuilt on every streamed token (its source memo keys on the
  // messages array), so geometry work keys on what actually changes the
  // rail: the ordered list of display indexes. Everything else reads the
  // latest items through a ref.
  const itemsRef = useRef(items)
  itemsRef.current = items
  const displayKey = items.map(item => item.displayIdx).join(',')
  const railHeight = useMemo(() => Math.max(24, (items.length - 1) * MARKER_STEP_PX), [items.length])
  const clearClose = useCallback(() => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = null
  }, [])
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearOpen = useCallback(() => {
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = null
  }, [])
  const scheduleClose = useCallback(() => {
    clearOpen()
    clearClose()
    closeTimer.current = setTimeout(() => setSelectedId(null), 120)
  }, [clearClose, clearOpen])

  const placePreview = useCallback((index: number) => {
    const button = buttonRef.current
    const current = itemsRef.current
    if (!button || !current[index]) return
    const rect = button.getBoundingClientRect()
    const markerY = rect.top + markerPosition(index, current.length) * rect.height
    const estimatedHeight = current[index].response ? 92 : 56
    setPreviewTop(Math.min(
      window.innerHeight - estimatedHeight - VIEWPORT_EDGE_PX,
      Math.max(VIEWPORT_EDGE_PX, markerY - estimatedHeight / 2),
    ))
    setPreviewLeft(Math.min(
      window.innerWidth - PREVIEW_WIDTH_PX - VIEWPORT_EDGE_PX,
      rect.right + PREVIEW_GAP_PX,
    ))
  }, [])

  const select = useCallback((index: number | null) => {
    clearClose()
    const item = index === null ? undefined : items[index]
    setSelectedId(item?.id ?? null)
    if (item && index !== null) placePreview(index)
  }, [clearClose, items, placePreview])

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
      visibleMarkerIndexes.current.clear()
      return
    }
    // A turn owns every row from its prompt up to the next turn's prompt, so a
    // long reply keeps its marker lit after the prompt row scrolls away.
    const turnStarts = itemsRef.current.map(item => item.displayIdx)
    const markerForRow = (displayIndex: number): number | undefined => {
      let lo = 0
      let hi = turnStarts.length - 1
      let found = -1
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (turnStarts[mid] <= displayIndex) { found = mid; lo = mid + 1 } else hi = mid - 1
      }
      return found >= 0 ? found : undefined
    }
    let frame = 0
    let settleFrames = 6
    // Markers mount one commit after `hasGutter` flips; allow a few frames for
    // that, never an open-ended retry.
    let markerRetryFrames = 6
    const measure = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const gutter = hasSafeLeftGutter(scroller)
        setHasGutter(gutter)
        if (!gutter) {
          // Rail not rendered: there are no markers to reconcile, so do not
          // retry — the scroll / resize / mutation listeners re-measure when
          // the layout changes.
          visibleMarkerIndexes.current.clear()
          return
        }
        const viewport = scroller.getBoundingClientRect()
        const nextVisible = new Set<number>()
        const mountedRows = scroller.querySelectorAll<HTMLElement>('[data-display-index]')
        for (const row of mountedRows) {
          const displayIndex = Number(row.dataset.displayIndex)
          const markerIndex = markerForRow(displayIndex)
          if (markerIndex === undefined) continue
          const rect = row.getBoundingClientRect()
          if (rect.bottom > viewport.top && rect.top < viewport.bottom) nextVisible.add(markerIndex)
        }
        const changed = new Set([...visibleMarkerIndexes.current, ...nextVisible])
        let needsMarkerRetry = false
        for (const index of changed) {
          const wasVisible = visibleMarkerIndexes.current.has(index)
          const isVisible = nextVisible.has(index)
          const marker = markerRefs.current[index]
          if (!marker) {
            needsMarkerRetry = true
            nextVisible.delete(index)
            continue
          }
          const markerIsVisible = marker.dataset.inView === 'true'
          if (wasVisible === isVisible && markerIsVisible === isVisible) continue
          marker.dataset.inView = isVisible ? 'true' : 'false'
          marker.style.background = isVisible ? 'var(--accent)' : 'var(--border-strong)'
          marker.style.opacity = isVisible ? '1' : '0.9'
          marker.style.width = `${markerWidth(index, selectedRef.current)}px`
        }
        visibleMarkerIndexes.current = nextVisible
        if (selectedRef.current !== null) placePreview(selectedRef.current)
        if ((needsMarkerRetry && markerRetryFrames-- > 0) || settleFrames-- > 0) measure()
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
    }
  }, [coarsePointer, displayKey, hasGutter, placePreview, scrollerRef])

  useEffect(() => {
    for (let index = 0; index < items.length; index++) {
      const marker = markerRefs.current[index]
      if (!marker) continue
      marker.style.width = `${markerWidth(index, selected)}px`
    }
  }, [items.length, selected])

  useEffect(() => () => { clearClose(); clearOpen() }, [clearClose, clearOpen])

  if (items.length < MIN_TURNS || !hasGutter || coarsePointer) return null
  const active = selected === null ? null : items[selected]
  const label = selected !== null && active
    ? i18nT(earlier ? 'pages.chatPage.turn_minimap_active_windowed' : 'pages.chatPage.turn_minimap_active', { current: selected + 1, count: items.length, prompt: active.label })
    : i18nT('pages.chatPage.turn_minimap')

  const onMouseMove = (event: MouseEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const index = pointerToTurnIndex(event.clientY, rect.top, rect.height, items.length)
    if (selected !== null) {
      select(index)
      return
    }
    // Nothing open yet: wait out an incidental traversal before showing the card.
    clearClose()
    clearOpen()
    openTimer.current = setTimeout(() => select(index), OPEN_DELAY_MS)
  }

  /** Where keyboard browsing starts with nothing selected: the last turn on screen, else the first. */
  const initialKeyboardIndex = () => {
    let last = -1
    for (const index of visibleMarkerIndexes.current) last = Math.max(last, index)
    return last >= 0 ? last : 0
  }

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const current = selected ?? initialKeyboardIndex()
    let next = current
    // With nothing selected, the first Arrow reveals the starting turn (the
    // last one on screen) rather than stepping past it.
    if (event.key === 'ArrowDown') next = selected === null ? current : Math.min(items.length - 1, current + 1)
    else if (event.key === 'ArrowUp') next = selected === null ? current : Math.max(0, current - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = items.length - 1
    else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onNavigate(items[current].displayIdx)
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
    // Desktop-only by design: a hover-revealed gutter rail has no useful
    // expression below the `md` breakpoint or on coarse pointers, and the
    // transcript scrolls exactly as before without it.
    <nav
      data-testid="turn-navigation-minimap"
      aria-label={i18nT('pages.chatPage.turn_minimap_landmark')}
      className="absolute left-2 top-20 bottom-36 z-[3] hidden md:flex w-10 pointer-events-none items-center"
    >
      {/* Accessible-name changes on an already-focused control are announced
          inconsistently across AT; this live region speaks the keyboard
          selection while the rail has focus and stays silent for hover. */}
      <span className="sr-only" aria-live="polite" aria-atomic="true">
        {focused && selected !== null ? label : ''}
      </span>
      <div className="flex h-full flex-col items-start justify-center gap-1.5">
      {earlier && (
        // The rail maps loaded turns only. This cap says so, honestly, at the
        // top of the rail, and is the "load older" affordance the outline
        // review on #8221 asked for -- keyed on slotHasMore by the caller.
        <button
          type="button"
          data-testid="turn-navigation-earlier"
          aria-label={i18nT('pages.chatPage.turn_minimap_earlier')}
          aria-busy={earlier.loading}
          title={i18nT('pages.chatPage.turn_minimap_earlier')}
          // aria-disabled, not disabled: the cap stays in the tab order and keeps
          // its tooltip while a page loads, mirroring EarlierMessagesBar.
          aria-disabled={earlier.loading}
          className={`pointer-events-auto flex w-7 flex-col items-start gap-[3px] border-none bg-transparent p-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)] rounded ${earlier.loading ? 'cursor-progress' : 'cursor-pointer'}`}
          onClick={() => { if (!earlier.loading) earlier.onLoad() }}
        >
          {[10, 7, 4].map(width => (
            <span key={width} aria-hidden className="block h-[2px] rounded-full" style={{ width, background: 'var(--border-strong)', opacity: earlier.loading ? 0.35 : 0.6 }} />
          ))}
        </button>
      )}
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-describedby={active ? 'turn-navigation-preview' : undefined}
        className="relative w-7 cursor-pointer pointer-events-auto bg-transparent border-none p-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)] rounded"
        style={{ height: `min(calc(100% - ${earlier ? 32 : 8}px), ${railHeight}px)` }}
        onMouseMove={onMouseMove}
        onMouseLeave={scheduleClose}
        // Focus alone does not open the card: a Tab pass toward the composer
        // must not flash it. The first navigation key opens it (see onKeyDown).
        onFocus={() => setFocused(true)}
        onBlur={() => { setFocused(false); scheduleClose() }}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          const index = event.detail > 0
            ? pointerToTurnIndex(event.clientY, rect.top, rect.height, items.length)
            : (selected ?? 0)
          select(index)
          onNavigate(items[index].displayIdx)
        }}
        onKeyDown={onKeyDown}
      >
        {items.map((item, index) => (
          <span
            key={item.id}
            ref={element => { markerRefs.current[index] = element }}
            data-testid="turn-navigation-marker"
            data-target-display-index={item.displayIdx}
            data-in-view="false"
            aria-hidden
            className="absolute left-0 block h-[3px] rounded-full transition-[width,background-color] duration-150"
            style={{
              top: `${markerPosition(index, items.length) * 100}%`,
              width: markerWidth(index, selected),
              background: 'var(--muted-strong)',
              opacity: 0.72,
              transform: 'translateY(-50%)',
            }}
          />
        ))}
      </button>
      </div>
      {active && createPortal(
        // The tooltip text is deliberately selectable. Hover handlers only keep
        // it open while the pointer crosses from the rail; all navigation stays
        // on the single keyboard-accessible button above.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          id="turn-navigation-preview"
          role="tooltip"
          data-testid="turn-navigation-preview"
          className="fixed z-[100] max-w-[calc(100vw-5rem)] rounded-lg border border-border bg-bg-elevated px-3 py-2 text-left shadow-xl pointer-events-auto select-text"
          style={{
            top: previewTop,
            left: previewLeft,
            width: PREVIEW_WIDTH_PX,
            borderLeftColor: 'var(--accent)',
            borderLeftWidth: 3,
          } as CSSProperties}
          onMouseEnter={clearClose}
          onMouseLeave={scheduleClose}
        >
          <div className="text-[12px] font-medium leading-[17px] text-text">
            {shortenTurnPreview(active.prompt, PROMPT_PREVIEW_MAX_CHARS)}
          </div>
          {active.response && (
            <div className="mt-1.5 border-t border-border pt-1.5 text-[11px] leading-[16px] text-muted">
              {shortenTurnPreview(active.response, RESPONSE_PREVIEW_MAX_CHARS)}
            </div>
          )}
        </div>,
        document.body,
      )}
    </nav>
  )
}
