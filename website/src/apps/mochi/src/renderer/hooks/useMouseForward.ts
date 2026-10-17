/**
 * useMouseForward — Sends the pet and bubble hitbox positions to the main
 * process, which polls cursor position at 60fps and toggles ignore-mouse
 * directly. This eliminates the IPC round-trip delay that caused clicks
 * on the pet body to pass through to windows behind.
 */
import { useEffect, useRef, useState } from 'react'

import { PET_W, PET_H, BUBBLE_W } from '../../shared/constants'

import { api } from '../../mochiApi'

export interface UseMouseForwardParams {
  pos: { x: number; y: number }
  visualPos: { x: number; y: number }
  bubble: string | null
  bubbleX: number
  bubbleAbove: boolean
  bubbleY: number
  bubbleHeight: number
  isPeekingForSvgRef: React.MutableRefObject<boolean>
  hideEdge: 'left' | 'right' | null
  dragging: React.MutableRefObject<boolean>
  isActiveRef: React.MutableRefObject<boolean>
}

export function useMouseForward(params: UseMouseForwardParams): void {
  const {
    pos, visualPos, bubble, bubbleX, bubbleAbove, bubbleY, bubbleHeight,
    isPeekingForSvgRef, hideEdge, dragging, isActiveRef,
  } = params

  const lastPetRef = useRef('')
  const lastBubbleRef = useRef('')

  // Tick counter to force hitbox re-send while bubble is visible (guards against
  // stale hitbox after sleep/wake where main process may have lost state).
  const [hitboxTick, setHitboxTick] = useState(0)

  // Send hitbox updates to main process whenever position/bubble changes
  useEffect(() => {
    if (!isActiveRef.current) {
      // An inactive overlay must say NOTHING. There is one pet and one hitbox
      // slot, but one overlay per display — this branch used to report its own
      // position "in case set-active hasn't arrived yet", which made every other
      // monitor's window a competing writer. The two reporters then overwrote
      // each other and the shell's poll flipped click-through on every tick.
      //
      // The startup race it was guarding is handled where it belongs: the main
      // process drops hitbox frames from any sender that is not the active
      // overlay, so a late set-active can no longer cost the pet its clicks.
      api?.updateHitbox?.(null, null)
      return
    }
    // NOT gated on `dragging.current`. The main process already ignores the
    // hitbox while it is drag-polling, so suppressing sends here buys nothing —
    // and it made a latched drag flag (a press whose mouseup was lost) freeze
    // the hitbox permanently, which reads to the user as "the pet stopped
    // responding to clicks" with no error anywhere. Reporting where the pet is
    // must never depend on another hook's flag being clean.
    const petBox = { x: visualPos.x, y: visualPos.y, w: PET_W, h: PET_H }

    let bubbleBox: { x: number; y: number; w: number; h: number } | null = null
    if (bubble) {
      let bx = bubbleX
      if (isPeekingForSvgRef.current && hideEdge === 'left') bx = visualPos.x + PET_W * 0.45
      else if (isPeekingForSvgRef.current && hideEdge === 'right') bx = visualPos.x + PET_W * 0.55 - BUBBLE_W
      const by = bubbleY
      const bh = (bubbleHeight || 200) + 20  // extra padding for tail
      bubbleBox = { x: bx, y: by, w: BUBBLE_W, h: bh }
    }

    // Only send if changed (avoid flooding IPC)
    const petKey = `${petBox.x},${petBox.y},${hitboxTick}`
    const bubbleKey = bubbleBox ? `${bubbleBox.x},${bubbleBox.y},${bubble}` : ''
    if (petKey !== lastPetRef.current || bubbleKey !== lastBubbleRef.current) {
      lastPetRef.current = petKey
      lastBubbleRef.current = bubbleKey
      api?.updateHitbox?.(petBox, bubbleBox)
    }
  }, [pos, bubble, bubbleX, bubbleAbove, bubbleY, bubbleHeight, visualPos, hideEdge, dragging, isActiveRef, isPeekingForSvgRef, hitboxTick])

  // Re-send hitbox periodically while bubble is visible (guards against
  // stale hitbox after sleep/wake where main process may have lost state)
  useEffect(() => {
    if (!bubble) return
    const interval = setInterval(() => {
      setHitboxTick(t => t + 1)
    }, 2000)
    return () => clearInterval(interval)
  }, [bubble])

  // Note: we intentionally do NOT clear the hitbox on mousedown/drag-start.
  // For full drags, main-process drag polling takes over (hitbox poll is skipped).
  // For short drags where polling never starts, keeping the hitbox valid ensures
  // the pet remains clickable after the drag ends. The main useEffect above
  // will update the hitbox on the next pos change.

  // Re-send hitbox after drag ends — dragging.current is a ref (no re-render),
  // so the main useEffect may not re-run. Force a hitbox update so the pet
  // becomes clickable again immediately after drag.
  // Also handles short drags: useDrag's mouseup sets dragging.current = false
  // and calls setPos, which triggers a re-render → main useEffect re-runs.
  // But if pos doesn't change (drag ended at same spot), we need this fallback.
  useEffect(() => {
    const off = api?.onDragEnded?.(() => {
      // Small delay to let position settle after edge-snap
      setTimeout(() => {
        if (!isActiveRef.current) return
        if (dragging.current) return  // new drag started
        const petBox = { x: visualPos.x, y: visualPos.y, w: PET_W, h: PET_H }
        api?.updateHitbox?.(petBox, null)
        lastPetRef.current = ''  // force next useEffect to also send
        lastBubbleRef.current = ''
      }, 50)
    })
    return () => { off?.() }
  }, [visualPos.x, visualPos.y, dragging, isActiveRef])

  // Fallback: after any mouseup, if we're not dragging, ensure hitbox is valid.
  // This catches short drags where drag polling never started (so onDragEnded
  // never fires). Uses requestAnimationFrame to run after React re-renders
  // with the final position from useDrag's mouseup handler.
  useEffect(() => {
    const onMouseUp = () => {
      requestAnimationFrame(() => {
        if (dragging.current || !isActiveRef.current) return
        // Invalidate cache so the main useEffect re-sends on next render
        lastPetRef.current = ''
        lastBubbleRef.current = ''
        // Also send immediately with current visualPos as safety net
        const petBox = { x: visualPos.x, y: visualPos.y, w: PET_W, h: PET_H }
        api?.updateHitbox?.(petBox, null)
      })
    }
    window.addEventListener('mouseup', onMouseUp)
    return () => window.removeEventListener('mouseup', onMouseUp)
  }, [dragging, isActiveRef, visualPos.x, visualPos.y])
}
