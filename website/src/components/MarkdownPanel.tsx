import { safeSetItem } from '../utils/safeStorage'
import { memo, useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo, useImperativeHandle, forwardRef, lazy, Suspense } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, Ellipsis, ChevronRight, Columns2, Hash, WrapText, Zap, Maximize2, Minimize2, MessageSquare, MessageSquarePlus, Copy, BookOpen, BookmarkPlus, Camera, Check, X, Component, FileText, FileDiff, CaseSensitive, ChevronUp, ChevronDown } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import hljs from '../utils/hljs'
import DOMPurify from 'dompurify'
import DetailPanel from './DetailPanel'
import Clickable from './Clickable'
import { CommentPopover, CommentList, formatCommentsMessage, type InlineComment } from './CommentOverlay'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import SelectionToolbar, { type SelectionAction } from './SelectionToolbar'
import MarkdownOutlineRail from './MarkdownToc'
import { useFileWatch } from '../hooks/useFileWatch'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { countLines } from './FileChangeChips'
import { store } from '../store'
import { findBestOccurrence } from '../hooks/useMarkdownCommentHighlights'
import { detectFileType } from './FileRenderers'
import { ContentRenderer, MD_EXTS, extOf, langFor, wrapCode } from './ContentRenderer'
import { api } from '../api/client'
import { fileReadUrl, fileDownloadUrl } from '../utils/fileReadUrl'
import { loadCommentDrafts, saveCommentDrafts, setCommentsForFile } from '../utils/commentDrafts'
import { copyToClipboard } from '../utils/clipboard'

// ── CSS Custom Highlight API accessors ───────────────────────────────────────
// Preview find highlights matches via the browser-native CSS Custom Highlight
// API (CSS.highlights + Range) instead of injecting <mark> nodes. The preview
// is React-reconciled (react-markdown), so mutating its DOM would crash React
// on the next re-render; ranges live outside the DOM and never touch it.
// These types aren't in this TS lib yet, so we reach them through narrow casts
// and feature-detect at runtime (graceful no-highlight fallback when absent).
type FindHighlight = object
const FindHighlightCtor: (new (...ranges: Range[]) => FindHighlight) | undefined =
  typeof window !== 'undefined'
    ? (window as unknown as { Highlight?: new (...r: Range[]) => FindHighlight }).Highlight
    : undefined
const cssHighlights: { set(n: string, h: FindHighlight): void; delete(n: string): boolean } | undefined =
  typeof CSS !== 'undefined'
    ? (CSS as unknown as { highlights?: { set(n: string, h: FindHighlight): void; delete(n: string): boolean } }).highlights
    : undefined
const FIND_HL_SUPPORTED = !!FindHighlightCtor && !!cssHighlights
// Global registry names. Assumes a single active markdown preview at a time
// (the panel's findActiveRef already encodes that assumption); a second
// concurrent preview find would share these names — a harmless visual overlap,
// never a crash.
const FIND_HL_ALL = 'mc-find'
const FIND_HL_CURRENT = 'mc-find-current'

/**
 * Locate the first char of `selected` in the raw source `content` and return
 * 1-based (line, column). Works perfectly for code files where rendered text
 * equals source text. For markdown, used as a fallback when DOM-based
 * `resolveSourcePos` can't resolve coordinates (rare). Exported for tests.
 */
/** One rendered breadcrumb segment: its display text, the ABSOLUTE path up to
 *  and including it (so a clicked directory opens that exact folder even though
 *  only the last three segments are shown), and whether it is the file itself. */
export interface BreadcrumbSegment { seg: string; path: string; isFile: boolean }

/**
 * Split a file path into the last three breadcrumb segments, each carrying its
 * own absolute path. The final segment is the open file (never a folder target);
 * the earlier ones are its ancestor directories.
 *
 * A leading slash is preserved explicitly: joining segments with '/' drops it,
 * which would turn an absolute path into a relative one the folder browser then
 * resolves against the wrong root. Exported for unit tests.
 */
export function breadcrumbSegments(filePath: string): BreadcrumbSegment[] {
  const isAbs = filePath.startsWith('/')
  const allSegs = filePath.replace(/\/+$/, '').split('/').filter(Boolean)
  const shown = Math.min(3, allSegs.length)
  return allSegs.slice(-3).map((seg, j) => {
    const absIndex = allSegs.length - shown + j
    const joined = allSegs.slice(0, absIndex + 1).join('/')
    return { seg, path: isAbs ? '/' + joined : joined, isFile: absIndex === allSegs.length - 1 }
  })
}

export function findCoords(content: string, selected: string): { line: number; column: number } | undefined {
  if (!selected) return undefined
  const idx = content.indexOf(selected)
  if (idx < 0) return undefined
  const before = content.slice(0, idx)
  const nl = before.lastIndexOf('\n')
  const line = (before.match(/\n/g)?.length ?? 0) + 1
  const column = (nl < 0 ? idx : idx - nl - 1) + 1
  return { line, column }
}

/**
 * Resolve a selection `Range` inside a markdown-rendered `root` to 1-based
 * (line, column) in the source `content`, using the `data-sourcepos`
 * attributes emitted by the `rehypeSourcepos` plugin.
 *
 * Strategy: walk up from the selection start to the nearest ancestor element
 * carrying `data-sourcepos`, compute the rendered-text offset from that
 * element's start to the selection, then locate the corresponding char in
 * the element's source span by substring search scoped to that tight window
 * (duplicate-text ambiguity drops to near-zero vs global search). Returns
 * undefined when no ancestor carries a position (should not happen when the
 * renderer is built with `sourcePos`) — caller falls back to `findCoords`.
 * Exported for tests.
 */
export function resolveSourcePos(range: Range, root: HTMLElement, content: string): { line: number; column: number } | undefined {
  let el: HTMLElement | null = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? range.startContainer as HTMLElement
    : range.startContainer.parentElement
  while (el && el !== root && !el.hasAttribute('data-sourcepos')) el = el.parentElement
  if (!el || !el.hasAttribute('data-sourcepos')) return undefined
  const m = /^(\d+):(\d+)-(\d+):(\d+)$/.exec(el.getAttribute('data-sourcepos') || '')
  if (!m) return undefined
  // Block offset: useBlockAssembler splits raw content into separate
  // MarkdownBlocks, so data-sourcepos line numbers are relative to the
  // block's own text. The enclosing `[data-block-start]` wrapper carries
  // the 1-based line of that block within the full source.
  let blockEl: HTMLElement | null = el
  while (blockEl && blockEl !== root && !blockEl.hasAttribute('data-block-start')) blockEl = blockEl.parentElement
  const blockStart = blockEl?.hasAttribute('data-block-start') ? +(blockEl.getAttribute('data-block-start') || '1') : 1
  const lineOffset = blockStart - 1
  const sLine = +m[1] + lineOffset, sCol = +m[2], eLine = +m[3] + lineOffset, eCol = +m[4]
  // Rendered-text offset from element start to selection start
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  let offset = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (node === range.startContainer) { offset += range.startOffset; break }
    offset += (node as Text).data.length
  }
  // Extract the element's source span from `content`
  const lines = content.split('\n')
  if (sLine < 1 || eLine > lines.length) return { line: sLine, column: sCol }
  let span: string
  if (sLine === eLine) span = lines[sLine - 1].slice(sCol - 1, eCol - 1)
  else {
    const parts = [lines[sLine - 1].slice(sCol - 1)]
    for (let i = sLine; i < eLine - 1; i++) parts.push(lines[i])
    parts.push(lines[eLine - 1].slice(0, eCol - 1))
    span = parts.join('\n')
  }
  // Align rendered text to source span char-by-char: walk `span`, advancing
  // a rendered cursor whenever they match. When the rendered cursor equals
  // `offset`, the current span index is the source position of the selection.
  // Handles `**bold**`, `*em*`, `` `code` ``, `# Heading`, list `- ` / `> `,
  // and similar leading / wrapping / trailing syntax without enumerating
  // syntax characters (anything in span that isn't in rendered is syntax).
  const rendered = el.textContent || ''
  if (offset >= rendered.length) return { line: sLine, column: sCol }
  let spanIdx = 0
  let renderedIdx = 0
  while (spanIdx < span.length && renderedIdx < offset) {
    if (span[spanIdx] === rendered[renderedIdx]) renderedIdx++
    spanIdx++
  }
  // spanIdx now points at the position in span for rendered[renderedIdx] —
  // but may sit on leading syntax between the previous match and the target
  // rendered char. Advance past any such syntax to land on the target.
  while (spanIdx < span.length && span[spanIdx] !== rendered[offset]) spanIdx++
  // Exhausted the span without finding rendered[offset] — happens when
  // rendered text can't be aligned to source char-by-char (HTML entities
  // like `&amp;` → `&`, raw HTML like `<br>` → newline). Returning
  // element-start would silently mislead the agent; return undefined so
  // the caller falls back to `findCoords`.
  if (spanIdx >= span.length) return undefined
  // Convert spanIdx back to (line, column) in source
  let ln = sLine, cl = sCol
  for (let i = 0; i < spanIdx; i++) {
    if (span[i] === '\n') { ln++; cl = 1 } else cl++
  }
  return { line: ln, column: cl }
}
interface Props {
  filePath: string
  content: string
  onContentChange: (c: string) => void
  onSave: (filePath: string, content: string) => Promise<void>
  onClose: () => void
  liveWatch?: boolean
  onSubmitComments?: (message: string) => void
  onRefresh?: (filePath: string) => Promise<void>
  reserveWidth?: number
  /** Restored file-tab preference. Undefined allows the initial modified-file
   *  auto-diff; false explicitly keeps the normal preview/source view. */
  initialDiffMode?: boolean
  onDiffModeChange?: (diffMode: boolean) => void
  /** Render as a SidePanel tab body (fills parent, no resize handle/border). */
  embedded?: boolean
  /** Open a directory (e.g. a clicked path-breadcrumb segment) as a folder tab.
   *  Omitted where no filesystem-navigation surface exists (the standalone,
   *  non-embedded panel), in which case breadcrumb segments stay inert text. */
  onOpenFolder?: (dirPath: string) => void
  /** The on-disk (last-saved) content. When provided, "dirty" is computed as
   *  content !== savedBaseline instead of only being set by local edits — so an
   *  editor RESTORED with a pre-edited buffer (e.g. the Files-tab inline draft
   *  after a remount) is correctly dirty, and its close guard won't silently
   *  discard the restored edits. Omitted by document tabs (unchanged behavior). */
  savedBaseline?: string
  /**
   * A 1-based source line to scroll to and flash, from a `file.py:447` chip.
   *
   * Carries a `nonce` because the line alone is not a change: clicking the same
   * chip again, after scrolling away, must re-fire the reveal, and a bare
   * `line: 447` prop is `===` to the previous one so no effect would run. Same
   * shape as `CommentsSidebar`'s `flashCommentId`.
   */
  revealLine?: RevealTarget
  /** Called once a reveal has landed, so the owner can drop the target and keep
   *  it a true one-shot (see `useLineReveal`). */
  onRevealConsumed?: () => void
}

import { monacoLang, useIsDark } from './MonacoCodeBlock'
import { kirocrewDark, kirocrewLight } from './monacoTheme'
import type { IDisposable } from 'monaco-editor'
import { useLineReveal, type RevealTarget } from '../hooks/useLineReveal'
import { i18nT } from '../i18n/t'
const MonacoDiffEditor = lazy(async () => {
  const { ensureMonacoLocal } = await import('../utils/monacoLocal')
  await ensureMonacoLocal()
  const { DiffEditor } = await import('@monaco-editor/react')
  return { default: DiffEditor }
})

/**
 * File types that render through a dedicated viewer instead of a text editor.
 *
 * One owner because this list was spelled out twice — inline in the `editing`
 * initializer and again in `isRichType` — and a citation-forced source mode has to
 * agree with both, or a file lands in an editor the rest of the panel's chrome does
 * not support. The two copies differed only in `svg`, which `isRichType` omitted;
 * that was harmless rather than a live bug, because `detectFileType` maps a
 * path-backed `.svg` to `image` and never returns `svg` here. `svg` is kept in the
 * list for the content-string SVG that artifacts render.
 */
const RICH_FILE_TYPES = ['image', 'svg', 'csv', 'json', 'jsonl', 'html', 'pdf', 'excalidraw']

/** Comment hint banner — shown once per session for markdown files */
function CommentHint({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div
      className="flex items-center gap-2.5 px-3 py-2.5 bg-bg-elevated border rounded-lg text-[12px] animate-scale-in mx-1 mb-2"
      style={{ borderColor: 'color-mix(in srgb, var(--accent) 40%, transparent)' }}
    >
      <span className="text-muted shrink-0"><MessageSquare className="lucide-inline" /></span>
      <span className="flex-1 text-text">
        <strong className="text-text-strong font-semibold">{i18nT('components.markdownPanel.tip')}</strong> {i18nT('components.markdownPanel.select_any_text_to_add_inline_comments_then_subm')}
      </span>
      <button className="text-accent hover:text-accent-hover cursor-pointer bg-transparent border-none text-[11px] font-medium shrink-0" onClick={onDismiss}>{i18nT('components.markdownPanel.got_it')}</button>
    </div>
  )
}

const HINT_KEY = 'kirocrew:comment-hint-dismissed'

async function downloadFile(filePath: string) {
  try {
    const res = await fetch(fileDownloadUrl(filePath))
    // eslint-disable-next-line no-console -- surface download failures for diagnostics
    if (!res.ok) { console.error('downloadFile failed', res.status, res.statusText); alert(i18nT('components.markdownPanel.download_failed')); return }
    const blob = await res.blob()
    const a = document.createElement('a')
    const url = URL.createObjectURL(blob)
    a.href = url
    a.download = filePath.split('/').pop() || 'download'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 2_000)
    // eslint-disable-next-line no-console -- surface download failures for diagnostics
  } catch (err) { console.error('downloadFile failed', err); alert(i18nT('components.markdownPanel.download_failed')) }
}

/**
 * Hand the open file to the desktop: `open` launches it in the OS default
 * application, `reveal` selects it in Finder / Explorer / the Linux file
 * manager.
 *
 * A headless host (SSH, container, cloud desktop) has neither, and the backend
 * says so by answering with `copy` rather than an error — `api.revealPath` puts
 * the path on the clipboard in that case, so the alert here tells the user why
 * nothing appeared on screen instead of leaving the click looking broken. A
 * rejected request (a path the SEL guard treats as sensitive, or `open` on a
 * directory) surfaces the server's own message.
 */
async function revealOrOpen(filePath: string, action: 'open' | 'reveal') {
  try {
    const res = await api.revealPath(filePath, action)
    if (res?.copy) alert(i18nT('components.markdownPanel.path_copied_to_clipboard_no_desktop_available'))
  } catch (err) {
    // eslint-disable-next-line no-console -- surface reveal failures for diagnostics
    console.error('revealPath failed', err)
    alert((err as Error).message)
  }
}

/** 26px square icon toggle for the file toolbar (borderless, accent when on). */
const barIconBtn = (on: boolean) =>
  `flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors border-none shrink-0 ${on ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`

/**
 * The file's artifact action, in exactly two states — one component so the
 * embedded header and the fullscreen header can never drift apart:
 *
 *   already in the library -> accent glyph that OPENS the artifact. Never a
 *     second save; one control is the entry point for both.
 *   not there yet          -> muted glyph that promotes the file. Whether that
 *     COPIES the content or LINKS the path is decided by the backend from the
 *     path, so nothing is decided here.
 *
 * Deliberately never a comment icon: commenting is an artifact-only feature.
 * The old star lived here and was inert — the artifact-store sweep only evicts
 * auto-registered chat widgets, so pinning a promoted file did nothing but set
 * a library filter flag.
 */
function FileArtifactActionButton({ state }: { state: ReturnType<typeof useFileArtifactState> }) {
  const navigate = useNavigate()
  if (state.existing) {
    return (
      <button
        className="p-1.5 rounded-md border border-border text-accent hover:border-border-strong cursor-pointer transition-all shrink-0"
        onClick={() => navigate(`/artifacts/${encodeURIComponent(state.existing!.slug)}`)}
        title={i18nT('components.markdownPanel.open_as_artifact')}
        aria-label={i18nT('components.markdownPanel.open_as_artifact')}
      >
        <Component size={14} className="fill-current" />
      </button>
    )
  }
  return (
    <button
      className="p-1.5 rounded-md border border-border text-muted hover:text-accent hover:border-border-strong cursor-pointer transition-all disabled:opacity-50 shrink-0"
      onClick={() => state.add()}
      disabled={state.adding}
      title={i18nT('components.markdownPanel.add_to_artifact_library')}
      aria-label={i18nT('components.markdownPanel.add_to_artifact_library')}
    >
      <Component size={14} />
    </button>
  )
}

/**
 * Row-2 icon: knowledge library toggle. Hidden by the caller
 * when the file's extension isn't supported (or the library is
 * unconfigured). When already added, renders as a static badge.
 */
function KnowledgeToggleIconButton({ state }: { state: ReturnType<typeof useFileKnowledgeState> }) {
  if (state.alreadyAdded) {
    return (
      <span
        className="p-1.5 rounded-md border border-border/40 text-muted inline-flex items-center"
        title={i18nT('components.markdownPanel.in_knowledge_library')}
        aria-label={i18nT('components.markdownPanel.in_knowledge_library')}
      >
        <BookOpen size={14} style={{ color: 'var(--ok)' }} />
      </span>
    )
  }
  return (
    <button
      className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-50"
      onClick={() => state.add()}
      disabled={state.adding}
      title={i18nT('components.markdownPanel.add_to_knowledge_library')}
      aria-label={i18nT('components.markdownPanel.add_to_knowledge_library')}
    >
      <BookOpen size={14} className={state.added ? 'lucide-inline' : ''} style={state.added ? { color: 'var(--ok)' } : undefined} />
    </button>
  )
}

/** Icon + short label toggle for the source-mode options row. */
const barLabelBtn = (on: boolean) =>
  `flex items-center gap-1.5 px-2 h-[26px] rounded-md cursor-pointer transition-colors border-none shrink-0 text-[11.5px] font-medium ${on ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`

export function OverflowMenu({ filePath, content, onRefresh, refreshDisabled, refreshTitle, onFullscreen, fullscreen, onSnapshot, snapshotting }: {
  filePath: string; content: string
  /** View actions folded in from the old header row (side-panel revamp): the
   *  ⋯ menu is the single home for everything that isn't a mode toggle. */
  onRefresh?: () => void; refreshDisabled?: boolean; refreshTitle?: string
  onFullscreen?: () => void; fullscreen?: boolean
  /** Snapshot the file's artifact (saves first when dirty — parent owns that
   *  logic). Entry renders only when the file is already an artifact. */
  onSnapshot?: () => void; snapshotting?: boolean
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  // Keyboard operability (WAI-ARIA menu pattern): roving focus across the
  // items on open, ArrowUp/Down + Home/End, Escape/Tab closes and returns
  // focus to the trigger. Shared hook with StyledSelect/AgentSelector.
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const noInputRef = useRef<HTMLElement>(null)
  const closeToTrigger = useCallback(() => {
    setOpen(false)
    triggerRef.current?.focus()
  }, [])
  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef: listRef,
    inputRef: noInputRef,
    hasFilterInput: false,
    filteredCount: 0,
    onEnterSingleMatch: () => {},
    closeToTrigger,
  })
  const closeTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => { clearTimeout(closeTimerRef.current) }, [])
  const navigate = useNavigate()
  const knowledge = useFileKnowledgeState(filePath)
  const artifact = useFileArtifactState(filePath, content)
  const delayedClose = () => { closeTimerRef.current = setTimeout(() => setOpen(false), 800) }
  useEffect(() => () => { if (closeTimerRef.current) clearTimeout(closeTimerRef.current) }, [])
  // Reset the per-mutation success flags whenever the menu closes so the
  // i18nT('components.markdownPanel.added') / 'Snapshotted!' acknowledgement doesn't bleed into the next
  // open if the user closed quickly. Destructure the callbacks so the dep
  // array stays stable across renders (object refs from hooks change every
  // render, causing the effect to re-fire constantly).
  const knowledgeReset = knowledge.reset
  const artifactResetAdd = artifact.resetAdd
  useEffect(() => {
    if (!open) {
      knowledgeReset()
      artifactResetAdd()
    }
  }, [open, knowledgeReset, artifactResetAdd])
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])
  const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
  const canAddToKnowledge = knowledge.formats && knowledge.formats.includes(ext)
  return (
    <div ref={ref} className="relative">
      <button ref={triggerRef} data-testid="markdown-panel-more-options" aria-label={i18nT('components.markdownPanel.more_options')} aria-haspopup="menu" aria-expanded={open} className={barIconBtn(open)} onClick={() => setOpen(!open)}>
        <Ellipsis size={15} />
      </button>
      {open && (
        <div ref={listRef} role="menu" onKeyDown={onListKeyDown} className="absolute right-0 top-full mt-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-1 min-w-[180px]">
          {onRefresh && (
            <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none disabled:opacity-40" disabled={refreshDisabled} title={refreshTitle} onClick={() => { onRefresh(); setOpen(false) }}>
              <RefreshCw size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.refresh')}
            </button>
          )}
          {onFullscreen && (
            <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => { onFullscreen(); setOpen(false) }}>
              {fullscreen ? <Minimize2 size={14} className="lucide-inline" /> : <Maximize2 size={14} className="lucide-inline" />} {fullscreen ? i18nT('components.markdownPanel.exit_full_screen') : i18nT('components.markdownPanel.full_screen')}
            </button>
          )}
          <div className="h-px bg-border my-1 mx-2" />
          {artifact.existing ? (
            <button
              role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none"
              onClick={() => { navigate(`/artifacts/${encodeURIComponent(artifact.existing!.slug)}`); setOpen(false) }}
              title={i18nT('components.markdownPanel.open_artifact', { name: artifact.existing.slug })}
            >
              <BookmarkPlus size={14} className="lucide-inline" style={{ color: 'var(--ok)' }} /> {i18nT('components.markdownPanel.in_artifacts')} <Check size={14} className="lucide-inline" />
            </button>
          ) : (
            <button
              role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none disabled:opacity-50"
              onClick={() => artifact.add(undefined, { onSuccess: delayedClose })}
              disabled={artifact.adding}
              title={i18nT('components.markdownPanel.save_this_file_as_an_artifact_versioned_persiste')}
            >
              {artifact.added
                ? <><BookmarkPlus size={14} className="lucide-inline" style={{ color: 'var(--ok)' }} /> {i18nT('components.markdownPanel.added')}</>
                : artifact.adding
                  ? i18nT('components.markdownPanel.adding')
                  : <><BookmarkPlus size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.add_to_artifacts')}</>}
            </button>
          )}
          {onSnapshot && artifact.existing && (
            <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none disabled:opacity-50" onClick={() => { onSnapshot(); delayedClose() }} disabled={snapshotting} title={i18nT('components.markdownPanel.capture_the_current_file_content_as_a_new_artifa')}>
              <Camera size={14} className="lucide-inline" /> {snapshotting ? i18nT('components.markdownPanel.snapshotting') : i18nT('components.markdownPanel.snapshot_version')}
            </button>
          )}
          {canAddToKnowledge && (
            knowledge.alreadyAdded ? (
              <span className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-muted">
                <BookOpen size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.in_library')} <Check size={14} className="lucide-inline" />
              </span>
            ) : (
              <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => knowledge.add(undefined, { onSuccess: delayedClose })} disabled={knowledge.adding}>
                {knowledge.added ? <><BookOpen size={14} className="lucide-inline" style={{color: 'var(--ok)'}} /> {knowledge.addResult === 'exists' ? i18nT('components.markdownPanel.already_in_library') : i18nT('components.markdownPanel.added')}</> : knowledge.adding ? i18nT('components.markdownPanel.adding_2') : <><BookOpen size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.add_to_knowledge')}</>}
              </button>
            )
          )}
          <div className="h-px bg-border my-1 mx-2" />
          {/* File-location group: hand the file to the desktop, then the
              clipboard/download fallbacks for hosts that have no desktop.
              Iconless like its neighbours — the group reads as a list of
              destinations, and two glyphs among five would look arbitrary. */}
          <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => { void revealOrOpen(filePath, 'open'); setOpen(false) }}>
            {i18nT('components.markdownPanel.open_with_default_app')}
          </button>
          <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => { void revealOrOpen(filePath, 'reveal'); setOpen(false) }}>
            {i18nT('components.markdownPanel.show_in_file_manager')}
          </button>
          <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => { copyToClipboard(filePath); setOpen(false) }}>
            {i18nT('components.markdownPanel.copy_path')}
          </button>
          <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => { copyToClipboard(content); setOpen(false) }}>
            {i18nT('components.markdownPanel.copy_content')}
          </button>
          <button role="menuitem" data-option tabIndex={-1} className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover focus:bg-bg-hover focus:outline-none" onClick={() => { downloadFile(filePath); setOpen(false) }}>
            {i18nT('components.markdownPanel.download')}
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * File-level knowledge-library state: query for the config + already-added
 * status, mutation to register the file as a source. Always-on so the
 * inline row-2 buttons and the overflow ⋮ entry share a single fetch via
 * React Query's cache.
 */
function useFileKnowledgeState(filePath: string) {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['knowledge-config', filePath],
    queryFn: async () => {
      const r = await fetch('/api/knowledge/config')
      if (!r.ok) return null
      const cfg = await r.json()
      const sr = await fetch(`/api/knowledge/sources?uri=${encodeURIComponent(filePath)}`)
      const sources = sr.ok ? await sr.json() : []
      return { ...cfg, alreadyAdded: sources.length > 0 }
    },
  })
  const formats: string[] | null = data?.enabled ? data.supported_formats : null
  const alreadyAdded = data?.alreadyAdded ?? false
  const { mutate: add, isPending: adding, isSuccess: added, data: addResult, reset } = useMutation({
    mutationFn: async () => {
      const name = filePath.split('/').pop() || filePath
      const res = await fetch('/api/knowledge/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, source_type: 'local_file', uri: filePath }),
      })
      if (res.status === 409) return 'exists' as const
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'failed' }))
        throw new Error(err.error || i18nT('components.markdownPanel.failed_to_add_source'))
      }
      return 'created' as const
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-config', filePath] })
    },
    onError: (err) => alert((err as Error).message),
  })
  return { formats, alreadyAdded, add, adding, added, addResult, reset }
}

/**
 * File-level artifact state: existing artifact for this source_path,
 * adding/snapshotting mutations. `live_dirty` flows through so
 * the inline Snapshot button can gate visibility/enable correctly.
 */
function useFileArtifactState(filePath: string, content: string) {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['artifact-by-source-path', filePath],
    queryFn: async () => {
      const res = await api.artifacts({ source_path: filePath })
      const list = (res?.artifacts ?? []) as { slug: string; name: string }[]
      if (list.length === 0) return null
      try {
        const full = await api.artifact(list[0].slug)
        return { slug: list[0].slug, name: list[0].name, live_dirty: !!full.live_dirty, pinned: !!full.pinned }
      } catch {
        return { slug: list[0].slug, name: list[0].name, live_dirty: false, pinned: false }
      }
    },
  })
  const existing = data ?? null
  const { mutate: add, isPending: adding, isSuccess: added, reset: resetAdd } = useMutation({
    mutationFn: async () => {
      const name = filePath.split('/').pop() || filePath
      const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
      const kind: 'markdown' | 'json' | 'svg' | 'html' | 'text' =
        ext === '.md' || ext === '.markdown' || ext === '.mdx' ? 'markdown'
        : ext === '.json' || ext === '.jsonl' ? 'json'
        : ext === '.svg' ? 'svg'
        : ext === '.html' || ext === '.htm' ? 'html'
        : 'text'
      // Re-read the file rather than promoting the in-memory `content`.
      // /api/file-read truncates very large files and flags it with
      // X-Truncated; the panel's copy carries no such marker, so promoting it
      // would persist a 512 KB prefix AS THOUGH it were the whole document --
      // and a disposable file is COPIED, so nothing would reference the
      // original and the loss would be silent and permanent. Reading here also
      // means the artifact captures the file as it is at promote time.
      const res = await fetch(fileReadUrl(filePath))
      if (!res.ok) throw new Error(i18nT('components.markdownPanel.cannot_read_file'))
      if (res.headers.get('X-Truncated') === 'true') {
        throw new Error(i18nT('components.markdownPanel.file_too_large_to_add'))
      }
      const fresh = await res.text()
      // Same slot is passed as the X-Session-Key so the server's
      // restricted-session gate sees the REAL session. With the transport's
      // shared `dashboard:ui` placeholder an incognito session could persist a
      // promoted file its own restriction was meant to refuse.
      const promoteSlot = store.getState().chat.activeSlot
      const created = await api.createArtifact({
        name,
        content: fresh,
        kind,
        source_path: filePath,
        description: i18nT('components.markdownPanel.tracking_description', { path: filePath }),
        origin_session_key: promoteSlot || undefined,
      }, promoteSlot ? `dashboard:${promoteSlot}` : undefined)
      return created as { slug: string; version: number }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    },
    onError: (err) => alert((err as Error).message),
  })
  const { mutate: snapshot, isPending: snapshotting, isSuccess: snapshotted } = useMutation({
    mutationFn: async () => {
      if (!existing) throw new Error('no existing artifact')
      await api.updateArtifact(existing.slug, { snapshot: true })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifact', existing?.slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-versions', existing?.slug] })
      queryClient.invalidateQueries({ queryKey: ['artifact-events', existing?.slug] })
    },
    onError: (err) => alert((err as Error).message),
  })
  const { mutate: toggleSave, isPending: toggling } = useMutation({
    mutationFn: async () => {
      // Read the owning slot ONCE, up front: both branches below pass it as the
      // X-Session-Key so a restricted slot is gated on the pin as well as the save.
      const saveSlot = store.getState().chat.activeSlot
      // Saved == pinned (consistent with the Artifacts tab/page + chat bookmark).
      if (existing) {
        await api.setArtifactPinned(
          existing.slug,
          !existing.pinned,
          saveSlot ? `dashboard:${saveSlot}` : undefined,
        )
        return
      }
      // Not yet an artifact — create (file-backed), then pin. createArtifact
      // dedups on source_path server-side, so this stays idempotent.
      const name = filePath.split('/').pop() || filePath
      const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
      const kind: 'markdown' | 'json' | 'svg' | 'html' | 'text' =
        ext === '.md' || ext === '.markdown' || ext === '.mdx' ? 'markdown'
        : ext === '.json' || ext === '.jsonl' ? 'json'
        : ext === '.svg' ? 'svg'
        : ext === '.html' || ext === '.htm' ? 'html'
        : 'text'
      const created = await api.createArtifact({
        name, content, kind, source_path: filePath, description: i18nT('components.markdownPanel.tracking_description', { path: filePath }), origin_session_key: saveSlot || undefined,
      }, saveSlot ? `dashboard:${saveSlot}` : undefined) as { slug: string }
      await api.setArtifactPinned(created.slug, true, saveSlot ? `dashboard:${saveSlot}` : undefined)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['artifact-by-source-path', filePath] })
      queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    },
    onError: (err) => alert((err as Error).message),
  })
  const saved = !!existing?.pinned
  return { existing, add, adding, added, resetAdd, snapshot, snapshotting, snapshotted, toggleSave, toggling, saved }
}

let diffThemesRegistered = false

/** Monaco diff editor for side-by-side git diff viewing */
function DiffEditorBlock({ diffMode, lang, originalContent, content, dark, diffActiveRef, handleChange, editing, lineNums, wordWrap, autocomplete, onSelect, flush, sideBySide = true }: {
  diffMode: boolean; lang: string; originalContent: string; content: string; dark: boolean
  diffActiveRef: React.MutableRefObject<boolean>; handleChange: (v: string) => void; editing: boolean; lineNums: boolean; wordWrap: boolean; autocomplete: boolean
  /**
   * Monaco renderSideBySide — false = unified inline diff.
   *
   * Paired with `useInlineViewWhenSpaceIsLimited: false` in the editor options.
   * Monaco silently overrides renderSideBySide when the editor is narrower than
   * renderSideBySideInlineBreakpoint (default 900px), because
   * useInlineViewWhenSpaceIsLimited defaults to true. This panel hosts its diff
   * in the chat side panel and the file explorer's pane, both well under 900px
   * at every usable width, so the split-view toggle appeared to do nothing —
   * the editor always fell back to the inline view. Opting out keeps
   * renderSideBySide authoritative: the toggle is an explicit user choice and
   * Monaco should not second-guess it on width. Side-by-side in a narrow pane
   * is cramped, but it is what the user asked for, and each side scrolls
   * horizontally.
   */
  sideBySide?: boolean
  /** Drop the rounded border box — host surface frames the content. */
  flush?: boolean
  onSelect?: (text: string, rect: DOMRect) => void
}) {
  const handleChangeRef = useRef(handleChange); handleChangeRef.current = handleChange
  const onSelectRef = useRef(onSelect); onSelectRef.current = onSelect
  const disposableRef = useRef<IDisposable | null>(null)
  const selDisposableRef = useRef<IDisposable | null>(null)
  useEffect(() => () => { disposableRef.current?.dispose(); selDisposableRef.current?.dispose() }, [])
  if (!diffMode) return null
  return (
    <div className={`w-full h-full overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}>
      <Suspense fallback={<div className="p-3 text-muted text-[12px] animate-pulse">{i18nT('components.markdownPanel.loading_diff')}</div>}>
        <MonacoDiffEditor height="100%" language={monacoLang(lang)} original={originalContent} modified={content}
          beforeMount={(monaco) => { if (!diffThemesRegistered) { monaco.editor.defineTheme('kirocrew-dark', kirocrewDark); monaco.editor.defineTheme('kirocrew-light', kirocrewLight); diffThemesRegistered = true } }}
          theme={dark ? 'kirocrew-dark' : 'kirocrew-light'} onMount={(editor) => {
            // Jump to the first change once the diff is computed (fires async).
            const nav = editor.onDidUpdateDiff(() => {
              nav.dispose()
              const changes = editor.getLineChanges()
              const first = changes?.[0]
              if (first) editor.getModifiedEditor().revealLineInCenter(first.modifiedStartLineNumber || first.modifiedEndLineNumber || 1)
            })
            const mod = editor.getModifiedEditor()
            disposableRef.current = mod.onDidChangeModelContent(() => { if (!diffActiveRef.current) return; handleChangeRef.current(mod.getValue()) })
            selDisposableRef.current = mod.onMouseUp(() => {
              setTimeout(() => {
                const sel = mod.getSelection()
                if (!sel || sel.isEmpty()) return
                const text = mod.getModel()?.getValueInRange(sel)
                if (!text?.trim()) return
                const pos = mod.getScrolledVisiblePosition(sel.getEndPosition())
                if (!pos) return
                const domNode = mod.getDomNode()
                if (!domNode) return
                const editorRect = domNode.getBoundingClientRect()
                const rect = new DOMRect(editorRect.left + pos.left, editorRect.top + pos.top + pos.height, 0, 0)
                onSelectRef.current?.(text.trim(), rect)
              }, 10)
            })
          }} options={{ minimap: { enabled: false }, readOnly: !editing, renderSideBySide: sideBySide, useInlineViewWhenSpaceIsLimited: false, renderValidationDecorations: 'off', guides: { indentation: false }, stickyScroll: { enabled: false }, renderLineHighlight: 'none', scrollBeyondLastLine: false, fontSize: 13, lineNumbers: lineNums ? 'on' : 'off', wordWrap: wordWrap ? 'on' : 'off', quickSuggestions: autocomplete, automaticLayout: true, hover: { enabled: editing } }} />
      </Suspense>
    </div>
  )
}

/** Shared comment overlay — popover + comment list */
const CommentOverlayBlock = memo(function CommentOverlayBlock({ popover, addComment, setPopover, onSubmitComments, comments, editComment, removeComment, submitAllComments, containerRef, scrollRef }: {
  popover: { x: number; y: number } | null; addComment: (text: string) => void; setPopover: (v: null) => void
  onSubmitComments?: (message: string) => void; comments: InlineComment[]; editComment: (id: string, text: string) => void; removeComment: (id: string) => void; submitAllComments: (extraPrompt?: string) => void; containerRef?: React.RefObject<HTMLElement | null>; scrollRef?: React.RefObject<HTMLElement | null>
}) {
  return (
    <>
      {popover && (
        <CommentPopover x={popover.x} y={popover.y} onSubmit={addComment} containerRef={containerRef} scrollRef={scrollRef}
          onCancel={() => { setPopover(null); window.getSelection()?.removeAllRanges() }} />
      )}
      {onSubmitComments && (
        <CommentList comments={comments} onEdit={editComment} onRemove={removeComment} onSubmitAll={submitAllComments} enableExtraPrompt />
      )}
    </>
  )
})

/** Imperative handle: lets a host trigger the SAME dirty-state close guard the
 *  panel uses internally (Escape / close button), so an external "back"/close
 *  control can't bypass the "Discard unsaved changes?" confirmation. */
export interface MarkdownPanelHandle { requestClose: () => void }

export default memo(forwardRef<MarkdownPanelHandle, Props>(function MarkdownPanel({ filePath, content, onContentChange, onSave, onClose, liveWatch, onSubmitComments, onRefresh, reserveWidth, initialDiffMode, onDiffModeChange, embedded, savedBaseline, revealLine, onRevealConsumed, onOpenFolder }: Props, ref) {
  const qc = useQueryClient()
  // Code files (non-rich, non-markdown) have no meaningful preview — their
  // "preview" was just a read-only render of the same text. They open
  // straight in source mode and the View Preview toggle is hidden for them.
  //
  // A requested line forces source mode: a line number only means something
  // against the source, and the rendered markdown preview has no per-line element
  // to scroll to (its `data-sourcepos` is per BLOCK, and soft wrapping breaks any
  // line-count correspondence anyway). So `README.md:42` opens the raw markdown at
  // line 42 rather than a paragraph that may contain it.
  //
  // Rich types are excluded, and that is a deliberate scope line rather than an
  // oversight. They have exactly ONE renderer by design — `isRichType` gates the
  // source/preview toggle, the Save/Cancel row, the line-number and diff controls,
  // and the Cmd+S handler — so forcing one into an editor creates a file that is in
  // source mode with none of the chrome that makes source mode usable, including no
  // way back to its own viewer and no visible Save for a buffer the user has
  // edited. Making that coherent means teaching every one of those gates about a
  // rich-file-in-source-mode state, i.e. making rich files editable as text, which
  // is a larger feature than a citation jump. So `data.json:42` opens the JSON
  // viewer and drops the line: strictly better than the inert chip it used to be,
  // and it strands nothing.
  const revealTargetsSource = !RICH_FILE_TYPES.includes(detectFileType(filePath))
  const [editing, setEditing] = useState(() => {
    if (revealLine && revealTargetsSource) return true
    if (MD_EXTS.has(extOf(filePath))) return false
    return !RICH_FILE_TYPES.includes(detectFileType(filePath))
  })
  const [diffMode, setDiffMode] = useState(initialDiffMode ?? false)
  const toggleDiffMode = useCallback(() => {
    const next = !diffMode
    setDiffMode(next)
    onDiffModeChange?.(next)
  }, [diffMode, onDiffModeChange])
  // Unified vs side-by-side diff rendering — persisted, and shares its key
  // with SidePanel's diff tabs so the preference is app-wide.
  const [diffSplit, setDiffSplit] = usePersistedBool('mc-diff-split', true)
  const [monacoSelection, setMonacoSelection] = useState<{ text: string; x: number; y: number } | null>(null)
  const diffActiveRef = useRef(false)
  diffActiveRef.current = diffMode && editing
  const diffInitFileRef = useRef<string | null>(null)
  const dark = useIsDark()
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(() => savedBaseline != null && content !== savedBaseline)
  // When a saved baseline is provided (inline preview), keep `dirty` derived
  // from content-vs-disk so a RESTORED draft is dirty and the close guard fires.
  // No-op for document tabs (savedBaseline undefined) — they keep the manual
  // edit-driven dirty model below.
  useEffect(() => {
    if (savedBaseline != null) setDirty(content !== savedBaseline)
  }, [content, savedBaseline])
  const [saveError, setSaveError] = useState<string | null>(null)
  // Editor view preferences — persisted so they survive tab switches/reloads.
  const [lineNums, setLineNums] = usePersistedBool('mc-file-linenums', true)
  const [wordWrap, setWordWrap] = usePersistedBool('mc-file-wordwrap', true)
  const [autocomplete, setAutocomplete] = usePersistedBool('mc-file-autocomplete', true)
  // The side panel renders markdown at a fixed default width (centered, capped
  // at --mc-content-width), matching the artifact detail page. No reading-width
  // (M/F) toggle.
  const mdPreviewStyle: React.CSSProperties = { maxWidth: 'var(--mc-content-width, 900px)', margin: '0 auto' }
  // Hydrate pending draft comments for this file from localStorage so they
  // survive panel close, refresh, and crash. Submitting clears them.
  const draftsRef = useRef<ReturnType<typeof loadCommentDrafts>>(null!)
  if (draftsRef.current === null) draftsRef.current = loadCommentDrafts()
  const [comments, setComments] = useState<InlineComment[]>(() => draftsRef.current[filePath] ?? [])
  // Sync state to the new filePath during render (not in a useEffect) so
  // `comments` and `filePath` never disagree within a single render — otherwise
  // a callback firing in the transition window would persist against the wrong
  // file. setState-during-render is a supported React pattern (triggers a
  // re-render before commit).
  const prevFilePathRef = useRef(filePath)
  if (prevFilePathRef.current !== filePath) {
    prevFilePathRef.current = filePath
    setComments(draftsRef.current[filePath] ?? [])
  }
  const [popover, setPopover] = useState<{ x: number; y: number; anchor: string; line?: number; column?: number; startOffset?: number } | null>(null)
  const highlightMarksRef = useRef<HTMLElement[]>([])

  const clearHighlightMarks = useCallback(() => {
    for (const mark of highlightMarksRef.current) {
      const parent = mark.parentNode
      if (!parent) continue
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark)
      parent.removeChild(mark)
      parent.normalize()
    }
    highlightMarksRef.current = []
  }, [])

  const applyHighlightMarks = useCallback((range: Range) => {
    clearHighlightMarks()
    const marks: HTMLElement[] = []
    const treeWalker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT)
    const textNodes: Text[] = []
    let node: Node | null
    while ((node = treeWalker.nextNode())) {
      if (range.intersectsNode(node)) textNodes.push(node as Text)
    }
    if (textNodes.length === 0 && range.startContainer.nodeType === Node.TEXT_NODE) {
      textNodes.push(range.startContainer as Text)
    }
    for (const textNode of textNodes) {
      const start = textNode === range.startContainer ? range.startOffset : 0
      const end = textNode === range.endContainer ? range.endOffset : textNode.length
      if (start === end) continue
      const highlightRange = document.createRange()
      highlightRange.setStart(textNode, start)
      highlightRange.setEnd(textNode, end)
      const mark = document.createElement('mark')
      mark.style.backgroundColor = 'var(--accent-subtle, rgba(99, 102, 241, 0.15))'
      mark.style.borderRadius = '2px'
      highlightRange.surroundContents(mark)
      marks.push(mark)
    }
    highlightMarksRef.current = marks
  }, [clearHighlightMarks])
  const [refreshing, setRefreshing] = useState(false)
  const [hintDismissed, setHintDismissed] = useState(() => localStorage.getItem(HINT_KEY) === '1')
  const [fullscreen, setFullscreen] = useState(false)
  const fileName = filePath.split('/').pop() || filePath
  // Artifact + knowledge state power the header star/knowledge toggles and
  // the ⋯ menu's Snapshot entry (same query cache as the OverflowMenu's own
  // hooks, so states stay coherent).
  const knowledge = useFileKnowledgeState(filePath)
  const artifactState = useFileArtifactState(filePath, content)
  const previewRef = useRef<HTMLDivElement>(null)
  const sidePanelScrollRef = useRef<HTMLDivElement>(null)
  const fullscreenPreviewRef = useRef<HTMLDivElement>(null)
  const gutterReadRef = useRef<HTMLDivElement>(null)
  const gutterFullscreenRef = useRef<HTMLDivElement>(null)
  const fullscreenBodyRef = useRef<HTMLDivElement>(null)
  const ext = extOf(filePath)
  const fileType = detectFileType(filePath)
  const isMarkdown = MD_EXTS.has(ext)
  const isRichType = RICH_FILE_TYPES.includes(fileType)
  useEffect(() => { if (isRichType) setDiffMode(false) }, [isRichType])
  // ── Preview-mode find (Cmd+F) ─────────────────────────────────────────────
  // Three surfaces compete for Cmd+F: Monaco owns it while editing (it stops
  // propagation before anything else sees the key), and ChatPage's chat-find
  // owns it via a document-level *bubble* listener. The rendered markdown
  // PREVIEW is the only surface with no editor to capture the key, so today it
  // falls through to chat-find — the reported bug. This adds a find scoped to
  // the preview that wins over chat-find using a *capture-phase* listener +
  // stopImmediatePropagation, but only when this panel is the active region
  // and we're in markdown preview (edit/Monaco and non-markdown are untouched).
  // Highlights paint via the CSS Custom Highlight API (Range objects outside
  // the DOM) so the react-markdown subtree is never mutated.
  const [findOpen, setFindOpen] = useState(false)
  const [findTerm, setFindTerm] = useState('')
  const [findCase, setFindCase] = useState(false)
  const [findIdx, setFindIdx] = useState(0)
  const [findCount, setFindCount] = useState(0)
  const findInputRef = useRef<HTMLInputElement>(null)
  const findRangesRef = useRef<Range[]>([])
  // Is this panel the region the cursor is in? Defaults true so Cmd+F right
  // after opening the doc searches the doc (the reported expectation). Flips
  // based on where the last pointer-down landed.
  const findActiveRef = useRef(true)

  useEffect(() => {
    const onPointer = (e: Event) => {
      const t = e.target as Element | null
      findActiveRef.current = !!t?.closest?.('[data-mc-mdpanel]')
    }
    document.addEventListener('pointerdown', onPointer, true)
    return () => document.removeEventListener('pointerdown', onPointer, true)
  }, [])

  // Highlights are external (CSS.highlights), so clearing never touches the
  // React-owned DOM — no reconciliation hazard.
  const clearFindMarks = useCallback(() => {
    findRangesRef.current = []
    if (cssHighlights) { cssHighlights.delete(FIND_HL_ALL); cssHighlights.delete(FIND_HL_CURRENT) }
  }, [])

  // Re-register the two highlights so `idx` paints as the current match and the
  // rest paint as plain matches. Cheap; called on every step.
  const paintFind = useCallback((ranges: Range[], idx: number) => {
    if (!FIND_HL_SUPPORTED || !FindHighlightCtor || !cssHighlights) return
    const others = ranges.filter((_, i) => i !== idx)
    cssHighlights.set(FIND_HL_ALL, new FindHighlightCtor(...others))
    const cur = ranges[idx]
    cssHighlights.set(FIND_HL_CURRENT, new FindHighlightCtor(...(cur ? [cur] : [])))
  }, [])

  const paintFindCurrent = useCallback((idx: number) => {
    const ranges = findRangesRef.current
    paintFind(ranges, idx)
    // Range has no scrollIntoView; scroll the match's nearest element instead.
    ranges[idx]?.startContainer.parentElement?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [paintFind])

  const runFind = useCallback((term: string, caseSensitive: boolean) => {
    clearFindMarks()
    const root = fullscreen ? fullscreenPreviewRef.current : previewRef.current
    if (!root || !term) { setFindCount(0); setFindIdx(0); return }
    const re = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), caseSensitive ? 'g' : 'gi')
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.nodeValue && n.nodeValue.trim()) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
    })
    const nodes: Text[] = []
    let node: Node | null
    while ((node = walker.nextNode())) nodes.push(node as Text)
    const ranges: Range[] = []
    for (const tn of nodes) {
      const text = tn.nodeValue ?? ''
      let m: RegExpExecArray | null
      re.lastIndex = 0
      while ((m = re.exec(text))) {
        const r = document.createRange()
        r.setStart(tn, m.index)
        r.setEnd(tn, m.index + m[0].length)
        ranges.push(r)
        if (m.index === re.lastIndex) re.lastIndex++
      }
    }
    findRangesRef.current = ranges
    setFindCount(ranges.length)
    setFindIdx(0)
    if (ranges.length) { paintFind(ranges, 0); ranges[0].startContainer.parentElement?.scrollIntoView({ block: 'center', behavior: 'smooth' }) }
  }, [clearFindMarks, paintFind, fullscreen])

  const stepFind = useCallback((dir: number) => {
    const n = findRangesRef.current.length
    if (!n) return
    setFindIdx((prev) => { const next = (prev + dir + n) % n; paintFindCurrent(next); return next })
  }, [paintFindCurrent])

  const closeFind = useCallback(() => {
    clearFindMarks()
    setFindOpen(false)
    setFindTerm('')
    setFindCount(0)
    setFindIdx(0)
  }, [clearFindMarks])

  // Recompute matches as the term/case/content/view changes while open. With
  // the CSS Highlight API there's no DOM to clean up if `content` re-renders
  // mid-find — stale ranges simply stop painting and are rebuilt here.
  useEffect(() => {
    if (!findOpen) return
    runFind(findTerm, findCase)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runFind is stable per `fullscreen`; listing it would re-run on every match repaint
  }, [findOpen, findTerm, findCase, content, fullscreen])

  // Highlight names are global; clear them if the panel unmounts while find is
  // open so a stale highlight can't leak onto the next preview.
  useEffect(() => () => {
    if (cssHighlights) { cssHighlights.delete(FIND_HL_ALL); cssHighlights.delete(FIND_HL_CURRENT) }
  }, [])

  // Leaving preview (edit/diff) has no rendered DOM to search — close find so
  // Monaco's own find takes over cleanly.
  useEffect(() => { if (editing || diffMode) closeFind() }, [editing, diffMode, closeFind])

  // Capture-phase Cmd+F: fires before ChatPage's bubble-phase chat-find. We
  // only steal the key in markdown preview when this panel is the active
  // region; otherwise we let it bubble (chat-find) or let Monaco handle it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== 'f') return
      if (editing || diffMode || !isMarkdown) return       // Monaco/edit owns it; non-markdown skip
      if (!findActiveRef.current) return                    // cursor is in chat → let chat-find handle
      e.preventDefault()
      e.stopImmediatePropagation()                          // beat ChatPage's bubble-phase chat-find
      setFindOpen(true)
      requestAnimationFrame(() => { findInputRef.current?.focus(); findInputRef.current?.select() })
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [editing, diffMode, isMarkdown])

  const findBar = findOpen ? (
    <div data-mc-mdpanel className="absolute top-2 right-3 z-30 flex items-center gap-1.5 bg-bg-elevated border border-border rounded-lg shadow-md px-2.5 py-1.5 text-[13px]">
      <input
        ref={findInputRef}
        type="text"
        value={findTerm}
        onChange={(e) => setFindTerm(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1) }
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeFind() }
        }}
        placeholder={i18nT('components.markdownPanel.find_in_document')}
        className="bg-transparent border-none outline-none text-text placeholder:text-muted w-[170px] text-[13px]"
        aria-label={i18nT('components.markdownPanel.find_in_document_2')}
      />
      <button onClick={() => setFindCase((c) => !c)} className={`p-0.5 rounded cursor-pointer border-none transition-colors ${findCase ? 'bg-accent/20 text-accent' : 'bg-transparent text-muted hover:text-text'}`} title={i18nT('components.markdownPanel.case_sensitive')} aria-label={i18nT('components.markdownPanel.case_sensitive')}><CaseSensitive size={15} /></button>
      {findTerm && <span className="text-muted text-[12px] whitespace-nowrap tabular-nums">{findCount > 0 ? `${findIdx + 1} of ${findCount}` : i18nT('components.markdownPanel.no_results')}</span>}
      <button onClick={() => stepFind(-1)} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.markdownPanel.previous_shift_enter')} aria-label={i18nT('components.markdownPanel.previous_match')}><ChevronUp size={15} /></button>
      <button onClick={() => stepFind(1)} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.markdownPanel.next_enter')} aria-label={i18nT('components.markdownPanel.next_match')}><ChevronDown size={15} /></button>
      <button onClick={closeFind} className="p-0.5 rounded text-muted hover:text-text cursor-pointer border-none bg-transparent" title={i18nT('components.markdownPanel.close_esc')} aria-label={i18nT('components.markdownPanel.close_find')}><X size={15} /></button>
    </div>
  ) : null

  const lang = langFor(ext)
  const displayContent = isMarkdown ? content : wrapCode(content, ext)

  const highlightedHtml = useMemo(() => {
    if (isMarkdown || editing || isRichType) return ''
    try { return DOMPurify.sanitize(hljs.highlight(content, { language: lang }).value) + '\n' }
    catch { return DOMPurify.sanitize(hljs.highlightAuto(content).value) + '\n' }
  }, [content, lang, isMarkdown, editing, isRichType])

  useFileWatch(
    liveWatch && !editing && !dirty ? filePath : null,
    useCallback((c: string) => { onContentChange(c) }, [onContentChange]),
  )


  // Detect if file has uncommitted changes and pre-fetch HEAD content
  const { data: diffData, isFetching: diffChecking } = useQuery({
    queryKey: ['file-diff', filePath],
    queryFn: () => api.fileDiff(filePath),
    enabled: !!filePath && !isRichType,
    staleTime: 10_000,
  })
  const originalContent = diffData?.original ?? ''
  // Auto-open diff mode once for a genuine edit unless this file tab already
  // carries an explicit choice. File-tab metadata survives ChatPage unmounts,
  // so returning to a session restores preview/source instead of re-enabling
  // diff after the user turned it off.
  useEffect(() => {
    if (!diffData || diffInitFileRef.current === filePath) return
    diffInitFileRef.current = filePath
    if (initialDiffMode !== undefined) {
      setDiffMode(initialDiffMode)
      return
    }
    if (diffData.diff && diffData.status === 'modified') {
      setDiffMode(true)
      onDiffModeChange?.(true)
    }
  }, [diffData, filePath, initialDiffMode, onDiffModeChange])

  const handleRefresh = useCallback(async () => {
    if (refreshing || dirty) return
    setRefreshing(true)
    try {
      if (onRefresh) { await onRefresh(filePath) }
      else {
        const res = await fetch(fileReadUrl(filePath))
        if (res.ok) onContentChange(await res.text())
      }
    } finally { setRefreshing(false) }
  }, [filePath, onContentChange, onRefresh, refreshing, dirty])

  // Discard pending edits (matches the artifact detail page's Cancel button).
  // Re-reads the file from disk into the buffer, clearing dirty. Confirms first
  // because edits are gone for good. Only markdown-ish files have a preview to
  // return to; code files stay in source mode (Cancel just discards edits).
  const canPreview = isMarkdown
  const handleCancel = useCallback(async () => {
    if (!dirty) { if (canPreview) setEditing(false); return }
    if (!window.confirm(i18nT('components.markdownPanel.discard_unsaved_changes'))) return
    setRefreshing(true)
    try {
      if (onRefresh) { await onRefresh(filePath) }
      else {
        const res = await fetch(fileReadUrl(filePath))
        if (res.ok) onContentChange(await res.text())
      }
      setDirty(false)
      if (canPreview) setEditing(false)
    } finally { setRefreshing(false) }
  }, [dirty, filePath, onContentChange, onRefresh, canPreview])

  const resolveSelectionCoords = useCallback((fallbackText?: string) => {
    const sel = window.getSelection()
    const root = previewRef.current ?? fullscreenPreviewRef.current
    if (!root) return undefined
    // Try live selection first
    if (sel && !sel.isCollapsed && sel.anchorNode && root.contains(sel.anchorNode)) {
      const raw = sel.toString()
      if (raw.trim()) {
        const range = sel.getRangeAt(0)
        if (root.contains(range.startContainer) && root.contains(range.endContainer)) {
          const anchor = raw.trim()
          const rect = range.getBoundingClientRect()
          const coords = isMarkdown
            ? (resolveSourcePos(range, root, displayContent) ?? findCoords(displayContent, raw) ?? findCoords(displayContent, anchor))
            : (findCoords(content, raw) ?? findCoords(content, anchor))
          // Compute the rendered-text character offset so repeated occurrences
          // of the same anchor text can be disambiguated at highlight time.
          let startOffset: number | undefined
          try {
            const preRange = document.createRange()
            preRange.setStart(root, 0)
            preRange.setEnd(range.startContainer, range.startOffset)
            startOffset = preRange.toString().length + (raw.length - raw.trimStart().length)
          } catch { /* leave undefined */ }
          return { anchor, rect, range: range.cloneRange(), line: coords?.line, column: coords?.column, startOffset }
        }
      }
    }
    // Fallback: selection was cleared by button click — use text + findCoords
    if (fallbackText) {
      const coords = isMarkdown ? findCoords(displayContent, fallbackText) : findCoords(content, fallbackText)
      return { anchor: fallbackText, rect: new DOMRect(0, 0, 0, 0), range: undefined, line: coords?.line, column: coords?.column }
    }
    return undefined
  }, [content, displayContent, isMarkdown])

  const handleCommentAction = useCallback((text: string, rect: DOMRect) => {
    const info = resolveSelectionCoords(text)
    if (info) {
      if (info.range) applyHighlightMarks(info.range)
      const popRect = info.rect.width > 0 ? info.rect : rect
      setPopover({ x: popRect.left, y: popRect.bottom, anchor: info.anchor, line: info.line, column: info.column, startOffset: info.startOffset })
    } else {
      // Monaco path — no DOM selection available, use rect directly
      setPopover({ x: rect.left, y: rect.top, anchor: text, line: undefined, column: undefined })
    }
    window.getSelection()?.removeAllRanges()
  }, [resolveSelectionCoords, applyHighlightMarks])

  const handleCopyAction = useCallback((text: string) => {
    if (text) copyToClipboard(text)
  }, [])

  const selectionActions: SelectionAction[] = useMemo(() => {
    if (!onSubmitComments) return [{ id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: handleCopyAction }]
    return [
      { id: 'comment', icon: <MessageSquarePlus size={12} />, label: 'Comment', onClick: handleCommentAction },
      { id: 'copy', icon: <Copy size={12} />, label: 'Copy', onClick: handleCopyAction },
    ]
  }, [onSubmitComments, handleCommentAction, handleCopyAction])

  const addComment = useCallback((text: string) => {
    if (!popover) return
    const newComment: InlineComment = { id: Math.random().toString(36).substring(2), anchor: popover.anchor, text, line: popover.line, column: popover.column, startOffset: popover.startOffset }
    setComments(prev => [...prev, newComment])
    setPopover(null)
    clearHighlightMarks()
  }, [popover, clearHighlightMarks])

  const removeComment = useCallback((id: string) => {
    setComments(prev => prev.filter(c => c.id !== id))
  }, [])

  const editComment = useCallback((id: string, text: string) => {
    setComments(prev => prev.map(c => c.id === id ? { ...c, text } : c))
  }, [])

  const submitAllComments = useCallback((extraPrompt?: string) => {
    if (!onSubmitComments || comments.length === 0) return
    onSubmitComments(formatCommentsMessage(filePath, comments, displayContent, extraPrompt))
    setComments([])
  }, [onSubmitComments, comments, filePath, displayContent])

  const dismissHint = useCallback(() => {
    setHintDismissed(true)
    safeSetItem(HINT_KEY, '1')
  }, [])

  useEffect(() => {
    if (editing) { setPopover(null); clearHighlightMarks(); window.getSelection()?.removeAllRanges() }
  }, [editing, clearHighlightMarks])

  // Centralize persistence: fires on any comments mutation (add / remove /
  // submit-clear) and after the filePath sync-reset above. Keeping it in one
  // place avoids duplicate writes from StrictMode double-invoked updaters and
  // eliminates persistComments from callback dep arrays.
  useEffect(() => {
    setCommentsForFile(draftsRef.current, filePath, comments)
    saveCommentDrafts(draftsRef.current)
  }, [comments, filePath])

  // ── Inline comment anchor highlights (CSS Custom Highlight API) ─────────
  // The markdown preview is react-markdown-reconciled, so we must NOT inject
  // <mark> nodes into it (that corrupts React's DOM on the next re-render and
  // produces phantom nodes, e.g. an extra empty list bullet). Instead we paint
  // highlights via the CSS Custom Highlight API — the ranges live entirely
  // outside the DOM — and detect hover / click by hit-testing the pointer
  // against those ranges with caretRangeFromPoint.
  const commentTooltipRef = useRef<HTMLDivElement | null>(null)
  const removeCommentTooltip = useCallback(() => {
    commentTooltipRef.current?.remove()
    commentTooltipRef.current = null
  }, [])
  // Tracks the currently-flashing sidebar comment row so a new click cancels
  // the previous flash immediately (instead of leaving two rows highlighted
  // until the first one's timeout fires).
  const commentFlashRef = useRef<{ row: HTMLElement; timers: number[] } | null>(null)
  const flashCommentRow = useCallback((row: HTMLElement) => {
    const prev = commentFlashRef.current
    if (prev) {
      prev.timers.forEach(clearTimeout)
      prev.row.style.background = ''
      prev.row.style.transition = ''
    }
    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    row.style.transition = 'background 0.3s ease'
    row.style.background = 'var(--accent-subtle, rgba(99, 102, 241, 0.25))'
    const t1 = window.setTimeout(() => { row.style.background = '' }, 2800)
    const t2 = window.setTimeout(() => { row.style.transition = ''; commentFlashRef.current = null }, 3100)
    commentFlashRef.current = { row, timers: [t1, t2] }
  }, [])

  /* ── Reveal a cited line (`…/_dispatch.py:447` chip) ────────────────────
   * The mechanics live in useLineReveal; this only decides which VIEW the reveal
   * needs and reports the target as consumed. */
  const { onEditorMount: handleEditorMount } = useLineReveal(revealLine, onRevealConsumed)

  // A line only resolves against source, so leave preview/diff for it. Keyed on
  // the whole target (nonce included), so a second chip click also pulls the
  // panel back out of a view the user switched to in between.
  useEffect(() => {
    if (!revealLine || !revealTargetsSource) return
    setEditing(true)
    setDiffMode(false)
  }, [revealLine, revealTargetsSource])

  useLayoutEffect(() => {
    const HL = 'mc-comment'
    const clear = () => { try { cssHighlights?.delete(HL) } catch { /* */ } }
    if (!FIND_HL_SUPPORTED || comments.length === 0 || editing) { clear(); return }
    // Bind the observer + pointer listeners to the STABLE scroll container.
    // The markdown text div (previewRef) is briefly null when react-markdown
    // lazy-mounts on a file switch — binding to it and bailing on null meant a
    // switched-to document never re-highlighted (regression). The scroll
    // container outlives the lazy content; apply() re-queries the text root.
    const scrollRoot = fullscreen ? fullscreenBodyRef.current : sidePanelScrollRef.current
    if (!scrollRoot) { clear(); return }

    // Inject the highlight paint rule once (background + accent underline).
    if (!document.getElementById('mc-comment-hl-style')) {
      const style = document.createElement('style')
      style.id = 'mc-comment-hl-style'
      style.textContent = `::highlight(${HL}){background-color:var(--accent-subtle,rgba(99,102,241,0.18));text-decoration-line:underline;text-decoration-color:var(--accent,#6366f1);text-decoration-thickness:2px;text-decoration-skip-ink:none;text-underline-offset:2px;}`
      document.head.appendChild(style)
    }

    // Recompute ranges + repaint. Called once now, then again whenever the
    // preview DOM settles — code-block syntax highlighting swaps text nodes
    // AFTER first paint, which strips a just-created highlight range (symptom:
    // the first comment in a code block doesn't underline until a second
    // comment forces a re-run). A MutationObserver + rAF re-apply fixes it.
    // `activeHits` is read by the pointer handlers below.
    let activeHits: { range: Range; comment: InlineComment }[] = []
    const apply = () => {
      const textRoot = previewRef.current ?? fullscreenPreviewRef.current
      if (!textRoot) { activeHits = []; clear(); return }
      const walker = document.createTreeWalker(textRoot, NodeFilter.SHOW_TEXT)
      const textNodes: { node: Text; start: number }[] = []
      let fullText = ''
      let node: Node | null
      while ((node = walker.nextNode())) {
        textNodes.push({ node: node as Text, start: fullText.length })
        fullText += (node as Text).nodeValue ?? ''
      }
      const locate = (off: number): { node: Text; offset: number } | null => {
        for (const tn of textNodes) {
          const len = tn.node.nodeValue?.length ?? 0
          if (off <= tn.start + len) return { node: tn.node, offset: off - tn.start }
        }
        return null
      }
      const ranges: Range[] = []
      const hits: { range: Range; comment: InlineComment }[] = []
      for (const comment of comments) {
        if (!comment.anchor) continue
        const bestIdx = findBestOccurrence(fullText, comment.anchor, comment.startOffset)
        if (bestIdx < 0) continue
        const s = locate(bestIdx)
        const e = locate(bestIdx + comment.anchor.length)
        if (!s || !e) continue
        try {
          const r = document.createRange()
          r.setStart(s.node, s.offset)
          r.setEnd(e.node, e.offset)
          ranges.push(r)
          hits.push({ range: r, comment })
        } catch { /* skip */ }
      }
      activeHits = hits
      if (ranges.length === 0) { clear(); return }
      try { cssHighlights!.set(HL, new FindHighlightCtor!(...ranges)) } catch { clear() }
    }
    apply()

    // Re-apply after async DOM mutations (syntax highlighting, lazy content).
    let raf1 = 0, raf2 = 0, timer = 0
    raf1 = requestAnimationFrame(() => { raf2 = requestAnimationFrame(apply) })
    const observer = new MutationObserver(() => {
      clearTimeout(timer)
      timer = window.setTimeout(apply, 60)
    })
    observer.observe(scrollRoot, { childList: true, subtree: true, characterData: true })

    // Hit-test the pointer against the comment ranges (no DOM elements exist).
    const hitAt = (x: number, y: number): { range: Range; comment: InlineComment } | null => {
      const caret = (document as unknown as { caretRangeFromPoint?: (x: number, y: number) => Range | null }).caretRangeFromPoint?.(x, y)
      if (!caret) return null
      for (const h of activeHits) {
        try { if (h.range.comparePoint(caret.startContainer, caret.startOffset) === 0) return h } catch { /* */ }
      }
      return null
    }

    const onMove = (ev: MouseEvent) => {
      const hit = hitAt(ev.clientX, ev.clientY)
      if (!hit) { scrollRoot.style.cursor = ''; removeCommentTooltip(); return }
      scrollRoot.style.cursor = 'pointer'
      let tip = commentTooltipRef.current
      if (!tip) {
        tip = document.createElement('div')
        tip.className = 'mc-comment-tooltip'
        tip.style.cssText = 'position:fixed;z-index:9999;background:var(--bg-elevated,#1e1e2e);color:var(--text,#e0e0e0);border:1px solid var(--border,#333);border-radius:6px;padding:6px 10px;font-size:12px;line-height:1.4;max-width:300px;word-wrap:break-word;box-shadow:0 4px 12px rgba(0,0,0,0.25);pointer-events:none;'
        document.body.appendChild(tip)
        commentTooltipRef.current = tip
      }
      tip.textContent = hit.comment.text
      // Follow the pointer.
      const w = tip.offsetWidth
      let left = ev.clientX + 12
      if (left + w > window.innerWidth - 6) left = ev.clientX - w - 12
      let top = ev.clientY - tip.offsetHeight - 12
      if (top < 6) top = ev.clientY + 16
      tip.style.left = left + 'px'
      tip.style.top = top + 'px'
    }
    const onLeave = () => { scrollRoot.style.cursor = ''; removeCommentTooltip() }
    const onClick = (ev: MouseEvent) => {
      const hit = hitAt(ev.clientX, ev.clientY)
      if (!hit) return
      ev.preventDefault(); ev.stopPropagation()
      window.getSelection()?.removeAllRanges()
      removeCommentTooltip()
      const row = document.querySelector(`[data-comment-id="${hit.comment.id}"]`) as HTMLElement | null
      if (row) flashCommentRow(row)
    }

    scrollRoot.addEventListener('mousemove', onMove)
    scrollRoot.addEventListener('mouseleave', onLeave)
    scrollRoot.addEventListener('click', onClick)
    return () => {
      clear()
      cancelAnimationFrame(raf1)
      cancelAnimationFrame(raf2)
      clearTimeout(timer)
      observer.disconnect()
      scrollRoot.style.cursor = ''
      scrollRoot.removeEventListener('mousemove', onMove)
      scrollRoot.removeEventListener('mouseleave', onLeave)
      scrollRoot.removeEventListener('click', onClick)
      removeCommentTooltip()
    }
  }, [comments, displayContent, editing, fullscreen, removeCommentTooltip, flashCommentRow])

  const handleSave = useCallback(async () => {
    setSaving(true); setSaveError(null)
    try { await onSave(filePath, content); setDirty(false); qc.invalidateQueries({ queryKey: ['file-diff', filePath] }) }
    catch (err) { setSaveError(err instanceof Error ? err.message : i18nT('components.markdownPanel.save_failed')) }
    finally { setSaving(false) }
  }, [filePath, content, onSave, qc])

  const handleSaveRef = useRef(handleSave)
  useEffect(() => { handleSaveRef.current = handleSave }, [handleSave])

  const guardedClose = useCallback(() => {
    if (dirty && !window.confirm(i18nT('components.markdownPanel.discard_unsaved_changes'))) return
    onClose()
  }, [dirty, onClose])

  // Expose the guarded close so an external control (e.g. the Files-tab inline
  // preview's "Back to files" bar) routes through the same dirty confirmation
  // instead of unmounting the editor and silently dropping unsaved edits.
  useImperativeHandle(ref, () => ({ requestClose: guardedClose }), [guardedClose])

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { if (popover) { setPopover(null); clearHighlightMarks() } else if (fullscreen) setFullscreen(false); else guardedClose() }
      if ((e.metaKey || e.ctrlKey) && e.key === 's' && editing && dirty) { e.preventDefault(); handleSaveRef.current() }
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [guardedClose, editing, dirty, fullscreen, popover, clearHighlightMarks])

  const handleChange = useCallback((v: string) => { onContentChange(v); setDirty(true) }, [onContentChange])
  const clearPopover = useCallback(() => { setPopover(null); clearHighlightMarks() }, [clearHighlightMarks])

  // Lock body scroll when fullscreen overlay is open
  useEffect(() => {
    if (!fullscreen) return
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [fullscreen])

  const editorToolbarButtons = (<>
    {!isRichType && (
      <button className={`p-1.5 rounded-md border cursor-pointer ${diffMode ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={toggleDiffMode} title={i18nT('components.markdownPanel.toggle_diff_view')} aria-label={i18nT('components.markdownPanel.toggle_diff_view')}><FileDiff size={14} /></button>
    )}
    {!isRichType && editing && (
      <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${wordWrap ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setWordWrap(!wordWrap)} title={i18nT('components.markdownPanel.toggle_word_wrap')} aria-label={i18nT('components.markdownPanel.toggle_word_wrap')}><WrapText size={14} /></button>
    )}
    {!isRichType && editing && (
      <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${autocomplete ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setAutocomplete(!autocomplete)} title={i18nT('components.markdownPanel.toggle_autocomplete')} aria-label={i18nT('components.markdownPanel.toggle_autocomplete')}><Zap size={14} /></button>
    )}
    {!isRichType && editing && (
      <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${lineNums ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setLineNums(!lineNums)} title={i18nT('components.markdownPanel.toggle_line_numbers')} aria-label={i18nT('components.markdownPanel.toggle_line_numbers')}><Hash size={14} /></button>
    )}
    {!isRichType && (
      <button className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all ${editing ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => { setEditing(!editing) }}>{editing ? i18nT('components.markdownPanel.preview') : i18nT('components.markdownPanel.edit')}</button>
    )}
    {!isRichType && editing && (
      <button className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`} disabled={saving || !dirty} onClick={handleSave}>{saving ? i18nT('components.markdownPanel.saving') : i18nT('components.markdownPanel.save')}</button>
    )}
  </>)

  // Breadcrumb: last two directories + filename (full path in tooltip/copy).
  const crumbs = breadcrumbSegments(filePath)
  // Diff-mode +N/-N stats over the same original/modified pair Monaco shows.
  const diffStats = useMemo(() => countLines(originalContent, content), [originalContent, content])
  // Snapshot (⋯ menu): capture current content as a new artifact version;
  // unsaved edits are persisted first so the snapshot reflects the screen.
  const handleSnapshot = useCallback(async () => {
    if (dirty) await handleSave()
    artifactState.snapshot()
  }, [dirty, handleSave, artifactState])

  return (
    <>
    <DetailPanel
      embedded={embedded}
      title={fileName}
      onClose={guardedClose}
      initialWidth={480}
      minWidth={420}
      reserveWidth={reserveWidth}
      storageKey="mc-panel-width"
      customHeader={
        /* Single-bar toolbar: the tab chip owns
           identity + close, so this bar carries a static breadcrumb + dirty
           dot + diff stats on the left, and library actions (star /
           knowledge), View Source/Preview toggle, diff toggle, and the ⋯
           overflow on the right. In source mode a second row pops down
           (grid-rows transition — compositor-friendly in Electron, unlike
           height auto) with the editor options and Save / Cancel so the
           main bar never crowds. */
        <div className="shrink-0 border-b border-border">
          <div className="flex items-center gap-2 h-[38px] px-3">
            <FileText size={14} className="text-muted shrink-0" />
            <span className="flex items-center min-w-0" title={filePath}>
              {crumbs.map((c, i) => {
                const clickable = !c.isFile && !!onOpenFolder
                return (
                  <span key={i} className="flex items-center min-w-0 text-[12px]">
                    {i > 0 && <ChevronRight size={14} className="text-muted opacity-60 shrink-0 mx-0.5" />}
                    {clickable ? (
                      <Clickable
                        onClick={() => onOpenFolder?.(c.path)}
                        className="truncate text-muted hover:text-text hover:underline cursor-pointer rounded px-0.5 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                        title={i18nT('components.markdownPanel.open_folder', { path: c.path })}
                      >{c.seg}</Clickable>
                    ) : (
                      <span className={`truncate ${c.isFile ? 'text-text-strong font-medium' : 'text-muted'}`}>{c.seg}</span>
                    )}
                  </span>
                )
              })}
            </span>
            {dirty && <span className="text-warn text-[15px] leading-none shrink-0" title={i18nT('components.markdownPanel.unsaved_changes')}>●</span>}
            {diffMode && (diffStats.added > 0 || diffStats.removed > 0) && (
              <span className="text-[11px] font-mono font-semibold shrink-0">
                {diffStats.added > 0 && <span className="text-ok">+{diffStats.added}</span>}
                {diffStats.removed > 0 && <span className="text-danger ml-1.5">-{diffStats.removed}</span>}
              </span>
            )}
            <span className="flex-1 min-w-[8px]" />
            <FileArtifactActionButton state={artifactState} />
            {(() => {
              const kExt = '.' + (filePath.split('.').pop() || '').toLowerCase()
              const canK = knowledge.formats && knowledge.formats.includes(kExt)
              if (!canK) return null
              return <KnowledgeToggleIconButton state={knowledge} />
            })()}
            {canPreview && (
              <button
                className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer transition-colors shrink-0"
                onClick={() => setEditing(!editing)}
                aria-pressed={editing}
              >{editing ? i18nT('components.markdownPanel.view_preview') : i18nT('components.markdownPanel.view_source')}</button>
            )}
            {!isRichType && diffMode && (
              <button className={barIconBtn(diffSplit)} onClick={() => setDiffSplit(!diffSplit)} title={diffSplit ? i18nT('components.markdownPanel.switch_to_unified_view') : i18nT('components.markdownPanel.switch_to_split_view')} aria-label={diffSplit ? i18nT('components.markdownPanel.switch_to_unified_view') : i18nT('components.markdownPanel.switch_to_split_view')} aria-pressed={diffSplit}><Columns2 size={14} /></button>
            )}
            {!isRichType && (
              <button className={barIconBtn(diffMode)} onClick={toggleDiffMode} title={i18nT('components.markdownPanel.toggle_diff_view')} aria-label={i18nT('components.markdownPanel.toggle_diff_view')} aria-pressed={diffMode}><FileDiff size={14} /></button>
            )}
            <OverflowMenu filePath={filePath} content={content}
              onRefresh={handleRefresh} refreshDisabled={refreshing || dirty} refreshTitle={dirty ? i18nT('components.markdownPanel.save_or_discard_changes_first') : i18nT('components.markdownPanel.refresh_file_re_read_from_disk')}
              onFullscreen={() => setFullscreen(f => !f)} fullscreen={fullscreen}
              onSnapshot={artifactState.existing ? handleSnapshot : undefined} snapshotting={artifactState.snapshotting}
            />
          </div>
          {/* Source-mode row: always mounted so grid-template-rows animates
              open/closed without the choppiness of height:auto in Electron. */}
          {!isRichType && (
            <div className="grid transition-[grid-template-rows] duration-200 ease-out" style={{ gridTemplateRows: editing ? '1fr' : '0fr' }} aria-hidden={!editing}>
              <div className="overflow-hidden min-h-0">
                <div className="flex items-center gap-1.5 h-[36px] px-3 overflow-x-auto scrollbar-none">
                  <button className={barLabelBtn(wordWrap)} onClick={() => setWordWrap(!wordWrap)} title={i18nT('components.markdownPanel.toggle_word_wrap')} aria-pressed={wordWrap} tabIndex={editing ? 0 : -1}><WrapText size={13} /><span>{i18nT('components.markdownPanel.word_wrap')}</span></button>
                  <button className={barLabelBtn(autocomplete)} onClick={() => setAutocomplete(!autocomplete)} title={i18nT('components.markdownPanel.toggle_autocomplete')} aria-pressed={autocomplete} tabIndex={editing ? 0 : -1}><Zap size={13} /><span>{i18nT('components.markdownPanel.autocomplete')}</span></button>
                  <button className={barLabelBtn(lineNums)} onClick={() => setLineNums(!lineNums)} title={i18nT('components.markdownPanel.toggle_line_numbers')} aria-pressed={lineNums} tabIndex={editing ? 0 : -1}><Hash size={13} /><span>{i18nT('components.markdownPanel.line_numbers')}</span></button>
                  <span className="flex-1" />
                  {/* Cancel/Save appear only once there's something to save;
                      a clean buffer keeps the row to just the view options. */}
                  {dirty && (<>
                    <button className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium text-muted hover:text-text border border-border bg-transparent cursor-pointer transition-colors disabled:opacity-40 shrink-0" onClick={handleCancel} disabled={refreshing} title={i18nT('components.markdownPanel.cancel_discard_unsaved_edits')} tabIndex={editing ? 0 : -1}>{i18nT('components.markdownPanel.cancel')}</button>
                    <button className="px-3 h-[26px] rounded-md text-[11.5px] font-semibold border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover transition-all disabled:opacity-40 shrink-0" disabled={saving} onClick={handleSave} tabIndex={editing ? 0 : -1}>{saving ? i18nT('components.markdownPanel.saving') : i18nT('components.markdownPanel.save')}</button>
                  </>)}
                </div>
              </div>
            </div>
          )}
        </div>
      }
    >
      {saveError && <div className="text-[11px] text-danger">{saveError}</div>}
      {/* Comment hint for markdown files */}
      {isMarkdown && !editing && onSubmitComments && !hintDismissed && (
        <CommentHint onDismiss={dismissHint} />
      )}
      {/* Code / editor / diff views run flush (edge-to-edge) against the
          panel — only markdown preview keeps reading padding. */}
      <div className={`flex-1 overflow-hidden -mx-5 -my-4 flex ${isMarkdown && !editing && !diffMode ? 'py-4 pl-4 pr-0' : ''}`}>
        {!fullscreen && <div data-mc-mdpanel className="relative flex-1 min-w-0 min-h-0">
          {findBar}
          {/* In markdown preview the scroll box runs flush to the panel's right
              border so the overlay scrollbar and outline rail share that edge;
              pr-6 keeps the text clear of the ticks. */}
          <div ref={sidePanelScrollRef} className={`h-full overflow-auto ${isMarkdown && !editing ? 'scrollbar-overlay pr-6' : ''}`}>
            {!diffChecking && !isRichType && (
              <DiffEditorBlock flush sideBySide={diffSplit} diffMode={diffMode} lang={lang} originalContent={originalContent} content={content} dark={dark} diffActiveRef={diffActiveRef} handleChange={handleChange} editing={editing} lineNums={lineNums} wordWrap={wordWrap} autocomplete={autocomplete} onSelect={onSubmitComments ? (text, rect) => setMonacoSelection({ text, x: rect.x, y: rect.y }) : undefined} />
            )}
            {!diffMode && <ContentRenderer flush isRichType={isRichType} fileType={fileType} filePath={filePath} content={content} editing={editing} lang={lang} lineNums={lineNums} wordWrap={wordWrap} autocomplete={autocomplete} onChange={handleChange}
              previewRef={previewRef} displayContent={displayContent} isMarkdown={isMarkdown} highlightedHtml={highlightedHtml} gutterReadRef={gutterReadRef} markdownClassName="msg-content text-sm leading-relaxed" onEditorMount={handleEditorMount} />}
          </div>
          {isMarkdown && !editing && <MarkdownOutlineRail containerRef={sidePanelScrollRef} />}
        </div>}
      </div>
      {!fullscreen && !editing && <SelectionToolbar containerRef={sidePanelScrollRef} actions={selectionActions} externalSelection={monacoSelection} />}
      {!fullscreen && <CommentOverlayBlock popover={popover} addComment={addComment} setPopover={clearPopover} onSubmitComments={onSubmitComments} comments={comments} editComment={editComment} removeComment={removeComment} submitAllComments={submitAllComments} />}
    </DetailPanel>
    {fullscreen && createPortal(
      // The onKeyDown here implements a focus trap for the modal dialog; a
      // role="dialog"/aria-modal container legitimately owns keyboard handling.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
      <div className="fixed inset-0 z-[9999] bg-bg flex flex-col" role="dialog" aria-modal="true" aria-label={i18nT('components.markdownPanel.full_screen_file_preview')}
        ref={el => { if (el && !el.dataset.focused) { el.dataset.focused = '1'; const first = el.querySelector<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); first?.focus() } }}
        onKeyDown={e => { if (e.key === 'Tab') { if ((document.activeElement as HTMLElement)?.closest('.monaco-editor')) return; const focusable = e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); if (focusable.length === 0) return; const first = focusable[0], last = focusable[focusable.length - 1]; if (e.shiftKey) { if (document.activeElement === first) { e.preventDefault(); last.focus() } } else { if (document.activeElement === last) { e.preventDefault(); first.focus() } } } }}>

        {/* Header — pl-20 clears macOS traffic-light buttons */}
        <div className="flex items-center justify-between pl-20 pr-6 h-12 shrink-0 border-b border-border">
          <span className="text-base font-semibold text-text-strong truncate">{fileName}</span>
          <div className="flex items-center gap-1.5">
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40" onClick={handleRefresh} disabled={refreshing || dirty} title={dirty ? i18nT('components.markdownPanel.save_or_discard_changes_first') : i18nT('components.markdownPanel.refresh_file')} aria-label={i18nT('components.markdownPanel.refresh_file')}><RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /></button>
            <FileArtifactActionButton state={artifactState} />
            {(() => {
              const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
              const canK = knowledge.formats && knowledge.formats.includes(ext)
              if (!canK) return null
              return <KnowledgeToggleIconButton state={knowledge} />
            })()}
            {editorToolbarButtons}
            <OverflowMenu filePath={filePath} content={content} />
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all" onClick={() => setFullscreen(false)} title={i18nT('components.markdownPanel.exit_full_screen_esc')} aria-label={i18nT('components.markdownPanel.exit_full_screen')}><Minimize2 size={14} /></button>
          </div>
        </div>
        {saveError && <div className="px-16 text-[11px] text-danger">{saveError}</div>}
        {isMarkdown && !editing && onSubmitComments && !hintDismissed && <div className="px-16"><CommentHint onDismiss={dismissHint} /></div>}
        {/* Body */}
        <div data-mc-mdpanel className="relative flex-1 overflow-hidden min-h-0">
          {findBar}
          <div ref={fullscreenBodyRef} className="h-full overflow-auto px-16 py-4">
            {!isRichType && <DiffEditorBlock sideBySide={diffSplit} diffMode={diffMode} lang={lang} originalContent={originalContent} content={content} dark={dark} diffActiveRef={diffActiveRef} handleChange={handleChange} editing={editing} lineNums={lineNums} wordWrap={wordWrap} autocomplete={autocomplete} onSelect={onSubmitComments ? (text, rect) => setMonacoSelection({ text, x: rect.x, y: rect.y }) : undefined} />}
            {!diffMode && <ContentRenderer isRichType={isRichType} fileType={fileType} filePath={filePath} content={content} editing={editing} lang={lang} lineNums={lineNums} wordWrap={wordWrap} autocomplete={autocomplete} onChange={handleChange}
              previewRef={fullscreenPreviewRef} displayContent={displayContent} isMarkdown={isMarkdown} highlightedHtml={highlightedHtml} gutterReadRef={gutterFullscreenRef} previewStyle={mdPreviewStyle} onEditorMount={handleEditorMount} />}
          </div>
          {isMarkdown && !editing && <MarkdownOutlineRail containerRef={fullscreenBodyRef} />}
        </div>
        {!editing && <SelectionToolbar containerRef={fullscreenBodyRef} actions={selectionActions} externalSelection={monacoSelection} />}
        <CommentOverlayBlock popover={popover} addComment={addComment} setPopover={clearPopover} onSubmitComments={onSubmitComments} comments={comments} editComment={editComment} removeComment={removeComment} submitAllComments={submitAllComments} scrollRef={fullscreenBodyRef} />
        {/* Footer */}
        <Clickable className="shrink-0 flex items-center px-3 h-6 text-[11px] text-muted font-mono truncate cursor-pointer hover:text-text transition-colors" title={i18nT('components.markdownPanel.click_to_copy_path')} onClick={() => copyToClipboard(filePath)}>{filePath}</Clickable>
      </div>,
      document.body
    )}
    </>
  )
}))
