/**
 * Ledger content helpers — checkbox line parsing for interactive todo rendering.
 */

export interface CheckboxLine {
  indent: string
  checked: boolean
  text: string
}

/** Regex matching markdown checkbox lines: `- [ ] text` or `* [x] text` with optional leading whitespace. */
const CHECKBOX_RE = /^(\s*[-*]\s)\[([ xX])\]\s(.*)$/

/**
 * Parse a single line of ledger content. Returns CheckboxLine if it's a checkbox,
 * null otherwise.
 */
export function parseCheckboxLine(line: string): CheckboxLine | null {
  const m = CHECKBOX_RE.exec(line)
  if (!m) return null
  return { indent: m[1], checked: m[2].toLowerCase() === 'x', text: m[3] }
}

/**
 * Returns true if the line matches the checkbox pattern.
 */
export function isCheckboxLine(line: string): boolean {
  return CHECKBOX_RE.test(line)
}

/**
 * Toggle a checkbox line's state. Returns the new full line text.
 */
export function toggleCheckboxText(line: string): string {
  return line.replace(/\[([ xX])\]/, (_, state: string) =>
    state === ' ' ? '[x]' : '[ ]'
  )
}
