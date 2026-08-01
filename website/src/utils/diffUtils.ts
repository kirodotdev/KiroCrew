/** Shared diff parsing utilities used by DiffBlock and ToolInputText. */

export interface DiffLine {
  type: 'add' | 'del' | 'context' | 'hunk' | 'meta'
  content: string
  oldNum?: number
  newNum?: number
  /** For `hunk` lines: unchanged lines skipped since the previous hunk.
   *  `undefined` on the first hunk (nothing to report — the gutter numbers
   *  already say where the diff starts). Renderers show a slim
   *  "N unchanged lines" separator instead of the raw `@@` header. */
  hidden?: number
}

/** Parse unified diff text into structured DiffLine objects.
 *  Handles standard unified diff, kiro-specific +N:content format,
 *  and diff --git / index headers. */
export function parseDiffLines(code: string): DiffLine[] {
  const raw = code.split('\n')
  const result: DiffLine[] = []
  let oldN = 0, newN = 0
  let seenHunk = false
  // Undelivered lines the current hunk's `@@ -a,b +c,d` header declared.
  // While a hunk is still consuming lines, a row starting with `--- ` is a
  // DELETION of a line beginning `-- ` (SQL comments, YAML doc markers) and a
  // row starting with `+++ ` is an ADDITION of a line beginning `++ ` — NOT a
  // file header. Only an exhausted (or absent) hunk lets the header branch
  // fire, so per-file state resets never trigger mid-hunk.
  let oldRemain = 0, newRemain = 0
  const inHunk = () => seenHunk && (oldRemain > 0 || newRemain > 0)

  for (const line of raw) {
    if (line.startsWith('\\')) {
      // "\ No newline at end of file" — a diff annotation, not content: emit
      // nothing and leave line numbers AND the remaining-count gate untouched
      // (it would otherwise eat a declared line and shift the header gate).
      continue
    }
    if (line.startsWith('@@')) {
      const m = /@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))?/.exec(line)
      let hidden: number | undefined
      if (m) {
        const oldStart = parseInt(m[1])
        // oldN points one past the last old line the previous hunk consumed,
        // so the gap to this hunk's start is the unchanged stretch between them.
        if (seenHunk) hidden = Math.max(oldStart - oldN, 0)
        oldN = oldStart; newN = parseInt(m[3])
        // Omitted count means 1 per the unified-diff format.
        oldRemain = m[2] !== undefined ? parseInt(m[2]) : 1
        newRemain = m[4] !== undefined ? parseInt(m[4]) : 1
      }
      seenHunk = true
      result.push({ type: 'hunk', content: line, hidden })
    } else if ((line.startsWith('--- ') || line.startsWith('+++ ')) && !inHunk()) {
      // A file header marks a new file: reset per-file hunk/line state so the
      // next hunk's `hidden` doesn't inherit the previous file's counters —
      // otherwise a multi-file patch fabricates an "unchanged lines"
      // separator joining unrelated files.
      seenHunk = false
      oldN = 0; newN = 0
      oldRemain = 0; newRemain = 0
      result.push({ type: 'meta', content: line })
    } else if (line.startsWith('diff --git ') && !inHunk()) {
      // Same reset for git's own file header (may appear without ---/+++
      // when a file is renamed with no content change).
      seenHunk = false
      oldN = 0; newN = 0
      oldRemain = 0; newRemain = 0
      result.push({ type: 'meta', content: line })
    } else if (!seenHunk && (line.startsWith('diff ') || line.startsWith('index '))) {
      result.push({ type: 'meta', content: line })
    } else if (line.startsWith('+')) {
      const kiroAdd = /^\+(\d+):(.*)/.exec(line)
      if (kiroAdd) {
        result.push({ type: 'add', content: kiroAdd[2], newNum: parseInt(kiroAdd[1]) })
      } else {
        result.push({ type: 'add', content: line.slice(1), newNum: newN })
        newN++
        if (newRemain > 0) newRemain--
      }
    } else if (line.startsWith('-')) {
      const kiroDel = /^-(\d+):(.*)/.exec(line)
      if (kiroDel) {
        result.push({ type: 'del', content: kiroDel[2], oldNum: parseInt(kiroDel[1]) })
      } else {
        result.push({ type: 'del', content: line.slice(1), oldNum: oldN })
        oldN++
        if (oldRemain > 0) oldRemain--
      }
    } else {
      const text = line.startsWith(' ') ? line.slice(1) : line
      result.push({ type: 'context', content: text, oldNum: oldN, newNum: newN })
      oldN++; newN++
      if (oldRemain > 0) oldRemain--
      if (newRemain > 0) newRemain--
    }
  }
  return result
}

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

/** Background color classes per diff line type. */
export const DIFF_BG: Record<DiffLine['type'], string> = {
  add: 'bg-diff-add', del: 'bg-diff-del', hunk: 'bg-diff-hunk', meta: '', context: ''
}

/** Foreground color classes per diff line type. */
export const DIFF_FG: Record<DiffLine['type'], string> = {
  add: 'text-diff-add-text', del: 'text-diff-del-text', hunk: 'text-diff-hunk-text',
  meta: 'text-diff-meta-text font-semibold', context: 'text-muted'
}

/** Line-number gutter color classes per diff line type (C1 style: the number
 *  itself carries the add/del color instead of a separate +/- sign column). */
export const DIFF_NUM: Record<DiffLine['type'], string> = {
  add: 'text-diff-add-text font-medium', del: 'text-diff-del-text font-medium',
  hunk: '', meta: '', context: 'text-muted/50'
}

/** 2px inset edge bar on changed rows — the vertical scan line that replaces
 *  the per-row +/- sign column. */
export const DIFF_EDGE: Record<'add' | 'del', string> = {
  add: 'shadow-[inset_2px_0_0_var(--diff-add-text)]',
  del: 'shadow-[inset_2px_0_0_var(--diff-del-text)]'
}
