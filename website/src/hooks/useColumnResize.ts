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
import { useIsMobile } from './useIsMobile'
import { usePointerDrag } from './usePointerDrag'

export interface CollapseConfig {
  /** Width of the collapsed strip, in px. */
  width: number
  /** Where the collapsed flag is persisted. */
  storageKey: string
  /** Overshoot past `min` (to collapse) / past `width` (to expand) required
   *  before the snap fires. */
  slop?: number
  /** Opt in to starting from the strip on a narrow viewport.
   *
   *  Deliberately opt-in, not automatic: the strip alone is only half the
   *  behaviour. A page also needs a drill-down for its expanded state (rail
   *  full width, detail stepped aside, collapse on select), or its expand
   *  button just leads back into the squeeze the strip was avoiding. Set it
   *  when the page implements that, not merely because it can collapse. */
  whenNarrow?: boolean
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
  const isMobile = useIsMobile()
  // A fixed-width rail cannot share a phone viewport with the pane beside it:
  // `flex-shrink-0` plus an inline width leaves that pane a ~124px column on a
  // 390px screen, which is not usable. So a collapsible column starts from its
  // strip on a narrow viewport, and the user can still open it from there.
  //
  // Held in state rather than written through `setCollapsed`, and deliberately
  // NOT persisted: the stored flag is a desktop layout preference, and a phone
  // visit must not come back as a collapsed rail on the desktop next time.
  const [openedWhileNarrow, setOpenedWhileNarrow] = useState(false)
  const narrowMode = isMobile && !!collapse?.whenNarrow
  // While narrow the effective state depends ONLY on the session override, never
  // on the stored flag. Or-ing the two would freeze the column: a user who
  // collapsed it on the desktop arrives with `collapsed` already true, and the
  // mobile paths deliberately do not touch that flag, so no affordance could
  // ever open it again.
  const collapsedEffective = !!collapse && (narrowMode ? !openedWhileNarrow : collapsed)
  const width = collapse && collapsedEffective ? collapse.width : openWidth

  // Leaving the narrow viewport drops the override, so returning to it starts
  // from the strip again instead of the width a previous phone visit opened.
  useEffect(() => {
    if (!narrowMode) setOpenedWhileNarrow(false)
  }, [narrowMode])

  const startWRef = useRef(0)
  const startCollapsedRef = useRef(false)
  const startOpenRef = useRef(0)
  const draggingRef = useRef(false)
  // usePointerDrag reads its options through a ref, but the resolver needs the
  // live values at pointer-down; keep them in refs so onStart never captures a
  // stale render.
  // Carries the EFFECTIVE collapsed flag, not the stored one: on a narrow
  // viewport the column renders as its strip, so a drag has to resolve from the
  // state the user can see or its first move would jump.
  const liveRef = useRef({ width, collapsed: collapsedEffective, openWidth })
  liveRef.current = { width, collapsed: collapsedEffective, openWidth }

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
    setOpenWidth(next.openWidth)
    if (narrowMode) {
      // Narrow: the open/closed state lives only in this session's override, so
      // a drag here can never touch the stored desktop flag.
      setOpenedWhileNarrow(!next.collapsed)
    } else {
      setCollapsed(next.collapsed)
    }
    return next
  }, [resolve, narrowMode])

  const persist = useCallback((next: { openWidth: number, collapsed: boolean }) => {
    try {
      // Always the OPEN width — the collapsed strip width is never a column width.
      localStorage.setItem(storageKey, String(next.openWidth))
      // The collapsed flag is a DESKTOP preference. While the viewport is narrow
      // the column's collapsed state is ephemeral (`openedWhileNarrow`), so
      // writing it here would let a phone visit — even a single tap on the drag
      // handle, which resolves to the strip it is already showing — come back as
      // a collapsed rail on the next desktop session.
      if (collapse && !narrowMode) localStorage.setItem(collapse.storageKey, next.collapsed ? '1' : '0')
    } catch {
      /* storage blocked or full — the layout still applies for this session */
    }
  }, [storageKey, collapse, narrowMode])

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
    // On a narrow viewport only the session override moves: the strip's expand
    // button must not rewrite the stored desktop flag.
    if (narrowMode) setOpenedWhileNarrow(true)
    else setCollapsed(false)
    persist({ openWidth: liveRef.current.openWidth, collapsed: false })
  }, [persist, narrowMode])

  /** Collapse to the icon strip, keeping the remembered open width.
   *
   *  Symmetric with `expand`, for consumers that need to collapse for a reason
   *  other than a drag — a narrow viewport, where a fixed-width rail beside a
   *  detail pane would leave the pane too narrow to use. Named apart from the
   *  `collapse` CONFIG parameter above, which it reads.  */
  const collapseColumn = useCallback(() => {
    if (!collapse) return
    if (narrowMode) setOpenedWhileNarrow(false)
    else setCollapsed(true)
    persist({ openWidth: liveRef.current.openWidth, collapsed: true })
  }, [persist, collapse, narrowMode])

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
      if (narrowMode) setOpenedWhileNarrow(true)
      else setCollapsed(false)
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
    if (narrowMode) setOpenedWhileNarrow(!next.collapsed)
    else setCollapsed(next.collapsed)
    setOpenWidth(next.openWidth)
    persist(next)
  }, [min, max, collapse, persist, narrowMode])

  return {
    width, collapsed: collapsedEffective, dragging, expand, nudge,
    collapse: collapseColumn, handleProps,
  }
}
