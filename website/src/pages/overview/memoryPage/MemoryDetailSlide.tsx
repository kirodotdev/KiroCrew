/**
 * The wide list's detail panel, sliding in from the right with a fade and
 * widening its column so the list beside it narrows smoothly; closing runs the
 * same motion back. Switching to another record while it is open crossfades the
 * content without sliding again. Reduced motion snaps.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { RAIL_MOTION_MS, prefersReducedMotion } from './MemoryRail'

const WIDTH = 360
const GAP = 24
const SHIFT = 16

export function MemoryDetailSlide({ contentKey, children }: { contentKey: string | null; children: ReactNode }) {
  const open = contentKey !== null
  // The last content stays on screen while the panel closes.
  const last = useRef<ReactNode>(children)
  if (open) last.current = children
  const [mounted, setMounted] = useState(open)
  const [expanded, setExpanded] = useState(open)
  const [faded, setFaded] = useState(false)
  const shownKey = useRef(contentKey)

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

  // Another record while open: fade the new content in, no slide.
  useEffect(() => {
    const before = shownKey.current
    shownKey.current = contentKey
    if (!open || before === null || before === contentKey || prefersReducedMotion()) return
    setFaded(true)
    const frame = requestAnimationFrame(() => setFaded(false))
    return () => cancelAnimationFrame(frame)
  }, [contentKey, open])

  if (!mounted) return null
  const motion = 'ease-out motion-reduce:transition-none'
  return <div data-testid="memory-detail-slide" className={`shrink-0 self-start overflow-hidden transition-[width,margin] ${motion}`}
    style={{ width: expanded ? WIDTH + GAP : 0, marginLeft: -GAP, transitionDuration: `${RAIL_MOTION_MS}ms` }}
    {...(open ? {} : { inert: true, 'aria-hidden': true })}>
    <div className={`ml-auto transition-[opacity,transform] ${motion}`} style={{ width: WIDTH, opacity: expanded && !faded ? 1 : 0, transform: expanded ? 'none' : `translateX(${SHIFT}px)`, transitionDuration: `${faded ? 0 : RAIL_MOTION_MS}ms` }}>
      {open ? children : last.current}
    </div>
  </div>
}
