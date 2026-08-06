/**
 * Shared content rendering primitives — used by both the file viewer
 * (`MarkdownPanel`) and the artifact detail page. Decouples the
 * type-dispatch + Monaco code editor from the file-specific chrome
 * (overflow menu, save-to-disk, file watch) so artifact pages can render
 * markdown / text / json / svg without iframing them.
 *
 * Exports:
 *   - `ContentRenderer` — the type-dispatch body. Picks ImageViewer /
 *     CsvViewer / JsonViewer / HtmlViewer / PdfViewer / Monaco editor /
 *     MarkdownRenderer / hljs based on `fileType` + `editing`.
 *   - `CodeEditor` — Monaco-based editor for editable text content.
 *   - `extOf`, `langFor`, `wrapCode`, `MD_EXTS` — shared helpers used by
 *     both consumers to compute `displayContent` / `isMarkdown` etc.
 */
import { memo, lazy, Suspense, useMemo } from 'react'
import DOMPurify from 'dompurify'
import type { Monaco } from '@monaco-editor/react'
import type { editor } from 'monaco-editor'
import MarkdownRenderer, { BasePathCtx } from './MarkdownRenderer'
import { ImageViewer, CsvViewer, JsonViewer, JsonlViewer, HtmlViewer, PdfViewer, SvgViewer, ExcalidrawViewer } from './FileRenderers'
import { monacoLang, useIsDark } from './MonacoCodeBlock'
import { kirocrewDark, kirocrewLight } from './monacoTheme'

import { i18nT } from '../i18n/t'
// Route through ensureMonacoLocal() so Monaco loads from the locally-bundled
// package instead of the default cdn.jsdelivr.net loader (blocked by CSP
// connect-src). Mirrors DiffPanel/MarkdownPanel/MonacoCodeBlock.
const MonacoEditor = lazy(async () => {
  const { ensureMonacoLocal } = await import('../utils/monacoLocal')
  await ensureMonacoLocal()
  return import('@monaco-editor/react')
})

export const MD_EXTS = new Set(['.md', '.markdown', '.mdx', '.txt', ''])

export function extOf(fp: string): string {
  const i = fp.lastIndexOf('.')
  return i >= 0 ? fp.slice(i).toLowerCase() : ''
}

export function wrapCode(content: string, ext: string): string {
  const lang = ext.replace('.', '')
  return '~~~' + lang + '\n' + content + '\n~~~'
}

export function langFor(ext: string): string {
  const map: Record<string, string> = {
    '.ts': 'typescript', '.tsx': 'typescript', '.js': 'javascript', '.jsx': 'javascript',
    '.py': 'python', '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
    '.sh': 'bash', '.css': 'css', '.html': 'html', '.md': 'markdown',
    '.rs': 'rust', '.go': 'go', '.java': 'java', '.kt': 'kotlin',
    '.rb': 'ruby', '.sql': 'sql', '.xml': 'xml', '.toml': 'ini', '.cfg': 'ini',
  }
  return map[ext] || 'plaintext'
}

let themesRegistered = false

/** Monaco-based code editor for editable text content. */
export function CodeEditor({
  content, lang, lineNums, wordWrap, autocomplete, onChange, flush, onEditorMount,
}: {
  content: string
  lang: string
  lineNums: boolean
  wordWrap: boolean
  autocomplete: boolean
  onChange: (v: string) => void
  /** Drop the rounded border box — the host surface (e.g. a side-panel tab
   *  body) provides the frame, so content runs edge-to-edge. */
  flush?: boolean
  /**
   * The editor instance and the monaco namespace, once mounted.
   *
   * The one escape hatch out of this component, so a host can reveal or
   * decorate a line without owning the editor's configuration. Monaco is
   * lazy-loaded, so this is also the readiness signal: there is no earlier
   * point at which a caller could reach a model. Mirrors how
   * `PapyrusEditor` reaches the same instance.
   */
  onEditorMount?: (ed: editor.IStandaloneCodeEditor, monaco: Monaco) => void
}) {
  const dark = useIsDark()
  const monoFont = useMemo(
    () => getComputedStyle(document.documentElement).getPropertyValue('--mono').trim() || 'monospace',
    [],
  )
  return (
    <div className={`w-full h-full overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}>
      <Suspense fallback={<div className="p-3 text-muted text-[12px] animate-pulse">{i18nT('components.contentRenderer.loading_editor')}</div>}>
        <MonacoEditor
          height="100%"
          language={monacoLang(lang)}
          value={content}
          onChange={v => onChange(v ?? '')}
          onMount={onEditorMount}
          beforeMount={(monaco) => {
            if (!themesRegistered) {
              monaco.editor.defineTheme('kirocrew-dark', kirocrewDark)
              monaco.editor.defineTheme('kirocrew-light', kirocrewLight)
              themesRegistered = true
            }
          }}
          theme={dark ? 'kirocrew-dark' : 'kirocrew-light'}
          options={{
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 13,
            fontFamily: monoFont,
            lineNumbers: lineNums ? 'on' : 'off',
            readOnly: false,
            wordWrap: wordWrap ? 'on' : 'off',
            quickSuggestions: autocomplete,
            suggestOnTriggerCharacters: autocomplete,
            wordBasedSuggestions: autocomplete ? 'currentDocument' : 'off',
            parameterHints: { enabled: autocomplete },
            automaticLayout: true,
            padding: { top: 8, bottom: 8 },
            hover: { enabled: true },
            // No red error squiggles by default — the panel editor is for
            // quick edits, not full IDE diagnostics.
            renderValidationDecorations: 'off',
            // No indent guide lines — visual noise in a quick-edit panel.
            guides: { indentation: false },
            // No sticky scroll (enclosing function pinned at the top).
            stickyScroll: { enabled: false },
            // No current-line highlight box.
            renderLineHighlight: 'none',
          }}
        />
      </Suspense>
    </div>
  )
}

/**
 * Type-dispatching content renderer. Used by both the file viewer (with
 * `filePath` for image/pdf URL construction) and the artifact detail page
 * (which passes only `content` for markdown/text/json/svg artifacts).
 *
 * Image and PDF rendering require a `filePath` because they fetch raw
 * bytes via `/api/file-raw?path=`. Markdown, csv, json, html, code, and
 * the Monaco editor work entirely from `content` and don't need a path.
 */
export const ContentRenderer = memo(function ContentRenderer({
  isRichType, fileType, filePath, content, editing,
  lang, lineNums, wordWrap, autocomplete, onChange,
  previewRef, displayContent, isMarkdown, highlightedHtml,
  gutterReadRef, markdownClassName, previewStyle, flush, onEditorMount,
}: {
  isRichType: boolean
  fileType: string
  /** Required for `image` / `pdf` (URL construction). Optional otherwise. */
  filePath?: string
  content: string
  editing: boolean
  lang: string
  lineNums: boolean
  wordWrap: boolean
  autocomplete: boolean
  onChange: (v: string) => void
  previewRef: React.RefObject<HTMLElement | null>
  displayContent: string
  isMarkdown: boolean
  highlightedHtml: string
  gutterReadRef?: React.RefObject<HTMLDivElement | null>
  markdownClassName?: string
  previewStyle?: React.CSSProperties
  /** Drop the rounded border boxes around the editor / code views — used when
   *  the host surface (side-panel tab body) already frames the content. */
  flush?: boolean
  /** Forwarded to `CodeEditor` — only fires on the `editing` (Monaco) branch,
   *  which is why a host that needs a line reveal forces source mode. */
  onEditorMount?: (ed: editor.IStandaloneCodeEditor, monaco: Monaco) => void
}) {
  const inner = (
    <>
      {isRichType && fileType === 'image' && filePath && <ImageViewer filePath={filePath} />}
      {isRichType && fileType === 'svg' && <SvgViewer content={content} />}
      {isRichType && fileType === 'csv' && <CsvViewer content={content} filePath={filePath ?? ''} />}
      {isRichType && fileType === 'json' && <JsonViewer content={content} />}
      {isRichType && fileType === 'jsonl' && <JsonlViewer content={content} />}
      {isRichType && fileType === 'html' && <HtmlViewer content={content} />}
      {isRichType && fileType === 'pdf' && filePath && <PdfViewer filePath={filePath} />}
      {isRichType && fileType === 'excalidraw' && <ExcalidrawViewer content={content} />}
      {!isRichType && editing && (
        <CodeEditor
          content={content}
          lang={lang}
          lineNums={lineNums}
          wordWrap={wordWrap}
          autocomplete={autocomplete}
          onChange={onChange}
          flush={flush}
          onEditorMount={onEditorMount}
        />
      )}
      {!isRichType && !editing && isMarkdown && (
        <div ref={previewRef as React.RefObject<HTMLDivElement>} className={markdownClassName ?? 'msg-content text-sm leading-relaxed'}>
          <BasePathCtx.Provider value={filePath || null}>
            <MarkdownRenderer content={displayContent} sourcePos />
          </BasePathCtx.Provider>
        </div>
      )}
      {!isRichType && !editing && !isMarkdown && (
        <div className={`relative w-full h-full font-mono text-[13px] leading-[18px] overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}>
          {lineNums && (
            <div ref={gutterReadRef as React.RefObject<HTMLDivElement>} className="absolute left-0 top-0 bottom-0 w-[3em] text-right pr-2 pt-3 text-[13px] text-muted select-none overflow-hidden z-10 leading-[18px]" style={{ fontFamily: "Menlo, Monaco, 'Courier New', monospace" }}>
              {Array.from({ length: content.split('\n').length }, (_, i) => <div key={i}>{i + 1}</div>)}
            </div>
          )}
          {/* previewRef also lands here (not just on the markdown branch) so a
              selection in a text/json/svg body has a root to map back to source:
              a <pre>'s textContent equals the source exactly (highlighting only
              wraps spans), so rendered-text offsets are source offsets. */}
          <pre
            ref={previewRef as React.RefObject<HTMLPreElement>}
            className="absolute inset-0 p-3 m-0 overflow-auto whitespace-pre"
            style={{ paddingLeft: lineNums ? 'calc(3em + 12px)' : undefined }}
            onScroll={gutterReadRef ? (e => { if (gutterReadRef.current) gutterReadRef.current.scrollTop = (e.target as HTMLElement).scrollTop }) : undefined}
            dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(highlightedHtml) }}
          />
        </div>
      )}
    </>
  )
  return previewStyle ? <div className="h-full" style={previewStyle}>{inner}</div> : inner
})
