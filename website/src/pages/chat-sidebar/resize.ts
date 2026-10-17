/** The sidebar's persisted width and its resize handle (pointer drag and arrow keys). */
import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { SIDEBAR_MIN, SIDEBAR_MAX, sidebarRoomWidth, parseStoredSidebarWidth, sidebarPaintWidth } from '../chat/sidebarWidth'
import { CHAT_PANE_MIN_W } from '../chat/SidePanel'
import { useRailWidth } from '../../hooks/useRailWidth'
import { useWindowWidth } from '../../hooks/useWindowWidth'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { safeSetItem } from '../../utils/safeStorage'
import { SIDEBAR_LS_KEY, SIDEBAR_PRE_BOARD_LS_KEY } from './persistence'

/** The drag ceiling: the width the root can paint at (sidebarRoomWidth), so a
 *  drag never ends past what it shows, and in the list views also SIDEBAR_MAX,
 *  since they gain nothing from a wider sidebar. `winW` is the same window
 *  width the paint uses. */
const sidebarCeiling = (boardActive: boolean, winW: number, railW: number, chatMin: number) => {
  const room = sidebarRoomWidth({ winW, railW, chatMin })
  return boardActive ? room : Math.min(SIDEBAR_MAX, room)
}

/** The persisted sidebar width and its drag and keyboard resize. */
export function useSidebarResize({ onWidthChange, onDragChange, fillsHost = false, boardActive }: {
  /** Told the width the root paints at, which the host seats it at. */
  onWidthChange: ((w: number) => void) | undefined
  onDragChange: ((dragging: boolean) => void) | undefined
  /** The host stretches the sidebar to its own width (the mobile drawer, the
   *  sessions embed), so no chat pane sits beside it to reserve room for. */
  fillsHost?: boolean
  /** Board view is showing. Only the board may grow past SIDEBAR_MAX. */
  boardActive: boolean
}) {
  // The drag ceiling follows the live window and nav rail, read at call time
  // (a ref, not a closure) so a resize mid-session widens or narrows it.
  const railWidth = useRailWidth()
  // The same live window width ChatPage sizes the drawer with (useWindowWidth).
  const winW = useWindowWidth()
  const railWidthRef = useRef(railWidth)
  railWidthRef.current = railWidth
  const boardActiveRef = useRef(boardActive)
  boardActiveRef.current = boardActive
  const winWRef = useRef(winW)
  winWRef.current = winW
  // Every view beside a chat pane leaves it its minimum, so the list views
  // and board view share one paint rule on a narrow window.
  const chatMin = fillsHost ? 0 : CHAT_PANE_MIN_W
  const chatMinRef = useRef(chatMin)
  chatMinRef.current = chatMin
  const ceilingNow = useCallback(
    () => sidebarCeiling(boardActiveRef.current, winWRef.current, railWidthRef.current, chatMinRef.current), [])
  // Sidebar width (self-managed). The saved width is kept as saved (see
  // parseStoredSidebarWidth); the paint below holds it to this view and this
  // window, so going back to board view or widening the window restores it.
  const [sidebarWidth, setSidebarWidth] = useState(() =>
    parseStoredSidebarWidth(localStorage.getItem(SIDEBAR_LS_KEY)) ?? 260)
  // The list views paint a wider saved width at SIDEBAR_MAX, leaving the
  // saved width itself for board view.
  const seatWidth = boardActive ? sidebarWidth : Math.min(sidebarWidth, SIDEBAR_MAX)
  // The width the root is painted at (see sidebarPaintWidth): a window narrowed
  // mid-session after a wide drag must not leave the root, and the resize handle
  // on its right edge, far outside the drawer box that clips it. The stored
  // preference itself is kept for a wider window, as ChatPage keeps it.
  const paintedSidebarWidth = sidebarPaintWidth({
    stored: seatWidth, winW, railW: railWidth, chatMin,
  })
  // The root's inline style. A switch between list and board view repaints at
  // once, as main's board auto-widen and its restore already do.
  const rootStyle = { width: paintedSidebarWidth }
  // Resize logic — Pointer Events (mouse + touch + pen) via usePointerDrag, so
  // the handle works on touch devices too, e.g. a tablet at desktop width where
  // the sidebar is a side-by-side panel (the mouse-only handler ignored touch).
  // setPointerCapture keeps move/up firing when the pointer leaves the thin
  // handle, replacing the old window-level mousemove/mouseup listeners.
  const sidebarStartW = useRef(0)
  // The stored preference when the drag began, restored if the drag ends where
  // it started (see commitResize).
  const sidebarStoredAtStart = useRef(0)
  const sidebarDraggingRef = useRef(false)
  const sidebarWidthRef = useRef(sidebarWidth)
  sidebarWidthRef.current = sidebarWidth
  // A drag or arrow key starts from the PAINTED width, what the user sees, not
  // from a wider stored preference the window is currently clipping.
  const paintedWidthRef = useRef(paintedSidebarWidth)
  paintedWidthRef.current = paintedSidebarWidth
  const onWidthChangeRef = useRef(onWidthChange)
  onWidthChangeRef.current = onWidthChange
  const onDragChangeRef = useRef(onDragChange)
  onDragChangeRef.current = onDragChange
  // The host seats the sidebar from this report, so it follows every change,
  // a switch between list and board view or a window resize included, before
  // the frame paints.
  useLayoutEffect(() => { onWidthChangeRef.current?.(paintedSidebarWidth) }, [paintedSidebarWidth])
  // The one place a drag or nudge saves. A resize that ends on the painted width
  // it began from changed nothing the user can see (a press with no travel, or
  // pushing against the ceiling), so the stored preference stands: it may be a
  // wider width this view or window only clips. Any other end saves.
  const commitResize = useCallback((w: number, fromPainted: number, storedBefore: number) => {
    if (w === fromPainted) {
      setSidebarWidth(storedBefore)
      return
    }
    setSidebarWidth(w)
    safeSetItem(SIDEBAR_LS_KEY, String(w))
  }, [])

  // threshold 0: a dedicated edge affordance resizes immediately on press (no
  // 10px hysteresis), matching the original mouse resizer's feel.
  const sidebarResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      sidebarStartW.current = paintedWidthRef.current
      sidebarStoredAtStart.current = sidebarWidthRef.current
      sidebarDraggingRef.current = true
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      onDragChangeRef.current?.(true)
    },
    onMove: ({ dx }) => {
      // A move with no horizontal travel (the zero-delta move threshold 0 fires
      // on press, or a pen's pressure or vertical jitter) lands on the start
      // width, so commitResize keeps the stored preference on release.
      const newW = Math.min(ceilingNow(), Math.max(SIDEBAR_MIN, sidebarStartW.current + dx))
      setSidebarWidth(newW)
    },
    onEnd: () => {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
      commitResize(sidebarWidthRef.current, sidebarStartW.current, sidebarStoredAtStart.current)
    },
  })
  // Arrow-key resize for the shared handle: the same clamp a drag applies,
  // committed at once since a key press has no "release" to commit on.
  const nudgeSidebar = useCallback((dx: number) => {
    const w = Math.min(ceilingNow(), Math.max(SIDEBAR_MIN, paintedWidthRef.current + dx))
    commitResize(w, paintedWidthRef.current, sidebarWidthRef.current)
  }, [commitResize, ceilingNow])
  /** Widen for a board's lanes, remembering what the user had so leaving board view
   *  can give it back. Persisting the automatic width without that destroys their
   *  chosen width permanently and strands a ~900px sidebar in list view. */
  const widenForBoard = useCallback((next: number) => {
    safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, String(sidebarWidthRef.current))
    setSidebarWidth(next)
    safeSetItem(SIDEBAR_LS_KEY, String(next))
  }, [])
  /** Leaving board view: give back the width the user chose before the lanes were
   *  auto-widened, rather than stranding a ~900px sidebar in list view. */
  const restorePreBoardWidth = useCallback(() => {
    const prior = parseStoredSidebarWidth(localStorage.getItem(SIDEBAR_PRE_BOARD_LS_KEY))
    if (prior !== null) {
      setSidebarWidth(prior)
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
  return { paintedSidebarWidth, rootStyle, sidebarMax: sidebarCeiling(boardActive, winW, railWidth, chatMin), paintedWidthRef, sidebarResize, nudgeSidebar, widenForBoard, restorePreBoardWidth }
}
