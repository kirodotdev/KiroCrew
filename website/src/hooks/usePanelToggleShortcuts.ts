import { useCallback, useEffect, useState } from 'react'

import { isMac } from '../utils/platform'
import { eventKeyToken, isValidChord, type QuickSearchChord as Chord } from '../lib/quickSearchShortcut'
import {
  loadPanelToggleOverrides,
  PANEL_TOGGLE_SHORTCUTS_EVENT,
  PANEL_TOGGLE_SHORTCUTS_KEY,
  resolvePanelToggleBindings,
  setPanelToggleBinding,
  type PanelToggleId,
} from '../lib/panelToggleShortcuts'

export interface UsePanelToggleShortcuts {
  /** The live resolved binding per panel (`null` = unbound), re-read on change. */
  bindings: Record<PanelToggleId, Chord | null>
  /** The panel whose chord is currently being recorded, or null. */
  recordingId: PanelToggleId | null
  /** Begin recording a new chord for `id`. Leaves the current binding live until a valid chord lands. */
  startRecording: (id: PanelToggleId) => void
  /** Abort recording without changing any binding. */
  cancelRecording: () => void
  /** Clear `id` to unbound (persisted, so it stays cleared across reloads). */
  clear: (id: PanelToggleId) => void
}

/**
 * Reactive editor state for the three panel-toggle shortcuts (Settings → Shortcuts
 * and the Alt+K modal). Mirrors {@link useQuickSearchShortcut}: it owns the
 * transient "recording" state and the window-capture keydown listener that turns
 * the next real keypress into a chord, `stopPropagation()`-ing it so the very
 * chord being recorded can't also fire an app shortcut on the same keystroke.
 * Bare keys / modifier-only presses are ignored (keep waiting); Escape cancels.
 */
export function usePanelToggleShortcuts(): UsePanelToggleShortcuts {
  const [overrides, setOverrides] = useState(() => loadPanelToggleOverrides())
  const [recordingId, setRecordingId] = useState<PanelToggleId | null>(null)

  // Re-read on same-tab writes (custom event) and other-tab writes (storage).
  useEffect(() => {
    const refresh = () => setOverrides(loadPanelToggleOverrides())
    const onStorage = (e: StorageEvent) => { if (e.key === PANEL_TOGGLE_SHORTCUTS_KEY) refresh() }
    window.addEventListener(PANEL_TOGGLE_SHORTCUTS_EVENT, refresh)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(PANEL_TOGGLE_SHORTCUTS_EVENT, refresh)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  const startRecording = useCallback((id: PanelToggleId) => setRecordingId(id), [])
  const cancelRecording = useCallback(() => setRecordingId(null), [])
  const clear = useCallback((id: PanelToggleId) => {
    setRecordingId(null)
    setPanelToggleBinding(id, null)
  }, [])

  useEffect(() => {
    if (recordingId === null) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setRecordingId(null)
        return
      }
      const token = eventKeyToken(e)
      if (token === null) return // bare modifier — keep waiting for a real key
      const chord: Chord = { key: token }
      if (isMac ? e.metaKey : e.ctrlKey) chord.mod = true
      if (e.altKey) chord.alt = true
      if (e.shiftKey) chord.shift = true
      // Require a command/option modifier — otherwise keep waiting rather than
      // installing a bare-key binding that would fire mid-typing.
      if (!isValidChord(chord)) return
      e.preventDefault()
      e.stopPropagation()
      setPanelToggleBinding(recordingId, chord)
      setRecordingId(null)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [recordingId])

  return { bindings: resolvePanelToggleBindings(overrides), recordingId, startRecording, cancelRecording, clear }
}
