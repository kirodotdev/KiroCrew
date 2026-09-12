/** Shared diff parsing utilities used by ToolInputText and DiffBlock. */


/** Detect whether text contains unified diff content.
 *  Requires @@ hunk headers or paired ---/+++ file headers to avoid
 *  false positives on markdown lists, negative numbers, and CLI flags.
 *  Note: YAML front matter (---) + markdown +++ headings could false-positive,
 *  but this is unlikely in tool input context where content is code/JSON. */
export function isDiffText(text: string): boolean {
  const lines = text.split('\n')
  return lines.some(l => /^@@\s/.test(l)) ||
    (lines.some(l => /^--- /.test(l)) && lines.some(l => /^\+\+\+ /.test(l)))
}
