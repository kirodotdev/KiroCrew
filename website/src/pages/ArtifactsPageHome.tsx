/** The Artifacts page's Home view: what a person sees with nothing selected in
 *  the side list. Two groups, one above the other in the gallery and side by
 *  side in the table:
 *
 *  - **Recent**: what was made on purpose or kept (starred, filed, edited,
 *    tagged, commented, shared). Nothing here is ever cleaned up.
 *  - **Recent output**: what sessions produced that nobody kept yet -- the
 *    records the server flags `sweepable`, plus documents agents wrote that are
 *    not in the library at all. Starring one keeps it and moves it up a row.
 *
 *  The split is the server's own sweep predicate, read off the list response,
 *  so the page never guesses what would be deleted. */
import { useMemo, useState } from 'react'
import { LayoutGroup, MotionConfig, motion } from 'framer-motion'
import { ExternalLink, FileText, Loader2, MoreHorizontal, Star, X } from 'lucide-react'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../components/ui/dropdown-menu'
import { guidePick } from '../uiLocations/targetRegistry'
import { openPopout } from '../utils/artifactPopout'
import { DndDraggable } from '../components/dnd'
import Clickable from '../components/Clickable'
import { Btn, IconButton } from '../components/ui'
import { isoToTs, KindPill, SessionDocStar } from '../components/library/LibraryTable'
import type { LibraryDrag } from '../components/library/LibraryTable'
import type { Artifact, ArtifactFolder, SessionDoc } from '../types'
import { timeAgo } from '../utils/timeAgo'
import { i18nT } from '../i18n/t'
import { ArtifactMiniThumb } from '../components/library/ArtifactThumbs'
import { LiveSiteLink } from '../components/library/LiveSiteLink'
import { PublicationMark, PublicationSyncError } from './ArtifactsPageGallery'

/** How many entries each group shows before "Show all". */
const HOME_ROW_LIMIT = 8

type OutputEntry =
  | { type: 'art'; key: string; ts: number; a: Artifact }
  | { type: 'doc'; key: string; ts: number; d: SessionDoc }

export type HomeHandlers = {
  onOpen: (slug: string) => void
  onTogglePin: (a: Artifact) => void
  pinningSlug: string | null
  onMaterialize: (path: string, sessionKey?: string) => void
  materializingPath: string | null
  onPreviewDoc: (d: SessionDoc) => void
}

/** "Doc · CI Health · 3d ago": what it is, where it lives or came from, when. */
function artifactMeta(a: Artifact, folderNames: ReadonlyMap<string, string>): string {
  // The server writes a parenthesised placeholder ("(deleted session)") for a
  // session it cannot resolve; that tells a reader nothing, so leave it out.
  const session = a.session_title && !a.session_title.startsWith('(') ? a.session_title : ''
  const where = (a.folder_id && folderNames.get(a.folder_id)) || session
  // The kind is drawn as a pill beside this line, so it is not repeated here.
  return [where, timeAgo(isoToTs(a.updated_at))].filter(Boolean).join(' · ')
}

function docMeta(d: SessionDoc): string {
  return [i18nT('pages.artifactsPage.output_file'), d.session_title, timeAgo(isoToTs(d.updated_at))].filter(Boolean).join(' · ')
}

function StarToggle({ a, busy, onToggle }: { a: Artifact; busy: boolean; onToggle: (a: Artifact) => void }) {
  return (
    // `relative z-10` lifts the star above the card's open button. `aria-disabled`
    // rather than `disabled` keeps focus on it while the save runs.
    <button
      type="button"
      aria-disabled={busy}
      aria-busy={busy}
      aria-pressed={!!a.pinned}
      onClick={(e) => { e.stopPropagation(); if (!busy) onToggle(a) }}
      title={a.pinned ? i18nT('pages.artifactsPage.starred_click_to_unstar') : i18nT('pages.artifactsPage.star_to_keep')}
      aria-label={a.pinned ? i18nT('pages.artifactsPage.remove_star_from_artifact') : i18nT('pages.artifactsPage.star_to_keep')}
      className={`relative z-10 shrink-0 p-1 rounded bg-transparent border-none cursor-pointer aria-disabled:cursor-default ${a.pinned ? 'text-accent' : 'text-muted hover:text-accent'}`}
    >
      {busy ? <Loader2 size={13} className="animate-spin" /> : <Star size={13} className={a.pinned ? 'fill-current' : ''} />}
    </button>
  )
}

/** Pop-out and remove behind one "⋯" trigger, so a card's row holds two
 *  controls (this and the star). Home leaves it off; the library grid (All
 *  artifacts, a folder) passes `onDelete`. */
function CardActions({ a, onDelete, deleting }: { a: Artifact; onDelete: (a: Artifact) => void; deleting: boolean }) {
  return (
    // `relative z-10` lifts the trigger above the card's open button.
    <div className="relative z-10 shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100 has-[[data-state=open]]:opacity-100 transition-opacity">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <IconButton
            aria-label={i18nT('pages.artifactsPage.more_actions')}
            title={i18nT('pages.artifactsPage.more_actions')}
            aria-busy={deleting || undefined}
            onClick={(e) => e.stopPropagation()}
          >
            {deleting ? <Loader2 size={13} className="animate-spin" /> : <MoreHorizontal size={13} />}
          </IconButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => openPopout(a.slug, a.name)}>
            <ExternalLink size={13} className="text-muted shrink-0" /> {i18nT('pages.artifactsPage.pop_out_to_window')}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => { if (!deleting) onDelete(a) }}>
            <X size={13} className="text-danger shrink-0" /> {i18nT('pages.artifactsPage.remove_from_library')}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

/** One artifact as a same-size card: a preview cropped to a fixed height, then
 *  its name and one meta line. Home and the library grid share it, so every
 *  card on the page has the same shape and a row scans straight across. */
export function ArtifactCard({ a, meta, output, h, onDelete, deletingSlug }: {
  a: Artifact
  meta: string
  /** Dashed border: an output the sweep could still take. */
  output: boolean
  h: HomeHandlers
  onDelete?: (a: Artifact) => void
  deletingSlug?: string | null
}) {
  return (
    <DndDraggable id={`card-artifact:${a.slug}`} data={{ type: 'artifact', slug: a.slug, name: a.name, folderId: a.folder_id || '' } satisfies LibraryDrag}>
      {({ setNodeRef, listeners, isDragging }) => (
        <div
          // The artifact's name as the card shows it: a guide's pick outlines
          // the one card named exactly like this (artifacts.list).
          {...guidePick(a.name, setNodeRef)}
          {...listeners}
          style={{ opacity: isDragging ? 0.4 : 1 }}
          className={`group relative rounded-lg border bg-card overflow-hidden hover:border-border-strong transition-colors ${output ? 'border-dashed border-border' : 'border-border'}`}
        >
          <button
            type="button"
            onClick={() => h.onOpen(a.slug)}
            aria-label={a.name}
            className="absolute inset-0 rounded-lg cursor-pointer bg-transparent border-none p-0 focus-visible:-outline-offset-2"
          />
          <ArtifactMiniThumb a={a} className="h-[140px] border-b border-border" />
          <div className="flex items-start gap-1 p-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5 min-w-0">
                <span className="text-[13px] font-medium text-text-strong truncate">{a.name}</span>
                <PublicationMark a={a} onOpen={h.onOpen} />
              </div>
              <div className="flex items-center gap-1.5 min-w-0 mt-0.5">
                <KindPill kind={a.kind} />
                <span className="text-[11px] text-muted truncate">{meta}</span>
              </div>
              {/* What the masonry card showed is kept, each line only when the
                * artifact has it: the live public-web link, a sync failure, the
                * description (one line) and the tags (one line). */}
              <LiveSiteLink slug={a.slug} />
              <PublicationSyncError a={a} />
              {a.description && <div className="text-[12px] text-muted mt-1 truncate">{a.description}</div>}
              {a.tags && a.tags.length > 0 && (
                <div className="flex gap-1 mt-1.5 overflow-hidden">
                  {a.tags.map((t) => (
                    <span key={t} className="shrink-0 text-[11px] px-1.5 py-0.5 rounded bg-bg-elevated border border-border text-muted">{t}</span>
                  ))}
                </div>
              )}
            </div>
            {onDelete && <CardActions a={a} onDelete={onDelete} deleting={deletingSlug === a.slug} />}
            <StarToggle a={a} busy={h.pinningSlug === a.slug} onToggle={h.onTogglePin} />
          </div>
        </div>
      )}
    </DndDraggable>
  )
}

/** The library's gallery on a desktop (All artifacts, or one folder): the same
 *  cards as Home, in rows of equal height. */
export function ArtifactCardGrid({ artifacts, folders, h, onDelete, deletingSlug }: {
  artifacts: Artifact[]
  folders: ArtifactFolder[]
  h: HomeHandlers
  onDelete: (a: Artifact) => void
  deletingSlug: string | null
}) {
  const folderNames = useMemo(() => new Map(folders.map((f) => [f.id, f.name])), [folders])
  return (
    <div className={GRID} data-testid="artifacts-card-grid">
      {artifacts.map((a) => (
        <ArtifactCard
          key={a.slug}
          a={a}
          meta={artifactMeta(a, folderNames)}
          output={!!a.sweepable}
          h={h}
          onDelete={onDelete}
          deletingSlug={deletingSlug}
        />
      ))}
    </div>
  )
}

/** The one layout identity a Home entry keeps across both groups. An artifact
 *  saved from a chat document is keyed by that document's path (its
 *  `source_path`), so the document's card and the artifact's card are one
 *  element and starring the document slides it into Recent. */
const layoutKeyOf = (a: Artifact) => (a.source_path ? `home-src:${a.source_path}` : `home-artifact:${a.slug}`)
const docLayoutKey = (d: SessionDoc) => `home-src:${d.path}`

function DocCard({ d, h }: { d: SessionDoc; h: HomeHandlers }) {
  return (
    <motion.div layout layoutId={docLayoutKey(d)}>
    <Clickable
      onClick={() => h.onPreviewDoc(d)}
      className="relative rounded-lg border border-dashed border-border bg-card overflow-hidden hover:border-border-strong transition-colors cursor-pointer"
    >
      <div className="h-[140px] border-b border-border bg-bg-elevated flex items-center justify-center text-muted">
        <FileText size={22} />
      </div>
      <div className="flex items-start gap-1 p-3">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium text-text-strong truncate">{d.name}</div>
          <div className="text-[11px] text-muted truncate">{docMeta(d)}</div>
        </div>
        <SessionDocStar d={d} busy={h.materializingPath === d.path} onMaterialize={h.onMaterialize} />
      </div>
    </Clickable>
    </motion.div>
  )
}

function ArtifactRow({ a, meta, h }: { a: Artifact; meta: string; h: HomeHandlers }) {
  return (
    <motion.li
      // One row per artifact across both groups (`layoutId`), so a row that
      // moves from Recent output to Recent slides there rather than vanishing.
      layout
      layoutId={layoutKeyOf(a)}
      // The artifact's name as the row shows it: a guide's pick outlines the
      // one row named exactly like this (artifacts.list).
      {...guidePick(a.name)}
      className="relative flex items-center gap-2 px-3 py-2 border-t border-border first:border-t-0 hover:bg-bg-hover"
    >
      <button
        type="button"
        onClick={() => h.onOpen(a.slug)}
        aria-label={a.name}
        className="absolute inset-0 cursor-pointer bg-transparent border-none p-0 focus-visible:-outline-offset-2"
      />
      <StarToggle a={a} busy={h.pinningSlug === a.slug} onToggle={h.onTogglePin} />
      <div className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5 min-w-0">
          <span className="text-[13px] text-text-strong truncate">{a.name}</span>
          <PublicationMark a={a} onOpen={h.onOpen} />
        </span>
        {/* A failed publish sync shows its reason and hand-off here, as the
          * library table and the cards do, not only as the red mark. */}
        <PublicationSyncError a={a} />
      </div>
      <span className="shrink-0 max-w-[30%] min-w-0"><LiveSiteLink slug={a.slug} /></span>
      <KindPill kind={a.kind} />
      <span className="shrink-0 max-w-[45%] text-[11px] text-muted truncate">{meta}</span>
    </motion.li>
  )
}

function DocRow({ d, h }: { d: SessionDoc; h: HomeHandlers }) {
  return (
    <motion.li layout layoutId={docLayoutKey(d)} className="border-t border-border first:border-t-0">
      <Clickable onClick={() => h.onPreviewDoc(d)} className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-bg-hover">
        <SessionDocStar d={d} busy={h.materializingPath === d.path} onMaterialize={h.onMaterialize} />
        <span className="min-w-0 flex-1 text-[13px] text-text-strong truncate">{d.name}</span>
        <span className="shrink-0 max-w-[50%] text-[11px] text-muted truncate">{docMeta(d)}</span>
      </Clickable>
    </motion.li>
  )
}

function GroupHeading({ title, subtitle, action }: { title: string; subtitle: string; action?: React.ReactNode }) {
  return (
    // The subtitle is small and takes its own line under the title, so it
    // wraps rather than truncates: on Recent output it carries the cleanup
    // consequence ("N of 200 before the oldest clear"), which must stay
    // readable in the half-width table column.
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 mb-2 min-w-0">
      <h3 className="text-sm font-semibold text-text-strong m-0 shrink-0">{title}</h3>
      {action && <span className="ml-auto shrink-0">{action}</span>}
      <p className="text-[11px] leading-snug text-muted m-0 basis-full min-w-0">{subtitle}</p>
    </div>
  )
}

/** At most four cards a row, so each stays big enough to read. */
const GRID = 'grid gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4'

/** A Home card in its grid cell, one element across both groups
 *  (`layoutId`): a card starred in Recent output slides up into Recent. */
function HomeCard({ a, meta, output, h }: { a: Artifact; meta: string; output: boolean; h: HomeHandlers }) {
  return (
    <motion.div layout layoutId={layoutKeyOf(a)}>
      <ArtifactCard a={a} meta={meta} output={output} h={h} />
    </motion.div>
  )
}

export function ArtifactsHome({ artifacts, sessionDocs, outputCap, folders, view, onSeeAll, handlers: h }: {
  artifacts: Artifact[]
  /** Unsaved session documents (not library records). */
  sessionDocs: SessionDoc[]
  /** The sweep's cap from the list response; absent on an older gateway. */
  outputCap?: number
  folders: ArtifactFolder[]
  view: 'grid' | 'table'
  /** Switches the page to "All artifacts". */
  onSeeAll: () => void
  handlers: HomeHandlers
}) {
  const [showAllOutput, setShowAllOutput] = useState(false)
  // Outputs starred on this visit, newest first: artifact slugs, and chat
  // documents by path (the artifact saved from one carries it as
  // `source_path`). They lead Recent so a kept card lands in the visible slice
  // instead of past "See all": keeping does not touch `updated_at`, so date
  // order alone would drop it out of sight.
  const [keptHere, setKeptHere] = useState<string[]>([])
  const hh = useMemo<HomeHandlers>(() => {
    const keepFirst = (id: string) => setKeptHere((k) => [id, ...k.filter((s) => s !== id)])
    return {
      ...h,
      onTogglePin: (a) => {
        if (!a.pinned && a.sweepable) keepFirst(`slug:${a.slug}`)
        h.onTogglePin(a)
      },
      onMaterialize: (path, sessionKey) => {
        keepFirst(`path:${path}`)
        h.onMaterialize(path, sessionKey)
      },
    }
  }, [h])
  const folderNames = useMemo(() => new Map(folders.map((f) => [f.id, f.name])), [folders])
  const recent = useMemo(() => {
    const rank = (a: Artifact) => {
      const i = keptHere.findIndex((k) => k === `slug:${a.slug}` || (!!a.source_path && k === `path:${a.source_path}`))
      return i < 0 ? Infinity : i
    }
    return artifacts
      .filter((a) => !a.sweepable)
      .sort((x, y) => rank(x) - rank(y) || isoToTs(y.updated_at) - isoToTs(x.updated_at))
  }, [artifacts, keptHere])
  const sweepableCount = useMemo(() => artifacts.filter((a) => a.sweepable).length, [artifacts])
  const output = useMemo<OutputEntry[]>(() => {
    // A document whose path already backs a listed artifact (saved, then
    // unstarred: the scan reports it unsaved again) shows as that artifact
    // only. Both would share the `home-src:` layout id, and two mounted
    // elements with one id inside a LayoutGroup hide or jump each other.
    const backed = new Set(artifacts.map((a) => a.source_path).filter(Boolean))
    const rows: OutputEntry[] = [
      ...artifacts.filter((a) => a.sweepable).map((a) => ({ type: 'art' as const, key: `a:${a.slug}`, ts: isoToTs(a.updated_at), a })),
      ...sessionDocs
        .filter((d) => !backed.has(d.path))
        .map((d) => ({ type: 'doc' as const, key: `d:${d.path}`, ts: isoToTs(d.updated_at), d })),
    ]
    return rows.sort((x, y) => y.ts - x.ts)
  }, [artifacts, sessionDocs])

  const recentShown = recent.slice(0, HOME_ROW_LIMIT)
  const outputShown = showAllOutput ? output : output.slice(0, HOME_ROW_LIMIT)

  const recentHeading = (
    <GroupHeading
      title={i18nT('pages.artifactsPage.recent')}
      subtitle={i18nT('pages.artifactsPage.recent_subtitle')}
      action={recent.length > HOME_ROW_LIMIT && (
        <Btn onClick={onSeeAll} className="shrink-0">
          {i18nT('pages.artifactsPage.see_all')}
        </Btn>
      )}
    />
  )
  const outputHeading = (
    <GroupHeading
      title={i18nT('pages.artifactsPage.recent_output')}
      subtitle={outputCap
        ? i18nT('pages.artifactsPage.recent_output_subtitle_cap', { used: sweepableCount, cap: outputCap })
        : i18nT('pages.artifactsPage.recent_output_subtitle')}
      action={output.length > HOME_ROW_LIMIT && (
        <Btn onClick={() => setShowAllOutput((v) => !v)} aria-expanded={showAllOutput} className="shrink-0">
          {showAllOutput ? i18nT('pages.artifactsPage.show_less') : i18nT('pages.artifactsPage.show_all_count', { count: output.length })}
        </Btn>
      )}
    />
  )
  const recentEmpty = <p className="text-[13px] text-muted m-0 py-3">{i18nT('pages.artifactsPage.recent_empty')}</p>
  const outputEmpty = <p className="text-[13px] text-muted m-0 py-3">{i18nT('pages.artifactsPage.recent_output_empty')}</p>

  if (view === 'table') {
    return (
      <MotionConfig reducedMotion="user">
      <LayoutGroup>
      <div className="grid gap-6 grid-cols-1 lg:grid-cols-2 items-start" data-testid="artifacts-home">
        <section>
          {recentHeading}
          {recentShown.length === 0 ? recentEmpty : (
            <ul className="list-none m-0 p-0 rounded-lg border border-border bg-card overflow-hidden">
              {recentShown.map((a) => <ArtifactRow key={a.slug} a={a} meta={artifactMeta(a, folderNames)} h={hh} />)}
            </ul>
          )}
        </section>
        <section>
          {outputHeading}
          {outputShown.length === 0 ? outputEmpty : (
            <ul className="list-none m-0 p-0 rounded-lg border border-dashed border-border bg-card overflow-hidden">
              {outputShown.map((e) => (e.type === 'art'
                ? <ArtifactRow key={e.key} a={e.a} meta={artifactMeta(e.a, folderNames)} h={hh} />
                : <DocRow key={e.key} d={e.d} h={hh} />))}
            </ul>
          )}
        </section>
      </div>
      </LayoutGroup>
      </MotionConfig>
    )
  }
  return (
    // `reducedMotion="user"` drops the slide for a reduced-motion setting; the
    // card still moves as one element, without the animation.
    <MotionConfig reducedMotion="user">
    <LayoutGroup>
    <div className="flex flex-col gap-6" data-testid="artifacts-home">
      <section>
        {recentHeading}
        {recentShown.length === 0 ? recentEmpty : (
          <div className={GRID}>
            {recentShown.map((a) => <HomeCard key={a.slug} a={a} meta={artifactMeta(a, folderNames)} output={false} h={hh} />)}
          </div>
        )}
      </section>
      <section>
        {outputHeading}
        {outputShown.length === 0 ? outputEmpty : (
          <div className={GRID}>
            {outputShown.map((e) => (e.type === 'art'
              ? <HomeCard key={e.key} a={e.a} meta={artifactMeta(e.a, folderNames)} output h={hh} />
              : <DocCard key={e.key} d={e.d} h={hh} />))}
          </div>
        )}
      </section>
    </div>
    </LayoutGroup>
    </MotionConfig>
  )
}
