/** The Artifacts gallery: the artifact card, and the three renderers that lay
 *  cards out (a content-sized CSS-columns masonry, the virtualized masonry, and
 *  the one-column list windowed against the page's own scroller), switched at
 *  `VIRTUALIZE_AT`. */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink, X, Share2, Loader2, Star } from 'lucide-react'
import { openPopout } from '../utils/artifactPopout'
import { VirtuosoMasonry } from '@virtuoso.dev/masonry'
import type { ItemContent } from '@virtuoso.dev/masonry'
// At one column the gallery is a list, and it is windowed by this app's own
// virtualizer rather than a library: see `LibraryList` for the measured reason.
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { api } from '../api/client'
import { Badge } from '../components/ui'
import { publishNoticeKey } from '../components/PublishHub'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import { DndDraggable } from '../components/dnd'
import type { Artifact } from '../types'
import { KIND_BADGE, isoToTs } from '../components/library/LibraryTable'
import type { LibraryDrag } from '../components/library/LibraryTable'
import { WidgetThumb, ContentThumb, ImageThumb, WebAppThumb } from '../components/library/ArtifactThumbs'
import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import { guidePick } from '../uiLocations/targetRegistry'

// ── Masonry library ──────────────────────────────────────────────────────
// The Library renders as a virtualized masonry (瀑布流) via VirtuosoMasonry.
// Widget/html artifacts get a live sandboxed preview thumbnail that self-sizes
// (height reporter), giving the waterfall its natural varying heights; other
// kinds get a content snippet. Virtualization means only on-screen previews
// mount, so N sandboxed iframes stay cheap.

/** A cell in the "Your Artifacts" grid — a local artifact. */
export type GridEntry = { kind: 'local'; key: string; art: Artifact }

type LibCtx = {
  onOpen: (slug: string) => void
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
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
    //. The sensors' activation constraints (see dndSensors in ArtifactsPage.tsx) keep a plain click
    // and a finger swipe reaching the card / the gallery scroller.
    <DndDraggable id={`artifact:${a.slug}`} data={{ type: 'artifact', slug: a.slug, name: a.name, folderId: a.folder_id || '' } satisfies LibraryDrag}>
      {({ setNodeRef, listeners, isDragging }) => (
    <div
      // The artifact's name as the card shows it: a guide's pick outlines the
      // one card named exactly like this (artifacts.list).
      {...guidePick(a.name, setNodeRef)}
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
        {a.kind === 'webapp' ? <WebAppThumb art={full ?? a} /> : a.kind === 'image' ? <ImageThumb a={a} /> : hasPreview ? <WidgetThumb content={content} slug={a.slug} goalReport={a.goal_report === true} /> : <ContentThumb content={content} kind={a.kind} />}
      </div>
      <div className="p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-text-strong font-medium truncate">{a.name}</span>
              {a.publication && (
                <Share2
                  size={12}
                  className={a.publication.last_error ? 'text-danger shrink-0' : a.publication.notice ? 'text-warn shrink-0' : 'text-ok shrink-0'}
                  aria-label={a.publication.last_error ? i18nT('pages.artifactsPage.published_sync_issue') : a.publication.notice ? i18nT(publishNoticeKey({ rolling_out: 'pages.artifactsPage.published_rolling_out', distribution_disabled: 'pages.artifactsPage.published_distribution_disabled', notice_generic: 'pages.artifactsPage.published_notice_generic' }, a.publication.notice_code)) : i18nT('pages.artifactsPage.published', { visibility: a.publication.visibility.toLowerCase() })}
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

/** Card count at which the gallery switches from a content-sized CSS-columns
 *  masonry to the virtualized one.
 *
 *  Module scope because TWO places decide on it: the gallery picks its render
 *  mode, and the page decides which element owns vertical scrolling. Virtualized
 *  mode brings its own scroller, so if these two ever read different numbers the
 *  page ends up with two same-axis scrollers again — the exact defect the
 *  single-scroller plumbing in `ArtifactsPage.tsx` exists to remove. */
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
export function LibraryMasonry({
  entries,
  cols,
  widthRef,
  scrollerRef,
  fillPage,
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
  widthRef: React.Ref<HTMLDivElement>
  /** The page's scrolling column. A ref, not the resolved element: the
   *  virtualizer takes `externalScrollerRef` and reads it when it needs it, so
   *  nothing has to re-render just because the element appeared. */
  scrollerRef: React.RefObject<HTMLDivElement | null>
  /** Fill the remaining page height only when no remote sections need the page axis. */
  fillPage: boolean
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
  // Multi-column masonry always needs its own viewport. It fills the page only
  // when there are no remote sections; otherwise it is a bounded section inside
  // the scrolling page. A one-column list uses the page's external scroller.
  const masonryOwnsScroll = virtualized && cols > 1
  return (
    // -mr-3 offsets each card's own mr-3 so the trailing column's gutter
    // doesn't add page width; cards carry mr-3 (gutter) + mb-3 (row gap).
    // The picker a guide's "choose the artifact" step points at (artifacts.list).
    <div {...uiLocation('artifacts.list', widthRef)} aria-label={i18nT('pages.artifactsPage.your_artifacts')} data-testid="artifacts-gallery" className={masonryOwnsScroll
      ? (fillPage ? '-mr-3 flex-1 min-h-0' : '-mr-3 h-[60vh]')
      : '-mr-3'}>
      {asList ? (
        <LibraryList entries={entries} context={context} scrollerRef={scrollerRef} />
      ) : masonryOwnsScroll ? (
        <VirtuosoMasonry
          key={cols}
          columnCount={cols}
          data={entries}
          context={context}
          ItemContent={GridCard}
          // The parent supplies either the remaining page height or a bounded
          // section height when remote lists need to scroll past the gallery.
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
