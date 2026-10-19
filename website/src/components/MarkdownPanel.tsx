import { safeSetItem } from '../utils/safeStorage'
import { composerDraftStoreFor } from '../utils/composerDraftStore'
import { clearAnnotationHighlight, paintAnnotationHighlight } from '../utils/annotationHighlight'
import { memo, useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo, useImperativeHandle, forwardRef } from 'react'
import { OverlayPortal, useLocalCrewHidden } from './overlayRoot'
import { RefreshCw, Hash, WrapText, Minimize2, Copy, X, FileText, FileDiff, Folders, TriangleAlert } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import DetailPanel from './DetailPanel'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { useConfirm } from './ConfirmDialog'
import Clickable from './Clickable'
import { formatCommentsMessage, instructionDraftKeyFor, type InlineComment } from './CommentOverlay'
import SelectionToolbar, { type SelectionAction, type SelectionComposer } from './SelectionToolbar'
import MarkdownOutlineRail from './MarkdownToc'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { useDiffSplit } from '../hooks/useDiffSplit'
import { countLines } from './FileChangeChips'
import { findBestOccurrence } from '../hooks/useMarkdownCommentHighlights'
import { detectFileType, BinaryFileCard } from './FileRenderers'
import { ContentRenderer, MD_EXTS, extOf, langFor, wrapCode } from './ContentRenderer'
import { downloadFileName } from '../utils/fileReadUrl'
import { dropDeliveredComments, loadCommentDrafts, persistFileComments } from '../utils/commentDrafts'
import { copyToClipboard } from '../utils/clipboard'
import { anchorFromRange } from '../utils/selectionAnchor'
import { codePassagePosition, codeQuoteRange, shadowSelectionRange, watchText } from '../utils/pierreShadow'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { FileHeaderBreadcrumb, OverflowMenu, FileArtifactActionButton, KnowledgeToggleIconButton, barIconBtn } from './MarkdownPanelHeader'
import { useFileKnowledgeState, useFileArtifactState, type ReportError } from './useMarkdownPanelFileState'
import { CommentHint, MissingFileBanner, ZeroDiffNotice, DiffViewBlock, CommentOverlayBlock, MarkdownPanelFindBar } from './MarkdownPanelBody'
import { findCoords, findUniqueCoords, resolveSourcePos } from './markdownPanelSourcePos'
import { FindHighlightCtor, cssHighlights, FIND_HL_SUPPORTED, useMarkdownPanelFind } from './useMarkdownPanelFind'
import { useMarkdownPanelDisk, useMarkdownPanelDiff } from './useMarkdownPanelDisk'

export { breadcrumbSegments, FileHeaderBreadcrumb, OverflowMenu, type BreadcrumbSegment } from './MarkdownPanelHeader'
export { findCoords, resolveSourcePos } from './markdownPanelSourcePos'
export { MISSING_FILE_RETRY_MS, MISSING_FILE_RETRY_MAX_MS } from './useMarkdownPanelDisk'

/** Where a selection sits, as the comment composer anchors it. */
interface SelectionInfo {
  anchor: string
  rect: DOMRect
  range: Range | undefined
  line?: number
  column?: number
  /** Offset in the preview's text; absent for text the offset space does not hold. */
  startOffset?: number
  /** Selected inside a code block (see `InlineComment.inCode`). */
  inCode?: boolean
  /** Where in the preview's code blocks (see `InlineComment.codePosition`). */
  codePosition?: number
}

interface Props {
  filePath: string
  content: string
  /** `/api/file-read` refused to decode this file (a NUL byte inside its sniff
   *  window), so `content` is empty BY DESIGN and the body renders a
   *  download/reveal card. Without it the panel put 512 KB of U+FFFD in the
   *  code editor and offered to save it back over the real bytes. Rich types
   *  (image, pdf, video, …) keep their own viewer — they are binary too, and
   *  they read the file through `/api/file-raw`, not through this buffer. */
  binary?: boolean
  /** The read that filled the buffer did not hand over the whole file -- cut at
   *  the gateway's cap (`X-Truncated`) or with credentials redacted
   *  (`X-Redacted`). Host-owned like `binary`, for the same reason: the seed
   *  read that fills a tab runs before any panel mounts, and the body cannot
   *  tell about itself (a whole file may quote the redaction tag). Decides only
   *  WORDS and a file name once the file is gone: the banner and the close
   *  prompt say "only part of it", and a download of it is named `.partial`. */
  partial?: boolean
  onContentChange: (c: string) => void
  /** Disk-originated content (file watch, Refresh). Document tabs restamp
   *  their saved baseline here so a re-open still treats the tab as clean;
   *  omitted by other hosts, which fall back to onContentChange.
   *
   *  `binary` is the verdict for the bytes THIS read saw. It travels with the
   *  content because the two are one fact about one read: a file that was text
   *  when the tab opened can be replaced on disk by binary bytes (a build
   *  output, a checkout, an agent write), and patching the buffer while leaving
   *  the verdict at its hydrated value leaves the editor live over bytes it
   *  cannot represent — where a save would overwrite them. `undefined` means
   *  the caller has no verdict to report and the stored one stands. `partial`
   *  travels the same way, for the same read. */
  onDiskContent?: (c: string, binary?: boolean, partial?: boolean) => void
  onSave: (filePath: string, content: string) => Promise<void>
  onClose: () => void
  liveWatch?: boolean
  /** Hands the comment batch to chat. Returning (or resolving) `false`, or
   *  throwing, means the send was refused and the batch stays pending; any
   *  other result counts as delivered and clears it. The optional second
   *  argument carries the count of comment rows in the batch. */
  onSubmitComments?: (message: string, count?: number) => void | boolean | Promise<void | boolean>
  /** Gateway connection flag. Gates the batch comment submit (mirrors
   *  ChatInput's Send gating) so pending comments can't be composed and
   *  cleared while the chat send path would silently refuse the message.
   *  Defaults true for embeddings without a chat send path. */
  connected?: boolean
  onRefresh?: (filePath: string) => Promise<void>
  reserveWidth?: number
  /** Restored file-tab preference. Undefined allows the initial modified-file
   *  auto-diff; false explicitly keeps the normal preview/source view. */
  initialDiffMode?: boolean
  /** Lock the panel to a read-only working-tree DIFF (the `wtdiff` tab, #9695):
   *  the tab stays in diff mode, cannot be toggled into the editor, and shows
   *  no Edit/Save affordance. The plain `file:` tab remains the single editable
   *  buffer for the path, so there is never a second editor to race a save
   *  against. */
  readOnly?: boolean
  onDiffModeChange?: (diffMode: boolean) => void
  /** Render as a SidePanel tab body (fills parent, no resize handle/border). */
  embedded?: boolean
  /** Is this panel the VISIBLE tab? A host that keeps background tabs mounted
   *  and merely hides them has several live panels at once, and each installs
   *  its own document-level key handlers — so without this every hidden tab
   *  also answers Cmd+F, Escape and Cmd+S. Hosts that mount a single panel can
   *  leave this unset. */
  active?: boolean
  /** File-browser rail (grip + tree column) rendered to the RIGHT of the
   *  content, under the shared full-width header — the file-tab body owns the
   *  rail's data and open handling; this panel only places it. */
  browserRail?: React.ReactNode
  /** Whether the rail is shown. The header renders a Folders toggle at its far
   *  right edge when `onRailToggle` is provided. */
  railOpen?: boolean
  onRailToggle?: () => void
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
   *  it a true one-shot. */
  onRevealConsumed?: () => void
  /** Stable cross-remount identity (slot + tab id) for the embedded body's
   *  scroll position — a chat-slot switch unmounts the whole tab body, and
   *  this is what lets the document come back where the user left it (see
   *  `useScrollMemory`). Omitted by hosts without that lifecycle. */
  scrollMemoryKey?: string
  /** Open a folder as a folder tab in the host's side panel. When passed, the
   *  header path's right-click menu offers "Show folder in side panel", an
   *  in-app action that works whichever machine the browser is on. */
  onFolderOpen?: (path: string) => void
}

import { type PierreEditorHandle, type RevealTarget } from '../pierre'
import { countDiffStats } from '../utils/diffLineCounts'
import { i18nT } from '../i18n/t'
import { useDocumentImeLatch, useImeGuard } from '../hooks/useImeGuard'
import { useScrollMemory } from '../hooks/useScrollMemory'
import { pathBasename } from '../utils/pathBasename'

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
const RICH_FILE_TYPES = ['image', 'svg', 'csv', 'json', 'jsonl', 'html', 'pdf', 'excalidraw', 'video', 'audio', 'sheet', 'office']

/**
 * The subset of {@link RICH_FILE_TYPES} whose viewer reads the FILE, not the
 * buffer -- each of these fetches from `filePath` (`/api/file-raw`,
 * `/api/file-stream`, `/api/file-sheet`, `/api/file-office-preview`) and never
 * looks at `content`.
 *
 * That distinction is what decides who gets the binary card. The rest of
 * `RICH_FILE_TYPES` -- `svg`, `csv`, `json`, `jsonl`, `html`, `excalidraw` --
 * renders FROM the content string, so a file whose bytes could not be decoded
 * leaves those viewers with `''`: an empty table, an empty tree, a blank frame.
 * A `.json` file that is actually a database, or a `.csv` with a NUL in it,
 * belongs on the card exactly as a `.zip` does.
 *
 * Named by mechanism rather than by "is rich", because the reason a viewer may
 * skip the card is that it can still show the real bytes -- not that it happens
 * to be a viewer.
 */
const BYTE_BACKED_FILE_TYPES = new Set(['image', 'pdf', 'sheet', 'office', 'video', 'audio'])

const HINT_KEY = 'kirocrew:comment-hint-dismissed'

/** Below this panel width the browser rail overlays the content instead of
 *  splitting it: a 240-300px rail inside a ~320px panel leaves the editor a few
 *  dozen pixels, which is not a usable file view. */
const RAIL_SPLIT_MIN_W = 620

/** Imperative handle: lets a host trigger the SAME dirty-state close guard the
 *  panel uses internally (Escape / close button), so an external "back"/close
 *  control can't bypass the "Discard unsaved changes?" confirmation. */
export interface MarkdownPanelHandle {
  requestClose: () => void
  /** Run `nav` with a predicate that says whether this buffer may be replaced
   *  in place. For an external control that REPLACES this panel's file (the
   *  browser rail's tree click re-targets the tab in place), which destroys the
   *  buffer just as a close does but reaches none of the close paths. Asks the
   *  disk first, the way the close does, so a file deleted under a live stream
   *  is found before the buffer -- then its only copy -- is replaced. */
  requestNavigate: (nav: (stillClean: () => boolean) => void) => void
}

export default memo(forwardRef<MarkdownPanelHandle, Props>(function MarkdownPanel({ filePath, content, binary, partial, onContentChange, onDiskContent, onSave, onClose, liveWatch, onSubmitComments, connected = true, onRefresh, reserveWidth, initialDiffMode, readOnly = false, onDiffModeChange, embedded, active = true, savedBaseline, revealLine, onRevealConsumed, browserRail, railOpen, onRailToggle, scrollMemoryKey, onFolderOpen }: Props, ref) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const ime = useImeGuard()
  const qc = useQueryClient()
  // Code files (non-rich, non-markdown) have no meaningful preview — their
  // "preview" was just a read-only render of the same text. They open
  // straight in source mode and the Edit/Preview toggle is hidden for them.
  //
  // A requested line forces source mode: a line number only means something
  // against the source, and the rendered markdown preview has no per-line element
  // to scroll to (its `data-sourcepos` is per BLOCK, and soft wrapping breaks any
  // line-count correspondence anyway). So `README.md:42` opens the raw markdown at
  // line 42 rather than a paragraph that may contain it.
  //
  // Rich types are excluded, and that is a deliberate scope line rather than an
  // oversight. They have exactly ONE renderer by design — `isRichType` gates the
  // source/preview toggle, the line-number controls,
  // and the Cmd+S handler — so forcing one into an editor creates a file that is in
  // source mode with none of the chrome that makes source mode usable, including no
  // way back to its own viewer and no visible Save for a buffer the user has
  // edited. Making that coherent means teaching every one of those gates about a
  // rich-file-in-source-mode state, i.e. making rich files editable as text, which
  // is a larger feature than a citation jump. So `data.json:42` opens the JSON
  // viewer and drops the line: strictly better than the inert chip it used to be,
  // and it strands nothing.
  const revealTargetsSource = !RICH_FILE_TYPES.includes(detectFileType(filePath))
  // Markdown (and other preview-capable types) opens in its viewer; everything
  // else editable opens straight in the Pierre editor — there is no separate
  // read-only source mode for code files.
  const [editing, setEditing] = useState(() => {
    // Undecodable bytes have no buffer to edit, and a code file's default IS
    // the editor -- so this has to be refused in the initializer, not in an
    // effect, or the editor paints for a frame before the card replaces it.
    if (readOnly) return false // the wtdiff diff view is never an editor (#9695)
    if (binary && !BYTE_BACKED_FILE_TYPES.has(detectFileType(filePath))) return false
    if (revealLine && revealTargetsSource) return true
    if (MD_EXTS.has(extOf(filePath))) return false
    return !RICH_FILE_TYPES.includes(detectFileType(filePath))
  })
  const [diffMode, setDiffMode] = useState(readOnly ? true : (initialDiffMode ?? false))
  const toggleDiffMode = useCallback(() => {
    if (readOnly) return // locked to diff view
    const next = !diffMode
    setDiffMode(next)
    onDiffModeChange?.(next)
  }, [readOnly, diffMode, onDiffModeChange])
  // Unified vs side-by-side diff rendering — persisted, and shares its key
  // with SidePanel's diff tabs so the preference is app-wide.
  const [diffSplit, setDiffSplit] = useDiffSplit()
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(() => !readOnly && savedBaseline != null && content !== savedBaseline)
  // Mirrors `dirty` for callers that must read it AFTER an await, where a value
  // captured in a closure would answer for the moment they started rather than
  // the moment they are about to discard the buffer.
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  // When a saved baseline is provided (inline preview), keep `dirty` derived
  // from content-vs-disk so a RESTORED draft is dirty and the close guard fires.
  // No-op for document tabs (savedBaseline undefined) — they keep the manual
  // edit-driven dirty model below.
  useEffect(() => {
    if (readOnly) { setDirty(false); return } // a read-only diff view is never dirty (#9695)
    if (savedBaseline != null) setDirty(content !== savedBaseline)
  }, [content, savedBaseline, readOnly])
  const [saveError, setSaveError] = useState<string | null>(null)
  // The outcome of the last row action (add to knowledge, promote, snapshot,
  // save-as-artifact, download, open/reveal) that FAILED. These used to raise a
  // blocking `alert()` from inside the mutation hooks; they now land here and
  // render beside the save error through the shared ErrorNotice.
  const [actionError, setActionError] = useState<string | null>(null)
  // Scoped to the file it was raised for: a late rejection from the previous
  // file's action must not render under the file now open.
  useEffect(() => { setActionError(null) }, [filePath])
  const reportActionError = useCallback<ReportError>((message) => setActionError(message), [])
  // Editor view preferences — persisted so they survive tab switches/reloads.
  const [lineNums, setLineNums] = usePersistedBool('mc-file-linenums', true)
  const [wordWrap, setWordWrap] = usePersistedBool('mc-file-wordwrap', true)
  // Off by default: a file-panel diff shows the whole file, folding only when
  // asked. (Chat diff blocks keep Pierre's collapsed default.)
  const [collapseUnchanged, setCollapseUnchanged] = usePersistedBool('mc-file-collapse-unchanged', false)
  // The side panel renders markdown at a fixed default width (centered, capped
  // at --mc-content-width), matching the artifact detail page. No reading-width
  // (M/F) toggle.
  const mdPreviewStyle: React.CSSProperties = { maxWidth: 'var(--mc-content-width, 900px)', margin: '0 auto' }
  // Hydrate pending draft comments for this file from localStorage so they
  // survive panel close, refresh, and crash. Submitting clears them.
  // A read-only panel (the `wtdiff:` tab, #9695) carries NO comment drafts: it
  // never shows the composer or overlay and must not touch the per-path store,
  // so the plain `file:` tab is the single owner of a path's comments and two
  // coexisting panels cannot overwrite each other's drafts. Hydrating `[]` here
  // (plus skipping the persistence effect below) keeps the read-only panel out
  // of the store entirely.
  // The unsent overall instruction is kept across an unmount (in memory only)
  // per chat and document: the tab's slot-scoped key, then this gateway-local
  // path. Without a chat to scope it to, it is not kept at all.
  const instructionDraftKey = readOnly ? undefined : instructionDraftKeyFor(scrollMemoryKey, filePath)
  const draftsRef = useRef<ReturnType<typeof loadCommentDrafts>>(null!)
  if (draftsRef.current === null) draftsRef.current = loadCommentDrafts()
  const [comments, setComments] = useState<InlineComment[]>(() => readOnly ? [] : (draftsRef.current[filePath] ?? []))
  // Sync state to the new filePath during render (not in a useEffect) so
  // `comments` and `filePath` never disagree within a single render — otherwise
  // a callback firing in the transition window would persist against the wrong
  // file. setState-during-render is a supported React pattern (triggers a
  // re-render before commit).
  const prevFilePathRef = useRef(filePath)
  if (prevFilePathRef.current !== filePath) {
    prevFilePathRef.current = filePath
    setComments(readOnly ? [] : (draftsRef.current[filePath] ?? []))
  }
  // The anchor of the selection the composer is currently open over, resolved
  // while the DOM selection was still live (focus in the input collapses it).
  // A ref, not state: it is read only by the submit handler, and nothing renders
  // from it.
  const pendingAnchorRef = useRef<{ anchor: string; line?: number; column?: number; startOffset?: number; inCode?: boolean; codePosition?: number } | null>(null)
  // Whether the annotation box is open — read by the panel's document-level
  // Escape handler so it yields the key to the box instead of closing the panel.
  const composerOpenRef = useRef(false)
  // The annotation highlight paints through CSS.highlights, never by wrapping
  // preview text in <mark> elements: the preview is React-owned, and splitting
  // or merging its text nodes leaves fibers pointing at nodes React did not
  // place, which crashes the next commit that rewrites that text. Ranges live
  // outside the DOM, so a content re-render simply stops painting them. When
  // the API is absent both callbacks are no-ops, matching how find degrades.
  const annotationHighlightOwnerRef = useRef<object>({})
  const clearHighlightMarks = useCallback(() => {
    clearAnnotationHighlight(annotationHighlightOwnerRef.current)
  }, [])

  const applyHighlightMarks = useCallback((range: Range) => {
    paintAnnotationHighlight(annotationHighlightOwnerRef.current, range)
  }, [])
  useEffect(() => () => clearHighlightMarks(), [clearHighlightMarks])
  const [refreshing, setRefreshing] = useState(false)
  const [hintDismissed, setHintDismissed] = useState(() => localStorage.getItem(HINT_KEY) === '1')
  const [fullscreen, setFullscreen] = useState(false)
  // True while a remote Crew covers the local dashboard this panel lives in.
  const crewHidden = useLocalCrewHidden()
  // Shared IME latch for the full-screen preview's Tab trap: a Tab that lands
  // during an IME composition (or its post-`compositionend` window) is
  // choosing a candidate, not leaving the field, so the trap must decline it
  // instead of yanking focus and aborting the composition
  // (`useDialogFocusTrap` is the reference consumer of the same seam).
  const fsImeLatch = useDocumentImeLatch(fullscreen)
  const fileName = pathBasename(filePath) || filePath
  // Artifact + knowledge state power the header star/knowledge toggles and
  // the ⋯ menu's Snapshot entry (same query cache as the OverflowMenu's own
  // hooks, so states stay coherent).
  const knowledge = useFileKnowledgeState(filePath, reportActionError)
  const artifactState = useFileArtifactState(filePath, content, reportActionError)
  const previewRef = useRef<HTMLDivElement>(null)
  const sidePanelScrollRef = useRef<HTMLDivElement>(null)
  // Cross-remount scroll memory for the embedded (side-panel) scroll box —
  // a chat-slot switch unmounts the whole tab body. A pending line reveal is
  // an explicit scroll target that outranks memory, so its presence at first
  // ready suppresses the restore for this mount; recording continues.
  const scrollMemory = useScrollMemory(
    scrollMemoryKey,
    sidePanelScrollRef,
    content !== '',
    { suppressRestore: !!revealLine },
  )
  const fullscreenPreviewRef = useRef<HTMLDivElement>(null)
  const fullscreenBodyRef = useRef<HTMLDivElement>(null)
  const ext = extOf(filePath)
  const fileType = detectFileType(filePath)
  const isMarkdown = MD_EXTS.has(ext)
  const isRichType = RICH_FILE_TYPES.includes(fileType)
  // A text diff needs the file as text. The content-backed rich types (json,
  // csv, html, ...) are text with a viewer on top, so they diff like code; only
  // the byte-backed ones have no text to diff. A wtdiff tab opens locked to
  // diff mode, so a type that cannot diff must not reach it: it would render
  // neither the diff nor its viewer.
  const canTextDiff = !BYTE_BACKED_FILE_TYPES.has(fileType)
  useEffect(() => { if (!canTextDiff) setDiffMode(false) }, [canTextDiff])
  // The verdict can arrive AFTER mount: a restored cold tab renders with
  // `binary` undefined and gets it from the hydration read, so the initializer
  // above is not the only entry point. Neither a source diff nor an edit buffer
  // means anything for bytes that were never decoded.
  const showBinaryCard = !!binary && !BYTE_BACKED_FILE_TYPES.has(fileType)
  const wasBinaryCardRef = useRef(showBinaryCard)
  useEffect(() => {
    const wasShowing = wasBinaryCardRef.current
    wasBinaryCardRef.current = showBinaryCard
    if (!showBinaryCard) {
      // Bytes that decoded after all: a code file's default view IS the editor and
      // `canPreview` renders no Edit toggle for it, so a stale false strands the tab.
      if (wasShowing) setEditing(!readOnly && !isMarkdown && !isRichType)
      return
    }
    setDiffMode(false)
    setEditing(false)
  }, [showBinaryCard, isMarkdown, isRichType, readOnly])
  const find = useMarkdownPanelFind({ active, editing, diffMode, isMarkdown, fullscreen, content, previewRef, fullscreenPreviewRef })

  const findBar = find.findOpen ? <MarkdownPanelFindBar find={find} ime={ime} /> : null

  const lang = langFor(ext)
  const displayContent = isMarkdown ? content : wrapCode(content, ext)

  const {
    diskReadAbortRef, readFromDisk, setMissingOnDisk, partialCopy, partialCopyRef, bufferIsLastCopy,
    lastCopy, downloadLastCopy, lastCopyCopied, copyLastCopy, evictFileRead, applyDiskRead, catchUpRef, adoptDisk,
    diskTickRef, diffStartTickRef, patchOwedTick,
  } = useMarkdownPanelDisk({ filePath, content, binary, partial, liveWatch, editing, dirty, dirtyRef, active, qc, onDiskContent, onContentChange, reportActionError })


  const { diffData, diffChecking, diffQueryError, originalContent, comparedContent, gitPatch, diffUnavailable, diffViewShown, zeroDiff, zeroDiffText, diffErrorText, diffNoBaselineText } =
    useMarkdownPanelDiff({ filePath, isRichType, canTextDiff, partial, editing, dirty, diffMode, content, initialDiffMode, onDiffModeChange, setDiffMode, diskTickRef, diffStartTickRef, patchOwedTick, qc })
  // Every failure the panel owns, rendered in ONE place (both layouts mount it
  // under the header). The three background reads used to fail silently: a
  // rejected knowledge / artifact / diff query rendered as "not added" / no
  // artifact / no diff.
  //
  // askAgent decision: OFF while the editor buffer is dirty — the hand-off
  // navigates away and would discard unsaved edits — ON otherwise (nothing to
  // lose, and a failed gateway read or write is what the agent can diagnose).
  // The save failure itself is always OFF: by definition the buffer holds the
  // edits that were NOT persisted.
  const panelNotices = (saveError || actionError || knowledge.queryError || artifactState.queryError || diffQueryError) ? (
    <div className="flex flex-col gap-1.5">
      {/* No hand-off: the editor buffer holds the unsaved edits this save failed to write. */}
      <ErrorNotice message={saveError} onDismiss={() => setSaveError(null)} testId="markdown-panel-save-error" />
      <ErrorNotice message={actionError} askAgent={!dirty} onDismiss={() => setActionError(null)} testId="markdown-panel-action-error" />
      <ErrorNotice
        message={knowledge.queryError ? (errMessage(knowledge.queryError) || i18nT('components.markdownPanel.knowledge_status_failed')) : null}
        askAgent={!dirty}
        testId="markdown-panel-knowledge-error"
      />
      <ErrorNotice
        message={artifactState.queryError ? (errMessage(artifactState.queryError) || i18nT('components.markdownPanel.artifact_status_failed')) : null}
        askAgent={!dirty}
        testId="markdown-panel-artifact-error"
      />
      <ErrorNotice
        message={diffQueryError ? (errMessage(diffQueryError) || i18nT('components.markdownPanel.diff_status_failed')) : null}
        askAgent={!dirty}
        testId="markdown-panel-diff-error"
      />
    </div>
  ) : null

  const handleRefresh = useCallback(async () => {
    if (refreshing || dirty) return
    setRefreshing(true)
    try {
      if (onRefresh) { await onRefresh(filePath) }
      else {
        const disk = await readFromDisk()
        // A newer read owns the answer; this one has nothing to add or report.
        if (disk.kind === 'superseded') return
        // A click that produces nothing reads as a broken button. A file that
        // is gone produces the banner -- the verdict has its own place, with
        // the exits that keep the buffer -- so only a read that could not
        // answer at all goes through the panel's action notice.
        if (disk.kind === 'missing') { setMissingOnDisk(true); return }
        if (disk.kind === 'failed') { reportActionError(i18nT('components.markdownPanel.cannot_read_file')); return }
        // The read is async, and the dirty check above ran at click time:
        // anything typed while it was in flight made the buffer dirty, and
        // these disk bytes must not clobber that work.
        if (dirtyRef.current) return
        applyDiskRead(disk)
      }
    } finally { setRefreshing(false) }
  }, [filePath, onRefresh, refreshing, dirty, readFromDisk, applyDiskRead, reportActionError, setMissingOnDisk])

  // Discard pending edits (matches the artifact detail page's Cancel button).
  // Re-reads the file from disk into the buffer, clearing dirty. Confirms first
  // because edits are gone for good. Only markdown-ish files have a preview to
  // return to; code files stay in source mode (Cancel just discards edits).
  // Also gates Cancel's "return to preview" — there is no preview to return
  // to, and no buffer to discard.
  const canPreview = isMarkdown && !showBinaryCard
  const handleCancel = useCallback(async () => {
    if (!dirty) { if (canPreview) setEditing(false); return }
    if (!(await confirm({
      title: i18nT('components.markdownPanel.discard_unsaved_changes'),
      confirmLabel: i18nT('components.markdownPanel.discard_changes_button'),
    }))) return
    setRefreshing(true)
    try {
      if (onRefresh) { await onRefresh(filePath) }
      else {
        // Cancel means "match the disk", so the re-read moves the saved
        // baseline too (onDiskContent), the same as Refresh — otherwise the
        // stale baseline could later read a deliberate edit back to it as
        // clean and let a close discard that work.
        const disk = await readFromDisk()
        if (disk.kind === 'superseded') return
        // Cancel means "match the disk". If the disk could not be read -- or the
        // file is gone -- the buffer still holds the edits, so the dirty flag
        // MUST stay set, or a later close discards work the panel just claimed
        // was safe. A gone file is said by the banner, over the edits it now
        // offers to keep; a read that could not answer is reported. Either way
        // the user is left exactly where they were.
        if (disk.kind === 'missing') { setMissingOnDisk(true); return }
        if (disk.kind === 'failed') { reportActionError(i18nT('components.markdownPanel.cannot_read_file')); return }
        applyDiskRead(disk)
      }
      setDirty(false)
      if (canPreview) setEditing(false)
    } finally { setRefreshing(false) }
  }, [dirty, filePath, onRefresh, canPreview, confirm, readFromDisk, applyDiskRead, reportActionError, setMissingOnDisk])

  const resolveSelectionCoords = useCallback((fallbackText?: string): SelectionInfo | undefined => {
    const sel = window.getSelection()
    const root = previewRef.current ?? fullscreenPreviewRef.current
    if (!root) return undefined
    // Text selected inside a code block reads as collapsed on the document's selection
    // (see `shadowSelectionRange`). The block's DOM carries no `data-sourcepos` and no
    // offset in `root`'s text, so text that occurs twice in the file cannot be placed and
    // carries no line, rather than the line of its first occurrence.
    const inCode = sel ? shadowSelectionRange(sel, root) : null
    if (sel && inCode) {
      const raw = sel.toString()
      const anchor = raw.trim()
      const coords = findUniqueCoords(content, raw)
      const codePosition = codePassagePosition(root, inCode) ?? undefined
      return { anchor, rect: inCode.getBoundingClientRect(), range: inCode.cloneRange(), line: coords?.line, column: coords?.column, inCode: true, codePosition }
    }
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
            startOffset = anchorFromRange(root, range)?.startOffset
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

  // Where an in-progress comment lives between teardowns the toolbar cannot
  // guard (a chat-slot switch replaces the side panel wholesale). Per file,
  // per passage; see `composerDraftStoreFor`.
  const composerDraftStore = useMemo(() => composerDraftStoreFor(`mc-comment-composer-draft:${filePath}`), [filePath])

  // An unsaved comment draft makes this tab NOT clean for the navigate/close
  // guards below: a rail click must open the next file beside it rather than
  // re-target this tab under the draft's anchor, a close must ask first, and
  // so must the view switches (Edit, full screen) that unmount the box.
  const composerDraftRef = useRef(false)
  // The passage the open draft belongs to, so a confirmed discard from one of
  // the panel's own guards clears that slot alone — a draft saved for another
  // passage of this file stays where it is.
  const composerDraftPassageRef = useRef<{ anchor: string; start: number } | null>(null)
  const handleComposerDraftChange = useCallback((hasDraft: boolean, passage: { anchor: string; start: number } | null) => {
    composerDraftRef.current = hasDraft
    composerDraftPassageRef.current = hasDraft ? passage : null
  }, [])
  const clearActiveDraftSlot = useCallback(() => {
    const p = composerDraftPassageRef.current
    if (p) composerDraftStore.clear(p.anchor, p.start)
    composerDraftPassageRef.current = null
  }, [composerDraftStore])
  /** Run `proceed` unless an unsaved comment draft would be lost, in which case
   *  ask first — in the draft's own words, since "unsaved changes" would read as
   *  file edits at risk. */
  const guardDraft = useCallback(async (proceed: () => void) => {
    if (composerDraftRef.current) {
      if (!(await confirm({
        title: i18nT('components.markdownPanel.discard_unsaved_comment'),
        confirmLabel: i18nT('components.markdownPanel.discard_comment_button'),
      }))) return
      // A confirmed discard: THIS draft's persisted copy must not resurface.
      clearActiveDraftSlot()
    }
    proceed()
  }, [confirm, clearActiveDraftSlot])

  const handleComposerOpen = useCallback((text: string) => {
    composerOpenRef.current = true
    const info = resolveSelectionCoords(text)
    if (info) {
      if (info.range) applyHighlightMarks(info.range)
      else clearHighlightMarks()
      pendingAnchorRef.current = { anchor: info.anchor, line: info.line, column: info.column, startOffset: info.startOffset, inCode: info.inCode, codePosition: info.codePosition }
    } else {
      // No DOM selection to map — anchor on the text the toolbar captured.
      clearHighlightMarks()
      pendingAnchorRef.current = { anchor: text }
    }
  }, [resolveSelectionCoords, applyHighlightMarks, clearHighlightMarks])

  const handleComposerClose = useCallback(() => {
    composerOpenRef.current = false
    composerDraftRef.current = false
    pendingAnchorRef.current = null
    clearHighlightMarks()
  }, [clearHighlightMarks])

  // Returns the clipboard result so the toolbar's checkmark is truthful.
  const handleCopyAction = useCallback((text: string) => copyToClipboard(text), [])

  const selectionActions: SelectionAction[] = useMemo(() => [
    // Icon-only beside the composer (14px), icon + label on the plain row (12px,
    // matching chat's toolbar). The hint says WHAT is copied — next to a comment
    // input, "Copy" alone reads as copying the draft.
    { id: 'copy', icon: <Copy size={onSubmitComments ? 14 : 12} />, label: i18nT('components.selectionToolbar.copy'), hint: onSubmitComments ? i18nT('components.selectionToolbar.copy_selection_hint') : undefined, onClick: handleCopyAction },
  ], [onSubmitComments, handleCopyAction])

  const handleComposerSubmit = useCallback((comment: string, text: string) => {
    const info = pendingAnchorRef.current ?? { anchor: text }
    const newComment: InlineComment = { id: Math.random().toString(36).substring(2), anchor: info.anchor, text: comment, line: info.line, column: info.column, startOffset: info.startOffset, inCode: info.inCode, codePosition: info.codePosition }
    setComments(prev => [...prev, newComment])
    composerOpenRef.current = false
    composerDraftRef.current = false
    pendingAnchorRef.current = null
    clearHighlightMarks()
  }, [clearHighlightMarks])

  // Only a host that can receive comments gets the type-first box; otherwise
  // the toolbar is the plain Copy row it always was.
  // Escape / ✕ over a typed draft ask in the same words as Edit / full screen / close.
  const confirmDiscardDraft = useCallback(() => confirm({
    title: i18nT('components.markdownPanel.discard_unsaved_comment'),
    confirmLabel: i18nT('components.markdownPanel.discard_comment_button'),
    // The same prompt is raised from inside the full-screen shell (z-[9999]).
    layer: 'top',
  }), [confirm])
  // Where an in-progress comment lives between teardowns the toolbar cannot
  // guard (a chat-slot switch replaces the side panel wholesale). Per file, in
  // sessionStorage: it should survive the switch, not a browser session.

  const selectionComposer: SelectionComposer | undefined = useMemo(() => onSubmitComments ? {
    onOpen: handleComposerOpen,
    onSubmit: handleComposerSubmit,
    onClose: handleComposerClose,
    onDraftChange: handleComposerDraftChange,
    confirmDiscard: confirmDiscardDraft,
    draftStore: composerDraftStore,
  } : undefined, [onSubmitComments, handleComposerOpen, handleComposerSubmit, handleComposerClose, handleComposerDraftChange, confirmDiscardDraft, composerDraftStore])

  const removeComment = useCallback((id: string) => {
    setComments(prev => prev.filter(c => c.id !== id))
  }, [])

  const editComment = useCallback((id: string, text: string) => {
    setComments(prev => prev.map(c => c.id === id ? { ...c, text } : c))
  }, [])

  /**
   * Compose + hand off the pending comment batch, and clear it only once the
   * send reports delivery. A refused send (`false`, a rejection or a throw)
   * keeps the batch so Submit All can offer it again. Only comments still
   * exactly as sent are removed, so one added or edited while the send was
   * pending survives. Bails while the gateway is offline; the Submit All
   * button is disabled offline too — this is the behavioral backstop.
   */
  const submitInFlightRef = useRef(false)
  // Rows with an inline edit open: their typed text lives only in the row
  // until it is saved, so delivery must not remove them.
  const editingIdsRef = useRef(new Set<string>())
  const trackRowEditing = useCallback((id: string, editing: boolean) => {
    if (editing) editingIdsRef.current.add(id)
    else editingIdsRef.current.delete(id)
  }, [])
  const submitAllComments = useCallback((extraPrompt?: string) => {
    if (!connected || !onSubmitComments || comments.length === 0 || submitInFlightRef.current) return
    const sentFile = filePath
    const sent = new Map(comments.map(c => [c.id, c]))
    const unchanged = (c: InlineComment) => {
      const s = sent.get(c.id)
      return !!s && s.text === c.text && s.anchor === c.anchor && !editingIdsRef.current.has(c.id)
    }
    let verdict: void | boolean | Promise<void | boolean>
    try {
      verdict = onSubmitComments(formatCommentsMessage(filePath, comments, content, extraPrompt), comments.length)
    } catch {
      verdict = false
    }
    submitInFlightRef.current = true
    Promise.resolve(verdict)
      .then(delivered => {
        if (delivered === false) return
        // Saved here rather than left to the persistence effect: the panel
        // may have unmounted or moved to another file while the send was
        // pending, and then no effect saves the sent file.
        dropDeliveredComments(draftsRef.current, sentFile, unchanged)
        if (prevFilePathRef.current === sentFile) setComments(prev => prev.filter(c => !unchanged(c)))
      })
      .catch(() => { /* undelivered — keep the batch pending */ })
      .finally(() => { submitInFlightRef.current = false })
  }, [connected, onSubmitComments, comments, filePath, content])

  const dismissHint = useCallback(() => {
    setHintDismissed(true)
    safeSetItem(HINT_KEY, '1')
  }, [])

  useEffect(() => {
    if (editing) { pendingAnchorRef.current = null; clearHighlightMarks(); window.getSelection()?.removeAllRanges() }
  }, [editing, clearHighlightMarks])

  // Centralize persistence: fires on any comments mutation (add / remove /
  // submit-clear) and after the filePath sync-reset above. Keeping it in one
  // place avoids duplicate writes from StrictMode double-invoked updaters and
  // eliminates persistComments from callback dep arrays.
  useEffect(() => {
    // A read-only panel (wtdiff) owns no drafts, so it never writes to the
    // per-path store (#9695) — otherwise it would clobber the file tab's.
    if (readOnly) return
    persistFileComments(draftsRef.current, filePath, comments)
  }, [comments, filePath, readOnly])

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
  // The tooltip is appended to `document.body` and removed on mouseleave, which
  // never fires when the panel is hidden under it by a Crew switch.
  useEffect(() => { if (crewHidden) removeCommentTooltip() }, [crewHidden, removeCommentTooltip])
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
   * The editor surface exposes an imperative jumpToLine; the effect below
   * forces source mode first, then this one fires once the surface mounts.
   *
   * The handle is STATE, not a ref: the request usually arrives before the
   * editor exists (the effect below flips the panel into source mode, which
   * mounts it a commit later), so the reveal effect has to re-run on the
   * commit that attaches the handle. A ref attaches without a render and
   * would strand the jump. `setRevealEditor` is a stable callback ref. */
  const [revealEditor, setRevealEditor] = useState<PierreEditorHandle | null>(null)
  const lastRevealNonce = useRef<number | null>(null)
  // The host passes an inline arrow, so `onRevealConsumed` is a new function on
  // every one of its renders. Reading it through a latest-value ref keeps the
  // reveal keyed on the request itself instead of re-firing on host renders.
  const onRevealConsumedRef = useRef(onRevealConsumed)
  useEffect(() => { onRevealConsumedRef.current = onRevealConsumed }, [onRevealConsumed])
  // The nonce guard keeps this idempotent, so a repeat reveal of the same line
  // needs a new nonce to fire again.
  useEffect(() => {
    if (!revealLine || !revealTargetsSource) return
    if (lastRevealNonce.current === revealLine.nonce) return
    if (!revealEditor) return
    lastRevealNonce.current = revealLine.nonce
    revealEditor.jumpToLine(revealLine.line, revealLine.endLine)
    onRevealConsumedRef.current?.()
  }, [revealLine, revealTargetsSource, revealEditor])

  // A line only resolves against source, so leave preview/diff for it. Keyed on
  // the whole target (nonce included), so a second chip click also pulls the
  // panel back out of a view the user switched to in between.
  useEffect(() => {
    if (!revealLine || !revealTargetsSource) return
    if (readOnly) return // the diff view has no editor to reveal into (#9695)
    setEditing(true)
    setDiffMode(false)
  }, [revealLine, revealTargetsSource, readOnly])

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
        if (comment.inCode) {
          // Placed in its own block by position; its quote is never looked up in the prose.
          const r = codeQuoteRange(textRoot, comment.anchor, comment.codePosition)
          if (r) { ranges.push(r); hits.push({ range: r, comment }) }
          continue
        }
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
    // The tree observer does not see a code block's shadow root fill; this does.
    const stopWatchingCode = watchText(scrollRoot, apply)

    // Hit-test the pointer against the comment ranges (no DOM elements exist).
    const hitAt = (x: number, y: number): { range: Range; comment: InlineComment } | null => {
      const caret = (document as unknown as { caretRangeFromPoint?: (x: number, y: number) => Range | null }).caretRangeFromPoint?.(x, y)
      for (const h of activeHits) {
        try { if (caret && h.range.comparePoint(caret.startContainer, caret.startOffset) === 0) return h } catch { /* */ }
      }
      // A caret may stop at a code block's host instead of entering its shadow root,
      // so a code comment is also hit by the boxes its text is drawn in.
      for (const h of activeHits) {
        if (!h.comment.inCode) continue
        for (const box of h.range.getClientRects()) if (x >= box.left && x <= box.right && y >= box.top && y <= box.bottom) return h
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
      if (hit.comment.inCode && window.getSelection()?.toString().trim()) return
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
      stopWatchingCode()
      scrollRoot.style.cursor = ''
      scrollRoot.removeEventListener('mousemove', onMove)
      scrollRoot.removeEventListener('mouseleave', onLeave)
      scrollRoot.removeEventListener('click', onClick)
      removeCommentTooltip()
    }
  }, [comments, displayContent, editing, fullscreen, removeCommentTooltip, flashCommentRow])

  /** Bumped by every local edit (handleChange). A save clears `dirty` only if
   *  no edit landed while its write was in flight: the file holds `content` as
   *  of the keypress, and a keystroke typed before the write round-trips is not
   *  in it. Marking that buffer clean misfiles the keystroke -- and, with the
   *  catch-up latch left `due` by the first edit, the clean flip would start a
   *  read that puts the just-written text back over it. */
  const editGenRef = useRef(0)
  const handleSave = useCallback(async () => {
    setSaving(true); setSaveError(null)
    const editGen = editGenRef.current
    try {
      await onSave(filePath, content)
      // The disk now holds `content`, so whatever the shared `['file-read',
      // path]` entry holds predates it: dropped, not rewritten. This panel puts
      // no body into the shared cache -- it only reads the disk -- and a
      // consumer opening the path inside the window refetches. The post-save
      // catch-up read is a fresh fetch either way (see readFromDisk).
      evictFileRead()
      if (editGenRef.current === editGen) setDirty(false)
      qc.invalidateQueries({ queryKey: ['file-diff', filePath] })
    }
    catch (err) { setSaveError(err instanceof Error ? err.message : i18nT('components.markdownPanel.save_failed')) }
    finally { setSaving(false) }
  }, [filePath, content, onSave, qc, evictFileRead])

  const handleSaveRef = useRef(handleSave)
  useEffect(() => { handleSaveRef.current = handleSave }, [handleSave])

  const splitRowRef = useRef<HTMLDivElement>(null)
  const [railNarrow, setRailNarrow] = useState(false)
  useEffect(() => {
    const el = splitRowRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(([e]) => setRailNarrow(e.contentRect.width < RAIL_SPLIT_MIN_W))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const guardedClose = useCallback(async () => {
    // A buffer whose file is gone from disk is the only copy there is, edited
    // or not, so the file being gone decides the prompt: "Discard unsaved
    // changes?" would imply a saved version survives the close, and none does.
    // Same stakes as unsaved edits, same prompt shape, same precedence over a
    // comment draft. A live-watched tab asks the disk first, dirty or not:
    // `api_file_watch` polls on silently through a FileNotFoundError, so a file
    // deleted while its tab was on screen has told the panel nothing (and a
    // dirty tab reads nothing on its own), and the close is the last moment
    // the copy can still be kept. A PARTIAL buffer (a truncated or redacted
    // read) is guarded exactly the same -- it is the most of the file that
    // survives -- and only the prompt's words change: the partial verdict is a
    // guess from the body's shape, and a guess may add a caveat but must never
    // lift the guard, or the sole copy of a whole file that happens to contain
    // the redaction tag would close unasked.
    let gone = lastCopy
    let unverified = false
    if (!gone && liveWatch) {
      // Quiet: whatever this read cannot answer, the dialog below says, so a
      // notice as well would tell it twice and outlive a Cancel.
      const verdict = await adoptDisk({ quietFailure: true })
      // The user may have typed while the read was out (which also aborts it):
      // `dirty` captured above is stale, so nothing below trusts it, and an
      // overtaken read closes nothing -- the next Escape asks again. A read
      // that FAILED could not say whether the file is there: that is a
      // question for the user, not a silent refusal (a gateway that is down
      // would otherwise make the tab impossible to close).
      if (verdict === 'superseded') return
      // ...and only when there is a copy the answer could protect: an empty
      // buffer, the seeded "file not found" sentence or a binary tab's empty
      // text buffer would be told it "may hold the only remaining version" of
      // nothing.
      unverified = verdict === 'failed' && bufferIsLastCopy()
      gone = verdict === 'missing' && bufferIsLastCopy()
    }
    const dirtyNow = dirtyRef.current
    const partialNow = partialCopyRef.current
    const name = downloadFileName(filePath)
    // The rescue rides in the dialog: the body names Download and carries it,
    // so keeping the copy is one click there, not cancel-then-hunt.
    const rescue = (text: string) => (
      <span className="flex flex-wrap items-center gap-2">
        <span>{text}</span>
        <button type="button" data-testid="markdown-panel-dialog-download" className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium border border-border bg-transparent text-text hover:bg-bg-hover cursor-pointer transition-colors shrink-0" onClick={downloadLastCopy}>{i18nT('components.markdownPanel.download')}</button>
      </span>
    )
    if (gone) {
      const body = partialNow
        ? (dirtyNow ? 'components.markdownPanel.close_partial_copy_dirty_body' : 'components.markdownPanel.close_partial_copy_body')
        : (dirtyNow ? 'components.markdownPanel.close_last_copy_dirty_body' : 'components.markdownPanel.close_last_copy_body')
      if (!(await confirm({
        title: i18nT('components.markdownPanel.close_last_copy'),
        body: rescue(i18nT(body, { name })),
        confirmLabel: i18nT('components.markdownPanel.close_last_copy_button'),
      }))) return
    } else if (unverified) {
      if (!(await confirm({
        title: i18nT('components.markdownPanel.close_unverified'),
        body: rescue(i18nT(dirtyNow ? 'components.markdownPanel.close_unverified_dirty_body' : 'components.markdownPanel.close_unverified_body', { name })),
        confirmLabel: i18nT('components.markdownPanel.close_unverified_button'),
      }))) return
    } else if (dirtyNow) {
      // An open comment draft is unsaved work too: closing discards it. The
      // prompt names what is at risk — file edits take precedence when both are.
      if (!(await confirm({
        title: i18nT('components.markdownPanel.discard_unsaved_changes'),
        confirmLabel: i18nT('components.markdownPanel.discard_changes_button'),
      }))) return
    } else if (composerDraftRef.current) {
      if (!(await confirm({
        title: i18nT('components.markdownPanel.discard_unsaved_comment'),
        confirmLabel: i18nT('components.markdownPanel.discard_comment_button'),
      }))) return
      clearActiveDraftSlot()
    }
    onClose()
  }, [lastCopy, liveWatch, filePath, adoptDisk, bufferIsLastCopy, downloadLastCopy, onClose, confirm, clearActiveDraftSlot, partialCopyRef])
  // Mirrors `lastCopy` for the navigate predicate below, which the host asks
  // after its own await.
  const lastCopyRef = useRef(lastCopy)
  lastCopyRef.current = lastCopy
  const guardedNavigate = useCallback(async (nav: (stillClean: () => boolean) => void) => {
    // Navigation never destroys this buffer, so there is nothing to confirm: a
    // dirty tab is left exactly as it is and the new file opens as its own tab.
    // Only a CLEAN tab is re-targeted in place. Closing still asks, because
    // closing really does discard.
    //
    // A tab with an open comment draft is not clean either: re-targeting it
    // would leave the draft (and the anchor it was resolved against) hanging
    // over a different file, so the next file opens beside it instead. Nor is
    // a tab whose buffer is the last copy of a file that is gone: re-targeting
    // it in place would discard that copy as surely as closing it.
    //
    // Whether it IS such a copy is asked of the disk first, the way the close
    // asks: a file deleted while its tab was on screen has told the panel
    // nothing (the watch polls on silently), so a clean tab's stored verdict
    // can be stale, and a re-target on it would discard the only copy with no
    // word. The read is the close path's own -- one fresh GET, the missing
    // verdict latched, the buffer left alone -- and a read that could not
    // answer (failed, or overtaken by typing) spares the tab too: opening
    // beside costs nothing, discarding on a guess costs the copy.
    //
    // The predicate is what decides between those two outcomes, and the caller
    // re-asks it after its file read: the user can start typing during the read,
    // so an answer computed here would already be stale.
    let unsure = false
    if (liveWatch && !dirtyRef.current && !composerDraftRef.current && !lastCopyRef.current) {
      const verdict = await adoptDisk({ quietFailure: true })
      unsure = verdict !== 'ok' && verdict !== 'missing'
      if (verdict === 'missing' && bufferIsLastCopy()) unsure = true
    }
    nav(() => !unsure && !dirtyRef.current && !composerDraftRef.current && !lastCopyRef.current)
  }, [liveWatch, adoptDisk, bufferIsLastCopy])

  // Expose the guarded close so an external control (e.g. the Files-tab inline
  // preview's "Back to files" bar) routes through the same dirty confirmation
  // instead of unmounting the editor and silently dropping unsaved edits.
  // `requestClose` deliberately swallows guardedClose's promise: the handle's
  // contract is fire-and-forget (() => void), and an imperative caller that
  // received the promise could `act()` on it un-awaited and wedge React's act
  // scope in tests — the close outcome is observable via onClose only. The
  // same for `requestNavigate`: its outcome is the `nav` call.
  useImperativeHandle(ref, () => ({ requestClose: () => { void guardedClose() }, requestNavigate: (nav) => { void guardedNavigate(nav) } }), [guardedClose, guardedNavigate])

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      // A background tab is mounted but invisible, so neither key may reach it:
      // Escape would close — or raise a discard prompt for — a document the user
      // cannot see, and Cmd+S would let a hidden dirty editor answer a save the
      // user aimed at the tab in front of them. The whole local dashboard is
      // equally invisible while a remote Crew is showing.
      if (!active || crewHidden) return
      // While the discard-confirm dialog is open, the keyboard belongs to it.
      // Escape dismisses it via the dialog's own handler (re-running the guard
      // here would re-open the dialog the same keystroke just closed), and the
      // save shortcut must not fire — a mid-dialog Cmd+S would persist the very
      // draft the user is about to confirm discarding.
      if (confirmOpen) return
      // An open annotation box owns Escape: the toolbar closes it (and hands the
      // selection back) on its own; the panel must not ALSO close or prompt.
      if (e.key === 'Escape') { if (composerOpenRef.current) return; if (fullscreen) setFullscreen(false); else guardedClose() }
      // Own the save chord whenever the editor is active, not only when dirty:
      // the editor-local capture handler in PierreEditorImpl exists only after
      // its lazy chunk resolves and only sees keydowns targeting its own
      // subtree, so during the load window this document-level handler is the
      // one place the chord can be claimed. Match case-insensitively so
      // Caps Lock / Shift+Cmd+S also count. preventDefault stops AppKit's
      // default (selecting the word under the cursor); only issue the write
      // when dirty so a clean buffer does not trigger a redundant save.
      // Bail if PierreEditorImpl's capture handler already claimed the chord
      // (defaultPrevented) so the save fires once, not twice.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's' && editing && !e.defaultPrevented) {
        e.preventDefault()
        if (dirty) handleSaveRef.current()
      }
    }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [active, guardedClose, editing, dirty, fullscreen, confirmOpen, crewHidden])

  // The first edit of a clean buffer fences the disk. A read still in flight
  // describes a buffer the user has now typed into: if the user saves before it
  // lands, `dirty` is clear again when it does and it would restamp the
  // just-saved text with the older revision -- and a read that Refresh had
  // already overtaken would otherwise leave the catch-up latch at `reading`
  // with no landing left to move it. Withdrawing the read makes it
  // `superseded`, which applies nothing and says nothing; `due` makes the next
  // clean moment -- save, undo or discard -- start a post-edit read. Only the
  // first edit: while the buffer is dirty the one read that can be in flight
  // is Cancel's, and Cancel means match the disk. Synchronous with the edit,
  // before the buffer moves, so no landing can slip between the two.
  const handleChange = useCallback((v: string) => {
    editGenRef.current++
    if (!dirtyRef.current) { diskReadAbortRef.current?.abort(); catchUpRef.current = 'due' }
    onContentChange(v); setDirty(true)
  }, [onContentChange, diskReadAbortRef, catchUpRef])

  // Lock body scroll when fullscreen overlay is open
  useEffect(() => {
    if (!fullscreen) return
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [fullscreen])

  const editorToolbarButtons = (<>
    {!isRichType && !showBinaryCard && !readOnly && (
      <button className={`p-1.5 rounded-md border cursor-pointer ${diffMode ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={toggleDiffMode} title={i18nT('components.markdownPanel.toggle_diff_view')} aria-label={i18nT('components.markdownPanel.toggle_diff_view')}><FileDiff size={14} /></button>
    )}
    {!isRichType && editing && (
      <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${wordWrap ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setWordWrap(!wordWrap)} title={i18nT('components.markdownPanel.toggle_word_wrap')} aria-label={i18nT('components.markdownPanel.toggle_word_wrap')}><WrapText size={14} /></button>
    )}
    {!isRichType && editing && (
      <button className={`p-1.5 rounded-md border cursor-pointer transition-all ${lineNums ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => setLineNums(!lineNums)} title={i18nT('components.markdownPanel.toggle_line_numbers')} aria-label={i18nT('components.markdownPanel.toggle_line_numbers')}><Hash size={14} /></button>
    )}
    {canPreview && !readOnly && (
      <button className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all ${editing ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text hover:border-border-strong'}`} onClick={() => { void guardDraft(() => setEditing(!editing)) }}>{editing ? i18nT('components.markdownPanel.preview') : i18nT('components.markdownPanel.edit')}</button>
    )}
    {readOnly && (
      <span className="px-2 py-1 text-[12px] font-medium text-muted select-none" aria-label={i18nT('components.markdownPanel.read_only')}>{i18nT('components.markdownPanel.read_only')}</span>
    )}
    {!isRichType && editing && (
      <button className={`px-2 py-1 rounded-md text-[12px] font-medium border transition-all disabled:opacity-40 ${dirty ? 'border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover' : 'border-border text-muted cursor-default'}`} disabled={saving || !dirty} onClick={handleSave}>{saving ? i18nT('components.markdownPanel.saving') : i18nT('components.markdownPanel.save')}</button>
    )}
  </>)

  // Diff-mode +N/-N stats over the same original/modified pair the diff view shows.
  const diffStats = useMemo(
    () => (gitPatch != null ? countDiffStats(gitPatch) : countLines(originalContent, comparedContent)),
    [gitPatch, originalContent, comparedContent],
  )
  // A rich-type tab's diff toggle lives in the ⋯ menu (the header holds no
  // diff button for it), so while its diff is on, the header carries this chip
  // to show the mode and switch back to the viewer in one click.
  const canToggleRichDiff = isRichType && canTextDiff && !showBinaryCard && !readOnly
  const richDiffChip = canToggleRichDiff && diffMode ? (
    <button
      type="button"
      className="inline-flex items-center gap-1 h-[22px] px-2 rounded-full text-[11px] font-medium text-accent bg-accent-subtle hover:text-text border-none cursor-pointer shrink-0"
      onClick={toggleDiffMode}
      title={i18nT('components.markdownPanel.close_diff_view')}
      aria-label={i18nT('components.markdownPanel.close_diff_view')}
      data-testid="markdown-panel-diff-chip"
    >{i18nT('components.markdownPanel.diff_view')} <X size={12} aria-hidden="true" /></button>
  ) : null
  // Snapshot (⋯ menu): capture current content as a new artifact version;
  // unsaved edits are persisted first so the snapshot reflects the screen.
  const handleSnapshot = useCallback(async () => {
    if (showBinaryCard) {
      reportActionError(i18nT('components.markdownPanel.binary_file_cannot_be_added'))
      return
    }
    if (dirty) await handleSave()
    artifactState.snapshot()
  }, [showBinaryCard, dirty, handleSave, artifactState, reportActionError])

  return (
    <>
    <DetailPanel
      embedded={embedded}
      // Mark this panel as the current Escape owner while its own document
      // handler (above) will answer Escape by closing: embedded, visible
      // (active + not hidden behind a remote Crew), and not already deferring
      // to a surface that carries its own role="dialog" (the discard confirm,
      // the fullscreen overlay). The composer's Escape-to-stop gate reads this
      // marker and yields, so one Escape closes the preview without also
      // stopping the running turn (#22315).
      escapeOwner={!!embedded && active && !crewHidden && !confirmOpen && !fullscreen}
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
           knowledge), Edit/Preview toggle, diff toggle, and the ⋯
           overflow on the right. In source mode a second row pops down
           (grid-rows transition — compositor-friendly in Electron, unlike
           height auto) with the editor options and Save / Cancel so the
           main bar never crowds. */
        <div className="shrink-0 border-b border-border">
          <div className="relative flex items-center gap-2 h-[38px] px-3">
            <FileText size={14} className="text-muted shrink-0" />
            <FileHeaderBreadcrumb filePath={filePath} onFolderOpen={onFolderOpen} />
            {diffMode && !diffUnavailable && (diffStats.added > 0 || diffStats.removed > 0) && (
              <span className="text-[11px] font-mono font-semibold shrink-0">
                {diffStats.added > 0 && <span className="text-ok">+{diffStats.added}</span>}
                {diffStats.removed > 0 && <span className="text-danger ml-1.5">-{diffStats.removed}</span>}
              </span>
            )}
            {richDiffChip}
            <span className="flex-1 min-w-[8px]" />
            {!showBinaryCard && <FileArtifactActionButton state={artifactState} />}
            {(() => {
              const kExt = '.' + (filePath.split('.').pop() || '').toLowerCase()
              const canK = knowledge.formats && knowledge.formats.includes(kExt)
              if (!canK) return null
              return <KnowledgeToggleIconButton state={knowledge} />
            })()}
            {canPreview && !readOnly && (
              <button
                className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer transition-colors shrink-0"
                onClick={() => { void guardDraft(() => setEditing(!editing)) }}
                aria-pressed={editing}
              >{editing ? i18nT('components.markdownPanel.preview') : i18nT('components.markdownPanel.edit')}</button>
            )}
            {readOnly && (
              <span className="px-2.5 h-[26px] inline-flex items-center text-[11.5px] font-medium text-muted select-none shrink-0" aria-label={i18nT('components.markdownPanel.read_only')}>{i18nT('components.markdownPanel.read_only')}</span>
            )}
            {!isRichType && !showBinaryCard && !readOnly && (
              <button className={barIconBtn(diffMode)} onClick={toggleDiffMode} title={i18nT('components.markdownPanel.toggle_diff_view')} aria-label={i18nT('components.markdownPanel.toggle_diff_view')} aria-pressed={diffMode}><FileDiff size={14} /></button>
            )}
            {onRailToggle && (
              <button
                className={barIconBtn(!!railOpen)}
                onClick={onRailToggle}
                title={i18nT('components.markdownPanel.toggle_file_browser')}
                aria-label={i18nT('components.markdownPanel.toggle_file_browser')}
                aria-pressed={!!railOpen}
              ><Folders size={14} /></button>
            )}
            <OverflowMenu filePath={filePath} content={content} onError={reportActionError} onDownload={lastCopy ? downloadLastCopy : undefined}
              onRefresh={handleRefresh} refreshDisabled={refreshing || dirty} refreshTitle={dirty ? i18nT('components.markdownPanel.save_or_discard_changes_first') : i18nT('components.markdownPanel.refresh_file_re_read_from_disk')}
              onFullscreen={() => { void guardDraft(() => setFullscreen(f => !f)) }} fullscreen={fullscreen}
              onSnapshot={artifactState.existing && !showBinaryCard ? handleSnapshot : undefined} snapshotting={artifactState.snapshotting}
              wordWrap={wordWrap} onToggleWordWrap={() => setWordWrap(!wordWrap)}
              lineNums={lineNums} onToggleLineNums={() => setLineNums(!lineNums)}
              collapseUnchanged={collapseUnchanged} onToggleCollapseUnchanged={() => setCollapseUnchanged(!collapseUnchanged)}
              diffSplit={diffSplit} onToggleDiffSplit={diffMode ? () => setDiffSplit(!diffSplit) : undefined}
              diffView={diffMode} onToggleDiffView={canToggleRichDiff ? toggleDiffMode : undefined}
            />
          </div>
        </div>
      }
    >
      {panelNotices}
      {/* Comment hint for markdown files */}
      {isMarkdown && !editing && onSubmitComments && !hintDismissed && (
        <CommentHint onDismiss={dismissHint} />
      )}
      {/* Code / editor / diff views run flush (edge-to-edge) against the
          panel — markdown preview keeps a left reading indent. The inset is
          horizontal only: this row also holds the browser rail (grip + tree)
          to the RIGHT of the content, so vertical padding here would push the
          rail down in preview and leave it level everywhere else. Prose
          breathing room at the top belongs inside the scroll box. */}
      <div ref={splitRowRef} className={`relative flex-1 overflow-hidden -mx-5 -my-4 flex ${isMarkdown && !editing && !diffMode ? 'pl-4 pr-0' : ''}`}>
        {!fullscreen && <div data-mc-mdpanel className="relative flex-1 min-w-0 min-h-0 flex flex-col">
          {findBar}
          {/* Unsaved-changes banner: slides in from the top of the PREVIEW
              column only (the header keeps its height and the browser rail
              never moves). 40px + the negative right margin over the grip
              keep its divider continuous with the rail header's. */}
          <div className={`grid transition-[grid-template-rows] duration-200 ease-out shrink-0 ${railOpen ? '-mr-1' : ''}`} style={{ gridTemplateRows: dirty ? '1fr' : '0fr' }} aria-hidden={!dirty}>
            <div className="overflow-hidden min-h-0">
              <div className="flex items-center gap-2 h-[40px] px-3 bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border-b border-[color-mix(in_srgb,var(--warn)_30%,transparent)]">
                <TriangleAlert size={13} className="text-warn shrink-0" />
                <span className="text-[12px] text-text truncate">{i18nT('components.markdownPanel.unsaved_changes')}</span>
                <span className="flex-1" />
                <button className="px-2.5 h-[26px] rounded-md text-[11.5px] font-medium text-muted hover:text-text border border-border bg-transparent cursor-pointer transition-colors disabled:opacity-40 shrink-0" onClick={handleCancel} disabled={refreshing} title={i18nT('components.markdownPanel.cancel_discard_unsaved_edits')} tabIndex={dirty ? 0 : -1}>{i18nT('components.markdownPanel.cancel')}</button>
                <button className="px-3 h-[26px] rounded-md text-[11.5px] font-semibold border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover transition-all disabled:opacity-40 shrink-0" disabled={saving} onClick={handleSave} tabIndex={dirty ? 0 : -1}>{saving ? i18nT('components.markdownPanel.saving') : i18nT('components.markdownPanel.save')}</button>
              </div>
            </div>
          </div>
          {lastCopy && <MissingFileBanner onDownload={downloadLastCopy} onCopy={() => { void copyLastCopy() }} copied={lastCopyCopied} partial={partialCopy} />}
          {/* In markdown preview the scroll box runs flush to the panel's right
              border so the outline rail sits on that edge; pr-6 keeps the text
              clear of the ticks. */}
          <div ref={sidePanelScrollRef} onScroll={scrollMemory.onScroll} className={`flex-1 min-h-0 overflow-auto ${isMarkdown && !editing ? 'pr-6' : ''}`}>
            {showBinaryCard && <BinaryFileCard filePath={filePath} />}
            {!showBinaryCard && zeroDiff && <ZeroDiffNotice message={zeroDiffText} onExitDiff={readOnly ? undefined : toggleDiffMode} />}
            {!showBinaryCard && diffErrorText && !editing && (
              <div className="p-3"><ErrorNotice message={diffErrorText} askAgent={!dirty} testId="markdown-panel-diff-compute-error" /></div>
            )}
            {!showBinaryCard && diffNoBaselineText && !editing && <ZeroDiffNotice message={diffNoBaselineText} onExitDiff={readOnly ? undefined : toggleDiffMode} />}
            {/* A background refetch keeps the cached diff on screen; only a first fetch blanks it. */}
            {!showBinaryCard && !zeroDiff && !diffUnavailable && !(diffChecking && diffData == null) && canTextDiff && (
              <DiffViewBlock flush sideBySide={diffSplit} diffMode={diffViewShown} fileName={fileName} originalContent={originalContent} content={comparedContent} gitPatch={gitPatch} lineNums={lineNums} wordWrap={wordWrap} collapseUnchanged={collapseUnchanged} />
            )}
            {!showBinaryCard && !zeroDiff && (!diffUnavailable || editing) && !diffViewShown && <ContentRenderer flush isRichType={isRichType} fileType={fileType} filePath={filePath} content={content} editing={editing} lang={lang} lineNums={lineNums} wordWrap={wordWrap} onChange={handleChange} onSave={handleSave}
              diffBase={diffMode && editing ? (originalContent || null) : undefined} diffSplit={diffSplit} diffExpandUnchanged={!collapseUnchanged}
              previewRef={previewRef} displayContent={displayContent} isMarkdown={isMarkdown} markdownClassName="msg-content text-sm leading-relaxed" editorRef={setRevealEditor} />}
          </div>
          {isMarkdown && !editing && <MarkdownOutlineRail containerRef={sidePanelScrollRef} />}
        </div>}
        {/* The rail is a flex SIBLING only while the panel is wide enough to
            seat both. Below `RAIL_SPLIT_MIN_W` a 240-300px rail would leave the
            editor a few dozen pixels, so it floats over the content instead --
            same tree, same state, just taken out of flow. */}
        {!fullscreen && railOpen && browserRail && (
          railNarrow
            ? <div className="absolute inset-y-0 right-0 z-20 flex max-w-full bg-bg shadow-[-8px_0_16px_-8px_rgba(0,0,0,0.45)]">{browserRail}</div>
            : browserRail
        )}
      </div>
      {/* Keyed by file: a re-target of this tab (e.g. a slot switch) must not
          carry an open composer — and the anchor it resolved against the OLD
          file — over to the new one. Remount drops it and fires onClose.
          Text selected in a code block is anchored by the preview only: the diff
          view has no preview root for `resolveSelectionCoords` to read. */}
      {!fullscreen && !editing && <SelectionToolbar key={filePath} containerRef={sidePanelScrollRef} actions={selectionActions} composer={selectionComposer} suspended={!active} codeSelection={!diffViewShown} />}
      {!fullscreen && <CommentOverlayBlock onSubmitComments={onSubmitComments} comments={comments} editComment={editComment} removeComment={removeComment} submitAllComments={submitAllComments} connected={connected} onEditingChange={trackRowEditing} draftKey={instructionDraftKey} />}
    </DetailPanel>
    {fullscreen && <OverlayPortal>{
      // The onKeyDown here implements a focus trap for the modal dialog; a
      // role="dialog"/aria-modal container legitimately owns keyboard handling.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
      <div className="fixed inset-0 z-[9999] bg-bg flex flex-col p-safe" role="dialog" aria-modal="true" aria-label={i18nT('components.markdownPanel.full_screen_file_preview')}
        ref={el => { if (el && !el.dataset.focused) { el.dataset.focused = '1'; const first = el.querySelector<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])'); first?.focus() } }}
        onKeyDown={e => {
          if (e.key !== 'Tab') return
          const focusable = e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),textarea,input,a[href],select,[tabindex]:not([tabindex="-1"])')
          if (focusable.length === 0) return
          const first = focusable[0], last = focusable[focusable.length - 1]
          const wrapsBackward = e.shiftKey && document.activeElement === first
          const wrapsForward = !e.shiftKey && document.activeElement === last
          // A mid-dialog Tab is the browser's to move, and not the trap's to
          // claim. A boundary Tab the IME owns must not cycle focus — the user
          // is choosing a candidate, not leaving the field — so `claimKey`
          // (native-event contract in useImeGuard.ts) runs before the
          // preventDefault() and focus move.
          if (!wrapsBackward && !wrapsForward) return
          // `claimSyntheticKey` owns BOTH halves of a decline: the native
          // event (which document/window listeners see) and React's own
          // propagation flag (which it walks when dispatching to component
          // ancestors), so a declined Tab cannot trigger an ancestor's
          // keyboard handling.
          if (!fsImeLatch.claimSyntheticKey(e)) return
          e.preventDefault()
          ;(wrapsBackward ? last : first).focus()
        }}>

        {/* Header — pl-20 clears macOS traffic-light buttons */}
        <div className="flex items-center justify-between pl-20 pr-6 h-12 shrink-0 border-b border-border">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-base font-semibold text-text-strong truncate">{fileName}</span>
            {richDiffChip}
          </div>
          <div className="flex items-center gap-1.5">
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all disabled:opacity-40" onClick={handleRefresh} disabled={refreshing || dirty} title={dirty ? i18nT('components.markdownPanel.save_or_discard_changes_first') : i18nT('components.markdownPanel.refresh_file')} aria-label={i18nT('components.markdownPanel.refresh_file')}><RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} /></button>
            {!showBinaryCard && <FileArtifactActionButton state={artifactState} />}
            {(() => {
              const ext = '.' + (filePath.split('.').pop() || '').toLowerCase()
              const canK = knowledge.formats && knowledge.formats.includes(ext)
              if (!canK) return null
              return <KnowledgeToggleIconButton state={knowledge} />
            })()}
            {editorToolbarButtons}
            <OverflowMenu filePath={filePath} content={content} onError={reportActionError} onDownload={lastCopy ? downloadLastCopy : undefined}
              diffView={diffMode} onToggleDiffView={canToggleRichDiff ? toggleDiffMode : undefined} />
            <button className="p-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all" onClick={() => { void guardDraft(() => setFullscreen(false)) }} title={i18nT('components.markdownPanel.exit_full_screen_esc')} aria-label={i18nT('components.markdownPanel.exit_full_screen')}><Minimize2 size={14} /></button>
          </div>
        </div>
        {panelNotices && <div className="px-16">{panelNotices}</div>}
        {isMarkdown && !editing && onSubmitComments && !hintDismissed && <div className="px-16"><CommentHint onDismiss={dismissHint} /></div>}
        {lastCopy && <div className="px-4 sm:px-16"><MissingFileBanner onDownload={downloadLastCopy} onCopy={() => { void copyLastCopy() }} copied={lastCopyCopied} partial={partialCopy} /></div>}
        {/* Body */}
        <div data-mc-mdpanel className="relative flex-1 overflow-hidden min-h-0">
          {findBar}
          <div ref={fullscreenBodyRef} className="h-full overflow-auto px-16 py-4">
            {showBinaryCard && <BinaryFileCard filePath={filePath} />}
            {!showBinaryCard && zeroDiff && <ZeroDiffNotice message={zeroDiffText} onExitDiff={readOnly ? undefined : toggleDiffMode} />}
            {!showBinaryCard && diffErrorText && !editing && (
              <div className="p-3"><ErrorNotice message={diffErrorText} askAgent={!dirty} testId="markdown-panel-diff-compute-error-fullscreen" /></div>
            )}
            {!showBinaryCard && diffNoBaselineText && !editing && <ZeroDiffNotice message={diffNoBaselineText} onExitDiff={readOnly ? undefined : toggleDiffMode} />}
            {!showBinaryCard && !zeroDiff && !diffUnavailable && !(diffChecking && diffData == null) && canTextDiff && <DiffViewBlock sideBySide={diffSplit} diffMode={diffViewShown} fileName={fileName} originalContent={originalContent} content={comparedContent} gitPatch={gitPatch} lineNums={lineNums} wordWrap={wordWrap} collapseUnchanged={collapseUnchanged} />}
            {!showBinaryCard && !zeroDiff && (!diffUnavailable || editing) && !diffViewShown && <ContentRenderer isRichType={isRichType} fileType={fileType} filePath={filePath} content={content} editing={editing} lang={lang} lineNums={lineNums} wordWrap={wordWrap} onChange={handleChange} onSave={handleSave}
              diffBase={diffMode && editing ? (originalContent || null) : undefined} diffSplit={diffSplit} diffExpandUnchanged={!collapseUnchanged}
              previewRef={fullscreenPreviewRef} displayContent={displayContent} isMarkdown={isMarkdown} previewStyle={mdPreviewStyle} editorRef={setRevealEditor} />}
          </div>
          {isMarkdown && !editing && <MarkdownOutlineRail containerRef={fullscreenBodyRef} />}
        </div>
        {!editing && <SelectionToolbar key={filePath} containerRef={fullscreenBodyRef} actions={selectionActions} composer={selectionComposer} suspended={!active} codeSelection={!diffViewShown} />}
        <CommentOverlayBlock onSubmitComments={onSubmitComments} comments={comments} editComment={editComment} removeComment={removeComment} submitAllComments={submitAllComments} connected={connected} onEditingChange={trackRowEditing} draftKey={instructionDraftKey} />
        {/* Footer */}
        <Clickable className="shrink-0 flex items-center px-3 h-6 text-[11px] text-muted font-mono truncate cursor-pointer hover:text-text transition-colors" title={i18nT('components.markdownPanel.click_to_copy_path')} onClick={() => copyToClipboard(filePath)}>{filePath}</Clickable>
      </div>
    }</OverlayPortal>}
    {confirmDialog}
    </>
  )
}))
