/** Markdown paragraph break: separates stacked quotes, and leaves the caret
 *  on its own line under the quote for the question. */
const BLANK_LINE = '\n\n'

/** Append `text` to a composer draft as a markdown blockquote. Multiple quotes
 *  stack: each lands below the existing content, separated by a blank line,
 *  and a trailing blank line leaves the caret ready for the question. One
 *  shaping for every surface that quotes — the main composer, a pane's, and
 *  the Side Chat seed. */
export function quoteIntoDraft(prev: string, text: string): string {
  const quoted = text.split('\n').map(line => '> ' + line).join('\n')
  if (!prev.trim()) return quoted + BLANK_LINE
  return prev.trimEnd() + BLANK_LINE + quoted + BLANK_LINE
}
