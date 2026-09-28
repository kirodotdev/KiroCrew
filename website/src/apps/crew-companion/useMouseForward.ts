/**
 * useMouseForward — reports the companion's and bubble's hitboxes to the main
 * process, which polls the cursor at ~60fps and toggles ignore-mouse itself.
 *
 * Ported from the desktop app's `src/renderer/hooks/useMouseForward.ts`. The
 * header there says it plainly: sending the hitbox rects and letting the main
 * process do the hit-test "eliminates the IPC round-trip delay that caused clicks
 * on the pet body to pass through to windows behind." That round-trip is exactly
 * the bug this fixes — the old pointer-enter/leave `setInteractive` call had to
 * make it to the main process and back before the window accepted input, so a
 * fast click landed while the window was still click-through.
 *
 * Adapted to this build's single-active-overlay model:
 *   - The main process picks ONE active display and signals each overlay
 *     (onSetActive), so only the active overlay describes a companion. When
 *     inactive this hook clears its hitbox once (updateHitbox(null, null)) and goes
 *     silent, so a background display never claims cursor input — that is the
 *     `isActive` gating restored here, which the earlier single-display port had
 *     dropped.
 *   - The source SKIPS sending while dragging because its main process runs a
 *     separate drag poll that owns ignore-mouse during a drag. This build has no
 *     such poll, so we do the opposite and keep sending: the companion's rect
 *     tracks the cursor during a drag (the grab offset is constant, so the cursor
 *     stays within the 128px box), which keeps the window interactive for the
 *     whole gesture and lets mouseup land.
 *   - The menu rect is reported separately via `petBridge.setMenuHitbox` from the
 *     context-menu component, so it is not a parameter here.
 */
import { useEffect, useRef, useState, type MutableRefObject } from 'react'

import type { Rect } from './bubbleLayout'
import { petBridge } from './petBridge'
import { petHitbox, bubbleHitbox } from './hitbox'

export interface UseMouseForwardParams {
  /** The companion's current overlay-local position. */
  pos: { x: number; y: number }
  /** The measured bubble rectangle, or null when no bubble is showing. */
  bubbleRect: Rect | null
  /** useDrag's dragging flag — a ref, so no re-render when it flips. */
  dragging: MutableRefObject<boolean>
  /** False on a background display: this overlay draws no companion, so send nothing. */
  isActive: boolean
}

export function useMouseForward({ pos, bubbleRect, dragging, isActive }: UseMouseForwardParams): void {
  // Last rects sent, so an unchanged frame does not flood IPC.
  const lastPet = useRef('')
  const lastBubble = useRef('')
  // True once we have cleared our hitbox for the current inactive spell, so an
  // inactive overlay sends updateHitbox(null, null) exactly once and nothing after.
  const clearedInactive = useRef(false)

  // Forces a re-send while a bubble is visible, guarding against a hitbox the
  // main process may have dropped across a sleep/wake.
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!isActive) {
      if (!clearedInactive.current) {
        clearedInactive.current = true
        lastPet.current = ''
        lastBubble.current = ''
        petBridge.updateHitbox?.(null, null)
      }
      return
    }
    clearedInactive.current = false
    const pet = petHitbox(pos)
    const bubble = bubbleHitbox(bubbleRect)
    const petKey = `${pet.x},${pet.y},${tick}`
    const bubbleKey = bubble ? `${bubble.x},${bubble.y},${bubble.w},${bubble.h}` : ''
    if (petKey === lastPet.current && bubbleKey === lastBubble.current) return
    lastPet.current = petKey
    lastBubble.current = bubbleKey
    petBridge.updateHitbox?.(pet, bubble)
  }, [pos, bubbleRect, tick, isActive])

  // Re-assert the hitbox every couple of seconds while a bubble is up.
  useEffect(() => {
    if (!bubbleRect) return
    const id = window.setInterval(() => setTick((t) => t + 1), 2000)
    return () => window.clearInterval(id)
  }, [bubbleRect])

  // After any mouseup — a drag usually ends with the pointer off the companion —
  // re-send so the companion becomes clickable again immediately. Runs after the
  // React commit (rAF) so it reads the settled post-drag position.
  useEffect(() => {
    // Inactive overlay stays silent — see the header note.
    if (!isActive) return
    const onUp = () => {
      requestAnimationFrame(() => {
        if (dragging.current) return
        lastPet.current = ''
        lastBubble.current = ''
        petBridge.updateHitbox?.(petHitbox(pos), bubbleHitbox(bubbleRect))
      })
    }
    window.addEventListener('mouseup', onUp)
    return () => window.removeEventListener('mouseup', onUp)
  }, [pos, bubbleRect, dragging, isActive])
}
