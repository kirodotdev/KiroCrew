/**
 * Format an Electron accelerator (e.g. `CommandOrControl+Shift+M`) into the
 * key symbols shown to the user, resolving `CommandOrControl` for THE CURRENT
 * platform. On macOS that is the ⌘⇧ glyph run; on Windows/Linux it is a
 * `Ctrl+Shift+…` word form. Hardcoding the Mac glyphs (as PetContextMenu and
 * ChatPanel used to) told a Windows user to press ⌘ for a chord that is really
 * Ctrl — and the pet's Hide hint is the ONLY recovery path once it is hidden.
 *
 * Machine glyphs / key-cap names only — never translatable copy (this module is
 * listed in eslint.i18n.config.js's no-literal-string ignore set, alongside the
 * other mochi shared/ machine-token modules).
 */

export const IS_MAC =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

export function formatShortcut(accel: string): string {
  if (IS_MAC) {
    return accel
      .replace(/CommandOrControl/g, '⌘')
      .replace(/Command|Super|Meta/g, '⌘')
      .replace(/Option|Alt/g, '⌥')
      .replace(/Control|Ctrl/g, '⌃')
      .replace(/Shift/g, '⇧')
      .replace(/\+/g, '')
  }
  return accel
    .replace(/CommandOrControl/g, 'Ctrl')
    .replace(/Command|Super|Meta/g, 'Win')
    .replace(/Option/g, 'Alt')
}
