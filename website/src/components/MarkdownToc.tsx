import { memo, useState, useCallback, useEffect, useRef } from 'react'

import { i18nT } from '../i18n/t'
export interface TocEntry { level: number; text: string; index: number }

/** Extract TOC entries from rendered DOM headings — guarantees consistency with what the user sees */
export function extractHeadingsFromDOM(container: HTMLElement | null): TocEntry[] {
  if (!container) return []
  const headings = container.querySelectorAll('h1, h2, h3, h4, h5, h6')
  const entries: TocEntry[] = []
  headings.forEach((h, i) => {
    const text = h.textContent?.trim() || ''
    if (!text) return
    entries.push({ level: parseInt(h.tagName[1], 10), text, index: i })
  })
  return entries
}

const easeInOutCubic = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

/**
 * Smoothly scroll `container` to `targetTop` with a distance-proportional
 * duration that is CLAMPED — short hops feel snappy, huge jumps don't drag on
 * for seconds. Native `scrollIntoView({behavior:'smooth'})` hands timing to the
 * browser (uncapped, inconsistent across engines), so we run our own rAF loop
 * instead. Honors prefers-reduced-motion (instant), bails if the user grabs the
 * scroll mid-flight, and calls `onDone` exactly once. Returns a cancel fn.
 */
function animateScrollTo(container: HTMLElement, targetTop: number, onDone?: () => void): () => void {
  const start = container.scrollTop
  const max = container.scrollHeight - container.clientHeight
  const dest = Math.max(0, Math.min(targetTop, max))
  const delta = dest - start
  const dist = Math.abs(delta)
  const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  if (reduced || dist < 4) { container.scrollTop = dest; onDone?.(); return () => {} }
  // ~0.4ms/px, floored at 160ms (snappy for near hops) and capped at 520ms so
  // a jump across a huge document never feels sluggish.
  const duration = Math.min(520, Math.max(160, dist * 0.4))
  let startTime = 0
  let lastSet = start
  let cancelled = false
  let raf = 0
  const step = (now: number) => {
    if (cancelled) return
    // If the actual position drifted from what we last set, the user took over
    // (wheel / trackpad / keyboard) — stop fighting them.
    if (startTime && Math.abs(container.scrollTop - lastSet) > 2) { onDone?.(); return }
    if (!startTime) startTime = now
    const t = Math.min(1, (now - startTime) / duration)
    container.scrollTop = start + delta * easeInOutCubic(t)
    lastSet = container.scrollTop
    if (t < 1) raf = requestAnimationFrame(step)
    else onDone?.()
  }
  raf = requestAnimationFrame(step)
  return () => { cancelled = true; cancelAnimationFrame(raf) }
}

// Tick width tapers by heading depth (h1 widest, deeper headings shorter).
const TICK_WIDTH = [16, 11, 8, 6, 5]
const tickWidth = (depth: number) => TICK_WIDTH[Math.min(depth, TICK_WIDTH.length - 1)]

/**
 * Document-style outline rail. A minimal column of tick marks is always pinned to
 * the right edge of the scroll container and floats *over* the content (never
 * reflows it). Hover or focus expands it into a labeled flyout. The currently
 * scrolled section is tracked with an IntersectionObserver; click a tick or row
 * to scroll there.
 *
 * When a document has more headings than fit vertically, the collapsed ticks
 * become a "follow window" synced to document scroll: the top of the document
 * shows the first headings, the bottom shows the last — no separate scrollbar.
 *
 * Anchors to `containerRef` — the scrollable viewport — so it must be rendered
 * inside a `position: relative` ancestor that shares that viewport's box.
 */
const MarkdownOutlineRail = memo(function MarkdownOutlineRail({ containerRef }: { containerRef: React.RefObject<HTMLElement | null> }) {
  const [entries, setEntries] = useState<TocEntry[]>([])
  const [active, setActive] = useState(0)
  const [expanded, setExpanded] = useState(false)
  // Which entry currently has keyboard focus (-1 = none / mouse-driven). Mirrored
  // into the flyout as a distinct highlight so a keyboard user can see where Enter
  // will jump — separate from `active`, which marks the current scroll position.
  const [focused, setFocused] = useState(-1)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const clipRef = useRef<HTMLDivElement>(null)
  const stripRef = useRef<HTMLDivElement>(null)
  const flyoutRef = useRef<HTMLDivElement>(null)
  // While a click-initiated scroll is animating, freeze the active index so the
  // IntersectionObserver doesn't light up every heading the viewport passes
  // through on the way to the target. Our animation releases it deterministically.
  const scrollLock = useRef(false)
  const animCancel = useRef<(() => void) | undefined>(undefined)
  // Latest active index for the scroll-sync loop to read without re-subscribing.
  const activeRef = useRef(0)
  activeRef.current = active

  // Re-extract headings whenever the rendered markdown changes.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const extract = () => {
      const next = extractHeadingsFromDOM(container)
      setEntries(prev => (prev.length === next.length && prev.every((e, i) => e.text === next[i].text && e.level === next[i].level && e.index === next[i].index)) ? prev : next)
    }
    extract()
    const mo = new MutationObserver(extract)
    mo.observe(container, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [containerRef])

  // Track the active section while scrolling.
  useEffect(() => {
    const container = containerRef.current
    if (!container || entries.length === 0) return
    const headings = container.querySelectorAll('h1, h2, h3, h4, h5, h6')
    if (headings.length === 0) return
    // Whether the viewport is scrolled to (within 2px of) the bottom. Guarded
    // on actual scrollability: for a short doc that fits without a scrollbar
    // (scrollHeight === clientHeight) the raw distance is 0, which would
    // wrongly read as "at bottom" — so require a real overflow first. There the
    // observer's top-20% band owns active-tracking and this stays false.
    const atBottom = () => {
      const max = container.scrollHeight - container.clientHeight
      return max > 2 && container.scrollTop >= max - 2
    }
    const observer = new IntersectionObserver((obs) => {
      if (scrollLock.current) return // a click jump is animating — don't fight it
      // At the very bottom the last heading can never reach the top-20% active
      // band (its trailing content is shorter than the -80% rootMargin), so the
      // observer would leave the final tick perpetually unlit. The scroll
      // listener below owns that case — don't let a stale intersection fight it.
      if (atBottom()) return
      for (const e of obs) {
        if (e.isIntersecting) {
          const domIdx = Array.from(headings).indexOf(e.target as Element)
          const entryIdx = entries.findIndex(en => en.index === domIdx)
          if (entryIdx >= 0) setActive(entryIdx)
          break
        }
      }
    }, { root: container, rootMargin: '0px 0px -80% 0px', threshold: 0 })
    headings.forEach(h => observer.observe(h))
    // Scroll-end fallback: when the viewport is scrolled to the bottom, force
    // the last heading active. Without this, any heading whose following
    // content is shorter than 80% of the viewport height (most commonly the
    // final heading) never enters the observer's top-20% band and its tick
    // stays dark even though the user has reached it.
    const onScrollEnd = () => {
      if (scrollLock.current) return
      if (atBottom()) setActive(entries.length - 1)
    }
    container.addEventListener('scroll', onScrollEnd, { passive: true })
    onScrollEnd() // catch the case where the doc loads already scrolled to the end
    return () => { observer.disconnect(); container.removeEventListener('scroll', onScrollEnd) }
  }, [containerRef, entries])

  // Position the collapsed tick column. When it fits, center it. When it
  // overflows, translate it as a window that follows document scroll — BUT the
  // active tick is never allowed to leave the visible band. Headings aren't
  // evenly distributed (a heading with lots of text below it spans more scroll
  // distance than one with little), so a pure scroll-fraction sync can push the
  // active tick off-screen. Priority: (1) keep the active tick in view,
  // (2) otherwise stay as close to the sync position as possible.
  const positionStrip = useCallback(() => {
    const container = containerRef.current, clip = clipRef.current, strip = stripRef.current
    if (!container || !clip || !strip) return
    const availH = clip.clientHeight
    const stripH = strip.scrollHeight
    if (stripH <= availH) { strip.style.transform = `translateY(${(availH - stripH) / 2}px)`; return }
    const overflow = stripH - availH
    // Desired offset from pure scroll-fraction sync (positive = strip scrolled up).
    const max = container.scrollHeight - container.clientHeight
    const frac = max > 0 ? Math.min(1, Math.max(0, container.scrollTop / max)) : 0
    let offset = frac * overflow
    // Clamp so the active tick stays within a comfortable margin of the band.
    const activeEl = strip.children[activeRef.current] as HTMLElement | undefined
    if (activeEl) {
      const margin = Math.min(24, availH / 4)
      offset = Math.min(offset, activeEl.offsetTop - margin) // larger would hide the tick's top
      offset = Math.max(offset, activeEl.offsetTop + activeEl.offsetHeight - (availH - margin)) // smaller would hide its bottom
    }
    offset = Math.min(overflow, Math.max(0, offset))
    strip.style.transform = `translateY(${-offset}px)`
  }, [containerRef])

  // Reposition on document scroll (rAF-throttled so a 500-heading doc never
  // re-renders the list) and on resize.
  useEffect(() => {
    const container = containerRef.current, clip = clipRef.current, strip = stripRef.current
    if (!container || !clip || !strip) return
    let raf = 0
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; positionStrip() }) }
    positionStrip()
    container.addEventListener('scroll', onScroll, { passive: true })
    const ro = new ResizeObserver(positionStrip)
    ro.observe(clip); ro.observe(strip)
    return () => { container.removeEventListener('scroll', onScroll); ro.disconnect(); cancelAnimationFrame(raf) }
  }, [containerRef, entries, positionStrip])

  // Reposition the instant the active tick changes (e.g. mid-scroll) so the
  // clamp pulls it back into view immediately, not only on the next scroll event.
  useEffect(() => { positionStrip() }, [active, positionStrip])

  // Keep the relevant row visible in the expanded flyout: the keyboard-focused
  // one when navigating by Tab, otherwise the active (current-scroll) one.
  useEffect(() => {
    if (!expanded) return
    const flyout = flyoutRef.current
    if (!flyout) return
    const sel = focused >= 0 ? `[data-focused="true"]` : `[data-active="true"]`
    flyout.querySelector<HTMLElement>(sel)?.scrollIntoView({ block: 'nearest' })
  }, [expanded, active, focused])

  useEffect(() => () => { clearTimeout(closeTimer.current); animCancel.current?.() }, [])

  const open = useCallback(() => { clearTimeout(closeTimer.current); setExpanded(true) }, [])
  // Small grace period on leave so a quick mouse path off a tick doesn't flicker.
  const scheduleClose = useCallback(() => { closeTimer.current = setTimeout(() => { setExpanded(false); setFocused(-1) }, 120) }, [])
  const jumpTo = useCallback((entry: TocEntry, i: number) => {
    const container = containerRef.current
    if (!container) return
    setActive(i)
    const el = container.querySelectorAll('h1, h2, h3, h4, h5, h6')[entry.index]
    if (!el) return
    const targetTop = container.scrollTop + (el.getBoundingClientRect().top - container.getBoundingClientRect().top) - 8
    scrollLock.current = true
    animCancel.current?.()
    animCancel.current = animateScrollTo(container, targetTop, () => { scrollLock.current = false })
  }, [containerRef])

  // Not worth the chrome for a single heading.
  if (entries.length < 2) return null
  const minLevel = Math.min(...entries.map(e => e.level))

  return (
    // Wrapper spans the viewport height but is click-through (pointer-events-none);
    // only the tick column and the expanded flyout opt back into pointer events,
    // so the rail never steals clicks/selection from the document underneath.
    // <nav> is the correct landmark for this TOC rail. The hover handlers only
    // reveal the flyout and are mirrored by onFocusCapture/onBlurCapture for
    // keyboard users, so the pointer listeners add no keyboard-only behavior.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <nav
      aria-label={i18nT('components.markdownToc.table_of_contents')}
      className="absolute inset-y-0 right-0 z-20 pointer-events-none select-none"
      onMouseEnter={open}
      onMouseLeave={scheduleClose}
      onFocusCapture={open}
      onBlurCapture={scheduleClose}
    >
      {/* Collapsed: tick marks, clipped to the viewport height and translated to
          follow document scroll when they overflow. Fade out as the flyout fades in.
          MUST drop pointer-events when expanded: this clip overlaps the flyout's
          right-edge scrollbar and its `will-change:transform` strip composites it
          above the flyout in hit-testing — so while visible it would swallow wheel
          events over the scrollbar (it's overflow-hidden, so nothing scrolls).
          NOT aria-hidden: the ticks are the always-available accessible TOC (each
          carries an aria-label) and stay focusable in both states — focusable
          elements inside an aria-hidden subtree are a WCAG violation. The flyout is
          a decorative visual mirror and is aria-hidden instead. */}
      <div
        ref={clipRef}
        className={`absolute inset-y-0 right-3 overflow-hidden transition-opacity duration-150 ${expanded ? 'opacity-0 pointer-events-none' : 'opacity-100 pointer-events-auto'}`}
      >
        <div ref={stripRef} className="flex flex-col items-end gap-[6px] will-change-transform">
          {entries.map((entry, i) => (
            <button
              key={entry.index}
              type="button"
              className="group flex items-center h-[6px] cursor-pointer bg-transparent border-none p-0"
              onClick={() => jumpTo(entry, i)}
              onFocus={() => setFocused(i)}
              title={entry.text}
              aria-label={entry.text}
            >
              <span
                className="block h-[2px] rounded-full transition-all duration-150 group-hover:!bg-[var(--accent)]"
                style={{ width: tickWidth(entry.level - minLevel), background: active === i ? 'var(--accent)' : 'var(--border-strong)' }}
              />
            </button>
          ))}
        </div>
      </div>

      {/* Expanded: labeled flyout — a decorative visual mirror of the ticks (which
          already carry aria-labels), so it stays aria-hidden to avoid announcing the
          TOC twice. Rows are mouse-only (tabIndex={-1}); keyboard users drive the same
          `focused` highlight via the focusable ticks behind it. */}
      <div
        ref={flyoutRef}
        className={`${expanded ? 'pointer-events-auto opacity-100 translate-x-0' : 'opacity-0 translate-x-2'} absolute top-1/2 right-2 -translate-y-1/2 w-[230px] max-h-[80%] overflow-y-auto scrollbar-overlay rounded-lg border border-border bg-bg-elevated shadow-xl py-1.5 transition-all duration-150`}
        aria-hidden
      >
        <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">{i18nT('components.markdownToc.contents')}</div>
        {entries.map((entry, i) => (
          <button
            key={entry.index}
            type="button"
            tabIndex={-1}
            data-active={active === i}
            data-focused={focused === i}
            className={`w-full text-left text-[12px] leading-tight pr-2 py-1.5 rounded-r cursor-pointer transition-colors truncate border-l-2 ${active === i ? 'border-[var(--accent)] text-accent font-medium' : 'border-transparent text-muted hover:text-text'} ${focused === i ? 'bg-bg-hover ring-1 ring-inset ring-[var(--accent)]' : 'bg-transparent hover:bg-bg-hover'}`}
            style={{ paddingLeft: `${(entry.level - minLevel) * 12 + 12}px` }}
            onClick={() => jumpTo(entry, i)}
            title={entry.text}
          >
            {entry.text}
          </button>
        ))}
      </div>
    </nav>
  )
})

export default MarkdownOutlineRail
