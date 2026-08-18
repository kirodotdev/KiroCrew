import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { useState, useMemo, useEffect, useRef, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { AlertTriangle, Bookmark, Cloud, ExternalLink, Globe, ImageOff, Rocket, X, Share2, Loader2, LayoutDashboard, Table as TableIcon, Folder as FolderIcon, FolderPlus, FolderOpen, ChevronRight, ChevronDown, ChevronUp, MoreVertical, Pencil, Trash2, Star, FileText, FilePlus } from 'lucide-react'
import { openPopout } from '../utils/artifactPopout'
import { VirtuosoMasonry } from '@virtuoso.dev/masonry'
import type { ItemContent } from '@virtuoso.dev/masonry'
// At one column the gallery is a list, and it is windowed by this app's own
// virtualizer rather than a library: see `LibraryList` for the measured reason.
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { useCollapseOnScroll, COLLAPSE_MS, CHROME_ATTR } from '../hooks/useCollapseOnScroll'
import { widgetHeightKey, getWidgetHeight, setWidgetHeight, estimateWidgetHeight } from '../utils/widgetHeights'
import { getImageDims, rememberImageDims } from '../utils/imageDims'
import { DndContext, MouseSensor, TouchSensor, useSensor, useSensors, DragOverlay, MeasuringStrategy, pointerWithin, type DragEndEvent, type DragStartEvent, type CollisionDetection, type Modifier } from '@dnd-kit/core'
import SegmentedControl from '../components/SegmentedControl'
import { api } from '../api/client'
import { Card, CardTitle, PageHeader, Btn, Badge, SearchInput, EmptyState, Input, IconButton } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import RemoteArtifactCard from '../components/RemoteArtifactCard'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '../components/ui/dropdown-menu'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import MarkdownRenderer from '../components/MarkdownRenderer'
import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import ArtifactFolderDeleteDialog from '../components/ArtifactFolderDeleteDialog'
import { DndDraggable, DndDroppable } from '../components/dnd'
import { useArtifactFolders, useMoveArtifactToFolder } from '../hooks/useArtifactFolders'
import { childFolders, isDescendantFolder, folderSubtreeStats, folderBreadcrumb } from '../utils/artifactFolderTree'
import { sanitize } from '../api/helpers'
import { compareText } from '../i18n/format'
import { useTheme } from '../hooks/useTheme'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { framablePreviewUrl } from '../lib/safeUrl'
import { useCloudDeploymentEnabled } from '../hooks/useCloudDeploymentEnabled'
import { markJustCreatedBlank } from '../lib/blankHandoff'
import { IMPORT_ACCEPT, IMPORTABLE_EXT_LIST, MAX_IMPORT_BYTES, planFileImport, wasContentRedacted, type ImportPlan, type ImportRejection } from '../lib/artifactImport'
import { useAppPreview } from '../components/WebAppArtifactCard'
import { THEME_VAR_NAMES, buildSrcdoc } from '../lib/widgetSrcdoc'
import type { Artifact, ArtifactFolder, PublishProviderDescriptor, RemoteArtifact, SessionDoc } from '../types'

import { i18nT } from '../i18n/t'
import { FOLDER_COLOR_PALETTE } from '../components/folderColorCatalog'
/** Read the current computed theme CSS vars (capped to the known set, each
 * value sanitized) so a sandboxed preview iframe matches the dashboard theme.
 * Mirrors the helper in ArtifactDetailPage. */
function readThemeVars(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const computed = getComputedStyle(document.documentElement)
  const out: Record<string, string> = {}
  for (const name of THEME_VAR_NAMES) {
    const v = sanitizeCssValue(computed.getPropertyValue(name))
    if (v) out[name] = v
  }
  return out
}

const KIND_OPTIONS = ['', 'widget', 'html', 'markdown', 'svg', 'json', 'text', 'webapp', 'image'] as const

const KIND_BADGE: Record<Artifact['kind'], 'ok' | 'err' | 'warn' | 'aim'> = {
  widget: 'aim',
  html: 'ok',
  markdown: 'ok',
  svg: 'warn',
  json: 'ok',
  text: 'ok',
  webapp: 'aim',
  image: 'warn',
}

/** Explain a refused "Add Artifact" pick in the library's error banner.
 *
 * Kept next to the page rather than inside `lib/artifactImport.ts` so that
 * module stays free of catalog lookups and unit-testable without i18n. */
function importRejectionText(reason: ImportRejection): string {
  switch (reason) {
    case 'unsupported-type':
      return `${i18nT('pages.artifactsPage.add_artifact_error_unsupported_type')} ${IMPORTABLE_EXT_LIST}`
    case 'too-large':
      return i18nT('pages.artifactsPage.add_artifact_error_too_large', {
        limit: Math.floor(MAX_IMPORT_BYTES / (1024 * 1024)),
      })
    case 'empty':
      return i18nT('pages.artifactsPage.add_artifact_error_empty')
    case 'not-text':
      return i18nT('pages.artifactsPage.add_artifact_error_not_text')
    case 'unreadable':
      return i18nT('pages.artifactsPage.add_artifact_error_unreadable')
  }
}

function isoToTs(iso: string): number {
  if (!iso) return 0
  const t = Date.parse(iso)
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0
}

// ── Table column sorting ─────────────────────────────────────────────────
// Clicking a header cycles asc → desc → default (the server's order). The
// star and Actions columns are controls, not data, and stay unsortable.
type SortKey = 'name' | 'slug' | 'kind' | 'source' | 'version' | 'tags' | 'updated'
type SortState = { key: SortKey; dir: 'asc' | 'desc' } | null

const ARTIFACT_SORT_STORAGE_KEY = 'mc-artifacts-sort'
const SORT_KEYS = new Set<SortKey>(['name', 'slug', 'kind', 'source', 'version', 'tags', 'updated'])

function readPersistedSort(): SortState {
  try {
    const value: unknown = JSON.parse(safeGetItem(ARTIFACT_SORT_STORAGE_KEY) ?? 'null')
    if (!value || typeof value !== 'object') return null
    const { key, dir } = value as { key?: unknown; dir?: unknown }
    return typeof key === 'string' && SORT_KEYS.has(key as SortKey) && (dir === 'asc' || dir === 'desc')
      ? { key: key as SortKey, dir }
      : null
  } catch {
    return null
  }
}

/** Type-aware comparator: numeric for version, chronological for updated,
 * locale-collated natural string for the rest (compareText names the active
 * UI locale — never the host's). Direction is applied by the caller. */
function compareArtifacts(a: Artifact, b: Artifact, key: SortKey): number {
  switch (key) {
    case 'version':
      return a.version - b.version
    case 'updated': {
      // Byte-order ISO-8601 compare (same backend format, +00:00 offset):
      // chronological AND keeps the microsecond precision Date.parse drops,
      // so two artifacts updated within the same second still order correctly.
      const au = a.updated_at || ''
      const bu = b.updated_at || ''
      return au < bu ? -1 : au > bu ? 1 : 0
    }
    case 'source':
      return compareText(a.session_title || a.source || '', b.session_title || b.source || '')
    case 'tags':
      return compareText((a.tags || []).join(', '), (b.tags || []).join(', '))
    case 'slug':
      return compareText(a.slug, b.slug)
    case 'kind':
      return compareText(a.kind, b.kind)
    default:
      return compareText(a.name, b.name)
  }
}

function sortArtifacts(items: Artifact[], sort: SortState): Artifact[] {
  if (!sort) return items
  const mul = sort.dir === 'desc' ? -1 : 1
  // Array.prototype.sort is stable, so equal rows keep the server's order.
  return [...items].sort((a, b) => compareArtifacts(a, b, sort.key) * mul)
}

/** Infer an artifact `kind` for a session document from its extension.
 * Mirrors the backend's DOC_EXTENSIONS (.md/.markdown/.mdx → markdown;
 * .txt/.rst → text). */
function docFileType(path: string): Artifact['kind'] {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return ext === 'txt' || ext === 'rst' ? 'text' : 'markdown'
}

// ── Masonry library ──────────────────────────────────────────────────────
// The Library renders as a virtualized masonry (瀑布流) via VirtuosoMasonry.
// Widget/html artifacts get a live sandboxed preview thumbnail that self-sizes
// (height reporter), giving the waterfall its natural varying heights; other
// kinds get a content snippet. Virtualization means only on-screen previews
// mount, so N sandboxed iframes stay cheap.

/** A cell in the "Your Artifacts" grid — a local artifact. */
type GridEntry = { kind: 'local'; key: string; art: Artifact }

type LibCtx = {
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
}

/** Responsive column count from the container width (~300px target column). */
/** The page column's own horizontal padding at narrow widths (`px-4`, one side).
 *  Only used to seed the column estimate below; the real width is measured. */
const PAGE_GUTTER = 16

function useColumnCount(minColWidth = 300): readonly [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null)
  // Seeded from the viewport instead of a constant because the page reads this
  // count to decide WHO OWNS THE SCROLL AXIS: a wrong first value hands the axis
  // over and takes it back a frame later, which reads as a jump. This is only an
  // estimate (it assumes the narrow gutter); the ResizeObserver corrects it
  // against the real element on mount either way.
  const [cols, setCols] = useState(() =>
    typeof window === 'undefined'
      ? 2
      : Math.max(1, Math.floor((window.innerWidth - PAGE_GUTTER * 2) / minColWidth)),
  )
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => setCols(Math.max(1, Math.floor(el.clientWidth / minColWidth)))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [minColWidth])
  return [ref, cols] as const
}

/** Key space for cached thumbnail heights. These are measured at BASE_W and
 *  clamped to VIEWPORT_H, so they are not comparable with the heights a
 *  full-width `WidgetFrame` measures and must not share its entries. */
const THUMB_HEIGHT_SPACE = 'thumb900'

/** Live preview of a widget/html artifact, rendered as a scaled-down
 * thumbnail: the iframe lays out at a fixed desktop width (BASE_W) so the
 * widget looks normal, then the whole frame is CSS-scaled to fit the column —
 * a minified webpage, not a cramped narrow render. */
function WidgetThumb({ content, slug }: { content: string; slug: string }) {
  const BASE_W = 900
  // Fixed iframe viewport height (in BASE_W space). The iframe NEVER grows past
  // this — it only shrinks for genuinely short flow-content. This makes
  // viewport-sized content (height:100vh / 100%, e.g. slide decks) impossible to
  // ratchet: the reported height is clamped to the viewport, so 100vh can't feed
  // itself taller. Tall flow-content (dashboards) is clipped to the viewport top.
  const VIEWPORT_H = 560
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const srcdoc = useMemo(
    () => (content ? buildSrcdoc({ html: content, themeVars, mode: theme, includeHeightReporter: true }) : null),
    [content, themeVars, theme],
  )
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  // Reserve the height this content had last time, or the median of thumbnails
  // already measured, before falling back to the viewport ceiling.
  //
  // Seeding from VIEWPORT_H instead makes every thumbnail start at the MAXIMUM
  // and correct downward when the iframe reports — so the error is one-way, and
  // inside a virtualized list the corrections accumulate into a scroller whose
  // total height keeps shrinking as you scroll (measured: 4.1k px of height
  // change and 1.2k px of drift over eight swipes). `WidgetFrame` solved the
  // same problem the same way; the key space is separate because these
  // thumbnails lay out at a fixed BASE_W while a frame lays out at its
  // container's width, so the two sets of heights are not comparable.
  const heightKey = useMemo(() => widgetHeightKey(content, THUMB_HEIGHT_SPACE), [content])
  const [contentH, setContentH] = useState(
    () => getWidgetHeight(heightKey) ?? Math.min(VIEWPORT_H, estimateWidgetHeight(THUMB_HEIGHT_SPACE, VIEWPORT_H)),
  ) // iframe height at BASE_W (≤ VIEWPORT_H)
  const [colW, setColW] = useState(320) // measured column/preview width
  const wrapRef = useRef<HTMLDivElement>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setColW(el.clientWidth || 320)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    if (!srcdoc) return
    const blob = new Blob([srcdoc], { type: 'text/html;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    setBlobUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [srcdoc])

  useEffect(() => {
    const handler = (e: MessageEvent) => {
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return
      if (e.data?.type === 'mc-widget-height' && typeof e.data.height === 'number') {
        // Reject non-finite reports outright: `typeof NaN === 'number'`, and
        // NaN flows straight through min/max/round into BOTH the persisted
        // cache and contentH (rendering height:NaN). A misbehaving widget
        // must not be able to corrupt the geometry every later mount reserves
        // from.
        if (!Number.isFinite(e.data.height)) return
        // Clamp to the viewport ceiling so viewport-sized content (100vh) can
        // never grow the iframe — and thus can never grow itself.
        const next = Math.min(VIEWPORT_H, Math.max(80, Math.round(e.data.height)))
        // Remember it before the state update: this is what lets the NEXT mount
        // of the same content reserve the right box and not correct at all.
        setWidgetHeight(heightKey, next)
        setContentH((prev) => (next === prev ? prev : next))
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [heightKey])

  // Re-reserve when the CONTENT changes: the card mounts before its lazy
  // fetch resolves (`hasPreview` gates on kind, not content), so the useState
  // initializer above ran against the EMPTY content's key. Without this
  // resync the persisted height for the real content is ignored and every
  // mount pays one avoidable correction when the iframe reports — the same
  // height-churn class the virtualized gallery works to eliminate.
  useEffect(() => {
    setContentH(getWidgetHeight(heightKey) ?? Math.min(VIEWPORT_H, estimateWidgetHeight(THUMB_HEIGHT_SPACE, VIEWPORT_H)))
  }, [heightKey])

  const scale = colW / BASE_W
  // contentH is already clamped to VIEWPORT_H in the reporter, so the iframe
  // never grows past the fixed viewport — no feedback loop is possible.
  const renderH = contentH
  const scaledH = Math.round(renderH * scale)

  return (
    <div
      ref={wrapRef}
      className="relative w-full overflow-hidden bg-card"
      // The SAME box before and after the iframe exists. Reserving a different
      // placeholder height (and then swapping) is a second height change per
      // card on top of the report, and in a virtualized list every one of those
      // re-lays out everything below it.
      style={{ height: scaledH }}
    >
      {blobUrl ? (
        <iframe
          ref={iframeRef}
          src={blobUrl}
          sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
          title={i18nT('pages.artifactsPage.preview', { slug })}
          tabIndex={-1}
          className="border-none bg-card block"
          style={{
            width: BASE_W,
            height: renderH,
            transform: `scale(${scale})`,
            transformOrigin: 'top left',
          }}
        />
      ) : (
        <div className="h-full bg-bg-elevated animate-pulse" />
      )}
    </div>
  )
}

/** Kind-aware preview for non-iframe artifacts: markdown is rendered, SVG is
 * drawn (sanitized), JSON is pretty-printed, everything else is a raw snippet.
 * All paths are height-capped so cards stay tidy. */
function ContentThumb({ content, kind }: { content: string; kind: Artifact['kind'] }) {
  if (!content.trim()) return <div className="h-[64px] bg-bg-elevated" />

  if (kind === 'markdown') {
    return (
      <div className="px-3 py-2 max-h-[300px] overflow-hidden bg-card msg-content text-[12px] leading-relaxed">
        <MarkdownRenderer content={content.slice(0, 4000)} />
      </div>
    )
  }

  if (kind === 'svg') {
    const clean = sanitize(content)
    return (
      <div
        className="px-3 py-3 max-h-[300px] overflow-hidden bg-card flex items-center justify-center [&>svg]:max-w-full [&>svg]:max-h-[280px] [&>svg]:h-auto"
        dangerouslySetInnerHTML={{ __html: clean }}
      />
    )
  }

  let body = content
  if (kind === 'json') {
    try { body = JSON.stringify(JSON.parse(content), null, 2) } catch { /* keep raw on parse failure */ }
  }
  return (
    <pre className="m-0 px-3 py-2 text-[11px] leading-snug text-muted font-mono whitespace-pre-wrap break-words max-h-[260px] overflow-hidden bg-bg-elevated">
      {body.slice(0, 1200)}
    </pre>
  )
}

/** Thumbnail for image artifacts: the picture streamed straight from the
 * artifact's asset endpoint (the server sets Content-Type), object-fit
 * contained and height-capped so cards stay uniform. Lazy so off-screen
 * gallery cards don't fetch bytes until scrolled into view. Alt text prefers
 * the stored `image.alt`, falling back to the artifact name. */
function ImageThumb({ a }: { a: Artifact }) {
  // The asset endpoint can legitimately 404/500 (pruned sidecar, unreadable
  // file, refused mime). A bare <img> would leave the browser's broken-image
  // glyph sitting in an otherwise healthy card with nothing to read.
  const [failed, setFailed] = useState(false)
  // Natural dimensions, best source first: the save-time header sniff in the
  // artifact metadata, else the client-side cache learned from a prior load
  // (legacy artifacts saved before the sniff existed). Either way the browser
  // derives an aspect ratio from the ATTRIBUTES and reserves the final
  // contain-fit box before any bytes arrive — without it the card mounts
  // ~16px tall and grows ~280px when the lazy load lands, shoving everything
  // below it mid-scroll on every pass (the virtualizer's row-height cache
  // sizes placeholders, not a remounted card's own empty <img> box).
  const meta = a.image
  const known = meta?.width && meta?.height ? { w: meta.width, h: meta.height } : getImageDims(a.slug)
  if (failed) {
    return (
      <div className="flex flex-col items-center justify-center gap-1 max-h-[300px] h-[120px] overflow-hidden bg-bg-elevated p-2 text-center">
        <ImageOff size={16} className="text-muted shrink-0" aria-hidden="true" />
        <span className="text-[11px] text-muted">
          {i18nT('pages.artifactsPage.image_could_not_be_loaded')}
        </span>
      </div>
    )
  }
  return (
    <div className="flex items-center justify-center max-h-[300px] overflow-hidden bg-bg-elevated p-2">
      <img
        src={`/api/artifacts/${a.slug}/asset`}
        alt={a.image?.alt || a.name}
        loading="lazy"
        width={known?.w}
        height={known?.h}
        className="max-w-full max-h-[280px] object-contain"
        draggable={false}
        // Learn the natural size on a successful load so the NEXT mount of a
        // legacy image (no sniffed metadata) reserves correctly. Slug-keyed:
        // a re-upload overwrites on its next load.
        onLoad={(e) => {
          if (!meta?.width || !meta?.height) {
            rememberImageDims(a.slug, e.currentTarget.naturalWidth, e.currentTarget.naturalHeight)
          }
        }}
        onError={() => setFailed(true)}
      />
    </div>
  )
}

const WEBAPP_STATUS_DOT: Record<string, string> = {
  live: 'bg-ok',
  deploying: 'bg-warn animate-pulse',
  expired: 'bg-muted-strong',
  error: 'bg-danger',
}

/** Gallery preview for webapp artifacts — a mock browser window so an app
 * card reads as "a website" next to the html/widget iframe thumbs, never as
 * a wall of raw description text. Live CloudFront deployments embed the real
 * site (same scaled-viewport trick as WidgetThumb, no height reporter needed:
 * fixed 16:10 viewport); every other state gets a status hero. `mini` drops
 * the iframe (an 84px folder tile can't render a meaningful site). */
function WebAppThumb({ art, mini = false }: { art: Artifact; mini?: boolean }) {
  const BASE_W = 1280
  const BASE_H = 800
  const meta = art.webapp_metadata
  const status = meta?.lifecycle?.status ?? 'draft'
  const publicUrl = meta?.deploy_target?.public_url || ''
  // When the platform withholds cloud deployment there is no deploy state worth
  // reporting: every artifact would read "Not deployed" for a capability that is
  // not on offer. The local preview below is unaffected and still renders.
  const cloudDeployEnabled = useCloudDeploymentEnabled()
  // Local-first: serve the app's local copy through the gateway preview
  // channel (works for every lifecycle state); fall back to iframing the
  // live CloudFront deployment; else a status hero.
  const { base: previewBase, remoteFramable } = useAppPreview(art.slug, !mini && !!meta)
  const frameUrl = previewBase
    || (!mini && status === 'live' && remoteFramable ? framablePreviewUrl(publicUrl) : null)
  const urlLabel = (() => {
    if (!publicUrl) return cloudDeployEnabled ? i18nT('pages.artifactsPage.not_deployed') : ''
    try {
      const u = new URL(publicUrl)
      return `${u.host}${u.pathname}`
    } catch {
      return cloudDeployEnabled ? i18nT('pages.artifactsPage.not_deployed') : ''
    }
  })()
  const wrapRef = useRef<HTMLDivElement>(null)
  const [colW, setColW] = useState(320)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setColW(el.clientWidth || 320)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const scale = colW / BASE_W
  const heroIcon = status === 'expired'
    ? <Cloud size={mini ? 16 : 24} className="text-muted" aria-hidden="true" />
    : <Rocket size={mini ? 16 : 24} className={status === 'deploying' ? 'text-warn animate-pulse' : 'text-accent/70'} aria-hidden="true" />
  const heroLabel = status === 'expired' ? i18nT('pages.artifactsPage.expired') : status === 'deploying' ? i18nT('pages.artifactsPage.deploying') : status === 'live' ? i18nT('pages.artifactsPage.live') : cloudDeployEnabled ? i18nT('pages.artifactsPage.not_deployed_2') : i18nT('pages.artifactsPage.local_preview')
  return (
    <div className="bg-card">
      {/* chrome bar */}
      <div className={`flex items-center gap-1.5 px-2 ${mini ? 'py-1' : 'py-1.5'} bg-bg-elevated border-b border-border`}>
        <div className="flex gap-1 shrink-0" aria-hidden="true">
          <span className="w-1.5 h-1.5 rounded-full bg-danger/40" />
          <span className="w-1.5 h-1.5 rounded-full bg-warn/40" />
          <span className="w-1.5 h-1.5 rounded-full bg-ok/40" />
        </div>
        <div className="flex-1 min-w-0 flex items-center gap-1 px-1.5 py-0.5 rounded bg-card border border-border">
          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${WEBAPP_STATUS_DOT[status] ?? 'bg-muted-strong'}`} aria-hidden="true" />
          <span className="text-[10px] text-muted truncate font-mono">{urlLabel}</span>
        </div>
      </div>
      {frameUrl ? (
        <div ref={wrapRef} className="relative w-full overflow-hidden bg-card" style={{ height: Math.round(BASE_H * scale) }}>
          <iframe
            src={frameUrl}
            // Local channel (/artifact-app/...): scripts ONLY — the path is
            // dashboard-origin, so allow-same-origin here would hand the app
            // the dashboard's cookies/DOM (the channel's own CSP `sandbox`
            // header enforces an opaque origin as a second layer).
            // Remote CloudFront fallback: allow-same-origin refers to the
            // site's own origin, never the dashboard's.
            sandbox={previewBase ? 'allow-scripts' : 'allow-scripts allow-same-origin'}
            referrerPolicy="no-referrer"
            loading="lazy"
            title={i18nT('pages.artifactsPage.app_preview', { slug: art.slug })}
            tabIndex={-1}
            className="border-none bg-card block"
            style={{ width: BASE_W, height: BASE_H, transform: `scale(${scale})`, transformOrigin: 'top left' }}
          />
        </div>
      ) : (
        <div className={`flex flex-col items-center justify-center gap-1.5 ${mini ? 'py-3' : 'py-8'} bg-gradient-to-br from-accent-subtle via-card to-bg-elevated`}>
          {heroIcon}
          <span className={`${mini ? 'text-[10px]' : 'text-[12px]'} text-muted font-medium`}>{heroLabel}</span>
          {!mini && meta?.architecture && (
            <span className="text-[10px] text-muted">
              {[meta.architecture.frontend && 'frontend', meta.architecture.backend && 'api', meta.architecture.state && 'db'].filter(Boolean).join(' \u00b7 ')}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

// ── Folders ──────────────────────────────────────────────────
// The library's DnD has only `folder-drop` droppables (folder cards/rows,
// breadcrumb segments, the Unfiled lane), so pointer containment is the whole
// story: a drop target is "over" only while the cursor is inside it. No
// closest-fallback — that would keep the nearest folder permanently
// highlighted during any drag, even with the cursor nowhere near it.
const artifactLibraryCollision: CollisionDetection = (args) => pointerWithin(args)

// Center the DragOverlay ghost on the cursor. Without this the overlay spawns
// at the dragged element's top-left — grabbing a tall masonry card near its
// bottom leaves the ghost hundreds of pixels above the pointer. (Inline port
// of @dnd-kit/modifiers' snapCenterToCursor; the package isn't a dependency.)
const snapOverlayToCursor: Modifier = ({ activatorEvent, draggingNodeRect, transform }) => {
  if (draggingNodeRect && activatorEvent && 'clientX' in activatorEvent && 'clientY' in activatorEvent) {
    const evt = activatorEvent as PointerEvent
    const offsetX = evt.clientX - draggingNodeRect.left
    const offsetY = evt.clientY - draggingNodeRect.top
    return {
      ...transform,
      x: transform.x + offsetX - draggingNodeRect.width / 2,
      y: transform.y + offsetY - draggingNodeRect.height / 2,
    }
  }
  return transform
}

/** Payload carried by draggable cards/rows; routes the drop in handleDragEnd. */
type LibraryDrag =
  | { type: 'artifact'; slug: string; name: string; folderId: string }
  | { type: 'folder'; id: string; name: string }

type FolderActions = {
  onOpen: (folderId: string) => void
  onRename: (f: ArtifactFolder) => void
  onMove: (f: ArtifactFolder, newParentId: string) => void
  onDelete: (f: ArtifactFolder) => void
  onSetColor: (f: ArtifactFolder, color: string) => void
  /** Folder currently in inline-rename mode (its card/row swaps the name for an input). */
  renamingId: string | null
  onRenameSubmit: (f: ArtifactFolder, name: string) => void
  onRenameCancel: () => void
}

/** Curated folder color palette (works on light + dark themes). '' = none. */
/** Swatch strip for picking a folder color ('' clears back to default).
 *  The palette is the shared folder catalog (folderColorCatalog.tsx), so
 *  artifact folders and chat folders offer the same hues and the aria labels
 *  reuse the localized color names. */
function FolderColorSwatches({ value, onPick, size = 16 }: { value?: string; onPick: (color: string) => void; size?: number }) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap" role="radiogroup" aria-label={i18nT('pages.artifactsPage.folder_color')}>
      {FOLDER_COLOR_PALETTE.map(({ value: c, label }) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={value === c}
          aria-label={label()}
          title={label()}
          onClick={(e) => { e.stopPropagation(); onPick(c) }}
          onPointerDown={(e) => e.stopPropagation()}
          className={`rounded-full border cursor-pointer transition-transform hover:scale-110 ${
            value === c ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'
          }`}
          style={{ width: size, height: size, background: c }}
        />
      ))}
      <button
        type="button"
        role="radio"
        aria-checked={!value}
        aria-label={i18nT('pages.artifactsPage.no_color')}
        title={i18nT('pages.artifactsPage.no_color')}
        onClick={(e) => { e.stopPropagation(); onPick('') }}
        onPointerDown={(e) => e.stopPropagation()}
        className={`rounded-full border cursor-pointer transition-transform hover:scale-110 flex items-center justify-center text-muted bg-transparent ${
          !value ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'
        }`}
        style={{ width: size, height: size }}
      >
        <X size={Math.max(8, size - 7)} />
      </button>
    </div>
  )
}

/** Folder glyph — same composition as the chat sidebar's FolderGlyph: the
 * Lucide Folder icon is always the icon (design-token colorable, CSS-sized,
 * fixed footprint), with the auto-derived emoji overlaid as a small badge on
 * the closed folder's flat face. Expanded folders show the open glyph alone
 * (its angled flap has no flat face for the badge). */
function FolderGlyph({ folder, size = 16, open = false }: { folder: ArtifactFolder; size?: number; open?: boolean }) {
  const Glyph = open ? FolderOpen : FolderIcon
  return (
    <span className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <Glyph size={size} className="shrink-0" style={{ color: folder.color || 'var(--accent)' }} />
      {folder.icon && !open && (
        <span
          aria-hidden
          className="absolute inset-x-0 bottom-0 flex items-center justify-center leading-none pointer-events-none"
          style={{ top: Math.round(size * 0.42), fontSize: Math.max(7, Math.round(size * 0.52)) }}
        >
          {folder.icon}
        </span>
      )}
    </span>
  )
}

/** Inline folder-name editor (create + rename) — the same native pattern the
 * chat sidebar uses for slot/folder renames: autofocused input, Enter commits,
 * Escape cancels, blur commits a non-empty value. IME-guarded. */
function FolderNameInput({ initial = '', placeholder = 'Folder name', onCommit, onCancel }: {
  initial?: string
  placeholder?: string
  onCommit: (name: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  const cancelledRef = useRef(false)
  const ime = useImeGuard()
  return (
    <Input
      autoFocus
      value={value}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(e) => setValue(e.target.value)}
      onFocus={(e) => e.target.select()}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      className="w-full bg-transparent border border-accent rounded px-1.5 py-0.5 text-text-strong outline-none text-sm select-text"
      {...ime.bindEnter<HTMLInputElement>({
        onEnter: () => { (document.activeElement as HTMLInputElement)?.blur() },
        onEscape: () => { cancelledRef.current = true; onCancel() },
        onBlur: () => {
          if (cancelledRef.current) { cancelledRef.current = false; return }
          const name = value.trim()
          if (name) onCommit(name)
          else onCancel()
        },
      })}
    />
  )
}

/** Shared "…" menu for a folder (gallery card + table row). The move submenu
 * excludes the folder's own subtree — a folder can't become its own descendant. */
function FolderMenu({ folder, folders, actions }: { folder: ArtifactFolder; folders: ArtifactFolder[]; actions: FolderActions }) {
  const moveTargets = folders.filter(f => !isDescendantFolder(folders, folder.id, f.id))
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          onClick={(e) => e.stopPropagation()}
          className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
          title={i18nT('pages.artifactsPage.folder_actions')}
          aria-label={i18nT('pages.artifactsPage.actions_for_folder', { name: folder.name })}
        >
          <MoreVertical size={13} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
        <DropdownMenuItem onSelect={() => actions.onRename(folder)}>
          <Pencil size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.rename')}
        </DropdownMenuItem>
        <FolderMoveSubmenu
          variant="dropdown"
          folders={moveTargets}
          currentFolderId={folder.parent_id || null}
          onPick={(pid) => actions.onMove(folder, pid || '')}
        />
        <DropdownMenuSeparator />
        {/* Color swatches live inline (not a menu item) so picking one doesn't
            navigate — the menu closes after the pick via the row's own click. */}
        <div className="px-2 py-1.5">
          <div className="text-[11px] text-muted mb-1.5">{i18nT('pages.artifactsPage.color')}</div>
          <FolderColorSwatches value={folder.color} onPick={(c) => actions.onSetColor(folder, c)} />
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem className="text-danger" onSelect={() => actions.onDelete(folder)}>
          <Trash2 size={13} className="shrink-0" /> {i18nT('pages.artifactsPage.delete')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Mini preview tile inside a gallery folder card — the same lazy full-fetch
 * the masonry cards use (shared ['artifact', slug] cache), clipped to a small
 * fixed-height tile so the folder reads as "a glimpse of what's inside". */
function FolderMiniThumb({ a }: { a: Artifact }) {
  const { data: full } = useQuery<Artifact>({
    queryKey: ['artifact', a.slug],
    queryFn: () => api.artifact(a.slug),
    staleTime: 60_000,
    enabled: !!a.slug,
  })
  const hasPreview = a.kind === 'widget' || a.kind === 'html'
  const content = full?.content || ''
  return (
    <div className="h-[84px] rounded-md border border-border overflow-hidden bg-bg-elevated pointer-events-none" title={a.name}>
      {a.kind === 'webapp' ? <WebAppThumb art={full ?? a} mini /> : a.kind === 'image' ? <ImageThumb a={a} /> : hasPreview ? <WidgetThumb content={content} slug={a.slug} /> : <ContentThumb content={content} kind={a.kind} />}
    </div>
  )
}

/** Gallery folder card: click to enter, draggable (nest via drop on another
 * folder card / breadcrumb), droppable (receives artifacts and folders).
 * Carries the same mr-3/mb-3 gutters the masonry cards use so folder cards
 * line up column-for-column with the gallery below. */
function FolderCard({ folder, folders, previewArtifacts, actions }: {
  folder: ArtifactFolder
  folders: ArtifactFolder[]
  previewArtifacts: Artifact[]
  actions: FolderActions
}) {
  const stats = folderSubtreeStats(folders, folder.id)
  const renaming = actions.renamingId === folder.id
  const preview = previewArtifacts.slice(0, 3)
  return (
    <DndDroppable id={`folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
      {({ setNodeRef: setDropRef, isOver }) => (
        <DndDraggable id={`folder:${folder.id}`} data={{ type: 'folder', id: folder.id, name: folder.name } satisfies LibraryDrag}>
          {({ setNodeRef: setDragRef, listeners, isDragging }) => (
            <div
              ref={(el) => { setDropRef(el); setDragRef(el) }}
              {...(renaming ? {} : listeners)}
              onClick={() => { if (!renaming) actions.onOpen(folder.id) }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (!renaming && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); actions.onOpen(folder.id) } }}
              aria-label={i18nT('pages.artifactsPage.open_folder', { name: folder.name })}
              className={`group mr-3 mb-3 rounded-lg border bg-card p-3 cursor-pointer transition-all hover:border-border-strong hover:shadow-md ${
                isOver ? 'border-accent ring-2 ring-accent/40 bg-accent/5' : 'border-border'
              }`}
              style={{
                opacity: isDragging ? 0.4 : 1,
                ...(folder.color && !isOver ? { borderLeft: `3px solid ${folder.color}` } : {}),
              }}
            >
              {/* Content glimpse: up to three mini previews of what's inside. */}
              <div className={`grid gap-1.5 mb-2.5 ${preview.length === 3 ? 'grid-cols-3' : preview.length === 2 ? 'grid-cols-2' : 'grid-cols-1'}`}>
                {preview.length > 0 ? (
                  preview.map((a) => <FolderMiniThumb key={a.slug} a={a} />)
                ) : (
                  <div className="h-[84px] rounded-md border border-dashed border-border flex items-center justify-center text-muted">
                    <FolderOpen size={22} className="opacity-50" />
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2">
                <FolderGlyph folder={folder} size={17} />
                <div className="min-w-0 flex-1">
                  {renaming ? (
                    <FolderNameInput
                      initial={folder.name}
                      placeholder={i18nT('pages.artifactsPage.rename_folder')}
                      onCommit={(name) => actions.onRenameSubmit(folder, name)}
                      onCancel={actions.onRenameCancel}
                    />
                  ) : (
                    <div className="text-[15px] leading-tight text-text-strong font-semibold truncate">{folder.name}</div>
                  )}
                  <div className="text-[11px] text-muted mt-0.5">
                    {i18nT('pages.artifactsPage.artifact', { count: stats.artifactCount })}
                    {stats.subfolderCount > 0 ? ` · ${stats.subfolderCount} folder${stats.subfolderCount === 1 ? '' : 's'}` : ''}
                  </div>
                </div>
                <div className="opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                  <FolderMenu folder={folder} folders={folders} actions={actions} />
                </div>
              </div>
            </div>
          )}
        </DndDraggable>
      )}
    </DndDroppable>
  )
}

/** Grid for folder cards using the same measurement + gutter scheme as
 * LibraryMasonry (-mr-3 container, cards carry mr-3/mb-3, identical 300px
 * min column width) so folder cards align column-for-column with the
 * masonry gallery below. */
function FolderCardGrid({ children }: { children: React.ReactNode }) {
  const [ref, cols] = useColumnCount(300)
  return (
    <div ref={ref} className="-mr-3">
      <div className="grid" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
        {children}
      </div>
    </div>
  )
}

/** Gallery breadcrumb: "All Artifacts › Parent › Current". Non-current
 * segments navigate on click and accept drops (move the dragged item up to
 * that level; the root segment unfiles). */
function FolderBreadcrumbBar({ folders, currentFolderId, onNavigate }: {
  folders: ArtifactFolder[]
  currentFolderId: string
  onNavigate: (folderId: string) => void
}) {
  const chain = folderBreadcrumb(folders, currentFolderId)
  const segment = (label: string, folderId: string, isCurrent: boolean) => (
    <DndDroppable key={folderId || 'root'} id={`crumb-drop:${folderId || 'root'}`} data={{ type: 'folder-drop', folderId }}>
      {({ setNodeRef, isOver }) => (
        <button
          ref={setNodeRef}
          type="button"
          disabled={isCurrent}
          onClick={() => onNavigate(folderId)}
          className={`px-1.5 py-0.5 rounded text-sm bg-transparent border-none transition-colors ${
            isCurrent
              ? 'text-text-strong font-medium cursor-default'
              : 'text-muted hover:text-text cursor-pointer'
          } ${isOver ? 'ring-2 ring-accent/40 text-text' : ''}`}
        >
          {label}
        </button>
      )}
    </DndDroppable>
  )
  return (
    <nav aria-label={i18nT('pages.artifactsPage.folder_breadcrumb')} className="flex items-center flex-wrap gap-0.5 mb-3">
      {segment(i18nT('pages.artifactsPage.all_artifacts'), '', chain.length === 0)}
      {chain.map((f, i) => (
        <span key={f.id} className="flex items-center gap-0.5">
          <ChevronRight size={12} className="text-muted" />
          {segment(f.name, f.id, i === chain.length - 1)}
        </span>
      ))}
    </nav>
  )
}

/** A single masonry card. Rendered by VirtuosoMasonry for each artifact. */
function LocalCardBody({ a, context }: { a: Artifact; context: LibCtx }) {
  const { onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug } = context
  // The list payload omits `content` (metadata only). Fetch the full artifact
  // lazily so the preview can render — virtualization means only on-screen
  // cards fetch, and the ['artifact', slug] key shares cache with the detail page.
  const { data: full } = useQuery<Artifact>({
    queryKey: ['artifact', a.slug],
    queryFn: () => api.artifact(a.slug),
    staleTime: 60_000,
    enabled: !!a.slug,
  })
  const deleting = deletingSlug === a.slug
  const hasPreview = a.kind === 'widget' || a.kind === 'html'
  const content = full?.content || ''
  // Author affordance: an imported artifact shows whose copy it came from; a
  // locally-authored artifact shows nothing (implicitly me).
  const author = a.source === 'import'
    ? (a.fork_metadata?.upstream_owner || a.publication?.published_by || '')
    : ''
  return (
    // Draggable onto folder cards / breadcrumb segments / table folder rows
    //. The sensors' activation constraints (see dndSensors) keep a plain click
    // and a finger swipe reaching the card / the gallery scroller.
    <DndDraggable id={`artifact:${a.slug}`} data={{ type: 'artifact', slug: a.slug, name: a.name, folderId: a.folder_id || '' } satisfies LibraryDrag}>
      {({ setNodeRef, listeners, isDragging }) => (
    <div
      ref={setNodeRef}
      {...listeners}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(a.slug)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen(a.slug)
        }
      }}
      style={{ opacity: isDragging ? 0.4 : 1 }}
      className="mb-3 mr-3 rounded-lg border border-border bg-card overflow-hidden hover:border-border-strong hover:shadow-md transition-all cursor-pointer"
    >
      {/* Preview is non-interactive so clicks fall through to the card's onClick. */}
      <div className="pointer-events-none">
        {a.kind === 'webapp' ? <WebAppThumb art={full ?? a} /> : a.kind === 'image' ? <ImageThumb a={a} /> : hasPreview ? <WidgetThumb content={content} slug={a.slug} /> : <ContentThumb content={content} kind={a.kind} />}
      </div>
      <div className="p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-text-strong font-medium truncate">{a.name}</span>
              {a.publication && (
                <Share2
                  size={12}
                  className={a.publication.last_error ? 'text-danger shrink-0' : 'text-ok shrink-0'}
                  aria-label={a.publication.last_error ? i18nT('pages.artifactsPage.published_sync_issue') : i18nT('pages.artifactsPage.published', { visibility: a.publication.visibility.toLowerCase() })}
                />
              )}
            </div>
            <code className="text-[11px] text-muted">{a.slug}</code>
            {author && <span className="block text-[11px] text-muted mt-0.5">{i18nT('pages.artifactsPage.by')} {author}</span>}
          </div>
          <Badge variant={KIND_BADGE[a.kind]}>{a.kind}</Badge>
        </div>
        {a.description && <div className="text-[12px] text-muted mt-1 line-clamp-2">{a.description}</div>}
        {a.tags && a.tags.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-2">
            {a.tags.map((t) => (
              <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted">{t}</span>
            ))}
          </div>
        )}
        <div className="flex items-center justify-between mt-2">
          <span className="text-[11px] text-muted">{i18nT('pages.artifactsPage.v')}{a.version} · {_timeAgo(isoToTs(a.updated_at))}</span>
          <div className="flex items-center gap-1">
            {/* Star sits FIRST: it is persistent state (and the retention
              * control that exempts an auto-registered widget from
              * prune_auto_widgets), so it reads apart from the one-shot
              * pop-out / delete actions that follow. Not overlaid on the
              * thumbnail — that layer is pointer-events-none so clicks fall
              * through to the card's onClick. */}
            <button
              type="button"
              disabled={pinningSlug === a.slug}
              onClick={(e) => { e.stopPropagation(); onTogglePin(a) }}
              className={`p-1 rounded transition-colors cursor-pointer bg-transparent border-none disabled:cursor-default ${a.pinned ? 'text-accent' : 'text-muted hover:text-accent'}`}
              title={a.pinned ? i18nT('pages.artifactsPage.starred_click_to_unstar') : i18nT('pages.artifactsPage.star_artifact')}
              aria-label={a.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_artifact')}
              aria-pressed={!!a.pinned}
            >
              {pinningSlug === a.slug
                ? <Loader2 size={13} className="animate-spin" />
                : <Star size={13} className={a.pinned ? 'fill-current' : ''} />}
            </button>
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); openPopout(a.slug, a.name) }}
              className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
              title={i18nT('pages.artifactsPage.pop_out_into_its_own_window')}
              aria-label={i18nT('pages.artifactsPage.pop_out_to_window')}
            >
              <ExternalLink size={13} />
            </button>
            <button
              type="button"
              disabled={deleting}
              onClick={(e) => { e.stopPropagation(); onDelete(a) }}
              className="p-1 rounded text-muted hover:text-danger transition-colors cursor-pointer bg-transparent border-none disabled:opacity-60 disabled:cursor-default"
              title={i18nT('pages.artifactsPage.remove_from_library')}
              aria-label={i18nT('pages.artifactsPage.remove_from_artifacts_library')}
            >
              {deleting ? <Loader2 size={13} className="animate-spin" /> : <X size={13} />}
            </button>
          </div>
        </div>
      </div>
    </div>
      )}
    </DndDraggable>
  )
}

const GridCard: ItemContent<GridEntry, LibCtx> = ({ data: entry, context }) => {
  // VirtuosoMasonry can pass an out-of-range (undefined) entry for one tick
  // while its internal list catches up to a shrunk data array; bail before any
  // field access (guards against the black-screen crash on an out-of-range entry).
  if (!entry) return null
  return <LocalCardBody a={entry.art} context={context} />
}

/**
 * Collapses its children to zero height when `collapsed`, freeing the space for
 * whatever follows in the flex column.
 *
 * Animates an explicitly MEASURED height rather than interpolating
 * `grid-template-rows` between `1fr` and `0fr`: that interpolation is not
 * implemented uniformly across engines, and the scroll defect this serves was
 * reported from WebKit — the same trap as shipping `overflow-clip-margin`, which
 * WebKit does not implement, to fix a WebKit-only clipping bug.
 *
 * The height stays `auto` until the first measurement lands, so nothing is
 * clipped on the first paint, and a ResizeObserver keeps the number honest when
 * the toolbar rewraps (a rotation, a longer tag name, a locale change).
 */
function CollapsibleChrome({ collapsed, children }: { collapsed: boolean; children: React.ReactNode }) {
  const inner = useRef<HTMLDivElement | null>(null)
  const shell = useRef<HTMLDivElement | null>(null)
  const [natural, setNatural] = useState<number | null>(null)

  useEffect(() => {
    const el = inner.current
    if (!el) return
    const measure = () => setNatural(el.offsetHeight)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // `inert` is not a typed React 18 prop, and it is what keeps a zero-height
  // toolbar out of the tab order and off the accessibility tree instead of
  // leaving invisible-but-focusable controls behind. Engines without it simply
  // ignore the attribute.
  useEffect(() => {
    shell.current?.toggleAttribute('inert', collapsed)
  }, [collapsed])

  return (
    <div
      ref={shell}
      {...{ [CHROME_ATTR]: '' }}
      className="overflow-hidden transition-[height,opacity] ease-out motion-reduce:transition-none"
      // Duration comes from the hook's constant, not a Tailwind `duration-*`
      // class: the settle window that stops the collapse from re-triggering is
      // derived from the same number, and a hardcoded class here would let the
      // two drift apart silently.
      style={{
        height: collapsed ? 0 : (natural ?? undefined),
        opacity: collapsed ? 0 : 1,
        transitionDuration: `${COLLAPSE_MS}ms`,
      }}
    >
      {/* `flow-root` is load-bearing, not cosmetic. Without it this wrapper is
        * not a block-formatting context, so the last child's bottom margin
        * (the toolbar's `mb-3`) collapses THROUGH it and is not counted in
        * `offsetHeight` — the measured height then comes out 12px short, the
        * shell clips exactly that margin, and the gap between the chrome and
        * the section below it disappears. */}
      <div ref={inner} className="flow-root">{children}</div>
    </div>
  )
}

/** Card count at which the gallery switches from a content-sized CSS-columns
 *  masonry to the virtualized one.
 *
 *  Module scope because TWO places decide on it: the gallery picks its render
 *  mode, and the page decides which element owns vertical scrolling. Virtualized
 *  mode brings its own scroller, so if these two ever read different numbers the
 *  page ends up with two same-axis scrollers again — the exact defect the
 *  single-scroller plumbing below exists to remove. */
export const VIRTUALIZE_AT = 30

/** Height-cache partition for the gallery. NOT a chat session id — it is
 *  exempted in `utils/storageGc` (`RESERVED_NAMESPACES`) so the startup pass
 *  does not read it as a dead session and wipe it. */
const ARTIFACT_HEIGHT_NS = 'artifacts-gallery'

/** Reserve for a card whose height has never been measured. The cache's own
 *  measured heights replace this per card; it only sets the very first paint. */
const GALLERY_ESTIMATED_CARD_H = 260

/** One column of artifact cards, windowed by this app's own virtualizer.
 *
 *  `react-virtuoso` was measured keeping the main thread ~25% busy THROUGH a
 *  swipe (53 layouts over six swipes) because it maintains a ResizeObserver per
 *  mounted item and recomputes continuously. `useVirtualChat` is built the other
 *  way on that hot path: a PASSIVE scroll listener, at most ONE rAF-coalesced
 *  window recompute per frame, computed as arithmetic over CACHED heights rather
 *  than by measuring, and ResizeObserver-driven work held back until the scroll
 *  settles (`SCROLL_SETTLE_MS`) so it cannot fire mid-fling. It also accepts an
 *  external scroller, which is the capability this page needs — the page column
 *  has to keep the axis.
 *
 *  `followOutput: false` because a gallery is not a transcript: appends must not
 *  pull the viewport to the bottom. */
function LibraryList({
  entries,
  context,
  scrollerRef,
}: {
  entries: GridEntry[]
  context: LibCtx
  scrollerRef: React.RefObject<HTMLDivElement | null>
}) {
  const virt = useVirtualChat<GridEntry>({
    items: entries,
    getKey: (e) => e.key,
    sessionId: ARTIFACT_HEIGHT_NS,
    estimatedHeight: GALLERY_ESTIMATED_CARD_H,
    // Deliberately tight. Every mounted card is a live sandboxed document, so the
    // window size IS the cost here: measured at the default (5 each way) the page
    // held 12-19 iframes and the scroll phase ran 31% busy over 63 layouts,
    // against 3-5 iframes and 25% over 53 at a tight window. A gallery has no
    // streaming tail to keep warm, so it has no reason to hold a wide span.
    overscan: 1,
    followOutput: false,
    // A gallery opens at the HEAD, not the chat default tail. Beyond the
    // landing position this kills a mount-time flicker loop: opening at the
    // tail puts every unmeasured card ABOVE the viewport, so each real
    // measurement forces a scrollTop compensation write; at the head the
    // corrections all land in the bottom spacer, out of sight.
    initialPlacement: 'top',
    // Mixed HTML/GIF/image cards vary widely around the estimate, and the
    // debounced first-measure sync starves under a scroll-driven mounting
    // streak — each handoff to the before-spacer then bounces the viewport by
    // (real − estimate). See the option doc.
    eagerFirstMeasure: true,
    externalScrollerRef: scrollerRef,
  })
  return (
    <div data-testid="artifacts-gallery-list">
      {/* Sentinels drive window expansion at the ends. */}
      <div ref={virt.topSentinelRef} aria-hidden style={{ height: 1 }} />
      {/* Spacers stand in for everything outside the mounted window so the
        * scrollbar stays honest while only the window is real DOM.
        * overflow-anchor:none so the browser anchors on real content rather than
        * on a spacer that resizes as the window moves. */}
      <div aria-hidden style={{ height: virt.offsetBefore, overflowAnchor: 'none' }} />
      {virt.virtualItems.map((vi) =>
        vi.mounted ? (
          // The measure ref is what feeds the height cache; without it a card's
          // real height is never learned and every reserve stays an estimate.
          // `flow-root` is load-bearing: the card inside carries the list gap
          // as its own mb-3, and without a BFC that margin COLLAPSES THROUGH
          // this wrapper — offsetHeight then under-reports every row by 12px,
          // the offset tree accumulates the error (~96px per viewport of short
          // cards), and engines without native scroll anchoring (iOS Safari)
          // render the drift as a visible bounce at content-determined, fixed
          // positions. Chrome's anchoring silently absorbs it, which is why a
          // Chromium probe shows nothing.
          <div key={vi.key} ref={virt.measureRef(vi.index)} className="flow-root">
            <MasonryCard data={vi.data} context={context} index={vi.index} />
          </div>
        ) : (
          <div key={vi.key} aria-hidden style={{ height: vi.height, overflowAnchor: 'none' }} />
        ),
      )}
      <div aria-hidden style={{ height: virt.offsetAfter, overflowAnchor: 'none' }} />
      <div ref={virt.bottomSentinelRef} aria-hidden style={{ height: 1 }} />
    </div>
  )
}

/** The "Your Artifacts" grid of local artifacts. */
function LibraryMasonry({
  entries,
  cols,
  widthRef,
  scrollerRef,
  onOpen,
  onDelete,
  deletingSlug,
  onTogglePin,
  pinningSlug,
}: {
  entries: GridEntry[]
  /** Measured ONCE, at the page level. The page reads the same number to decide
   *  who owns the scroll axis, so measuring it a second time here could disagree
   *  at a boundary width and leave the page with two same-axis scrollers — the
   *  trap `VIRTUALIZE_AT` is hoisted to module scope to avoid. */
  cols: number
  /** Attached to the width-defining wrapper below so the page's measurement is
   *  taken from the element that actually lays the columns out. */
  widthRef: React.RefObject<HTMLDivElement>
  /** The page's scrolling column. A ref, not the resolved element: the
   *  virtualizer takes `externalScrollerRef` and reads it when it needs it, so
   *  nothing has to re-render just because the element appeared. */
  scrollerRef: React.RefObject<HTMLDivElement | null>
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
}) {
  const context = useMemo<LibCtx>(
    () => ({ onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug }),
    [onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug],
  )
  // Below this count, render a content-sized CSS-columns masonry so the
  // gallery takes only the height its cards need — no reserved blank space.
  // At or above it the gallery virtualizes so a large library of iframe-preview
  // cards stays performant.
  const virtualized = entries.length >= VIRTUALIZE_AT
  // ONE column is not a waterfall, it is a list — and a list can be windowed
  // against an EXTERNAL scroller (`LibraryList`). `VirtuosoMasonry` cannot: its
  // whole prop surface is columnCount/data/context/ItemContent/initialItemCount/
  // useWindowScroll, so a virtualized masonry can only ever own a scroller of its
  // own. Handing the axis to the page instead is what keeps every section above
  // and below the gallery reachable by scrolling, and it is free here because at
  // one column the two layouts render the same thing.
  const asList = virtualized && cols === 1
  // The masonry owns the axis only when it is actually a masonry. This must stay
  // in lockstep with the page's own `galleryOwnsScroll`.
  const masonryOwnsScroll = virtualized && cols > 1
  return (
    // -mr-3 offsets each card's own mr-3 so the trailing column's gutter
    // doesn't add page width; cards carry mr-3 (gutter) + mb-3 (row gap).
    //
    // Only the masonry needs to fill the page's content column (`flex-1
    // min-h-0`, which is what lets a flex child shrink to its parent instead of
    // its content). A list scrolling inside the page column is content-sized.
    <div ref={widthRef} className={masonryOwnsScroll ? '-mr-3 flex-1 min-h-0' : '-mr-3'}>
      {asList ? (
        <LibraryList entries={entries} context={context} scrollerRef={scrollerRef} />
      ) : masonryOwnsScroll ? (
        <VirtuosoMasonry
          key={cols}
          columnCount={cols}
          data={entries}
          context={context}
          ItemContent={GridCard}
          // 100% of the flex-sized parent, NOT a viewport fraction: a `72vh`
          // box does not know how much room the toolbar and folder rows above
          // it already took, so it overflowed the page column and forced a
          // second scroller into existence.
          style={{ height: '100%' }}
        />
      ) : (
        <div style={{ columnCount: cols, columnGap: 0 }}>
          {entries.map((e, i) => (
            <MasonryGridItem key={e.key} data={e} context={context} index={i} />
          ))}
        </div>
      )}
    </div>
  )
}

// Non-virtualized wrapper so small galleries use a content-sized CSS-columns
// layout (no reserved blank height) while reusing the exact same card renderer
// the virtualized masonry uses. break-inside-avoid keeps a card whole within a
// column. GridCard is an ItemContent (FC|ComponentClass union), so it must be
// rendered as a JSX element, not called.
const MasonryCard = GridCard
function MasonryGridItem({ data, context, index }: { data: GridEntry; context: LibCtx; index: number }) {
  return (
    <div className="break-inside-avoid">
      <MasonryCard data={data} context={context} index={index} />
    </div>
  )
}

/** Column headers shared by the flat table and the folder tree table. Data
 * columns sort on click (asc → desc → default); the star and Actions columns
 * are control columns and stay plain. */
function LibraryTableHead({ sort, onSort }: { sort: SortState; onSort: (key: SortKey) => void }) {
  const th = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
  const sortable = (key: SortKey, label: string, extra: string) => {
    const active = sort?.key === key
    return (
      <th
        className={`${th} ${extra}`}
        aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
      >
        <Btn
          type="button"
          onClick={() => onSort(key)}
          className={`bg-transparent border-none p-0 gap-1 rounded-none text-[12px] font-medium uppercase tracking-[.04em] hover:bg-transparent active:scale-100 ${active ? 'text-text hover:text-text' : 'text-muted hover:text-text'}`}
        >
          {label}
          {active && (sort.dir === 'asc'
            ? <ChevronUp size={12} className="shrink-0" aria-hidden="true" />
            : <ChevronDown size={12} className="shrink-0" aria-hidden="true" />)}
        </Btn>
      </th>
    )
  }
  return (
    <thead>
      <tr>
        <th className={`${th} w-[40px] text-center`} aria-label={i18nT('pages.artifactsPage.starred')}></th>
        {sortable('name', i18nT('pages.artifactsPage.name'), 'min-w-[160px]')}
        {sortable('slug', i18nT('pages.artifactsPage.slug'), 'w-[180px]')}
        {sortable('kind', i18nT('pages.artifactsPage.kind'), 'w-[100px]')}
        {sortable('source', i18nT('pages.artifactsPage.source'), 'w-[110px]')}
        {sortable('version', i18nT('pages.artifactsPage.ver'), 'w-[60px]')}
        {sortable('tags', i18nT('pages.artifactsPage.tags'), 'min-w-[160px]')}
        {sortable('updated', i18nT('pages.artifactsPage.updated'), 'w-[110px]')}
        <th className={`${th} w-[120px]`}>{i18nT('pages.artifactsPage.actions')}</th>
      </tr>
    </thead>
  )
}

/** One artifact row, shared by the flat table and the folder tree. Draggable
 * onto folder rows / the Unfiled lane (indent nests it under its folder). */
function ArtifactRow({ a, onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug = null, indent = 0, dropFolderId, dropHighlight = false }: {
  a: Artifact
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  /** Toggle the artifact's pin/favorite mark. */
  onTogglePin: (a: Artifact) => void
  /** Slug whose pin toggle is in flight (disables its star to avoid double-fire). */
  pinningSlug?: string | null
  indent?: number
  /** When set, the row also accepts drops, filing the dragged item into this
   * folder (''=unfile) — so dropping anywhere over an expanded folder's
   * region (or the Unfiled section) works, not just on the header row. */
  dropFolderId?: string
  /** True while the active drag hovers anywhere over this row's folder region. */
  dropHighlight?: boolean
}) {
  const inner = (setDropRef?: (el: HTMLElement | null) => void) => (
    <DndDraggable id={`artifact-row:${a.slug}`} data={{ type: 'artifact', slug: a.slug, name: a.name, folderId: a.folder_id || '' } satisfies LibraryDrag}>
      {({ setNodeRef, listeners, isDragging }) => (
        <tr
          ref={(el) => { setNodeRef(el); setDropRef?.(el) }}
          {...listeners}
          style={{ opacity: isDragging ? 0.4 : 1 }}
          className={`transition-colors cursor-pointer ${dropHighlight ? 'bg-accent/10' : 'hover:bg-bg-hover'}`}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey) {
              openPopout(a.slug, a.name)
            } else {
              onOpen(a.slug)
            }
          }}
        >
          <td className="px-2.5 py-2 border-b border-border text-center">
            <button
              type="button"
              disabled={pinningSlug === a.slug}
              onClick={(e) => { e.stopPropagation(); onTogglePin(a) }}
              className={`p-0.5 rounded transition-colors cursor-pointer bg-transparent border-none disabled:cursor-default ${a.pinned ? 'text-accent' : 'text-muted/40 hover:text-accent'}`}
              title={a.pinned ? i18nT('pages.artifactsPage.starred_click_to_unstar') : i18nT('pages.artifactsPage.star_artifact')}
              aria-label={a.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_artifact')}
              aria-pressed={!!a.pinned}
            >
              <Star size={14} className={a.pinned ? 'fill-current' : ''} />
            </button>
          </td>
          <td className="px-2.5 py-2 border-b border-border" style={indent > 0 ? { paddingLeft: `${10 + indent * 20}px` } : undefined}>
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-text-strong font-medium">{a.name}</span>
              {a.publication && (
                <Share2
                  size={12}
                  className={a.publication.last_error ? 'text-danger' : 'text-ok'}
                  aria-label={a.publication.last_error ? i18nT('pages.artifactsPage.published_sync_issue') : i18nT('pages.artifactsPage.published', { visibility: a.publication.visibility.toLowerCase() })}
                />
              )}
            </div>
            {a.description && <div className="text-[12px] text-muted truncate max-w-[400px]">{a.description}</div>}
          </td>
          <td className="px-2.5 py-2 border-b border-border">
            <code className="text-[12px] text-muted">{a.slug}</code>
          </td>
          <td className="px-2.5 py-2 border-b border-border">
            <Badge variant={KIND_BADGE[a.kind]}>{a.kind}</Badge>
          </td>
          <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted truncate max-w-[180px]" title={a.session_title || a.source}>{a.session_title || a.source}</td>
          <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{i18nT('pages.artifactsPage.v')}{a.version}</td>
          <td className="px-2.5 py-2 border-b border-border">
            <div className="flex flex-wrap gap-1">
              {(a.tags || []).map((t) => (
                <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted">{t}</span>
              ))}
            </div>
          </td>
          <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">{_timeAgo(isoToTs(a.updated_at))}</td>
          <td className="px-2.5 py-2 border-b border-border">
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); openPopout(a.slug, a.name) }}
                className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                title={i18nT('pages.artifactsPage.pop_out_into_its_own_window')}
                aria-label={i18nT('pages.artifactsPage.pop_out_to_window')}
              >
                <ExternalLink size={13} />
              </button>
              <button
                type="button"
                disabled={deletingSlug === a.slug}
                onClick={(e) => { e.stopPropagation(); onDelete(a) }}
                className="p-1 rounded text-muted hover:text-danger transition-colors cursor-pointer bg-transparent border-none disabled:opacity-60 disabled:cursor-default"
                title={i18nT('pages.artifactsPage.remove_from_library')}
                aria-label={i18nT('pages.artifactsPage.remove_from_artifacts_library')}
              >
                {deletingSlug === a.slug ? <Loader2 size={13} className="animate-spin" /> : <X size={13} />}
              </button>
            </div>
          </td>
        </tr>
      )}
    </DndDraggable>
  )
  if (dropFolderId === undefined) return inner()
  return (
    <DndDroppable id={`row-drop:${a.slug}`} data={{ type: 'folder-drop', folderId: dropFolderId }}>
      {({ setNodeRef }) => inner(setNodeRef)}
    </DndDroppable>
  )
}

/** The star-to-materialize affordance shared by the table/tree rows and the
 * gallery section, so the two views cannot drift (this PR is already the
 * second "feature existed in one view only" fix of this class). */
function SessionDocStar({ d, busy, onMaterialize }: { d: SessionDoc; busy: boolean; onMaterialize: (path: string, sessionKey?: string) => void }) {
  return (
    <IconButton
      variant="accent"
      disabled={busy}
      onClick={() => onMaterialize(d.path, d.session_key)}
      title={i18nT('pages.artifactsPage.star_creates_a_starred_artifact_from_this_docume')}
      aria-label={i18nT('pages.artifactsPage.star_document')}
      className="shrink-0"
    >
      {busy ? <Loader2 size={14} className="animate-spin" /> : <Star size={14} />}
    </IconButton>
  )
}

/** A single unsaved session-document row (from "your chats"). Leading star
 * materializes it into a real, starred artifact. Shares the same columns as
 * ArtifactRow so both live in one unified table. */
function SessionDocRow({ d, busy, onMaterialize }: { d: SessionDoc; busy: boolean; onMaterialize: (path: string, sessionKey?: string) => void }) {
  const ftype = docFileType(d.path)
  return (
    <tr className="transition-colors hover:bg-bg-hover">
      <td className="px-2.5 py-2 border-b border-border text-center">
        <SessionDocStar d={d} busy={busy} onMaterialize={onMaterialize} />
      </td>
      <td className="px-2.5 py-2 border-b border-border">
        <div className="flex items-center gap-1.5 min-w-0">
          <FileText size={13} className="text-ok shrink-0" />
          <span className="text-sm text-text-strong font-medium truncate">{d.name}</span>
        </div>
        <div className="text-[11px] text-muted truncate max-w-[420px]">{d.path}</div>
      </td>
      <td className="px-2.5 py-2 border-b border-border"><code className="text-[12px] text-muted">—</code></td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">{ftype}</td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted truncate max-w-[180px]" title={d.session_title}>{d.session_title}</td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted">—</td>
      <td className="px-2.5 py-2 border-b border-border"></td>
      <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted whitespace-nowrap">{_timeAgo(isoToTs(d.updated_at))}</td>
      <td className="px-2.5 py-2 border-b border-border"></td>
    </tr>
  )
}

/** Unsaved session documents in the GALLERY view. The table and tree views
 * fold these into their rows (SessionDocRow) — but the gallery is the DEFAULT
 * view, so without this section a document badged "Artifact" in the chat
 * transcript is invisible on this page until the user discovers the table
 * toggle. Same affordance as SessionDocRow: the leading star materializes the
 * document into a real, starred artifact. */
/* Cap the docs section so an active user's cross-session firehose cannot push
 * the saved library — the page's primary content — below the fold (the same
 * burial this section exists to cure, inverted). Same disclosure pattern as
 * FileChangeChips' COLLAPSED_COUNT. */
const SESSION_DOCS_COLLAPSED = 5
const SESSION_DOCS_COLLAPSE_KEY = 'mc-artifacts-session-docs-collapsed'

function SessionDocsGallery({ docs, pending, onMaterialize, materializingPath }: {
  docs: SessionDoc[]
  /** True while the session-docs query is in flight — renders a fixed-height
   *  skeleton so the section does not pop in and shift the gallery under the
   *  user's cursor once the query resolves. */
  pending: boolean
  onMaterialize: (path: string, sessionKey?: string) => void
  materializingPath: string | null
}) {
  const [expanded, setExpanded] = useState(false)
  // Persisted: a user who never intends to save these docs can put the section
  // away for good; the header stays as a one-click way back.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SESSION_DOCS_COLLAPSE_KEY) === '1',
  )
  const listRef = useRef<HTMLDivElement>(null)
  const toggleCollapsed = () => {
    setCollapsed((v) => {
      safeSetItem(SESSION_DOCS_COLLAPSE_KEY, v ? '' : '1')
      return !v
    })
  }
  // A successful materialize unmounts its row; without this, focus falls to
  // <body> and keyboard users lose their place. Re-anchor on the list.
  const handleMaterialize = (path: string, sessionKey?: string) => {
    onMaterialize(path, sessionKey)
    listRef.current?.focus()
  }
  if (pending && !docs.length) {
    // Fixed-height placeholder (~header + one row) reserving the slot.
    return (
      <Card className="mt-0 p-3" aria-busy="true">
        <div className="h-[24px] w-40 rounded bg-bg-hover animate-pulse mb-2" />
        <div className="h-[32px] rounded-lg bg-bg-hover animate-pulse" />
      </Card>
    )
  }
  if (!docs.length) return null
  const overflow = docs.length > SESSION_DOCS_COLLAPSED
  const visible = overflow && !expanded ? docs.slice(0, SESSION_DOCS_COLLAPSED) : docs
  return (
    <Card className="mt-0 p-3">
      <CardTitle className={collapsed ? 'mb-0 px-1' : 'mb-2 px-1'}>
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-expanded={!collapsed}
          className="flex items-center gap-2 bg-transparent border-none p-0 cursor-pointer text-inherit font-inherit"
        >
          {collapsed ? <ChevronRight size={14} className="shrink-0 text-muted" /> : <ChevronDown size={14} className="shrink-0 text-muted" />}
          {i18nT('pages.artifactsPage.from_your_chats')}
          {collapsed && <span className="text-muted font-normal">({docs.length})</span>}
        </button>
      </CardTitle>
      {!collapsed && (
      <>
      {/* Expanded, this list is the full session-doc set (hundreds of rows). It
        * lives inside the page column, which hands its scroll axis to the
        * gallery once that virtualizes — so an unbounded list here is CLIPPED
        * with no way to reach the rest of it. Cap it and let it scroll itself.
        * Collapsed to five rows it needs no cap, so the common case still has a
        * single scroller on the page. */}
      <div
        ref={listRef}
        tabIndex={-1}
        className={`flex flex-col gap-0.5 outline-none ${expanded ? 'max-h-[40vh] overflow-y-auto' : ''}`}
      >
        {visible.map((d) => (
          <div key={d.path} className="flex items-center gap-2.5 px-2 py-1.5 rounded-lg">
            <SessionDocStar d={d} busy={materializingPath === d.path} onMaterialize={handleMaterialize} />
            <FileText size={13} className="text-ok shrink-0" />
            <span className="text-sm text-text-strong font-medium truncate min-w-0 max-w-[280px]">{d.name}</span>
            <span className="text-[11px] text-muted truncate min-w-0 flex-1">{d.path}</span>
            <span className="text-[12px] text-muted truncate min-w-0 max-w-[180px]" title={d.session_title}>{d.session_title}</span>
            <span className="text-[12px] text-muted whitespace-nowrap shrink-0">{_timeAgo(isoToTs(d.updated_at))}</span>
          </div>
        ))}
      </div>
      {/* OUTSIDE the scrollable list on purpose: inside it, "Show less" sat
        * after the last of hundreds of rows, so the control that undoes the
        * expansion was itself only reachable by scrolling past everything the
        * expansion added. */}
      {overflow && (
        <Btn
          onClick={() => setExpanded((v) => !v)}
          className="justify-center w-full px-2 py-1.5 mt-1 rounded-lg text-[11.5px] font-medium border-none"
          aria-expanded={expanded}
        >
          {expanded
            ? <><ChevronUp size={13} className="shrink-0" /> {i18nT('pages.artifactsPage.show_less')}</>
            : <><ChevronDown size={13} className="shrink-0" /> {i18nT('pages.artifactsPage.show_all_count', { count: docs.length })}</>}
        </Btn>
      )}
      </>
      )}
    </Card>
  )
}

/** The compact table view of the local artifact library (flat —
 * rendered while any filter is active, when folder scoping is bypassed). */
function LibraryTable({
  items,
  sort,
  onSort,
  onOpen,
  onDelete,
  deletingSlug,
  onTogglePin,
  pinningSlug,
  sessionDocs = [],
  onMaterialize,
  materializingPath = null,
}: {
  items: Artifact[]
  sort: SortState
  onSort: (key: SortKey) => void
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
  sessionDocs?: SessionDoc[]
  onMaterialize?: (path: string, sessionKey?: string) => void
  materializingPath?: string | null
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse table-striped">
        <LibraryTableHead sort={sort} onSort={onSort} />
        <tbody>
          {items.map((a) => (
            <ArtifactRow key={a.slug} a={a} onOpen={onOpen} onDelete={onDelete} deletingSlug={deletingSlug} onTogglePin={onTogglePin} pinningSlug={pinningSlug} />
          ))}
          {onMaterialize && sessionDocs.map((d) => (
            <SessionDocRow key={d.path} d={d} busy={materializingPath === d.path} onMaterialize={onMaterialize} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Folder header row in the tree table: collapsible (chevron / row click),
 * draggable (reorder among siblings, nest elsewhere), droppable. */
function FolderRow({ folder, folders, depth, expanded, onToggle, actions, dropHighlight = false }: {
  folder: ArtifactFolder
  folders: ArtifactFolder[]
  depth: number
  expanded: boolean
  onToggle: (id: string) => void
  actions: FolderActions
  /** True while the active drag hovers anywhere over this folder's region
   * (header row or any row inside it) — lights the whole folder up. */
  dropHighlight?: boolean
}) {
  const stats = folderSubtreeStats(folders, folder.id)
  const Chevron = expanded ? ChevronDown : ChevronRight
  const renaming = actions.renamingId === folder.id
  return (
    <DndDroppable id={`folder-row-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
      {({ setNodeRef: setDropRef, isOver }) => (
        <DndDraggable id={`folder-row:${folder.id}`} data={{ type: 'folder', id: folder.id, name: folder.name } satisfies LibraryDrag}>
          {({ setNodeRef: setDragRef, listeners, isDragging }) => (
            <tr
              ref={(el) => { setDropRef(el); setDragRef(el) }}
              {...(renaming ? {} : listeners)}
              onClick={() => { if (!renaming) onToggle(folder.id) }}
              style={{ opacity: isDragging ? 0.4 : 1 }}
              className={`group cursor-pointer transition-colors ${isOver || dropHighlight ? 'bg-accent/15' : 'hover:bg-bg-hover'}`}
              aria-expanded={expanded}
            >
              <td colSpan={9} className="px-2.5 py-1.5 border-b border-border" style={depth > 0 ? { paddingLeft: `${10 + depth * 20}px` } : undefined}>
                <div className={`flex items-center gap-1.5 rounded transition-shadow ${isOver || dropHighlight ? 'ring-2 ring-inset ring-accent/50 px-1 -mx-1' : ''}`}>
                  <Chevron size={13} className="text-muted shrink-0" />
                  <FolderGlyph folder={folder} size={14} open={expanded} />
                  {renaming ? (
                    <span className="min-w-0 flex-1 max-w-[280px]">
                      <FolderNameInput
                        initial={folder.name}
                        placeholder={i18nT('pages.artifactsPage.rename_folder')}
                        onCommit={(name) => actions.onRenameSubmit(folder, name)}
                        onCancel={actions.onRenameCancel}
                      />
                    </span>
                  ) : (
                    <span className="text-sm text-text-strong font-medium truncate">{folder.name}</span>
                  )}
                  <span className="text-[11px] text-muted">
                    {stats.artifactCount}{stats.subfolderCount > 0 ? ` · ${stats.subfolderCount} folder${stats.subfolderCount === 1 ? '' : 's'}` : ''}
                  </span>
                  <span className="ml-auto opacity-0 group-hover:opacity-100 transition-opacity">
                    <FolderMenu folder={folder} folders={folders} actions={actions} />
                  </span>
                </div>
              </td>
            </tr>
          )}
        </DndDraggable>
      )}
    </DndDroppable>
  )
}

/** Nested, collapsible tree table (browse mode): folders in pre-order with
 * their artifacts indented beneath, Unfiled at the end. Collapsed by default —
 * expansion is client-local (localStorage), by design (§2.5). */
function LibraryTree({ items, sort, onSort, folders, expandedIds, onToggleExpand, folderActions, onOpen, onDelete, deletingSlug, onTogglePin, pinningSlug, overFolderId, dragActive, sessionDocs = [], onMaterialize, materializingPath = null }: {
  items: Artifact[]
  sort: SortState
  onSort: (key: SortKey) => void
  folders: ArtifactFolder[]
  expandedIds: ReadonlySet<string>
  onToggleExpand: (id: string) => void
  folderActions: FolderActions
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
  /** Folder the active drag currently hovers (''=Unfiled, null=none). */
  overFolderId: string | null
  /** True while any library drag is in flight. */
  dragActive: boolean
  sessionDocs?: SessionDoc[]
  onMaterialize?: (path: string, sessionKey?: string) => void
  materializingPath?: string | null
}) {
  const folderIds = new Set(folders.map(f => f.id))
  const byFolder = new Map<string, Artifact[]>()
  for (const a of items) {
    // Dangling folder_id (deleted folder) degrades to Unfiled.
    const fid = a.folder_id && folderIds.has(a.folder_id) ? a.folder_id : ''
    const bucket = byFolder.get(fid)
    if (bucket) bucket.push(a)
    else byFolder.set(fid, [a])
  }
  const rows: React.ReactNode[] = []
  const walk = (parentId: string, depth: number, visited: Set<string>) => {
    for (const f of childFolders(folders, parentId)) {
      if (visited.has(f.id) || depth > 20) continue
      visited.add(f.id)
      const expanded = expandedIds.has(f.id)
      rows.push(
        <FolderRow
          key={`folder:${f.id}`}
          folder={f}
          folders={folders}
          depth={depth}
          expanded={expanded}
          onToggle={onToggleExpand}
          actions={folderActions}
          dropHighlight={overFolderId === f.id}
        />,
      )
      if (expanded) {
        for (const a of byFolder.get(f.id) || []) {
          rows.push(
            <ArtifactRow
              key={a.slug}
              a={a}
              onOpen={onOpen}
              onDelete={onDelete}
              deletingSlug={deletingSlug}
              onTogglePin={onTogglePin}
              pinningSlug={pinningSlug}
              indent={depth + 1}
              dropFolderId={f.id}
              dropHighlight={overFolderId === f.id}
            />,
          )
        }
        walk(f.id, depth + 1, visited)
      }
    }
  }
  walk('', 0, new Set())
  const unfiled = byFolder.get('') || []
  const unfiledHot = overFolderId === ''
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse table-striped">
        <LibraryTableHead sort={sort} onSort={onSort} />
        <tbody>
          {rows}
          {folders.length > 0 && (
            <DndDroppable id="unfiled-lane" data={{ type: 'folder-drop', folderId: '' }}>
              {({ setNodeRef, isOver }) => (
                <tr ref={setNodeRef} className={`transition-colors ${isOver || unfiledHot ? 'bg-accent/15' : ''}`}>
                  <td colSpan={9} className="px-2.5 border-b border-border" style={{ paddingTop: dragActive ? 10 : 6, paddingBottom: dragActive ? 10 : 6 }}>
                    <div className={`flex items-center gap-2 rounded transition-all ${
                      dragActive ? `border border-dashed px-2 py-1.5 ${isOver || unfiledHot ? 'border-accent text-text' : 'border-border text-muted'}` : ''
                    }`}>
                      <span className="text-[11px] uppercase tracking-[.04em] text-muted font-medium">
                        {i18nT('pages.artifactsPage.unfiled')} {unfiled.length}
                      </span>
                      {dragActive && (
                        <span className="text-[11px] text-muted italic">{i18nT('pages.artifactsPage.drop_here_to_unfile')}</span>
                      )}
                    </div>
                  </td>
                </tr>
              )}
            </DndDroppable>
          )}
          {unfiled.map((a) => (
            <ArtifactRow
              key={a.slug}
              a={a}
              onOpen={onOpen}
              onDelete={onDelete}
              deletingSlug={deletingSlug}
              onTogglePin={onTogglePin}
              pinningSlug={pinningSlug}
              dropFolderId={folders.length > 0 ? '' : undefined}
              dropHighlight={unfiledHot}
            />
          ))}
          {onMaterialize && sessionDocs.map((d) => (
            <SessionDocRow key={d.path} d={d} busy={materializingPath === d.path} onMaterialize={onMaterialize} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ArtifactsPage() {  const navigate = useNavigate()
  const qc = useQueryClient()
  // Hides the AWS deploy console entry when the platform withholds cloud
  // deployment — otherwise the option is visible and only explains itself after
  // a click.
  const cloudDeployEnabled = useCloudDeploymentEnabled()
  const [filter, setFilter] = useState('')
  const isMobile = useIsMobile()
  const [tagFilter, setTagFilter] = useState('')
  const [kindFilter, setKindFilter] = useState<string>('')
  // Default to "All" artifacts, but remember the last visit's choice: if the
  // user last selected "Starred", start there again.
  const [pinnedOnly, setPinnedOnly] = useState(
    () => localStorage.getItem('mc-artifacts-pinned-only') === '1',
  )
  const [view, setView] = useState<'grid' | 'table'>(
    () => (localStorage.getItem('mc-artifacts-view') === 'table' ? 'table' : 'grid'),
  )
  // Table column sort persists beside the view choice; null renders the
  // server's order, and stale storage safely falls back to that default.
  const [sort, setSort] = useState<SortState>(readPersistedSort)
  const handleSort = useCallback((key: SortKey) => {
    const next: SortState = sort?.key !== key
      ? { key, dir: 'asc' }
      : sort.dir === 'asc'
        ? { key, dir: 'desc' }
        : null
    setSort(next)
    safeSetItem(ARTIFACT_SORT_STORAGE_KEY, JSON.stringify(next))
  }, [sort])

  // ── Folder browse scope ──────────────────────────────────────
  // The open folder rides the URL (?folder=<id>) so gallery navigation is
  // back-button-friendly and linkable. Any active filter bypasses folder
  // scoping entirely — matches show flat across all folders.
  const [searchParams, setSearchParams] = useSearchParams()
  const currentFolderId = searchParams.get('folder') || ''
  const openFolder = useCallback((folderId: string) => {
    setSearchParams(folderId ? { folder: folderId } : {}, { replace: false })
  }, [setSearchParams])
  const { folders } = useArtifactFolders()
  const filtersActive = !!(filter || tagFilter || kindFilter || pinnedOnly)
  // If the URL points at a deleted/unknown folder, treat it as root rather
  // than showing a phantom empty view.
  const scopeFolderId = folders.some(f => f.id === currentFolderId) ? currentFolderId : ''

  // Tree expansion for the table view — client-local by design (§2.5):
  // collapsed by default, expanded ids persisted per browser.
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem('mc-artifact-folders-expanded') || '[]')
      return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : [])
    } catch { return new Set() }
  })
  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      safeSetItem('mc-artifact-folders-expanded', JSON.stringify([...next]))
      return next
    })
  }, [])

  const invalidateFolders = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['artifact-folders'] })
    qc.invalidateQueries({ queryKey: ['artifacts'] })
  }, [qc])
  const createFolderMut = useMutation({
    mutationFn: (body: { name: string; parent_id?: string }) => api.createArtifactFolder(body),
    onSuccess: invalidateFolders,
  })
  const updateFolderMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; parent_id?: string; order?: number; icon?: string; color?: string } }) =>
      api.updateArtifactFolder(id, body),
    onSettled: invalidateFolders,
  })
  const [deletingFolder, setDeletingFolder] = useState<ArtifactFolder | null>(null)
  const moveArtifact = useMoveArtifactToFolder()

  const [creatingFolder, setCreatingFolder] = useState(false)
  const [newFolderColor, setNewFolderColor] = useState('')
  const [renamingFolderId, setRenamingFolderId] = useState<string | null>(null)

  // The emoji icon is derived by a background LLM task server-side after
  // create/rename — refetch shortly after so it pops in without a reload.
  const scheduleIconRefetch = useCallback(() => {
    window.setTimeout(() => qc.invalidateQueries({ queryKey: ['artifact-folders'] }), 5000)
    window.setTimeout(() => qc.invalidateQueries({ queryKey: ['artifact-folders'] }), 15000)
  }, [qc])

  const handleNewFolder = useCallback(() => { setNewFolderColor(''); setCreatingFolder(true) }, [])
  const commitNewFolder = useCallback((name: string) => {
    setCreatingFolder(false)
    // In the gallery, create inside the folder being browsed; the tree view
    // creates at root (nest afterwards via drag or the folder menu).
    const parent = view === 'grid' && !filtersActive ? scopeFolderId : ''
    createFolderMut.mutate({
      name,
      ...(parent ? { parent_id: parent } : {}),
      ...(newFolderColor ? { color: newFolderColor } : {}),
    })
    scheduleIconRefetch()
  }, [createFolderMut, view, filtersActive, scopeFolderId, newFolderColor, scheduleIconRefetch])

  // ── Add Artifact — import a file from the user's machine ──────
  // The file's text is COPIED into artifact storage, so the artifact does not
  // stay bound to the file on disk (see lib/artifactImport.ts for why).
  const addFileInputRef = useRef<HTMLInputElement>(null)
  const [addError, setAddError] = useState<string | null>(null)
  const addArtifactMut = useMutation({
    mutationFn: async (vars: ImportPlan & { folder: string }) => {
      // Create unfiled, then file by id. `POST /api/artifacts` resolves its
      // `folder` field with mkdir -p semantics, so a folder id that went
      // stale between the pick and the save (folder deleted in another tab,
      // or a bookmarked ?folder=<id> URL) is not recognised as an id and gets
      // treated as a NAME — minting a junk folder called e.g. "a1b2c3d4e5f6".
      // The dedicated folder endpoint resolves ids only and errors on a stale
      // one, so the worst case is an artifact left at the library root.
      // This mirrors New Folder, which passes `parent_id` for the same reason.
      const art = (await api.createArtifact({
        name: vars.name,
        content: vars.content,
        kind: vars.kind,
      })) as Artifact
      // The store redacts credential-like text on every READ but stores the
      // POST body verbatim, so a file carrying real credential material would
      // read back as placeholders — and the next edit would save those
      // placeholders over the imported text. Refuse the import rather than
      // leave an artifact that silently corrupts itself (and rather than keep
      // the secret in the library at all).
      if (wasContentRedacted(vars.content, art.content)) {
        try {
          await api.deleteArtifact(art.slug)
        } catch {
          // Best effort: the refusal message is what matters, and the artifact
          // is reachable in the library if this cleanup did not land.
        }
        return { art, filed: false, redacted: true }
      }
      let filed = true
      if (vars.folder) {
        try {
          await api.setArtifactFolder(art.slug, vars.folder)
        } catch {
          // The artifact exists and is reachable — only its placement failed.
          // Surfaced below rather than failing the whole add.
          filed = false
        }
      }
      return { art, filed, redacted: false }
    },
    onSuccess: ({ art, filed, redacted }) => {
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      invalidateFolders()
      if (redacted) {
        setAddError(i18nT('pages.artifactsPage.add_artifact_error_redacted'))
        return
      }
      if (!filed) {
        // Stay put so the note is actually read; the artifact is at the root.
        setAddError(i18nT('pages.artifactsPage.add_artifact_error_unfiled'))
        return
      }
      // Open the new artifact: it confirms the file rendered, and it is the
      // only reliable feedback — a fresh artifact is unpinned, so it would be
      // invisible to a user whose library is filtered to Starred.
      navigate(`/artifacts/${art.slug}`)
    },
  })
  const handleAddArtifact = useCallback(() => {
    setAddError(null)
    addFileInputRef.current?.click()
  }, [])
  const handleAddArtifactFile = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    // Clear the input first so picking the SAME file again re-fires `change`
    // (the value is unchanged otherwise, and the event never fires).
    e.target.value = ''
    if (!file) return
    setAddError(null)
    const result = await planFileImport(file)
    if (!result.ok) {
      setAddError(importRejectionText(result.reason))
      return
    }
    // File it into the folder being browsed, matching New Folder's placement.
    const parent = view === 'grid' && !filtersActive ? scopeFolderId : ''
    addArtifactMut.mutate({ ...result.plan, folder: parent })
  }, [addArtifactMut, view, filtersActive, scopeFolderId])

  // ── New Artifact — start a blank document in the library ───────
  // Create-first, name-later: the artifact is created empty and the detail
  // page opens with its editor already focused, so the user starts typing
  // immediately instead of answering a name prompt before they know what
  // they are writing. The kind is left unspecified so the store defaults it
  // to markdown and marks it auto-assigned — the first save that looks like
  // JSON or SVG re-types it (see detect_editor_kind in artifacts.py).
  //
  // The cost of create-first is litter (abandoned empty documents); the
  // detail page pays for it by discarding an untouched blank on leave, which
  // is why `justCreatedBlank` is handed over in navigation state.
  const newArtifactMut = useMutation({
    mutationFn: async (vars: { folder: string }) => {
      const art = (await api.createArtifact({
        name: i18nT('pages.artifactsPage.untitled_artifact_name'),
        content: '',
      })) as Artifact
      let filed = true
      if (vars.folder) {
        try {
          // Same reasoning as the import path: file by id through the
          // dedicated endpoint, which errors on a stale folder id rather
          // than minting a junk folder from it.
          await api.setArtifactFolder(art.slug, vars.folder)
        } catch {
          filed = false
        }
      }
      return { art, filed }
    },
    onSuccess: ({ art, filed }) => {
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      invalidateFolders()
      if (!filed) setAddError(i18nT('pages.artifactsPage.add_artifact_error_unfiled'))
      // One-shot, module-scoped: a reload must NOT re-arm the detail page's
      // cleanup on a document the user has come back to.
      markJustCreatedBlank(art.slug, art.name)
      navigate(`/artifacts/${art.slug}`)
    },
  })
  const handleNewArtifact = useCallback(() => {
    setAddError(null)
    if (newArtifactMut.isPending) return
    const parent = view === 'grid' && !filtersActive ? scopeFolderId : ''
    newArtifactMut.mutate({ folder: parent })
  }, [newArtifactMut, view, filtersActive, scopeFolderId])

  const folderActions = useMemo<FolderActions>(() => ({
    onOpen: openFolder,
    onRename: (f) => setRenamingFolderId(f.id),
    onMove: (f, newParentId) => {
      if (isDescendantFolder(folders, f.id, newParentId)) return
      if ((f.parent_id || '') !== newParentId) updateFolderMut.mutate({ id: f.id, body: { parent_id: newParentId } })
    },
    onDelete: (f) => {
      // An empty folder (no artifacts, no subfolders anywhere in its subtree)
      // has nothing at stake — delete it immediately, no choice dialog.
      const stats = folderSubtreeStats(folders, f.id)
      if (stats.artifactCount === 0 && stats.subfolderCount === 0) {
        api.deleteArtifactFolder(f.id, false).finally(() => {
          if (scopeFolderId && isDescendantFolder(folders, f.id, scopeFolderId)) {
            openFolder(f.parent_id || '')
          }
          invalidateFolders()
        })
        return
      }
      setDeletingFolder(f)
    },
    onSetColor: (f, color) => {
      if ((f.color || '') !== color) updateFolderMut.mutate({ id: f.id, body: { color } })
    },
    renamingId: renamingFolderId,
    onRenameSubmit: (f, name) => {
      setRenamingFolderId(null)
      if (name && name !== f.name) {
        updateFolderMut.mutate({ id: f.id, body: { name } })
        scheduleIconRefetch()
      }
    },
    onRenameCancel: () => setRenamingFolderId(null),
  }), [openFolder, updateFolderMut, folders, renamingFolderId, scheduleIconRefetch, scopeFolderId, invalidateFolders])

  const confirmDeleteFolder = useCallback(async (deleteContents: boolean) => {
    if (!deletingFolder) return
    try {
      await api.deleteArtifactFolder(deletingFolder.id, deleteContents)
    } finally {
      setDeletingFolder(null)
      // If we were inside the deleted subtree, pop back to its parent.
      if (scopeFolderId && isDescendantFolder(folders, deletingFolder.id, scopeFolderId)) {
        openFolder(deletingFolder.parent_id || '')
      }
      invalidateFolders()
    }
  }, [deletingFolder, folders, scopeFolderId, openFolder, invalidateFolders])

  // ── Library drag-and-drop ─────────────────────────────────────────────────
  // One DndContext covers both views. Artifact → folder-drop moves it; folder
  // → folder-drop nests it into the target, cycle-guarded. (Folders sort
  // alphabetically, so there is no manual sibling reorder.)
  //
  // Split mouse/touch sensors so a finger can both SCROLL and drag (mirrors the
  // Apps nav rail in App.tsx):
  //  - MouseSensor: 6px distance, so a plain click still opens the card and only
  //    a deliberate mouse drag starts a move.
  //  - TouchSensor: 250ms press-and-hold (5px tolerance), so a finger swipe that
  //    travels past the tolerance CANCELS the sensor and the gallery pans
  //    natively; only a deliberate hold picks the card up.
  //
  // A single PointerSensor cannot do this. Past its activation distance
  // `AbstractPointerSensor.handleMove` calls `preventDefault()` on every
  // subsequent move event — and dnd-kit installs a non-passive window
  // `touchmove` listener precisely so those calls take effect ("This is
  // required for iOS Safari", TouchSensor.setup). Chromium ignores
  // preventDefault on `pointermove` for panning, so the swallowed swipe only
  // shows up on WebKit: a gesture starting on a CARD dies while the same
  // gesture starting in the GAP between cards (no listener, no sensor) scrolls.
  const dndSensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 5 } }),
  )
  const [activeDrag, setActiveDrag] = useState<LibraryDrag | null>(null)
  // The folder the drag is currently over (''=unfile target, null=none) —
  // drives group highlighting: hovering anywhere over an expanded folder's
  // region (its rows included) lights the whole folder up as the drop target.
  const [overFolderId, setOverFolderId] = useState<string | null>(null)
  const handleDragOver = useCallback((e: { over: DragEndEvent['over'] }) => {
    const o = e.over?.data.current as { type?: string; folderId?: string } | undefined
    setOverFolderId(o?.type === 'folder-drop' ? (o.folderId ?? '') : null)
  }, [])
  const handleDragStart = useCallback((e: DragStartEvent) => {
    const d = e.active.data.current as LibraryDrag | undefined
    if (d?.type === 'artifact' || d?.type === 'folder') setActiveDrag(d)
  }, [])
  const handleDragEnd = useCallback((e: DragEndEvent) => {
    setActiveDrag(null)
    setOverFolderId(null)
    const a = e.active.data.current as LibraryDrag | undefined
    const o = e.over?.data.current as { type?: string; folderId?: string } | undefined
    if (!a || o?.type !== 'folder-drop') return
    const target = o.folderId ?? ''
    if (a.type === 'artifact') {
      if ((a.folderId || '') !== target) moveArtifact(a.slug, target)
      return
    }
    // Folder drop = nest into the target (cycle-guarded — a folder can never
    // be dropped into itself or its own subtree). Siblings sort
    // alphabetically, so there is no manual reorder: a same-parent drop is a
    // no-op.
    if (a.id === target) return
    if (isDescendantFolder(folders, a.id, target)) return
    const dragged = folders.find(f => f.id === a.id)
    if (!dragged) return
    if ((dragged.parent_id || '') !== target) {
      updateFolderMut.mutate({ id: a.id, body: { parent_id: target } })
    }
  }, [folders, moveArtifact, updateFolderMut])
  const handleDragCancel = useCallback(() => { setActiveDrag(null); setOverFolderId(null) }, [])

  const { data, isLoading, error } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', { tag: tagFilter, kind: kindFilter }],
    queryFn: () =>
      api.artifacts({
        tag: tagFilter || undefined,
        kind: kindFilter || undefined,
      }),
  })

  // Separate unfiltered query that drives the tag dropdown options so users
  // can switch between tags without first resetting to "all tags". Without
  // this, allTags would be derived only from currently-filtered results and
  // co-occurring tags would disappear when one is selected.
  const { data: allTagsData } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', 'all-tags'],
    queryFn: () => api.artifacts({}),
  })

  const artifacts = data?.artifacts || []
  const allTags = useMemo(() => {
    const s = new Set<string>()
    for (const a of allTagsData?.artifacts || []) for (const t of a.tags || []) s.add(t)
    return Array.from(s).sort()
  }, [allTagsData])

  // Registered publish providers gate the ENTIRE remote-browse surface: the
  // public edition ships an empty registry, so this resolves to [] and no
  // remote section renders (zero extra requests beyond this one probe).
  const { data: providersData } = useQuery<{ providers: PublishProviderDescriptor[] }>({
    queryKey: ['publish-providers', 'widget'],
    queryFn: () => api.getArtifactPublishProviders('widget'),
    staleTime: 300_000,
  })
  const discoveryProviders = useMemo(
    () =>
      (providersData?.providers || []).filter(
        (p) =>
          p.discovery_model.list_mine ||
          p.discovery_model.list_shared_with_me ||
          p.discovery_model.list_public,
      ),
    [providersData],
  )

  const visible = useMemo(() => {
    let list = artifacts
    if (pinnedOnly) list = list.filter((a) => a.pinned)
    if (!filter) return list
    const q = filter.toLowerCase()
    return list.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        a.slug.toLowerCase().includes(q) ||
        (a.description || '').toLowerCase().includes(q) ||
        (a.session_title || '').toLowerCase().includes(q),
    )
  }, [artifacts, filter, pinnedOnly])

  // Column-sorted rows for the table views. The tree view buckets by folder
  // after sorting, so rows sort within each folder. The gallery has no
  // columns, so it keeps the server's order.
  const sortedVisible = useMemo(() => sortArtifacts(visible, sort), [visible, sort])

  // Browse-mode gallery scoping: no filters → only artifacts filed in the open
  // folder (a dangling folder_id degrades to unfiled). Any filter active →
  // flat matches across all folders (§2.6). The tree table buckets for itself.
  const scopedVisible = useMemo(() => {
    if (filtersActive) return visible
    const ids = new Set(folders.map(f => f.id))
    return visible.filter(a => {
      const fid = a.folder_id && ids.has(a.folder_id) ? a.folder_id : ''
      return fid === scopeFolderId
    })
  }, [visible, filtersActive, folders, scopeFolderId])

  // Subfolder cards shown above the gallery masonry (browse mode only).
  const subfolders = useMemo(
    () => (filtersActive ? [] : childFolders(folders, scopeFolderId)),
    [filtersActive, folders, scopeFolderId],
  )

  // Up to three preview artifacts per subfolder card — direct children first,
  // then deeper descendants, so every folder card gives a visual glimpse of
  // what's filed inside it.
  const folderPreviews = useMemo(() => {
    const map = new Map<string, Artifact[]>()
    for (const f of subfolders) {
      const direct = artifacts.filter((a) => (a.folder_id || '') === f.id)
      let pool = direct
      if (direct.length < 3) {
        const deeper = artifacts.filter(
          (a) => a.folder_id && a.folder_id !== f.id && isDescendantFolder(folders, f.id, a.folder_id),
        )
        pool = [...direct, ...deeper]
      }
      map.set(f.id, pool.slice(0, 3))
    }
    return map
  }, [subfolders, artifacts, folders])

  const deleteMut = useMutation({
    mutationFn: (slug: string) => api.deleteArtifact(slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['artifacts'] }),
  })

  const pinMut = useMutation({
    mutationFn: ({ slug, pinned }: { slug: string; pinned: boolean }) => api.setArtifactPinned(slug, pinned),
    onSuccess: (_data, { slug, pinned }) => {
      // Patch the detail entry too, so opening the artifact right after starring
      // shows the new state instead of a stale chip for up to staleTime. Same
      // shape-preserving spread useArtifactFolders uses — never invalidate this
      // key for a pin: a content refetch can move an open editor's baseline.
      qc.setQueryData(
        ['artifact', slug],
        (old: Artifact | undefined) => (old ? { ...old, pinned } : old),
      )
      qc.invalidateQueries({ queryKey: ['artifacts'] })
    },
  })
  const handleTogglePin = useCallback((a: Artifact) => {
    pinMut.mutate({ slug: a.slug, pinned: !a.pinned })
  }, [pinMut])
  const pinningSlug = pinMut.isPending ? (pinMut.variables as { slug: string }).slug : null

  // "All" view firehose: non-code docs produced across all sessions. Only
  // fetched when All is active (Starred is the default, so this stays idle then).
  const sessionDocsQ = useQuery<{ docs: SessionDoc[] }>({
    queryKey: ['artifact-session-docs'],
    queryFn: () => api.artifactSessionDocs(),
    enabled: !pinnedOnly,
    staleTime: 30_000,
  })
  const materializeMut = useMutation({
    mutationFn: ({ path, sessionKey }: { path: string; sessionKey?: string }) => api.materializeArtifact(path, sessionKey),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['artifacts'] })
      qc.invalidateQueries({ queryKey: ['artifact-session-docs'] })
    },
  })
  const handleMaterialize = useCallback(
    (path: string, sessionKey?: string) => materializeMut.mutate({ path, sessionKey }),
    [materializeMut],
  )
  const materializingPath = materializeMut.isPending
    ? ((materializeMut.variables as { path: string } | undefined)?.path ?? null)
    : null
  const sessionDocs = useMemo(() => {
    let docs = (sessionDocsQ.data?.docs || []).filter((d) => !d.saved)
    if (kindFilter) docs = docs.filter((d) => docFileType(d.path) === kindFilter)
    if (filter) {
      const q = filter.toLowerCase()
      docs = docs.filter((d) =>
        d.name.toLowerCase().includes(q) ||
        d.path.toLowerCase().includes(q) ||
        (d.session_title || '').toLowerCase().includes(q))
    }
    return docs
  }, [sessionDocsQ.data, filter, kindFilter])

  const handleOpen = useCallback((slug: string) => navigate(`/artifacts/${slug}`), [navigate])

  const gridEntries = useMemo<GridEntry[]>(
    () => scopedVisible.map((a) => ({ kind: 'local' as const, key: a.slug, art: a })),
    [scopedVisible],
  )

  const handleDelete = useCallback((a: Artifact) => {
    if (window.confirm(i18nT('pages.artifactsPage.remove_artifact_confirm', { slug: a.slug }))) {
      deleteMut.mutate(a.slug)
    }
  }, [deleteMut])

  const errMessage = error ? (error instanceof Error ? error.message : String(error)) : null
  const asMessage = (e: unknown) => (e instanceof Error ? e.message : String(e))
  const mutErr = deleteMut.error
    ? asMessage(deleteMut.error)
    : addArtifactMut.error
      ? asMessage(addArtifactMut.error)
      : newArtifactMut.error
        ? asMessage(newArtifactMut.error)
        : materializeMut.error
          ? asMessage(materializeMut.error)
          : null

  // Hooks must run before the `isLoading` early return below, so the scroll
  // wiring lives here rather than beside the JSX it feeds.
  //
  // The virtualized gallery brings its OWN vertical scroller. Two same-axis
  // scrollers on one page is a defect: whichever one the finger lands in decides
  // whether anything moves, and the page-level one has only ~113px of travel
  // once the gallery is on screen, so a swipe that lands there stops dead after
  // a few pixels and reads as "this card does not scroll". Measured at 390px with
  // 42 artifacts: page column 706px tall over 819px of content, gallery scroller
  // 608px tall over 12485px. So exactly one element owns the axis; below the
  // threshold the gallery is content-sized and the page column scrolls, as before.
  // Measured here, not inside the gallery, because two independent measurements
  // of the same width could disagree at a boundary and leave the page holding an
  // axis the gallery also thinks it owns. `galleryWidthRef` is attached to the
  // gallery's own column-defining wrapper so the number still describes the
  // element that lays the columns out.
  const [galleryWidthRef, cols] = useColumnCount(300)
  // Scroll ownership. A virtualized MASONRY can only own a scroller of its own,
  // so the page column has to stop scrolling and hand the axis over — otherwise
  // both scroll on the same axis and the page column has only ~113px of travel
  // once the gallery is on screen, so a swipe that lands there stops dead after
  // a few pixels and reads as "this card does not scroll". Measured at 390px with
  // 42 artifacts: page column 706px tall over 819px of content, gallery scroller
  // 608px tall over 12485px.
  //
  // At ONE column there is no masonry to preserve, so the gallery renders as a
  // list windowed against this column (`LibraryList`) and the page column KEEPS
  // the axis. That is the narrow case, and it is the one where handing the axis over
  // hurt: it is what forced the pre-gallery region to be capped into a scroller
  // of its own and the chrome to hide on scroll, and it is what left sections
  // rendered after the gallery unreachable.
  const galleryOwnsScroll = view === 'grid' && gridEntries.length >= VIRTUALIZE_AT && cols > 1
  // Hide-on-scroll for the page's own chrome. At 390x844 the title, subtitle,
  // heading row and filter rows pin 317px — 38% of the viewport — above a 527px
  // gallery. This is only reachable when the masonry owns the axis (so, several
  // columns on a short viewport); at one column the chrome scrolls away with the
  // page instead, which tracks the finger 1:1 and needs no animation, no
  // threshold and no settle window.
  //
  // Narrow only: at desktop heights the chrome is a small fraction of the column
  // and moving it on scroll would be motion nobody asked for.
  const chromeHostRef = useRef<HTMLDivElement | null>(null)
  const chromeCollapsed = useCollapseOnScroll(chromeHostRef, isMobile && galleryOwnsScroll)

  if (isLoading) return <div className="p-6 text-muted">{i18nT('pages.artifactsPage.loading')}</div>

  // Both controls below are rendered ONCE and placed differently per width, not
  // duplicated per branch: the view switcher owns a framer `layoutId`, and two
  // live elements sharing one id fight over the same animated indicator.
  const viewSwitcher = (
    <SegmentedControl
      segments={[
        { key: 'grid', label: i18nT('pages.artifactsPage.gallery'), icon: <LayoutDashboard size={13} />, tooltip: i18nT('pages.artifactsPage.masonry_preview_gallery') },
        { key: 'table', label: i18nT('pages.artifactsPage.table'), icon: <TableIcon size={13} />, tooltip: i18nT('pages.artifactsPage.compact_table') },
      ]}
      value={view}
      onChange={(v) => { setView(v); safeSetItem('mc-artifacts-view', v) }}
      layoutId="artifact-view"
      // This control sits in a content-hugging group, so its own
      // measurement always reads "plenty of room". Even on its own line
      // the two labelled segments do not fit beside the create actions
      // at 320px, and they are the widest thing in the row.
      compact={isMobile}
    />
  )
  const starredToggle = (
    <div className="inline-flex items-center rounded-lg border border-border bg-bg-elevated p-0.5" role="group" aria-label={i18nT('pages.artifactsPage.filter_starred')}>
      <button
        type="button"
        onClick={() => { setPinnedOnly(true); safeSetItem('mc-artifacts-pinned-only', '1') }}
        aria-pressed={pinnedOnly}
        className={`px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors cursor-pointer border-none inline-flex items-center gap-1 ${pinnedOnly ? 'bg-accent text-accent-fg' : 'bg-transparent text-muted hover:text-text'}`}
      >
        <Star size={12} className={pinnedOnly ? 'fill-current' : ''} /> {i18nT('pages.artifactsPage.starred')}
      </button>
      <button
        type="button"
        onClick={() => { setPinnedOnly(false); safeSetItem('mc-artifacts-pinned-only', '0') }}
        aria-pressed={!pinnedOnly}
        className={`px-2.5 py-1 rounded-md text-[12px] font-medium transition-colors cursor-pointer border-none ${!pinnedOnly ? 'bg-accent text-accent-fg' : 'bg-transparent text-muted hover:text-text'}`}
      >
        {i18nT('pages.artifactsPage.all')}
      </button>
    </div>
  )

  // Scroll ownership and the hide-on-scroll wiring are decided above, before the
  // `isLoading` early return, because hooks cannot run after it.
  return (
    <>
      {/* The scroll host carries NO padding of its own: `PageHeader` brings the
        * page gutter with it, and doubling that would put the title 32px in
        * while the cards it labels stay at 16px. The gutter lives on the inner
        * content wrapper instead.
        *
        * The header sits INSIDE this element so that, when the page owns the
        * axis, it scrolls away with the content by physics — 1:1 with the
        * finger, no animation and no threshold. When the masonry owns the axis
        * this element does not scroll, and the header collapses instead. */}
      <div
        ref={chromeHostRef}
        data-testid="artifacts-scroll-host"
        className={`flex-1 min-h-0 flex flex-col ${
          galleryOwnsScroll ? 'overflow-hidden' : 'overflow-y-auto'
        }`}
      >
        {/* shrink-0 so flex cannot absorb the header the way it absorbed the
          * folder region: inside an `overflow-hidden` column a squeezed child
          * has nothing able to scroll it back into view. */}
        <div className="shrink-0">
          <CollapsibleChrome collapsed={chromeCollapsed}>
            <PageHeader title={i18nT('pages.artifactsPage.artifacts')} subtitle={i18nT('pages.artifactsPage.widgets_files_and_snippets_live_tracked_with_ver')} />
          </CollapsibleChrome>
        </div>
      <div
        className={`px-4 md:px-6 ${
          galleryOwnsScroll ? 'flex flex-col flex-1 min-h-0' : 'pb-8'
        }`}
      >
        {(errMessage || mutErr || addError) && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <span className="text-danger text-lg shrink-0"><AlertTriangle className="lucide-inline" /></span>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-danger font-medium">{i18nT('pages.artifactsPage.error')}</div>
              <div className="text-[13px] text-danger/90 mt-0.5">{errMessage || mutErr || addError}</div>
            </div>
            <Btn aria-label={i18nT('app.dismiss')} onClick={() => { deleteMut.reset(); addArtifactMut.reset(); newArtifactMut.reset(); materializeMut.reset(); setAddError(null) }} className="text-danger/60 hover:text-danger shrink-0"><X className="lucide-inline" /></Btn>
          </div>
        )}

        {/* Collapses with the page title on the way down. The heading row and
          * the filters are the other 180px of the 317px of pinned chrome; the
          * breadcrumb and folder cards below stay put because they are content,
          * not chrome. */}
        <CollapsibleChrome collapsed={chromeCollapsed}>
        {/* `flex-wrap` moves the ACTION GROUP to its own line when the title
          * cannot share one with it — it does not let the button row itself
          * wrap, which is what would cost the row its ranking. Without it the
          * title is the flex item that gives, and at 320px it is squeezed to a
          * few pixels while the view switcher still hangs off the right edge. */}
        <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
          <h3 className="text-sm font-semibold text-text-strong">{i18nT('pages.artifactsPage.your_artifacts')}</h3>
          <div className="flex items-center gap-2">
            {/* Split button: creating a blank document is the common verb and
              * gets the zero-click path; importing a file keeps its muscle
              * memory one click away under the caret. */}
            <div className="flex items-center">
              <Btn
                onClick={handleNewArtifact}
                disabled={newArtifactMut.isPending}
                className="flex items-center gap-1.5 rounded-r-none"
                title={i18nT('pages.artifactsPage.start_a_new_blank_document_in_the_library')}
              >
                {newArtifactMut.isPending ? <Loader2 size={13} className="animate-spin" /> : <FilePlus size={13} />} {i18nT('pages.artifactsPage.new_artifact')}
              </Btn>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Btn
                    // On a phone this menu also holds the folder action, so an
                    // add-only name would under-promise its contents — and the
                    // name is all a screen reader gets from a chevron.
                    aria-label={isMobile
                      ? i18nT('pages.artifactsPage.more_actions')
                      : i18nT('pages.artifactsPage.more_ways_to_add_an_artifact')}
                    disabled={addArtifactMut.isPending}
                    // The caret holds only a 13px icon, so its content box is
                    // ~7px shorter than the labelled half next to it (whose
                    // text line-height sets the split button's height). The
                    // parent centres it, which shows as a gap above AND below
                    // the caret — `self-stretch` makes it take the row's height
                    // instead, keeping the seam a single continuous edge without
                    // pinning a literal height that the label's font would
                    // outgrow.
                    className="rounded-l-none border-l-0 px-1 self-stretch"
                  >
                    {addArtifactMut.isPending ? <Loader2 size={13} className="animate-spin" /> : <ChevronDown size={13} />}
                  </Btn>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={handleAddArtifact}>
                    <FileText size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.import_from_a_file')}
                  </DropdownMenuItem>
                  {/* On a phone this menu is also where the folder action lives:
                    * three peer controls do not fit a 320px line in the wide
                    * locales (fr/it/bn run ~40% longer than en), and clipping
                    * one off the edge is the only worse outcome than moving it
                    * one tap away. Creating is the row's verb, so creating is
                    * what keeps the visible slot. */}
                  {isMobile && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem onSelect={handleNewFolder}>
                        <FolderPlus size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.new_folder')}
                      </DropdownMenuItem>
                    </>
                  )}
                  {/* Deploy LEAVES the page rather than filtering it, so on a
                    * phone it is the one control in the toolbar that can move
                    * behind a tap without costing anything: keeping it visible
                    * is what forced the filter row to wrap and left a lone
                    * right-floated button on a line of its own. */}
                  {isMobile && cloudDeployEnabled && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem onSelect={() => navigate('/deploy')}>
                        <Globe size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.artifact_deploy')}
                      </DropdownMenuItem>
                    </>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            <Input
              ref={addFileInputRef}
              type="file"
              accept={IMPORT_ACCEPT}
              aria-label={i18nT('pages.artifactsPage.add_a_file_from_your_computer_to_the_library')}
              className="hidden"
              onChange={handleAddArtifactFile}
            />
            {!isMobile && (
              <Btn onClick={handleNewFolder} className="flex items-center gap-1.5" title={i18nT('pages.artifactsPage.create_a_folder_to_organize_your_artifacts')}>
                <FolderPlus size={13} /> {i18nT('pages.artifactsPage.new_folder')}
              </Btn>
            )}
            {/* On a phone the view switcher moves down to pair with the Starred
              * toggle: both answer "what am I looking at", and putting them on
              * one justified row gives the toolbar's last line a left AND a
              * right edge instead of a single stranded control. */}
            {!isMobile && viewSwitcher}
          </div>
        </div>
        {/* Narrow-first toolbar: one control per row, every row spanning the
          * full content width, so the four rows share one left edge and one
          * right edge. A single `flex-wrap` row is what produced the scatter —
          * it broke wherever the widths happened to land (search + kind on one
          * line, a right-floated Deploy alone on the next, a left-aligned
          * toggle on a third), giving every line a different x. From `md` up
          * `md:contents` dissolves the mobile grouping wrappers so the same
          * children are direct flex items again and the desktop row is
          * byte-for-byte the layout it was. */}
        <div className="flex flex-col gap-2 mb-3 md:flex-row md:flex-wrap md:items-center">
            <SearchInput
              placeholder={i18nT('pages.artifactsPage.filter_by_name_slug_description')}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            {/* The "all" row of each filter is the empty string, the value both
                filters initialise to. SimpleSelect routes '' through an internal
                sentinel, so it stays a selectable option as long as '' is present
                in `options` — which is why it leads each array and takes its
                visible label from the matching `optionLabels` slot. */}
            <div className="flex gap-2 md:contents">
              <div className="flex-1 min-w-0 md:flex-initial">
                <SimpleSelect
                  options={[...KIND_OPTIONS]}
                  optionLabels={KIND_OPTIONS.map((k) => (k ? `kind: ${k}` : i18nT('pages.artifactsPage.all_kinds')))}
                  value={kindFilter}
                  aria-label={i18nT('pages.artifactsPage.filter_by_kind')}
                  onChange={setKindFilter}
                />
              </div>
              {/* The popup is exactly this trigger's width, so a trigger sized to
                  its own placeholder would clip the user-defined tag names it
                  lists. Floor the TRIGGER, not the panel — that keeps the two in
                  lockstep while leaving the rows readable. The floor is desktop
                  only: sharing the row half-and-half already gives the trigger
                  ~179px at 390px, and a hard 180px floor on a phone would push
                  the pair past the viewport instead. */}
              <div className="flex-1 min-w-0 md:flex-initial md:min-w-[180px]">
                <SimpleSelect
                  options={['', ...allTags]}
                  optionLabels={[i18nT('pages.artifactsPage.all_tags'), ...allTags.map((t) => `${i18nT('pages.artifactsPage.tag')} ${t}`)]}
                  value={tagFilter}
                  aria-label={i18nT('pages.artifactsPage.filter_by_tag')}
                  onChange={setTagFilter}
                />
              </div>
            </div>
            {cloudDeployEnabled && !isMobile && (
              <Btn onClick={() => navigate('/deploy')} className="flex items-center gap-1.5 ml-auto" title={i18nT('pages.artifactsPage.artifact_deploy_aws_profiles_and_published_sites')}>
                <Globe size={13} /> {i18nT('pages.artifactsPage.artifact_deploy')}
              </Btn>
            )}
            {isMobile
              ? (
                <div className="flex items-center justify-between gap-2">
                  {starredToggle}
                  {viewSwitcher}
                </div>
              )
              : starredToggle}
          </div>
        </CollapsibleChrome>

          {/* One DndContext spans breadcrumb + folder cards + gallery/table so
              artifacts and folders can be dragged between all of them. */}
          <DndContext
            sensors={dndSensors}
            collisionDetection={artifactLibraryCollision}
            measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
            onDragStart={handleDragStart}
            onDragOver={handleDragOver}
            onDragEnd={handleDragEnd}
            onDragCancel={handleDragCancel}
          >
            {/* Everything between the chrome and the gallery is capped and
              * scrolls itself once the gallery owns the page's scroll axis.
              *
              * Without the cap this content is a plain flex sibling of a
              * `flex-1 min-h-0` gallery inside an `overflow-hidden` column, so a
              * tall stack of folder cards is absorbed by flex-shrink and there is
              * NOTHING that can scroll it into view. Measured at 390x844 with a
              * 700px stand-in above the gallery: it rendered at 562px (squeezed
              * 138px), the gallery collapsed to 0px — the artifact list vanishes
              * outright — and no ancestor scroller could reach either
              * (`docScrollable: 0`).
              *
              * `shrink-0` is what stops the squeeze; `max-h-[45%]` is what leaves
              * the gallery a floor to live in. Below the virtualization threshold
              * the page column still scrolls, so no cap is wanted there. Marked
              * as chrome so scrolling this region does not also hide the toolbar
              * above it. */}
            <div
              {...(galleryOwnsScroll ? { [CHROME_ATTR]: '' } : {})}
              className={galleryOwnsScroll ? 'shrink-0 max-h-[45%] overflow-y-auto' : ''}
            >
            {view === 'grid' && !filtersActive && scopeFolderId && (
              <FolderBreadcrumbBar folders={folders} currentFolderId={scopeFolderId} onNavigate={openFolder} />
            )}
            {view === 'grid' && (subfolders.length > 0 || (creatingFolder && !filtersActive)) && (
              <FolderCardGrid>
                {creatingFolder && !filtersActive && (
                  <div className="mr-3 mb-3 rounded-lg border border-accent bg-card p-3" style={newFolderColor ? { borderLeft: `3px solid ${newFolderColor}` } : undefined}>
                    <div className="h-[84px] rounded-md border border-dashed border-border flex items-center justify-center text-muted mb-2.5">
                      <FolderPlus size={22} className="opacity-50" />
                    </div>
                    <div className="flex items-center gap-2">
                      <FolderIcon size={17} className="shrink-0" style={{ color: newFolderColor || 'var(--accent)' }} />
                      <div className="min-w-0 flex-1">
                        <FolderNameInput
                          placeholder={i18nT('pages.artifactsPage.new_folder_name')}
                          onCommit={commitNewFolder}
                          onCancel={() => setCreatingFolder(false)}
                        />
                      </div>
                    </div>
                    <div className="mt-2">
                      <FolderColorSwatches size={14} value={newFolderColor} onPick={setNewFolderColor} />
                    </div>
                  </div>
                )}
                {subfolders.map((f) => (
                  <FolderCard
                    key={f.id}
                    folder={f}
                    folders={folders}
                    previewArtifacts={folderPreviews.get(f.id) ?? []}
                    actions={folderActions}
                  />
                ))}
              </FolderCardGrid>
            )}
            {(view !== 'grid' || filtersActive) && creatingFolder && (
              <div className="mb-2 max-w-[360px]">
                <div className="flex items-center gap-2">
                  <FolderPlus size={15} className="shrink-0" style={{ color: newFolderColor || 'var(--accent)' }} />
                  <div className="min-w-0 flex-1">
                    <FolderNameInput
                      placeholder={i18nT('pages.artifactsPage.new_folder_name')}
                      onCommit={commitNewFolder}
                      onCancel={() => setCreatingFolder(false)}
                    />
                  </div>
                </div>
                <div className="mt-1.5 ml-6">
                  <FolderColorSwatches size={13} value={newFolderColor} onPick={setNewFolderColor} />
                </div>
              </div>
            )}

            {/* Session docs render ABOVE the masonry: at ≥30 artifacts the
              * virtualized gallery becomes a viewport-height scroller, and a
              * section after it would hide below the fold — the exact
              * discoverability gap this feature exists to close. Table/tree
              * views fold the docs into their own rows instead. Skipped while
              * folder-scoped (docs are unfiled) and in the Starred view. */}
            {view === 'grid' && !pinnedOnly && !tagFilter && (filtersActive || !scopeFolderId) && (
              <CollapsibleChrome collapsed={chromeCollapsed}>
                <SessionDocsGallery
                  docs={sessionDocs}
                  pending={sessionDocsQ.isPending}
                  onMaterialize={handleMaterialize}
                  materializingPath={materializingPath}
                />
              </CollapsibleChrome>
            )}
            </div>

            {gridEntries.length === 0 && (view === 'grid' || filtersActive) ? (
              (artifacts.length === 0 && folders.length === 0) ? (
                <EmptyState
                  icon={<Bookmark className="lucide-inline" />}
                  title={i18nT('pages.artifactsPage.no_artifacts_yet')}
                  subtitle={sessionDocs.length > 0 && !pinnedOnly
                    ? i18nT('pages.artifactsPage.star_a_document_in_from_your_chats_to_save_it_he')
                    : i18nT('pages.artifactsPage.click_the_bookmark_icon_on_any_rendered_widget_i')}
                />
              ) : (
                <div className="text-muted italic px-2.5 py-3.5 text-sm">
                  {filtersActive
                    ? i18nT('pages.artifactsPage.no_artifacts_match_your_filters')
                    : scopeFolderId
                      ? (subfolders.length ? i18nT('pages.artifactsPage.no_artifacts_directly_in_this_folder') : i18nT('pages.artifactsPage.this_folder_is_empty_drag_artifacts_onto_it_to_f'))
                      : i18nT('pages.artifactsPage.no_unfiled_artifacts_everything_is_filed_in_fold')}
                </div>
              )
            ) : view === 'grid' ? (
              <LibraryMasonry
                entries={gridEntries}
                cols={cols}
                widthRef={galleryWidthRef}
                scrollerRef={chromeHostRef}
                onOpen={handleOpen}
                onDelete={handleDelete}
                deletingSlug={deleteMut.isPending ? (deleteMut.variables as string) : null}
                onTogglePin={handleTogglePin}
                pinningSlug={pinningSlug}
              />
            ) : filtersActive ? (
              <LibraryTable
                items={sortedVisible}
                sort={sort}
                onSort={handleSort}
                onOpen={handleOpen}
                onDelete={handleDelete}
                deletingSlug={deleteMut.isPending ? (deleteMut.variables as string) : null}
                onTogglePin={handleTogglePin}
                pinningSlug={pinningSlug}
                sessionDocs={pinnedOnly || tagFilter ? [] : sessionDocs}
                onMaterialize={pinnedOnly ? undefined : handleMaterialize}
                materializingPath={materializingPath}
              />
            ) : (
              <LibraryTree
                items={sortedVisible}
                sort={sort}
                onSort={handleSort}
                folders={folders}
                expandedIds={expandedIds}
                onToggleExpand={toggleExpanded}
                folderActions={folderActions}
                onOpen={handleOpen}
                onDelete={handleDelete}
                deletingSlug={deleteMut.isPending ? (deleteMut.variables as string) : null}
                onTogglePin={handleTogglePin}
                pinningSlug={pinningSlug}
                overFolderId={overFolderId}
                dragActive={!!activeDrag}
                sessionDocs={pinnedOnly || tagFilter ? [] : sessionDocs}
                onMaterialize={pinnedOnly ? undefined : handleMaterialize}
                materializingPath={materializingPath}
              />
            )}

            <DragOverlay dropAnimation={null} modifiers={[snapOverlayToCursor]}>
              {activeDrag && (
                <div className="flex items-center gap-2 rounded-lg border border-accent bg-card px-3 py-2 shadow-lg text-sm text-text-strong max-w-[260px]">
                  {activeDrag.type === 'folder' ? (
                    (() => {
                      const gf = folders.find((f) => f.id === activeDrag.id)
                      return gf
                        ? <FolderGlyph folder={gf} size={14} />
                        : <FolderIcon size={14} className="text-accent shrink-0" />
                    })()
                  ) : (
                    <Bookmark size={14} className="text-accent shrink-0" />
                  )}
                  <span className="truncate">{activeDrag.name}</span>
                </div>
              )}
            </DragOverlay>
          </DndContext>

          <ArtifactFolderDeleteDialog
            folder={deletingFolder}
            folders={folders}
            onConfirm={confirmDeleteFolder}
            onClose={() => setDeletingFolder(null)}
          />

        {/* Remote browse — one section per discovery-capable registered
            publish provider. The public edition registers no provider, so
            discoveryProviders is [] and NOTHING renders (inert surface). */}
        {discoveryProviders.map((p) => (
          <RemoteBrowseSection
            key={p.name}
            provider={p}
            onForked={(slug) => { qc.invalidateQueries({ queryKey: ['artifacts'] }); qc.invalidateQueries({ queryKey: ['remote-artifacts', p.name] }); navigate(`/artifacts/${slug}`) }}
            onCloned={(slug) => { qc.invalidateQueries({ queryKey: ['artifacts'] }); qc.invalidateQueries({ queryKey: ['remote-artifacts', p.name] }); navigate(`/artifacts/${slug}`) }}
          />
        ))}
        </div>
      </div>
    </>
  )
}


/** Browse one publish provider's remote artifacts (provider-routed; vendor
 * copy comes from the provider's own display_name). Renders nothing while
 * loading/failed so the library page never blocks on a remote. */
function RemoteBrowseSection({ provider, onForked, onCloned }: {
  provider: PublishProviderDescriptor
  onForked: (slug: string) => void
  onCloned: (slug: string) => void
}) {
  const [search, setSearch] = useState('')
  const scope = provider.discovery_model.list_mine ? 'mine'
    : provider.discovery_model.list_shared_with_me ? 'shared' : 'public'
  const useSearch = !!search && provider.discovery_model.full_text_search
  const {
    data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage, isPlaceholderData,
  } = useInfiniteQuery<
    { artifacts: RemoteArtifact[]; next_page_token?: string | null }
  >({
    // Tag the key with the mode ('q' vs 'scope') so a full-text query that
    // happens to equal the scope word (e.g. typing "mine") can't collide with
    // the scope listing's cache entry.
    queryKey: ['remote-artifacts', provider.name, useSearch ? ['q', search] : ['scope', scope]],
    queryFn: ({ pageParam }) =>
      api.browseRemoteArtifacts(provider.name, {
        ...(useSearch ? { q: search } : { scope }),
        ...(pageParam ? { pageToken: pageParam as string } : {}),
      }),
    initialPageParam: '',
    // The provider paginates via next_page_token; stop when it stops handing
    // one out (null/empty ⇒ last page). Without this, remote artifacts beyond
    // the provider's first page would be unreachable.
    getNextPageParam: (last) => last.next_page_token || undefined,
    staleTime: 60_000,
    // Keep the prior page's rows while a new full-text query fetches. Without
    // this, every keystroke changes the key → data=undefined → isLoading=true →
    // the section (and the focused SearchInput inside it) unmounts, dropping
    // keyboard focus mid-word.
    placeholderData: keepPreviousData,
  })
  const items: RemoteArtifact[] = (data?.pages || []).flatMap((p) => p.artifacts || [])
  // Drop artifacts already on this device (cloned or forked) — they live in
  // Your Artifacts above, so listing them here too would be a duplicate.
  const notLocal = items.filter(a => !a.local_slug)
  if (isLoading && !notLocal.length) return null
  if (error) return null
  if (!notLocal.length && !search) return null
  const filtered = search && !useSearch
    ? notLocal.filter(a => a.title.toLowerCase().includes(search.toLowerCase()) || a.tags?.some(t => t.toLowerCase().includes(search.toLowerCase())))
    : notLocal
  // In full-text mode the shown rows can be the PREVIOUS query's results
  // (keepPreviousData) while a new query fetches, and they are NOT locally
  // re-filtered — so clone/fork would act on a stale artifact. Disable those
  // actions until the current query resolves. (Scope/list mode isn't affected:
  // its rows are locally filtered and the id-match stays valid.)
  const actionsStale = useSearch && isPlaceholderData
  return (
    <Card className="mt-4">
      <CardTitle>{i18nT('pages.artifactsPage.on')} {provider.display_name}</CardTitle>
      <div className="mb-2">
        <SearchInput placeholder={i18nT('pages.artifactsPage.filter_artifacts', { provider: provider.display_name })} value={search} onChange={e => setSearch((e.target as HTMLInputElement).value)} />
      </div>
      <div className="divide-y divide-border">
        {filtered.map((a) => (
          <RemoteArtifactCard
            key={a.external_id}
            artifact={a}
            provider={provider.name}
            providerLabel={provider.display_name}
            onForked={onForked}
            onCloned={onCloned}
            actionsDisabled={actionsStale}
          />
        ))}
      </div>
      {hasNextPage && (
        <div className="mt-2 flex justify-center">
          {/* Stable aria-label: while loading the button shows only a spinner
              icon, so it needs an accessible name in both states. */}
          <Btn
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
            aria-label={i18nT('pages.artifactsPage.load_more_artifacts', { provider: provider.display_name })}
          >
            {isFetchingNextPage
              ? <Loader2 className="lucide-inline w-3.5 h-3.5 animate-spin" />
              : i18nT('pages.artifactsPage.load_more')}
          </Btn>
        </div>
      )}
    </Card>
  )
}

