import { memo, useState, useMemo, useCallback, useRef } from 'react'
import DOMPurify from 'dompurify'

import { i18nT } from '../i18n/t'
import { useI18nRevision } from '../i18n/useI18nRevision'
import { ExcalidrawBlock } from './ExcalidrawBlock'
/* ── extension helpers ── */
const IMG_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'])
const CSV_EXTS = new Set(['.csv', '.tsv'])
const JSON_EXTS = new Set(['.json'])
const JSONL_EXTS = new Set(['.jsonl'])
const HTML_EXTS = new Set(['.html', '.htm'])
const PDF_EXTS = new Set(['.pdf'])
// Excalidraw scene JSON. Without this the extension falls through to `code` and
// the user gets a wall of raw element JSON in Monaco instead of the diagram.
const EXCALIDRAW_EXTS = new Set(['.excalidraw'])

export type FileType = 'image' | 'svg' | 'csv' | 'json' | 'jsonl' | 'html' | 'pdf' | 'excalidraw' | 'code' | 'markdown'

/**
 * Map a filesystem path to a FileType. Note: SVG files (path-backed) are
 * detected as `image` because ImageViewer fetches them by URL. The `svg`
 * FileType value is reserved for content-string SVG rendering used by
 * artifacts (where there's no path to fetch from).
 */
export function detectFileType(filePath: string): FileType {
  const ext = extOf(filePath)
  if (IMG_EXTS.has(ext)) return 'image'
  if (CSV_EXTS.has(ext)) return 'csv'
  if (JSON_EXTS.has(ext)) return 'json'
  if (JSONL_EXTS.has(ext)) return 'jsonl'
  if (HTML_EXTS.has(ext)) return 'html'
  if (PDF_EXTS.has(ext)) return 'pdf'
  if (EXCALIDRAW_EXTS.has(ext)) return 'excalidraw'
  if (['.md', '.markdown', '.mdx', '.txt', ''].includes(ext)) return 'markdown'
  return 'code'
}

function extOf(fp: string) { const i = fp.lastIndexOf('.'); return i >= 0 ? fp.slice(i).toLowerCase() : '' }

/* ── Image viewer ── */
export const ImageViewer = memo(function ImageViewer({ filePath }: { filePath: string }) {
  useI18nRevision()
  return (
    <div className="flex items-center justify-center h-full overflow-auto p-4 bg-bg-elevated rounded-md border border-border">
      <img
        src={'/api/file-raw?path=' + encodeURIComponent(filePath)}
        alt={filePath.split('/').pop()}
        className="max-w-full max-h-full object-contain"
        draggable={false}
      />
    </div>
  )
})

/* ── Inline SVG viewer (content-string-based) ─────────────────────────────
 * Used for artifact `kind: svg` where the SVG XML lives in the artifact
 * content, not on disk. DOMPurify with the SVG profile strips dangerous
 * elements (script, foreignObject) while preserving normal SVG markup. */
export const SvgViewer = memo(function SvgViewer({ content }: { content: string }) {
  const safe = useMemo(
    () => DOMPurify.sanitize(content, { USE_PROFILES: { svg: true, svgFilters: true } }),
    [content],
  )
  return (
    <div
      className="flex items-center justify-center h-full overflow-auto p-4 bg-bg-elevated rounded-md border border-border"
      dangerouslySetInnerHTML={{ __html: safe }}
    />
  )
})

/* ── Excalidraw scene viewer (content-string-based) ───────────────────────
 * Reuses the same ExcalidrawBlock the chat fence renders through, so both
 * surfaces share one renderer and stay in sync. Read-only: opening a scene
 * never mutates the file on disk. */
export const ExcalidrawViewer = memo(function ExcalidrawViewer({ content }: { content: string }) {
  return (
    <div className="h-full overflow-auto p-4 bg-bg-elevated rounded-md border border-border">
      <ExcalidrawBlock code={content} className="flex justify-center min-h-[60px]" />
    </div>
  )
})

/* ── CSV table viewer ── */
export const CsvViewer = memo(function CsvViewer({ content, filePath }: { content: string; filePath: string }) {
  const delimiter = extOf(filePath) === '.tsv' ? '\t' : ','
  const rows = useMemo(() => {
    const lines = content.split('\n').filter(l => l.trim())
    return lines.map(line => {
      const cells: string[] = []
      let cur = '', inQuote = false
      for (let i = 0; i < line.length; i++) {
        const ch = line[i]
        if (ch === '"') {
          if (inQuote && line[i + 1] === '"') { cur += '"'; i++; continue }
          inQuote = !inQuote; continue
        }
        if (ch === delimiter && !inQuote) { cells.push(cur); cur = ''; continue }
        cur += ch
      }
      cells.push(cur)
      return cells
    })
  }, [content, delimiter])

  if (rows.length === 0) return <div className="text-muted text-sm">{i18nT('components.fileRenderers.empty_file')}</div>
  const [header, ...body] = rows

  return (
    <div className="h-full overflow-auto border border-border rounded-md bg-bg-elevated">
      <table className="w-full text-sm font-mono border-collapse">
        <thead className="sticky top-0 bg-chrome">
          <tr>{header.map((h, i) => <th key={i} className="px-3 py-2 text-left text-[11px] font-semibold text-muted border-b border-border whitespace-nowrap">{h}</th>)}</tr>
        </thead>
        <tbody>
          {body.slice(0, 500).map((row, ri) => (
            <tr key={ri} className="hover:bg-bg-hover">
              {row.map((cell, ci) => <td key={ci} className="px-3 py-1.5 text-text border-b border-border/50 whitespace-nowrap">{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {body.length > 500 && <div className="text-center text-muted text-[11px] py-2">{i18nT('components.fileRenderers.showing_500_of')} {body.length} {i18nT('components.fileRenderers.rows')}</div>}
    </div>
  )
})

/* ── JSON tree viewer ── */
export const JsonViewer = memo(function JsonViewer({ content }: { content: string }) {
  const parsed = useMemo(() => {
    try { return { ok: true as const, value: JSON.parse(content) } }
    catch (e) { return { ok: false as const, error: e instanceof Error ? e.message : String(e) } }
  }, [content])
  if (!parsed.ok) {
    // Surface the actual parse error and a content preview so failures are
    // diagnosable instead of a bare "Invalid JSON" with no clue why.
    const preview = content.slice(0, 2000)
    return (
      <div className="h-full overflow-auto p-3 bg-bg-elevated border border-border rounded-md text-sm">
        <div className="text-danger font-semibold font-mono mb-2">{i18nT('components.fileRenderers.invalid_json')} {parsed.error}</div>
        <div className="text-[11px] text-muted mb-1 font-mono">{content.length > preview.length ? i18nT('components.fileRenderers.showing_raw_content_truncated_count', { count: content.length, shown: preview.length }) : i18nT('components.fileRenderers.showing_raw_content_count', { count: content.length })}</div>
        <pre className="text-[13px] font-mono whitespace-pre-wrap break-all text-text">{preview}{content.length > preview.length ? '\n…' : ''}</pre>
      </div>
    )
  }
  return (
    <div className="h-full overflow-auto p-3 bg-bg-elevated border border-border rounded-md font-mono text-sm">
      <JsonNode value={parsed.value} depth={0} />
    </div>
  )
})

function JsonNode({ value, depth }: { value: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 2)
  if (value === null) return <span className="text-muted">{i18nT('components.fileRenderers.null')}</span>
  if (typeof value === 'boolean') return <span className="text-ok">{String(value)}</span>
  if (typeof value === 'number') return <span className="text-accent">{value}</span>
  if (typeof value === 'string') return <span className="text-warning">"{value.length > 200 ? value.slice(0, 200) + '…' : value}"</span>

  const isArr = Array.isArray(value)
  const entries = isArr ? (value as unknown[]).map((v, i) => [i, v] as const) : Object.entries(value as Record<string, unknown>)
  const bracket = isArr ? ['[', ']'] : ['{', '}']

  return (
    <span>
      <button className="text-muted hover:text-text cursor-pointer bg-transparent border-none font-mono text-sm" onClick={() => setOpen(!open)}>
        {open ? '▼' : '▶'} {bracket[0]}{!open && <span className="text-muted"> {entries.length} {i18nT('components.fileRenderers.items')} {bracket[1]}</span>}
      </button>
      {open && (
        <div style={{ paddingLeft: 16 }}>
          {entries.slice(0, 200).map(([k, v]) => (
            <div key={String(k)}>
              {!isArr && <span className="text-accent">"{String(k)}"</span>}
              {!isArr && <span className="text-muted">: </span>}
              <JsonNode value={v} depth={depth + 1} />
            </div>
          ))}
          {entries.length > 200 && <div className="text-muted">… {entries.length - 200} {i18nT('components.fileRenderers.more')}</div>}
        </div>
      )}
      {open && <span className="text-muted">{bracket[1]}</span>}
    </span>
  )
}

/* ── JSONL viewer (reuses JsonViewer per line) ── */
const JSONL_PAGE_SIZE = 100

export const JsonlViewer = memo(function JsonlViewer({ content }: { content: string }) {
  const lines = useMemo(() => content.split('\n').filter(l => l.trim()), [content])
  const [visible, setVisible] = useState(JSONL_PAGE_SIZE)
  const containerRef = useRef<HTMLDivElement>(null)

  const onScroll = useCallback(() => {
    const el = containerRef.current
    if (!el || visible >= lines.length) return
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 100) {
      setVisible(v => Math.min(v + JSONL_PAGE_SIZE, lines.length))
    }
  }, [visible, lines.length])

  return (
    <div ref={containerRef} onScroll={onScroll} className="h-full overflow-auto p-3 bg-bg-elevated border border-border rounded-md space-y-2">
      <div className="text-muted text-[11px]">{lines.length} {i18nT('components.fileRenderers.lines')}</div>
      {lines.slice(0, visible).map((line, i) => (
        <div key={i} className="border-b border-border/50 pb-2">
          <JsonViewer content={line} />
        </div>
      ))}
      {visible < lines.length && (
        <div className="text-muted text-xs text-center py-2">{i18nT('components.fileRenderers.scroll_for_more_count', { count: lines.length - visible })}</div>
      )}
    </div>
  )
})

/* ── HTML preview (sandboxed iframe) ── */
export const HtmlViewer = memo(function HtmlViewer({ content }: { content: string }) {
  return (
    <div className="h-full border border-border rounded-md overflow-hidden bg-white">
      <iframe
        srcDoc={content}
        sandbox=""
        className="w-full h-full border-none"
        title={i18nT('components.fileRenderers.html_preview')}
      />
    </div>
  )
})

/* ── PDF viewer (embedded + fallback open externally) ── */
export const PdfViewer = memo(function PdfViewer({ filePath }: { filePath: string }) {
  const url = '/api/file-raw?path=' + encodeURIComponent(filePath)
  return (
    <div className="h-full border border-border rounded-md overflow-hidden bg-white flex flex-col">
      <iframe src={url} className="flex-1 w-full border-none" title={i18nT('components.fileRenderers.pdf_preview')} />
      <div className="flex justify-end p-1 bg-chrome border-t border-border">
        <button
          className="px-2 py-1 rounded text-[11px] text-muted hover:text-text cursor-pointer bg-transparent border-none"
          onClick={() => window.open(url, '_blank')}
        >{i18nT('components.fileRenderers.open_in_new_tab')}</button>
      </div>
    </div>
  )
})
