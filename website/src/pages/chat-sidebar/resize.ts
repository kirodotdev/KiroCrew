/** The sidebar's persisted width and its resize handle (pointer drag and arrow keys). */
import { useState, useRef, useEffect, useCallback } from 'react'
import { SIDEBAR_MIN, SIDEBAR_MAX } from '../chat/sidebarWidth'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { safeSetItem } from '../../utils/safeStorage'
import { SIDEBAR_LS_KEY, SIDEBAR_PRE_BOARD_LS_KEY } from './persistence'

/** The persisted sidebar width and its drag and keyboard resize. */
export function useSidebarResize({ onWidthChange, onDragChange }: {
  onWidthChange: ((w: number) => void) | undefined
  onDragChange: ((dragging: boolean) => void) | undefined
}) {
  // Sidebar width (self-managed, reported to parent)
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = localStorage.getItem(SIDEBAR_LS_KEY)
    const n = saved ? parseInt(saved, 10) : NaN
    return !isNaN(n) && n >= SIDEBAR_MIN && n <= SIDEBAR_MAX ? n : 260
  })
  // Resize logic — Pointer Events (mouse + touch + pen) via usePointerDrag, so
  // the handle works on touch devices too, e.g. a tablet at desktop width where
  // the sidebar is a side-by-side panel (the mouse-only handler ignored touch).
  // setPointerCapture keeps move/up firing when the pointer leaves the thin
  // handle, replacing the old window-level mousemove/mouseup listeners.
  const sidebarStartW = useRef(0)
  const sidebarDraggingRef = useRef(false)
  const sidebarWidthRef = useRef(sidebarWidth)
  sidebarWidthRef.current = sidebarWidth
  const onWidthChangeRef = useRef(onWidthChange)
  onWidthChangeRef.current = onWidthChange
  const onDragChangeRef = useRef(onDragChange)
  onDragChangeRef.current = onDragChange
  useEffect(() => { onWidthChangeRef.current?.(sidebarWidth) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // threshold 0: a dedicated edge affordance resizes immediately on press (no
  // 10px hysteresis), matching the original mouse resizer's feel.
  const sidebarResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      sidebarStartW.current = sidebarWidthRef.current
      sidebarDraggingRef.current = true
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      onDragChangeRef.current?.(true)
    },
    onMove: ({ dx }) => {
      const newW = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, sidebarStartW.current + dx))
      setSidebarWidth(newW)
      onWidthChangeRef.current?.(newW)
    },
    onEnd: () => {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
      const w = sidebarWidthRef.current
      safeSetItem(SIDEBAR_LS_KEY, String(w))
      onWidthChangeRef.current?.(w)
    },
  })
  // Arrow-key resize for the shared handle: the same clamp a drag applies,
  // persisted at once since a key press has no "release" to persist on.
  const nudgeSidebar = useCallback((dx: number) => {
    const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, sidebarWidthRef.current + dx))
    setSidebarWidth(w)
    safeSetItem(SIDEBAR_LS_KEY, String(w))
    onWidthChangeRef.current?.(w)
  }, [])
  /** Widen for a board's lanes, remembering what the user had so leaving board view
   *  can give it back. Persisting the automatic width without that destroys their
   *  chosen width permanently and strands a ~900px sidebar in list view. */
  const widenForBoard = useCallback((next: number) => {
    safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, String(sidebarWidthRef.current))
    setSidebarWidth(next)
    onWidthChangeRef.current?.(next)
    safeSetItem(SIDEBAR_LS_KEY, String(next))
  }, [])
  /** Leaving board view: give back the width the user chose before the lanes were
   *  auto-widened, rather than stranding a ~900px sidebar in list view. */
  const restorePreBoardWidth = useCallback(() => {
    const prior = parseInt(localStorage.getItem(SIDEBAR_PRE_BOARD_LS_KEY) || '', 10)
    if (!isNaN(prior) && prior >= SIDEBAR_MIN && prior <= SIDEBAR_MAX) {
      setSidebarWidth(prior)
      onWidthChangeRef.current?.(prior)
      safeSetItem(SIDEBAR_LS_KEY, String(prior))
      safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, '')
    }
  }, [])

  // Unmount guard: if the sidebar unmounts mid-drag (collapse / route change),
  // onEnd never fires — setPointerCapture dies with the element — so the global
  // body styles and the parent's dragging state would stay stuck. Restore them
  // on teardown. The old mouse-only handler did this in its listener cleanup;
  // the pointer migration must preserve it.
  useEffect(() => () => {
    if (sidebarDraggingRef.current) {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
    }
  }, [])
  return { sidebarWidth, sidebarWidthRef, sidebarResize, nudgeSidebar, widenForBoard, restorePreBoardWidth }
}
