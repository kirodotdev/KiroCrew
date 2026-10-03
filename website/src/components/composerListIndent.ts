/**
 * Tab / Shift+Tab indentation for list lines in the plain-text composer.
 *
 * Pure text math shared by both composer surfaces (the textarea and the
 * Lexical editor), so each surface only decides WHEN to ask and how to apply
 * the returned splice. The stored prompt stays plain text: an indent is two
 * literal spaces at the start of the line.
 */

import { isListLine } from './composerListContinuation'

/** One indent level. */
const LIST_INDENT = '  '

/**
 * A single splice plus the resulting value and caret. `start`/`deleteCount`/
 * `insert` describe the change against the ORIGINAL value, so an editor that
 * applies edits in place (Lexical) does not have to diff the two strings.
 */
export interface ListIndentEdit {
  start: number
  deleteCount: number
  insert: string
  value: string
  selectionStart: number
  selectionEnd: number
}

function lineBounds(value: string, offset: number): { start: number; end: number } {
  const start = offset === 0 ? 0 : value.lastIndexOf('\n', offset - 1) + 1
  const newline = value.indexOf('\n', offset)
  return { start, end: newline === -1 ? value.length : newline }
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

  if (!outdent) {
    const next = caret + LIST_INDENT.length
    return {
      start: line.start,
      deleteCount: 0,
      insert: LIST_INDENT,
      value: value.slice(0, line.start) + LIST_INDENT + value.slice(line.start),
      selectionStart: next,
      selectionEnd: next,
    }
  }

  // Remove one level: a leading tab, or up to two leading spaces.
  const removed = text.startsWith('\t') ? 1 : text.startsWith(LIST_INDENT) ? 2 : text.startsWith(' ') ? 1 : 0
  if (removed === 0) return null
  // A caret inside the removed whitespace lands at the line start.
  const next = caret - Math.min(removed, caret - line.start)
  return {
    start: line.start,
    deleteCount: removed,
    insert: '',
    value: value.slice(0, line.start) + value.slice(line.start + removed),
    selectionStart: next,
    selectionEnd: next,
  }
}
