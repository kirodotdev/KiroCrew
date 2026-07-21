import { memo, useState } from 'react'
import { Star } from 'lucide-react'
import type { FileChipStyle } from '../pages/chat/ChatSettings'
import { colorForExt, fileIcon } from '../utils/fileIcons'

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

/* ── Expanded: liquid-glass pill with icon + filename + stats ── */
function ExpandedChip({ fc, onClick }: { fc: FileChangeEntry; onClick: () => void }) {
  const { added, removed } = countLines(fc.before, fc.after)
  const Icon = fileIcon(fc.path)
  return (
    <button onClick={onClick} className="glass-surface file-chip inline-flex items-center gap-1 rounded-full px-2.5 py-1.5 text-[11.5px] font-medium cursor-pointer text-text" aria-label={fc.path}>
      <Icon size={12} className={colorForExt(fc.path)} />
      <span className="truncate max-w-[180px]">{basename(fc.path)}</span>
      <Stats added={added} removed={removed} />
    </button>
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
 * Pull light-weight structure out of a markdown doc's *new* content so the
 * DocChip can show something more useful than a bare filename:
 *   - `title`    → first `# ` H1 heading, if any
 *   - `sections` → the `## ` level-2 headings, in order (fenced code skipped)
 */
export function extractMdInfo(after: string): { title: string | null; sections: string[] } {
  let title: string | null = null
  const sections: string[] = []
  let inFence = false
  for (const raw of after.split('\n')) {
    const line = raw.trim()
    if (line.startsWith('```')) { inFence = !inFence; continue }
    if (inFence) continue
    if (!title) {
      const h1 = line.match(/^#\s+(.+)/)
      if (h1) { title = h1[1].trim(); continue }
    }
    const h2 = line.match(/^##\s+(.+)/)
    if (h2) sections.push(h2[1].trim())
  }
  return { title, sections }
}

/* ── Markdown docs: a larger card with icon, filename, status + section list ── */
const DOC_MAX_SECTIONS = 3
function DocChip({ fc, onClick, onSaveDoc }: { fc: FileChangeEntry; onClick: () => void; onSaveDoc?: (path: string) => void }) {
  const [saved, setSaved] = useState(false)
  const { added, removed } = countLines(fc.before, fc.after)
  const isNew = fc.before === ''
  const Icon = fileIcon(fc.path)
  const { title, sections } = extractMdInfo(fc.after)
  const shown = sections.slice(0, DOC_MAX_SECTIONS)
  const extra = sections.length - shown.length
  const sectionText = shown.length
    ? shown.join(' • ') + (extra > 0 ? ` +${extra} more` : '')
    : (title || 'Markdown document')
  const status = isNew ? 'New document' : 'Updated'
  return (
    // A div (not a button) so the Save control can nest without invalid DOM.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}
      className="glass-surface file-chip flex items-start gap-3 w-full max-w-[440px] rounded-xl px-3.5 py-3 text-left cursor-pointer text-text"
      aria-label={fc.path}
    >
      <span className="shrink-0 mt-0.5 flex items-center justify-center w-9 h-9 rounded-lg bg-bg-hover border border-border">
        <Icon size={18} className={colorForExt(fc.path)} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="truncate font-semibold text-[13px]">{basename(fc.path)}</span>
          <span className="shrink-0 ml-auto text-[11px]"><Stats added={added} removed={removed} /></span>
        </span>
        <span className="mt-0.5 block truncate text-[11.5px] text-muted">
          {status} · {sectionText}
        </span>
      </span>
      {onSaveDoc && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); if (saved) return; setSaved(true); Promise.resolve(onSaveDoc(fc.path)).catch(() => setSaved(false)) }}
          className={`shrink-0 mt-0.5 p-1 rounded-md transition-colors bg-transparent border-none cursor-pointer ${saved ? 'text-accent' : 'text-muted/50 hover:text-accent'}`}
          title={saved ? 'Starred' : 'Star'}
          aria-label={saved ? 'Starred in library' : 'Star document'}
        >
          {saved ? <Star size={14} className="fill-current" /> : <Star size={14} />}
        </button>
      )}
    </div>
  )
}

const MD_RE = /\.(md|markdown|mdx)$/i

const RENDERERS = { expanded: ExpandedChip, minimal: MinimalChip } as const

/**
 * Renders a row of file-change chips below an assistant message.
 * Each chip shows the modified file's basename + line stats, and on
 * click opens the Monaco diff panel via `onOpenDiff(path, after, before)`.
 */
const FileChangeChips = memo(function FileChangeChips({ fileChanges, onOpenDiff, onSaveDoc, style = 'expanded' }: {
  fileChanges: FileChangeEntry[]
  onOpenDiff?: (path: string, modified: string, original: string) => void
  /** Save a document chip into the artifact library (materialize + pin). */
  onSaveDoc?: (path: string) => void
  style?: FileChipStyle
}) {
  if (!fileChanges?.length) return null
  const Chip = RENDERERS[style] ?? ExpandedChip
  return (
    <div className="ft-block-reveal flex flex-wrap items-center gap-1.5 mt-2 mb-1.5">
      {fileChanges.map(fc => (
        // Markdown docs get the larger info card (with a Save action);
        // everything else keeps the compact pill from the user's setting.
        MD_RE.test(fc.path)
          ? <DocChip key={fc.path} fc={fc} onClick={() => onOpenDiff?.(fc.path, fc.after, fc.before)} onSaveDoc={onSaveDoc} />
          : <Chip key={fc.path} fc={fc} onClick={() => onOpenDiff?.(fc.path, fc.after, fc.before)} />
      ))}
    </div>
  )
})

export default FileChangeChips
