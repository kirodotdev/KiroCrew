/**
 * Pure line-counting helpers shared by the diff surfaces.
 *
 * Their own module rather than members of `components/FileChangeChips.tsx` and
 * `pages/chat/ActivityViewer.tsx` so a pure test can reach them without
 * importing either: both pull the Pierre diff runtime, framer-motion,
 * react-markdown, katex and highlight.js into the importing fork, which line
 * arithmetic over two strings has no reason to pay for.
 */

/**
 * Line-level diff count via LCS — correctly attributes moves as +N/-N
 * (a moved line shows up as a removal at the old position and an addition
 * at the new). Falls back to a cheap multiset count for huge files to bound
 * cost; that fallback can under-report pure moves but only on files we
 * already cap at 200KB, so the cap is rarely hit in practice.
 */
export function countLines(before: string, after: string): { added: number; removed: number } {
  if (before === after) return { added: 0, removed: 0 }
  // Guard empty strings: ''.split('\n') yields [''] (1 phantom line), which would
  // mis-count a new file as +1/-1 instead of +1, and a fully cleared file as
  // +1/-2 instead of -2. Treat empty content as zero lines.
  const a = before ? before.split('\n') : []
  const b = after ? after.split('\n') : []
  const m = a.length, n = b.length
  // LCS with rolling rows: O(mn) time, O(min(m,n)) space.
  // 1M cell cap = ~1000x1000 lines which covers anything inside our 200KB snapshot cap comfortably.
  if (m * n <= 1_000_000) {
    let prev = new Int32Array(n + 1)
    let curr = new Int32Array(n + 1)
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (a[i - 1] === b[j - 1]) curr[j] = prev[j - 1] + 1
        else curr[j] = prev[j] >= curr[j - 1] ? prev[j] : curr[j - 1]
      }
      const tmp = prev; prev = curr; curr = tmp
      curr.fill(0)
    }
    const lcs = prev[n]
    return { added: n - lcs, removed: m - lcs }
  }
  // Huge-file fallback: multiset count. Cheap but doesn't detect pure moves.
  const aMap = new Map<string, number>()
  const bMap = new Map<string, number>()
  for (const line of a) aMap.set(line, (aMap.get(line) || 0) + 1)
  for (const line of b) bMap.set(line, (bMap.get(line) || 0) + 1)
  let added = 0, removed = 0
  for (const [line, count] of bMap) {
    const aCount = aMap.get(line) || 0
    if (count > aCount) added += count - aCount
  }
  for (const [line, count] of aMap) {
    const bCount = bMap.get(line) || 0
    if (count > bCount) removed += count - bCount
  }
  return { added, removed }
}

/**
 * Added/removed counts read straight off a UNIFIED DIFF's own markers, for a
 * surface that already holds a patch rather than the before/after pair
 * `countLines` needs. `+++`/`---` are the file headers, not content lines.
 */
export function countDiffStats(diff: string): { added: number; removed: number } {
  let added = 0, removed = 0
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++
    else if (line.startsWith('-') && !line.startsWith('---')) removed++
  }
  return { added, removed }
}

/** The 0-based line span that differs between two texts, per side, or `null`
 *  when they are identical. `oldStart`/`oldEnd` index `before`'s lines and
 *  `newStart`/`newEnd` index `after`'s; each `*End` is exclusive.
 *
 *  This is a common-prefix / common-suffix walk, NOT a diff: it finds only the
 *  OUTER bounds of the change (first line that differs from the top, last line
 *  that differs from the bottom), so a scatter of edits reports one span that
 *  covers them all. That span is a locality region, not a row-level diff. Only
 *  its first and last non-empty rows are proven to differ; consumers must not
 *  give the interior add/remove semantics. The oversized fallback exists because
 *  a real line-level diff is too expensive to run on the renderer thread for
 *  these inputs (see `renderBudget`), and this stays cheap for the same reason:
 *  two pointer walks that stop at the first difference, no LCS, no allocation
 *  beyond the two line arrays the caller already holds. Its one job is to tell
 *  the fallback WHERE to look so it can anchor there instead of at line 1. */
export function changedLineSpan(
  beforeLines: readonly string[],
  afterLines: readonly string[],
): { oldStart: number; oldEnd: number; newStart: number; newEnd: number } | null {
  const m = beforeLines.length
  const n = afterLines.length
  let start = 0
  const max = Math.min(m, n)
  while (start < max && beforeLines[start] === afterLines[start]) start++
  if (start === m && start === n) return null // identical
  // Walk the common suffix, but never cross the common prefix on either side.
  let endBack = 0
  while (
    endBack < m - start
    && endBack < n - start
    && beforeLines[m - 1 - endBack] === afterLines[n - 1 - endBack]
  ) endBack++
  return { oldStart: start, oldEnd: m - endBack, newStart: start, newEnd: n - endBack }
}
