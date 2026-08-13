/**
 * useDrag — Handles all drag behavior including cross-display drag support.
 * Manages pet position state, mousedown/mousemove/mouseup handlers,
 * position clamping, edge snap detection, and main-process drag polling
 * for cross-display transfers.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { PetState } from './types'

import { PET_W, PET_H } from './constants'
import { petBridge } from './petBridge'

// Same method names as the desktop app's IPC bridge, re-implemented over
// Kiro Crew's gateway — so everything below is unchanged.
const api = petBridge

export interface UseDragOptions {
  clearPersistentMood: () => void
  displayState: PetState
  setDisplayState: (s: PetState) => void
  isPeekingRef: React.MutableRefObject<boolean>
  setIsPeeking: (v: boolean) => void
  setHideEdge: (v: 'left' | 'right' | null) => void
  /** When false (custom packs), the pet never peeks/docks at a screen edge. */
  allowPeek: boolean
  /** Grip point under the cursor for this drag — see shared/dragGrip.ts. Read
   *  once when the drag actually starts, so crossing the screen midline (which
   *  mirrors the art) can't yank the pet sideways mid-drag. */
  getGrip: () => { x: number; y: number }
}

export interface UseDragReturn {
  pos: { x: number; y: number }
  setPos: React.Dispatch<React.SetStateAction<{ x: number; y: number }>>
  onMouseDown: (e: React.MouseEvent) => void
  dragging: React.MutableRefObject<boolean>
  dragPollingStarted: React.MutableRefObject<boolean>
  posReady: boolean
  /** True while actively dragging — used to show the "held" leaning pose. */
  isDragging: boolean
}

/** Vertical grip and the horizontal inset now live in shared/dragGrip.ts, which
 *  mirrors the grip with the art and centres it for custom packs. */

export function useDrag(
  initialPos: { x: number; y: number },
  options: UseDragOptions
): UseDragReturn {
  const [pos, setPos] = useState(initialPos)
  const [posReady, setPosReady] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const dragging = useRef(false)
  const dragOffset = useRef({ x: 0, y: 0 })
  const dragPollingStarted = useRef(false)
  const posRef = useRef(pos); posRef.current = pos
  const dragStartPt = useRef({ x: 0, y: 0 })   // where the mouse went down
  const dragEnteredRef = useRef(false)          // true once movement crosses the drag threshold

  // Stable ref for options so useEffect closures stay current
  const optionsRef = useRef(options)
  optionsRef.current = options

  // Load saved position from main process
  useEffect(() => {
    setTimeout(() => {
      api?.getWindowPosition?.().then((p: { x: number; y: number } | null) => {
        if (p) {
          const x = Math.max(0, Math.min(window.innerWidth - PET_W, p.x))
          const y = Math.max(0, Math.min(window.innerHeight - PET_H, p.y))
          setPos({ x, y })
          setPosReady(true)
          const edgeThreshold = 40
          if (optionsRef.current.allowPeek) {
            if (x <= edgeThreshold) {
              optionsRef.current.setHideEdge('left')
              optionsRef.current.setIsPeeking(true)
            } else if (x >= window.innerWidth - PET_W - edgeThreshold) {
              optionsRef.current.setHideEdge('right')
              optionsRef.current.setIsPeeking(true)
            }
          }
        } else {
          setPos({ x: 0, y: Math.floor(window.innerHeight - PET_H - 80) })
          if (optionsRef.current.allowPeek) {
            optionsRef.current.setHideEdge('left')
            optionsRef.current.setIsPeeking(true)
          }
          setPosReady(true)
        }
      }).catch(() => {
        setPos({ x: 0, y: Math.floor(window.innerHeight - PET_H - 80) })
        if (optionsRef.current.allowPeek) {
          optionsRef.current.setHideEdge('left')
          optionsRef.current.setIsPeeking(true)
        }
        setPosReady(true)
      })
    }, 300)
  }, [])

  // ── Drag handlers ──
  const onMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return
    dragging.current = true
    // Do NOT enter the "held" drag pose yet — a plain click should not flash it.
    // We only switch to grab-by-tip once the mouse actually moves (see onMove).
    dragEnteredRef.current = false
    dragStartPt.current = { x: e.clientX, y: e.clientY }
    dragOffset.current = { x: e.clientX - posRef.current.x, y: e.clientY - posRef.current.y }
    dragPollingStarted.current = false
    optionsRef.current.clearPersistentMood()
    if (optionsRef.current.displayState === 'offline') optionsRef.current.setDisplayState('idle')
    e.preventDefault()
  }, [])

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
      setIsDragging(false)
      dragEnteredRef.current = false
      const edgeThreshold = 40
      let fx = Math.max(-PET_W / 2, Math.min(window.innerWidth - PET_W / 2, x))
      const fy = Math.max(0, Math.min(window.innerHeight - PET_H, y))
      const atLeft = fx <= edgeThreshold
      const atRight = fx >= window.innerWidth - PET_W - edgeThreshold
      if (atLeft) fx = 0
      if (atRight) fx = window.innerWidth - PET_W
      setPos({ x: fx, y: fy })
      api?.savePosition?.(fx, fy)
      if (optionsRef.current.allowPeek && (atLeft || atRight)) {
        optionsRef.current.setHideEdge(atLeft ? 'left' : 'right')
        optionsRef.current.setIsPeeking(true)
      } else if (optionsRef.current.isPeekingRef.current) {
        optionsRef.current.setIsPeeking(false)
        optionsRef.current.setHideEdge(null)
      }
    })
    return () => { off?.() }
  }, [])

  // Local mousemove/mouseup for same-display drag (faster than polling)
  // Includes a drag-stuck safety timer: if no mousemove for 2s while dragging,
  // assume mouseup was swallowed (e.g. by macOS dock) and auto-end drag.
  useEffect(() => {
    let dragStuckTimer: ReturnType<typeof setTimeout> | null = null

    const resetStuckTimer = () => {
      if (dragStuckTimer) clearTimeout(dragStuckTimer)
      if (dragging.current) {
        dragStuckTimer = setTimeout(() => {
          if (dragging.current) onUp()
        }, 2000)
      }
    }

    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return
      resetStuckTimer()
      // Only treat it as a drag once the pointer has moved past a small threshold —
      // otherwise a plain click would flash the "held" pose and jump the ghost.
      if (!dragEnteredRef.current) {
        const dx = e.clientX - dragStartPt.current.x
        const dy = e.clientY - dragStartPt.current.y
        if (Math.hypot(dx, dy) < 6) return
        dragEnteredRef.current = true
        setIsDragging(true)  // now show the leaning "held" pose
        // Switch to grab-by-tip so the ghost hangs from the cursor at its grip point.
        dragOffset.current = optionsRef.current.getGrip()
      }
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
      if (dragStuckTimer) { clearTimeout(dragStuckTimer); dragStuckTimer = null }
      if (dragPollingStarted.current) {
        api?.dragEnd?.()
        dragPollingStarted.current = false
      }
      if (!dragging.current) return
      dragging.current = false
      setIsDragging(false)
      dragEnteredRef.current = false
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
        if (optionsRef.current.allowPeek && (atLeft || atRight)) {
          optionsRef.current.setHideEdge(atLeft ? 'left' : 'right')
          optionsRef.current.setIsPeeking(true)
        } else if (optionsRef.current.isPeekingRef.current) {
          optionsRef.current.setIsPeeking(false)
          optionsRef.current.setHideEdge(null)
        }
        return newPos
      })
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      if (dragStuckTimer) clearTimeout(dragStuckTimer)
    }
  }, [])

  return { pos, setPos, onMouseDown, dragging, dragPollingStarted, posReady, isDragging }
}
