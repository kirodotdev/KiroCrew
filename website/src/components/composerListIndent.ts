/**
 * Tab / Shift+Tab indentation for list lines in the plain-text composer.
 *
 * Pure text math: the textarea composer decides WHEN to ask and applies the
 * returned value and caret. The stored prompt stays plain text: an indent is
 * literal spaces at the start of the line, two per level or the parent item's
 * text column under an ordered marker.
 */

import { isListLine, listContentColumn } from './composerListContinuation'

/** One indent level. */
const LIST_INDENT = '  '

/** The new draft and where the caret goes. */
export interface ListIndentEdit {
  value: string
  caret: number
}

function lineBounds(value: string, offset: number): { start: number; end: number } {
  const start = offset === 0 ? 0 : value.lastIndexOf('\n', offset - 1) + 1
  const newline = value.indexOf('\n', offset)
  return { start, end: newline === -1 ? value.length : newline }
}

/** Count of leading spaces and tabs. */
function leadingWidth(line: string): number {
  return line.length - line.replace(/^[ \t]+/, '').length
}

/**
 * The closest list line above `lineStart` indented at most `maxIndent`, skipping
 * other lines, or `null`. A negative `maxIndent` finds nothing.
 */
function nearestListLineAbove(value: string, lineStart: number, maxIndent: number): string | null {
  if (maxIndent < 0 || lineStart === 0) return null
  const lines = value.slice(0, lineStart - 1).split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    if (isListLine(lines[i]) && leadingWidth(lines[i]) <= maxIndent) return lines[i]
  }
  return null
}

/**
 * The edit Tab (`outdent` false) or Shift+Tab (`outdent` true) should make, or
 * `null` when the key is not ours and must keep its default meaning (moving
 * focus): a ranged selection, a line with no list marker, or an outdent on a
 * line that has no leading indentation.
 */
export function listIndentEdit(
  value: string,
  selectionStart: number,
  selectionEnd: number,
  outdent: boolean,
): ListIndentEdit | null {
  if (selectionStart !== selectionEnd) return null
  const caret = Math.max(0, Math.min(selectionStart, value.length))
  const line = lineBounds(value, caret)
  const text = value.slice(line.start, line.end)
  if (!isListLine(text)) return null
  const indent = leadingWidth(text)
  const above = nearestListLineAbove(value, line.start, outdent ? indent - 1 : indent)

  if (!outdent) {
    // Nest under the sibling above at its text column (`1. ` needs 3 spaces to
    // nest under CommonMark, `- ` needs 2); otherwise one two-space level.
    const column = above && leadingWidth(above) === indent ? listContentColumn(above) : null
    const pad = column !== null && column > indent ? ' '.repeat(column - indent) : LIST_INDENT
    return {
      value: value.slice(0, line.start) + pad + value.slice(line.start),
      caret: caret + pad.length,
    }
  }

  // Remove one level: back to the parent's indent when there is a parent above,
  // else a leading tab or up to two leading spaces.
  const removed = above
    ? indent - leadingWidth(above)
    : text.startsWith('\t') ? 1 : text.startsWith(LIST_INDENT) ? 2 : text.startsWith(' ') ? 1 : 0
  if (removed === 0) return null
  // A caret inside the removed whitespace lands at the line start.
  const next = caret - Math.min(removed, caret - line.start)
  return {
    value: value.slice(0, line.start) + value.slice(line.start + removed),
    caret: next,
  }
}

/** True when any line of `value` is a list line, so Tab may indent somewhere. */
export function hasListLine(value: string): boolean {
  return value.split('\n').some(isListLine)
}
