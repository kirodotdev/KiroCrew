import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FileDiff, ChevronDown, ChevronUp, ChevronRight, Columns2 } from 'lucide-react'
import type { FileChipStyle } from '../pages/chat/ChatSettings'
import { useRowDisclosure } from '../pages/chat/rowDisclosure'
import { PierreFilePair } from '../pierre'
import {
  ROW_ANIM_MS,
  ROW_BODY_MAX_H,
  ROW_CSS_CLICKABLE_TITLE,
  ROW_CSS_CLOSING,
  ROW_CSS_OPEN,
} from './fileChangeChipsCss'
import { countLines } from '../utils/diffLineCounts'
import { useDiffSplit } from '../hooks/useDiffSplit'

import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
export interface FileChangeEntry {
  path: string
  before: string
  after: string
  truncated?: boolean
  snapshot_limit_chars?: number
  /** Content dropped because the whole turn outgrew its snapshot budget, not
   *  because this one file did. The path is still recorded, so the row opens
   *  the real file. */
  content_omitted?: boolean
  turn_budget_chars?: number
}

const LEGACY_SNAPSHOT_LIMIT_CHARS = 200_000

const turnBudgetSentence = (fc: FileChangeEntry) =>
  i18nT('components.fileChangeChips.diff_unavailable_turn_snapshot_budget', {
    limit: fmtNumber(fc.turn_budget_chars ?? LEGACY_SNAPSHOT_LIMIT_CHARS * 2),
  })

/* Two reasons a row carries no diff, and they are NOT the same sentence: one
 * file outgrew the per-file cap, or the turn as a whole outgrew its budget and
 * this file's content was dropped to keep the newest changes complete. Telling
 * a small file's owner it was "too large to compare" sends them looking for a
 * size problem that is not theirs.
 *
 * This notice is the per-file one, so it stays beside its file in both styles.
 * The turn-budget sentence describes the turn and is said ONCE per surface
 * (`TurnBudgetNotice`): a demoted card row wears only `DemotedTag`, and a
 * demoted pill wears its filename plus the same marker at a muted size,
 * because repeating the sentence per row squeezes each row's filename — the
 * one fact that differs between rows — into an ellipsis to make room.
 *
 * Two states, two vocabularies. A demoted row is still a file the reader can
 * open, so its marker names what the row offers (`file_only`). A file the
 * turn left out of the list is not here at all, so the card-level line says
 * exactly that (`omitted_files_notice`: not listed) and offers nothing. Neither
 * borrows the other's word, so a reader can tell a file that is here without
 * its diff from a file that is not here. */
function TruncatedNotice({ fc }: { fc: FileChangeEntry }) {
  const limit = fmtNumber(fc.snapshot_limit_chars ?? LEGACY_SNAPSHOT_LIMIT_CHARS)
  return <span className="text-muted text-[11px] italic">{i18nT('components.fileChangeChips.diff_unavailable_file_exceeds_snapshot_limit', { limit })}</span>
}

/** Same pill as the artifact badge, so a demoted row gains a tag in a shape
 *  the row already uses rather than a second style of caption — including the
 *  badge's `title`, so hovering the tag restates the turn-budget reason the
 *  card's notice row gives once. */
function DemotedTag({ fc }: { fc: FileChangeEntry }) {
  return <span data-fcc-demoted-tag title={turnBudgetSentence(fc)} className="shrink-0 text-[10px] leading-none px-1.5 py-0.5 rounded-full border border-border text-muted font-medium">{i18nT('components.fileChangeChips.file_only')}</span>
}

/** The omitted-files count as a positive integer, or 0 for an absent, zero or
 *  malformed field — an older message carries no field at all. The gateway
 *  sends a plain count with no ceiling, so none is applied here. */
const omittedFilesOf = (raw: unknown): number =>
  typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : 0

/* Files the turn's snapshot limits left out of the list altogether: entries
 * past the row cap and entries the character budget dropped, in one number.
 * It counts FILES, the same unit as the "N files
 * changed" header, so the two numbers add up for the reader. The sentence
 * says the files are not listed — the one fact the reader can check against
 * the card — and promises no way to see them: nothing anywhere can show a
 * file the turn did not keep. It is a fact about the turn, so the card says
 * it once beneath the header exactly as `TurnBudgetNotice` does, and a
 * minimal pill row says it once after the pills. Plain text, not an
 * ErrorNotice: nothing failed and there is nothing to retry. `count` picks
 * the plural form; `files` is the locale-formatted digits. */
function OmittedFilesNotice({ count, className }: { count: number; className: string }) {
  return (
    <div data-fcc-omitted-notice className={className}>
      {i18nT('components.fileChangeChips.omitted_files_notice', { count, files: fmtNumber(count) })}
    </div>
  )
}

/* One notice per surface: a row directly beneath the card header in the
 * header's face, or a full-width line after the minimal pills. The open hint
 * renders only when a file-open handler exists: a demoted row's filename is a
 * button exactly then, and the hint must not promise a click that does
 * nothing. It is status about a turn, not the outcome of a failed request, so
 * it is plain text and not an ErrorNotice. */
const NOTICE_ROW_CLASS = 'px-[10px] py-1.5 border-b border-border font-mono text-[11px] leading-[18px] text-muted italic'
const PILL_ROW_NOTICE_CLASS = 'basis-full text-muted text-[11px] italic'

function TurnBudgetNotice({ fc, canOpen, className }: { fc: FileChangeEntry; canOpen: boolean; className: string }) {
  return (
    <div data-fcc-turn-notice className={className}>
      {turnBudgetSentence(fc)}
      {canOpen && <> {i18nT('components.fileChangeChips.turn_snapshot_budget_open_hint')}</>}
    </div>
  )
}

/** Re-exported for this component's existing importers; defined in
 *  `utils/diffLineCounts` so a pure test need not import this module (and with
 *  it the Pierre diff runtime). */
export { countLines }

const basename = (p: string) => p.split('/').pop() || p
const FALLBACK_CONTENT_STYLE = { maxHeight: ROW_BODY_MAX_H, overflowY: 'auto' } as const

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
  if (!has('[data-diffs-header], [data-fcc-header]')) return 'ignore'
  return has('[data-title], [data-fcc-filename]') ? 'open' : 'toggle'
}

function RowMetadata({ added, removed, isArtifact }: {
  added: number
  removed: number
  isArtifact?: boolean
}) {
  return (
    <span
      data-testid="fcc-metadata"
      className="ml-auto flex min-w-0 items-center gap-2"
    >
      <span className="flex items-center gap-2">
        {isArtifact && (
          <span
            data-fcc-artifact-badge
            className="shrink-0 text-[10px] leading-none px-1.5 py-0.5 rounded-full border border-border text-muted font-medium"
            title={i18nT('components.fileChangeChips.this_document_is_tracked_as_a_session_artifact_n')}
          >
            {i18nT('components.fileChangeChips.artifact')}
          </span>
        )}
        <span
          data-fcc-secondary-metadata
          className={isArtifact ? 'flex max-[420px]:hidden' : 'flex'}
        >
          <DiffStatBar added={added} removed={removed} />
        </span>
      </span>
    </span>
  )
}

function CollapsedRowHeader({ fc, added, removed, isArtifact, onFileOpen, onToggle }: {
  fc: FileChangeEntry
  added: number
  removed: number
  isArtifact?: boolean
  onFileOpen?: (path: string) => void
  onToggle: () => void
}) {
  const name = basename(fc.path)
  return (
    /* Header whitespace delegates through ExpandedRow's outer click listener;
       the chevron remains the keyboard disclosure control and the filename
       remains a separate file-open action. */
    <div
      data-fcc-header
      data-testid={`fcc-header-${fc.path}`}
      className="flex items-center gap-2 min-h-[36px] px-[10px] py-1.5 bg-[color-mix(in_srgb,var(--bg-elevated)_50%,var(--bg))] font-mono text-[12px] leading-[18px] text-muted"
    >
      <button
        data-testid={`fcc-toggle-${fc.path}`}
        onClick={e => { e.stopPropagation(); onToggle() }}
        aria-expanded={false}
        aria-label={i18nT('components.fileChangeChips.toggle_diff', { path: fc.path })}
        className="shrink-0 flex items-center justify-center w-[16px] h-[16px] rounded text-muted hover:text-text cursor-pointer bg-transparent border-none"
      >
        <ChevronRight size={13} />
      </button>
      <FileDiff size={13} className="shrink-0 text-muted" aria-hidden />
      {onFileOpen ? (
        <button
          type="button"
          data-fcc-filename
          className="min-w-0 truncate text-left text-muted hover:text-accent cursor-pointer bg-transparent border-none p-0 font-mono text-[12px]"
          onClick={e => { e.stopPropagation(); onFileOpen(fc.path) }}
          title={fc.path}
          aria-label={i18nT('components.fileChangeChips.open_in_side_panel', { path: fc.path })}
        >
          {name}
        </button>
      ) : (
        <span className="min-w-0 truncate" title={fc.path} data-fcc-filename>{name}</span>
      )}
      <RowMetadata added={added} removed={removed} isArtifact={isArtifact} />
      <span className="flex min-w-[8ch] items-center justify-end gap-1">
        <Stats added={added} removed={removed} />
      </span>
    </div>
  )
}

function TruncatedRow({ fc, isArtifact, onFileOpen }: {
  fc: FileChangeEntry
  isArtifact?: boolean
  onFileOpen?: (path: string) => void
}) {
  const name = basename(fc.path)
  return (
    <div data-testid={`fcc-row-${fc.path}`} className="fcc-row group/fcrow pierre-surface" title={fc.path}>
      <div data-fcc-header className="flex items-center gap-2 min-h-[36px] px-[10px] py-1.5 bg-[color-mix(in_srgb,var(--bg-elevated)_50%,var(--bg))] font-mono text-[12px] leading-[18px] text-muted">
        <FileDiff size={13} className="shrink-0 text-muted" aria-hidden />
        {onFileOpen ? (
          <button type="button" data-fcc-filename className="min-w-0 truncate text-left text-muted hover:text-accent cursor-pointer bg-transparent border-none p-0 font-mono text-[12px]" onClick={() => onFileOpen(fc.path)} title={fc.path} aria-label={i18nT('components.fileChangeChips.open_in_side_panel', { path: fc.path })}>{name}</button>
        ) : (
          <span className="min-w-0 truncate" title={fc.path} data-fcc-filename>{name}</span>
        )}
        {isArtifact && <span data-fcc-artifact-badge className="shrink-0 text-[10px] leading-none px-1.5 py-0.5 rounded-full border border-border text-muted font-medium">{i18nT('components.fileChangeChips.artifact')}</span>}
        <span className="ml-auto shrink-0">{fc.content_omitted ? <DemotedTag fc={fc} /> : <TruncatedNotice fc={fc} />}</span>
      </div>
    </div>
  )
}

const ROW_WARM_FALLBACK_MAX_CHARS = 8_000
const ROW_WARM_FALLBACK_ELLIPSIS = String.fromCodePoint(0x2026)

function ExpandedRow({ fc, added, removed, isArtifact, onFileOpen, disclosureKey, sideBySide }: {
  fc: FileChangeEntry
  added: number
  removed: number
  isArtifact?: boolean
  onFileOpen?: (path: string) => void
  disclosureKey?: string
  /** Split vs unified layout — owned by the card so every row flips together. */
  sideBySide?: boolean
}) {
  const [open, setOpen] = useRowDisclosure(disclosureKey, false)
  // Held mounted for one animation after `open` goes false, so collapsing has
  // a frame to animate in before Pierre drops the body.
  const [closing, setClosing] = useState(false)
  const rowRef = useRef<HTMLDivElement>(null)
  const pierreToggleRef = useRef<HTMLButtonElement | null>(null)
  const openFocusPending = useRef(false)
  const collapseFocusPending = useRef(false)
  const [focusProxy, setFocusProxy] = useState(false)
  const renderPierre = open || closing
  // Pierre titles the header from `name`; the full path would wrap the row and
  // bury the filename, so the row shows the basename and the path stays on the
  // Open button's tooltip.
  const name = basename(fc.path)
  const toggleLabel = i18nT('components.fileChangeChips.toggle_diff', { path: fc.path })
  const oldFile = useMemo(() => ({ name, contents: fc.before }), [name, fc.before])
  const newFile = useMemo(() => ({ name, contents: fc.after }), [name, fc.after])
  // Depend on WHETHER a file-open handler exists, never on its identity: the
  // options only splice a CSS block in when the title is clickable, so an
  // unstable callback from a parent must not re-create `options` — Pierre
  // re-initializes the diff view when options change identity, which reads as
  // the row flashing/reloading.
  const clickableTitle = !!onFileOpen
  const options = useMemo(
    () => ({
      collapsed: false,
      diffStyle: (sideBySide ? 'split' : 'unified') as 'split' | 'unified',
      overflow: 'wrap' as const,
      disableFileHeader: false,
      unsafeCSS: (closing ? ROW_CSS_CLOSING : ROW_CSS_OPEN) + (clickableTitle ? ROW_CSS_CLICKABLE_TITLE : ''),
    }),
    [closing, clickableTitle, sideBySide],
  )
  useEffect(() => {
    if (!closing) {
      if (collapseFocusPending.current) {
        collapseFocusPending.current = false
        rowRef.current?.querySelector<HTMLElement>('[data-testid^="fcc-toggle-"]')?.focus()
        rowRef.current?.removeAttribute('tabindex')
        setFocusProxy(false)
      }
      return
    }
    const t = setTimeout(() => setClosing(false), ROW_ANIM_MS)
    return () => clearTimeout(t)
  }, [closing])
  const toggle = () => {
    // Reopening inside the collapse window must CLEAR `closing`, not leave it:
    // the closing stylesheet runs `fccHide` with `animation-fill-mode: forwards`,
    // so a stale `closing` keeps hiding a row that is now open — the row snaps
    // shut and springs back. `setClosing(open)` arms it on collapse and disarms
    // it on reopen, and the effect above cancels the pending timer either way.
    const focusInside = !!rowRef.current?.contains(document.activeElement)
    openFocusPending.current = !open && focusInside
    collapseFocusPending.current = open && focusInside
    const handoffPending = openFocusPending.current || collapseFocusPending.current
    setFocusProxy(handoffPending)
    if (handoffPending && rowRef.current) {
      rowRef.current.tabIndex = -1
      rowRef.current.focus({ preventScroll: true })
    }
    setClosing(open)
    setOpen(v => !v)
    // The transcript may be pinned to the bottom, so growing content pushes the
    // header up and out. `nearest` reveals it again with the smallest possible
    // correction rather than fighting the auto-follow.
    if (!open) {
      requestAnimationFrame(() => {
        rowRef.current?.scrollIntoView({ block: 'nearest' })
        completeOpenFocus()
      })
    }
  }
  const onRowBlur = (e: React.FocusEvent<HTMLDivElement>) => {
    // A delayed lazy mount or collapse timer may finish after the user has
    // deliberately moved elsewhere. Keep the handoff only while focus moves
    // within this row; never reclaim a different control's focus later.
    if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return
    openFocusPending.current = false
    collapseFocusPending.current = false
    setFocusProxy(false)
    e.currentTarget.removeAttribute('tabindex')
  }
  const onRowKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!focusProxy || e.target !== e.currentTarget || (e.key !== 'Enter' && e.key !== ' ')) return
    e.preventDefault()
    toggle()
  }
  const completeOpenFocus = useCallback(() => {
    const el = pierreToggleRef.current
    if (!el || !openFocusPending.current) return
    if (typeof window !== 'undefined' && window.getComputedStyle(el).visibility === 'hidden') return
    el.focus()
    if (!el.matches(':focus')) return
    openFocusPending.current = false
    setFocusProxy(false)
    rowRef.current?.removeAttribute('tabindex')
  }, [])
  const setPierreToggle = useCallback((el: HTMLButtonElement | null) => {
    pierreToggleRef.current = el
    completeOpenFocus()
  }, [completeOpenFocus])
  // The chevron is the explicit toggle; header whitespace toggles too (see
  // `headerClickAction`), while the filename opens the file — so clicking the
  // filename never collapses the diff out from under it.
  const prefix = () => (
    <button
      ref={setPierreToggle}
      data-testid={`fcc-toggle-${fc.path}`}
      onClick={toggle}
      aria-expanded={open}
      aria-label={toggleLabel}
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
    /* This wrapper delegates clicks to Pierre's shadow-DOM filename and is
       normally not a control. While a lazy/animated mount swap parks focus on
       the persistent row, it temporarily exposes the chevron's disclosure
       semantics and keyboard handling; the real chevron retakes focus and the
       proxy semantics are removed as soon as the handoff completes. */
    /* eslint-disable-next-line jsx-a11y/no-static-element-interactions */
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
      onBlur={onRowBlur}
      onKeyDown={onRowKeyDown}
      role={focusProxy ? 'button' : undefined}
      aria-label={focusProxy ? toggleLabel : undefined}
      aria-expanded={focusProxy ? open : undefined}
    >
      {renderPierre ? (
        <PierreFilePair
          oldFile={oldFile}
          newFile={newFile}
          options={options}
          fallbackText={fc.after.length > ROW_WARM_FALLBACK_MAX_CHARS
            ? `${fc.after.slice(0, ROW_WARM_FALLBACK_MAX_CHARS)}\n${ROW_WARM_FALLBACK_ELLIPSIS}`
            : fc.after}
          fallbackClassName="max-h-[376px] overflow-auto"
          fallbackContentStyle={FALLBACK_CONTENT_STYLE}
          onVisible={completeOpenFocus}
          renderHeaderPrefix={prefix}
          renderHeaderFilenameSuffix={filenameSuffix}
          renderHeaderMetadata={metadata}
          titleClickable={clickableTitle}
        />
      ) : (
        <CollapsedRowHeader
          fc={fc}
          added={added}
          removed={removed}
          isArtifact={isArtifact}
          onFileOpen={onFileOpen}
          onToggle={toggle}
        />
      )}
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

function ExpandedList({ fileChanges, omittedFiles, onFileOpen, artifactPaths, disclosureKey }: {
  fileChanges: FileChangeEntry[]
  /** Already validated by `omittedFilesOf`; 0 renders no notice. */
  omittedFiles: number
  onFileOpen?: (path: string) => void
  artifactPaths?: Set<string>
  disclosureKey?: string
}) {
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  // Shares the app-wide `mc-diff-split` preference with the other diff
  // surfaces (#6024). One toggle on the card, not per row, so it flips the
  // layout of every file in the card together.
  const [sideBySide, setSideBySide] = useDiffSplit()
  const n = fileChanges.length
  // Count once per file: reused by each row AND the header roll-up.
  const stats = fileChanges.map(fc => fc.truncated ? { added: 0, removed: 0 } : countLines(fc.before, fc.after))
  const hasTruncatedSnapshot = fileChanges.some(fc => fc.truncated)
  const hasCompleteDiff = fileChanges.some(fc => !fc.truncated)
  // Every demoted entry in one turn carries the same budget, so the first one
  // speaks for the card.
  const demoted = fileChanges.find(fc => fc.content_omitted)
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
          the right, so the row carries one summary instead of two.
          flex-wrap + py-1.5 let the toggle wrap below the summary on narrow
          viewports (~320px with long i18n labels) instead of being clipped by
          the card's overflow-hidden; min-h keeps the desktop render identical. */}
      <div className="flex flex-wrap items-center gap-2 px-[10px] py-1.5 min-h-[36px] bg-[color-mix(in_srgb,var(--bg-elevated)_50%,var(--bg))] border-b border-border font-mono text-[12px] leading-[18px] text-muted">
        <FileDiff size={14} className="text-muted shrink-0" />
        <span className="font-medium">{i18nT('components.fileChangeChips.file', { count: n })} {i18nT('components.fileChangeChips.changed')}</span>
        {!hasTruncatedSnapshot && (totalAdded > 0 || totalRemoved > 0) && (
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
        {/* Split/unified toggle for the whole card — same active styling as the
            side panel's toggle (lit in split mode; diffSplitToggles.test.ts
            asserts the gate is not inverted). Always visible: the header bar
            has no hover reveal, unlike DiffBlock's slotted controls. */}
        {hasCompleteDiff && <button onClick={() => setSideBySide(v => !v)} className={`ml-auto flex items-center justify-center w-[22px] h-[22px] rounded-md cursor-pointer transition-colors border-none shrink-0 ${sideBySide ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`} title={sideBySide ? i18nT('components.fileChangeChips.switch_to_unified_view') : i18nT('components.fileChangeChips.switch_to_split_view')} aria-label={sideBySide ? i18nT('components.fileChangeChips.switch_to_unified_view') : i18nT('components.fileChangeChips.switch_to_split_view')}><Columns2 size={13} /></button>}
      </div>
      {demoted && <TurnBudgetNotice fc={demoted} canOpen={!!onFileOpen} className={NOTICE_ROW_CLASS} />}
      {omittedFiles > 0 && <OmittedFilesNotice count={omittedFiles} className={NOTICE_ROW_CLASS} />}
      <div className="flex flex-col">
        {fileChanges.slice(0, visibleCount).map((fc, i) => (
          fc.truncated ? (
            <TruncatedRow
              key={fc.path}
              fc={fc}
              isArtifact={artifactPaths?.has(fc.path)}
              onFileOpen={onFileOpen}
            />
          ) : (
            <ExpandedRow
              key={fc.path}
              fc={fc}
              added={stats[i].added}
              removed={stats[i].removed}
              isArtifact={artifactPaths?.has(fc.path)}
              onFileOpen={onFileOpen}
              sideBySide={sideBySide}
              // Per-file key so each row's open/closed state survives a
              // re-render (and a scroll-out remount) independently.
              disclosureKey={disclosureKey ? `${disclosureKey}-${fc.path}` : undefined}
            />
          )
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

/* ── Minimal: stats-only liquid-glass pill, filename hovers above on hover ──
 *
 * `named` is true for a row that holds a demoted pill. A demoted pill has no
 * numbers, so its face is its filename plus a muted `file_only` marker; the
 * stats pill beside it then names its file too, because a row where some
 * pills say which file they are about and one does not leaves the reader
 * unsure whether the anonymous pill stands for the missing file or for the
 * whole set. A row with no demoted pill keeps the stats-only face, filename
 * on hover. */
function MinimalChip({ fc, named, onClick }: { fc: FileChangeEntry; named: boolean; onClick: () => void }) {
  const { added, removed } = fc.truncated ? { added: 0, removed: 0 } : countLines(fc.before, fc.after)
  return (
    <span className="relative inline-flex group/tip">
      <span className="glass-surface absolute bottom-full left-0 mb-1 px-2 py-0.5 rounded-md text-[11px] font-medium text-text whitespace-nowrap font-mono z-10 pointer-events-none opacity-0 translate-y-1 group-hover/tip:opacity-100 group-hover/tip:translate-y-0 transition-all duration-150">
        {basename(fc.path)}
      </span>
      {fc.content_omitted ? (
        /* A demoted pill shows its filename and the `file_only` marker, muted
           and a size down so it never competes with the name: without it a
           demoted pill is a bare filename that reads as a healthy, complete one.
           The turn-budget sentence is about the turn and the pill row says it
           once (`TurnBudgetNotice`), so three demoted pills read as three files
           rather than one repeated line. The pill wraps, so `text-left` keeps a
           basename that breaks at a hyphen starting under the left edge like
           every other pill. */
        <button type="button" onClick={onClick} className="glass-surface file-chip inline-flex max-w-full min-w-0 min-h-[22px] h-auto items-center gap-1 px-2.5 py-1 whitespace-normal text-left rounded-full text-[11px] font-medium cursor-pointer" aria-label={fc.path}>
          <span data-fcc-pill-filename className="font-mono text-text">{basename(fc.path)}</span>
          <span data-fcc-pill-marker className="shrink-0 text-[10px] leading-none text-muted">{i18nT('components.fileChangeChips.file_only')}</span>
        </button>
      ) : fc.truncated ? (
        <button type="button" onClick={onClick} className="glass-surface file-chip inline-flex max-w-full min-w-0 min-h-[22px] h-auto items-center gap-1 px-2.5 py-1 whitespace-normal rounded-full text-[11px] font-medium cursor-pointer" aria-label={fc.path}>
          <TruncatedNotice fc={fc} />
        </button>
      ) : (
        <button type="button" onClick={onClick} className="glass-surface file-chip inline-flex items-center gap-1 h-[22px] px-2.5 rounded-full text-[11px] font-medium cursor-pointer" aria-label={fc.path}>
          {named && <span data-fcc-pill-filename className="font-mono text-text">{basename(fc.path)}</span>}
          <Stats added={added} removed={removed} />
        </button>
      )}
    </span>
  )
}

/**
 * Renders the file-change block below an assistant message.
 *
 * - `expanded` (default): one card with a lightweight summary header per
 *   changed file. Closed rows never load Pierre. Clicking a header expands the
 *   file inline and mounts Pierre until the row finishes collapsing; the
 *   filename opens the side-panel file tab when that action is available.
 * - `minimal`: stats-only glass pills that wrap, filename on hover. A complete
 *   snapshot opens its standalone diff via `onOpenDiff`; a truncated snapshot
 *   opens the actual file via `onFileOpen` because no trustworthy diff exists.
 *   A turn's notices (budget, omitted files) follow the pills, once each.
 *
 * No entries renders nothing, whatever `omittedFiles` says: the gateway
 * attaches the count only alongside a non-empty list, and the budget always
 * retains at least one entry, so a notice with no rows has no producer.
 */
const FileChangeChips = memo(function FileChangeChips({ fileChanges, omittedFiles, onOpenDiff, onFileOpen, style = 'expanded', artifactPaths, disclosureKey }: {
  fileChanges: FileChangeEntry[]
  /** `meta.file_changes_omitted_files` as the gateway sent it; absent is 0. */
  omittedFiles?: unknown
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  if (!fileChanges?.length) return null
  const omitted = omittedFilesOf(omittedFiles)
  // Minimal keeps the wrapping pill row; anything else uses the grouped card.
  if (style === 'minimal') {
    // Every demoted entry in one turn carries the same budget, so the first
    // one speaks for the row.
    const demoted = fileChanges.find(fc => fc.content_omitted)
    return (
      <div className="ft-block-reveal flex flex-wrap items-center gap-1.5 mt-2 mb-1.5">
        {fileChanges.map(fc => (
          <MinimalChip
            key={fc.path}
            fc={fc}
            named={!!demoted}
            onClick={fc.truncated
              ? () => onFileOpen?.(fc.path)
              : () => onOpenDiff?.(fc.path, fc.after, fc.before)}
          />
        ))}
        {demoted && <TurnBudgetNotice fc={demoted} canOpen={!!onFileOpen} className={PILL_ROW_NOTICE_CLASS} />}
        {omitted > 0 && <OmittedFilesNotice count={omitted} className={PILL_ROW_NOTICE_CLASS} />}
      </div>
    )
  }
  return <ExpandedList fileChanges={fileChanges} omittedFiles={omitted} onFileOpen={onFileOpen} artifactPaths={artifactPaths} disclosureKey={disclosureKey} />
})

export default FileChangeChips
