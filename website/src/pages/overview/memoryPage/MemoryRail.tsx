/**
 * The memory page's wide filter rail: sticky beside the list, as tall as the part
 * of its scroll area that is in view, resizable at its right edge, and animated
 * open and shut.
 *
 * Scrolling has one rule. A wheel over the rail moves the innermost scroller under
 * the pointer that can still move that way (a value group, else the rail), and never
 * the page; a wheel over the list moves the page. Browsers otherwise pick the
 * scroller by how far each one has gone, so the rail sometimes moved and sometimes
 * the page did.
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

export const RAIL_MIN = 200
export const RAIL_MAX = 420
/** Open and close take this long (ms), the same both ways. */
export const RAIL_MOTION_MS = 225
// The rail's gap to the list, animated with it so the list reflows smoothly.
const GAP = 24
const STICKY_TOP = 16

export const prefersReducedMotion = () => typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

const scrolls = (el: Element) => { const y = getComputedStyle(el).overflowY; return y === 'auto' || y === 'scroll' }

/** The nearest ancestor that scrolls: the page's own scroll area. */
function scrollParent(el: HTMLElement | null): HTMLElement | null {
  for (let node = el?.parentElement; node; node = node.parentElement) if (scrolls(node)) return node
  return null
}

/** Move the innermost scroller between `target` and `root` that can still move by `dy`. */
export function routeWheel(target: Element | null, root: HTMLElement, dy: number): boolean {
  for (let node = target; node; node = node.parentElement) {
    // The rail and its groups say they scroll (`data-scroll`); others are read from their style.
    if (node instanceof HTMLElement && (node.dataset.scroll === 'y' || scrolls(node)) && node.scrollHeight > node.clientHeight) {
      const room = dy > 0 ? node.scrollHeight - node.clientHeight - node.scrollTop : node.scrollTop
      if (room > 0.5) { node.scrollTop += dy; return true }
    }
    if (node === root) break
  }
  return false
}

export function FilterRail({ open, width, onResize, children }: { open: boolean; width: number; onResize: (width: number, save?: boolean) => void; children: ReactNode }) {
  const [mounted, setMounted] = useState(open)
  const [expanded, setExpanded] = useState(open)
  const [resizing, setResizing] = useState(false)
  const shell = useRef<HTMLDivElement>(null)
  const aside = useRef<HTMLElement>(null)
  const [maxHeight, setMaxHeight] = useState<number>()

  // Opening mounts at width 0, then widens; closing narrows, then unmounts.
  useEffect(() => {
    if (open) {
      setMounted(true)
      if (prefersReducedMotion()) { setExpanded(true); return }
      const frame = requestAnimationFrame(() => setExpanded(true))
      return () => cancelAnimationFrame(frame)
    }
    setExpanded(false)
    if (prefersReducedMotion()) { setMounted(false); return }
    const timer = setTimeout(() => setMounted(false), RAIL_MOTION_MS + 50)
    return () => clearTimeout(timer)
  }, [open])

  // The rail fits the part of the scroll area in view, so its own end is always reachable.
  useLayoutEffect(() => {
    if (!mounted) return
    const page = scrollParent(shell.current)
    const fit = () => {
      const box = shell.current
      if (!box) return
      const view = page ? page.getBoundingClientRect() : { top: 0, bottom: window.innerHeight }
      const top = Math.max(box.getBoundingClientRect().top, view.top + STICKY_TOP)
      setMaxHeight(Math.max(160, Math.floor(view.bottom - top - STICKY_TOP)))
    }
    fit()
    let frame = 0
    const later = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(fit) }
    const target: HTMLElement | Window = page ?? window
    target.addEventListener('scroll', later, { passive: true })
    window.addEventListener('resize', later)
    return () => { cancelAnimationFrame(frame); target.removeEventListener('scroll', later); window.removeEventListener('resize', later) }
  }, [mounted])

  // A wheel over the rail never reaches the page (a non-passive listener, so it can stop it).
  useEffect(() => {
    const root = aside.current
    if (!mounted || !root) return
    const wheel = (event: WheelEvent) => {
      if (event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return
      event.preventDefault()
      const dy = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaMode === 2 ? event.deltaY * root.clientHeight : event.deltaY
      routeWheel(event.target as Element, root, dy)
    }
    root.addEventListener('wheel', wheel, { passive: false })
    return () => root.removeEventListener('wheel', wheel)
  }, [mounted])

  if (!mounted) return null
  return <div ref={shell} data-testid="filter-rail" className={`sticky shrink-0 self-start overflow-hidden ${resizing ? '' : 'transition-[width,margin,opacity] ease-out motion-reduce:transition-none'}`}
    style={{ top: STICKY_TOP, width: expanded ? width + GAP : 0, marginRight: -GAP, opacity: expanded ? 1 : 0, transitionDuration: `${RAIL_MOTION_MS}ms` }}
    // Closing: out of the tab order and the accessibility tree at once.
    {...(open ? {} : { inert: true, 'aria-hidden': true })}>
    {/* Fixed width inside, so the content is clipped while it animates, never reflowed. */}
    <div className="relative" style={{ width }}>
      <RailAside asideRef={aside} maxHeight={maxHeight}>{children}</RailAside>
      <RailResizer width={width} onResize={onResize} onDrag={setResizing} />
    </div>
  </div>
}

function RailAside({ asideRef, maxHeight, children }: { asideRef: React.RefObject<HTMLElement | null>; maxHeight?: number; children: ReactNode }) {
  const { t } = useTranslation()
  return <aside ref={asideRef as React.RefObject<HTMLElement>} data-scroll="y" className="overflow-y-auto overscroll-contain pb-4 pr-2.5 pt-1" style={{ maxHeight: maxHeight ?? 'calc(100dvh - 6rem)' }} aria-label={t('memoryPage.filters')}>{children}</aside>
}

/** The rail's right edge: drag, or arrow keys, to set its width. */
function RailResizer({ width, onResize, onDrag }: { width: number; onResize: (width: number, save?: boolean) => void; onDrag: (on: boolean) => void }) {
  const { t } = useTranslation()
  const start = useRef<{ x: number; width: number } | null>(null)
  return <div role="separator" aria-orientation="vertical" aria-label={t('memoryPage.filters_width')} aria-valuemin={RAIL_MIN} aria-valuemax={RAIL_MAX} aria-valuenow={width} tabIndex={0}
    className="group absolute -right-4 top-0 flex h-full w-3 cursor-col-resize touch-none justify-center focus-ring"
    onPointerDown={event => { start.current = { x: event.clientX, width }; onDrag(true); event.currentTarget.setPointerCapture(event.pointerId) }}
    onPointerMove={event => { if (start.current) onResize(start.current.width + event.clientX - start.current.x) }}
    onPointerUp={event => { if (start.current) onResize(start.current.width + event.clientX - start.current.x, true); start.current = null; onDrag(false) }}
    onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); onResize(width + (event.key === 'ArrowRight' ? 16 : -16), true) } }}>
    <span className="h-full w-px bg-transparent transition-colors group-hover:bg-border-strong group-focus-visible:bg-accent" />
  </div>
}
