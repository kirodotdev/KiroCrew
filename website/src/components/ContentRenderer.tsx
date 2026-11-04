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
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import MarkdownRenderer, { BasePathCtx } from './MarkdownRenderer'
import { ImageViewer, CsvViewer, JsonViewer, JsonlViewer, HtmlViewer, PdfViewer, OfficeViewer, SheetViewer, SvgViewer, ExcalidrawViewer, MediaPlayer } from './FileRenderers'
import { PierreCode, type PierreEditorHandle } from '../pierre'
import { CodeEditor } from './CodeEditor'

export { CodeEditor } from './CodeEditor'

import { i18nT } from '../i18n/t'
import { highlightLanguageForExtension } from '../utils/highlightLanguages'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { isSailXmlFile } from '../languages/sail'
import { findSailUsages, openSailRef, sailRefAtEvent, sailRuleIndex, type SailUsage } from '../languages/sailNavigation'
import SailUsagesPopover from '../languages/SailUsagesPopover'
import { useAppSelector } from '../store'
import { selectActiveSlotProject } from '../store/chatSlice'

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
  // An edition extension is passed on as its token (`.foo` -> `foo`), not as the
  // language id: fenceLanguage then checks the core tables before edition
  // languages, so a core extension the edition also claims stays core.
  return map[ext] || (highlightLanguageForExtension(ext) ? ext.slice(1) : 'plaintext')
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
  onFileOpen, onFolderOpen,
}: {
  isRichType: boolean
  fileType: string
  /** Required for `image` / `pdf` (URL construction). Optional otherwise. */
  filePath?: string
  /** Where a path chip in the markdown preview opens a file or folder. Absent, a
   *  chip falls back to revealing the path on the host. */
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  onFolderOpen?: (path: string) => void
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Appian rule XML highlights as SAIL-in-XML (see languages/sail.ts); `.xml`
  // alone is a core extension, so the language is forced by file name/content.
  const sailXml = isSailXmlFile(filePath, content)
  // .java too: SAIL in Java strings (expression tests) navigates, and a
  // Java-backed function's declaration (FN_NAME / register("x")) finds usages.
  const sailish = sailXml || lang === 'sail' || /\.(sail|java)$/i.test(filePath ?? '')
  const codeFile = useMemo(
    () => ({ name: filePath?.split('/').pop() || `file.${lang}`, contents: content, ...(sailXml ? { lang: 'sail-xml' } : {}) }),
    [filePath, lang, content, sailXml],
  )
  // Cmd/Ctrl+click go-to-definition for SAIL references (languages/sailNavigation.ts).
  const project = useAppSelector(selectActiveSlotProject)
  // Pierre's editor reads Cmd/Ctrl+pointerdown as "add a cursor", and that
  // fires before `click`. So the gesture is claimed at POINTERDOWN, in React's
  // root capture phase (which runs before Pierre's listener on its content
  // element): stopping it there keeps Pierre from ever seeing it. The matching
  // mousedown (native caret move/selection) and click are swallowed too.
  // Warm the repo's rule index as soon as a SAIL file opens, so the first
  // Cmd+click is already instant.
  useEffect(() => { if (sailish && filePath) void sailRuleIndex(filePath) }, [sailish, filePath])
  const sailGestureRef = useRef(false)
  const onSailPointerDown = useCallback((e: React.PointerEvent) => {
    sailGestureRef.current = false
    if (!sailish || e.button !== 0 || !(e.metaKey || e.ctrlKey) || !filePath || !onFileOpen) return
    const ref = sailRefAtEvent(e.nativeEvent)
    if (!ref) return
    sailGestureRef.current = true
    e.preventDefault()
    e.stopPropagation()
    // IntelliJ model: Cmd+click a REFERENCE -> go to its declaration; Cmd+click
    // a DECLARATION (the rule's <name>, a rule input <name>, a `local!x:`
    // binding) -> the usages popup.
    if (ref.domain === 'decl' || ref.domain === 'localdecl' || ref.domain === 'jdecl') {
      const local = ref.domain === 'localdecl'
      const java = ref.domain === 'jdecl'
      const isRule = java || (!local && ref.name.toLowerCase() === filePath.split('/').pop()!.replace(/\.xml$/i, '').replace(/^SYSTEM_SYSRULES_/i, '').toLowerCase())
      const base = { name: ref.name, x: e.clientX, y: e.clientY, scope: (isRule ? 'repo' : 'file') as 'repo' | 'file', local, java }
      setUsages({ ...base, loading: true, usages: [], truncated: false })
      void findSailUsages(ref.name, { filePath, content, local, repo: java }).then(r => setUsages(u => (u && u.name === ref.name ? { ...base, loading: false, ...r } : u)))
      return
    }
    void openSailRef(ref, { filePath, content, project, onFileOpen })
  }, [sailish, filePath, onFileOpen, content, project])
  const [usages, setUsages] = useState<{ name: string; x: number; y: number; loading: boolean; usages: SailUsage[]; truncated: boolean; scope: 'repo' | 'file'; local: boolean; java?: boolean } | null>(null)
  const closeUsages = useCallback(() => setUsages(null), [])
  const swallowSailGesture = useCallback((e: React.MouseEvent) => {
    if (!sailGestureRef.current) return
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'click') sailGestureRef.current = false
  }, [])
  const sailHandlers = { onPointerDownCapture: onSailPointerDown, onMouseDownCapture: swallowSailGesture, onMouseUpCapture: swallowSailGesture, onClickCapture: swallowSailGesture }
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
        <div className="contents" {...sailHandlers}>
        <CodeEditor
          content={content}
          lang={lang}
          fileLang={sailXml ? 'sail-xml' : undefined}
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
        </div>
      )}
      {!isRichType && !editing && isMarkdown && (
        <div ref={previewRef as React.RefObject<HTMLDivElement>} className={markdownClassName ?? 'msg-content text-sm leading-relaxed'}>
          <BasePathCtx.Provider value={filePath || null}>
            <MarkdownRenderer content={displayContent} sourcePos onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} />
          </BasePathCtx.Provider>
        </div>
      )}
      {!isRichType && !editing && !isMarkdown && (
        <div
          ref={previewRef as React.RefObject<HTMLDivElement>}
          className={`relative w-full h-full overflow-hidden ${flush ? '' : 'border border-border rounded-md'}`}
          {...sailHandlers}
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
  const usagesPopover = usages && (
    <SailUsagesPopover
      name={usages.name} x={usages.x} y={usages.y} loading={usages.loading} usages={usages.usages} truncated={usages.truncated}
      scope={usages.scope} prefix={usages.local ? 'local' : usages.java ? '' : usages.scope === 'repo' ? 'a' : 'ri'} root={project}
      onOpen={(u) => { setUsages(null); onFileOpen?.(u.path, { line: u.line }) }}
      onClose={closeUsages}
    />
  )
  return previewStyle ? <div className="h-full" style={previewStyle}>{inner}{usagesPopover}</div> : <>{inner}{usagesPopover}</>
})
