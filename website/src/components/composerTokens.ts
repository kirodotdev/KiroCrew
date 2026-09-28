// Caret-relative @/$/path token detection for the chat composer.
//
// Every matcher takes the text BEFORE the caret (`value.slice(0, selectionStart)`)
// and returns the query — the run of token chars after the sigil, up to the caret
// — or `null` when the caret is not inside such a token. Anchoring to the
// before-caret slice (rather than the whole textarea value) is what lets the
// file (@), skill ($) and path (./) pickers fire mid-sentence and when trailing
// text or newlines follow the token, instead of only when the token is the last
// thing in the message. A bare sigil at a word boundary returns "" (open the full
// list).

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
 * The path-completion token ending at the caret, used to REPLACE it on select.
 * Group 1 is the word-boundary prefix, as `replaceTokenAtCaret` requires.
 */
export const PATH_TOKEN_RE = /(^|[\s])\.{1,2}\/\S*$/

/**
 * Relative-path (path picker) token at the caret, or null.
 *
 * Unlike the two above the whole token is the query, separator included: the
 * completion is resolved directory-by-directory, so `./src/comp` means "entries
 * of `./src` starting with `comp`" and the token is what carries both halves.
 *
 * A separator is REQUIRED to trigger (`./`, `../`, `../../`) — that is what
 * tells a path apart from an abbreviation or a sentence's final full stop, so
 * "e.g." and "done." never open a menu. `~/` is deliberately absent: the search
 * roots this completes against are project-scoped and bare `$HOME` is not one of
 * them (see `api_file_search` in `handlers/files.py`).
 */
export function matchPathToken(before: string): string | null {
  const m = before.match(/(^|[\s])((?:\.\/|(?:\.\.\/)+)\S*)$/)
  return m ? m[2] : null
}

/**
 * Split a path token into the literal directory prefix the user typed and the
 * partial entry name after it. `./src/comp` → `{ dir: './src/', partial: 'comp' }`;
 * `../` → `{ dir: '../', partial: '' }`.
 *
 * The prefix is kept VERBATIM rather than normalized because it is what the
 * accepted completion is built on (`dir + name`), so the inserted path reads the
 * way the user was typing it. A path token always contains a separator (see
 * `matchPathToken`), so `dir` is never empty.
 */
export function splitPathToken(token: string): { dir: string; partial: string } {
  const cut = token.lastIndexOf('/')
  return { dir: token.slice(0, cut + 1), partial: token.slice(cut + 1) }
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
