/**
 * Display formatting for Electron accelerator strings.
 *
 * The desktop shell's system-wide summon hotkey is stored and registered as an
 * Electron accelerator ("CommandOrControl+Shift+K"). The shortcuts UI renders
 * key caps, so the raw token spelling has to be mapped to the same cap style
 * `formatShortcut()` (useKeyboardShortcuts) uses for the in-app chords: symbol
 * glyphs on macOS, spelled-out names elsewhere.
 */

import { chordLetter } from './shortcutRegistry'

/** Shape resolved by `electronAPI.getGlobalHotkey()` (see electron/preload.js). */
export interface GlobalHotkeyInfo {
  /** The accelerator actually bound right now; "" when nothing is bound. */
  accelerator: string
  /** What the stored preference asked for; "" when turned off. Absent on an older shell. */
  requested?: string
  /** The platform default the desktop shell would fall back to. */
  default: string
}

/**
 * Every Electron modifier token, including aliases, mapped to its display cap.
 * A token missing here is treated as a plain key (uppercased when a single
 * character, verbatim otherwise) — mirroring how Electron itself parses the
 * accelerator: everything that is not a known modifier is the key.
 */
const MODIFIER_CAPS: Record<string, { mac: string; other: string }> = {
  CommandOrControl: { mac: '\u2318', other: 'Ctrl' },
  CmdOrCtrl: { mac: '\u2318', other: 'Ctrl' },
  Command: { mac: '\u2318', other: '\u2318' },
  Cmd: { mac: '\u2318', other: '\u2318' },
  Meta: { mac: '\u2318', other: 'Win' },
  Super: { mac: '\u2318', other: 'Win' },
  Control: { mac: '\u2303', other: 'Ctrl' },
  Ctrl: { mac: '\u2303', other: 'Ctrl' },
  Alt: { mac: '\u2325', other: 'Alt' },
  Option: { mac: '\u2325', other: 'Alt' },
  // AltGr deliberately has no row: its display form IS the token, so it takes
  // the verbatim multi-character key path below on every platform.
  Shift: { mac: '\u21e7', other: 'Shift' },
}

/**
 * The Electron accelerator a keydown records for the summon hotkey, or `null`
 * when the press is not one to record yet (a bare modifier, a key outside the
 * set below, or no Command, Control or Alt held). The hotkey is grabbed from
 * every program, so a bare key or a Shift-only chord would swallow plain typing
 * system-wide; the desktop shell refuses those too (`parseRecordedHotkey` in
 * electron/global-hotkey.js), and it accepts exactly the spelling built here:
 * Cmd, Ctrl, Alt, Shift in that order, then the key. The key is a letter, a
 * top-row digit, F1 to F24, or a named key whose `KeyboardEvent.code` is its
 * accelerator name (an arrow drops the "Arrow").
 *
 * shortcut: punctuation and numpad keys are not recordable, because their
 * accelerator tokens depend on the keyboard layout. The config file still takes
 * any Electron accelerator; widen this set together with the shell's check.
 */
export function acceleratorFromEvent(
  e: Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  mac: boolean,
): string | null {
  // Off macOS, Meta is the Windows key: the recorded chord would not name it.
  if (e.metaKey && !mac) return null
  if (!e.metaKey && !e.ctrlKey && !e.altKey) return null
  const code = e.code
  const key = chordLetter(e.key, code)?.toUpperCase()
    ?? (/^Digit\d$/.test(code) ? code.slice(5) : null)
    ?? (/^(?:F(?:[1-9]|1\d|2[0-4])|Space|Tab|Enter|Backspace|Delete|Insert|Home|End|PageUp|PageDown)$/.test(code) ? code : null)
    ?? (/^Arrow(?:Up|Down|Left|Right)$/.test(code) ? code.slice(5) : null)
  if (!key) return null
  return [e.metaKey && 'Cmd', e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', key]
    .filter(Boolean)
    .join('+')
}

/**
 * Split an Electron accelerator into display key caps, one cap per token.
 * Returns [] for an empty/unbound accelerator so callers can hide the row.
 */
export function formatAcceleratorKeys(accelerator: string, mac: boolean): string[] {
  if (!accelerator) return []
  return accelerator
    .split('+')
    .filter(Boolean)
    .map(token => {
      const mod = MODIFIER_CAPS[token]
      if (mod) return mac ? mod.mac : mod.other
      return token.length === 1 ? token.toUpperCase() : token
    })
}
