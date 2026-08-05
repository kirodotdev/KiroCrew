/** Parse a unified diff patch into renderable rows with line numbers.
 *
 * Produces GitHub-style rows: context/add/del lines carry old/new line
 * numbers (null on the side that doesn't exist), and `hunk-gap` rows mark
 * the unmodified stretches between hunks with how many lines were skipped.
 */

export type DiffRow =
  | { kind: 'hunk-gap'; hiddenCount: number }
  | { kind: 'context' | 'add' | 'del'; oldLine: number | null; newLine: number | null; text: string }

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

export function parseUnifiedDiff(patch: string): DiffRow[] {
  const rows: DiffRow[] = []
  if (!patch) return rows
  let oldLine = 0
  let newLine = 0
  // Old-file line number just past the previous hunk, for gap sizing.
  let prevHunkOldEnd: number | null = null

  for (const line of patch.split('\n')) {
    const hunk = line.match(HUNK_RE)
    if (hunk) {
      const oldStart = Number(hunk[1])
      if (prevHunkOldEnd === null) {
        if (oldStart > 1) rows.push({ kind: 'hunk-gap', hiddenCount: oldStart - 1 })
      } else {
        rows.push({ kind: 'hunk-gap', hiddenCount: Math.max(oldStart - prevHunkOldEnd, 0) })
      }
      oldLine = oldStart
      newLine = Number(hunk[3])
      prevHunkOldEnd = null
      continue
    }
    if (line.startsWith('\\')) continue // "\ No newline at end of file"
    if (oldLine === 0 && newLine === 0) continue // preamble before any hunk
    if (line.startsWith('+')) {
      rows.push({ kind: 'add', oldLine: null, newLine, text: line.slice(1) })
      newLine += 1
    } else if (line.startsWith('-')) {
      rows.push({ kind: 'del', oldLine, newLine: null, text: line.slice(1) })
      oldLine += 1
    } else {
      // Context lines are prefixed with a space; a bare empty string can
      // appear for an empty context line or a trailing split artifact.
      rows.push({ kind: 'context', oldLine, newLine, text: line.slice(1) })
      oldLine += 1
      newLine += 1
    }
    prevHunkOldEnd = oldLine
  }
  // Drop a trailing empty context row created by a trailing newline split.
  const last = rows[rows.length - 1]
  if (last && last.kind === 'context' && last.text === '' && patch.endsWith('\n')) rows.pop()
  return rows
}
