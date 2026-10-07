// Pure text helpers for the meeting note. Kept out of the component so the draft
// hook (which inserts a pasted image's markdown after the panel may have closed)
// and the panel share one implementation without the hook importing a component.

/**
 * Insert *snippet* into *text* at *caret*, on its own line.
 *
 * Pasted images are block content: dropping one mid-sentence would split the
 * sentence around it. Exported because the caret arithmetic is the part worth
 * testing, and it is pure.
 */
export function insertBlock(text: string, caret: number, snippet: string): string {
  const at = Math.max(0, Math.min(caret, text.length))
  const before = text.slice(0, at)
  const after = text.slice(at)
  // Only add separators that are missing, so repeated pastes do not accumulate
  // blank lines.
  const lead = before === '' || before.endsWith('\n') ? '' : '\n'
  const trail = after === '' || after.startsWith('\n') ? '' : '\n'
  return `${before}${lead}${snippet}${trail}${after}`
}

/** The markdown for one stored image. `alt` may be empty when a meeting is not live. */
export function imageSnippet(alt: string, src: string): string {
  return `![${alt}](${src})`
}
