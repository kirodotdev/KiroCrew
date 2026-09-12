import { useCallback, useEffect, useRef } from 'react'
import type { RefObject, UIEvent } from 'react'

/**
 * Remember the reading position per file within a session: the viewer body's
 * scroll offset is saved (debounced) as the user reads and restored when the
 * same file is opened again, so switching files and coming back does not throw
 * away their place.
 *
 * NAMED `useFileScrollMemory`, not `useScrollMemory`: the dashboard already has
 * `website/src/hooks/useScrollMemory.ts` for side-panel document tabs, keyed by
 * (chat slot, tab id). That one is not reusable here — this keys by absolute
 * file path — but two same-named hooks with different identities would be a
 * genuine trap for the next reader.
 *
 * IN-MEMORY ONLY, adopting the reasoning of that existing hook rather than
 * contradicting it: a persisted pixel offset restores into whatever the file
 * contains NEXT time, and a file on disk can be rewritten between visits (by an
 * editor, an agent, a git checkout), so the offset would scroll to an unrelated
 * place with false confidence. Starting a reloaded file at the top is correct.
 * An earlier revision of this hook persisted to localStorage; that was both a
 * collision with the above policy and wrong for the same reason.
 */

/** Module-scope so the memory survives the viewer unmounting on a file switch,
 * which is the whole point; it dies with the page, as intended. */
const positions = new Map<string, number>()

/** FIFO cap: one number per absolute path, so this is generous. It exists only
 * so a session left open for weeks cannot grow the map unbounded. */
const MAX_REMEMBERED_FILES = 300

/** Layout settles asynchronously (markdown, images), so restoring retries
 * briefly until the offset sticks or the attempts run out. */
const RESTORE_ATTEMPTS = 14
const RESTORE_INTERVAL_MS = 120

function remember(path: string, top: number): void {
  if (!positions.has(path) && positions.size >= MAX_REMEMBERED_FILES) {
    const oldest = positions.keys().next().value
    if (oldest !== undefined) positions.delete(oldest)
  }
  positions.set(path, top)
}

export function useFileScrollMemory(
  bodyRef: RefObject<HTMLElement | null>,
  filePath: string | null,
  contentReady: boolean,
): (e: UIEvent<HTMLElement>) => void {
  const saveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const onScroll = useCallback((e: UIEvent<HTMLElement>) => {
    if (!filePath) return
    const el = e.currentTarget
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => remember(filePath, el.scrollTop), 350)
  }, [filePath])

  useEffect(() => () => clearTimeout(saveTimer.current), [])

  useEffect(() => {
    if (!filePath || !contentReady) return
    const target = positions.get(filePath) || 0
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
