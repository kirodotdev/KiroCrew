/**
 * Display formatting for Electron accelerator strings.
 *
 * The desktop shell's system-wide summon hotkey is stored and registered as an
 * Electron accelerator ("CommandOrControl+Shift+K"). The shortcuts UI renders
 * key caps, so the raw token spelling has to be mapped to the same cap style
 * `formatShortcut()` (useKeyboardShortcuts) uses for the in-app chords: symbol
 * glyphs on macOS, spelled-out names elsewhere.
 */

/** Shape resolved by `electronAPI.getGlobalHotkey()` (see electron/preload.js). */
export interface GlobalHotkeyInfo {
  /** The accelerator actually bound right now; "" when nothing is bound. */
  accelerator: string
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
