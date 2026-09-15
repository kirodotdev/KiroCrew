/**
 * useDrag — Handles all drag behavior including cross-display drag support.
 * Manages pet position state, mousedown/mousemove/mouseup handlers,
 * position clamping, edge snap detection, and main-process drag polling
 * for cross-display transfers.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { PetState } from '../../shared/types'

import { PET_W, PET_H } from '../../shared/constants'

import { api } from '../../mochiApi'

/**
 * How long a drag may sit with no mouse movement before it is force-ended.
 *
 * This is a recovery deadline, not a UX timing: it exists because a lost mouseup
 * leaves the drag flag latched, and that latch stops the pet reporting its
 * hitbox to the shell — which makes the pet permanently unclickable.
 */
const DRAG_STUCK_MS = 2000

export interface UseDragOptions {
  clearPersistentMood: () => void
  displayState: PetState
  setDisplayState: (s: PetState) => void
  isPeekingRef: React.MutableRefObject<boolean>
  setIsPeeking: (v: boolean) => void
  setHideEdge: (v: 'left' | 'right' | null) => void
}

export interface UseDragReturn {
  pos: { x: number; y: number }
  setPos: React.Dispatch<React.SetStateAction<{ x: number; y: number }>>
  onMouseDown: (e: React.MouseEvent) => void
  dragging: React.MutableRefObject<boolean>
  dragPollingStarted: React.MutableRefObject<boolean>
  posReady: boolean
}

export function useDrag(
  initialPos: { x: number; y: number },
  options: UseDragOptions
): UseDragReturn {
  const [pos, setPos] = useState(initialPos)
  const [posReady, setPosReady] = useState(false)
  const dragging = useRef(false)
  const dragOffset = useRef({ x: 0, y: 0 })
  const dragPollingStarted = useRef(false)
  /**
   * Stuck-drag watchdog, at hook scope so BOTH mousedown and mousemove can arm
   * it. It used to live inside the mousemove effect, which meant a press with no
   * movement armed nothing: if that press's mouseup never arrived (the main
   * process can flip the overlay to click-through mid-press, and a
   * forward:true window is not guaranteed a mouseup), `dragging.current` latched
   * true forever. That latch is load-bearing far beyond dragging — the hitbox
   * reporter skips every update while it is set, so the pet stopped telling the
   * shell where it was and went permanently click-through with nothing logged.
   */
  const stuckTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const endDragRef = useRef<(() => void) | null>(null)

  const armStuckTimer = useCallback(() => {
    if (stuckTimer.current) clearTimeout(stuckTimer.current)
    stuckTimer.current = null
    if (!dragging.current) return
    stuckTimer.current = setTimeout(() => {
      if (dragging.current) endDragRef.current?.()
    }, DRAG_STUCK_MS)
  }, [])

  // Stable ref for options so useEffect closures stay current
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Load saved position from main process
  useEffect(() => {
    setTimeout(() => {
      api?.getWindowPosition?.().then((p: { x: number; y: number }) => {
        if (p) {
          const x = Math.max(0, Math.min(window.innerWidth - PET_W, p.x))
          const y = Math.max(0, Math.min(window.innerHeight - PET_H, p.y))
          setPos({ x, y })
          setPosReady(true)
          const edgeThreshold = 40
          if (x <= edgeThreshold) {
            optionsRef.current.setHideEdge('left')
            optionsRef.current.setIsPeeking(true)
          } else if (x >= window.innerWidth - PET_W - edgeThreshold) {
            optionsRef.current.setHideEdge('right')
            optionsRef.current.setIsPeeking(true)
          }
        } else {
          setPos({ x: 0, y: Math.floor(window.innerHeight - PET_H - 80) })
          optionsRef.current.setHideEdge('left')
          optionsRef.current.setIsPeeking(true)
          setPosReady(true)
        }
      }).catch(() => {
        setPos({ x: 0, y: Math.floor(window.innerHeight - PET_H - 80) })
        optionsRef.current.setHideEdge('left')
        optionsRef.current.setIsPeeking(true)
        setPosReady(true)
      })
    }, 300)
  }, [])

  // ── Drag handlers ──
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return
    dragging.current = true
    dragOffset.current = { x: e.clientX - pos.x, y: e.clientY - pos.y }
    dragPollingStarted.current = false
    // Arm the stuck-drag watchdog HERE, not on the first mousemove. A press with
    // no movement whose mouseup never arrives (the main process can flip the
    // overlay to click-through mid-press, and a forwarded window sees no mouseup)
    // used to leave `dragging.current` latched true forever with no timer running
    // — and that latch also froze the hitbox reporter, so the pet went
    // permanently click-through with nothing logged.
    armStuckTimer()
    optionsRef.current.clearPersistentMood()
    if (optionsRef.current.displayState === 'offline') optionsRef.current.setDisplayState('idle')
    e.preventDefault()
  }, [pos, armStuckTimer])

  // Listen for position updates from main process during drag
  useEffect(() => {
    const off = api?.onDragUpdate?.((x: number, y: number) => {
      if (!dragging.current) return
      if (optionsRef.current.isPeekingRef.current) {
        optionsRef.current.setIsPeeking(false)
        optionsRef.current.setHideEdge(null)
      }
      setPos({ x, y })
    })
    return () => { off?.() }
  }, [])

  // Listen for drag-ended from main process (reliable cross-display drag end)
  useEffect(() => {
    const off = api?.onDragEnded?.((x: number, y: number) => {
      dragging.current = false
      const edgeThreshold = 40
      let fx = Math.max(-PET_W / 2, Math.min(window.innerWidth - PET_W / 2, x))
      const fy = Math.max(0, Math.min(window.innerHeight - PET_H, y))
      const atLeft = fx <= edgeThreshold
      const atRight = fx >= window.innerWidth - PET_W - edgeThreshold
      if (atLeft) fx = 0
      if (atRight) fx = window.innerWidth - PET_W
      setPos({ x: fx, y: fy })
      api?.savePosition?.(fx, fy)
      if (atLeft || atRight) {
        optionsRef.current.setHideEdge(atLeft ? 'left' : 'right')
        optionsRef.current.setIsPeeking(true)
      } else if (optionsRef.current.isPeekingRef.current) {
        optionsRef.current.setIsPeeking(false)
        optionsRef.current.setHideEdge(null)
      }
    })
    return () => { off?.() }
  }, [])

  // Local mousemove/mouseup for same-display drag (faster than polling).
  // The stuck-drag watchdog is armed at hook scope (see `armStuckTimer`) so a
  // press that never moves is covered too.
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      armStuckTimer()
      if (!dragPollingStarted.current) {
        dragPollingStarted.current = true
        api?.dragStart?.(dragOffset.current.x, dragOffset.current.y)
      }
      const rawX = e.clientX - dragOffset.current.x
      const rawY = e.clientY - dragOffset.current.y
      const x = Math.max(-PET_W / 2, Math.min(window.innerWidth - PET_W / 2, rawX))
      const y = Math.max(0, Math.min(window.innerHeight - PET_H, rawY))
      setPos({ x, y })
    }

    const onUp = () => {
      if (stuckTimer.current) { clearTimeout(stuckTimer.current); stuckTimer.current = null }
      if (dragPollingStarted.current) {
        api?.dragEnd?.()
        dragPollingStarted.current = false
      }
      if (!dragging.current) return
      dragging.current = false
      setPos(p => {
        const edgeThreshold = 40
        let x = Math.max(-PET_W / 2, Math.min(window.innerWidth - PET_W / 2, p.x))
        const y = Math.max(0, Math.min(window.innerHeight - PET_H, p.y))
        const atLeft = x <= edgeThreshold
        const atRight = x >= window.innerWidth - PET_W - edgeThreshold
        if (atLeft) x = 0
        if (atRight) x = window.innerWidth - PET_W
        const newPos = { x, y }
        api?.savePosition?.(x, y)
        if (atLeft || atRight) {
          optionsRef.current.setHideEdge(atLeft ? 'left' : 'right')
          optionsRef.current.setIsPeeking(true)
        } else if (optionsRef.current.isPeekingRef.current) {
          optionsRef.current.setIsPeeking(false)
          optionsRef.current.setHideEdge(null)
        }
        return newPos
      })
    }

    // Publish the ender so the hook-scope watchdog (armed on mousedown, before
    // this effect's listeners see anything) can end a drag it did not start.
    endDragRef.current = onUp

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      if (stuckTimer.current) { clearTimeout(stuckTimer.current); stuckTimer.current = null }
      endDragRef.current = null
    }
  }, [armStuckTimer])

  return { pos, setPos, onMouseDown, dragging, dragPollingStarted, posReady }
}
