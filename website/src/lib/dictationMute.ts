/**
 * The window event the `toggle-dictation-mute` shortcut dispatches.
 *
 * The keyboard handler cannot tell which composer holds the microphone, so it
 * broadcasts and the composer that owns the live recording answers. Every
 * other composer ignores it, and with nothing recording the chord does nothing.
 */
export const DICTATION_MUTE_TOGGLE_EVENT = 'dictation-mute-toggle'

export function requestDictationMuteToggle(): void {
  window.dispatchEvent(new Event(DICTATION_MUTE_TOGGLE_EVENT))
}
