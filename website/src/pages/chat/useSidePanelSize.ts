/**
 * The chat side panel's size.
 *
 * The width constants and the fill-or-beside decision the panel's hosts share
 * (`SIDE_PANEL_MIN_W`, `SIDE_PANEL_RESERVED_W`, `CHAT_PANE_MIN_W`,
 * `sidePanelFillWidth`), the panel's own width resolution
 * (`sidePanelEffectiveWidth`), and `useSidePanelSize`, which owns the panel's
 * size state: the width and height remembered per chat (`sidePanelWidth.ts`),
 * the dock placement, the clamp to the window, the two resize gestures and the
 * eased edge on a chat switch (`sidePanelMount.ts`). `SidePanel.tsx` calls the
 * hook and re-exports the constants and the layout functions, which callers
 * import from there. This module never imports it.
 */
import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useRailWidth } from '../../hooks/useRailWidth'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { useSidePanelDock } from '../../hooks/useSidePanelDock'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { SIDE_PANEL_HEIGHT_KEY, SIDE_PANEL_WIDTH_KEY, loadSidePanelDim, ownDim, saveSidePanelDim } from './sidePanelWidth'
import { SIDE_PANEL_MOTION_MS, sidePanelDimTransition } from './sidePanelMount'

/** Panel minimum width (also the resize handle's lower clamp). */
export const SIDE_PANEL_MIN_W = 320
/**
 * Worst-case static budget for the space left of the panel: the nav rail at
 * its EXPANDED width plus a working chat-pane minimum. Used only for the
 * layout-mode gate on pages that decide beside-vs-overlay before the rail is
 * known (MembersPage.panelSitsBeside). The panel's own width ceiling does NOT
 * use it: that follows the live rail width (`useRailWidth`) plus
 * `CHAT_PANE_MIN_W`, so a collapsed rail hands its space to the panel.
 *
 * Deliberately not a measurement of the top bar either: the header spans all
 * three grid columns ('"topbar topbar topbar"'), so the panel sits UNDER it and
 * cannot shorten it.
 */
export const SIDE_PANEL_RESERVED_W = 560

/** Usable minimum for the chat pane itself, beside the panel. */
export const CHAT_PANE_MIN_W = 320

/**
 * Decide the panel's mode from the width left for the CHAT, not from the
 * viewport: subtract the shell's hideable chrome (nav rail track, session
 * sidebar) and ask whether the remainder seats the panel at SIDE_PANEL_MIN_W
 * beside a CHAT_PANE_MIN_W chat pane.
 *
 * Returns `undefined` for BESIDE mode, or the px width the panel should take to
 * FILL the chat column. Mobile always fills (its viewport cannot seat both, and
 * its sidebar is a fixed-position drawer that consumes no row width).
 *
 * Pure and loop-free on purpose: every input is a shell-level fact that does
 * NOT change when the panel opens. Feeding it the chat container's painted
 * width instead would oscillate, since opening the panel shrinks that width.
 */
export function sidePanelFillWidth(
  { winW, railW, sidebarW, isMobile }:
  { winW: number; railW: number; sidebarW: number; isMobile: boolean },
): number | undefined {
  if (isMobile) return Math.max(SIDE_PANEL_MIN_W, winW)
  const chatAvail = winW - railW - sidebarW
  if (chatAvail >= SIDE_PANEL_MIN_W + CHAT_PANE_MIN_W) return undefined
  return Math.max(SIDE_PANEL_MIN_W, chatAvail)
}

/**
 * Resolve the panel's rendered width.
 *
 * Order matters. FILL (an explicit px width) wins over the mobile percentage:
 * `width: 100%` is unreliable because the inline (non-portal) render path wraps
 * the panel in a shrink-to-fit `width: auto` flex item, and a percentage child
 * cannot resolve against an auto containing block during intrinsic sizing — the
 * browser falls back to the panel's own max-content width, so the panel comes
 * out narrow and the chat pane keeps the rest. An explicit px width is
 * deterministic in BOTH paths. '100%' survives only as the fallback for a
 * mobile frame that somehow receives no fillWidth.
 */
export function sidePanelEffectiveWidth(
  { fillWidth, isMobile, expanded, width, maxW }:
  { fillWidth?: number; isMobile: boolean; expanded?: boolean; width: number; maxW: number },
): number | string {
  if (fillWidth != null) return fillWidth
  if (isMobile) return '100%'
  if (expanded) return Math.max(SIDE_PANEL_MIN_W, maxW)
  return Math.max(SIDE_PANEL_MIN_W, Math.min(width, maxW))
}

/** The panel's size state, for `SidePanel`. Each input is the `SidePanel` prop
 *  of the same name, with its default applied (see `SidePanelProps`). */
export function useSidePanelSize({ slot, slotOwner, persistSlot, defaultWidth, extraReserveW, canDockBottom, expanded, fillWidth }: {
  slot: string
  slotOwner?: string
  persistSlot?: string
  defaultWidth: number
  extraReserveW: number
  canDockBottom: boolean
  expanded?: boolean
  fillWidth?: number
}) {
  // Resizable width (the actbar grid column is auto-sized, so the panel owns
  // its own width), remembered PER CHAT rather than once for the panel (see
  // sidePanelWidth.ts): the size follows `slot` and never moves on a tab switch
  // inside the chat. Held as a map keyed by slot so switching chats reads the
  // other chat's size without a round trip through storage, and a slot never
  // dragged in this session falls through to what is stored (`loadSidePanelDim`).
  const MIN_W = SIDE_PANEL_MIN_W
  // The slot a resize drag started on, held for the whole gesture and `null`
  // outside one. The host can re-key the panel mid-drag (the Members page
  // confirms its thread's slot when the POST answers, which flips `slot` from
  // '' to the key), and reading the live slot would retarget the remaining
  // movement and the release to the new chat and discard the adjustment the
  // user is making. State rather than a ref because the RENDERED size reads it
  // too: the size on screen and the size being written must be the same chat's,
  // or a mid-drag re-key shows one chat's width while the pointer moves another's.
  const [dragSlot, setDragSlot] = useState<string | null>(null)
  // The chat whose size is shown AND written: the dragged one during a gesture,
  // the shown one otherwise. One resolver for both, read by the render path
  // directly and by the drag callbacks through the latest-value ref.
  const dimSlot = dragSlot ?? slot
  const dimSlotRef = useRef(dimSlot); dimSlotRef.current = dimSlot
  const slotRef = useRef(slot); slotRef.current = slot
  // Where a released drag is saved: the chat it started on, with one exception.
  // A drag that started on an empty slot goes to the key confirmed mid-gesture
  // only when the host says it is the SAME chat receiving its key: `slotOwner`
  // unchanged from the start (the Members page confirming its thread). Saved on
  // the bare key alone, it would never become that chat's. Without that proof
  // the new key may be ANOTHER chat (a Ctrl+digit switch while the handle is
  // held), whose own size must not be overwritten, so the drag stays on ''.
  const ownerRef = useRef(slotOwner); ownerRef.current = slotOwner
  const dragOwnerRef = useRef<string | undefined>(undefined)
  // Either way, a host that confirms keys (`persistSlot`, the Members page) must
  // still confirm this one at the RELEASE: a stale key kept through a refusal
  // belongs to another session, so the drag stays on ''. Confirmation at the
  // start does not count, because a refusal that lands mid-drag must still win.
  // The cost is a release during a routine re-confirm, which saves only the
  // shared default. A host that passes no `persistSlot` (the chat page) owns
  // every key it passes, so nothing is gated.
  const persistRef = useRef(persistSlot); persistRef.current = persistSlot
  const releaseSlot = () => {
    const started = dimSlotRef.current
    const owner = dragOwnerRef.current
    const target = started || (owner && owner === ownerRef.current ? slotRef.current : '')
    return target && (persistRef.current === undefined || target === persistRef.current) ? target : ''
  }
  const [widthBySlot, setWidthBySlot] = useState<Record<string, number>>({})
  const width = useMemo(
    () => ownDim(widthBySlot, dimSlot) ?? loadSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, slot: dimSlot, min: MIN_W, fallback: defaultWidth }),
    [widthBySlot, dimSlot, MIN_W, defaultWidth],
  )
  const setWidth = useCallback((w: number) => {
    const target = dimSlotRef.current
    setWidthBySlot(m => ({ ...m, [target]: w }))
  }, [])
  const widthRef = useRef(width); widthRef.current = width
  // Dock position (right column vs bottom row). Bottom dock is height-
  // resizable instead of width-resizable, so it carries its own persisted
  // dimension. Kept separate from width so flipping back and forth restores
  // each orientation's last size.
  const [dock, setDock] = useSidePanelDock()
  const MIN_H = 200
  const [heightBySlot, setHeightBySlot] = useState<Record<string, number>>({})
  const height = useMemo(
    () => ownDim(heightBySlot, dimSlot) ?? loadSidePanelDim({ base: SIDE_PANEL_HEIGHT_KEY, slot: dimSlot, min: MIN_H, fallback: 360 }),
    [heightBySlot, dimSlot, MIN_H],
  )
  const setHeight = useCallback((h: number) => {
    const target = dimSlotRef.current
    setHeightBySlot(m => ({ ...m, [target]: h }))
  }, [])
  const heightRef = useRef(height); heightRef.current = height
  // Responsive clamp: the user's chosen width is persisted untouched, but the
  // rendered width yields to the window so the chat keeps its reserved
  // minimum. On mobile the panel simply takes the full width. Re-measured on
  // window resize.
  const isMobile = useIsMobile()
  // Bottom dock only applies on desktop; mobile always renders as the
  // full-width inline panel regardless of the stored preference.
  const isBottom = canDockBottom && dock === 'bottom' && !isMobile
  // The ceiling is what the ROW actually leaves for the panel: the live nav
  // rail track (0 / 74 / 236 -- it collapses, so a static budget at its expanded
  // width wasted up to 236px), the chat pane's minimum, and whatever sibling
  // column the host adds via `extraReserveW`.
  const railW = useRailWidth()
  const reserveW = railW + CHAT_PANE_MIN_W + extraReserveW
  const [maxW, setMaxW] = useState(() => window.innerWidth - reserveW)
  // Bottom-dock height cap: leave the topbar row + a usable chat minimum
  // visible above the panel. Re-measured on resize.
  const [maxH, setMaxH] = useState(() => Math.max(MIN_H, Math.round(window.innerHeight * 0.85)))
  // A window drag fires `resize` continuously, and easing each clamp step
  // retargets the tween every frame so the edge trails the window instead of
  // tracking it. An active window resize therefore suppresses the ease the same
  // way holding the handle does. It decays rather than opting out permanently,
  // so a DISCRETE clamp change — a sibling column settling — still eases.
  const [windowResizing, setWindowResizing] = useState(false)
  const settleRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => {
    const recalc = () => {
      setMaxW(window.innerWidth - reserveW)
      setMaxH(Math.max(MIN_H, Math.round(window.innerHeight * 0.85)))
    }
    const onWindowResize = () => {
      setWindowResizing(true)
      clearTimeout(settleRef.current)
      settleRef.current = setTimeout(() => setWindowResizing(false), SIDE_PANEL_MOTION_MS)
      recalc()
    }
    recalc()
    window.addEventListener('resize', onWindowResize)
    // Clearing the settle timer without resetting the flag would latch it on
    // whenever `reserveW` changes mid-resize (the last event can re-run this
    // effect), and every later chat switch would then snap.
    return () => {
      window.removeEventListener('resize', onWindowResize)
      clearTimeout(settleRef.current)
      setWindowResizing(false)
    }
    // `reserveW` folds in LIVE widths (the rail collapses; the Members roster
    // and the chat sidebar are drag-resizable), so the clamp re-derives when
    // any of them moves.
  }, [reserveW])
  const effectiveWidth = sidePanelEffectiveWidth({ fillWidth, isMobile, expanded, width, maxW })
  const effectiveHeight = Math.max(MIN_H, Math.min(height, maxH))
  // While the user drags the resize handle, every mousemove shifts the whole
  // panel's viewport position (the handle is on the LEFT edge; the right edge
  // is pinned to the window). Framer's layout projection on each Reorder.Item
  // sees the chips' screen positions change and spring-animates them toward
  // the new spot each frame — so tabs visibly lag the resize and "catch up"
  // when it stops. During a resize we make the layout transition instant.
  const resizing = dragSlot !== null
  // Switching to a chat whose remembered size differs moves the panel's free
  // edge, which the chips' layout projection reads exactly like a drag frame
  // and springs after. `slotSwitched` covers the render that carries the
  // switch; `dimAnimating` covers the rest of the eased move, because the edge
  // travels over SIDE_PANEL_MOTION_MS rather than jumping, and without it the
  // chips trail the panel for the whole animation and catch up at the end,
  // which is the same artifact `resizing` exists to suppress.
  //
  // Tracks `dimSlot`, the chat whose size is on screen, not the `slot` prop: a
  // re-key that lands mid-drag changes nothing on screen until the release, and
  // that release is the move to ease.
  const reduceMotion = useReducedMotion()
  // Maximize, restore and the browser tab's fill-width mode move the edge
  // INSTANTLY, as they did before per-chat sizes, and that has to hold even
  // when one of them lands inside a chat switch's ease: switching away from a
  // chat maximized on its Browser tab clears `expanded` in the same render, and
  // without this the restore would ride the switch's tween. A render whose
  // sizing mode changed never eases, and a mode change also ends an ease that
  // is already running.
  const sizingMode = fillWidth != null ? `fill:${fillWidth}` : expanded ? 'max' : 'free'
  const paintedModeRef = useRef(sizingMode)
  const modeChanged = paintedModeRef.current !== sizingMode
  const paintedSlotRef = useRef(dimSlot)
  const slotSwitched = paintedSlotRef.current !== dimSlot
  const [dimAnimating, setDimAnimating] = useState(false)
  useEffect(() => {
    // Equal on mount and on any re-render that did not change chat, so this
    // never fires an animation the user did not cause. A tab switch inside the
    // chat leaves `dimSlot` alone and therefore never lands here.
    const slotMoved = paintedSlotRef.current !== dimSlot
    const modeMoved = paintedModeRef.current !== sizingMode
    paintedSlotRef.current = dimSlot
    paintedModeRef.current = sizingMode
    if (modeMoved) { setDimAnimating(false); return }
    if (!slotMoved) return
    setDimAnimating(true)
    const t = setTimeout(() => setDimAnimating(false), SIDE_PANEL_MOTION_MS)
    return () => clearTimeout(t)
  }, [dimSlot, sizingMode])
  // A drag starts from the size ON SCREEN. Outside a chat switch's ease that is
  // the logical size, exactly as before per-chat sizes. Inside the ease the
  // edge is mid-flight and the logical size is where it is HEADING: starting
  // there would jump the panel to the target the moment the handle is pressed
  // (`resizing` drops the transition) and save a size the user never saw. So
  // while the ease is live and the painted size is not yet the target, the
  // gesture starts from the painted size. A zero-size rect (not laid out, or
  // hidden) and a non-numeric target (mobile's full width) keep the logical
  // size, as does a clamped or maximized panel, whose painted size already
  // equals its target.
  const rootRef = useRef<HTMLDivElement>(null)
  const easingRef = useRef(false)
  const effectiveRef = useRef<{ width: number | string; height: number }>({ width: 0, height: 0 })
  const grabbedDim = (axis: 'width' | 'height', logical: number): number => {
    const target = effectiveRef.current[axis]
    const el = rootRef.current
    if (!easingRef.current || typeof target !== 'number' || !el) return logical
    const painted = Math.round(el.getBoundingClientRect()[axis])
    return painted > 0 && Math.abs(painted - target) >= 1 ? painted : logical
  }
  const startWRef = useRef(0)
  // The width the gesture last computed. `widthRef` follows the RENDERED width,
  // which lags the pointer by a render, so a release that lands before the last
  // move has painted would store the previous frame's number. This holds what
  // the pointer produced, whatever has or has not rendered in between.
  const dragWRef = useRef(0)
  const panelResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      setDragSlot(slot)
      dragOwnerRef.current = slotOwner
      const w0 = grabbedDim('width', widthRef.current)
      startWRef.current = w0
      dragWRef.current = w0
    },
    onMove: ({ dx }) => {
      // Left-edge handle with the right edge pinned: dragging left (dx < 0) widens.
      // The ceiling is the SAME reserve-based clamp the render path and the
      // preview-expand toggle use (the chat keeps its minimum) — not a viewport
      // fraction on top of it. A 70% cap sat well under that reserve on wide
      // windows and read as an arbitrary stop.
      const max = window.innerWidth - reserveW
      const w = Math.max(MIN_W, Math.min(startWRef.current - dx, max))
      dragWRef.current = w
      setWidth(w)
    },
    onEnd: () => {
      // The chat's own key AND the bare key, so a new chat opens at this size.
      // The in-memory size is set too: a chat re-keyed onto mid-drag may hold
      // an older drag's size there, which would otherwise shadow the saved one.
      const target = releaseSlot()
      const w = dragWRef.current
      saveSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, slot: target, value: w })
      setWidthBySlot(m => ({ ...m, [target]: w }))
      setDragSlot(null)
    },
  })
  // Top-edge resize for the bottom dock: drag up to grow the panel's height.
  // The bottom edge is pinned to the window, so a negative dy (dragging up)
  // widens the panel.
  const startHRef = useRef(0)
  const dragHRef = useRef(0)
  const panelResizeV = usePointerDrag({
    threshold: 0,
    onStart: () => {
      setDragSlot(slot)
      dragOwnerRef.current = slotOwner
      const h0 = grabbedDim('height', heightRef.current)
      startHRef.current = h0
      dragHRef.current = h0
    },
    onMove: ({ dy }) => {
      const max = Math.max(MIN_H, Math.round(window.innerHeight * 0.85))
      const h = Math.max(MIN_H, Math.min(startHRef.current - dy, max))
      dragHRef.current = h
      setHeight(h)
    },
    onEnd: () => {
      const target = releaseSlot()
      const h = dragHRef.current
      saveSidePanelDim({ base: SIDE_PANEL_HEIGHT_KEY, slot: target, value: h })
      setHeightBySlot(m => ({ ...m, [target]: h }))
      setDragSlot(null)
    },
  })

  // The remembered size is per chat, so switching chats MOVES the panel's free
  // edge. Ease it on the panel's own open/close curve (`sidePanelDimTransition`)
  // rather than letting it jump: opening the panel and moving between chats
  // are the same edge traveling the same distance, and a person reads them as
  // one gesture. A tab switch inside the chat never moves the edge, so it never
  // reaches this.
  //
  // Gated to the chat switch, via the `slotSwitched` / `dimAnimating` pair
  // above: `slotSwitched` carries the switch render, `dimAnimating` the rest of
  // the eased move. Maximize (`expanded`) and the browser tab's fill-width mode
  // reach the same `effectiveWidth`, and before per-chat sizes they moved the
  // edge INSTANTLY. Easing those is a behavior change this feature does not
  // need, so they keep jumping.
  //
  // Suppressed while dragging the resize handle (a transition there makes the
  // edge lag the pointer), under prefers-reduced-motion, which framer does not
  // apply to a CSS transition on our behalf, and during a live window resize,
  // which retargets the tween every frame. Same shape as DiagramLightbox's
  // `pinching || dragging || reduceMotion` guard.
  const dimTransition = (slotSwitched || dimAnimating) && !modeChanged && !resizing && !windowResizing && !reduceMotion
    ? sidePanelDimTransition(isBottom ? 'height' : 'width')
    : undefined
  easingRef.current = dimTransition !== undefined
  effectiveRef.current = { width: effectiveWidth, height: effectiveHeight }
  return {
    rootRef, isMobile, isBottom, setDock, effectiveWidth, effectiveHeight, dimTransition,
    resizing, slotSwitched, dimAnimating, panelResize, panelResizeV,
  }
}
