// Inline markdown recognition for the chat composer's live styling.
//
// `parseInlineMarkdown` splits one run of composer text (a single line, or the
// text between two paste chips) into segments and says how each should LOOK:
// bold, italic, strikethrough, inline code, and whether the characters are the
// markdown markers themselves (`**`, `*`, `_`, `~~`, a backtick). It never
// changes the text: joining every segment's `text` gives back the input
// byte-for-byte, so the prompt the user sends is exactly what they typed and
// styling stays display-only.
//
// Scope is deliberately small: emphasis (`*`, `_`), strong (`**`, `__`),
// strikethrough (`~~`) and single-backtick code spans. Headings, links and
// block syntax are out of scope. Emphasis follows the CommonMark delimiter-run
// rules (left/right flanking, the intraword `_` restriction, the "rule of 3")
// closely enough that `***both***`, `2*3*4`, `snake_case_name` and an
// unmatched `**` read the way a markdown renderer would show them. Escaped
// markers (`\*`) and markers inside a code span are literal. A run of two or
// more backticks is left literal: only single-backtick spans are styled.

/** Format bits. The values match Lexical's TextNode format flags so the editor
 *  plugin can pass them through unchanged; this module does not import Lexical. */
export const INLINE_BOLD = 1
export const INLINE_ITALIC = 2
export const INLINE_STRIKETHROUGH = 4
export const INLINE_CODE = 16

export interface InlineSegment {
  text: string
  /** Bitwise OR of the INLINE_* flags that apply to these characters. */
  format: number
  /** True when the characters are markdown markers (shown dimmed). */
  marker: boolean
}

/** Runs longer than this are returned as one plain segment. Recognition is
 *  re-run on every keystroke, so a pathological single-line paste must not
 *  cost more than a bounded amount of work. */
export const MAX_STYLED_RUN = 20_000
/** Delimiter matching is quadratic in the number of delimiter runs; past this
 *  many the run is returned plain rather than risk a slow keystroke. */
const MAX_DELIMITER_RUNS = 1_000

const ASCII_PUNCTUATION = /[!-/:-@[-`{-~]/
const UNICODE_PUNCTUATION = /[\p{P}\p{S}]/u
const WHITESPACE = /\s/

function isWhitespace(ch: string | undefined): boolean {
  return ch === undefined || WHITESPACE.test(ch)
}

function isPunctuation(ch: string | undefined): boolean {
  return ch !== undefined && UNICODE_PUNCTUATION.test(ch)
}

interface DelimiterRun {
  ch: '*' | '_' | '~'
  /** Current bounds: an opener gives up characters from its right end, a
   *  closer from its left end, as pairs are matched. */
  start: number
  end: number
  originalLength: number
  canOpen: boolean
  canClose: boolean
  /** Dropped because a pair was matched around it. */
  removed: boolean
}

function plain(text: string): InlineSegment[] {
  return text ? [{ text, format: 0, marker: false }] : []
}

/** Mark backslash escapes and code spans: their characters become inert (they
 *  can never be emphasis delimiters), and code content gets INLINE_CODE. */
function scanLiterals(text: string, inert: Uint8Array, format: Uint8Array, marker: Uint8Array): void {
  const n = text.length
  let i = 0
  while (i < n) {
    const ch = text[i]
    if (ch === '\\' && i + 1 < n && ASCII_PUNCTUATION.test(text[i + 1])) {
      // A backslash-escaped marker is literal text. Both characters stay
      // visible and neither can open or close anything.
      inert[i] = 1
      inert[i + 1] = 1
      i += 2
      continue
    }
    if (ch === '`') {
      let runEnd = i
      while (runEnd < n && text[runEnd] === '`') runEnd += 1
      if (runEnd - i === 1) {
        // Find the next backtick run of exactly one; longer runs in between
        // are part of the code content.
        let k = runEnd
        let close = -1
        while (k < n) {
          if (text[k] !== '`') { k += 1; continue }
          let m = k
          while (m < n && text[m] === '`') m += 1
          if (m - k === 1) { close = k; break }
          k = m
        }
        if (close > runEnd) {
          marker[i] = 1
          marker[close] = 1
          for (let p = i; p <= close; p += 1) inert[p] = 1
          for (let p = runEnd; p < close; p += 1) format[p] |= INLINE_CODE
          i = close + 1
          continue
        }
      }
      // An unmatched backtick, or a multi-backtick run: literal.
      for (let p = i; p < runEnd; p += 1) inert[p] = 1
      i = runEnd
      continue
    }
    i += 1
  }
}

function collectDelimiterRuns(text: string, inert: Uint8Array): DelimiterRun[] | null {
  const runs: DelimiterRun[] = []
  const n = text.length
  let i = 0
  while (i < n) {
    const ch = text[i]
    if (inert[i] || (ch !== '*' && ch !== '_' && ch !== '~')) { i += 1; continue }
    let end = i
    while (end < n && text[end] === ch && !inert[end]) end += 1
    const length = end - i
    // Strikethrough uses exactly two tildes; any other tilde run is literal.
    if (ch === '~' && length !== 2) { i = end; continue }
    const before = i > 0 ? text[i - 1] : undefined
    const after = end < n ? text[end] : undefined
    const leftFlanking = !isWhitespace(after) &&
      (!isPunctuation(after) || isWhitespace(before) || isPunctuation(before))
    const rightFlanking = !isWhitespace(before) &&
      (!isPunctuation(before) || isWhitespace(after) || isPunctuation(after))
    let canOpen = leftFlanking
    let canClose = rightFlanking
    if (ch === '_') {
      // `_` never emphasises inside a word, so snake_case_names stay plain.
      canOpen = leftFlanking && (!rightFlanking || isPunctuation(before))
      canClose = rightFlanking && (!leftFlanking || isPunctuation(after))
    }
    if (canOpen || canClose) {
      runs.push({ ch: ch as DelimiterRun['ch'], start: i, end, originalLength: length, canOpen, canClose, removed: false })
      if (runs.length > MAX_DELIMITER_RUNS) return null
    }
    i = end
  }
  return runs
}

function matchDelimiters(runs: DelimiterRun[], format: Uint8Array, marker: Uint8Array): void {
  for (let ci = 0; ci < runs.length; ci += 1) {
    const closer = runs[ci]
    if (!closer.canClose || closer.removed) continue
    while (closer.end > closer.start) {
      let found = -1
      for (let oi = ci - 1; oi >= 0; oi -= 1) {
        const opener = runs[oi]
        if (opener.removed || opener.ch !== closer.ch || !opener.canOpen || opener.end <= opener.start) continue
        if (opener.ch !== '~' && (opener.canClose || closer.canOpen) &&
          (opener.originalLength + closer.originalLength) % 3 === 0 &&
          !(opener.originalLength % 3 === 0 && closer.originalLength % 3 === 0)) continue
        found = oi
        break
      }
      if (found < 0) break
      const opener = runs[found]
      const openerLeft = opener.end - opener.start
      const closerLeft = closer.end - closer.start
      const use = opener.ch === '~' ? 2 : openerLeft >= 2 && closerLeft >= 2 ? 2 : 1
      const kind = opener.ch === '~' ? INLINE_STRIKETHROUGH : use === 2 ? INLINE_BOLD : INLINE_ITALIC
      for (let p = opener.end - use; p < opener.end; p += 1) marker[p] = 1
      for (let p = closer.start; p < closer.start + use; p += 1) marker[p] = 1
      for (let p = opener.end; p < closer.start; p += 1) format[p] |= kind
      opener.end -= use
      closer.start += use
      for (let k = found + 1; k < ci; k += 1) runs[k].removed = true
    }
  }
}

export function parseInlineMarkdown(text: string): InlineSegment[] {
  if (!text || text.length > MAX_STYLED_RUN) return plain(text)
  const n = text.length
  const inert = new Uint8Array(n)
  const format = new Uint8Array(n)
  const marker = new Uint8Array(n)
  scanLiterals(text, inert, format, marker)
  const runs = collectDelimiterRuns(text, inert)
  if (runs === null) return plain(text)
  matchDelimiters(runs, format, marker)

  const segments: InlineSegment[] = []
  let start = 0
  for (let i = 1; i <= n; i += 1) {
    if (i < n && format[i] === format[start] && marker[i] === marker[start]) continue
    segments.push({ text: text.slice(start, i), format: format[start], marker: marker[start] === 1 })
    start = i
  }
  return segments
}

/** True when the run needs no styling at all (one plain segment or empty). */
export function isPlainInline(segments: InlineSegment[]): boolean {
  return segments.every(segment => segment.format === 0 && !segment.marker)
}
