/**
 * Shared content rendering primitives — used by both the file viewer
 * (`MarkdownPanel`) and the artifact detail page. Decouples the
 * type-dispatch + Pierre code editor from the file-specific chrome
 * (overflow menu, save-to-disk, file watch) so artifact pages can render
 * markdown / text / json / svg without iframing them.
 *
 * Exports:
 *   - `ContentRenderer` — the type-dispatch body. Picks ImageViewer /
 *     CsvViewer / JsonViewer / HtmlViewer / PdfViewer / PierreEditor /
 *     MarkdownRenderer / PierreCode based on `fileType` + `editing`.
 *   - `CodeEditor` — Pierre-based editor for editable text content.
 *   - `extOf`, `langFor`, `wrapCode`, `MD_EXTS` — shared helpers used by
 *     both consumers to compute `displayContent` / `isMarkdown` etc.
 */
import { memo, useCallback, useMemo, useRef } from 'react'
import MarkdownRenderer, { BasePathCtx } from './MarkdownRenderer'
import { ImageViewer, CsvViewer, JsonViewer, JsonlViewer, HtmlViewer, PdfViewer, OfficeViewer, SheetViewer, SvgViewer, ExcalidrawViewer, MediaPlayer } from './FileRenderers'
import { PierreCode, PierreEditor, type PierreEditorHandle } from '../pierre'

import { i18nT } from '../i18n/t'

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

/** Pierre-based code editor for editable text content. */
export function CodeEditor({
  content, lang, lineNums, wordWrap, onChange, onSave, flush, editorRef, filePath, diffBase, diffSplit, diffExpandUnchanged,
}: {
  content: string
  lang: string
  lineNums: boolean
  wordWrap: boolean
  onChange: (v: string) => void
  /** Cmd/Ctrl+S inside the editor surface. */
  onSave?: () => void
  /** Drop the rounded border box — the host surface (e.g. a side-panel tab
   *  body) provides the frame, so content runs edge-to-edge. */
  flush?: boolean
  /** Imperative reveal/focus handle — how a host jumps to a cited line. */
  editorRef?: React.Ref<PierreEditorHandle>
  filePath?: string
  /** Live-diff editing: baseline contents to diff the buffer against while
   *  typing (`null` = new file). `undefined` renders the plain editor. */
  diffBase?: string | null
  /** Live-diff surface: show unchanged regions instead of folding them. */
  diffExpandUnchanged?: boolean
  /** Split vs unified layout for the live-diff surface. */
  diffSplit?: boolean
}) {
  // Pierre owns the buffer during an edit session: the file object is the
  // session's INITIAL state, so it must not change identity on every parent
  // re-render fed by our own onChange — that would restart the session per
  // keystroke and drop the caret. Keyed by path/lang/mode; a diff-mode toggle
  // deliberately restarts the session seeded from the CURRENT buffer so
  // unsaved edits carry across the flip.
  //
  // EXTERNAL content changes are the exception: a `content` value this editor
  // did not emit (Cancel restoring the on-disk text, refresh, a live-watch
  // reload) must actually show up, so the session reseeds — the seed counter
  // in the cacheKey remounts the editor on the new text (caret loss is
  // correct there).
  const initialRef = useRef<{ key: string; file: { name: string; contents: string; cacheKey: string } }>()
  const lastEmittedRef = useRef<string | null>(null)
  const lastContentRef = useRef<string | null>(null)
  const seedRef = useRef(0)
  const key = `${filePath ?? ''}:${lang}:${diffBase === undefined ? 'plain' : 'diff'}`
  const seedFile = () => ({
    key,
    file: {
      name: filePath?.split('/').pop() || `file.${lang}`,
      contents: content,
      cacheKey: `edit:${key}:${seedRef.current}`,
    },
  })
  if (initialRef.current?.key !== key) {
    initialRef.current = seedFile()
  } else if (content !== lastContentRef.current) {
    if (content === lastEmittedRef.current) {
      // Our own edit echoing back through the parent. Consume the marker: it
      // matches exactly ONE echo, so a LATER external change that happens to
      // restore this same text (Cancel, refresh, live-watch reload) is still
      // seen as external and still reseeds. Leaving it set made that case
      // silently keep the stale buffer, and the next save wrote it back.
      lastEmittedRef.current = null
    } else {
      // The prop changed to a value this editor did not emit — an external
      // change. (Compared against the last RECEIVED prop, not the initial seed:
      // Cancel restores the original text, which must still reseed.)
      seedRef.current++
      initialRef.current = seedFile()
    }
  }
  lastContentRef.current = content
  const handleChange = useCallback((v: string) => {
    lastEmittedRef.current = v
    onChange(v)
  }, [onChange])
  // Contents track the live buffer while the cacheKey stays pinned to the seed.
  // The editor owns its TextDocument and only rebuilds it when name/lang/
  // cacheKey change, so a fresh `contents` cannot disturb the caret — but the
  // File render DOES size its rows from `contents`, and a frozen value leaves
  // the row scaffolding one line short after an insert (the line below appears
  // to vanish) or one long after a delete (the row below duplicates).
  const liveFile = useMemo(
    () => ({ ...initialRef.current!.file, contents: content }),
    [content],
  )
  const options = useMemo(
    () => ({
      disableLineNumbers: !lineNums,
      overflow: (wordWrap ? 'wrap' : 'scroll') as 'wrap' | 'scroll',
    }),
    [lineNums, wordWrap],
  )
  return (
    <div className={`w-full h-full overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}>
      <PierreEditor
        key={initialRef.current.file.cacheKey}
        ref={editorRef}
        file={liveFile}
        options={options}
        onChange={handleChange}
        onSave={onSave}
        diffBase={diffBase}
        diffSplit={diffSplit}
        diffExpandUnchanged={diffExpandUnchanged}
      />
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
 * the Pierre editor work entirely from `content` and don't need a path.
 */
export const ContentRenderer = memo(function ContentRenderer({
  isRichType, fileType, filePath, content, editing,
  lang, lineNums, wordWrap, onChange, onSave,
  previewRef, displayContent, isMarkdown,
  markdownClassName, previewStyle, flush, editorRef, diffBase, diffSplit, diffExpandUnchanged,
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
  onChange: (v: string) => void
  /** Cmd/Ctrl+S inside the editor surface. */
  onSave?: () => void
  previewRef: React.RefObject<HTMLElement | null>
  displayContent: string
  isMarkdown: boolean
  markdownClassName?: string
  previewStyle?: React.CSSProperties
  /** Drop the rounded border boxes around the editor / code views — used when
   *  the host surface (side-panel tab body) already frames the content. */
  flush?: boolean
  /** Imperative reveal/focus handle for the editing surface — how a host jumps
   *  to a cited line (only live on the `editing` branch). */
  editorRef?: React.Ref<PierreEditorHandle>
  /** Live-diff editing baseline for the code-editor branch (`null` = new
   *  file). `undefined` renders the plain editor. */
  diffBase?: string | null
  /** Live-diff surface: show unchanged regions instead of folding them. */
  diffExpandUnchanged?: boolean
  /** Split vs unified layout for the live-diff editing surface. */
  diffSplit?: boolean
}) {
  const codeFile = useMemo(
    () => ({ name: filePath?.split('/').pop() || `file.${lang}`, contents: content }),
    [filePath, lang, content],
  )
  const codeOptions = useMemo(
    () => ({
      disableLineNumbers: !lineNums,
      overflow: (wordWrap ? 'wrap' : 'scroll') as 'wrap' | 'scroll',
    }),
    [lineNums, wordWrap],
  )
  const inner = (
    <>
      {isRichType && fileType === 'image' && filePath && <ImageViewer filePath={filePath} />}
      {isRichType && fileType === 'svg' && (
        editing ? (
          <div data-testid="svg-edit-split" className="flex h-full min-h-0 flex-col gap-2">
            <div className="flex shrink-0 flex-wrap items-baseline gap-x-2 gap-y-1 text-[12px]">
              <span className="font-medium text-accent">
                {i18nT('components.contentRenderer.editing_svg')}
              </span>
              <span className="text-muted">
                {i18nT('components.contentRenderer.changes_update_svg_preview_as_you_type')}
              </span>
            </div>
            <div
              className="grid min-h-0 flex-1 gap-3"
              style={{
                gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 360px), 1fr))',
                gridAutoRows: 'minmax(220px, 1fr)',
              }}
            >
              <section
                aria-label={i18nT('components.markdownPanel.preview')}
                className="flex min-h-0 flex-col gap-1.5"
              >
                <div className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  {i18nT('components.markdownPanel.preview')}
                </div>
                <div className="min-h-0 flex-1">
                  <SvgViewer content={content} />
                </div>
              </section>
              <section
                aria-label={i18nT('components.contentRenderer.svg_source_code')}
                className="flex min-h-0 flex-col gap-1.5"
              >
                <div className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  {i18nT('components.contentRenderer.svg_source_code')}
                </div>
                <div className="min-h-0 flex-1">
                  <CodeEditor
                    content={content}
                    lang="xml"
                    lineNums={lineNums}
                    wordWrap={wordWrap}
                    onChange={onChange}
                    onSave={onSave}
                    flush={flush}
                    filePath={filePath}
                  />
                </div>
              </section>
            </div>
          </div>
        ) : <SvgViewer content={content} />
      )}
      {isRichType && fileType === 'csv' && <CsvViewer content={content} filePath={filePath ?? ''} />}
      {isRichType && fileType === 'json' && <JsonViewer content={content} />}
      {isRichType && fileType === 'jsonl' && <JsonlViewer content={content} />}
      {isRichType && fileType === 'html' && <HtmlViewer content={content} />}
      {isRichType && fileType === 'pdf' && filePath && <PdfViewer filePath={filePath} />}
      {isRichType && fileType === 'sheet' && filePath && <SheetViewer filePath={filePath} />}
      {isRichType && fileType === 'office' && filePath && <OfficeViewer filePath={filePath} />}
      {isRichType && fileType === 'video' && filePath && <MediaPlayer filePath={filePath} kind="video" />}
      {isRichType && fileType === 'audio' && filePath && <MediaPlayer filePath={filePath} kind="audio" />}
      {isRichType && fileType === 'excalidraw' && <ExcalidrawViewer content={content} />}
      {!isRichType && editing && (
        <CodeEditor
          content={content}
          lang={lang}
          lineNums={lineNums}
          wordWrap={wordWrap}
          onChange={onChange}
          onSave={onSave}
          flush={flush}
          editorRef={editorRef}
          filePath={filePath}
          diffBase={diffBase}
          diffSplit={diffSplit}
          diffExpandUnchanged={diffExpandUnchanged}
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
        <div
          ref={previewRef as React.RefObject<HTMLDivElement>}
          className={`relative w-full h-full overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}
        >
          {/* previewRef is a QUERY root only (TreeWalker / selection
              containment for find and the comment flow), never scrolled, so it
              sits on this box while Pierre owns the scroller — which is what
              lets it window rows instead of rendering one per line. */}
          <PierreCode
            file={codeFile}
            langHint={lang}
            options={codeOptions}
            scrollClassName="absolute inset-0 overflow-auto pierre-surface"
          />
        </div>
      )}
    </>
  )
  return previewStyle ? <div className="h-full" style={previewStyle}>{inner}</div> : inner
})
