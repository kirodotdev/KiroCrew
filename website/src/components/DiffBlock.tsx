import { memo, useCallback, useState, useMemo, useEffect, useRef } from 'react'
import { motion } from 'framer-motion'
import { Copy, Check, ChevronUp, Columns2, Rows2, X } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { fileReadUrl } from '../utils/fileReadUrl'
import { isSafePath } from '../utils/safePath'
import { PierrePatch, type PatchReviewHooks, type ReviewFileRef } from '../pierre'
import { PIERRE_COMPACT_HEADER_CSS, PIERRE_WRAP_NO_HSCROLL_CSS, PIERRE_SEPARATOR_BG_CSS } from '../pierre/config'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { usePlainDiff } from '../hooks/usePlainDiff'
import { useContextSlotId, useReviewSurface } from '../providers/SlotContext'
import { addReviewComment, getReviewComment, removeReviewComment, useReviewComments } from '../store/reviewComments'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/** Extract the target file path from unified-diff header lines.
 *
 * Tries several formats in order of specificity:
 *   1. `+++ b/<path>` — git's unified diff (preferred — explicitly the new side)
 *   2. `+++ <path>`   — plain unified diff without git's a/ b/ prefix
 *   3. `--- a/<path>` — git's old-side header (used when only the - side is named)
 *   4. `--- <path>`   — plain unified-diff old-side header
 *   5. `diff --git a/... b/<path>` — git's diff command header (greedy)
 *
 * Skips conventional placeholder paths like `/dev/null` (used for adds /
 * deletes) and bare `-`/`+` markers.
 *
 * `prefixStripped` reports whether the winning candidate had a git `a/` / `b/`
 * prefix removed. That matters because git JOINS the prefix onto the path,
 * collapsing an absolute path's leading slash: `git diff --no-index /tmp/x /tmp/y`
 * emits `+++ b/tmp/y`, so the stripped remainder `tmp/y` is a rootless spelling
 * of `/tmp/y` — syntactically indistinguishable from a genuine repo-relative
 * path. The caller resolves the ambiguity with an existence probe (see
 * `ROOTLESS_ABS_RE` below); this function only preserves the signal.
 *
 * Only lines OUTSIDE hunks are considered: `@@` starts a hunk, and within one
 * a `--- ` / `+++ ` row is content (a deleted `-- ` / added `++ ` line), not a
 * header. Scanning stops at the first hunk since git headers precede hunks.
 */
export function extractFilePath(code: string): { path: string; prefixStripped: boolean } | null {
  let plusFallback: string | null = null
  let minusGit: string | null = null
  let minusPlain: string | null = null
  let gitFallback: string | null = null
  const skip = (p: string) => !p || p === '/dev/null' || p === '-' || p === '+'
  for (const line of code.split('\n')) {
    if (line.startsWith('@@')) break
    // Header paths terminate at a TAB (the unified-diff timestamp separator)
    // or end of line — never at a space, which is a legal path character.
    // Both difflib (the backend's diff generator) and git emit the path as
    // the whole remainder of the line, so a lazy match cut at the first
    // space would resolve "/work/report final.md" to the SIBLING file
    // "/work/report" — and the open-in-panel affordance would read and save
    // the wrong file. A trailing space-separated timestamp from some other
    // tool stays attached instead; the existence probe then fails and the
    // affordance is simply not offered — fail-safe in the harmless direction.
    const plusGitMatch = /^\+\+\+ b\/(.+?)(?:\t|$)/.exec(line)
    if (plusGitMatch && !skip(plusGitMatch[1])) return { path: plusGitMatch[1], prefixStripped: true }
    const plusPlainMatch = /^\+\+\+ ([^\s].*?)(?:\t|$)/.exec(line)
    if (plusPlainMatch && !skip(plusPlainMatch[1]) && !plusFallback) {
      plusFallback = plusPlainMatch[1]
    }
    const minusGitMatch = /^--- a\/(.+?)(?:\t|$)/.exec(line)
    if (minusGitMatch && !skip(minusGitMatch[1]) && !minusGit) {
      minusGit = minusGitMatch[1]
    }
    const minusPlainMatch = /^--- ([^\s].*?)(?:\t|$)/.exec(line)
    if (minusPlainMatch && !skip(minusPlainMatch[1]) && !minusPlain) {
      minusPlain = minusPlainMatch[1]
    }
    if (!gitFallback) {
      const gitMatch = /^diff --git a\/.+ b\/(.+)/.exec(line)
      if (gitMatch) gitFallback = gitMatch[1]
    }
  }
  if (plusFallback) return { path: plusFallback, prefixStripped: false }
  if (minusGit) return { path: minusGit, prefixStripped: true }
  if (minusPlain) return { path: minusPlain, prefixStripped: false }
  if (gitFallback) return { path: gitFallback, prefixStripped: true }
  return null
}

/** Prefix-stripped candidates whose first segment names a conventional
 * filesystem root — the shape a mangled absolute path takes after git's
 * `a/` / `b/` join swallowed its leading slash (issue #2493: the dashboard was
 * observed requesting `path=home/<user>/…`, which the backend correctly 400s).
 * A header matching this is treated as AMBIGUOUS: it is probed only as the
 * rooted spelling, and only when the surrounding chat text independently
 * names that spelling (pathHint corroboration) — otherwise it gets no probe
 * and no affordance. Existence probing cannot arbitrate the ambiguity itself,
 * because with no project dir configured the backend rejects every relative
 * path, making "relative spelling absent" meaningless as evidence. */
const ROOTLESS_ABS_RE = /^(home|Users|tmp|var|opt|workplace)\//

/** Fallback identity for one diff block's content when the host supplies no
 * `reviewAnchor`: full-length FNV-1a over the whole text. A content hash can
 * never separate two RENDERS of byte-identical content, which is why the
 * caller-supplied anchor (slot + message + fence line, or a tool_call_id) is
 * the primary identity and this is only the anchor-less fallback (bare
 * embeds, tests). Exported for tests. */
export function diffBlockId(code: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < code.length; i++) {
    h ^= code.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  let h2 = 5381
  for (let i = code.length - 1; i >= 0; i--) h2 = ((h2 << 5) + h2 + code.charCodeAt(i)) | 0
  return `db-${code.length}-${(h >>> 0).toString(36)}-${(h2 >>> 0).toString(36)}`
}

/* File identity and line text for review comments come from Pierre's OWN
 * parse — `ReviewFileRef` produced in `pierre/PierreImpl.tsx` from the same
 * `parsePatchFiles` enumeration Pierre renders. No second patch parser exists
 * here by design: every prior local parser (basename keys, header-pair
 * counting) eventually disagreed with Pierre's file enumeration on some patch
 * shape — duplicate basenames, hunk bodies that look like headers, deleted
 * files, headerless rename entries — and each disagreement corrupted draft
 * identity a different way. One parse, one identity. */

/** Inline draft form rendered as a Pierre line annotation under the picked
 * diff line. Local text state, so typing never re-renders the diff. */
function CommentForm({ onSubmit, onCancel, initialText }: { onSubmit: (text: string) => void; onCancel: () => void; initialText?: string }) {
  const [text, setText] = useState(initialText ?? '')
  const submit = () => { if (text.trim()) onSubmit(text) }
  const isMac = typeof navigator !== 'undefined' && /Mac|iP(hone|ad|od)/.test(navigator.platform)
  return (
    <div className="px-3 py-2 bg-bg-elevated border-y border-border flex flex-col gap-1.5">
      <textarea
        autoFocus
        value={text}
        onChange={e => setText(e.target.value)}
        rows={2}
        aria-label={i18nT('components.diffBlock.comment_placeholder')}
        placeholder={i18nT('components.diffBlock.comment_placeholder')}
        className="w-full resize-y rounded-md border border-border bg-bg-hover/30 px-2 py-1 text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/50"
        onKeyDown={e => {
          if (e.key === 'Escape') { e.preventDefault(); onCancel() }
          else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit() }
        }}
      />
      <div className="flex gap-2 items-center justify-end">
        <span className="text-[11px] text-muted mr-auto" aria-hidden="true">{i18nT('components.diffBlock.comment_keys', { save: isMac ? '⌘↵' : 'Ctrl+↵' })}</span>
        <button className="px-2 py-0.5 rounded text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={onCancel}>{i18nT('components.diffBlock.comment_cancel')}</button>
        <button className="px-2 py-0.5 rounded text-[12px] text-text bg-bg-hover disabled:opacity-50 cursor-pointer" disabled={!text.trim()} onClick={submit}>{i18nT(initialText != null ? 'components.diffBlock.comment_save' : 'components.diffBlock.comment_add')}</button>
      </div>
    </div>
  )
}

export default memo(function DiffBlock({ code, complete, onFileOpen, pathHint, streaming, onFold, reviewAnchor }: { code: string; complete: boolean; onFileOpen?: (path: string) => void; pathHint?: string; streaming?: boolean; onFold?: () => void; reviewAnchor?: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  // Shares the app-wide `mc-diff-split` preference with the side panel and
  // markdown panel (#6024): the choice made on any diff surface sticks and
  // seeds the next block, instead of every fence resetting to unified.
  const [sideBySide, setSideBySide] = usePersistedBool('mc-diff-split', true)
  // Plain-diff preference (Settings → Display). PierrePatch honours it on its
  // own; this block reads it too because the controls below are injected into
  // PIERRE's file header, which the plain render does not draw — so without a
  // header of our own here, turning colour off would also take Open/Copy away.
  const [plain] = usePlainDiff()
  // ---- Inline review comments -------------------------------------------
  // Slot from context ONLY (useContextSlotId): DiffBlock stays mountable
  // with no provider (bare tests, embeds) — commenting simply switches off.
  const slotId = useContextSlotId()
  // Drafting is offered only where the composer DRAINS drafts (ChatPage marks
  // it via ReviewSurfaceProvider). A pane or side surface with its own send
  // path shows the diff without the gutter, rather than collecting drafts its
  // composer would never display or send.
  const reviewSurface = useReviewSurface()
  const drafts = useReviewComments(slotId)
  // The line a comment is being drafted on; null = no form open. `fileIndex`
  // is the file's position in the patch — the identity Pierre's per-file
  // callbacks carry (rendered names are basenames and can collide).
  const [commentTarget, setCommentTarget] = useState<{ fileIndex: number; side: 'old' | 'new'; line: number; endLine?: number } | null>(null)
  // Resolve the file path: prefer headers inside the diff, fall back to the
  // pathHint extracted from the surrounding chat text by MarkdownRenderer
  // (helps when a tool emits "Created /path/to/file:" before a
  // bare diff with no +++/--- headers).
  const extracted = useMemo(() => extractFilePath(code), [code])
  // The header shows the basename only; `extracted` above keeps the full path
  // for the Open button, so shortening the copy Pierre parses costs nothing.
  // Only in HIGHLIGHTED mode, though: there the `--- `/`+++ ` lines are consumed
  // by Pierre to draw that header and never shown as text, so rewriting them is
  // invisible. The plain render prints the patch verbatim, so the same rewrite
  // would put a basename where the reader expects the original path — the one
  // thing "show me the raw diff" promises not to do, and wrong in what gets
  // copied out. So plain mode renders `code` untouched; its stand-in header
  // below does its own basename shortening on `headerPath` instead.
  const headerPath = extracted?.path ?? pathHint ?? null
  // When a git prefix was stripped and the remainder starts with a
  // conventional root (`home/…`, `tmp/…`, …), the header is ambiguous between
  // a repo-relative path and an absolute path whose leading slash git's
  // `a/` / `b/` join collapsed (`git diff --no-index /tmp/x` → `+++ b/tmp/x`).
  // An existence probe cannot settle this safely: with no project dir
  // configured the backend 400s EVERY relative path, so "relative spelling
  // absent" is not evidence, and an existence race could point the Open
  // button (which leads to an editor a save can write through) at an
  // unrelated host file. So the ambiguity is resolved by OUTSIDE
  // corroboration only: when the surrounding chat text independently names
  // the rooted spelling (pathHint), that spelling is probed instead; without
  // corroboration the header is suppressed outright — no probe (this was the
  // captured `path=home/<user>/…&resolve=1` 400 from issue #2493) and no
  // affordance, because no button beats a guessed target.
  const ambiguousRootless = extracted != null && extracted.prefixStripped && ROOTLESS_ABS_RE.test(extracted.path)
  const corroboratedRooted = ambiguousRootless && extracted != null && pathHint === '/' + extracted.path ? pathHint : null
  const probePath = ambiguousRootless ? corroboratedRooted : headerPath
  // The path the Open button acts on — committed by the probe effect, KEYED to
  // the headerPath that initiated the probe. The keyed derivation means a
  // verdict measured for a PREVIOUS header is never rendered against the
  // current one (same pattern as usePathKind): during the one render between a
  // header change and the effect re-running, the stale entry mismatches and
  // the button disappears instead of targeting the old path.
  const [resolved, setResolved] = useState<{ forHeader: string; path: string } | null>(null)
  const filePath = resolved && resolved.forHeader === headerPath ? resolved.path : null

  // Stash onFileOpen in a ref so the effect below only depends on the probe
  // candidates. If onFileOpen were a direct dep, every parent re-render that
  // produced a new function reference would refire the effect →
  // setResolved(null) → HEAD probe → setResolved(...), causing the Open
  // button to flicker and reflowing the diff body by 1-2px each time.
  const onFileOpenRef = useRef(onFileOpen)
  onFileOpenRef.current = onFileOpen

  useEffect(() => {
    setResolved(null)
    if (!probePath || !headerPath || !isSafePath(probePath) || !onFileOpenRef.current) return
    const ac = new AbortController()
    ;(async () => {
      let ok = false
      try {
        ok = (await fetch(fileReadUrl(probePath), { method: 'HEAD', signal: ac.signal })).ok
      } catch { /* network failure / abort → no affordance */ }
      // An aborted run must not commit: its fetch may have settled before
      // abort() fired, and the next run's setResolved(null) has already
      // cleared the slate this result was measured against.
      if (ok && !ac.signal.aborted) setResolved({ forHeader: headerPath, path: probePath })
    })()
    return () => ac.abort()
  }, [headerPath, probePath])

  // Diff layout follows the shared, persisted split preference (toggled from
  // this block's own header); wrap because chat/side-panel columns are
  // width-constrained. Pierre's own file header is the block's title row
  // (file icon, name, +/- counts).
  const options = useMemo(
    () => ({
      diffStyle: (sideBySide ? 'split' : 'unified') as 'split' | 'unified',
      overflow: 'wrap' as const,
      disableFileHeader: false,
      // A chat diff is a snippet, not a review surface: `simple` is a bare
      // hairline with no label and no expand control, which keeps a short block
      // reading as continuous code. Every other surface keeps `line-info`, whose
      // count and arrows earn their room on a full file. It also keeps the
      // library's untranslated "N unmodified lines" out of chat entirely.
      hunkSeparators: 'simple' as const,
      unsafeCSS: PIERRE_COMPACT_HEADER_CSS + PIERRE_WRAP_NO_HSCROLL_CSS + PIERRE_SEPARATOR_BG_CSS,
    }),
    [sideBySide],
  )

  const copy = () => { copyToClipboard(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }

  // Patch-level controls, slotted into Pierre's header metadata area (light
  // DOM, so outer-tree styling and the group-hover reveal both apply).
  // Space for the Open affordance is reserved on exactly the condition that runs
  // the probe, so the probe's OUTCOME never changes this row's geometry.
  //
  // Without the reserve, a successful HEAD adds a third button to the actions row
  // after an async round-trip. On a pointer surface that reflows the diff body by a
  // couple of pixels; under `HOVER_NONE_ACTIONS_ROW_CLS` (touch) the row is
  // `flex-wrap` with `p-3` targets, so the third button WRAPS it to a second line
  // and the header grows by a whole row — the transcript below then slides by that
  // much, mid-read, which is what "the file diff's loading pushed it up" is.
  //
  // Reserved space costs a phone row even for a file that turns out to be missing.
  // That is the right trade: a header that is one row taller from first paint is
  // stationary, and a header that changes height while someone is reading is not.
  const reserveOpen = Boolean(onFileOpen && probePath && isSafePath(probePath))

  // ---- Review-comment hooks handed to Pierre ------------------------------
  // File identity arrives as a `ReviewFileRef` from Pierre's OWN parse (see
  // pierre/PierreImpl.tsx): full path, enumeration index, and a line-text
  // resolver, all from the single `parsePatchFiles` call Pierre renders.
  // The STORE keys drafts by `ref.path`, so the outgoing message cites a
  // usable path; ephemeral form state keys by `ref.index`.
  // Identity of THIS diff block: the host's collision-free anchor (slot +
  // message + fence line, or a tool_call_id) when supplied, else the content
  // hash. Drafts carry it so the same file+line diffed twice in one
  // conversation never cross-binds.
  const blockId = useMemo(() => reviewAnchor ?? diffBlockId(code), [reviewAnchor, code])
  const toStoreSide = (s: 'deletions' | 'additions'): 'old' | 'new' => (s === 'deletions' ? 'old' : 'new')

  const annotationsFor = useCallback((file: ReviewFileRef) => {
    const storeKey = file.path || 'diff'
    const anns: { side: 'deletions' | 'additions'; lineNumber: number; metadata: unknown }[] = []
    for (const d of drafts) {
      if (d.blockId === blockId && d.fileIndex === file.index && d.file === storeKey) anns.push({ side: d.side === 'old' ? 'deletions' : 'additions', lineNumber: d.line, metadata: null })
    }
    if (
      commentTarget && commentTarget.fileIndex === file.index
      && !drafts.some(d => d.blockId === blockId && d.fileIndex === file.index && d.file === storeKey && d.side === commentTarget.side && d.line === commentTarget.line)
    ) {
      anns.push({ side: commentTarget.side === 'old' ? 'deletions' : 'additions', lineNumber: commentTarget.line, metadata: null })
    }
    return anns.length ? anns : undefined
  }, [drafts, commentTarget, blockId])

  const onGutterUtilityClick = useCallback((file: ReviewFileRef, range: { start: number; end: number; side?: 'deletions' | 'additions' }) => {
    const side = toStoreSide(range.side ?? 'additions')
    const start = Math.min(range.start, range.end)
    const end = Math.max(range.start, range.end)
    setCommentTarget(t => (
      t && t.fileIndex === file.index && t.side === side && t.line === start
        ? null
        : { fileIndex: file.index, side, line: start, endLine: end !== start ? end : undefined }
    ))
  }, [])

  const renderAnnotation = useCallback((annotation: { side: 'deletions' | 'additions'; lineNumber: number }, file: ReviewFileRef) => {
    const side = toStoreSide(annotation.side)
    const line = annotation.lineNumber
    const storeKey = file.path || 'diff'
    const formOpen = commentTarget !== null && commentTarget.fileIndex === file.index && commentTarget.side === side && commentTarget.line === line
    const draft = getReviewComment(slotId, blockId, file.index, storeKey, side, line)
    if (!formOpen && !draft) return null
    // ONE persistent annotation container for both states (saved chip ↔ edit
    // form): the outer div keeps its identity across the swap and the inner
    // motion.div transitions each state in, so the chip visibly *becomes*
    // the form instead of vanishing and being replaced (repo animation rule:
    // Framer, no new CSS keyframes).
    return (
      <div key={`ann:${side}:${line}`}>
        <motion.div
          key={formOpen ? 'form' : 'chip'}
          initial={{ opacity: 0, y: -3 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.15, ease: 'easeOut' }}
        >
          {formOpen ? (
            <CommentForm
              initialText={draft?.text}
              onCancel={() => setCommentTarget(null)}
              onSubmit={text => {
                if (slotId) {
                  addReviewComment(slotId, {
                    blockId,
                    fileIndex: file.index,
                    file: storeKey,
                    side,
                    line,
                    endLine: commentTarget?.endLine,
                    lineText: file.lineTextAt(side, line) ?? '',
                    text,
                  })
                }
                setCommentTarget(null)
              }}
            />
          ) : (
            // Pending-draft chip under its line: the note text, click to edit,
            // X to remove. Buttons, so both are keyboard-reachable.
            <div className="flex items-center gap-2 px-3 py-1 bg-bg-elevated border-y border-border text-[12px]">
              <button
                className="flex-1 min-w-0 truncate text-left text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
                onClick={() => setCommentTarget({ fileIndex: file.index, side, line, endLine: draft!.endLine })}
                title={i18nT('components.diffBlock.comment_edit')}
                aria-label={`${i18nT('components.diffBlock.comment_edit')}: ${draft!.text}`}
              >
                {draft!.text}
              </button>
              <button
                className="p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer shrink-0 bg-transparent border-none"
                onClick={() => { if (slotId) removeReviewComment(slotId, draft!.id) }}
                title={i18nT('components.diffBlock.comment_remove')}
                aria-label={i18nT('components.diffBlock.comment_remove')}
              >
                <X size={12} className="lucide-inline" aria-hidden />
              </button>
            </div>
          )}
        </motion.div>
      </div>
    )
  }, [commentTarget, slotId, blockId])

  // Only offered when a slot exists to hold drafts; plain mode renders raw
  // text through a different path and never sees these hooks.
  const review: PatchReviewHooks | undefined = useMemo(
    () => (slotId && reviewSurface ? { annotationsFor, renderAnnotation, onGutterUtilityClick } : undefined),
    [slotId, reviewSurface, annotationsFor, renderAnnotation, onGutterUtilityClick],
  )

  const headerControls = () => (
    <span className={`relative z-10 flex items-center gap-1 opacity-0 group-hover/diff:opacity-100 group-focus-within/diff:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
      {reserveOpen && (
        <button
          className={`px-1.5 py-0.5 rounded text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer ${filePath ? '' : 'invisible pointer-events-none'}`}
          onClick={filePath && onFileOpen ? () => onFileOpen(filePath) : undefined}
          disabled={!filePath}
          aria-hidden={!filePath}
          tabIndex={filePath ? undefined : -1}
          title={filePath ? i18nT('components.diffBlock.open_in_side_panel', { path: filePath }) : undefined}
          aria-label={filePath ? i18nT('components.diffBlock.open_in_side_panel', { path: filePath }) : undefined}
        >
          {i18nT('components.diffBlock.open')}
        </button>
      )}
      {/* Split/unified is a PIERRE layout option, so the control is omitted in
          plain mode rather than left there doing nothing to the raw patch. */}
      {!plain && (
        <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={() => setSideBySide(!sideBySide)} title={sideBySide ? i18nT('components.diffBlock.unified_view') : i18nT('components.diffBlock.split_view')} aria-label={sideBySide ? i18nT('components.diffBlock.switch_to_unified_view') : i18nT('components.diffBlock.switch_to_split_view')}>{sideBySide ? <Rows2 size={13} /> : <Columns2 size={13} />}</button>
      )}
      <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.diffBlock.copied') : i18nT('components.diffBlock.copy_patch')} aria-label={copied ? i18nT('components.diffBlock.copied') : i18nT('components.diffBlock.copy_patch')}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
    </span>
  )

  return (
    /* The header shows the basename, so two changed files sharing a name render
       as identical blocks; the full path lives here as a tooltip. It sits on the
       wrapper because Pierre paints the title inside its shadow root — a native
       `title` resolves up the flat tree, so hovering the filename picks it up. */
    <div className="diff-block group/diff rounded-xl border border-border overflow-hidden" title={headerPath ?? undefined}>
      <div className={`relative pierre-surface ${streaming ? 'ft-stream-block' : ''}`}>
        {/* Fold handle: a narrow chevron zone at the header's left edge — NOT
            the whole strip (the filename must stay inert for select/copy and
            its full-path tooltip) and NOT a member of the actions row
            (max-two-buttons-per-row counts siblings in the horizontal group).
            The chevron is visible at rest (muted) so the only density control
            is discoverable without mousing over; it brightens on hover/focus.
            NO `title` — it would shadow the wrapper's full-path tooltip;
            aria-label carries the action for this icon-only control. */}
        {onFold && (
          <button
            type="button"
            className="group/fold absolute left-0 top-0 w-8 h-8 z-0 flex items-center justify-center bg-transparent border-none cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 rounded-tl-xl"
            data-diff-toggle
            onClick={onFold}
            aria-label={i18nT('pages.chat.toolCallLine.aria_hide_diff')}
          >
            <ChevronUp
              size={13}
              aria-hidden
              className="text-muted/60 group-hover/diff:text-muted hover:!text-text group-focus-visible/fold:text-text transition-colors"
            />
          </button>
        )}
        {/* Plain mode: Pierre's file header is what normally carries the
            filename and hosts `headerControls`, so a header of our own stands
            in for it — otherwise turning colour off would silently remove
            Open/Copy and the filename too. Padded left when the fold chevron
            is present, since that button overlays this row's left edge.
            `min-h-8`, not `h-8`: `headerControls` grows its buttons to 40px on a
            touch device (`HOVER_NONE_ACTIONS_ROW_CLS` pads them for thumbs), and
            a fixed 32px band would clip the top of them against `.diff-block`'s
            `overflow-hidden` and push the rest over the patch body. Pierre's own
            header band — the thing this stands in for — is `min-height` for the
            same reason. */}
        {plain && (
          <div className={`flex items-center justify-between gap-2 min-h-8 pr-2 border-b border-border text-[12px] text-muted ${onFold ? 'pl-8' : 'pl-3'}`}>
            <span className="truncate font-mono">{headerPath ? headerPath.split('/').pop() : ''}</span>
            {headerControls()}
          </div>
        )}
        <PierrePatch patch={code} displayBasenames options={options} renderHeaderMetadata={headerControls} review={review} />
        {!complete && <div className="px-3 py-1 text-muted text-[12px] italic animate-pulse">{i18nT('components.diffBlock.generating_diff')}</div>}
      </div>
    </div>
  )
})
