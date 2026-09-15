/**
 * Quoting a value as one POSIX shell word.
 *
 * Two features need this for unrelated reasons -- webhook request examples embed
 * caller-supplied ids and URLs in `curl` snippets, and Run-in-terminal hands a
 * snippet to a shell the fence names -- so the idiom lives here rather than
 * being spelled twice and kept in sync by hand.
 */

/**
 * Wrap `value` in single quotes, ending the quote around each embedded
 * apostrophe, emitting an escaped one, and reopening: `it's` becomes
 * `'it'\''s'`. There is no way to escape `'` inside single quotes, so this is
 * the only correct form. Nothing in `value` can end the quoting and be read as
 * command text.
 */
export function posixSingleQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`
}
