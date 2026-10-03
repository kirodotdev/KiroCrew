/**
 * Editable code surface on Pierre's editor (`@pierre/diffs/edit`) — the
 * editing surface for every code-editing view. Lives beside `PierreImpl` in
 * the same lazy chunk; reach it through `../pierre` only.
 */
import { forwardRef, useEffect, useId, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { BaseCodeOptions, FileContents } from '@pierre/diffs'
import { EditProvider, File, MultiFileDiff, Virtualizer } from '@pierre/diffs/react'
import { Editor, type EditorOptions } from '@pierre/diffs/edit'
import { useIsDark } from '../hooks/useIsDark'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import {
  PIERRE_EDIT_CARET_ALIGN_CSS,
  PIERRE_VIRTUALIZER_CONFIG,
  pierreDiffOptions,
  pierreFileOptions,
  pierreThemeType,
} from './config'
import { activeWorkerPool, contentCacheKey, PierreShell, usePierreWorkerPool, useRegisterEditorSurface } from './PierreImpl'
import { isPierreFilePairWithinBudget } from './renderBudget'

export interface EditorMarker {
  severity: 'error' | 'warning' | 'info'
  message: string
  line: number
}

export interface PierreEditorHandle {
  /** Reveal `line` (1-based) and focus it. With `endLine`, select the whole
   *  inclusive span so a `path:2410-2465` citation highlights every cited row
   *  rather than only its first. */
  jumpToLine: (line: number, endLine?: number) => void
  focus: () => void
}

function createEditor<LAnnotation>(options: EditorOptions<LAnnotation>) {
  return new Editor(options)
}

/** Reveal `line` (1-based), selecting through `endLine` when a span was cited.
 *
 *  `focus({ lineNumber })` is Pierre's jump entry point and the only path that
 *  survives windowing: it expands a collapsed region and, when the target row is
 *  not built yet, scrolls to the modelled position to force a render and parks a
 *  retry so a later sync lands precisely.
 *
 *  The span selection goes second and deliberately reuses that reveal rather
 *  than fighting it: `direction: 'none'` makes Pierre reveal the selection's
 *  START, which is the row already on screen, so the span highlights without the
 *  view jumping to its end. */
function revealSpan(editor: Editor<undefined>, line: number, endLine?: number): void {
  editor.focus({ lineNumber: line })
  if (endLine === undefined || endLine <= line) return
  editor.setSelections([{
    start: { line: line - 1, character: 0 },
    // Matches how markers spell "to the end of this row".
    end: { line: endLine - 1, character: Number.MAX_SAFE_INTEGER },
    direction: 'none',
  }])
}

function offsetToEditorPosition(contents: string, offset: number): { line: number; character: number } {
  const before = contents.slice(0, Math.max(0, Math.min(offset, contents.length)))
  const lastNewline = before.lastIndexOf('\n')
  return {
    line: lastNewline < 0 ? 0 : before.split('\n').length - 1,
    character: lastNewline < 0 ? before.length : before.length - lastNewline - 1,
  }
}

function applyMarkers(editor: Editor<undefined>, markers: EditorMarker[] | undefined): void {
  if (markers == null) return
  editor.setMarkers(
    markers.map(marker => ({
      severity: marker.severity,
      message: marker.message,
      start: { line: marker.line - 1, character: 0 },
      end: { line: marker.line - 1, character: Number.MAX_SAFE_INTEGER },
    })),
  )
}

export const PierreEditorImpl = forwardRef<PierreEditorHandle, {
  file: FileContents
  options?: BaseCodeOptions
  onChange: (contents: string) => void
  /** Cmd/Ctrl+S inside the surface. */
  onSave?: () => void
  markers?: EditorMarker[]
  onCursorChange?: (line: number, column: number) => void
  /** Live-diff editing: the baseline contents to diff the edit session
   *  against (`null` = new file, whole buffer reads as added). `undefined`
   *  renders the plain editor. */
  diffBase?: string | null
  /** Split vs unified layout for the live-diff surface. */
  diffSplit?: boolean
  /** Show unchanged regions in the live-diff surface instead of folding them. */
  diffExpandUnchanged?: boolean
  className?: string
}>(function PierreEditorImpl({ file, options, onChange, onSave, markers, onCursorChange, diffBase, diffSplit, diffExpandUnchanged, className }, ref) {
  const dark = useIsDark()
  const poolState = usePierreWorkerPool()
  useRegisterEditorSurface()
  const activePool = activeWorkerPool(poolState)
  const pierreActive = activePool !== undefined
  const resolved = useMemo(
    () => pierreFileOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  const resolvedDiff = useMemo(
    () => pierreDiffOptions({
      themeType: pierreThemeType(dark),
      diffStyle: diffSplit ? 'split' : 'unified',
      ...(diffExpandUnchanged == null ? {} : { expandUnchanged: diffExpandUnchanged }),
      ...options,
      unsafeCSS: (options?.unsafeCSS ?? '') + PIERRE_EDIT_CARET_ALIGN_CSS,
    }),
    [dark, options, diffSplit, diffExpandUnchanged],
  )
  const surfaceId = useId()
  const recoveryStatusId = `${surfaceId}-recovery-status`
  const containerRef = useRef<HTMLDivElement>(null)
  const fallbackRef = useRef<HTMLTextAreaElement>(null)
  const restoreFocusRef = useRef(false)
  const fallbackSelectionRef = useRef<{ start: number; end: number; direction: 'forward' | 'backward' | 'none' } | null>(null)
  const propContentsRef = useRef(file.contents)
  const latestContentsRef = useRef(file.contents)
  const seedContentsRef = useRef(file.contents)
  const seedKeyRef = useRef(contentCacheKey(file.name, file.contents))
  const reseedCountRef = useRef(0)
  const remountDraftRef = useRef<string | null>(null)
  const previousPierreActiveRef = useRef(pierreActive)
  const [fallbackDraft, setFallbackDraft] = useState(file.contents)
  const propChanged = propContentsRef.current !== file.contents
  if (propChanged) propContentsRef.current = file.contents
  // A prop equal to the buffer this editor last emitted is the caller echoing
  // its own edit, and leaves the session alone; only a change from outside
  // (a disk read, Cancel, Refresh) reseeds it.
  if (propChanged && file.contents !== latestContentsRef.current) {
    seedContentsRef.current = file.contents
    // Cancel can put back exactly the text Pierre is showing (the seed, or a
    // recovery remount's draft); the key must still move so Pierre drops the
    // edited document and shows the restored one.
    const nextKey = contentCacheKey(file.name, file.contents)
    const renderedKey = remountDraftRef.current === null
      ? seedKeyRef.current
      : contentCacheKey(file.name, remountDraftRef.current)
    seedKeyRef.current = nextKey === renderedKey ? `${nextKey}#${++reseedCountRef.current}` : nextKey
    latestContentsRef.current = file.contents
    remountDraftRef.current = null
    if (fallbackDraft !== file.contents) setFallbackDraft(file.contents)
  }
  const previousPierreActive = previousPierreActiveRef.current
  const enteringFallback = previousPierreActive && !pierreActive
  const leavingFallback = !previousPierreActive && pierreActive
  if (enteringFallback && containerRef.current?.contains(document.activeElement)) {
    restoreFocusRef.current = true
  }
  const focusedFallback = fallbackRef.current
  if (leavingFallback && focusedFallback !== null && focusedFallback === document.activeElement) {
    restoreFocusRef.current = true
    fallbackSelectionRef.current = {
      start: focusedFallback.selectionStart,
      end: focusedFallback.selectionEnd,
      direction: focusedFallback.selectionDirection ?? 'none',
    }
  }
  previousPierreActiveRef.current = pierreActive
  if (enteringFallback) {
    remountDraftRef.current = latestContentsRef.current
    if (fallbackDraft !== latestContentsRef.current) setFallbackDraft(latestContentsRef.current)
  }
  const recoveryContents = enteringFallback ? latestContentsRef.current : fallbackDraft
  // The remount seed is frozen for the life of a Pierre generation: the buffer
  // is the source of truth while an edit session is active, and a `file` prop
  // that changed on every keystroke would clear Pierre's dirty render cache
  // mid-edit. Later edits live in `latestContentsRef`, which the next
  // recovery snapshot reads, so nothing typed after a remount is lost.
  const remountDraft = remountDraftRef.current
  const editorContents = remountDraft ?? seedContentsRef.current
  const editorKey = remountDraft === null ? seedKeyRef.current : contentCacheKey(file.name, remountDraft)
  // The seam owns Pierre's `file` contract so no caller has to. While an edit
  // session is live, Pierre's own document owns the text, and the `file` handed
  // to it is ONE object per session, keyed by the seed:
  //  - a new `cacheKey` makes the editor rebuild its document and reset its
  //    selections, so a key that moved with each keystroke would drop focus and
  //    put the caret at line 1 every time;
  //  - a new object under the same key cannot be sequenced against the render
  //    Pierre schedules for the object it still holds, so that render reads the
  //    old object's text, and once the document has grown past it Return throws
  //    "Line doesnt exist".
  // A new object appears, with a content-derived key, only on an external source
  // change or a worker-recovery remount.
  //
  // The object's `contents`, though, mirror the buffer: the editor's `onChange`
  // writes each new text into this same object. Pierre reads a file's `contents`
  // again whenever it renders one without a grammar, which it does for a
  // plain-text file on every line added or removed (an untagged chat fence is
  // `snippet.txt`, and a fence tag that is not a file extension -- `python`,
  // `typescript` -- also resolves to plain text), and for any file whose grammar
  // is still loading. From a frozen seed that render puts the removed line back
  // on screen while the document keeps the edit, so the next keystroke lands on
  // the line that moved into that row. A highlighted file reads its rows from
  // the editor's render cache instead and never looks at `contents` again, so
  // the mirror costs it nothing. Pierre treats the held file's `contents` as the
  // session's text as well: it writes the edited lines back into that object
  // whenever it drops the render cache. `name`, `lang` and `header` are read
  // when the object is created: every caller changes them only by remounting.
  const sessionFileRef = useRef<FileContents | null>(null)
  let editorFile = sessionFileRef.current
  if (editorFile === null || editorFile.cacheKey !== editorKey) {
    editorFile = {
      name: file.name,
      contents: editorContents,
      ...(file.lang === undefined ? {} : { lang: file.lang }),
      ...(file.header === undefined ? {} : { header: file.header }),
      cacheKey: editorKey,
    }
  }
  sessionFileRef.current = editorFile
  // The live-diff surface keeps the opening text, as before: its renderer
  // re-parses the pair whenever the baseline changes, and the premise above
  // (the renderer re-reads the held file and writes edits back into it) is
  // the single-file renderer's.
  const diffNewFile = useMemo<FileContents>(
    () => ({
      name: file.name,
      contents: editorContents,
      ...(file.lang === undefined ? {} : { lang: file.lang }),
      ...(file.header === undefined ? {} : { header: file.header }),
      cacheKey: editorKey,
    }),
    [file.name, file.lang, file.header, editorContents, editorKey],
  )
  const baseFile = useMemo<FileContents | null>(
    () => (diffBase == null
      ? null
      : { name: file.name, contents: diffBase, cacheKey: contentCacheKey(file.name, diffBase, surfaceId + ':edit-base') }),
    [diffBase, file.name, surfaceId],
  )
  const renderLiveDiff = diffBase !== undefined
    && isPierreFilePairWithinBudget(baseFile, diffNewFile)
  const editorRef = useRef<Editor<undefined> | null>(null)
  /** A jump requested before Pierre bound its editor, replayed on attach. */
  const pendingJumpRef = useRef<{ line: number; endLine?: number } | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const onSaveRef = useRef(onSave)
  onSaveRef.current = onSave
  const onCursorRef = useRef(onCursorChange)
  onCursorRef.current = onCursorChange
  const generationRef = useRef(poolState.generation)
  generationRef.current = poolState.generation
  const markersRef = useRef(markers)
  markersRef.current = markers
  const markerApplicationRef = useRef<{ generation: number; markers: EditorMarker[] | undefined } | null>(null)

  useLayoutEffect(() => {
    if (pierreActive) return
    editorRef.current = null
    if (restoreFocusRef.current) {
      fallbackRef.current?.focus()
      restoreFocusRef.current = false
    }
  }, [pierreActive, poolState.generation])

  const reportCursor = () => {
    const sel = editorRef.current?.getState()?.selections?.[0]
    if (sel) onCursorRef.current?.(sel.end.line + 1, sel.end.character + 1)
  }

  // Editor identity is per-mounted-surface: the factory caches by options
  // object identity, so this memo must be stable for the component lifetime.
  const editorOptions = useMemo<EditorOptions<undefined>>(
    () => ({
      onAttach(editor) {
        editorRef.current = editor
        const generation = generationRef.current
        const currentMarkers = markersRef.current
        const applied = markerApplicationRef.current
        if (
          currentMarkers != null
          && currentMarkers.length > 0
          && !(applied?.generation === generation && applied.markers === currentMarkers)
        ) {
          applyMarkers(editor, currentMarkers)
          markerApplicationRef.current = { generation, markers: currentMarkers }
        }
        const fallbackSelection = fallbackSelectionRef.current
        if (fallbackSelection !== null) {
          fallbackSelectionRef.current = null
          editor.setSelections([{
            start: offsetToEditorPosition(latestContentsRef.current, fallbackSelection.start),
            end: offsetToEditorPosition(latestContentsRef.current, fallbackSelection.end),
            direction: fallbackSelection.direction,
          }])
        }
        const pending = pendingJumpRef.current
        if (pending !== null) {
          pendingJumpRef.current = null
          revealSpan(editor, pending.line, pending.endLine)
        }
        if (restoreFocusRef.current) {
          restoreFocusRef.current = false
          editor.focus()
        }
      },
      onChange(changed) {
        // `contents` is a getter that joins the whole document: read it once.
        const contents = changed.contents
        latestContentsRef.current = contents
        // The editor reports the change before it re-renders, so the held
        // file carries the new text for every render this change triggers,
        // Pierre's own included.
        const session = sessionFileRef.current
        if (session !== null) session.contents = contents
        onChangeRef.current(contents)
        reportCursor()
      },
    }),
    [],
  )

  useEffect(() => {
    const editor = editorRef.current
    if (!editor) return
    const applied = markerApplicationRef.current
    if (applied?.generation === poolState.generation && applied.markers === markers) return
    applyMarkers(editor, markers)
    markerApplicationRef.current = { generation: poolState.generation, markers }
  }, [markers, poolState.generation])

  useImperativeHandle(ref, () => ({
    jumpToLine: (line: number, endLine?: number) => {
      // Pierre's own jump entry point, and the only one that survives windowing:
      // it sets the selection, expands a collapsed region, and when the target
      // row is not built yet scrolls to the modelled position to force a render
      // and parks a retry so a later sync lands precisely. `lineNumber` is
      // one-based, matching the `path:line` references callers hold.
      //
      // Setting selections and then calling focus() without `preventScroll`
      // instead issues a SECOND scroll toward a caret element that may not exist
      // yet, which lands at the estimated offset and stays there.
      //
      // The handle is published on mount but Pierre binds its editor a commit or
      // more later, so a request arriving in between is held for `onAttach`:
      // callers consume their reveal nonce on the call and will not ask twice.
      const target = Math.max(1, line)
      const editor = editorRef.current
      if (editor === null) {
        pendingJumpRef.current = { line: target, endLine }
        return
      }
      revealSpan(editor, target, endLine)
    },    focus: () => editorRef.current?.focus(),
  }), [])

  return (
    // The wrapper only intercepts the save chord and mirrors cursor position;
    // the interactive, focusable surface is Pierre's own editable content
    // inside — a role here would misdescribe the scroll container.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      ref={containerRef}
      className="h-full w-full overflow-hidden"
      onKeyDownCapture={e => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
          e.preventDefault()
          onSaveRef.current?.()
        }
      }}
      onKeyUp={reportCursor}
      onMouseUp={reportCursor}
    >
      {activePool !== undefined ? (
        <PierreShell pool={activePool} generation={poolState.generation}>
        <Virtualizer
          config={PIERRE_VIRTUALIZER_CONFIG}
          className={`pierre-surface h-full w-full overflow-auto ${className ?? ''}`}
        >
          <EditProvider createEditor={createEditor}>
          {renderLiveDiff ? (
            // Live-diff edit session: Pierre diffs the buffer against the
            // baseline as you type. Inputs outside the renderer-thread budget
            // keep the same editor behavior but omit live diff decoration.
            // Keyed so flipping modes rebuilds the edit session rather than
            // rebinding one editor across surface kinds.
            <MultiFileDiff
              key="diff"
              oldFile={baseFile}
              newFile={diffNewFile}
              edit
              editorOptions={editorOptions}
              options={resolvedDiff}
            />
          ) : (
            <File key="file" file={editorFile} edit editorOptions={editorOptions} options={resolved} />
          )}
          </EditProvider>
        </Virtualizer>
        </PierreShell>
      ) : (
        // Same `className` as the Virtualizer branch: the caller's size cap
        // must hold whichever surface is on screen.
        <div className={`grid h-full w-full grid-rows-[auto_minmax(0,1fr)] ${className ?? ''}`}>
          <textarea
            ref={fallbackRef}
            aria-label={file.name}
            aria-describedby={recoveryStatusId}
            className="pierre-editor-fallback row-start-2 h-full min-h-0 w-full resize-none overflow-auto bg-transparent px-3 py-2 text-[13px] font-mono leading-5 text-text"
            spellCheck={false}
            value={recoveryContents}
            onChange={event => {
              const contents = event.currentTarget.value
              fallbackSelectionRef.current = {
                start: event.currentTarget.selectionStart,
                end: event.currentTarget.selectionEnd,
                direction: event.currentTarget.selectionDirection ?? 'none',
              }
              latestContentsRef.current = contents
              remountDraftRef.current = contents
              setFallbackDraft(contents)
              onChangeRef.current(contents)
            }}
          />
          {poolState.phase === 'unavailable' ? (
            <div id={recoveryStatusId} className="row-start-1 shrink-0 bg-bg-elevated">
              {/* No hand-off: the recovery textarea may hold an unsaved draft,
                  so an agent action must not replace or navigate away from it. */}
              <ErrorNotice
                variant="inline"
                className="px-3 py-1 text-[11px]"
                message={i18nT('components.pierreEditorImpl.highlighting_unavailable_reload')}
              />
            </div>
          ) : poolState.phase === 'starting' ? (
            // Cold start is not a failure: a quiet status line, not an alert.
            <div
              id={recoveryStatusId}
              role="status"
              className="pointer-events-none row-start-1 shrink-0 border-t border-border bg-bg-elevated px-3 py-1 text-[11px] text-muted"
            >
              {i18nT('components.pierreEditorImpl.highlighting_starting_editing_available')}
            </div>
          ) : (
            <div id={recoveryStatusId} className="row-start-1 shrink-0 bg-bg-elevated">
              {/* No hand-off: the recovery textarea may hold an unsaved draft,
                  so an agent action must not replace or navigate away from it. */}
              <ErrorNotice
                variant="inline"
                className="px-3 py-1 text-[11px]"
                message={i18nT('components.pierreEditorImpl.highlighting_restarting_editing_available')}
              />
            </div>
          )}
        </div>
      )}
    </div>
  )
})
