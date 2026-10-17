import { leadingMentionBoundary } from '../utils/fileTokens'

// Markdown list continuation for the chat composer's "new line" action.
//
// Pure text in, one edit out: the composer hands over its plain-text value and
// a collapsed caret, and gets back either `null` (insert an ordinary newline)
// or the single replacement that continues or ends the list item under the
// caret. The stored prompt stays plain text; nothing here knows about editor
// nodes.

/** One replacement of `value.slice(start, end)` by `insert`. */
export interface ListLineBreakEdit {
  start: number
  end: number
  insert: string
  /** Number rewrites on the ordered items below the new one, each a span of
   *  digits in the ORIGINAL value after `end`, in document order. Present only
   *  when a number changes. Apply them last to first, then the edit itself, so
   *  no offset moves under a later write. */
  renumber?: ListLineBreakEdit[]
}

/** `value` with `edit` and its renumbering applied. */
export function applyListEdit(value: string, edit: ListLineBreakEdit): string {
  let next = value
  for (const write of [...(edit.renumber ?? [])].reverse()) {
    next = next.slice(0, write.start) + write.insert + next.slice(write.end)
  }
  return next.slice(0, edit.start) + edit.insert + next.slice(edit.end)
}

/** A half-open `[start, end)` span the caret must not split (an inline chip). */
export interface ProtectedRange {
  start: number
  end: number
}

interface ListMarker {
  indent: string
  bullet: string | null
  digits: string | null
  delimiter: string | null
  gap: string
  task: boolean
  taskGap: string
  /** Offset in the line where the item's own text begins. */
  contentStart: number
}

// indent, then a bullet or `N.`/`N)`, then at least one space or tab. A bare
// marker with nothing after it ("-", "2024.") is ordinary text, so a line that
// merely ends in a number never loses it on Enter.
const MARKER_RE = /^([ \t]*)(?:([-*+])|(\d+)([.)]))([ \t]+)/
const TASK_RE = /^\[[ xX]\](?=[ \t]|$)([ \t]*)/

function parseMarker(line: string): ListMarker | null {
  const match = MARKER_RE.exec(line)
  if (!match) return null
  const [whole, indent, bullet, digits, delimiter, gap] = match
  const task = TASK_RE.exec(line.slice(whole.length))
  return {
    indent,
    bullet: bullet ?? null,
    digits: digits ?? null,
    delimiter: delimiter ?? null,
    gap,
    task: task !== null,
    taskGap: task ? task[1] || ' ' : '',
    contentStart: whole.length + (task ? task[0].length : 0),
  }
}

/** `9` → `10`, `09` → `10`, `007` → `008`; unsafe integers repeat unchanged. */
function nextOrdinal(digits: string): string {
  const current = Number(digits)
  if (!Number.isSafeInteger(current) || !Number.isSafeInteger(current + 1)) return digits
  const next = String(current + 1)
  return digits.length > 1 && digits.startsWith('0') ? next.padStart(digits.length, '0') : next
}

function nextPrefix(marker: ListMarker): string {
  const head = marker.bullet ?? `${nextOrdinal(marker.digits!)}${marker.delimiter}`
  return `${marker.indent}${head}${marker.gap}${marker.task ? `[ ]${marker.taskGap}` : ''}`
}

// A mention starts a whitespace-free word, optionally after one opening
// wrapper, using the boundary mention resolution uses (`leadingMentionBoundary`
// in utils/fileTokens). A `$` only starts a skill mention before the skill
// slug's first character.
const MENTION_START_RE = new RegExp(`^(?:${leadingMentionBoundary})(?:@|\\$[a-z0-9])`)

// A plain-text @file or $skill mention the caret sits in the middle of.
function insideMentionToken(line: string, column: number): boolean {
  if (column === 0 || column >= line.length) return false
  if (/\s/.test(line[column - 1]) || /\s/.test(line[column])) return false
  // Walk back to the word's first character: linear in the word.
  let wordStart = column - 1
  while (wordStart > 0 && !/\s/.test(line[wordStart - 1])) wordStart -= 1
  return MENTION_START_RE.test(` ${line.slice(wordStart, Math.min(line.length, wordStart + 3))}`)
}

/**
 * The edit the composer's new-line action should make at `caret`, or `null`
 * for an ordinary newline.
 *
 * - On a list item, the break continues the list: same indent and bullet, or
 *   the next number with the same delimiter and zero padding; a task item
 *   continues as an unchecked `[ ]`. Text after the caret moves to the new
 *   item.
 * - On an empty item (marker and whitespace only) the marker is removed and
 *   the line is left empty, which is how a list is ended.
 * - A caret inside the marker, off a list line, or inside an inline chip or
 *   mention token gets `null`.
 */
export function listLineBreakEdit(
  value: string,
  caret: number,
  protectedRanges: readonly ProtectedRange[] = [],
): ListLineBreakEdit | null {
  if (caret < 0 || caret > value.length) return null
  if (protectedRanges.some(range => range.start < caret && caret < range.end)) return null
  const lineStart = value.lastIndexOf('\n', caret - 1) + 1
  const newline = value.indexOf('\n', caret)
  const lineEnd = newline === -1 ? value.length : newline
  const line = value.slice(lineStart, lineEnd)
  const marker = parseMarker(line)
  if (!marker) return null
  const column = caret - lineStart
  if (column < marker.contentStart) return null
  if (insideMentionToken(line, column)) return null
  if (line.slice(marker.contentStart).trim() === '') {
    return removeMarker({ start: lineStart, end: lineEnd, insert: '' }, value, lineEnd, marker, protectedRanges)
  }
  const edit: ListLineBreakEdit = { start: caret, end: caret, insert: `\n${nextPrefix(marker)}` }
  // The new item takes the next number, so the items below move down by one.
  return withRenumber(edit, value, lineEnd, marker, protectedRanges, marker.digits && nextOrdinal(nextOrdinal(marker.digits)))
}

/** `edit` that removes an ordered item's marker, with the items below moved
 *  up by one into the number it gave up, so Enter then Backspace (or Enter on
 *  that empty item) puts the list back as it was. */
function removeMarker(
  edit: ListLineBreakEdit,
  value: string,
  lineEnd: number,
  marker: ListMarker,
  protectedRanges: readonly ProtectedRange[],
): ListLineBreakEdit {
  return withRenumber(edit, value, lineEnd, marker, protectedRanges, marker.digits)
}

function withRenumber(
  edit: ListLineBreakEdit,
  value: string,
  lineEnd: number,
  marker: ListMarker,
  protectedRanges: readonly ProtectedRange[],
  first: string | null,
): ListLineBreakEdit {
  if (first === null) return edit
  const renumber = renumberFollowing(value, lineEnd, marker, protectedRanges, first)
  if (renumber.length) edit.renumber = renumber
  return edit
}

/**
 * The number rewrites for the ordered items below `marker`'s line: the first
 * of them becomes `first` and each later one counts on from it. A new item
 * passes the number after its own, so `1. |foo` / `2. bar` ends as 1, 2, 3
 * rather than repeating 2; a removed marker passes its own number, so the
 * items below close the gap it leaves.
 *
 * Only a run the user already numbered in sequence moves: siblings at the same
 * indent with the same delimiter, each one higher than the item before it. The
 * first sibling off that count (a `1.` / `1.` / `1.` list, a deliberate jump),
 * a blank line, or a line at a shallower or equal indent that is not a sibling
 * ends the run, and nothing past it changes. Deeper lines (nested items, wrapped
 * text) are passed over untouched.
 */
function renumberFollowing(
  value: string,
  lineEnd: number,
  marker: ListMarker,
  protectedRanges: readonly ProtectedRange[],
  first: string,
): ListLineBreakEdit[] {
  const writes: ListLineBreakEdit[] = []
  let typed = marker.digits!
  let assigned = first
  let pos = lineEnd
  while (pos < value.length) {
    const start = pos + 1
    const newline = value.indexOf('\n', start)
    const end = newline === -1 ? value.length : newline
    pos = end
    const line = value.slice(start, end)
    if (line.trim() === '') break
    const sibling = parseMarker(line)
    if (sibling && sibling.indent === marker.indent && sibling.digits !== null) {
      if (sibling.delimiter !== marker.delimiter) break
      typed = nextOrdinal(typed)
      if (sibling.digits !== typed) break
      const digitsStart = start + sibling.indent.length
      const digitsEnd = digitsStart + sibling.digits.length
      if (protectedRanges.some(range => range.start < digitsEnd && digitsStart < range.end)) break
      if (sibling.digits !== assigned) writes.push({ start: digitsStart, end: digitsEnd, insert: assigned })
      assigned = nextOrdinal(assigned)
      continue
    }
    const indent = /^[ \t]*/.exec(line)![0]
    if (indent.length <= marker.indent.length) break
  }
  return writes
}

/**
 * The edit the composer's Backspace should make at `caret`, or `null` for an
 * ordinary one-character delete.
 *
 * Backspace with the caret right where a list item's text begins removes the
 * whole marker in one press: the bullet or number, its delimiter, the gap and
 * any `[ ]` task box. Without this, clearing a `2. ` the Enter key just made
 * takes three presses, and `  - [ ] ` takes eight.
 *
 * - An empty item (marker and whitespace only) is cleared to an empty line,
 *   indent included: the same result as Enter on that item, so either key
 *   ends the list in one press.
 * - An item with text keeps its indent and its text and loses only the
 *   marker, so the text stays where it was as a plain line.
 * - A caret anywhere else (inside or before the marker, mid-text, off a list
 *   line, inside an inline chip) gets `null`.
 */
export function listMarkerBackspaceEdit(
  value: string,
  caret: number,
  protectedRanges: readonly ProtectedRange[] = [],
): ListLineBreakEdit | null {
  if (caret <= 0 || caret > value.length) return null
  if (protectedRanges.some(range => range.start < caret && caret < range.end)) return null
  const lineStart = value.lastIndexOf('\n', caret - 1) + 1
  const newline = value.indexOf('\n', caret)
  const lineEnd = newline === -1 ? value.length : newline
  const line = value.slice(lineStart, lineEnd)
  const marker = parseMarker(line)
  if (!marker || caret - lineStart !== marker.contentStart) return null
  if (line.slice(marker.contentStart).trim() === '') {
    return removeMarker({ start: lineStart, end: lineEnd, insert: '' }, value, lineEnd, marker, protectedRanges)
  }
  return { start: lineStart + marker.indent.length, end: caret, insert: '' }
}
