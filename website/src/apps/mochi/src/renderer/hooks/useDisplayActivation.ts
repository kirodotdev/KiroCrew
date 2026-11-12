/**
 * useDisplayActivation — per-display activation/deactivation logic.
 * Each overlay window listens for activation from the main process.
 * When activated with coordinates, calls onActivate so PetWidget can set pos/dragging.
 */
import { useEffect, useRef, useState } from 'react'

import { api } from '../../mochiApi'

export interface DisplayInfo {
  id: number
  x: number
  y: number
  width: number
  height: number
}

interface UseDisplayActivationCallbacks {
  onActivate: (x: number, y: number, isDragging: boolean) => void
  onDeactivate: () => void
}

export interface UseDisplayActivationReturn {
  isActive: boolean
  isActiveRef: React.MutableRefObject<boolean>
  myDisplay: DisplayInfo | null
  myDisplayRef: React.MutableRefObject<DisplayInfo | null>
  allDisplays: DisplayInfo[]
  allDisplaysRef: React.MutableRefObject<DisplayInfo[]>
}

export function useDisplayActivation(
  callbacks: UseDisplayActivationCallbacks
): UseDisplayActivationReturn {
  const [isActive, setIsActive] = useState(false)
  const isActiveRef = useRef(false)

  const [myDisplay, setMyDisplay] = useState<DisplayInfo | null>(null)
  const [allDisplays, setAllDisplays] = useState<DisplayInfo[]>([])
  const myDisplayRef = useRef<DisplayInfo | null>(null)
  const allDisplaysRef = useRef<DisplayInfo[]>([])

  // Stable ref for callbacks so useEffect doesn't re-run
  const callbacksRef = useRef(callbacks)
  callbacksRef.current = callbacks

  // Listen for activation/deactivation from main process
  useEffect(() => {
    const off = api?.onSetActive?.(
      (active: boolean, x?: number, y?: number, isDragging?: boolean) => {
        isActiveRef.current = active
        setIsActive(active)
        if (active && x != null && y != null) {
          callbacksRef.current.onActivate(x, y, !!isDragging)
        }
        if (!active) {
          callbacksRef.current.onDeactivate()
        }
      }
    )

    // The listener is mounted: ask the main process for this overlay's
    // activation. A set-active sent before this point was dropped, and this
    // request is answered with a fresh one.
    api?.petReady?.()

    return () => {
      off?.()
    }
  }, [])

  // Listen for display info updates
  useEffect(() => {
    const off = api?.onDisplaysInfo?.(
      (displays: DisplayInfo[], myId: number) => {
        setAllDisplays(displays)
        allDisplaysRef.current = displays
        const mine = displays.find((d) => d.id === myId) || null
        setMyDisplay(mine)
        myDisplayRef.current = mine
      }
    )
    return () => {
      off?.()
    }
  }, [])

  return {
    isActive,
    isActiveRef,
    myDisplay,
    myDisplayRef,
    allDisplays,
    allDisplaysRef,
  }
}
