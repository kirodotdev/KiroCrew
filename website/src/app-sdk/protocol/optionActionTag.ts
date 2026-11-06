// The close-session chip's control tag: `<!-- option-action:close=<label> -->`.
//
// One family of the control-tag grammar (`keep-visible`, `deliver:`, `plan_task_id:`;
// the twin is `keepVisibleMarker.ts`, the backend `constants.py`). It sits on its own
// line in the trailing block of control tags at the very end of a reply, after the
// `[OPTIONS: ...]` line when there is one. The head is case-insensitive. The label has no
// `>` and no newline, and the body after `option-action:` is at most 256 characters.
//
// Read with a line scanner, not a pattern: walk the reply's trailing lines from the end,
// skip blank lines and other control tags, and take the first `option-action` tag found.
// A tail inside an unterminated code fence is code the reader sees, so it offers nothing.

/** The actions the tag can offer. Only `close` exists; the label is inert prose. */
export type OfferedActionKind = 'close'
export interface OfferedAction {
  action: OfferedActionKind
  label: string
}

const OPEN = '<!--'
const CLOSE = '-->'
const HEAD = 'option-action:'
const MAX_BODY = 256
const MAX_INDENT = 3
const ACTION_KINDS: ReadonlySet<string> = new Set<OfferedActionKind>(['close'])

/** The Markdown column `line`'s leading blanks reach, and where they end. A tab advances to
 *  the next multiple of four, as CommonMark counts it. */
function indentOf(line: string): { columns: number; at: number } {
  let columns = 0
  let at = 0
  for (; at < line.length; at++) {
    if (line[at] === ' ') columns += 1
    else if (line[at] === '\t') columns += 4 - (columns % 4)
    else break
  }
  return { columns, at }
}

/** The body of a control-tag line (`<!-- body -->`), trimmed, or null when the line is not
 *  one. Indent past three columns is code, so it is not a tag. */
function tagBody(line: string): string | null {
  const { columns, at } = indentOf(line)
  if (columns > MAX_INDENT) return null
  const rest = line.slice(at).trimEnd()
  if (!rest.startsWith(OPEN) || !rest.endsWith(CLOSE) || rest.length < OPEN.length + CLOSE.length) return null
  const body = rest.slice(OPEN.length, rest.length - CLOSE.length)
  if (body.includes('>')) return null
  return body.trim()
}

/** Whether `line` opens or closes a code fence: up to three spaces, then three or more
 *  backticks or tildes. Returns the fence run, or null. */
function fenceRun(line: string): string | null {
  const { columns, at: i } = indentOf(line)
  if (columns > MAX_INDENT) return null
  return runAt(line, i)
}

/** A run of three or more backticks or tildes starting at `i`, or null. */
function runAt(line: string, i: number): string | null {
  const ch = line[i]
  if (ch !== '`' && ch !== '~') return null
  let j = i
  while (j < line.length && line[j] === ch) j++
  return j - i >= 3 ? line.slice(i, j) : null
}

/** List bullets, ordered-list digits and punctuation, blockquote markers and blanks: what may
 *  stand before a fence this scanner cannot place. */
const CONTAINER_CHARS = ' \t>+*-0123456789.)'
const MAX_CONTAINER_PREFIX = 40

/** Whether `line` may open a fence inside a list item, a blockquote or indented code: a fence
 *  run after container markers, or after four or more columns. The scanner tracks no
 *  containers, so such a line vetoes the whole tail rather than risk reading code as a tag,
 *  the rule `keepVisibleMarker.ts` follows. */
function ambiguousFence(line: string): boolean {
  let i = 0
  while (i < line.length && i < MAX_CONTAINER_PREFIX && CONTAINER_CHARS.includes(line[i] ?? '')) i++
  return runAt(line, i) !== null
}

/** Whether the lines before `end` leave a code fence open, or hold a fence this scanner
 *  cannot place. A fence closes only on the same character, at least as long, with nothing
 *  but blanks after it. Inside a tracked fence every line is code, so none vetoes. */
function fenceOpenBefore(lines: string[], end: number): boolean {
  let open: string | null = null
  for (let k = 0; k < end; k++) {
    const run = fenceRun(lines[k] ?? '')
    if (run === null) {
      if (open === null && ambiguousFence(lines[k] ?? '')) return true
      continue
    }
    if (open === null) open = run
    else if (run[0] === open[0] && run.length >= open.length && (lines[k] ?? '').trim() === run) open = null
  }
  return open !== null
}

/** The `option-action` tag body in the reply's trailing control-tag block, with its line
 *  index, or null. */
function findTag(lines: string[]): { body: string; at: number } | null {
  for (let k = lines.length - 1; k >= 0; k--) {
    const line = lines[k] ?? ''
    if (line.trim() === '') continue
    const body = tagBody(line)
    if (body === null) return null
    if (body.slice(0, HEAD.length).toLowerCase() === HEAD) {
      const rest = body.slice(HEAD.length)
      return rest.length <= MAX_BODY ? { body: rest, at: k } : null
    }
  }
  return null
}

/** The action the reply's `option-action` tag offers, or null. An unknown action, a missing
 *  `=`, or an empty label offers nothing, so a typo never becomes a chip. */
export function optionActionOf(content: string): OfferedAction | null {
  const lines = content.split('\n')
  const tag = findTag(lines)
  if (tag === null || fenceOpenBefore(lines, tag.at)) return null
  const eq = tag.body.indexOf('=')
  if (eq < 0) return null
  const action = tag.body.slice(0, eq).trim().toLowerCase()
  const label = tag.body.slice(eq + 1).trim()
  if (!label || !ACTION_KINDS.has(action)) return null
  return { action: action as OfferedActionKind, label }
}

/** `content` without its live `option-action` tag line, for copy and search. The tag is an
 *  HTML comment the renderer never shows, so text read from it would be a phantom match. */
export function stripOptionActionTag(content: string): string {
  const lines = content.split('\n')
  const tag = findTag(lines)
  if (tag === null || fenceOpenBefore(lines, tag.at)) return content
  lines.splice(tag.at, 1)
  return lines.join('\n').trimEnd()
}
