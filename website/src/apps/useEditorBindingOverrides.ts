/**
 * Live state for the reader's editor-binding overrides, and the recorder
 * Settings → Shortcuts uses to change one.
 *
 * Mirrors `usePanelToggleShortcuts`: recording defers persistence, so the
 * binding in force stays live until a chord lands, Escape cancels, and the
 * captured keystroke is stopped so it cannot also fire something else. A binding
 * may be a modifier-click as well as a chord, so a pointer press with a modifier
 * held is recorded too.
 */
import { useCallback, useEffect, useState } from 'react'
import { isMac } from '../utils/platform'
import { RESERVED_CHORDS, triggerOfEvent } from './editorBindingTriggers'
import {
  EDITOR_BINDING_OVERRIDES_EVENT,
  EDITOR_BINDING_OVERRIDES_KEY,
  loadEditorBindingOverrides,
  resetEditorBindingOverride,
  setEditorBindingOverride,
  type EditorBindingOverrides,
} from './editorBindingOverrides'

/** The overrides, re-read on a same-tab write (event) and another tab's (storage). */
export function useEditorBindingOverrides(): EditorBindingOverrides {
  const [overrides, setOverrides] = useState(() => loadEditorBindingOverrides())
  useEffect(() => {
    const refresh = () => setOverrides(loadEditorBindingOverrides())
    const onStorage = (e: StorageEvent) => { if (e.key === EDITOR_BINDING_OVERRIDES_KEY) refresh() }
    window.addEventListener(EDITOR_BINDING_OVERRIDES_EVENT, refresh)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(EDITOR_BINDING_OVERRIDES_EVENT, refresh)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return overrides
}

/** Why the last recorded trigger was not taken; cleared when recording starts again. */
export type RecordRefusal =
  | { kind: 'reserved'; canonical: string }
  | { kind: 'in-use'; canonical: string; by: string }

export interface EditorBindingRecorder {
  /** The binding being recorded, as `<app>:<id>`, or null. */
  recording: string | null
  refusal: RecordRefusal | null
  start: (app: string, id: string) => void
  cancel: () => void
  /** Unbind a binding. */
  clear: (app: string, id: string) => void
  /** Back to the app's own chord. */
  reset: (app: string, id: string) => void
}

/**
 * The recorder. `inUse(canonical, key)` names the binding that already holds a
 * trigger (its label), or returns null; a trigger another binding holds is
 * refused, as Save and quick search are, so one chord never answers to two.
 */
export function useEditorBindingRecorder(
  inUse: (canonical: string, except: string) => string | null,
): EditorBindingRecorder {
  const [recording, setRecording] = useState<{ app: string; id: string } | null>(null)
  const [refusal, setRefusal] = useState<RecordRefusal | null>(null)

  const start = useCallback((app: string, id: string) => {
    setRefusal(null)
    setRecording({ app, id })
  }, [])
  const cancel = useCallback(() => setRecording(null), [])
  const clear = useCallback((app: string, id: string) => {
    setRecording(null)
    setRefusal(null)
    setEditorBindingOverride(app, id, null)
  }, [])
  const reset = useCallback((app: string, id: string) => {
    setRecording(null)
    setRefusal(null)
    resetEditorBindingOverride(app, id)
  }, [])

  useEffect(() => {
    if (!recording) return
    const key = `${recording.app}:${recording.id}`
    const take = (canonical: string, e: Event) => {
      e.preventDefault()
      e.stopPropagation()
      if (RESERVED_CHORDS.has(canonical)) {
        setRefusal({ kind: 'reserved', canonical })
        return
      }
      const by = inUse(canonical, key)
      if (by !== null) {
        setRefusal({ kind: 'in-use', canonical, by })
        return
      }
      setEditorBindingOverride(recording.app, recording.id, canonical)
      setRefusal(null)
      setRecording(null)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setRecording(null)
        return
      }
      // A bare modifier or a key no binding may take: keep waiting.
      const canonical = triggerOfEvent(e, isMac)
      if (canonical) take(canonical, e)
    }
    const onPointerDown = (e: PointerEvent) => {
      // A plain click is the reader using the page (or the button that cancels);
      // only a click with a modifier held is a trigger.
      const canonical = triggerOfEvent({ button: e.button, shiftKey: e.shiftKey, altKey: e.altKey, metaKey: e.metaKey, ctrlKey: e.ctrlKey }, isMac)
      if (canonical) take(canonical, e)
    }
    window.addEventListener('keydown', onKeyDown, true)
    window.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      window.removeEventListener('keydown', onKeyDown, true)
      window.removeEventListener('pointerdown', onPointerDown, true)
    }
  }, [recording, inUse])

  return {
    recording: recording ? `${recording.app}:${recording.id}` : null,
    refusal,
    start,
    cancel,
    clear,
    reset,
  }
}
