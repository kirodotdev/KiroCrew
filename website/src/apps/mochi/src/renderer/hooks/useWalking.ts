/**
 * useWalking — Manages walk animation state and IPC listeners for walking.
 * Handles onWalk and onHide IPC events, walk target/duration/direction/tilt,
 * and calls onWalkEnd when the walk animation completes so PetWidget can
 * update edge state and persist position.
 */
import { useEffect, useRef, useState } from 'react'

import { PET_W } from '../../shared/constants'

import { api } from '../../mochiApi'

export type OnWalkEnd = (finalPos: { x: number; y: number }) => void

export interface UseWalkingReturn {
  isWalking: boolean
  walkDir: -1 | 1
  walkTilt: number
  cancelWalk: () => void
}

export function useWalking(
  pos: { x: number; y: number },
  setPos: React.Dispatch<React.SetStateAction<{ x: number; y: number }>>,
  onWalkEnd: OnWalkEnd,
  setIsPeeking: (v: boolean) => void,
  setHideEdge: (v: 'left' | 'right' | null) => void,
): UseWalkingReturn {
  const [walkTarget, setWalkTarget] = useState<{ x: number; y: number } | null>(null)
  const [walkDir, setWalkDir] = useState<-1 | 1>(1)
  const [walkTilt, setWalkTilt] = useState(0)
  const walkingRef = useRef(false)

  // Stable refs for callbacks so effects don't re-subscribe
  const onWalkEndRef = useRef(onWalkEnd)
  onWalkEndRef.current = onWalkEnd
  const setIsPeekingRef = useRef(setIsPeeking)
  setIsPeekingRef.current = setIsPeeking
  const setHideEdgeRef = useRef(setHideEdge)
  setHideEdgeRef.current = setHideEdge

  // onWalk IPC listener — start walking to a target position
  const walkQueueRef = useRef<Array<{x: number; y: number}>>([])

  const startWalkTo = (x: number, y: number) => {
    walkingRef.current = true
    setIsPeekingRef.current(false)
    setHideEdgeRef.current(null)
    setPos(cur => {
      setWalkDir(x < cur.x ? -1 : 1)
      const angle = Math.atan2(y - cur.y, x - cur.x) * (180 / Math.PI)
      const absDeg = Math.abs(angle)
      const isDiagonal = (absDeg > 30 && absDeg < 60) || (absDeg > 120 && absDeg < 150)
      const tilt = isDiagonal ? Math.max(-6, Math.min(6, angle * 0.07)) : 0
      setWalkTilt(tilt)
      return cur
    })
    setWalkTarget({ x, y })
  }

  const cancelWalk = () => {
    cancelAnimationFrame(walkRafRef.current)
    flushWalkDist()
    walkQueueRef.current = []
    walkingRef.current = false
    setWalkTarget(null)
  }

  // Track current position for distance checks in IPC listeners
  const posRef = useRef(pos)
  posRef.current = pos

  useEffect(() => {
    const off = api?.onWalk?.((x: number, y: number) => {
      cancelWalk()
      const dist = Math.sqrt((x - posRef.current.x) ** 2 + (y - posRef.current.y) ** 2)
      if (dist < 5) {
        // Already at target — skip walk, just signal done
        api?.walkDone?.()
        return
      }
      startWalkTo(x, y)
    })
    return () => { off?.() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe-once: `cancelWalk`/`startWalkTo` are per-render closures, so listing them would re-register the `onWalk` listener on every render — and a walk calls `setPos` every frame, so the subscription would be torn down and re-added ~60x/s. Neither closure can go stale: both reach live state through refs (`posRef`, `walkQueueRef`, `walkingRef`) and stable setters.
  }, [setPos])

  // Walk path — queue of waypoints
  useEffect(() => {
    const off = api?.onWalkPath?.((points: Array<{x: number; y: number}>) => {
      if (points.length === 0) return
      cancelWalk()
      walkQueueRef.current = points.slice(1)
      startWalkTo(points[0].x, points[0].y)
    })
    return () => { off?.() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe-once, same as `onWalk` above: the two per-render closures would re-register this listener on every frame of a walk. The waypoint queue lives in `walkQueueRef`, so a listener registered at mount still appends to the queue the current walk is draining.
  }, [setPos])

  // Cancel walk
  useEffect(() => {
    const off = api?.onWalkCancel?.(() => { cancelWalk() })
    return () => { off?.() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe-once: `cancelWalk` is a per-render closure but touches only refs and stable setters, so the one captured at mount cancels the CURRENT walk. Listing it would re-register a cancel listener on every render, and a cancel that arrives during the swap is a walk that never stops.
  }, [])

  // Append waypoints to current walk queue
  useEffect(() => {
    const off = api?.onWalkAppend?.((points: Array<{x: number; y: number}>) => {
      if (points.length === 0) return
      if (walkingRef.current) {
        walkQueueRef.current.push(...points)
      } else {
        walkQueueRef.current = points.slice(1)
        startWalkTo(points[0].x, points[0].y)
      }
    })
    return () => { off?.() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe-once: `startWalkTo` is a per-render closure, and re-registering this listener on every render would mean re-subscribing on every frame of the very walk it appends to. `walkingRef`/`walkQueueRef` carry the live walk, so the mount-time closure appends to the right queue.
  }, [setPos])

  // onHide IPC listener — walk to screen edge
  // (posRef declared above for use in onWalk and onHide)

  useEffect(() => {
    const off = api?.onHide?.((edge: 'left' | 'right') => {
      cancelWalk()
      const targetX = edge === 'left' ? 0 : window.innerWidth - PET_W
      const dist = Math.abs(posRef.current.x - targetX)
      if (dist < 40) {
        // Already at or near the edge — skip walk, just snap to edge state
        setHideEdgeRef.current(edge)
        setIsPeekingRef.current(true)
        setPos({ x: targetX, y: posRef.current.y })
        onWalkEndRef.current({ x: targetX, y: posRef.current.y })
        api?.walkDone?.()
        return
      }
      setHideEdgeRef.current(edge)
      startWalkTo(targetX, posRef.current.y)
    })
    return () => { off?.() }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe-once: `setPos` is a stable `useState` setter, and `cancelWalk`/`startWalkTo` are per-render closures whose inclusion would re-register the hide listener on every render — including every frame of the walk this handler starts. The handler reads the live position through `posRef`, never through the captured `pos`.
  }, [])

  // Walk animation — rAF-based interpolation (no CSS transition)
  const walkRafRef = useRef(0)
  const walkDistRef = useRef(0)  // accumulated actual pixel distance
  const lastWalkPosRef = useRef<{x: number; y: number} | null>(null)

  // Flush accumulated walk distance to main process
  const flushWalkDist = () => {
    if (walkDistRef.current > 0) {
      api?.reportWalkDistance?.(walkDistRef.current)
      walkDistRef.current = 0
    }
    lastWalkPosRef.current = null
  }

  useEffect(() => {
    if (!walkTarget) return
    const startX = pos.x
    const startY = pos.y
    const dx = walkTarget.x - startX
    const dy = walkTarget.y - startY
    const dist = Math.sqrt(dx * dx + dy * dy)
    if (dist < 5) {
      walkingRef.current = false
      setWalkTarget(null)
      flushWalkDist()
      api?.walkDone?.()
      return
    }
    const dur = Math.max(800, dist * 6)
    const startTime = performance.now()
    lastWalkPosRef.current = { x: startX, y: startY }

    const animate = (now: number) => {
      const elapsed = now - startTime
      const t = Math.min(1, elapsed / dur)
      const x = startX + dx * t
      const y = startY + dy * t

      // Track actual distance moved
      if (lastWalkPosRef.current) {
        const adx = x - lastWalkPosRef.current.x
        const ady = y - lastWalkPosRef.current.y
        walkDistRef.current += Math.sqrt(adx * adx + ady * ady)
      }
      lastWalkPosRef.current = { x, y }

      setPos({ x, y })
      if (t < 1) {
        walkRafRef.current = requestAnimationFrame(animate)
      } else {
        setPos(walkTarget)
        // Process next waypoint in queue
        const next = walkQueueRef.current.shift()
        if (next) {
          startWalkTo(next.x, next.y)
        } else {
          walkingRef.current = false
          setWalkTarget(null)
          flushWalkDist()
          onWalkEndRef.current(walkTarget)
          api?.walkDone?.()
        }
      }
    }
    walkRafRef.current = requestAnimationFrame(animate)
    return () => cancelAnimationFrame(walkRafRef.current)
  }, [walkTarget]) // eslint-disable-line react-hooks/exhaustive-deps

  const isWalking = walkTarget !== null

  return { isWalking, walkDir, walkTilt, cancelWalk }
}
