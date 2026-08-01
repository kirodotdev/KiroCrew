/**
 * PapyrusEditor — the LaTeX source pane, backed by Monaco.
 *
 * Monaco is already vendored in this repository (`@monaco-editor/react` +
 * `monaco-editor`, loaded from the local bundle by `utils/monacoLocal.ts`), so
 * the upstream app's CodeMirror dependency is not reintroduced. Monaco ships no
 * TeX grammar, so `latexLanguage.ts` registers a small Monarch tokenizer on first
 * mount and `monacoLanguage()` returns its id.
 *
 * The component owns no document state: the page holds the buffer and passes it
 * down, and Monaco reports edits back through `onChange`. It exposes one
 * imperative affordance — `jumpToLine` — so clicking a compiler error can move
 * the cursor without the page reaching into the editor's internals.
 *
 * Compiler diagnostics are pushed into Monaco's own marker store, which is what
 * paints the squiggles and populates the gutter. That is deliberately Monaco's
 * job rather than a hand-rolled overlay: markers survive scrolling, folding and
 * window resize for free.
 */
import { forwardRef, lazy, Suspense, useCallback, useEffect, useImperativeHandle, useRef } from 'react'
import type { Monaco } from '@monaco-editor/react'
import type { editor } from 'monaco-editor'
import { useIsDark } from '../../components/MonacoCodeBlock'
import { registerLatexLanguage } from './latexLanguage'
import { monacoLanguage } from './lib'
import type { Diagnostic } from './api'

import { i18nT } from '../../i18n/t'

const Editor = lazy(async () => {
  const { ensureMonacoLocal } = await import('../../utils/monacoLocal')
  await ensureMonacoLocal()
  return import('@monaco-editor/react')
})

/** Marker owner id — Monaco groups markers by owner, so ours replace ours only. */
const MARKER_OWNER = 'papyrus-latex'

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
   * file's text under the NEW path. A keystroke in that window made the old
   * content dirty against the new file, which then rejected the fetched content
   * as "dirty" and saved the wrong text over the selected file. Read-only is the
   * fix that removes the window rather than trying to reconcile afterwards.
   */
  readOnly?: boolean
}

/** Map a compiler diagnostic level to a Monaco marker severity. */
function severityFor(monaco: Monaco, level: Diagnostic['level']): number {
  if (level === 'error') return monaco.MarkerSeverity.Error
  if (level === 'warning') return monaco.MarkerSeverity.Warning
  return monaco.MarkerSeverity.Info
}

const PapyrusEditor = forwardRef<PapyrusEditorHandle, PapyrusEditorProps>(function PapyrusEditor(
  { path, value, onChange, onSave, diagnostics, onCursorChange, readOnly = false },
  ref,
) {
  const dark = useIsDark()
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null)
  const monacoRef = useRef<Monaco | null>(null)
  // Kept in refs so the Monaco command/listener registered once at mount always
  // calls the LATEST callback instead of the one captured at mount time.
  const saveRef = useRef(onSave)
  const cursorRef = useRef(onCursorChange)
  useEffect(() => { saveRef.current = onSave }, [onSave])
  useEffect(() => { cursorRef.current = onCursorChange }, [onCursorChange])

  useImperativeHandle(ref, () => ({
    jumpToLine: (line: number) => {
      const ed = editorRef.current
      if (!ed) return
      const model = ed.getModel()
      if (!model) return
      const target = Math.max(1, Math.min(line, model.getLineCount()))
      ed.revealLineInCenter(target)
      ed.setPosition({ lineNumber: target, column: 1 })
      ed.focus()
    },
    focus: () => editorRef.current?.focus(),
  }), [])

  // Push compiler diagnostics into Monaco's marker store. Re-runs whenever the
  // diagnostics or the open file change; an empty list clears the previous set.
  useEffect(() => {
    const ed = editorRef.current
    const monaco = monacoRef.current
    if (!ed || !monaco) return
    const model = ed.getModel()
    if (!model) return
    const lineCount = model.getLineCount()
    monaco.editor.setModelMarkers(
      model,
      MARKER_OWNER,
      diagnostics
        // A diagnostic with no line, or one past the end of THIS file (the error
        // may belong to an \input-ed file), cannot be placed — dropping it here
        // is better than pinning it to line 1 and pointing at innocent source.
        .filter(d => d.line !== null && d.line >= 1 && d.line <= lineCount)
        .map(d => {
          const line = d.line as number
          return {
            severity: severityFor(monaco, d.level),
            message: d.message,
            startLineNumber: line,
            startColumn: 1,
            endLineNumber: line,
            endColumn: model.getLineMaxColumn(line),
          }
        }),
    )
  }, [diagnostics, path])

  const handleMount = useCallback((ed: editor.IStandaloneCodeEditor, monaco: Monaco) => {
    editorRef.current = ed
    monacoRef.current = monaco
    registerLatexLanguage(monaco)
    // Cmd/Ctrl+S compiles. Registered as a Monaco command so it fires while the
    // editor has focus without a document-level key listener that would also
    // swallow the browser's Save in every other pane.
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current())
    ed.onDidChangeCursorPosition(e =>
      cursorRef.current?.(e.position.lineNumber, e.position.column),
    )
  }, [])

  return (
    <div className="h-full w-full min-h-0 overflow-hidden" data-testid="papyrus-editor">
      <Suspense
        fallback={
          <div className="p-3 text-muted text-[13px] animate-pulse" role="status">
            {i18nT('apps.papyrus.editor.loading_editor')}
          </div>
        }
      >
        <Editor
          height="100%"
          path={path}
          language={monacoLanguage(path)}
          value={value}
          onChange={v => onChange(v ?? '')}
          onMount={handleMount}
          theme={dark ? 'vs-dark' : 'vs'}
          options={{
            readOnly,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 13,
            lineNumbers: 'on',
            wordWrap: 'on',
            automaticLayout: true,
            renderWhitespace: 'selection',
            padding: { top: 8, bottom: 8 },
            // A paper is prose: the ruler and bracket guides are noise here.
            rulers: [],
            guides: { bracketPairs: false, indentation: false },
            quickSuggestions: false,
            occurrencesHighlight: 'off',
          }}
        />
      </Suspense>
    </div>
  )
})

export default PapyrusEditor
