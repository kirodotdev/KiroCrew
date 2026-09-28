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
    return { start: lineStart, end: lineEnd, insert: '' }
  }
  return { start: caret, end: caret, insert: `\n${nextPrefix(marker)}` }
}
