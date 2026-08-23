/**
 * Editable code surface on Pierre's editor (`@pierre/diffs/edit`) — the
 * editing surface for every code-editing view. Lives beside `PierreImpl` in
 * the same lazy chunk; reach it through `../pierre` only.
 */
import { forwardRef, useEffect, useId, useImperativeHandle, useMemo, useRef } from 'react'
import type { BaseCodeOptions, FileContents } from '@pierre/diffs'
import { EditProvider, File, MultiFileDiff, Virtualizer } from '@pierre/diffs/react'
import { Editor, type EditorOptions } from '@pierre/diffs/edit'
import { useIsDark } from '../hooks/useIsDark'
import {
  PIERRE_EDIT_CARET_ALIGN_CSS,
  PIERRE_VIRTUALIZER_CONFIG,
  pierreDiffOptions,
  pierreFileOptions,
  pierreThemeType,
} from './config'
import { contentCacheKey, PierreShell } from './PierreImpl'

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
  const baseFile = useMemo<FileContents | null>(
    () => (diffBase == null
      ? null
      : { name: file.name, contents: diffBase, cacheKey: contentCacheKey(file.name, diffBase, surfaceId + ':edit-base') }),
    [diffBase, file.name, surfaceId],
  )
  const editorRef = useRef<Editor<undefined> | null>(null)
  /** A jump requested before Pierre bound its editor, replayed on attach. */
  const pendingJumpRef = useRef<{ line: number; endLine?: number } | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const onSaveRef = useRef(onSave)
  onSaveRef.current = onSave
  const onCursorRef = useRef(onCursorChange)
  onCursorRef.current = onCursorChange

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
        const pending = pendingJumpRef.current
        if (pending !== null) {
          pendingJumpRef.current = null
          revealSpan(editor, pending.line, pending.endLine)
        }
      },
      onChange(changed) {
        onChangeRef.current(changed.contents)
        reportCursor()
      },
    }),
    [],
  )

  useEffect(() => {
    const editor = editorRef.current
    if (!editor || markers == null) return
    editor.setMarkers(
      markers.map(m => ({
        severity: m.severity,
        message: m.message,
        start: { line: m.line - 1, character: 0 },
        end: { line: m.line - 1, character: Number.MAX_SAFE_INTEGER },
      })),
    )
  }, [markers])

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
      <PierreShell>
      <Virtualizer
        config={PIERRE_VIRTUALIZER_CONFIG}
        className={`pierre-surface h-full w-full overflow-auto ${className ?? ''}`}
      >
        <EditProvider createEditor={createEditor}>
        {diffBase !== undefined ? (
          // Live-diff edit session: Pierre diffs the buffer against the
          // baseline as you type. Keyed so flipping modes rebuilds the edit
          // session rather than rebinding one editor across surface kinds.
          <MultiFileDiff
            key="diff"
            oldFile={baseFile}
            newFile={file}
            edit
            editorOptions={editorOptions}
            options={resolvedDiff}
          />
        ) : (
          <File key="file" file={file} edit editorOptions={editorOptions} options={resolved} />
        )}
      </EditProvider>
      </Virtualizer>
      </PierreShell>
    </div>
  )
})
