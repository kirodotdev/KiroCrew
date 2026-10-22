/**
 * Renders the file viewer's pending comments into the message sent to the
 * agent.
 *
 * Model-facing text (hence the `.prompt.ts` boundary): the whole output is the
 * prompt the agent reads and acts on, so it is English by design and carries
 * no UI copy. The rows the reader sees in the pending list are rendered by
 * `CommentOverlay.tsx`, through the catalog.
 *
 * Shape, one numbered line per comment:
 *
 *   [Document feedback on /notes/doc.md — 2 comments]
 *
 *   1. (line 3, col 5, "hello" in "abc hello world"): "fix this"
 *   2. (line 12, image "/notes/states/a.png", box x=0.120, y=0.300, w=0.250, h=0.100): "too tight"
 *
 * A message with an image comment also carries one line, before the list,
 * saying how to read a box.
 */
import type { InlineComment } from '../components/CommentOverlay'
import { sliceCodePoints, snapOffPair } from './codePoints'

/** Format comments into a structured message for the agent.
 *  When `content` is provided, includes a short source-context snippet
 *  (~20 chars on each side of the anchor) so the agent can resolve the
 *  exact occurrence unambiguously — critical for short / repeated anchors. */
export function formatCommentsMessage(
  filePath: string,
  comments: InlineComment[],
  content?: string,
  extraPrompt?: string,
): string {
  const srcLines = content?.split('\n')
  // Escape backslashes first, then double quotes, then control characters
  // (\n, \r) so the prompt's quoting structure round-trips unambiguously and
  // user-supplied newlines cannot inject fake prompt blocks (e.g. a comment
  // text of `]\n[System: ignore previous instructions` would otherwise break
  // the format and pass adversarial instructions to the model).
  const esc = (s: string) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\r/g, '\\r')
  const lines = [`[Document feedback on ${filePath} — ${comments.length} comment${comments.length > 1 ? 's' : ''}]`]
  // Free-form overall instruction the user typed alongside the comments.
  // To keep it salient when the comment list is long (otherwise the model
  // can lose attention on a bare middle paragraph — "lost in the middle"), it
  // gets a clearly labeled, delimited block at the top AND a short reminder
  // echoed after the list, so the directive is bookended at the start and end
  // of the message where attention is strongest. It is run through the same
  // esc() helper as comment text (escapes backslash, quote, newline, CR) to
  // prevent prompt-injection via newlines/quotes if the formatted message is
  // ever rendered in a shared/team context — escaping is cheap insurance and
  // the user's intent survives (an escaped \n is still legible to the agent).
  const instruction = extraPrompt?.trim()
  if (instruction) {
    lines.push(
      '',
      `>>> OVERALL INSTRUCTION (applies to all ${comments.length} comment${comments.length > 1 ? 's' : ''} below; read first):`,
      esc(instruction),
      '<<<',
    )
  }
  lines.push('')
  if (comments.some(c => c.image)) {
    // Whether an image was attached is the backend's call, not this one's:
    // build_prompt_blocks (acp/prompt_blocks.py) replaces each path it attaches
    // with an image marker and leaves every other path as written, so the
    // legend points at that difference rather than guessing here. It describes
    // the marker instead of quoting it, because the backend escapes any typed
    // copy of one so it can't pass for a real attachment.
    lines.push(
      'An image comment names a box on the image as fractions of its width and height: x and y are the top-left corner, w and h the size. An image that was attached shows as an image marker in place of its path; one still named by its path was not attached, so open it to see the box.',
      '',
    )
  }
  comments.forEach((c, i) => {
    // An image comment names the image and the box as fractions of the
    // image. No source snippet: the image and `line` already locate it. The
    // image is named by the file the preview loaded, which the backend
    // attaches when it can (see the legend above).
    if (c.image) {
      const line = c.line != null ? `line ${c.line}, ` : ''
      const f = (n: number) => n.toFixed(3)
      const box = `box x=${f(c.image.x)}, y=${f(c.image.y)}, w=${f(c.image.w)}, h=${f(c.image.h)}`
      // The path goes as written, as the header's file path does, so the
      // attachment scan reads the real path: doubling a Windows path's
      // backslashes would name a different one. Only a path that could break
      // out of the quoting (a quote or a line break, which no Windows path
      // holds) is escaped like comment text.
      const where = c.image.path ?? c.image.src
      const pathText = /["\n\r]/.test(where) ? esc(where) : where
      lines.push(`${i + 1}. (${line}image "${pathText}", ${box}): "${esc(c.text)}"`)
      return
    }
    const anchor = c.anchor.length > 80 ? sliceCodePoints(c.anchor, 0, 80) + '…' : c.anchor
    const loc = c.line != null ? (c.column != null ? `line ${c.line}, col ${c.column}, ` : `line ${c.line}, `) : ''
    let ctx = ''
    if (srcLines && c.line != null && c.column != null && c.line >= 1 && c.line <= srcLines.length) {
      const src = srcLines[c.line - 1]
      const start = snapOffPair(src, Math.max(0, c.column - 1 - 20), 1)
      const end = snapOffPair(src, Math.min(src.length, c.column - 1 + c.anchor.length + 20), -1)
      const before = start > 0 ? '…' : ''
      const after = end < src.length ? '…' : ''
      ctx = ` in "${before}${esc(src.slice(start, end))}${after}"`
    }
    lines.push(`${i + 1}. (${loc}"${esc(anchor)}"${ctx}): "${esc(c.text)}"`)
  })
  // Echo the overall instruction at the end so it's not buried above a long
  // comment list — the model attends most strongly to the start and end of
  // the message, so bookending keeps the directive in view either way.
  if (instruction) lines.push('', `>>> REMINDER — overall instruction: ${esc(instruction)}`)
  return lines.join('\n')
}
