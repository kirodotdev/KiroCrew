import { memo, useState, useMemo, useCallback, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, ExternalLink, FileText, Film, Music } from 'lucide-react'
import DOMPurify from 'dompurify'

import { i18nT } from '../i18n/t'
import { ExcalidrawBlock } from './ExcalidrawBlock'
import { useCanOpenFile, useCopyAck } from './FilePathMenu'
import { fileDownloadUrl, fileStreamUrl, fileOfficePreviewUrl } from '../utils/fileReadUrl'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
/* ── extension helpers ── */
const IMG_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'])
const CSV_EXTS = new Set(['.csv', '.tsv'])
// Media served through /api/file-stream (Range-capable). Split decides the
// element: <video> renders a picture surface, <audio> a compact control bar.
// .ogg goes to audio -- the extension is overwhelmingly audio in practice and
// the .ogv variant exists for video.
const VIDEO_EXTS = new Set(['.mp4', '.m4v', '.webm', '.mov', '.mkv', '.ogv'])
const AUDIO_EXTS = new Set(['.mp3', '.wav', '.m4a', '.flac', '.ogg', '.oga'])
const JSON_EXTS = new Set(['.json'])
const JSONL_EXTS = new Set(['.jsonl'])
const HTML_EXTS = new Set(['.html', '.htm'])
const PDF_EXTS = new Set(['.pdf'])
// Excalidraw scene JSON. Without this the extension falls through to `code` and
// the user gets a wall of raw element JSON in the code editor instead of the diagram.
const EXCALIDRAW_EXTS = new Set(['.excalidraw'])
// OOXML spreadsheets rendered inline by SheetViewer via /api/file-sheet
// (server-side openpyxl parse). Legacy .xls (OLE compound) and ODF .ods stay
// on the OfficeViewer download card — openpyxl reads neither.
const SHEET_EXTS = new Set(['.xlsx', '.xlsm'])
// Office binary formats (ZIP-based OOXML + legacy OLE + ODF). These cannot be
// rendered inline by browsers and produce garbled output when routed through
// /api/file-read (which decodes as UTF-8 with errors='replace'). OfficeViewer
// surfaces a download link instead. .pdf is intentionally excluded — it has
// its own PdfViewer that iframes /api/file-raw using the browser's built-in
// PDF renderer.
const OFFICE_EXTS = new Set([
  '.docx', '.doc',
  '.xls',
  '.pptx', '.ppt',
  '.odt', '.ods', '.odp',
])

export type FileType = 'image' | 'svg' | 'csv' | 'json' | 'jsonl' | 'html' | 'pdf' | 'excalidraw' | 'video' | 'audio' | 'sheet' | 'office' | 'code' | 'markdown'

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
  if (VIDEO_EXTS.has(ext)) return 'video'
  if (AUDIO_EXTS.has(ext)) return 'audio'
  if (SHEET_EXTS.has(ext)) return 'sheet'
  if (OFFICE_EXTS.has(ext)) return 'office'
  if (['.md', '.markdown', '.mdx', '.txt', ''].includes(ext)) return 'markdown'
  return 'code'
}

function extOf(fp: string) { const i = fp.lastIndexOf('.'); return i >= 0 ? fp.slice(i).toLowerCase() : '' }

/* ── Image viewer ── */
export const ImageViewer = memo(function ImageViewer({ filePath }: { filePath: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  return (
    <div className="h-full overflow-auto p-4 bg-bg-elevated rounded-md border border-border">
      <ExcalidrawBlock code={content} className="flex justify-center min-h-[60px]" />
    </div>
  )
})

/* ── CSV table viewer ── */
export const CsvViewer = memo(function CsvViewer({ content, filePath }: { content: string; filePath: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
  // JSON.stringify restores the escapes JSON.parse consumed (\" \\ \n …), so a
  // string holding embedded JSON (an SNS Message, an SQS body) renders as valid
  // JSON source instead of masquerading as unescaped nested JSON — which reads
  // as a corrupt file. Truncate the RAW value before stringifying so the slice
  // can never cut an escape sequence in half, and keep the ellipsis OUTSIDE the
  // quotes so a truncated leaf never reads as the faithful full value.
  if (typeof value === 'string') {
    const truncated = value.length > 200
    return <span className="text-warn">{JSON.stringify(truncated ? value.slice(0, 200) : value)}{truncated && '…'}</span>
  }

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
              {!isArr && <span className="text-accent">{JSON.stringify(String(k))}</span>}
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  return (
    <div className="h-full border border-border rounded-md overflow-hidden bg-white">
      <iframe
        srcDoc={content}
        sandbox=""
        className="w-full h-full border-none"
        // Own compositing layer: a sandboxed srcDoc frame with no transform can
        // skip its first paint and render blank. Same remedy as McpAppFrame.
        style={{ transform: 'translateZ(0)' }}
        title={i18nT('components.fileRenderers.html_preview')}
      />
    </div>
  )
})

/* ── PDF viewer (embedded + fallback open externally) ── */
export const PdfViewer = memo(function PdfViewer({ filePath }: { filePath: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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

/* ── Office viewer ─────────────────────────────────────────────────
 *
 * Office binary formats are ZIP archives (OOXML) or legacy OLE compound files
 * that browsers cannot render inline. Serving them through /api/file-read
 * decodes them as UTF-8 with errors='replace', producing garbled control-code
 * text (raw ZIP bytes starting with 'PK…').
 *
 * Two rendering states:
 *   1. **Preview** — for .docx and .pptx the backend can extract plaintext
 *      via `kiro_crew.doc_parser.extract_text` (defusedxml-hardened, no
 *      python-docx / python-pptx dep). We render that text in a scrollable
 *      pre with a smaller "Download original" button pinned at the bottom.
 *   2. **Download-only card** — for extensions the backend can't preview
 *      (.xls / .xlsx / .doc / .ppt / .odt / .ods / .odp) we render the
 *      original card: filename + extension badge + full-size Download button.
 *      This is also the fallback when the preview fetch fails, the document
 *      is empty, or extract_text returns "" (parse failure).
 *
 * The preview endpoint returns 415 for unsupported extensions, so anything
 * other than a 2xx-with-non-empty-text falls through to the card without
 * duplicating the previewable-ext list on the frontend. */

/** Card body shared by both rendering states — full-size Download button
 *  (fallback mode) or compact "Download original" affordance (preview mode).
 *
 *  Both modes lead with **Open with default app**, not Download. The file is
 *  already on disk at the path this panel is showing, so downloading writes a
 *  SECOND copy: the user then edits that copy, which the agent never reads back
 *  and a later agent write silently diverges from. Handing the existing file to
 *  Word / PowerPoint / Excel is the action the surface is actually for.
 *
 *  Open is shown only when `useCanOpenFile` allows it — the same gate every other
 *  Open surface reads. A browser talking to a remote gateway has no desktop to
 *  open on, so there Download is the only thing that can work and it takes the
 *  accent styling back. */
function OfficeCard({ filePath, showBigDownload, hideHint }: { filePath: string; showBigDownload: boolean; hideHint?: boolean }) {
  // Split on BOTH separators — Kiro Crew ships native on Windows where paths
  // arrive as `C:\Users\…\report.docx`, and a `/`-only split would surface the
  // whole path as the "filename". Matches the pattern in MarkdownRenderer.tsx
  // and VectorMemoryCard.tsx.
  const filename = filePath.split(/[\\/]/).pop() || filePath
  const ext = extOf(filePath).replace('.', '').toUpperCase()
  const url = fileDownloadUrl(filePath)
  const sizeCls = showBigDownload ? 'px-3 py-1.5 text-sm' : 'px-2 py-1 text-xs'
  const iconSize = showBigDownload ? 16 : 14
  // The ONE gate for an Open-with-default-app surface, shared with the file
  // panel's ⋯ entry and MarkdownPanel's overflow: the browser must sit on the
  // gateway host, or `/api/reveal` has no desktop to open on and the click looks
  // broken. When it is false the card falls back to Download-as-primary, which
  // is what a remote session actually needs — the bytes.
  const canOpen = useCanOpenFile('file')
  // A headless direct-local host has no desktop, so the backend degrades the
  // `open` to a clipboard copy. Shared with the file-path menu (see useCopyAck)
  // so the primary button acknowledges that degrade with the same inline swap
  // instead of reading as a dead click.
  const { copyStatus, revealOrOpenWithAck } = useCopyAck(filePath)
  const openLabel = copyStatus === 'copied'
    ? i18nT('components.filePathMenu.path_copied')
    : copyStatus === 'failed'
      ? i18nT('components.filePathMenu.copy_failed')
      : i18nT('components.markdownPanel.open_with_default_app')
  return (
    <div className="flex flex-col items-center gap-3 max-w-md text-center mx-auto">
      <div className="relative">
        <FileText size={showBigDownload ? 64 : 40} className="text-muted" strokeWidth={1.25} />
        <span
          className={`absolute bottom-1 left-1/2 -translate-x-1/2 px-1.5 py-0.5 rounded font-semibold bg-accent text-white text-[10px]`}
          aria-hidden="true"
        >{ext}</span>
      </div>
      <div className="text-sm text-text break-all">{filename}</div>
      {showBigDownload && !hideHint && (
        <div className="text-xs text-muted">
          {/* The hint is the card's only instruction, so it must name the action
              the card leads with. Pointing a local user at Download is pointing
              them at the duplicate-file divergence this card exists to avoid;
              on remote/Windows, where Open is gated away, Download IS the
              action and the original wording stays correct. */}
          {canOpen
            ? i18nT('components.fileRenderers.office_open_hint')
            : i18nT('components.fileRenderers.office_download_hint')}
        </div>
      )}
      {/* Wraps rather than shrinks: the file panel is narrow, and a clipped
          label is worse than a second line. */}
      <div className="flex items-center justify-center gap-2 flex-wrap">
        {canOpen && (
          <button
            type="button"
            onClick={() => { void revealOrOpenWithAck('open') }}
            className={`inline-flex items-center gap-2 rounded border-none cursor-pointer bg-accent text-white hover:opacity-90 ${sizeCls}`}
          >
            <ExternalLink size={iconSize} aria-hidden="true" />
            {openLabel}
          </button>
        )}
        <a
          href={url}
          download={filename}
          className={`inline-flex items-center gap-2 rounded no-underline ${sizeCls} ${canOpen
            ? 'border border-border bg-bg-hover text-text hover:bg-bg-elevated'
            : 'bg-accent text-white hover:opacity-90'}`}
          aria-label={i18nT('components.fileRenderers.download_file', { filename })}
        >
          <Download size={iconSize} aria-hidden="true" />
          {showBigDownload
            ? i18nT('components.fileRenderers.download')
            : i18nT('components.fileRenderers.office_download_original')}
        </a>
      </div>
    </div>
  )
}

type OfficePreviewBody = { text?: string; truncated?: boolean }

// Extensions the backend can actually extract (mirrors _OFFICE_PREVIEWABLE_EXT
// in dashboard/handlers/files.py). Known-unsupported office formats render the
// download card directly — no fetch, no "Loading preview…" flash for a
// guaranteed 415. The 415 fallback below stays as the safety net if the two
// lists ever drift.
const OFFICE_PREVIEWABLE_EXTS = new Set(['.docx', '.pptx'])

export const OfficeViewer = memo(function OfficeViewer({ filePath, hideHint }: { filePath: string; hideHint?: boolean }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const filename = filePath.split(/[\\/]/).pop() || filePath
  const previewable = OFFICE_PREVIEWABLE_EXTS.has(extOf(filePath))
  // React Query (repo convention for server fetches — see ArtifactPanel /
  // AgentSkillsEditor). Keyed on filePath so navigating between .docx files
  // in the tree never flashes a stale response; aborts via the provided
  // signal on unmount/key change.
  const previewQuery = useQuery<OfficePreviewBody | null>({
    queryKey: ['office-preview', filePath],
    queryFn: async ({ signal }) => {
      const res = await fetch(fileOfficePreviewUrl(filePath), { signal })
      if (!res.ok) {
        // 415 (unsupported ext), 404, 400, 500 → all fall through to the
        // download-only card. We don't distinguish here because a broken
        // preview should never block downloading the real file.
        return null
      }
      return await res.json() as OfficePreviewBody
    },
    enabled: previewable,
    // No staleTime: a reopened file must show its CURRENT contents — the
    // document may have been edited since the last preview. Deduping within
    // a single mount still applies; only remounts refetch.
    staleTime: 0,
    retry: false,
  })

  if (previewable && previewQuery.isLoading) {
    return (
      <div className="h-full flex items-center justify-center p-4 bg-bg-elevated rounded-md border border-border">
        <div className="text-xs text-muted animate-pulse">
          {i18nT('components.fileRenderers.office_preview_loading')}
        </div>
      </div>
    )
  }

  const body = previewable && !previewQuery.isError ? previewQuery.data : null
  if (!body?.text) {
    return (
      <div className="h-full flex items-center justify-center p-4 bg-bg-elevated rounded-md border border-border">
        <OfficeCard filePath={filePath} showBigDownload={true} hideHint={hideHint} />
      </div>
    )
  }

  // Preview state — scrollable plaintext + compact download affordance at bottom.
  // tabIndex + aria-label make the scroll container keyboard-reachable so long
  // documents stay readable past the fold without a pointer.
  return (
    <div className="h-full flex flex-col bg-bg-elevated rounded-md border border-border overflow-hidden">
      {/* Keyboard-scrollable region — same pattern as CodeBlock.tsx. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div className="flex-1 overflow-auto p-4" tabIndex={0} role="region" aria-label={filename}>
        <pre className="text-sm text-text whitespace-pre-wrap break-words font-sans leading-relaxed">{body.text}</pre>
      </div>
      <div className="border-t border-border p-3 bg-bg">
        {/* Truncation notice lives in the always-visible pinned bar (not after
            the 512 KB of text) so users skimming the top of a large document
            learn the preview is partial without scrolling to the end. */}
        {body.truncated && (
          <div className="mb-2 text-xs text-muted italic text-center">
            {i18nT('components.fileRenderers.office_preview_truncated')}
          </div>
        )}
        <OfficeCard filePath={filePath} showBigDownload={false} />
      </div>
    </div>
  )
})

/* ── Media player (inline video/audio via /api/file-stream) ── */
export const MediaPlayer = memo(function MediaPlayer({ filePath, kind }: { filePath: string; kind: 'video' | 'audio' }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [failed, setFailed] = useState(false)
  const filename = filePath.split(/[\\/]/).pop() || filePath
  const src = fileStreamUrl(filePath)
  const Icon = kind === 'video' ? Film : Music
  if (failed) {
    // Endpoint refusal (unsupported container, oversize) or a codec the
    // browser cannot decode -- same fallback contract as the other rich
    // viewers: never render a broken surface, always offer the bytes.
    return (
      <div className="h-full flex items-center justify-center p-4 bg-bg-elevated rounded-md border border-border">
        <div className="flex flex-col items-center gap-3 max-w-md text-center">
          <Icon size={64} className="text-muted" strokeWidth={1.25} aria-hidden="true" />
          <div className="text-sm text-text break-all">{filename}</div>
          <div className="text-xs text-muted">
            {i18nT('components.fileRenderers.media_preview_failed')}
          </div>
          <a
            href={fileDownloadUrl(filePath)}
            download={filename}
            className="inline-flex items-center gap-2 px-3 py-1.5 rounded text-sm bg-accent text-white hover:opacity-90 no-underline"
            aria-label={i18nT('components.fileRenderers.download_file', { filename })}
          >
            <Download size={16} aria-hidden="true" />
            {i18nT('components.fileRenderers.download')}
          </a>
        </div>
      </div>
    )
  }
  if (kind === 'video') {
    return (
      <div className="h-full flex items-center justify-center p-3 bg-bg-elevated">
        {/* eslint-disable-next-line jsx-a11y/media-has-caption -- local files carry no track sidecar; captions are out of scope */}
        <video
          controls
          preload="metadata"
          src={src}
          className="max-h-full max-w-full rounded-md"
          onError={() => setFailed(true)}
          aria-label={filename}
        />
      </div>
    )
  }
  return (
    <div className="h-full flex flex-col items-center justify-center gap-3 p-4 bg-bg-elevated">
      <Icon size={40} className="text-muted" strokeWidth={1.25} aria-hidden="true" />
      <div className="text-sm text-text break-all text-center max-w-md">{filename}</div>
      {/* eslint-disable-next-line jsx-a11y/media-has-caption -- local files carry no track sidecar; captions are out of scope */}
      <audio
        controls
        preload="metadata"
        src={src}
        className="w-full max-w-md"
        onError={() => setFailed(true)}
        aria-label={filename}
      />
    </div>
  )
})


/* ── Sheet viewer (inline xlsx preview) ──────────────────────────────────────
 *
 * Renders OOXML spreadsheets as a real grid: sheet tabs, a column-letter
 * header row, and a row-number gutter — a spreadsheet has no reason to promote
 * its first row to <th> the way CsvViewer does. Cell data comes from
 * GET /api/file-sheet (server-side openpyxl parse), already capped server-side
 * at 500 rows × 100 columns per sheet with explicit truncation flags. Cells
 * holding a formula with no cached value render the formula source ("=…") in
 * muted styling. Any endpoint failure (legacy format, parse error, endpoint
 * unavailable) degrades to the OfficeViewer download card, so the viewer is
 * never worse than what it replaces. */
type SheetGrid = {
  name: string
  rows: (string | number | boolean | null)[][]
  truncated_rows: boolean
  truncated_cols: boolean
}
type SheetPayload = { sheets: SheetGrid[]; total_sheets: number; truncated_sheets: boolean }

/** 0-based column index → spreadsheet letters (0→A, 25→Z, 26→AA). */
export function columnLetter(index: number): string {
  let n = index + 1, s = ''
  while (n > 0) { n--; s = String.fromCharCode(65 + (n % 26)) + s; n = Math.floor(n / 26) }
  return s
}

export const SheetViewer = memo(function SheetViewer({ filePath }: { filePath: string }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [payload, setPayload] = useState<SheetPayload | null>(null)
  const [failed, setFailed] = useState(false)
  const [active, setActive] = useState(0)

  useEffect(() => {
    const ctrl = new AbortController()
    setPayload(null); setFailed(false); setActive(0)
    fetch('/api/file-sheet?path=' + encodeURIComponent(filePath), { signal: ctrl.signal })
      .then(async r => {
        if (!r.ok) throw new Error(String(r.status))
        const body: SheetPayload = await r.json()
        if (!Array.isArray(body?.sheets)) throw new Error('malformed payload')
        setPayload(body)
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setFailed(true)
      })
    return () => ctrl.abort()
  }, [filePath])

  if (failed) {
    return (
      <div className="h-full flex flex-col">
        <div className="text-center text-[11px] py-1.5 text-warn">
          {i18nT('components.fileRenderers.sheet_preview_failed')}
        </div>
        <div className="flex-1 min-h-0"><OfficeViewer filePath={filePath} hideHint /></div>
      </div>
    )
  }
  if (!payload) {
    return (
      <div className="h-full flex items-center justify-center p-4 bg-bg-elevated rounded-md border border-border">
        <div className="text-sm text-muted animate-pulse">{i18nT('components.fileRenderers.sheet_loading')}</div>
      </div>
    )
  }

  const sheet = payload.sheets[Math.min(active, Math.max(payload.sheets.length - 1, 0))]
  const colCount = sheet ? sheet.rows.reduce((m, r) => Math.max(m, r.length), 0) : 0
  const hasFormulas = !!sheet && sheet.rows.some(r => r.some(c => typeof c === 'string' && c.startsWith('=')))

  return (
    <div className="h-full flex flex-col border border-border rounded-md bg-bg-elevated overflow-hidden">
      {payload.sheets.length > 1 && (
        <div className="flex gap-1 px-2 pt-2 pb-0 bg-chrome border-b border-border overflow-x-auto shrink-0">
          {payload.sheets.map((s, i) => (
            <button
              key={i}
              aria-pressed={i === active}
              onClick={() => setActive(i)}
              className={`px-3 py-1.5 rounded-t text-[12px] whitespace-nowrap cursor-pointer border border-b-0 ${
                i === active ? 'bg-bg-elevated text-text border-border font-semibold' : 'bg-transparent text-muted border-transparent hover:text-text'
              }`}
            >{s.name}</button>
          ))}
        </div>
      )}
      {sheet && hasFormulas && (
        <div className="text-center text-muted text-[12px] py-1.5 border-b border-border bg-chrome shrink-0">
          {i18nT('components.fileRenderers.sheet_formulas_note')}
          {' · '}
          <a href={fileDownloadUrl(filePath)} download className="text-accent no-underline hover:underline">
            {i18nT('components.fileRenderers.download')}
          </a>
        </div>
      )}
      <div className="flex-1 overflow-auto">
        {!sheet || sheet.rows.length === 0 ? (
          <div className="p-4 text-muted text-sm">{i18nT('components.fileRenderers.sheet_empty')}</div>
        ) : (
          <table className="text-sm font-mono border-collapse">
            <thead className="sticky top-0 z-10 bg-chrome">
              <tr>
                <th className="px-2 py-1 text-[10px] font-semibold text-muted border-b border-r border-border bg-chrome sticky left-0" aria-hidden="true" />
                {Array.from({ length: colCount }, (_, c) => (
                  <th key={c} scope="col" className="px-3 py-1 text-center text-[10px] font-semibold text-muted border-b border-border whitespace-nowrap min-w-[64px]">{columnLetter(c)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sheet.rows.map((row, ri) => (
                <tr key={ri} className="hover:bg-bg-hover">
                  <th scope="row" className="px-2 py-1 text-right text-[10px] font-semibold text-muted border-b border-r border-border/50 bg-chrome sticky left-0">{ri + 1}</th>
                  {Array.from({ length: colCount }, (_, ci) => {
                    const cell = row[ci]
                    const isFormula = typeof cell === 'string' && cell.startsWith('=')
                    const display = cell === null || cell === undefined ? '' : typeof cell === 'boolean' ? (cell ? 'TRUE' : 'FALSE') : String(cell)
                    return (
                      <td
                        key={ci}
                        className={`px-3 py-1 border-b border-border/50 whitespace-nowrap ${
                          typeof cell === 'number' ? 'text-right text-text' : isFormula ? 'text-muted italic' : 'text-text'
                        }`}
                      >{display}</td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {sheet && (sheet.truncated_rows || sheet.truncated_cols || payload.truncated_sheets) && (
        <div className="text-center text-muted text-[11px] py-1.5 border-t border-border bg-chrome shrink-0">
          {sheet.truncated_rows && i18nT('components.fileRenderers.sheet_showing_rows', { shown: sheet.rows.length })}
          {sheet.truncated_rows && (sheet.truncated_cols || payload.truncated_sheets) ? ' · ' : ''}
          {sheet.truncated_cols && i18nT('components.fileRenderers.sheet_cols_truncated')}
          {sheet.truncated_cols && payload.truncated_sheets ? ' · ' : ''}
          {payload.truncated_sheets && i18nT('components.fileRenderers.sheet_tabs_truncated', { shown: payload.sheets.length, total: payload.total_sheets })}
          {(sheet.truncated_rows || sheet.truncated_cols || payload.truncated_sheets) && (
            <>
              {' · '}
              <a href={fileDownloadUrl(filePath)} download className="text-accent no-underline hover:underline">
                {i18nT('components.fileRenderers.download')}
              </a>
            </>
          )}
        </div>
      )}
    </div>
  )
})
