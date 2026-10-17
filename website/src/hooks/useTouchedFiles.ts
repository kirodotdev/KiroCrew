import { safeSetItem } from '../utils/safeStorage'
import { useState, useCallback, useEffect, useRef, useMemo } from 'react'

export interface TouchedFile {
  path: string
  /** First seen timestamp (Date.now() at add time) */
  ts: number
  /** Last write timestamp — updates when agent writes to this file again */
  lastWrite?: number
  /** How the file was accessed:
   *  - 'history' — viewed by the user (file viewer panel, etc.)
   *  - 'tool'    — read/written by the agent during a turn
   */
  source: 'history' | 'tool'
}

const STORAGE_PREFIX = 'kirocrew:touched-files:'
const WATERMARK_SUFFIX = ':toolClearedAt'

/**
 * Tracks files touched during a chat session and persists per-session to
 * localStorage. Files surface in the activity Files tab.
 *
 * Clear behavior:
 *   - clearBySource('tool') wipes agent-touched files AND records a watermark.
 *     Subsequent message scans skip files referenced by messages older than
 *     the watermark (via shouldScanAdd) so cleared files stay gone — even
 *     after a page refresh, since the watermark is also persisted to
 *     localStorage alongside the files.
 *   - clearBySource('history') wipes user-opened files. No watermark — there
 *     is no auto-scan source for history files.
 *   - clear() (legacy) wipes everything and records the tool watermark.
 */
export function useTouchedFiles(sessionKey: string | undefined) {
  const [files, setFiles] = useState<TouchedFile[]>([])
  const keyRef = useRef(sessionKey)

  /** Watermark: tool files referenced by messages with ts <= this are
   *  ignored by the message scan. Persisted per-session so clears survive
   *  page refresh / component remount. Reset on session change. */
  const toolClearedAtRef = useRef(0)

  useEffect(() => {
    keyRef.current = sessionKey
    if (!sessionKey) { toolClearedAtRef.current = 0; setFiles([]); return }
    try {
      const raw = localStorage.getItem(STORAGE_PREFIX + sessionKey)
      setFiles(raw ? JSON.parse(raw) : [])
    } catch { setFiles([]) }
    // Restore watermark — without this, refreshing the page after clearing
    // tool files would re-add them on the next message scan.
    try {
      const wm = localStorage.getItem(STORAGE_PREFIX + sessionKey + WATERMARK_SUFFIX)
      toolClearedAtRef.current = wm ? Number(wm) || 0 : 0
    } catch { toolClearedAtRef.current = 0 }
  }, [sessionKey])

  const persist = useCallback((next: TouchedFile[]) => {
    if (!keyRef.current) return
    try { safeSetItem(STORAGE_PREFIX + keyRef.current, JSON.stringify(next)) } catch { /* quota */ }
  }, [])

  const persistWatermark = useCallback((ts: number) => {
    if (!keyRef.current) return
    try { safeSetItem(STORAGE_PREFIX + keyRef.current + WATERMARK_SUFFIX, String(ts)) } catch { /* quota */ }
  }, [])

  const addFile = useCallback((path: string, source: 'history' | 'tool' = 'history') => {
    setFiles(prev => {
      const existing = prev.find(f => f.path === path)
      if (existing) {
        if (source === 'history' && existing.source === 'tool') {
          const next = prev.map(f => f.path === path ? { ...f, source: 'history' as const } : f)
          persist(next)
          return next
        }
        if (source === 'tool') {
          const next = prev.map(f => f.path === path ? { ...f, lastWrite: Date.now() } : f)
          persist(next)
          return next
        }
        return prev
      }
      const next = [...prev, { path, ts: Date.now(), lastWrite: source === 'tool' ? Date.now() : undefined, source }]
      persist(next)
      return next
    })
  }, [persist])

  const removeFile = useCallback((path: string) => {
    setFiles(prev => {
      const next = prev.filter(x => x.path !== path)
      persist(next)
      return next
    })
  }, [persist])

  const clearBySource = useCallback((source: 'history' | 'tool') => {
    if (source === 'tool') {
      const now = Date.now()
      toolClearedAtRef.current = now
      persistWatermark(now)
    }
    setFiles(prev => {
      const next = prev.filter(f => f.source !== source)
      persist(next)
      return next
    })
  }, [persist, persistWatermark])

  const clear = useCallback(() => {
    const now = Date.now()
    toolClearedAtRef.current = now
    persistWatermark(now)
    setFiles([])
    if (keyRef.current) localStorage.removeItem(STORAGE_PREFIX + keyRef.current)
  }, [persistWatermark])

  /** Returns true if a tool-sourced file should be added by the message scan.
   *  False if tool files were cleared after the given message's timestamp. */
  const shouldScanAdd = useCallback((messageTs: number) => {
    return messageTs > toolClearedAtRef.current
  }, [])

  return useMemo(
    () => ({ files, addFile, removeFile, clearBySource, clear, shouldScanAdd }),
    [files, addFile, removeFile, clearBySource, clear, shouldScanAdd],
  )
}
