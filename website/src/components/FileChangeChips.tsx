import { memo, useState } from 'react'
import { FileDiff, ChevronDown, ChevronUp } from 'lucide-react'
import type { FileChipStyle } from '../pages/chat/ChatSettings'
import { colorForExt, fileIcon } from '../utils/fileIcons'

import { i18nT } from '../i18n/t'
export interface FileChangeEntry {
  path: string
  before: string
  after: string
}

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

const basename = (p: string) => p.split('/').pop() || p

function Stats({ added, removed }: { added: number; removed: number }) {
  return <>
    {added > 0 && <span className="text-ok font-mono">+{added}</span>}
    {removed > 0 && <span className="text-danger font-mono">-{removed}</span>}
  </>
}

/* ── Diffstat cells: a compact 5-cell bar (GitHub-style) giving an at-a-glance
 *   sense of the add/remove proportion — green cells for additions, red for
 *   removals, the rest neutral. Purely decorative, so aria-hidden.          */
function DiffStatBar({ added, removed }: { added: number; removed: number }) {
  const CELLS = 5
  const total = added + removed
  let g = 0, r = 0
  if (total > 0) {
    g = added > 0 ? Math.max(1, Math.round((added / total) * CELLS)) : 0
    r = removed > 0 ? Math.max(1, Math.round((removed / total) * CELLS)) : 0
    while (g + r > CELLS) { if (g >= r) g--; else r-- }
  }
  const neutral = CELLS - g - r
  const cell = (cls: string, key: string) => <span key={key} className={`w-[7px] h-[7px] rounded-[2px] ${cls}`} />
  return (
    <span className="flex items-center gap-[3px] shrink-0" aria-hidden="true">
      {Array.from({ length: g }, (_, i) => cell('bg-ok', `g${i}`))}
      {Array.from({ length: r }, (_, i) => cell('bg-danger', `r${i}`))}
      {Array.from({ length: neutral }, (_, i) => cell('bg-border', `n${i}`))}
    </span>
  )
}

/* ── Expanded row: one changed file per line — icon + filename left, a
 *   diffstat bar + aligned +N/-N stats right. Rows are padded + rounded and
 *   lift on hover (no hard dividers) so the block reads as a soft list.     */
function ExpandedRow({ fc, added, removed, isArtifact, onClick }: { fc: FileChangeEntry; added: number; removed: number; isArtifact?: boolean; onClick: () => void }) {
  const Icon = fileIcon(fc.path)
  return (
    <button
      onClick={onClick}
      className="group/row flex items-center gap-2.5 w-full px-2 py-1.5 rounded-lg text-left text-[12px] font-medium text-text cursor-pointer transition-colors hover:bg-bg-elevated"
      aria-label={fc.path}
    >
      <Icon size={14} className={`shrink-0 ${colorForExt(fc.path)}`} />
      <span className="truncate min-w-0 transition-colors">{basename(fc.path)}</span>
      {isArtifact && (
        <span
          className="shrink-0 text-[10px] leading-none px-1.5 py-0.5 rounded-full border border-border text-muted font-medium"
          title={i18nT('components.fileChangeChips.this_document_is_tracked_as_a_session_artifact_n')}
        >
          {i18nT('components.fileChangeChips.artifact')}
        </span>
      )}
      <span className="flex-1 min-w-0" />
      <DiffStatBar added={added} removed={removed} />
      <span className="shrink-0 flex items-center justify-end gap-1.5 tabular-nums min-w-[52px]">
        <Stats added={added} removed={removed} />
      </span>
    </button>
  )
}

/* ── Expanded: a single elevated card grouping the changed files into aligned
 *   rows, with a header carrying a neutral icon chip, the file count, and an
 *   aggregate +N/-N roll-up (multi-file only). Reads as one structured unit.
 *   `artifactPaths` (paths the session tracks as documents/artifacts) badges
 *   those rows so generated docs read distinctly from source-file edits.
 *   Long lists are capped at COLLAPSED_COUNT rows behind a "Show N more"
 *   toggle so a big turn doesn't wall off the transcript (the header still
 *   shows the true total + aggregate stats while collapsed).                */
const COLLAPSED_COUNT = 8

function ExpandedList({ fileChanges, onOpenDiff, artifactPaths }: {
  fileChanges: FileChangeEntry[]
  onOpenDiff?: (path: string, modified: string, original: string) => void
  artifactPaths?: Set<string>
}) {
  const [expanded, setExpanded] = useState(false)
  const n = fileChanges.length
  // Count once per file: reused by each row AND the header roll-up.
  const stats = fileChanges.map(fc => countLines(fc.before, fc.after))
  const totalAdded = stats.reduce((s, x) => s + x.added, 0)
  const totalRemoved = stats.reduce((s, x) => s + x.removed, 0)
  const overflow = n > COLLAPSED_COUNT
  const visibleCount = overflow && !expanded ? COLLAPSED_COUNT : n
  const hiddenCount = n - COLLAPSED_COUNT
  return (
    <div className="ft-block-reveal mt-2 mb-1.5 w-full max-w-full rounded-xl border border-border bg-bg overflow-hidden">
      <div className="flex items-center gap-2.5 px-3.5 py-2 border-b border-border">
        <FileDiff size={14} className="text-muted shrink-0" />
        <span className="text-[12px] font-medium text-muted">{n} {i18nT('components.fileChangeChips.file')}{n === 1 ? '' : 's'} {i18nT('components.fileChangeChips.changed')}</span>
        {n > 1 && (
          <span className="ml-auto flex items-center gap-1.5 text-[11px] tabular-nums">
            <Stats added={totalAdded} removed={totalRemoved} />
          </span>
        )}
      </div>
      <div className="flex flex-col gap-0.5 p-1.5">
        {fileChanges.slice(0, visibleCount).map((fc, i) => (
          <ExpandedRow key={fc.path} fc={fc} added={stats[i].added} removed={stats[i].removed} isArtifact={artifactPaths?.has(fc.path)} onClick={() => onOpenDiff?.(fc.path, fc.after, fc.before)} />
        ))}
        {overflow && (
          <button
            onClick={() => setExpanded(v => !v)}
            className="flex items-center justify-center gap-1 w-full px-2 py-1.5 rounded-lg text-[11.5px] font-medium text-muted hover:text-text hover:bg-bg-elevated cursor-pointer transition-colors bg-transparent border-none"
            aria-expanded={expanded}
          >
            {expanded
              ? <><ChevronUp size={13} className="shrink-0" /> {i18nT('components.fileChangeChips.show_less')}</>
              : <><ChevronDown size={13} className="shrink-0" /> {i18nT('components.fileChangeChips.show')} {hiddenCount} {i18nT('components.fileChangeChips.more')}</>}
          </button>
        )}
      </div>
    </div>
  )
}

/* ── Minimal: stats-only liquid-glass pill, filename hovers above on hover ── */
function MinimalChip({ fc, onClick }: { fc: FileChangeEntry; onClick: () => void }) {
  const { added, removed } = countLines(fc.before, fc.after)
  return (
    <span className="relative inline-flex group/tip">
      <span className="glass-surface absolute bottom-full left-0 mb-1 px-2 py-0.5 rounded-md text-[11px] font-medium text-text whitespace-nowrap font-mono z-10 pointer-events-none opacity-0 translate-y-1 group-hover/tip:opacity-100 group-hover/tip:translate-y-0 transition-all duration-150">
        {basename(fc.path)}
      </span>
      <button onClick={onClick} className="glass-surface file-chip inline-flex items-center gap-1 h-[22px] px-2.5 rounded-full text-[11px] font-medium cursor-pointer" aria-label={fc.path}>
        <Stats added={added} removed={removed} />
      </button>
    </span>
  )
}

/**
 * Renders the file-change block below an assistant message.
 *
 * - `expanded` (default): a single card grouping every changed file into
 *   aligned rows (icon + filename left, +N/-N stats right) with a header —
 *   reads as one structured unit instead of a loose pile of pills.
 * - `minimal`: stats-only glass pills that wrap, filename on hover.
 *
 * Clicking any file opens the Monaco diff panel via
 * `onOpenDiff(path, after, before)`.
 */
const FileChangeChips = memo(function FileChangeChips({ fileChanges, onOpenDiff, style = 'expanded', artifactPaths }: {
  fileChanges: FileChangeEntry[]
  onOpenDiff?: (path: string, modified: string, original: string) => void
  style?: FileChipStyle
  /** Paths the session tracks as documents/artifacts — badged in the expanded
   *  card so generated docs read distinctly from source-file edits. */
  artifactPaths?: Set<string>
}) {
  if (!fileChanges?.length) return null
  // Minimal keeps the wrapping pill row; anything else uses the grouped card.
  if (style === 'minimal') {
    return (
      <div className="ft-block-reveal flex flex-wrap items-center gap-1.5 mt-2 mb-1.5">
        {fileChanges.map(fc => (
          <MinimalChip key={fc.path} fc={fc} onClick={() => onOpenDiff?.(fc.path, fc.after, fc.before)} />
        ))}
      </div>
    )
  }
  return <ExpandedList fileChanges={fileChanges} onOpenDiff={onOpenDiff} artifactPaths={artifactPaths} />
})

export default FileChangeChips
