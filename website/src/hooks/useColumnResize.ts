// Drag-to-resize behaviour shared by the dashboard's fixed-width workspace
// columns (Issue Radar's left rail and issue / PR lists, Task Runner's run
// rail, the Webhooks rail). Every column behaves the same way — the handle sits
// on its right edge,
// dragging right widens it, the width is clamped while dragging and persisted to
// localStorage on release — so the logic lives here once instead of being
// duplicated per column.
//
// A column may also opt into COLLAPSING (the rails do): drag far enough
// past the minimum and the column snaps to a narrow icon strip instead of
// stopping dead at the minimum. Expanding again requires dragging a comparable
// distance back out, so the snap has hysteresis and doesn't flicker around the
// boundary.
import { useCallback, useEffect, useRef, useState } from 'react'
import { usePointerDrag } from './usePointerDrag'

export interface CollapseConfig {
  /** Width of the collapsed strip, in px. */
  width: number
  /** Where the collapsed flag is persisted. */
  storageKey: string
  /** Overshoot past `min` (to collapse) / past `width` (to expand) required
   *  before the snap fires. */
  slop?: number
}

const DEFAULT_SLOP = 48

export interface ColumnResize {
  /** Current width in px: inside [min, max], or the collapsed width. */
  width: number
  /** True while the column is showing its collapsed strip. */
  collapsed: boolean
  /** True only while a resize drag is in flight. Consumers use it to switch off
   *  layout animations that would otherwise scale-distort their content while
   *  the column width changes on every pointer move. */
  dragging: boolean
  /** Re-open a collapsed column at the width the user last dragged it to. */
  expand: () => void
  /** Move the column by `dx` px and persist, clamping / collapsing the same way
   *  a drag of that distance would. Drives the handle's arrow keys. */
  nudge: (dx: number) => void
  /** Collapse to the icon strip without a drag — e.g. on a narrow viewport,
   *  where a fixed-width column would squeeze the pane beside it. No-op when the
   *  column was created without a collapse config. */
  collapse: () => void
  /** Spread onto the drag handle element (see components/ResizeHandle). */
  handleProps: ReturnType<typeof usePointerDrag>
}

export function useColumnResize(
  storageKey: string,
  load: () => number,
  min: number,
  max: number,
  collapse?: CollapseConfig,
  loadCollapsed?: () => boolean,
): ColumnResize {
  // The stored width is always an EXPANDED width, so collapsing and reopening
  // restores the user's chosen size rather than the default.
  const [openWidth, setOpenWidth] = useState<number>(load)
  const [collapsed, setCollapsed] = useState<boolean>(() => !!collapse && !!loadCollapsed?.())
  const [dragging, setDragging] = useState(false)
  const width = collapse && collapsed ? collapse.width : openWidth

  const startWRef = useRef(0)
  const startCollapsedRef = useRef(false)
  const startOpenRef = useRef(0)
  const draggingRef = useRef(false)
  // usePointerDrag reads its options through a ref, but the resolver needs the
  // live values at pointer-down; keep them in refs so onStart never captures a
  // stale render.
  const liveRef = useRef({ width, collapsed, openWidth })
  liveRef.current = { width, collapsed, openWidth }

  /** Where the column lands for a drag delta of `dx`, as the OPEN width plus the
   *  collapsed flag. Pure in (dx, drag start), so onMove and onEnd can never
   *  disagree about the result. */
  const resolve = useCallback((dx: number): { openWidth: number, collapsed: boolean } => {
    const raw = startWRef.current + dx
    const clamp = (v: number) => Math.min(max, Math.max(min, v))
    if (!collapse) return { openWidth: clamp(raw), collapsed: false }
    const slop = collapse.slop ?? DEFAULT_SLOP
    if (startCollapsedRef.current) {
      // Collapsed: stay collapsed until the pointer has pulled clearly outwards,
      // then reopen AT the remembered width and grow from there. Resolving to
      // the raw pointer position instead would reopen at the clamped minimum and
      // bank it, losing the width the user had before collapsing.
      const overshoot = raw - (collapse.width + slop)
      return overshoot < 0
        ? { openWidth: startOpenRef.current, collapsed: true }
        : { openWidth: clamp(startOpenRef.current + overshoot), collapsed: false }
    }
    // A drag that ends collapsed must not bank the widths it swept through on
    // the way in: dragging a 400px rail slowly through the 220px minimum would
    // otherwise leave 220 as the remembered width, so reopening lost the 400.
    return raw <= min - slop
      ? { openWidth: startOpenRef.current, collapsed: true }
      : { openWidth: clamp(raw), collapsed: false }
  }, [min, max, collapse])

  const apply = useCallback((dx: number) => {
    const next = resolve(dx)
    setCollapsed(next.collapsed)
    setOpenWidth(next.openWidth)
    return next
  }, [resolve])

  const persist = useCallback((next: { openWidth: number, collapsed: boolean }) => {
    try {
      // Always the OPEN width — the collapsed strip width is never a column width.
      localStorage.setItem(storageKey, String(next.openWidth))
      if (collapse) localStorage.setItem(collapse.storageKey, next.collapsed ? '1' : '0')
    } catch {
      /* storage blocked or full — the layout still applies for this session */
    }
  }, [storageKey, collapse])

  const handleProps = usePointerDrag({
    threshold: 0,
    onStart: () => {
      startWRef.current = liveRef.current.width
      startCollapsedRef.current = liveRef.current.collapsed
      startOpenRef.current = liveRef.current.openWidth
      draggingRef.current = true
      setDragging(true)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    onMove: ({ dx }) => { apply(dx) },
    onEnd: ({ dx }) => {
      draggingRef.current = false
      setDragging(false)
      persist(apply(dx))
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    },
  })

  // Unmount guard: onEnd can't fire if the component unmounts mid-drag
  // (setPointerCapture dies with the element), so restore the global body
  // styles here to avoid leaving the resize cursor / text-selection lock stuck.
  useEffect(() => () => {
    if (draggingRef.current) {
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])

  const expand = useCallback(() => {
    setCollapsed(false)
    persist({ openWidth: liveRef.current.openWidth, collapsed: false })
  }, [persist])

  /** Collapse to the icon strip, keeping the remembered open width.
   *
   *  Symmetric with `expand`, for consumers that need to collapse for a reason
   *  other than a drag — a narrow viewport, where a fixed-width rail beside a
   *  detail pane would leave the pane too narrow to use. Named apart from the
   *  `collapse` CONFIG parameter above, which it reads.  */
  const collapseColumn = useCallback(() => {
    if (!collapse) return
    setCollapsed(true)
    persist({ openWidth: liveRef.current.openWidth, collapsed: true })
  }, [persist, collapse])

  // Keyboard counterpart to the drag. A pointer-only handle leaves keyboard
  // users with no way to resize — or, once the rail can collapse, no way back
  // out of a collapsed rail except the strip's own expand button. Steps are
  // resolved from the CURRENT width rather than a drag origin, so repeated
  // presses accumulate the way held-arrow behaviour is expected to.
  const nudge = useCallback((dx: number) => {
    const live = liveRef.current
    const clamp = (v: number) => Math.min(max, Math.max(min, v))
    // Collapsed and growing: reopen at the remembered width instead of the
    // minimum, mirroring how the drag resolver treats an outward pull.
    if (collapse && live.collapsed) {
      if (dx <= 0) return
      const next = { openWidth: clamp(live.openWidth), collapsed: false }
      setCollapsed(false)
      setOpenWidth(next.openWidth)
      persist(next)
      return
    }
    const raw = live.openWidth + dx
    // Shrinking past the minimum collapses, matching the drag's snap rather
    // than stopping dead at a wall the keyboard can never get past.
    const next = collapse && raw < min
      ? { openWidth: live.openWidth, collapsed: true }
      : { openWidth: clamp(raw), collapsed: false }
    setCollapsed(next.collapsed)
    setOpenWidth(next.openWidth)
    persist(next)
  }, [min, max, collapse, persist])

  return {
    width, collapsed: !!collapse && collapsed, dragging, expand, nudge,
    collapse: collapseColumn, handleProps,
  }
}
