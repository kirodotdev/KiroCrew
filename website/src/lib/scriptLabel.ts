/**
 * Name a single refused letter so it cannot pass for an allowed one.
 *
 * A Cyrillic or Greek capital renders exactly like the Latin capital a slug rule
 * allows, so a message that shows only the glyph shows a character that looks fine.
 * The refusal copy therefore puts the letter's script and code point beside it:
 * `“О” (Cyrillic letter, U+041E)`.
 *
 * The script labels are a small catalog-backed set: Latin, Greek and Cyrillic, the
 * scripts whose capitals are drawn alike. A letter from any other script is labelled
 * a plain "character", and its code point still sets it apart.
 */
import { i18nT } from '../i18n/t'

const SCRIPT_LABELS: ReadonlyArray<readonly [RegExp, () => string]> = [
  [/^\p{Script=Latin}$/u, () => i18nT('lib.scriptLabel.latin')],
  [/^\p{Script=Greek}$/u, () => i18nT('lib.scriptLabel.greek')],
  [/^\p{Script=Cyrillic}$/u, () => i18nT('lib.scriptLabel.cyrillic')],
]

/** The code point of a single character as Unicode writes it: `U+041E`, `U+1D400`. */
export function codePointLabel(char: string): string {
  const cp = char.codePointAt(0) ?? 0
  return `U+${cp.toString(16).toUpperCase().padStart(4, '0')}`
}

/** The app-language label for the script of one letter, e.g. "Cyrillic letter". */
export function scriptLabel(char: string): string {
  for (const [pattern, label] of SCRIPT_LABELS) {
    if (pattern.test(char)) return label()
  }
  return i18nT('lib.scriptLabel.other')
}

/** The interpolation values a `*_letter` refusal key takes beside `char`. */
export function refusedLetterParams(char: string): { char: string; script: string; code: string } {
  return { char, script: scriptLabel(char), code: codePointLabel(char) }
}
