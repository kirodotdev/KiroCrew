/**
 * PapyrusEditor — the LaTeX source pane, backed by the Pierre editor.
 *
 * The component owns no document state: the page holds the buffer and passes it
 * down, and the editor reports edits back through `onChange`. It exposes one
 * imperative affordance — `jumpToLine` — so clicking a compiler error can move
 * the cursor without the page reaching into the editor's internals. Compiler
 * diagnostics are pushed into the editor's marker store.
 */
import { forwardRef, useCallback, useImperativeHandle, useMemo, useRef } from 'react'
import { PierreEditor, type EditorMarker, type PierreEditorHandle } from '../../pierre'
import type { Diagnostic } from './api'

const EDITOR_OPTIONS = { overflow: 'wrap' } as const

export interface PapyrusEditorHandle {
  /** Move the cursor to `line`, scroll it into view, and focus the editor. */
  jumpToLine: (line: number) => void
  /** Focus without moving the cursor. */
  focus: () => void
}

export interface PapyrusEditorProps {
  /** The file being edited, as a project-relative path. Drives the language id. */
  path: string
  value: string
  onChange: (value: string) => void
  /** Invoked on Cmd/Ctrl+S. The page saves and compiles. */
  onSave: () => void
  diagnostics: Diagnostic[]
  onCursorChange?: (line: number, column: number) => void
  /**
   * Refuse edits while the file's content is still in flight.
   *
   * `path` changes the moment the user picks a file, but `value` only catches up
   * when the fetch lands — so between the two the editor shows the PREVIOUS
   * file's text under the NEW path. Read-only removes the window rather than
   * trying to reconcile afterwards.
   */
  readOnly?: boolean
}

const PapyrusEditor = forwardRef<PapyrusEditorHandle, PapyrusEditorProps>(function PapyrusEditor(
  { path, value, onChange, onSave, diagnostics, onCursorChange, readOnly = false },
  ref,
) {
  const editorRef = useRef<PierreEditorHandle | null>(null)
  useImperativeHandle(ref, () => ({
    jumpToLine: (line: number) => editorRef.current?.jumpToLine(line),
    focus: () => editorRef.current?.focus(),
  }), [])

  const markers = useMemo<EditorMarker[]>(
    () =>
      diagnostics
        // A diagnostic with no line cannot be placed — dropping it here is
        // better than pinning it to line 1 and pointing at innocent source.
        .filter(d => d.line !== null && d.line >= 1)
        .map(d => ({
          severity: d.level === 'error' ? 'error' : d.level === 'warning' ? 'warning' : 'info',
          message: d.message,
          line: d.line as number,
        })),
    [diagnostics],
  )

  // Pierre owns its TextDocument and only rebuilds it when name/cacheKey change,
  // so the seed decides the editing SESSION while `contents` stays live. Keying
  // the seed on `path` alone is not enough: the page sets `path` before the fetch
  // lands and clears `readOnly` one render BEFORE it adopts the buffer, so a
  // path-keyed seed captures the empty string and — the path never having changed
  // — never corrects itself, leaving the document blank.
  //
  // So a value this editor did not emit is treated as an external change (the
  // fetch landing, a pull rewriting the file) and re-seeds; a value it did emit
  // is the page echoing back a keystroke and must NOT, or the remount would
  // drop the caret mid-word. Mirrors ContentRenderer's editor session handling.
  const initialRef = useRef<{ path: string; file: { name: string; contents: string; cacheKey: string } }>()
  const lastEmittedRef = useRef<string | null>(null)
  const lastValueRef = useRef<string | null>(null)
  const seedRef = useRef(0)
  const seedFile = () => ({
    path,
    file: {
      name: path.split('/').pop() || path,
      contents: value,
      cacheKey: `papyrus:${path}:${seedRef.current}`,
    },
  })
  if (initialRef.current?.path !== path) {
    initialRef.current = seedFile()
  } else if (value !== lastValueRef.current) {
    if (value === lastEmittedRef.current) {
      // Consume the marker: it matches ONE echo of our own edit, so a later
      // external change back to this same text still reseeds.
      lastEmittedRef.current = null
    } else {
      seedRef.current++
      initialRef.current = seedFile()
    }
  }
  lastValueRef.current = value

  const handleChange = useCallback((v: string) => {
    lastEmittedRef.current = v
    onChange(v)
  }, [onChange])

  // Contents track the live buffer so the rendered rows stay in step with the
  // text; the cacheKey stays pinned to the seed so the caret survives typing.
  const liveFile = useMemo(
    () => ({ ...initialRef.current!.file, contents: value }),
    [value],
  )

  if (readOnly) {
    // The read-only window is the brief gap between picking a file and its
    // content landing; a static pre avoids an editable stale buffer.
    return (
      <div className="h-full w-full min-h-0 overflow-auto pierre-surface" data-testid="papyrus-editor">
        <pre className="m-0 px-3 py-2 text-[13px] font-mono leading-relaxed whitespace-pre-wrap opacity-60">{value}</pre>
      </div>
    )
  }

  return (
    <div className="h-full w-full min-h-0 overflow-hidden" data-testid="papyrus-editor">
      <PierreEditor
        key={initialRef.current.file.cacheKey}
        ref={editorRef}
        file={liveFile}
        options={EDITOR_OPTIONS}
        onChange={handleChange}
        onSave={onSave}
        markers={markers}
        onCursorChange={onCursorChange}
      />
    </div>
  )
})

export default PapyrusEditor
