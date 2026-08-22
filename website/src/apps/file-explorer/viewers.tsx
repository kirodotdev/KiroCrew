import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, FileQuestion } from 'lucide-react'

import { EmptyState } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fileExplorerApi } from './api'
import SlideCanvas from './SlideCanvas'
import type { DocxBlock, FileMeta, OfficeExtract, XlsxSheet } from './types'
import { basename, formatBytes } from './utils'

/** EMU per typographic point — converts slide width to the font-scaling base. */
const EMU_PER_POINT = 12700

function ToggleBar<T extends string | number>({ options, value, onChange }: {
  options: Array<[T, string]>
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div style={{ display: 'flex', gap: 4, padding: '4px 8px', borderBottom: '1px solid var(--border)', flex: '0 0 auto', flexWrap: 'wrap' }}>
      {options.map(([key, label]) => (
        <button
          key={String(key)}
          onClick={() => onChange(key)}
          style={{ fontSize: 11, padding: '3px 10px', borderRadius: 4, border: '1px solid var(--border)', background: value === key ? 'color-mix(in srgb, var(--accent) 18%, transparent)' : 'transparent', color: value === key ? 'var(--accent)' : 'var(--muted)', cursor: 'pointer' }}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function SourceView({ content }: { content: string }) {
  return (
    <pre style={{ margin: 0, padding: 16, fontSize: 12, lineHeight: 1.5, overflow: 'auto', height: '100%', boxSizing: 'border-box' }}>
      <code>{content}</code>
    </pre>
  )
}

export function DownloadButton({ path }: { path: string }) {
  return (
    <a
      href={fileExplorerApi.rawUrl(path, true)}
      download
      style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 14px', textDecoration: 'none', color: 'var(--accent)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 12 }}
    >
      <Download size={12} /> {i18nT('apps.fileExplorer.fileViewer.download')}
    </a>
  )
}

export function BinaryFallback({ path, fileMeta }: { path: string; fileMeta: FileMeta }) {
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12 }}>
      <EmptyState icon={<FileQuestion size={22} />} title={i18nT('apps.fileExplorer.fileViewer.binary_file')} subtitle={`${formatBytes(fileMeta.size)} · ${fileMeta.mime || 'unknown'}`} />
      <DownloadButton path={path} />
    </div>
  )
}

export function PdfViewer({ path }: { path: string }) {
  return <iframe src={fileExplorerApi.rawUrl(path)} title={basename(path)} style={{ width: '100%', height: '100%', border: 0, background: '#3a3d41' }} />
}

export function ImageViewer({ path }: { path: string }) {
  return (
    <div className="mc-fe-img-wrap">
      <img src={fileExplorerApi.rawUrl(path)} alt={basename(path)} style={{ maxWidth: '100%', maxHeight: '100%' }} />
    </div>
  )
}

export function MediaViewer({ path, kind }: { path: string; kind: 'audio' | 'video' }) {
  if (kind === 'video') {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', background: '#000' }}>
        {/* Local user files carry no caption tracks to offer. */}
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video controls src={fileExplorerApi.rawUrl(path)} aria-label={basename(path)} style={{ maxWidth: '100%', maxHeight: '100%' }} />
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 12 }}>
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio controls src={fileExplorerApi.rawUrl(path)} aria-label={basename(path)} />
      <DownloadButton path={path} />
    </div>
  )
}

/** Static HTML preview in a fully inert sandboxed iframe — no scripts, no
 * popups, no modals: a saved page is untrusted content and script access
 * could exfiltrate the rendered document. Interactive pages show as layout
 * only; the source toggle carries the rest. */
export function HtmlViewer({ content }: { content: string }) {
  const [mode, setMode] = useState<'preview' | 'source'>('preview')
  const blobUrl = useMemo(() => URL.createObjectURL(new Blob([content || ''], { type: 'text/html' })), [content])
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ToggleBar
        options={[['preview', i18nT('apps.fileExplorer.viewers.preview')], ['source', i18nT('apps.fileExplorer.viewers.source')]]}
        value={mode}
        onChange={setMode}
      />
      <div style={{ flex: 1, minHeight: 0, overflow: mode === 'preview' ? 'hidden' : 'auto' }}>
        {mode === 'preview'
          ? <iframe src={blobUrl} sandbox="" title={i18nT('apps.fileExplorer.viewers.preview')} style={{ width: '100%', height: '100%', border: 0, background: '#fff' }} />
          : <SourceView content={content} />}
      </div>
    </div>
  )
}

/** Minimal CSV/TSV parser handling quoted fields and escaped quotes. */
export function parseDelimited(text: string, delim: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let cur = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cur += '"'; i++ } else quoted = false
      } else cur += ch
    } else if (ch === '"') quoted = true
    else if (ch === delim) { row.push(cur); cur = '' }
    else if (ch === '\n') { row.push(cur); rows.push(row); row = []; cur = '' }
    else if (ch !== '\r') cur += ch
  }
  if (cur !== '' || row.length) { row.push(cur); rows.push(row) }
  return rows
}

const MAX_GRID_ROWS = 1000
const MAX_GRID_COLS = 200
const MAX_GRID_CELLS = 200_000

export function DataGrid({ rows, truncated }: { rows: string[][]; truncated?: boolean }) {
  // Row, column, and total-cell caps: a 4 MB single-row stream would
  // otherwise materialize millions of DOM nodes and hang the tab.
  let clipped = false
  const shown: string[][] = []
  let cells = 0
  for (const row of rows.slice(0, MAX_GRID_ROWS)) {
    if (cells >= MAX_GRID_CELLS) { clipped = true; break }
    if (row.length > MAX_GRID_COLS) clipped = true
    const slice = row.slice(0, Math.min(MAX_GRID_COLS, MAX_GRID_CELLS - cells))
    cells += slice.length
    shown.push(slice)
  }
  return (
    <div style={{ overflow: 'auto', maxHeight: '100%' }}>
      <table style={{ borderCollapse: 'collapse', fontSize: 12, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
        <tbody>
          {shown.map((row, r) => (
            <tr key={r}>
              {row.map((cell, c) => {
                const Tag = r === 0 ? 'th' : 'td'
                return (
                  <Tag key={c} style={{ border: '1px solid var(--border)', padding: '3px 8px', textAlign: 'left', whiteSpace: 'nowrap', maxWidth: 420, overflow: 'hidden', textOverflow: 'ellipsis', position: r === 0 ? 'sticky' : undefined, top: r === 0 ? 0 : undefined, background: r === 0 ? 'var(--bg, inherit)' : undefined }}>
                    {String(cell ?? '')}
                  </Tag>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {(rows.length > MAX_GRID_ROWS || clipped || truncated) && (
        <div style={{ padding: 8, fontSize: 11, color: 'var(--muted)' }}>
          {i18nT('apps.fileExplorer.viewers.showing_first_rows', { count: Math.min(rows.length, MAX_GRID_ROWS) })}
        </div>
      )}
    </div>
  )
}

export function DelimitedViewer({ content, delim }: { content: string; delim: string }) {
  const [mode, setMode] = useState<'table' | 'source'>('table')
  const rows = useMemo(() => parseDelimited(content || '', delim), [content, delim])
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <ToggleBar
        options={[['table', i18nT('apps.fileExplorer.viewers.table')], ['source', i18nT('apps.fileExplorer.viewers.source')]]}
        value={mode}
        onChange={setMode}
      />
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {mode === 'table' ? <DataGrid rows={rows} /> : <SourceView content={content} />}
      </div>
    </div>
  )
}

function DocxView({ blocks }: { blocks: DocxBlock[] }) {
  return (
    <div style={{ height: '100%', overflow: 'auto' }}>
      <div style={{ maxWidth: 860, margin: '0 auto', padding: '24px 28px', fontSize: 14, lineHeight: 1.65 }}>
        {blocks.map((block, i) => {
          if (block.type === 'table' && block.rows) {
            return <div key={i} style={{ margin: '12px 0' }}><DataGrid rows={block.rows} /></div>
          }
          if (block.type.startsWith('h')) {
            const level = Math.min(parseInt(block.type.slice(1) || '1', 10) || 1, 6)
            return <div key={i} style={{ fontWeight: 600, fontSize: Math.max(24 - 2 * level, 14), margin: `${level === 1 ? 18 : 12}px 0 6px` }}>{block.text}</div>
          }
          return <p key={i} style={{ margin: '6px 0' }}>{block.text}</p>
        })}
      </div>
    </div>
  )
}

function XlsxView({ sheets }: { sheets: XlsxSheet[] }) {
  const [active, setActive] = useState(0)
  const sheet = sheets[Math.min(active, Math.max(sheets.length - 1, 0))]
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {sheets.length > 1 && (
        <ToggleBar options={sheets.map((s, i) => [i, s.name] as [number, string])} value={active} onChange={setActive} />
      )}
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {sheet
          ? <DataGrid rows={sheet.rows} truncated={sheet.truncated} />
          : <div style={{ padding: 16, color: 'var(--muted)' }}>{i18nT('apps.fileExplorer.viewers.empty_workbook')}</div>}
      </div>
    </div>
  )
}

function PptxView({ data, path }: { data: OfficeExtract; path: string }) {
  const slides = data.slides || []
  const widthPt = (data.slideW || 12192000) / EMU_PER_POINT
  const ratio = (data.slideW || 12192000) / (data.slideH || 6858000)
  return (
    <div style={{ height: '100%', overflow: 'auto', padding: 16 }}>
      {slides.map((slide) => (
        <div key={slide.n} style={{ maxWidth: 980, margin: '0 auto 18px' }}>
          <div style={{ fontSize: 11, color: 'var(--muted)', margin: '0 0 4px 2px' }}>
            {i18nT('apps.fileExplorer.viewers.slide_n', { n: slide.n })}
          </div>
          {(slide.shapes || []).length > 0
            ? <SlideCanvas slide={slide} path={path} widthPt={widthPt} ratio={ratio} />
            : (
              <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '14px 18px' }}>
                {(slide.lines || []).map((line, i) => (
                  <div key={i} style={{ fontSize: i === 0 ? 16 : 13, fontWeight: i === 0 ? 600 : 400, margin: '3px 0' }}>{line}</div>
                ))}
              </div>
            )}
        </div>
      ))}
    </div>
  )
}

/** A failed /extract carries a JSON `{"error": …}` body — show the sentence,
 * not the serialized envelope. */
export function extractErrorText(error: unknown): string {
  const raw = String((error as Error | null)?.message || '')
  try {
    const parsed = JSON.parse(raw) as { error?: string }
    if (parsed && typeof parsed.error === 'string') return parsed.error
  } catch {
    // Not JSON: the message is already plain text.
  }
  return raw
}

export function OfficeViewer({ path }: { path: string }) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['file-explorer', 'extract', path],
    queryFn: () => fileExplorerApi.extract(path),
    staleTime: 30_000,
    retry: 1,
  })
  if (isLoading) {
    return <div style={{ padding: 16, color: 'var(--muted)', fontSize: 12 }}>{i18nT('apps.fileExplorer.viewers.extracting_document')}</div>
  }
  if (error || !data) {
    return <EmptyState icon={<FileQuestion size={22} />} title={i18nT('apps.fileExplorer.viewers.could_not_extract')} subtitle={extractErrorText(error)} />
  }
  if (data.kind === 'docx') return <DocxView blocks={data.blocks || []} />
  if (data.kind === 'xlsx') return <XlsxView sheets={data.sheets || []} />
  return <PptxView data={data} path={path} />
}
