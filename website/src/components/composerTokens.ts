// Caret-relative @/$ token detection for the chat composer.
//
// Both matchers take the text BEFORE the caret (`value.slice(0, selectionStart)`)
// and return the query — the run of token chars after the sigil, up to the caret
// — or `null` when the caret is not inside such a token. Anchoring to the
// before-caret slice (rather than the whole textarea value) is what lets the
// file (@) and skill ($) pickers fire mid-sentence and when trailing text or
// newlines follow the token, instead of only when the token is the last thing
// in the message. A bare sigil at a word boundary returns "" (open the full list).

/** @-mention (file picker) query at the caret, or null. */
export function matchFileToken(before: string): string | null {
  const m = before.match(/(^|[\s])@(\S*)$/)
  return m ? m[2] : null
}

/**
 * $-mention (skill picker) query at the caret, or null.
 *
 * The slug charset (`[a-z0-9][a-z0-9/_-]*`) mirrors the backend $skill token
 * grammar — `_DOLLAR_SKILL_PATTERN` in `skills.py` is `\$([a-z0-9][a-z0-9/_-]*)`
 * (lowercase, digits, slash for nested keys, underscore, hyphen) — so the picker
 * triggers on exactly the tokens the backend will resolve, including a digit-led
 * slug. `$PATH`/`$VAR` don't trigger because uppercase isn't in the class, and a
 * bare `$` at a word boundary returns "" so "type `$` then browse" works.
 */
export function matchSkillToken(before: string): string | null {
  const m = before.match(/(^|[\s])\$([a-z0-9][a-z0-9/_-]*)?$/)
  return m ? (m[2] ?? '') : null
}

/**
 * Replace the sigil-token ending at `caret` with `token` (already including the
 * sigil + trailing space), preserving the word-boundary prefix and re-appending
 * any text after the caret. Returns the new value and the caret offset just
 * after the inserted token. Shared by the @ and $ picker onSelect handlers so
 * the caret-relative insertion lives in one tested place. `tokenRe` must anchor
 * the token to the end of the before-caret slice (a trailing `$`).
 */
export function replaceTokenAtCaret(
  value: string,
  caret: number,
  tokenRe: RegExp,
  token: string,
): { value: string; caret: number } {
  const before = value.slice(0, caret).replace(tokenRe, (_m, prefix: string) => `${prefix}${token}`)
  return { value: before + value.slice(caret), caret: before.length }
}
