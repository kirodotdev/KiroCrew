import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, Ellipsis, ChevronRight, Columns2, Hash, WrapText, FoldVertical, Maximize2, Minimize2, BookOpen, BookmarkPlus, Camera, Check, Component, FileDiff } from 'lucide-react'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useFileMenuItems, visibleFileMenuItems, invokeFileMenuItem, FileMenuItemIcon, FileMenuItemLabel } from '../apps/fileMenuContributions'
import { FILE_EXPLORER_APP, FILE_EXPLORER_ROUTE, fileExplorerDeepLink } from '../apps/file-explorer/deepLink'
import { useInstalledApps } from '../hooks/panelTabRegistry'
import { appNavTarget } from '../appNav'
import { useBranding } from '../hooks/useBranding'
import { downloadFileToDisk, isAbsolutePath } from '../utils/fileReadUrl'
import { copyToClipboard } from '../utils/clipboard'
import { WINDOWS_ABS_PATH_RE } from '../utils/urlTransform'
import { i18nT } from '../i18n/t'
import FilePathMenu, { revealOrOpen, useRevealLabel, useCanOpenFile } from './FilePathMenu'
import { useFileArtifactState, useFileKnowledgeState, type ReportError } from './useMarkdownPanelFileState'

/**
 * `MarkdownPanel`'s header controls: the path breadcrumb, the artifact and
 * knowledge-library toggles, and the ⋯ overflow menu with its view toggles,
 * library rows, file-location group and app-contributed rows. The panel lays
 * the bar out; each control here owns its own behaviour. `MarkdownPanel.tsx`
 * re-exports `breadcrumbSegments`, `FileHeaderBreadcrumb` and `OverflowMenu`.
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
 * resolves against the wrong root. A drive-rooted Windows path (`C:\x`, `C:/x`)
 * needs no such restoration: unlike POSIX's leading '/', its root is not a
 * separator at all, so split()+filter(Boolean) leaves it in place as the first
 * segment ('C:') and the plain join reconstructs it correctly on its own — the
 * bug here was never the missing prefix, it was that the OLD split (`/` only)
 * read a whole backslash path as a single segment. Splitting on either
 * separator, and rejoining with whichever one the input used, fixes the
 * Windows shape. Only a drive-rooted path (`WINDOWS_ABS_PATH_RE`) is split on
 * `\`: on POSIX a backslash is a legal filename character, so
 * `/tmp/we\ird.md` stays one segment. One exception to the plain join: when the
 * path is short enough (<= 3 segments) that the drive itself is a shown crumb,
 * its own path is the drive ROOT (`C:\`), not the bare `C:` the join yields --
 * on Windows a bare `C:` is drive-RELATIVE (the drive's current directory),
 * which would break the absolute-path contract of `BreadcrumbSegment`.
 * Exported for unit tests.
 */
export function breadcrumbSegments(filePath: string): BreadcrumbSegment[] {
  const isPosixAbs = filePath.startsWith('/')
  const isWindows = WINDOWS_ABS_PATH_RE.test(filePath)
  const sep = isWindows && filePath.includes('\\') ? '\\' : '/'
  const splitAt = isWindows ? /[\\/]+/ : /\/+/
  const allSegs = filePath.split(splitAt).filter(Boolean)
  const shown = Math.min(3, allSegs.length)
  return allSegs.slice(-3).map((seg, j) => {
    const absIndex = allSegs.length - shown + j
    const joined = allSegs.slice(0, absIndex + 1).join(sep)
    // The drive segment alone (`C:`) is drive-relative; its root needs the
    // trailing separator (`C:\` / `C:/`) to be absolute.
    const isDriveSeg = isWindows && absIndex === 0
    const path = isPosixAbs ? '/' + joined : isDriveSeg ? joined + sep : joined
    return { seg, path, isFile: absIndex === allSegs.length - 1 }
  })
}

/**
 * The file-viewer header's path display. Only the last three segments are shown
 * (see breadcrumbSegments), so the full path lives in the hover affordance.
 *
 * Three routes to the full path, one per user:
 * - `title` shows it on POINTER hover.
 * - `role="group"` + `aria-label={filePath}` name the focused element with the
 *   full path, so a SCREEN READER announces it on focus.
 * - a small readout below the breadcrumb, shown only while it has keyboard
 *   focus, prints the full path for a SIGHTED KEYBOARD-only user, who hears no
 *   aria-label and to whom a native `title` does not open on focus.
 *
 * `tabIndex={0}` is what puts the element in the tab order in the first place.
 * The label and the readout both ARE the path (runtime data), so no localized
 * string is added. Right-click still opens the FilePathMenu, which offers "Show
 * folder in side panel" when the host passes `onFolderOpen`. The file is
 * already on screen here, so the menu gets no "Open in side panel" handler.
 *
 * Exported so the accessibility contract is unit-testable without mounting the
 * whole panel (which drags in the Pierre editor tree).
 */
export function FileHeaderBreadcrumb({ filePath, onFolderOpen }: { filePath: string; onFolderOpen?: (path: string) => void }) {
  const crumbs = breadcrumbSegments(filePath)
  const [focused, setFocused] = useState(false)
  return (
    // Content-sized (NOT flex-1), so the header keeps its original order:
    // breadcrumb, then the diff-stats badge beside the filename, then the
    // spacer, then the actions. The focus readout below is anchored to the
    // header BAR (its relative ancestor), not to this row, so its width is the
    // panel's, not the compressed breadcrumb's -- see the readout comment.
    <>
      <FilePathMenu filePath={filePath} onFolderOpen={onFolderOpen}>
        {/* Focusable so a keyboard user can read the full path: a screen reader
            from the accessible name, a sighted keyboard user from the readout
            below. Same focusable-region pattern as CodeBlock / FileRenderers. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <div
          className="flex items-center min-w-0 focus-ring-accent rounded-sm"
          title={filePath}
          role="group"
          aria-label={filePath}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
        >
          {crumbs.map((c, i) => (
            <span key={i} className="flex items-center min-w-0 text-[12px]">
              {i > 0 && <ChevronRight size={14} className="text-muted opacity-60 shrink-0 mx-0.5" />}
              <span className={`truncate ${c.isFile ? 'text-text-strong font-medium' : 'text-muted'}`}>{c.seg}</span>
            </span>
          ))}
        </div>
      </FilePathMenu>
      {focused && (
        // The visible half of the keyboard route: the full path on screen for a
        // sighted keyboard user. aria-hidden because the group's aria-label
        // already carries it, so a screen reader would otherwise hear it twice.
        // Anchored left-3 right-3 to the header BAR (its relative ancestor, with
        // matching px-3), so its right edge is the panel's inner edge: the path
        // wraps (break-all) inside the panel and the box grows downward, never
        // past the edge, at any panel width -- and the header's own layout
        // (diff badge included) is left exactly as it was.
        <span
          aria-hidden="true"
          data-testid="file-header-full-path"
          className="absolute left-3 right-3 top-full mt-1 z-10 px-2 py-1 rounded-md border border-border bg-bg-elevated text-[11px] font-mono text-text-strong break-all shadow-sm"
        >{filePath}</span>
      )}
    </>
  )
}

async function downloadFile(filePath: string, onError: ReportError) {
  await downloadFileToDisk(filePath, onError)
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
/** 26px square icon toggle for the file toolbar (borderless, accent when on). */
export const barIconBtn = (on: boolean) =>
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
export function FileArtifactActionButton({ state }: { state: ReturnType<typeof useFileArtifactState> }) {
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
 * unconfigured). When already added, renders as a green badge — removable
 * (real button) when the source is a per-file local_file, inert otherwise.
 */
export function KnowledgeToggleIconButton({ state }: { state: ReturnType<typeof useFileKnowledgeState> }) {
  // A per-file local_file source is one this panel created, so the toggle
  // flips both ways: clicking the green badge removes it again. Folder and
  // other wider sources keep the inert badge — they are owned by the
  // Knowledge page's own delete flows, not this file panel.
  if (state.alreadyAdded && state.sourceType === 'local_file' && state.sourceId) {
    return (
      <button
        className="p-1.5 rounded-md border border-border/40 text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-50 inline-flex items-center"
        onClick={() => state.remove()}
        disabled={state.removing}
        title={i18nT('components.markdownPanel.remove_from_knowledge_library')}
        aria-label={i18nT('components.markdownPanel.remove_from_knowledge_library')}
      >
        <BookOpen size={14} style={{ color: 'var(--ok)' }} />
      </button>
    )
  }
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

/**
 * One row of the ⋯ overflow menu.
 *
 * The roving-focus tint is `focus-visible`, not `focus`: the WAI-ARIA menu
 * pattern moves real DOM focus onto the first row as the menu opens, and a
 * plain `focus:` tint paints that row with the same colour as `hover:` for as
 * long as the menu stays open. A pointer user then sees the first row lit the
 * whole time — and two lit rows at once as soon as they hover something else.
 * `:focus-visible` matches a script-moved focus only when the interaction that
 * moved it was a keypress, so arrow-key navigation keeps its indicator while a
 * mouse click paints nothing.
 */
// `min-w-0 overflow-hidden`: the menu is capped in width (see the container below), and
// a row's truncating children only shrink when the row itself may shrink past its
// content. Without it a long app-contributed label sets the row's width and spills past
// the cap instead of being clipped by it.
const menuRowCls = 'flex items-center gap-2 w-full min-w-0 overflow-hidden px-3 py-1.5 text-[13px] text-text cursor-pointer border-none bg-transparent text-left whitespace-nowrap hover:bg-bg-hover focus-visible:bg-bg-hover focus:outline-hidden'

/**
 * The Files app as a destination for the file under the ⋯ menu, or `null` when
 * there is none to offer.
 *
 * Eligibility, route and label come from the same `appNavTarget` derivation the
 * left rail and the command palette use, so this row cannot offer an app those
 * two hide: the Files app ships `defaultEnabled: false`, and a disabled app has
 * nowhere to go. Narrower than theirs in one way — the row needs the NATIVE
 * page (`FILE_EXPLORER_ROUTE`), because only `FileExplorerPage` reads the
 * `?path=` the link carries. An orphaned app (routed to its migration page) or
 * one an edition re-homes under AppHost would swallow the param and land the
 * reader on a page that opened nothing, so neither gets a row.
 *
 * Costs no request: `useInstalledApps` observes the shell's own `['apps']`
 * query, which the sidebar fetches on every dashboard load.
 */
function useFilesAppTarget(): { label: string } | null {
  const { apps } = useInstalledApps()
  return useMemo(() => {
    const app = apps.find(a => a.name === FILE_EXPLORER_APP)
    const target = app ? appNavTarget(app) : null
    return target?.route === FILE_EXPLORER_ROUTE ? { label: target.label } : null
  }, [apps])
}

export function OverflowMenu({ filePath, content, onError, onRefresh, refreshDisabled, refreshTitle, onFullscreen, fullscreen, onSnapshot, snapshotting, wordWrap, onToggleWordWrap, lineNums, onToggleLineNums, collapseUnchanged, onToggleCollapseUnchanged, diffSplit, onToggleDiffSplit, diffView, onToggleDiffView, onDownload }: {
  filePath: string; content: string
  /** Where a failed row action (add to knowledge, promote, snapshot, save,
   *  download, open/reveal, an app-contributed row's dispatch) is reported: the
   *  panel renders it through the shared ErrorNotice. The menu itself closes on
   *  select, so it cannot host the notice. */
  onError: ReportError
  /** What the Download row does instead of fetching the path: the panel passes
   *  its buffer download while the file is gone from disk, where the fetch
   *  would only answer 404 for the one file the banner just said to keep. */
  onDownload?: () => void
  /** View actions folded in from the old header row (side-panel revamp): the
   *  ⋯ menu is the single home for everything that isn't a mode toggle. */
  onRefresh?: () => void; refreshDisabled?: boolean; refreshTitle?: string
  onFullscreen?: () => void; fullscreen?: boolean
  /** Editor view toggles (checkbox rows) — provided by the embedded panel. */
  wordWrap?: boolean; onToggleWordWrap?: () => void
  lineNums?: boolean; onToggleLineNums?: () => void
  /** Diff-only: fold the unchanged stretches between hunks. */
  collapseUnchanged?: boolean; onToggleCollapseUnchanged?: () => void
  /** Diff-only: side-by-side instead of unified. A menu row rather than a
   *  header button so the action row stays within its two-control cap once the
   *  file-browser toggle is present. */
  diffSplit?: boolean; onToggleDiffSplit?: () => void
  /** The diff toggle for a rich-type file (JSON, CSV, ...), which opens on its
   *  viewer: a menu row for the same two-control cap, since its header holds
   *  no diff button. */
  diffView?: boolean; onToggleDiffView?: () => void
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
  // Open/Reveal shell out on the gateway, so they only make sense when the
  // browser is on that same machine. Remote/tunneled sessions (directLocal
  // false) see the clipboard/download fallbacks only — matching the shared
  // FilePathMenu, which self-gates on the same flag.
  const { directLocal } = useBranding()
  // Open uses the shared gate (FilePathMenu.useCanOpenFile): directLocal AND a
  // non-Windows gateway — a local Windows user must not get an Open row here
  // that the shared menu hides. This panel only renders file content, so no
  // kind is passed (dir suppression is moot). Reveal keeps the laxer
  // directLocal-only gate because reveal works on Windows.
  const canOpen = useCanOpenFile()
  // Platform-aware reveal label from the shared owner (FilePathMenu) so this
  // overflow and FileViewer's overflow name the identical action identically.
  const revealLabel = useRevealLabel()
  // "Open in Files": the in-dashboard destination, gated on the app being
  // enabled and natively routed (see useFilesAppTarget) and on the path being
  // absolute — the Files backend has no project directory to resolve a relative
  // path against, so for one of those the row would only promise an error.
  const filesApp = useFilesAppTarget()
  const openInFiles = filesApp && isAbsolutePath(filePath) ? filesApp : null
  const knowledge = useFileKnowledgeState(filePath, onError)
  const artifact = useFileArtifactState(filePath, content, onError)
  const contribItems = useFileMenuItems('file-overflow')
  const contribRows = visibleFileMenuItems(contribItems, { path: filePath, kind: 'file' })
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
        <div ref={listRef} role="menu" tabIndex={-1} onKeyDown={onListKeyDown} className="absolute right-0 top-full mt-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-1 min-w-[180px] w-max max-w-[min(420px,calc(100vw-2rem))]">
          {onRefresh && (
            <button role="menuitem" data-option tabIndex={-1} className={`${menuRowCls} disabled:opacity-40`} disabled={refreshDisabled} title={refreshTitle} onClick={() => { onRefresh(); setOpen(false) }}>
              <RefreshCw size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.refresh')}
            </button>
          )}
          {onFullscreen && (
            <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { onFullscreen(); setOpen(false) }}>
              {fullscreen ? <Minimize2 size={14} className="lucide-inline" /> : <Maximize2 size={14} className="lucide-inline" />} {fullscreen ? i18nT('components.markdownPanel.exit_full_screen') : i18nT('components.markdownPanel.full_screen')}
            </button>
          )}
          {/* View options are their own section: they change how the file is
              DISPLAYED, unlike refresh/full-screen above which act on it. */}
          {(onToggleWordWrap || onToggleLineNums || onToggleCollapseUnchanged || onToggleDiffView) && (
            <div className="h-px bg-border my-1 mx-2" />
          )}
          {onToggleDiffView && (
            <button role="menuitemcheckbox" aria-checked={!!diffView} data-option tabIndex={-1} className={menuRowCls} onClick={onToggleDiffView}>
              <FileDiff size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.show_changes')} {diffView && <Check size={14} className="lucide-inline" />}
            </button>
          )}
          {onToggleWordWrap && (
            <button role="menuitemcheckbox" aria-checked={!!wordWrap} data-option tabIndex={-1} className={menuRowCls} onClick={onToggleWordWrap}>
              <WrapText size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.word_wrap')} {wordWrap && <Check size={14} className="lucide-inline" />}
            </button>
          )}
          {onToggleLineNums && (
            <button role="menuitemcheckbox" aria-checked={!!lineNums} data-option tabIndex={-1} className={menuRowCls} onClick={onToggleLineNums}>
              <Hash size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.line_numbers')} {lineNums && <Check size={14} className="lucide-inline" />}
            </button>
          )}
          {onToggleCollapseUnchanged && (
            <button role="menuitemcheckbox" aria-checked={!!collapseUnchanged} data-option tabIndex={-1} className={menuRowCls} onClick={onToggleCollapseUnchanged}>
              <FoldVertical size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.collapse_unchanged')} {collapseUnchanged && <Check size={14} className="lucide-inline" />}
            </button>
          )}
          {onToggleDiffSplit && (
            <button role="menuitemcheckbox" aria-checked={!!diffSplit} data-option tabIndex={-1} className={menuRowCls} onClick={onToggleDiffSplit}>
              <Columns2 size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.split_view')} {diffSplit && <Check size={14} className="lucide-inline" />}
            </button>
          )}
          <div className="h-px bg-border my-1 mx-2" />
          {artifact.existing ? (
            <button
              role="menuitem" data-option tabIndex={-1} className={menuRowCls}
              onClick={() => { navigate(`/artifacts/${encodeURIComponent(artifact.existing!.slug)}`); setOpen(false) }}
              title={i18nT('components.markdownPanel.open_artifact', { name: artifact.existing.slug })}
            >
              <BookmarkPlus size={14} className="lucide-inline" style={{ color: 'var(--ok)' }} /> {i18nT('components.markdownPanel.in_artifacts')} <Check size={14} className="lucide-inline" />
            </button>
          ) : (
            <button
              role="menuitem" data-option tabIndex={-1} className={`${menuRowCls} disabled:opacity-50`}
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
            <button role="menuitem" data-option tabIndex={-1} className={`${menuRowCls} disabled:opacity-50`} onClick={() => { onSnapshot(); delayedClose() }} disabled={snapshotting} title={i18nT('components.markdownPanel.capture_the_current_file_content_as_a_new_artifa')}>
              <Camera size={14} className="lucide-inline" /> {snapshotting ? i18nT('components.markdownPanel.snapshotting') : i18nT('components.markdownPanel.snapshot_version')}
            </button>
          )}
          {canAddToKnowledge && (
            knowledge.alreadyAdded ? (
              knowledge.sourceType === 'local_file' && knowledge.sourceId ? (
                <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { knowledge.remove(undefined, { onSuccess: delayedClose }) }} disabled={knowledge.removing}>
                  <BookOpen size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.remove_from_knowledge_library')}
                </button>
              ) : (
                <span className="flex items-center gap-2 w-full px-3 py-1.5 text-[13px] text-muted">
                  <BookOpen size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.in_library')} <Check size={14} className="lucide-inline" />
                </span>
              )
            ) : (
              <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => knowledge.add(undefined, { onSuccess: delayedClose })} disabled={knowledge.adding}>
                {knowledge.added ? <><BookOpen size={14} className="lucide-inline" style={{color: 'var(--ok)'}} /> {knowledge.addResult === 'exists' ? i18nT('components.markdownPanel.already_in_library') : i18nT('components.markdownPanel.added')}</> : knowledge.adding ? i18nT('components.markdownPanel.adding_2') : <><BookOpen size={14} className="lucide-inline" /> {i18nT('components.markdownPanel.add_to_knowledge')}</>}
              </button>
            )
          )}
          <div className="h-px bg-border my-1 mx-2" />
          {/* File-location group: hand the file to another surface — the Files
              app inside the dashboard first, then the desktop — then the
              clipboard/download fallbacks for hosts that have no desktop.
              Iconless like its neighbours — the group reads as a list of
              destinations, and two glyphs among six would look arbitrary.
              Open in Files needs the app enabled at its native route plus an
              absolute path (openInFiles); Open uses the shared canOpen gate
              (directLocal + non-Windows); Reveal uses directLocal alone — a
              remote session cannot usefully drive Finder on the gateway, so it
              sees the fallbacks only. Same gates the shared FilePathMenu applies. */}
          {openInFiles && (
            <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { navigate(fileExplorerDeepLink(filePath)); setOpen(false) }}>
              {i18nT('components.markdownPanel.open_in_app', { app: openInFiles.label })}
            </button>
          )}
          {canOpen && (
            <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { void revealOrOpen(filePath, 'open', { onError }); setOpen(false) }}>
              {i18nT('components.markdownPanel.open_with_default_app')}
            </button>
          )}
          {directLocal && (
            <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { void revealOrOpen(filePath, 'reveal', { onError }); setOpen(false) }}>
              {revealLabel}
            </button>
          )}
          <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { copyToClipboard(filePath); setOpen(false) }}>
            {i18nT('components.markdownPanel.copy_path')}
          </button>
          <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { copyToClipboard(content); setOpen(false) }}>
            {i18nT('components.markdownPanel.copy_content')}
          </button>
          <button role="menuitem" data-option tabIndex={-1} className={menuRowCls} onClick={() => { if (onDownload) onDownload(); else void downloadFile(filePath, onError); setOpen(false) }}>
            {i18nT('components.markdownPanel.download')}
          </button>
          {/* App-contributed rows (`contributes.fileMenuItems`, surface
              'file-overflow'), LAST and in their own group: the core rows above are
              a fixed vocabulary a reader learns, and splicing third-party rows into
              the middle of it would move a familiar row whenever an app is
              installed. Activation POSTs the file's PATH to the app's own endpoint;
              core imports no app code. Filtered by the declaration's `when`
              predicate, so the stock build (no declaring app) renders neither the
              rows nor the separator. `data-option` is what makes each row navigable
              to `useListboxKeyboard`. */}
          {contribRows.length > 0 && <div className="h-px bg-border my-1 mx-2" />}
          {contribRows.map(item => (
            <button
              key={`${item.app}:${item.id}`}
              role="menuitem"
              data-option
              tabIndex={-1}
              className={menuRowCls}
              onClick={() => {
                invokeFileMenuItem(item, { surface: 'file-overflow', path: filePath, kind: 'file' }, onError)
                setOpen(false)
              }}
            >
              <FileMenuItemIcon name={item.icon} /> <FileMenuItemLabel item={item} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
