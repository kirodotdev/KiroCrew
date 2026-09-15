/**
 * useWalking — the companion's walk animation, ported from the desktop app and
 * adapted to this build's architecture.
 *
 * ARCHITECTURE DIFFERENCE. In the standalone app the pet was its OWN small window,
 * and the MAIN PROCESS moved that window and pushed `onWalk` / `onHide` IPC events
 * into the renderer. Here the companion is a DOM element inside one full-display
 * transparent overlay, so there is no window to move and no main-process push
 * channel: walking instead animates the element's `pos` within the overlay via
 * `setPos`, and persistence goes through `petBridge.savePosition` (petX / petY on the
 * gateway config) from the caller's `onWalkEnd`.
 *
 * The MECHANISM changed; the BEHAVIOUR did not. The rAF interpolation, the
 * 6ms-per-pixel duration (see walkMath), the diagonal tilt and the waypoint queue are
 * all exactly as they shipped, so a hop looks the same as it did in the window model.
 * `walkPath` is what the idle fidget drives — a small hop out and straight back home.
 */
import { useEffect, useRef, useState } from 'react'
import {
  walkDirFor,
  walkTiltFor,
  walkDurationMs,
  WALK_MIN_DIST,
  type Edge,
} from './walkMath'

export type OnWalkEnd = (finalPos: { x: number; y: number }) => void

export interface UseWalkingReturn {
  isWalking: boolean
  walkDir: -1 | 1
  walkTilt: number
  cancelWalk: () => void
  /** Walk to an absolute overlay coordinate. */
  walkTo: (x: number, y: number) => void
  /** Walk a sequence of waypoints in order (e.g. a small hop out, then back home). */
  walkPath: (points: Array<{ x: number; y: number }>) => void
}

export function useWalking(
  pos: { x: number; y: number },
  setPos: React.Dispatch<React.SetStateAction<{ x: number; y: number }>>,
  onWalkEnd: OnWalkEnd,
  setIsPeeking: (v: boolean) => void,
  setHideEdge: (v: Edge | null) => void,
): UseWalkingReturn {
  const [walkTarget, setWalkTarget] = useState<{ x: number; y: number } | null>(null)
  const [walkDir, setWalkDir] = useState<-1 | 1>(1)
  const [walkTilt, setWalkTilt] = useState(0)
  const walkingRef = useRef(false)
  const walkQueueRef = useRef<Array<{ x: number; y: number }>>([])
  const walkRafRef = useRef(0)

  // Stable refs so the rAF effect never needs to re-subscribe on a new callback.
  const onWalkEndRef = useRef(onWalkEnd); onWalkEndRef.current = onWalkEnd
  const setIsPeekingRef = useRef(setIsPeeking); setIsPeekingRef.current = setIsPeeking
  const setHideEdgeRef = useRef(setHideEdge); setHideEdgeRef.current = setHideEdge
  const posRef = useRef(pos); posRef.current = pos

  const startWalkTo = (x: number, y: number) => {
    walkingRef.current = true
    // A walk leaves any docked/peeking state: the companion stands up and moves.
    setIsPeekingRef.current(false)
    setHideEdgeRef.current(null)
    setPos((cur) => {
      setWalkDir(walkDirFor(cur.x, x))
      setWalkTilt(walkTiltFor(cur.x, cur.y, x, y))
      return cur
    })
    setWalkTarget({ x, y })
  }

  const cancelWalk = () => {
    cancelAnimationFrame(walkRafRef.current)
    walkQueueRef.current = []
    walkingRef.current = false
    setWalkTarget(null)
  }

  const walkPath = (points: Array<{ x: number; y: number }>) => {
    if (!points.length) return
    cancelWalk()
    walkQueueRef.current = points.slice(1)
    startWalkTo(points[0].x, points[0].y)
  }

  // Walk animation — rAF interpolation, no CSS transition on position (identical to
  // the desktop app). Position moves each frame via setPos; the layout follows.
  useEffect(() => {
    if (!walkTarget) return
    const startX = posRef.current.x
    const startY = posRef.current.y
    const dx = walkTarget.x - startX
    const dy = walkTarget.y - startY
    const dist = Math.hypot(dx, dy)
    if (dist < WALK_MIN_DIST) {
      walkingRef.current = false
      setWalkTarget(null)
      return
    }
    const dur = walkDurationMs(startX, startY, walkTarget.x, walkTarget.y)
    const startTime = performance.now()
    const animate = (now: number) => {
      const t = Math.min(1, (now - startTime) / dur)
      setPos({ x: startX + dx * t, y: startY + dy * t })
      if (t < 1) {
        walkRafRef.current = requestAnimationFrame(animate)
        return
      }
      setPos(walkTarget)
      const next = walkQueueRef.current.shift()
      if (next) {
        startWalkTo(next.x, next.y)
      } else {
        walkingRef.current = false
        const final = walkTarget
        setWalkTarget(null)
        onWalkEndRef.current(final)
      }
    }
    walkRafRef.current = requestAnimationFrame(animate)
    return () => cancelAnimationFrame(walkRafRef.current)
  }, [walkTarget]) // eslint-disable-line react-hooks/exhaustive-deps

  return {
    isWalking: walkTarget !== null,
    walkDir,
    walkTilt,
    cancelWalk,
    walkTo: startWalkTo,
    walkPath,
  }
}
