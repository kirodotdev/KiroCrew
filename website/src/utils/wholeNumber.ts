/**
 * Read a settings field's text as a whole number, or `null` when the text is
 * not one.
 *
 * The WHOLE string is read. `parseInt` reads only a leading run of digits, so
 * "50.9" gives 50, "8O" gives 8 and "1e3" gives 1: a number the reader never
 * typed, saved as if they had. Here each of those is `null`, and the caller
 * shows its own invalid-input path instead of saving.
 *
 * Accepted: digits, with optional surrounding whitespace and an optional
 * all-zero fraction ("50", " 50 ", "50.0"), which are the shapes a number input
 * hands over for a whole number. The range check stays with the caller, which
 * knows the field's bounds.
 */
export function parseWholeNumber(raw: string): number | null {
  const text = raw.trim()
  if (!/^[0-9]+(\.0*)?$/.test(text)) return null
  const n = Number(text)
  return Number.isSafeInteger(n) ? n : null
}
