import { useEffect } from 'react'

const SHORTCUT_RECORDING_EVENT = 'mc:shortcut-recording'

/**
 * Say that a shortcut recorder is starting, so any other recorder still
 * recording stops (see {@link useCancelWhenAnotherRecorderStarts}). Call it
 * BEFORE setting the recorder's own state: its listener is attached only once
 * it is recording, so it does not hear its own announcement.
 */
export function announceShortcutRecording(): void {
  window.dispatchEvent(new Event(SHORTCUT_RECORDING_EVENT))
}

/**
 * Keeps one shortcut recorder live at a time. Every recorder (Search
 * Everywhere, the panel toggles, the summon hotkey) captures keydown on the
 * window, and `stopPropagation()` does not stop another listener on the same
 * target. Two recorders left running would both take the next chord, and one
 * binding would be overwritten by a press meant for the other.
 */
export function useCancelWhenAnotherRecorderStarts(recording: boolean, cancel: () => void): void {
  useEffect(() => {
    if (!recording) return
    window.addEventListener(SHORTCUT_RECORDING_EVENT, cancel)
    return () => window.removeEventListener(SHORTCUT_RECORDING_EVENT, cancel)
  }, [recording, cancel])
}
