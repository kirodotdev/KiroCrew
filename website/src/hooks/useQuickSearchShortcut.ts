import { useCallback, useEffect, useState } from 'react'

import { isMac } from '../utils/platform'
import {
  eventKeyToken,
  isValidChord,
  loadQuickSearchConfig,
  QUICK_SEARCH_SHORTCUT_EVENT,
  QUICK_SEARCH_SHORTCUT_KEY,
  saveQuickSearchConfig,
  type QuickSearchChord,
  type QuickSearchConfig,
  type QuickSearchMode,
} from '../lib/quickSearchShortcut'

export interface UseQuickSearchShortcut {
  /** The live preference (re-read on same-tab change and cross-tab storage sync). */
  config: QuickSearchConfig
  /** True while the custom-chord recorder is capturing the next keypress. */
  recording: boolean
  /**
   * Select a preset, or begin recording a custom chord. Preset modes persist
   * immediately; `'custom'` enters recording WITHOUT persisting, so the current
   * binding stays live (and the palette stays reachable) until a valid chord is
   * captured.
   */
  selectMode: (mode: QuickSearchMode) => void
  /** Re-enter recording for the custom chord (e.g. a "change" affordance). */
  startRecording: () => void
  /** Abort recording, leaving the persisted preference untouched. */
  cancelRecording: () => void
}

/**
 * Reactive wrapper over {@link loadQuickSearchConfig} / {@link saveQuickSearchConfig}
 * for the Settings → Shortcuts editor and the Alt+K reference modal. Owns the
 * transient "recording" state and the window-capture keydown listener that turns
 * the next real keypress into a {@link QuickSearchChord}.
 *
 * Recording captures on the window in the CAPTURE phase and `stopPropagation()`s
 * the accepted keypress, so the very chord being recorded can't also fire the
 * palette trigger (or any app shortcut) on the same keystroke. Bare modifier
 * presses and chords with no `mod`/`alt` are ignored (the recorder keeps
 * waiting); Escape cancels.
 */
export function useQuickSearchShortcut(): UseQuickSearchShortcut {
  const [config, setConfig] = useState<QuickSearchConfig>(() => loadQuickSearchConfig())
  const [recording, setRecording] = useState(false)

  // Re-read on same-tab writes (custom event) and other-tab writes (storage).
  useEffect(() => {
    const refresh = () => setConfig(loadQuickSearchConfig())
    const onStorage = (e: StorageEvent) => {
      if (e.key === QUICK_SEARCH_SHORTCUT_KEY) refresh()
    }
    window.addEventListener(QUICK_SEARCH_SHORTCUT_EVENT, refresh)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(QUICK_SEARCH_SHORTCUT_EVENT, refresh)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  const selectMode = useCallback((mode: QuickSearchMode) => {
    if (mode === 'custom') {
      // Defer persistence until a chord is captured — see the interface doc.
      setRecording(true)
      return
    }
    setRecording(false)
    saveQuickSearchConfig({ mode })
  }, [])

  const startRecording = useCallback(() => setRecording(true), [])
  const cancelRecording = useCallback(() => setRecording(false), [])

  useEffect(() => {
    if (!recording) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setRecording(false)
        return
      }
      const token = eventKeyToken(e)
      if (token === null) return // bare modifier — keep waiting for a real key
      const chord: QuickSearchChord = { key: token }
      if (isMac ? e.metaKey : e.ctrlKey) chord.mod = true
      if (e.altKey) chord.alt = true
      if (e.shiftKey) chord.shift = true
      // Require a command/option modifier — otherwise keep waiting rather than
      // installing a bare-key binding that would fire mid-typing.
      if (!isValidChord(chord)) return
      e.preventDefault()
      e.stopPropagation()
      saveQuickSearchConfig({ mode: 'custom', custom: chord })
      setRecording(false)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [recording])

  return { config, recording, selectMode, startRecording, cancelRecording }
}
