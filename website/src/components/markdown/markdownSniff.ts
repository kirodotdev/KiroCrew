/**
 * Whether an untagged or generic-tagged fence holds a markdown document, so
 * the chat transcript may offer it the Formatted | Raw card (#13152). Any
 * fence that names a real language is never sniffed.
 *
 * The bar is "clearly markdown", and a miss is fail-safe: the fence stays a
 * copyable code block, which is the pre-#13152 behaviour.
 */

/** Tags that name no language, so the fence's content decides. */
const GENERIC_LANGS = new Set(['', 'text', 'txt', 'plaintext', 'plain'])

/** Signals that ordinary code, YAML, shell transcripts, diffs and test output
 *  also produce: a `# comment` at column 0 reads as a heading, a YAML or
 *  diff line as a list item, a `---` separator as a thematic break, a shell
 *  continuation prompt as a blockquote. They count, but never on their own. */
const WEAK_SIGNALS = [
  /^#{1,6}[ \t]+\S/m, // ATX heading
  /^ {0,3}(?:[-*+]|\d+\.)[ \t]+\S/m, // list item (CommonMark caps indent at 3)
  /^>[ \t]+\S/m, // blockquote
  /^(?:\*{3,}|-{3,}|_{3,})[ \t]*$/m, // thematic break
]

/** Signals code does not produce. A link must look like markdown, not an
 *  indexed call: `handlers[name](req)` has an identifier or `]` before the
 *  `[` and no URL-ish target, so it is not a link. */
const STRONG_SIGNALS = [
  /^ {0,3}(?:\|[ \t]*)?:?-+:?[ \t]*\|[-:| \t]*$/m, // table delimiter row
  /(?<![\w\])])!?\[[^[\]\n]+\]\((?:https?:\/\/|mailto:|\.{0,2}\/|#)[^()\s]*\)/, // link or image
]

// The content is untrusted, so every pattern stays on one line ([ \t], never
// \s) with no two adjacent runs that can match the same characters: each test
// is linear in the content.

function countHits(patterns: RegExp[], content: string, cap: number): number {
  let hits = 0
  for (const re of patterns) {
    if (re.test(content) && ++hits >= cap) break
  }
  return hits
}

/** Two DISTINCT signals, at least one of them strong. */
export function looksLikeMarkdown(content: string): boolean {
  const strong = countHits(STRONG_SIGNALS, content, 2)
  if (strong === 0) return false
  return strong >= 2 || countHits(WEAK_SIGNALS, content, 1) >= 1
}

/** True when a fence tagged `lang` should be sniffed at all. */
export function isGenericLang(lang: string | undefined): boolean {
  return GENERIC_LANGS.has((lang ?? '').toLowerCase())
}
