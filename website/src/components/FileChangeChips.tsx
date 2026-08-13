import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { FileDiff, ChevronDown, ChevronUp, ChevronRight } from 'lucide-react'
import type { FileChipStyle } from '../pages/chat/ChatSettings'
import { useRowDisclosure } from '../pages/chat/rowDisclosure'
import { PierreFilePair } from '../pierre'
import { ROW_ANIM_MS, ROW_CSS_CLICKABLE_TITLE, ROW_CSS_CLOSING, ROW_CSS_OPEN } from './fileChangeChipsCss'
import { countLines } from '../utils/diffLineCounts'

import { i18nT } from '../i18n/t'
export interface FileChangeEntry {
  path: string
  before: string
  after: string
}

/** Re-exported for this component's existing importers; defined in
 *  `utils/diffLineCounts` so a pure test need not import this module (and with
 *  it the Pierre diff runtime). */
export { countLines }

const basename = (p: string) => p.split('/').pop() || p

/* Removals first, additions second — the order Pierre's own file headers use
 * (`createMetadataElement` pushes the deletions span before the additions one),
 * so the minimal pills read the same way as Pierre's headers. */
function Stats({ added, removed }: { added: number; removed: number }) {
  if (added === 0 && removed === 0) {
    return <span className="text-muted text-[11px] italic">{i18nT('components.fileChangeChips.no_changes')}</span>
  }
  return <>
    {removed > 0 && <span className="text-danger font-mono">-{removed}</span>}
    {added > 0 && <span className="text-ok font-mono">+{added}</span>}
  </>
}

/* ── Diffstat cells: a compact 5-cell bar (GitHub-style) giving an at-a-glance
 *   sense of the add/remove proportion — green cells for additions, red for
 *   removals, the rest neutral. Purely decorative, so aria-hidden.          */
function DiffStatBar({ added, removed }: { added: number; removed: number }) {
  const CELLS = 5
  const total = added + removed
  // No-op: hide the bar entirely — 5 neutral cells carry no signal.
  if (total === 0) return null
  let g = added > 0 ? Math.max(1, Math.round((added / total) * CELLS)) : 0
  let r = removed > 0 ? Math.max(1, Math.round((removed / total) * CELLS)) : 0
  while (g + r > CELLS) { if (g >= r) g--; else r-- }
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


/** Which action a header click belongs to, from the event's composed path.
 *
 *  Pierre paints the filename into its shadow root, so a light-DOM listener's
 *  `event.target` is retargeted to the host and cannot tell the filename apart
 *  from the rest of the header — `composedPath()` still carries the real inner
 *  node. The header therefore has two actions and no dead zone: the filename
 *  opens the file, the remaining header whitespace toggles the diff (matching
 *  the chevron), and anything below the header is left alone so selecting code
 *  never collapses it. */
export function headerClickAction(path: readonly EventTarget[]): 'open' | 'toggle' | 'ignore' {
  const has = (sel: string) => path.some(n => n instanceof Element && n.matches(sel))
  if (!has('[data-diffs-header]')) return 'ignore'
  return has('[data-title]') ? 'open' : 'toggle'
}

function ExpandedRow({ fc, added, removed, isArtifact, onFileOpen, disclosureKey }: {
  fc: FileChangeEntry
  added: number
  removed: number
  isArtifact?: boolean
  onFileOpen?: (path: string) => void
  disclosureKey?: string
}) {
  const [open, setOpen] = useRowDisclosure(disclosureKey, false)
  // Held mounted for one animation after `open` goes false, so collapsing has
  // a frame to animate in before Pierre drops the body.
  const [closing, setClosing] = useState(false)
  const rowRef = useRef<HTMLDivElement>(null)
  // Pierre titles the header from `name`; the full path would wrap the row and
  // bury the filename, so the row shows the basename and the path stays on the
  // Open button's tooltip.
  const name = basename(fc.path)
  const oldFile = useMemo(() => ({ name, contents: fc.before }), [name, fc.before])
  const newFile = useMemo(() => ({ name, contents: fc.after }), [name, fc.after])
  const options = useMemo(
    () => ({
      collapsed: !open && !closing,
      overflow: 'wrap' as const,
      disableFileHeader: false,
      unsafeCSS: (closing ? ROW_CSS_CLOSING : ROW_CSS_OPEN) + (onFileOpen ? ROW_CSS_CLICKABLE_TITLE : ''),
    }),
    [open, closing, onFileOpen],
  )
  useEffect(() => {
    if (!closing) return
    const t = setTimeout(() => setClosing(false), ROW_ANIM_MS)
    return () => clearTimeout(t)
  }, [closing])
  const toggle = () => {
    // Reopening inside the collapse window must CLEAR `closing`, not leave it:
    // the closing stylesheet runs `fccHide` with `animation-fill-mode: forwards`,
    // so a stale `closing` keeps hiding a row that is now open — the row snaps
    // shut and springs back. `setClosing(open)` arms it on collapse and disarms
    // it on reopen, and the effect above cancels the pending timer either way.
    setClosing(open)
    setOpen(v => !v)
    // The transcript may be pinned to the bottom, so growing content pushes the
    // header up and out. `nearest` reveals it again with the smallest possible
    // correction rather than fighting the auto-follow.
    if (!open) requestAnimationFrame(() => rowRef.current?.scrollIntoView({ block: 'nearest' }))
  }
  // The chevron is the explicit toggle; header whitespace toggles too (see
  // `headerClickAction`), while the filename opens the file — so clicking the
  // filename never collapses the diff out from under it.
  const prefix = () => (
    <button
      data-testid={`fcc-toggle-${fc.path}`}
      onClick={toggle}
      aria-expanded={open}
      aria-label={i18nT('components.fileChangeChips.toggle_diff', { path: fc.path })}
      className="shrink-0 flex items-center justify-center w-[16px] h-[16px] rounded text-muted hover:text-text cursor-pointer bg-transparent border-none"
    >
      {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
    </button>
  )
  // Opening a file is driven by clicking the FILENAME, which Pierre renders
  // inside its shadow root — so this keeps a keyboard- and screen-reader-
  // reachable control for the same action. It is visually hidden rather than
  // absent because a pointer-only affordance would strand keyboard users.
  const filenameSuffix = () => (
    onFileOpen ? (
      <button
        onClick={() => onFileOpen(fc.path)}
        className="sr-only focus-visible:not-sr-only focus-visible:ml-1.5 focus-visible:px-1.5 focus-visible:py-0.5 focus-visible:rounded focus-visible:text-[11px] focus-visible:text-text focus-visible:bg-bg-hover focus-visible:border focus-visible:border-border cursor-pointer bg-transparent"
        title={i18nT('components.fileChangeChips.open_in_side_panel', { path: fc.path })}
        aria-label={i18nT('components.fileChangeChips.open_in_side_panel', { path: fc.path })}
      >
        {i18nT('components.fileChangeChips.open')}
      </button>
    ) : null
  )
  // Clicks on our own slotted controls (the chevron, the sr-only Open button,
  // the artifact pill) return early: those are light-DOM children of this
  // wrapper, so they are NOT retargeted and would otherwise be handled twice.
  // The rest is decided by `headerClickAction` above.
  const onRowClick = (e: React.MouseEvent) => {
    if (e.target instanceof Element && e.target.closest('button')) return
    const action = headerClickAction(e.nativeEvent.composedPath?.() ?? [])
    if (action === 'open') {
      if (onFileOpen) onFileOpen(fc.path)
    } else if (action === 'toggle') {
      toggle()
    }
  }
  const metadata = () => (
    <span className="flex items-center gap-2">
      {isArtifact && (
        <span
          className="shrink-0 text-[10px] leading-none px-1.5 py-0.5 rounded-full border border-border text-muted font-medium"
          title={i18nT('components.fileChangeChips.this_document_is_tracked_as_a_session_artifact_n')}
        >
          {i18nT('components.fileChangeChips.artifact')}
        </span>
      )}
      <DiffStatBar added={added} removed={removed} />
    </span>
  )
  return (
    /* This wrapper delegates clicks to Pierre's shadow-DOM filename; it is not
       itself the control, so a role and tab stop here would announce a button
       that spans the whole diff. The keyboard and screen-reader path is the
       visually-hidden Open button in the filename-suffix slot above. */
    /* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */
    <div
      ref={rowRef}
      data-testid={`fcc-row-${fc.path}`}
      className="fcc-row group/fcrow pierre-surface"
      /* The row shows the basename, so two changed files sharing a name render
         as identical rows; the full path lives here as a tooltip. Pierre paints
         the title inside its shadow root, and a native `title` resolves up the
         flat tree, so hovering the filename picks this up. */
      title={fc.path}
      onClick={onRowClick}
    >
      <PierreFilePair
        oldFile={oldFile}
        newFile={newFile}
        options={options}
        renderHeaderPrefix={prefix}
        renderHeaderFilenameSuffix={filenameSuffix}
        renderHeaderMetadata={metadata}
      />
    </div>
  )
}

/* ── Expanded: a single elevated card grouping the changed files into aligned
 *   rows, with a header carrying a neutral icon chip, the file count, and
 *   worded totals ("N additions" / "N removals", each shown when its side is
 *   nonzero). Reads as one structured unit.
 *   `artifactPaths` (paths the session tracks as documents/artifacts) badges
 *   those rows so generated docs read distinctly from source-file edits.
 *   Long lists are capped at COLLAPSED_COUNT rows behind a "Show N more"
 *   toggle so a big turn doesn't wall off the transcript (the header still
 *   shows the true total + aggregate stats while collapsed).                */
const COLLAPSED_COUNT = 8

function ExpandedList({ fileChanges, onFileOpen, artifactPaths, disclosureKey }: {
  fileChanges: FileChangeEntry[]
  onFileOpen?: (path: string) => void
  artifactPaths?: Set<string>
  disclosureKey?: string
}) {
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const n = fileChanges.length
  // Count once per file: reused by each row AND the header roll-up.
  const stats = fileChanges.map(fc => countLines(fc.before, fc.after))
  const totalAdded = stats.reduce((s, x) => s + x.added, 0)
  const totalRemoved = stats.reduce((s, x) => s + x.removed, 0)
  const overflow = n > COLLAPSED_COUNT
  const visibleCount = overflow && !expanded ? COLLAPSED_COUNT : n
  const hiddenCount = n - COLLAPSED_COUNT
  return (
    <div className="ft-block-reveal mt-2 mb-1.5 w-full max-w-full rounded-xl border border-border bg-bg-elevated overflow-hidden">
      {/* Matches Pierre's header band exactly: 44px min-height, the same
          inline padding as ROW_CSS_BASE sets on the file headers, and the
          13px/20px header font — which `.pierre-surface` maps to var(--mono),
          so `font-mono` here is Pierre's face, not an unrelated pin.
          The roll-up is spelled out inline rather than repeated as a ±pair on
          the right, so the row carries one summary instead of two. */}
      <div className="flex items-center gap-2 px-[10px] min-h-[36px] bg-[color-mix(in_srgb,var(--bg-elevated)_50%,var(--bg))] border-b border-border font-mono text-[12px] leading-[18px] text-muted">
        <FileDiff size={14} className="text-muted shrink-0" />
        <span className="font-medium">{i18nT('components.fileChangeChips.file', { count: n })} {i18nT('components.fileChangeChips.changed')}</span>
        {(totalAdded > 0 || totalRemoved > 0) && (
          <>
            <span className="text-muted/50" aria-hidden="true">·</span>
            {totalAdded > 0 && (
              <span className="tabular-nums">{i18nT('components.fileChangeChips.additions', { count: totalAdded })}</span>
            )}
            {totalRemoved > 0 && (
              <span className="tabular-nums">{i18nT('components.fileChangeChips.removals', { count: totalRemoved })}</span>
            )}
          </>
        )}
      </div>
      <div className="flex flex-col">
        {fileChanges.slice(0, visibleCount).map((fc, i) => (
          <ExpandedRow
            key={fc.path}
            fc={fc}
            added={stats[i].added}
            removed={stats[i].removed}
            isArtifact={artifactPaths?.has(fc.path)}
            onFileOpen={onFileOpen}
            // Per-file key so each row's open/closed state survives a
            // re-render (and a scroll-out remount) independently.
            disclosureKey={disclosureKey ? `${disclosureKey}-${fc.path}` : undefined}
          />
        ))}
        {overflow && (
          <button
            onClick={() => setExpanded(v => !v)}
            className="flex items-center justify-center gap-1 w-full px-4 py-2 text-[11.5px] font-medium text-muted hover:text-text hover:bg-bg-elevated cursor-pointer transition-colors bg-transparent border-none"
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
 * - `expanded` (default): one card, one Pierre diff per changed file collapsed
 *   to its native header. Clicking a header expands that file's diff INLINE;
 *   the header's Open button routes to the side-panel file tab instead.
 * - `minimal`: stats-only glass pills that wrap, filename on hover. Clicking
 *   one still opens the standalone diff tab via `onOpenDiff`.
 */
const FileChangeChips = memo(function FileChangeChips({ fileChanges, onOpenDiff, onFileOpen, style = 'expanded', artifactPaths, disclosureKey }: {
  fileChanges: FileChangeEntry[]
  /** Minimal style only — the expanded card diffs in place instead. */
  onOpenDiff?: (path: string, modified: string, original: string) => void
  /** Opens the file as a side-panel tab from a row's Open button. */
  onFileOpen?: (path: string) => void
  style?: FileChipStyle
  /** Paths the session tracks as documents/artifacts — badged in the expanded
   *  card so generated docs read distinctly from source-file edits. */
  artifactPaths?: Set<string>
  disclosureKey?: string
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
  return <ExpandedList fileChanges={fileChanges} onFileOpen={onFileOpen} artifactPaths={artifactPaths} disclosureKey={disclosureKey} />
})

export default FileChangeChips
