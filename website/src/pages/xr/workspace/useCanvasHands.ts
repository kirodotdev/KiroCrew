/**
 * What the canvas of the workspace (`Workspace.tsx`) does with the hands that touch it: the fingers that are down and
 * the two that turn the head, the quiet time after a tap that held the touch back (`previewLook.ts`), the pointers
 * the canvas has heard of, and what makes the person busy (the pointers down, the pinches, the last key and wheel).
 */
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import type { TurningRef } from './EmptySpace'
import type { makePagesStore } from './hands'
import { pinchRoute } from './pinchRoute'
import { TAP_AGAIN_MS, notAPress, type Direction, type Turn } from './previewLook'

/** A touch that was held back: where it came down on the canvas, and what it took hold of once it moved far enough to be a drag. */
export interface Held { pointerId: number; at: [number, number]; grab: Direction | null }

/** What the hands read from the workspace: the bridge, the session's store, and the preview's head. */
export interface CanvasHandsInputs {
  bridge: MutableRefObject<{ inSession: boolean }>
  store: ReturnType<typeof makePagesStore>
  turning: TurningRef
  turn: MutableRefObject<Turn>
  shift: MutableRefObject<number>
  inSession: boolean
}

export function useCanvasHands({ bridge, store, turning, turn, shift, inSession }: CanvasHandsInputs) {
  // The fingers that are down on the canvas, and what two of them took hold of while they turn the head and come
  // nearer: the direction under their middle, how far apart they were, and how near the eye was then.
  const fingers = useRef(new Map<number, [number, number]>())
  const two = useRef<{ grab: Direction; spread: number; near: number; shift: number } | null>(null)
  // The quiet time (`previewLook.ts`): until `quiet` no touch on the canvas does anything to a page or a panel.
  // `afterClose`: it began with a close, so each touch that is held back starts it anew. While it runs the panels
  // are `hushed`: they take no pointer, and what was lit goes dark. `taken` is the touch that is held back now:
  // when it moves far enough it is a drag, and holds the room as a finger on empty space does.
  const taken = useRef<Held | null>(null)
  const quiet = useRef(0)
  const afterClose = useRef(false)
  const [hushed, setHushed] = useState(false)
  const host = useRef<HTMLDivElement>(null)
  /** The pointers that the canvas has heard of (see the leave, below). */
  const known = useRef(new Set<number>())
  // The same as it is right now: set when the time begins, and cleared once the panels take pointers again.
  const hushedNow = useRef(false)
  // The canvas presses, and lights, at the place of the last move that it heard. So it is told a mouse's place by
  // a move of this page's own making: where the mouse was last when its moves were held back (`missed`, told when
  // the time is up), and where a press is, right before the press. A pointer that left the canvas is nowhere.
  const missed = useRef<{ pointerId: number; pointerType: string; x: number; y: number } | null>(null)
  const tellCanvas = useCallback((pointerId: number, pointerType: string, x: number, y: number) => {
    known.current.add(pointerId)
    host.current?.querySelector('canvas')?.dispatchEvent(new PointerEvent('pointermove', { pointerId, pointerType, clientX: x, clientY: y, isPrimary: true, bubbles: true }))
  }, [])
  useEffect(() => {
    hushedNow.current = hushed
    const m = missed.current
    if (hushed || !m) return
    missed.current = null
    tellCanvas(m.pointerId, m.pointerType, m.x, m.y)
  }, [hushed, tellCanvas])
  const wake = useRef<number | null>(null)
  useEffect(() => () => { if (wake.current !== null) window.clearTimeout(wake.current) }, [])
  /** Whether a session is on, or about to draw its first frame. In a session there is no quiet time. */
  const inXr = useCallback(() => bridge.current.inSession || store.getState().session != null, [store, bridge])
  const hush = useCallback((closed: boolean) => {
    // In a session the quiet time is the pinches' own (`pinchRoute.hush`): a pinch in it presses nothing.
    if (inXr()) { pinchRoute.hush(performance.now(), TAP_AGAIN_MS, closed); return }
    quiet.current = performance.now() + TAP_AGAIN_MS
    afterClose.current = closed
    hushedNow.current = true
    setHushed(true)
    if (wake.current !== null) return
    // The time may be started anew while it runs, and a turn may still be under way when it is up.
    const look = () => {
      const left = quiet.current - performance.now()
      // A turn moves on only outside a session: in one, nothing is waited for.
      if (!inXr() && (left > 0 || turning.current)) { wake.current = window.setTimeout(look, Math.max(left, 30)); return }
      wake.current = null
      setHushed(false)
    }
    wake.current = window.setTimeout(look, TAP_AGAIN_MS)
  }, [inXr, turning])
  // A session begins: the quiet time is the flat preview's, and it leaves nothing behind. A turn that is under way
  // is finished at once, so the picture is on its page when the person comes back; no panel stays hushed.
  useEffect(() => {
    if (!inSession) return
    const going = turning.current
    if (going) { turn.current = going.to; if (going.shift) shift.current = going.shift.to; turning.current = null }
    quiet.current = 0
    pinchRoute.quietUntil = 0
    taken.current = null
    missed.current = null
    if (wake.current !== null) { window.clearTimeout(wake.current); wake.current = null }
    hushedNow.current = false
    setHushed(false)
  }, [inSession, turning, turn, shift])
  /** Whether what the pointer does on the canvas now is held back. Never in a session. */
  const heldBack = () => !inXr() && (hushedNow.current || notAPress(turning.current !== null, performance.now(), quiet.current))
  // The pointers that the canvas has heard of. When a pointer leaves that it never heard of (a finger whose whole
  // touch was held back), the canvas must not hear of the leave either: it would first look what is under that
  // pointer, and light it. React hears no leave on its way down, so the host listens for it itself.
  useEffect(() => {
    const el = host.current
    if (!el) return
    const leave = (e: PointerEvent) => {
      if (!e.isTrusted || !(e.target instanceof HTMLCanvasElement)) return
      if (missed.current?.pointerId === e.pointerId) missed.current = null
      if (!known.current.delete(e.pointerId)) e.stopPropagation()
    }
    el.addEventListener('pointerleave', leave, true)
    return () => el.removeEventListener('pointerleave', leave, true)
  }, [])
  // What makes the person busy: the pointers that are down on the canvas and since when, the pinches of a session, and when a key last went to a page.
  const downs = useRef(new Map<number, number>())
  const selects = useRef(0)
  const lastKeyAt = useRef(0)
  const lastWheelAt = useRef(0)
  /** The pages drawn by this tab itself in which a pointer is down now, and since when: the browser hands them input itself. */
  const pageDowns = useRef(new Map<string, number>())
  return { fingers, two, taken, quiet, afterClose, hushed, host, known, hushedNow, missed, tellCanvas, inXr, hush, heldBack, downs, selects, lastKeyAt, lastWheelAt, pageDowns }
}
