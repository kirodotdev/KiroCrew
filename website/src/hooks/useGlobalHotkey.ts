import { useCallback, useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { isElectron } from '../lib/electron'
import { isMac } from '../utils/platform'
import { acceleratorFromEvent, type GlobalHotkeyInfo } from '../lib/globalHotkey'
import { announceShortcutRecording, useCancelWhenAnotherRecorderStarts } from './useExclusiveShortcutRecording'

const GLOBAL_HOTKEY_QUERY_KEY = ['global-hotkey']

/**
 * The desktop shell's system-wide summon hotkey, as reported by the main
 * process (registration can degrade to the platform default or to nothing when
 * a key is taken by another application — see electron/global-hotkey.js).
 *
 * Returns null in a plain browser and on a desktop build without the bridge:
 * there is no desktop shell to configure, so there is nothing to show. On a
 * desktop build it returns the info even when the hotkey is UNBOUND
 * (`accelerator: ""`, the fresh-install opt-out default), so the Settings UI
 * can show the summon shortcut as "Off" with how-to-enable guidance rather than
 * hide it — a hidden switch cannot be opted back into. Callers that must not
 * advertise a dead chord check `accelerator` themselves. The binding changes
 * only through {@link useGlobalHotkeyRecorder}, which writes the shell's answer
 * into this query, hence `staleTime: Infinity`.
 */
export function useGlobalHotkey(): GlobalHotkeyInfo | null {
  const api = window.electronAPI
  const available = isElectron && typeof api?.getGlobalHotkey === 'function'
  const { data } = useQuery({
    queryKey: GLOBAL_HOTKEY_QUERY_KEY,
    // `available` gates the query, so the bridge is present when this runs.
    queryFn: () => api!.getGlobalHotkey!(),
    enabled: available,
    staleTime: Infinity,
  })
  if (!available || !data) return null
  return data
}

/**
 * Whether this window can change the summon hotkey itself. False in a plain
 * browser, on an older desktop shell, and in a window connected to a remote
 * host (the shell withholds the setter there): the desktop config file is then
 * the way to change it.
 */
export function canRecordGlobalHotkey(): boolean {
  return isElectron && typeof window.electronAPI?.setGlobalHotkey === 'function'
}

export interface UseGlobalHotkeyRecorder {
  /** True while the next key press is being recorded as the new hotkey. */
  recording: boolean
  /** Begin recording. The current hotkey stays bound until a valid chord lands. */
  startRecording: () => void
  /** Abort recording without changing the hotkey. */
  cancelRecording: () => void
  /** Turn the hotkey off. */
  clear: () => void
  /** True when the shell refused or failed the last change; the hotkey is then unchanged. */
  failed: boolean
}

/**
 * Editor state for the summon hotkey in Settings → Shortcuts. Mirrors
 * {@link usePanelToggleShortcuts}: a window-capture keydown listener turns the
 * next real chord into the binding and stops it there, so the chord being
 * recorded cannot also fire an app shortcut. A press that is not recordable is
 * ignored (keep waiting); Escape cancels. The shell stores the chord and binds
 * it at once, and its answer replaces {@link useGlobalHotkey}'s cached value.
 *
 * shortcut: another open window keeps its cached value until it reloads; add a
 * shell broadcast when two windows editing shortcuts at once becomes real.
 */
export function useGlobalHotkeyRecorder(): UseGlobalHotkeyRecorder {
  const queryClient = useQueryClient()
  const [recording, setRecording] = useState(false)
  const [failed, setFailed] = useState(false)

  const apply = useCallback((accelerator: string) => {
    setRecording(false)
    setFailed(false)
    const set = window.electronAPI?.setGlobalHotkey
    if (!set) return
    set(accelerator).then(
      info => queryClient.setQueryData(GLOBAL_HOTKEY_QUERY_KEY, info),
      () => setFailed(true),
    )
  }, [queryClient])

  const startRecording = useCallback(() => {
    announceShortcutRecording()
    setRecording(true)
  }, [])
  const cancelRecording = useCallback(() => setRecording(false), [])
  const clear = useCallback(() => apply(''), [apply])
  useCancelWhenAnotherRecorderStarts(recording, cancelRecording)

  useEffect(() => {
    if (!recording) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setRecording(false)
        return
      }
      const accelerator = acceleratorFromEvent(e, isMac)
      if (accelerator === null) return // not a recordable chord yet — keep waiting
      e.preventDefault()
      e.stopPropagation()
      apply(accelerator)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [recording, apply])

  return { recording, startRecording, cancelRecording, clear, failed }
}
