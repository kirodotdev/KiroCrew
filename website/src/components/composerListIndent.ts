/**
 * Tab / Shift+Tab indentation for list lines in the plain-text composer.
 *
 * Pure text math: the textarea composer decides WHEN to ask and applies the
 * returned value and selection. The stored prompt stays plain text: an indent is
 * literal spaces after the line's own leading whitespace, two per level or the
 * parent item's text column under an ordered marker.
 */

import { isListLine, listContentColumn } from './composerListContinuation'

/** One indent level. */
const LIST_INDENT = '  '

/** The new draft and the selection to restore; `caret === end` when collapsed. */
export interface ListIndentEdit {
  value: string
  caret: number
  end: number
}

/** `removed` characters at `at` replaced by `insert`. */
interface Splice {
  at: number
  removed: number
  insert: string
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
  if (maxIndent < 0) return null
  // Line by line upwards, stopping at the first match: a selection asks once per line.
  for (let end = lineStart - 1; end > 0;) {
    const start = value.lastIndexOf('\n', end - 1) + 1
    const line = value.slice(start, end)
    if (isListLine(line) && leadingWidth(line) <= maxIndent) return line
    end = start - 1
  }
  return null
}

/** Where `offset` lands once `splice` is applied; inside the removed text it lands at `at`. */
function shift(offset: number, splice: Splice): number {
  if (offset < splice.at) return offset
  return Math.max(splice.at, offset - splice.removed) + splice.insert.length
}

/**
 * What Tab (`outdent` false) or Shift+Tab (`outdent` true) does to the list line
 * that starts at `lineStart` with `indent` leading spaces and tabs, or `null` for
 * an outdent on a line that has no leading indentation.
 */
function lineSplice(value: string, lineStart: number, indent: number, outdent: boolean): Splice | null {
  const above = nearestListLineAbove(value, lineStart, outdent ? indent - 1 : indent)

  if (!outdent) {
    // Nest under the sibling above at its text column (`1. ` needs 3 spaces to
    // nest under CommonMark, `- ` needs 2); otherwise one two-space level. The
    // pad goes after the line's own indentation, so a leading tab keeps its stop.
    const column = above && leadingWidth(above) === indent ? listContentColumn(above) : null
    const pad = column !== null && column > indent ? ' '.repeat(column - indent) : LIST_INDENT
    return { at: lineStart + indent, removed: 0, insert: pad }
  }

  // Remove one level next to the marker, as Tab added it: whatever lies past the
  // parent's indent when there is a parent above, else a trailing tab or up to
  // two trailing spaces.
  if (above) {
    const keep = leadingWidth(above)
    return { at: lineStart + keep, removed: indent - keep, insert: '' }
  }
  if (indent === 0) return null
  const removed = value.endsWith(LIST_INDENT, lineStart + indent) ? 2 : 1
  return { at: lineStart + indent - removed, removed, insert: '' }
}

/**
 * `lead`'s step taken by a list line beside or under it: Tab adds the same pad
 * after the line's own indentation, Shift+Tab removes the same text from the end
 * of it, or `null` when the indentation does not end with that text.
 */
function followSplice(value: string, lead: Splice, lineStart: number, indent: number): Splice | null {
  const at = lineStart + indent
  if (lead.insert) return { at, removed: 0, insert: lead.insert }
  const cut = value.slice(lead.at, lead.at + lead.removed)
  return value.endsWith(cut, at) ? { at: at - cut.length, removed: cut.length, insert: '' } : null
}

/**
 * The edit Tab (`outdent` false) or Shift+Tab (`outdent` true) should make on the
 * caret's line, or on every list line a selection over several lines touches, or
 * `null` when the key is not ours and must keep its default meaning (moving
 * focus): a selection inside one line, no list line to change, or an outdent
 * where no list line has leading indentation. A selection that ends at the start
 * of a line leaves that line alone.
 */
export function listIndentEdit(
  value: string,
  selectionStart: number,
  selectionEnd: number,
  outdent: boolean,
): ListIndentEdit | null {
  let caret = Math.max(0, Math.min(selectionStart, value.length))
  let end = Math.min(selectionEnd, value.length)
  if (caret !== end && !value.slice(caret, end).includes('\n')) return null
  const starts = [lineBounds(value, caret).start]
  for (let nl = value.indexOf('\n', starts[0]); nl !== -1 && nl < end - 1; nl = value.indexOf('\n', nl + 1)) {
    starts.push(nl + 1)
  }

  // The first selected list line at each depth takes its own step, measured on
  // the draft as it was; its selected siblings and children take the same step
  // where they can, so the selection moves as one block.
  const splices: Splice[] = []
  const items: { indent: number; splice: Splice | null }[] = []
  for (const lineStart of starts) {
    const text = value.slice(lineStart, lineBounds(value, lineStart).end)
    if (!isListLine(text)) continue
    const indent = leadingWidth(text)
    while (items.length > 0 && items[items.length - 1].indent > indent) items.pop()
    const sibling = items[items.length - 1]?.indent === indent ? items.pop() : undefined
    const lead = sibling ?? items[items.length - 1]
    const splice = (lead?.splice ? followSplice(value, lead.splice, lineStart, indent) : null)
      ?? lineSplice(value, lineStart, indent, outdent)
    items.push({ indent, splice })
    if (splice) splices.push(splice)
  }
  if (splices.length === 0) return null

  let next = ''
  let from = 0
  for (const splice of splices) {
    next += value.slice(from, splice.at) + splice.insert
    from = splice.at + splice.removed
  }
  next += value.slice(from)
  // Last splice first: each one's `at` is an offset in the original draft, which
  // only the splices below it would have moved.
  for (const splice of splices.reverse()) {
    caret = shift(caret, splice)
    end = shift(end, splice)
  }
  return { value: next, caret, end }
}

/** True when any line of `value` is a list line, so Tab may indent somewhere. */
export function hasListLine(value: string): boolean {
  return value.split('\n').some(isListLine)
}
