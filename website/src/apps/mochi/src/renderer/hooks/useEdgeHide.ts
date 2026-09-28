/**
 * useEdgeHide — Manages edge hide/peek state for the pet widget.
 * Tracks whether the pet is hidden at a screen edge and whether it's peeking.
 * The onHide IPC listener stays in PetWidget because it depends on walking state.
 */
import { useRef, useState } from 'react'
import { api } from '../../mochiApi'

export interface UseEdgeHideReturn {
  hideEdge: 'left' | 'right' | null
  isPeeking: boolean
  setIsPeeking: (v: boolean) => void
  setHideEdge: (v: 'left' | 'right' | null) => void
  isPeekingRef: React.MutableRefObject<boolean>
}

export function useEdgeHide(
  isPeekingForSvgRef: React.MutableRefObject<boolean>
): UseEdgeHideReturn {
  const [hideEdge, setHideEdge] = useState<'left' | 'right' | null>(null)
  const [isPeeking, setIsPeekingState] = useState(false)
  const isPeekingRef = useRef(false)

  const setIsPeeking = (v: boolean) => {
    isPeekingRef.current = v
    isPeekingForSvgRef.current = v
    setIsPeekingState(v)
    ;api?.setPeeking?.(v)
  }

  return { hideEdge, isPeeking, setIsPeeking, setHideEdge, isPeekingRef }
}
