import { IS_MAC, bareModifierFamily, type PttBinding } from './pushToTalk'
import { loadShortcutOverrides, resolveShortcut } from './shortcutRegistry'

const MUTE_SHORTCUT_ID = 'toggle-dictation-mute'

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

/**
 * Could a press of this push-to-talk key be the first key of the mute chord?
 *
 * True when push-to-talk is a lone modifier and the mute chord, as the user
 * has it bound now, uses that same modifier. Pressing the chord then starts
 * with the push-to-talk key going down on its own, which during a recording
 * means "stop". The push-to-talk hook uses this to wait for the release
 * before stopping, so the chord can finish and mute instead.
 */
export function pttKeyCouldStartMuteChord(binding: PttBinding, mac: boolean = IS_MAC): boolean {
  const family = bareModifierFamily(binding)
  if (!family) return false
  const chord = resolveShortcut(MUTE_SHORTCUT_ID, loadShortcutOverrides(), mac ? 'mac' : 'other').primary
  if (!chord) return false
  if (family === 'alt') return !!chord.alt
  if (family === 'shift') return !!chord.shift
  // `mod` is Command on macOS and Control elsewhere.
  if (family === 'meta') return mac && !!chord.mod
  return !!chord.ctrl || (!mac && !!chord.mod)
}
