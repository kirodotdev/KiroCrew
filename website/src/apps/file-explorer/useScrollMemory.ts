import { useCallback, useEffect, useRef } from 'react'
import type { RefObject, UIEvent } from 'react'

import { SCROLL_STORAGE_KEY } from './constants'

const MAX_REMEMBERED_FILES = 300
/** Layout settles asynchronously (markdown, images), so restoring retries
 * briefly until the offset sticks or the attempts run out. */
const RESTORE_ATTEMPTS = 14
const RESTORE_INTERVAL_MS = 120

function loadMap(): Record<string, number> {
  try {
    return JSON.parse(localStorage.getItem(SCROLL_STORAGE_KEY) || '{}')
  } catch {
    return {}
  }
}

function saveOffset(path: string, top: number) {
  try {
    const map = loadMap()
    map[path] = top
    const keys = Object.keys(map)
    // FIFO cap so the map cannot grow unbounded across months of browsing.
    while (keys.length > MAX_REMEMBERED_FILES) {
      const oldest = keys.shift()
      if (oldest !== undefined) delete map[oldest]
    }
    localStorage.setItem(SCROLL_STORAGE_KEY, JSON.stringify(map))
  } catch {
    // Quota/serialisation failures only lose the convenience, never the view.
  }
}

/**
 * Remember the reading position per file: the viewer body's scroll offset is
 * saved (debounced) as the user reads and restored when the same file is
 * opened again — surviving tab switches, app switches, and full restarts.
 */
export function useScrollMemory(
  bodyRef: RefObject<HTMLElement | null>,
  filePath: string | null,
  contentReady: boolean,
): (e: UIEvent<HTMLElement>) => void {
  const saveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const onScroll = useCallback((e: UIEvent<HTMLElement>) => {
    if (!filePath) return
    const el = e.currentTarget
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => saveOffset(filePath, el.scrollTop), 350)
  }, [filePath])

  useEffect(() => () => clearTimeout(saveTimer.current), [])

  useEffect(() => {
    if (!filePath || !contentReady) return
    const target = loadMap()[filePath] || 0
    if (!target) return
    let attempts = 0
    const timer = setInterval(() => {
      const el = bodyRef.current
      attempts += 1
      if (el) {
        el.scrollTop = target
        if (Math.abs(el.scrollTop - target) < 4 || attempts > RESTORE_ATTEMPTS) {
          clearInterval(timer)
        }
      } else if (attempts > RESTORE_ATTEMPTS) {
        clearInterval(timer)
      }
    }, RESTORE_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [bodyRef, filePath, contentReady])

  return onScroll
}
