/**
 * useEdgeHide — Manages edge hide/peek state for the companion.
 *
 * Tracks whether the companion is tucked at a screen edge (`hideEdge`) and whether
 * it is peeking (`isPeeking`), and exposes a mirror `isPeekingRef` that `setIsPeeking`
 * updates SYNCHRONOUSLY. That synchronous update is the point of the hook: `useDrag`
 * reads `isPeekingRef.current` inside its imperative mouse handlers to decide whether
 * to un-dock as a drag begins, so the ref has to be current the instant the state is
 * set — not one render later. The overlay used to assign the ref at render time
 * (`isPeekingRef.current = isPeeking`), which lagged those imperative reads and left
 * the dock state inconsistent.
 *
 * Ported from the desktop app's src/renderer/hooks/useEdgeHide.ts. Two adaptations
 * for this build: there is no separate SVG ref (the avatar reads `docked` from
 * `hideEdge` directly), and there is no `crewCompanion.setPeeking` bridge on the
 * overlay window, so neither the `isPeekingForSvgRef` parameter nor the bridge call
 * is carried over.
 */
import { useRef, useState } from 'react'

export interface UseEdgeHideReturn {
  hideEdge: 'left' | 'right' | null
  isPeeking: boolean
  setIsPeeking: (v: boolean) => void
  setHideEdge: (v: 'left' | 'right' | null) => void
  isPeekingRef: React.MutableRefObject<boolean>
}

export function useEdgeHide(): UseEdgeHideReturn {
  const [hideEdge, setHideEdge] = useState<'left' | 'right' | null>(null)
  const [isPeeking, setIsPeekingState] = useState(false)
  const isPeekingRef = useRef(false)

  const setIsPeeking = (v: boolean) => {
    isPeekingRef.current = v
    setIsPeekingState(v)
  }

  return { hideEdge, isPeeking, setIsPeeking, setHideEdge, isPeekingRef }
}
