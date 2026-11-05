/**
 * Split an inline `<thinking>` block out of an assistant reply's text.
 *
 * A model that has no separate reasoning channel writes its scratchpad into the
 * answer itself, wrapped in `<thinking>…</thinking>`. The ACP reasoning stream
 * (`agent_thought_chunk`) reaches the transcript as its own `thinking` row, but
 * this inline form arrives as ordinary message text, so without a split the
 * bubble prints the tag and the whole scratchpad verbatim. The messaging
 * channels already drop it (`kiro_crew.messaging.markup.strip_thinking_tags`);
 * this is the dashboard's half, which keeps the reasoning and shows it behind
 * the same collapsed row as the streamed kind.
 *
 * Only a block that OPENS a line, outside a fenced code block, is reasoning.
 * A reply that explains the tag (`` `<thinking>` `` in prose, or an example in
 * a code fence) is the answer, and stripping it would delete what the model
 * was asked to write.
 *
 * The text is read by an exact line scanner, not a pattern: each line's
 * indent, fence run and opener are counted and compared at the line start.
 */

/** Tag names, without brackets, that open a reasoning block. */
const TAG_NAMES = ['thinking', 'antml:thinking'] as const

/** Four leading spaces make a line indented code, which can neither open a
 *  fence nor a reasoning block. */
const CODE_INDENT = 4

/** The fewest backticks or tildes that make a fence. */
const MIN_FENCE_RUN = 3

export interface InlineThinking {
  /** The reply with every reasoning block removed. */
  cleaned: string
  /** The reasoning text, blocks joined by a blank line; `''` when there is none. */
  thinking: string
}

/** Leading spaces of the line starting at `start`, counted up to `CODE_INDENT`. */
function indentOf(text: string, start: number, end: number): number {
  let n = 0
  while (n < CODE_INDENT && start + n < end && text[start + n] === ' ') n += 1
  return n
}

/** Length of the run of `ch` starting at `at`, stopping at `end`. */
function runOf(text: string, at: number, end: number, ch: string): number {
  let n = 0
  while (at + n < end && text[at + n] === ch) n += 1
  return n
}

/** The tag name whose opener sits exactly at `at`, or `null`. */
function openerAt(text: string, at: number): string | null {
  for (const name of TAG_NAMES) {
    if (text.startsWith(`<${name}>`, at)) return name
  }
  return null
}

/** Index of the next line start at or after `pos`: `pos` itself when it is
 *  one (position 0 or right after a `\n`), else just past the next `\n`. */
function lineStartFrom(text: string, pos: number): number {
  if (pos === 0 || text[pos - 1] === '\n') return pos
  const nl = text.indexOf('\n', pos)
  return nl < 0 ? text.length : nl + 1
}

/**
 * `hidePartial` also removes an opener whose closer has not arrived yet, with
 * everything after it. It is for a reply that is still streaming: the closer
 * is simply later in the stream, and without it the scratchpad types itself
 * out as prose until the block completes. A finished reply keeps an unclosed
 * opener as written, since nothing more is coming to close it.
 */
export function extractInlineThinking(text: string, { hidePartial = false }: { hidePartial?: boolean } = {}): InlineThinking {
  if (!text.includes('thinking>')) return { cleaned: text, thinking: '' }
  const parts: string[] = []
  let out = ''
  let cursor = 0
  let removed = false
  let fence: { ch: string; len: number } | null = null
  let lineStart = 0
  while (lineStart < text.length) {
    const nl = text.indexOf('\n', lineStart)
    const lineEnd = nl < 0 ? text.length : nl
    const next = nl < 0 ? text.length : nl + 1
    const indent = indentOf(text, lineStart, lineEnd)
    const first = indent < CODE_INDENT ? text[lineStart + indent] : undefined
    if (fence) {
      // A closing fence: the same character, at least as long, nothing after it
      // but whitespace.
      if (first === fence.ch) {
        const run = runOf(text, lineStart + indent, lineEnd, fence.ch)
        if (run >= fence.len && text.slice(lineStart + indent + run, lineEnd).trim() === '') fence = null
      }
      lineStart = next
      continue
    }
    if (first === '`' || first === '~') {
      const run = runOf(text, lineStart + indent, lineEnd, first)
      if (run >= MIN_FENCE_RUN) fence = { ch: first, len: run }
      lineStart = next
      continue
    }
    const name = first === '<' ? openerAt(text, lineStart + indent) : null
    if (!name) {
      lineStart = next
      continue
    }
    const bodyStart = lineStart + indent + name.length + 2
    const close = `</${name}>`
    const closeAt = text.indexOf(close, bodyStart)
    if (closeAt < 0) {
      if (!hidePartial) break
      const inner = text.slice(bodyStart).trim()
      if (inner) parts.push(inner)
      out += text.slice(cursor, lineStart)
      cursor = text.length
      removed = true
      break
    }
    const inner = text.slice(bodyStart, closeAt).trim()
    if (inner) parts.push(inner)
    out += text.slice(cursor, lineStart)
    cursor = closeAt + close.length
    removed = true
    // The block opened its line, so its line break goes with it: otherwise a
    // block between two lines leaves a blank line where it stood. When the
    // text before the block is empty or already ends in a blank line, the
    // blank lines after it go too, so the answer neither opens with a gap nor
    // doubles one. Only line breaks at this cut are touched: the answer's own
    // blank lines and indentation stay exactly as written.
    if (text[cursor] === '\n') cursor += 1
    if (out === '' || out.endsWith('\n\n')) {
      while (text[cursor] === '\n') cursor += 1
    }
    // Text after the closer on its own line is answer prose, not a line start.
    lineStart = lineStartFrom(text, cursor)
  }
  if (!removed) return { cleaned: text, thinking: '' }
  return { cleaned: out + text.slice(cursor), thinking: parts.join('\n\n') }
}
