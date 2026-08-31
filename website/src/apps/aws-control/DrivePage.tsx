/**
 * AWS Control - the cloud drive, as its own page.
 *
 * Reached from the Cloud drive capability row on the account console; a
 * breadcrumb returns. Like the console it is view state inside `AwsControlPage`
 * rather than a route of its own, because `BuiltinAppRoute` resolves only
 * single-segment routes.
 *
 * One bucket holds three sections behind their own prefixes - the artifact
 * library, the file drive, and backups - and this page is where all three live,
 * together with the share ledger that governs links into them. They were four
 * stacked sections on the account console; that page now carries one row saying
 * the drive exists, and everything about its CONTENTS is here.
 *
 * The file listing renders through the shared library table header
 * (`components/library/LibraryTable`), declaring its own columns: an S3 object
 * has no slug, kind, source, version or tags, so the artifact library's nine
 * columns would have to be invented for it. What IS shared is the header chrome
 * - the sort control and the pinned Actions cell with its measured seam - which
 * is the part that is subtle and expensive to keep in sync by hand.
 *
 * Every mutation is confirmed before it runs and ends by invalidating its
 * react-query key. All AWS access runs through the gateway's audited CLI
 * chokepoint; this surface never talks to AWS from the browser.
 */
import { Fragment, useRef, useState } from 'react'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ChevronLeft, ChevronDown, RefreshCw, HardDrive, Library, Archive, Share2,
  Download, Trash2, Upload, FolderClosed, FolderPlus, FileText, X,
  MoreHorizontal, Code, LayoutGrid, List, Search, CloudOff, Plus,
} from 'lucide-react'
import { Btn, Badge, Toggle, Input, ContentSkeleton, IconButton } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import SegmentedControl from '../../components/SegmentedControl'
import { LibraryTableHead } from '../../components/library/LibraryTable'
import type { LibraryColumn } from '../../components/library/LibraryTable'
import {
  WidgetThumb, ContentThumb, ImageThumb, WebAppThumb,
} from '../../components/library/ArtifactThumbs'
import { useColumnCount } from '../../hooks/useColumnCount'
import { usePersistedString } from '../../hooks/usePersistedString'
import { api } from '../../api/client'
import type { Artifact } from '../../types'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useNearViewport } from '../../hooks/useNearViewport'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtNumber, fmtRelative } from '../../i18n/format'
import { awsControlApi } from './api'
import type {
  AwsAccount, DriveSection, DriveStatus, ArtifactKind, LibraryArtifact,
  BackupKind, Share, DriveUsage, DriveSectionUsage,
} from './types'
import { CopyBtn, SectionHeader } from './shared'

/** The account's display name, or the not-connected label. */
function accountNameOf(account: AwsAccount): string {
  return account.name || i18nT('apps.awsControl.page.not_connected_yet')
}

/* Literal-key maps from enum → full catalog key, so no i18nT() call assembles a
 * key by interpolation (dynamicKeys gate): extractors and unused-key tooling
 * can then see every key, and a missing one fails the parity gate rather than
 * rendering raw. Mirrors UPDATE_ERROR_KEYS in pages/settings/AboutPanel.tsx. */
const KIND_LABEL_KEY: Record<ArtifactKind, string> = {
  widget: 'apps.awsControl.console.kind_widget',
  markdown: 'apps.awsControl.console.kind_markdown',
  html: 'apps.awsControl.console.kind_html',
  svg: 'apps.awsControl.console.kind_svg',
  json: 'apps.awsControl.console.kind_json',
  text: 'apps.awsControl.console.kind_text',
  webapp: 'apps.awsControl.console.kind_webapp',
  image: 'apps.awsControl.console.kind_image',
}

const EXPIRY_LABEL_KEY: Record<string, string> = {
  '1h': 'apps.awsControl.console.expiry_1h',
  '1d': 'apps.awsControl.console.expiry_1d',
  '7d': 'apps.awsControl.console.expiry_7d',
}

const BACKUP_KIND_LABEL_KEY: Record<BackupKind, string> = {
  snapshot: 'apps.awsControl.console.backup_kind_snapshot',
  sessions: 'apps.awsControl.console.backup_kind_sessions',
}

/** A collapsible `</>` drawer: the bucket, a prefix, and a generic CLI line. */
function CliDrawer({ bucket, prefix }: { bucket: string; prefix: string }) {
  const [open, setOpen] = useState(false)
  const line = `aws s3 ls s3://${bucket}/${prefix}`
  return (
    <div className="mt-2" data-testid="cli-drawer">
      <button
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
        aria-expanded={open}
        data-testid="cli-drawer-toggle"
      >
        <Code size={12} />
        {i18nT('apps.awsControl.console.cli_drawer_label')}
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="mt-1.5 rounded-md border border-border bg-bg-elevated p-2.5 text-[12px]" data-testid="cli-drawer-body">
          <div className="text-muted mb-1">
            {i18nT('apps.awsControl.console.cli_drawer_hint', { bucket, prefix })}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">
              {line}
            </code>
            <CopyBtn text={line} />
          </div>
        </div>
      )}
    </div>
  )
}

/* ── Section 4: Library ──────────────────────────────────────────────────── */

const KIND_KEYS: ArtifactKind[] =
  ['widget', 'markdown', 'html', 'svg', 'json', 'text', 'webapp', 'image']

/**
 * How a listing is drawn: as thumbnail cards, or as table rows.
 *
 * Persisted PER SECTION, with a different default for each, because the two
 * folders hold different things. Library holds artifacts that have a real
 * rendered preview, so a grid of thumbnails is what makes it readable at a
 * glance. Files holds arbitrary uploads with no preview but with a size, a type
 * and a modified time worth comparing down a column, so it opens as a table --
 * the same split a file manager makes between a photo folder and a documents
 * folder. Once a reader chooses, that choice is remembered for that section.
 */
type ViewMode = 'grid' | 'list'

/* Literal keys, not an interpolated one. Same discipline as the catalog-key maps
 * above: a key assembled at the call site is invisible to any tool that greps for
 * it, and the i18n added-lines gate reads a template literal in this position as a
 * built string rather than a constant. Spelling the three out costs two lines. */
const VIEW_MODE_STORAGE_KEY = {
  drive: 'awsControl.drive.viewMode.drive',
  library: 'awsControl.drive.viewMode.library',
} as const

function useViewMode(section: keyof typeof VIEW_MODE_STORAGE_KEY, fallback: ViewMode): readonly [ViewMode, (v: ViewMode) => void] {
  const [raw, setRaw] = usePersistedString(VIEW_MODE_STORAGE_KEY[section], fallback)
  // Anything other than the two known words reads as the section's own default
  // rather than rendering nothing: localStorage is writable by hand and survives
  // a rename of these values, so an unknown string must not be able to blank a
  // folder the reader can no longer get back.
  const mode: ViewMode = raw === 'list' ? 'list' : raw === 'grid' ? 'grid' : fallback
  return [mode, (v: ViewMode) => setRaw(v)] as const
}

/**
 * The grid/list pair.
 *
 * This is `SegmentedControl`, not a hand-rolled pair of buttons: the Artifacts
 * gallery already drives the IDENTICAL grid-vs-table choice through it, and a
 * second spelling of one control is how the two drift apart. `collapse={false}`
 * because this sits in a content-hugging header group rather than a measured
 * column, which is the same reason the gallery passes it. Each section owns its
 * own `layoutId` -- the indicator is a framer shared-layout animation, and two
 * live controls sharing one id fight over it.
 */
function ViewModeToggle({ section, mode, onChange }: {
  section: keyof typeof VIEW_MODE_STORAGE_KEY
  mode: ViewMode
  onChange: (v: ViewMode) => void
}) {
  return (
    <SegmentedControl<ViewMode>
      segments={[
        { key: 'grid', label: i18nT('apps.awsControl.console.view_grid'), icon: <LayoutGrid size={13} /> },
        { key: 'list', label: i18nT('apps.awsControl.console.view_list'), icon: <List size={13} /> },
      ]}
      value={mode}
      onChange={onChange}
      layoutId={`aws-drive-view-${section}`}
      collapse={false}
    />
  )
}

/**
 * One artifact's preview, drawn with the SAME components the Artifacts gallery
 * uses rather than a second set written for this page.
 *
 * The listing payloads here carry metadata only, so the full artifact (with its
 * `content`) is fetched lazily per slug on the shared `['artifact', slug]` key --
 * the same key the gallery and the detail page use, so a reader who has already
 * seen an artifact anywhere else pays nothing to see it here.
 *
 * The fetch is gated on the card being NEAR THE VIEWPORT, and that gate is
 * load-bearing rather than a nicety. The gallery this borrows from renders
 * through `VirtuosoMasonry`, so only on-screen cards ever mount and the eager
 * fetch costs what is visible. Both grids here are plain `.map()` with no
 * virtualization -- a Library page can hold up to 500 slugs and the picker holds
 * the WHOLE local library (212 artifacts on a real one) -- so an ungated fetch
 * fires hundreds of concurrent full-artifact GETs at the gateway the moment the
 * picker opens. `WidgetThumb` already defers its document mint through this same
 * hook for the same reason; the JSON body needs it just as much.
 */
function ArtifactPreview({ slug, kind }: { slug: string; kind: ArtifactKind }) {
  const boxRef = useRef<HTMLDivElement>(null)
  const near = useNearViewport(boxRef)
  const { data: full } = useQuery<Artifact>({
    queryKey: ['artifact', slug],
    queryFn: () => api.artifact(slug),
    staleTime: 60_000,
    enabled: !!slug && near,
  })
  const content = full?.content || ''
  /* The box exists before the fetch does, because the observer needs something
     mounted to watch -- and it reserves roughly the height a thumb settles at, so
     a card does not jump when the preview arrives. */
  return (
    <div ref={boxRef} className="min-h-[120px]">
      {!near || !full ? (
        <div className="h-[120px] bg-bg-elevated" />
      ) : kind === 'webapp' ? (
        <WebAppThumb art={full} />
      ) : kind === 'image' ? (
        <ImageThumb a={full} />
      ) : kind === 'widget' || kind === 'html' ? (
        <WidgetThumb content={content} slug={slug} />
      ) : (
        <ContentThumb content={content} kind={kind} />
      )}
    </div>
  )
}

/**
 * A stored object with no local artifact behind it.
 *
 * The cloud copy outlives the local one — an artifact deleted locally, or a drive
 * pushed to from another machine, both land here. There is nothing to preview
 * (the bytes are in S3 and previewing them would cost a presign plus a fetch per
 * card), so the card says so plainly instead of showing a broken frame.
 */
function OrphanThumb() {
  return (
    <div className="flex h-[120px] flex-col items-center justify-center gap-1.5 bg-bg-elevated p-3 text-center">
      <CloudOff size={18} className="text-muted" aria-hidden="true" />
      <span className="text-[11px] leading-tight text-muted">
        {i18nT('apps.awsControl.console.library_cloud_only')}
      </span>
    </div>
  )
}

/**
 * The Library folder — what is ACTUALLY in the bucket's `artifacts/` prefix.
 *
 * This section used to render `GET /library/{account}`, which lists every LOCAL
 * artifact with its push state. That made a folder inside the drive show 212
 * things that were not in the drive, all of them labelled "not synced", while the
 * Files folder next to it sat empty — so the two folders could not be told apart
 * by looking at them, which is exactly what a reader asked about. It now lists
 * the prefix, so an object is here if and only if it is in the cloud, and the
 * local library is reached through the "add from Artifacts" picker instead.
 *
 * A push writes `library/{slug}/v{version}{ext}` plus `library/{slug}/meta.json`,
 * so the prefix's top level is one FOLDER per slug. Each folder name IS the slug,
 * which is what lets a card recover the artifact's name, kind and preview from
 * the local library; an object with no local copy falls back to `OrphanThumb`.
 */
function LibrarySection({ account, bucket }: { account: string; bucket: string }) {
  const [mode, setMode] = useViewMode('library', 'grid')
  const [picking, setPicking] = useState(false)
  const [gridRef, cols] = useColumnCount(258)

  /* What is in the cloud, ACCUMULATED across pages. A plain query keyed by the
     continuation token replaced the visible page on every "Load more", which is
     the opposite of what that label promises -- the reader pressed it to see
     more and the first page vanished. The page list stays under the same
     ['aws-control','drive',account] prefix every mutation invalidates. */
  const listQ = useInfiniteQuery({
    queryKey: ['aws-control', 'drive', account, 'list', 'library'],
    queryFn: ({ pageParam }) => awsControlApi.driveList(account, 'library', '', pageParam),
    initialPageParam: '',
    getNextPageParam: (last) => last.nextToken || undefined,
  })
  // The local library, used ONLY as a slug -> {name, kind} lookup for the cards
  // and as the picker's source. Never as the listing itself.
  const localQ = useQuery({
    queryKey: ['aws-control', 'library', account],
    queryFn: () => awsControlApi.library(account),
  })

  /* Identity for a cloud object, and ONLY from a local artifact this machine's
     ledger says it actually pushed.
     
     A bare slug match is not identity. Slugs come from names, so "notes" or
     "readme" collide across machines easily -- and a locally created,
     never-pushed artifact that happens to share a slug with an object someone
     else pushed would have lent this card its name, its kind AND its preview,
     under a footer asserting the thing is in the cloud. The card's whole
     contract is that it shows what IS up there, so a confident wrong answer is
     the one failure it cannot afford; the stale-version warning could not catch
     it either, since that needs a recorded push to compare against.
     
     An unverified match therefore renders as what it honestly is: a cloud object
     whose identity this machine cannot vouch for, same treatment as one with no
     local copy at all. That loses a name we might have guessed right, which is
     the correct trade against naming it wrong. The cause-level fix is reading
     the pushed meta.json sidecar instead of inferring from local state (#6987). */
  const bySlug = new Map<string, LibraryArtifact>()
  for (const a of localQ.data?.artifacts ?? []) {
    if (a.pushedAt !== null) bySlug.set(a.slug, a)
  }
  /* Whether the local library ACTUALLY answered. Until it has, `bySlug` is empty
     and every cloud object looks orphaned -- which would present each one as
     cloud-only AND offer it the orphan-only removal, the exact ledger-stranding
     path that removal is restricted to avoid. A pending or failed lookup is not
     the answer "there is no local copy", so nothing may be concluded from a miss
     until this is true. */
  const localAnswered = localQ.isSuccess

  const slugs = (listQ.data?.pages ?? []).flatMap((pg) => pg.folders.map((f) => f.split('/').pop() ?? f))
  /* Only what can ACTUALLY be added. Counting images here overpromised on the
     empty state's primary button -- they are the bulk of a real library and the
     picker then refuses every one of them. */
  const pushable = (localQ.data?.artifacts ?? [])
    .filter((a) => a.pushedVersion === null && a.kind !== 'image')
  /* Nothing addable BECAUSE everything left is a kind we cannot push yet -- as
     opposed to nothing addable because it is all already up here. The two look
     identical from the count alone and mean opposite things, and with the button
     hidden this sentence is the ONLY thing an images-only library ever sees. */
  const onlyUnaddable =
    pushable.length === 0 && (localQ.data?.artifacts ?? []).some((a) => a.kind === 'image')
  /* A local library with NOTHING in it is a third case, and it was falling into
     the second one: `library_add_nothing` ("Nothing left to add -- everything
     that can be is already here") rendered under "Nothing copied to the cloud
     yet", which contradicts itself and is false -- on the first screen a fresh
     install sees. There is no true sentence to put here that the empty state's
     own body does not already say, so this case says nothing. */
  const nothingLocalYet = (localQ.data?.artifacts ?? []).length === 0

  return (
    <section data-testid="library-section">
      <SectionHeader
        icon={<Library size={15} />}
        title={i18nT('apps.awsControl.console.section_library')}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <ViewModeToggle section="library" mode={mode} onChange={setMode} />
            <Btn primary onClick={() => setPicking(true)} data-testid="library-add-open">
              <Plus size={13} />
              {i18nT('apps.awsControl.console.library_add')}
            </Btn>
          </div>
        }
      />

      {/* What this folder holds, said once. The reader arrived here from a root
          card next to one called Files, and the distinction is the whole point
          of the section. */}
      {/* Not while the empty state is up: that state's own body explains what
          this folder holds, so the blurb restated the same two facts about
          100px above it. Kept during loading, so it does not flash out and
          back in as the listing resolves. */}
      {!(listQ.isSuccess && slugs.length === 0) && (
      <p className="mb-3 text-[12px] text-muted" data-testid="library-blurb">
        {i18nT('apps.awsControl.console.library_blurb')}{' '}
        {/* This folder can be filled from the picker and not emptied from here
            yet, and storage costs money. Saying where the exit is beats letting
            someone discover there isn't one.

            Only once there is something to remove, though: on an empty folder it
            warned about copies that do not exist, which is noise in front of the
            one screen whose job is explaining what the folder is FOR. The picker
            carries its own one-way warning, so the person about to create the
            first copy is still told. */}
        {slugs.length > 0 && (
          <span data-testid="library-remove-hint">{i18nT('apps.awsControl.console.library_remove_hint')}</span>
        )}
      </p>
      )}

      {listQ.isLoading && <ContentSkeleton rows={2} />}

      {/* A failed listing is not an empty folder. Without this the page showed
          the blurb over blank space, which reads as "there is nothing here" --
          the one conclusion we specifically cannot draw. */}
      {listQ.isError && (
        <div className="rounded-lg border border-border bg-card p-6 text-center" data-testid="library-error">
          <p className="mb-3 text-[13px] text-text">{i18nT('apps.awsControl.console.library_list_failed')}</p>
          <Btn onClick={() => listQ.refetch()} data-testid="library-retry">
            <RefreshCw size={13} />
            {i18nT('apps.awsControl.console.retry')}
          </Btn>
        </div>
      )}

      {/* The cloud listing can succeed while the LOCAL lookup behind it fails.
          When that happens the folder's contents are known but their names, kinds
          and previews are not, so the cards fall back to a neutral placeholder --
          correctly, since asserting "cloud only" from a failed lookup is the bug
          this join was built to avoid. What was missing is saying so: without
          this notice the placeholders never resolve and the empty state's action
          never appears, with nothing anywhere explaining why. The cards stay;
          only the silence goes. */}
      {localQ.isError && (
        <div
          className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-border bg-card px-3 py-2.5"
          data-testid="library-local-error"
        >
          <p className="flex-1 text-[12px] text-text">{i18nT('apps.awsControl.console.library_local_failed')}</p>
          <Btn onClick={() => localQ.refetch()} data-testid="library-local-retry">
            <RefreshCw size={13} />
            {i18nT('apps.awsControl.console.retry')}
          </Btn>
        </div>
      )}

      {listQ.isSuccess && slugs.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-8 text-center" data-testid="library-empty">
          <div className="mb-1.5 text-[13px] font-medium text-text-strong">
            {i18nT('apps.awsControl.console.library_empty_title')}
          </div>
          <p className="mx-auto mb-4 max-w-[52ch] text-[12px] leading-relaxed text-muted">
            {i18nT('apps.awsControl.console.library_empty_body')}
          </p>
          {/* With nothing addable the count read "(0 ready)" on a button that
              opens a picker refusing everything in it. Say that instead. */}
          {/* Every branch below asserts something about the LOCAL library, so
              none may render until it answered -- otherwise a failed lookup
              produces a confident "everything is already here" built on nothing.
              Same mistake as reading orphan-hood out of an empty map. */}
          {!localAnswered || nothingLocalYet ? null : pushable.length > 0 ? (
            <Btn primary onClick={() => setPicking(true)} data-testid="library-empty-add">
              <Plus size={13} />
              {i18nT('apps.awsControl.console.library_add_count', { count: fmtNumber(pushable.length) })}
            </Btn>
          ) : (
            <p className="text-[12px] text-muted" data-testid="library-empty-none">
              {onlyUnaddable
                ? i18nT('apps.awsControl.console.library_not_pushable')
                : i18nT('apps.awsControl.console.library_add_nothing')}
            </p>
          )}
        </div>
      )}

      {slugs.length > 0 && mode === 'grid' && (
        <div ref={gridRef} className="-mr-3" data-testid="library-grid">
          <div className="grid items-start" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
            {slugs.map((slug) => (
              <LibraryCloudCard
                key={slug}
                slug={slug}
                local={bySlug.get(slug)}
                localAnswered={localAnswered}
              />
            ))}
          </div>
        </div>
      )}

      {slugs.length > 0 && mode === 'list' && (
        <div className="rounded-md border border-border bg-card divide-y divide-border" data-testid="library-list">
          {/* A view is a way of LOOKING at this folder, not a capability tier --
              the same rule the Files grid states. So a row carries everything the
              card does: the route to the artifact, when it was added, and above
              all the stale-version warning. That warning is the card's whole
              contract (the preview is rendered from the LOCAL copy, so it is not
              always what the bucket holds), and because the view choice PERSISTS
              per section, leaving it out of rows meant a reader who once switched
              to the list silently never saw that disclosure again. */}
          {slugs.map((slug) => {
            const local = bySlug.get(slug)
            const stale = local && local.pushedVersion !== null && local.pushedVersion !== local.version
            const RowInner = (
              <>
                <FileText size={14} className="shrink-0 text-muted" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-text">{local?.name || slug}</span>
                {stale && (
                  <span className="min-w-0 text-[12px] text-warn" data-testid="library-list-stale">
                    {/* NOT shrink-0. This is the longest string in the row, and
                        at 320px a non-shrinking copy of it pushed itself and the
                        badge past the viewport edge. It has to be able to shrink
                        and wrap -- and it must not truncate either, because an
                        ellipsised warning is one the reader cannot read. The row
                        wraps for the same reason, which is how the grid card
                        already handles this string.

                        The GRID copy says "preview shows your newer local copy",
                        which is true of a card and false of a row: a row has no
                        preview to be wrong. Same fact, worded for what the
                        reader is actually looking at. */}
                    {i18nT('apps.awsControl.console.library_stale_list', { version: local.pushedVersion })}
                  </span>
                )}
                {local?.pushedAt && (
                  <span className="hidden shrink-0 text-[12px] text-muted lg:inline">
                    {i18nT('apps.awsControl.console.library_added', { when: fmtRelative(local.pushedAt) })}
                  </span>
                )}
                {local ? (
                  <Badge variant="muted">{i18nT(KIND_LABEL_KEY[local.kind])}</Badge>
                ) : localAnswered ? (
                  <span className="text-[12px] text-muted">{i18nT('apps.awsControl.console.library_cloud_only')}</span>
                ) : null}
                {local && <span className="hidden shrink-0 font-mono text-[12px] text-muted sm:inline">v{local.pushedVersion ?? local.version}</span>}
              </>
            )
            /* flex-wrap, so at 320px the stale warning drops to its own line
               instead of shoving the badge past the viewport edge. At any
               normal width nothing wraps and the row is unchanged. */
            const ROW = 'flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-[13px]'
            /* Same split as the cards: only a row with a local copy behind it has
               an artifact page to open, so a cloud-only row stays inert rather
               than linking somewhere that would 404. */
            return local ? (
              <Link
                key={slug}
                to={`/artifacts/${slug}`}
                aria-label={i18nT('apps.awsControl.console.library_open', { name: local.name || slug })}
                className={`${ROW} transition-colors hover:bg-bg-hover`}
                data-testid="library-list-row"
              >
                {RowInner}
              </Link>
            ) : (
              <div key={slug} className={ROW} data-testid="library-list-row">
                {RowInner}
              </div>
            )
          })}
        </div>
      )}

      {listQ.hasNextPage && (
        <div className="mt-2">
          <Btn
            onClick={() => listQ.fetchNextPage()}
            disabled={listQ.isFetchingNextPage}
            data-testid="library-load-more"
          >
            {i18nT('apps.awsControl.console.load_more')}
          </Btn>
        </div>
      )}

      <CliDrawer bucket={bucket} prefix="artifacts/" />

      {picking && <AddFromArtifactsDialog account={account} onClose={() => setPicking(false)} />}
    </section>
  )
}

/**
 * The confirm for one grid tile, rendered by that tile.
 *
 * Not one strip above the grid: that names a single item while sitting next to
 * every other one -- the same trap the table rows avoid by making the confirm
 * their own next row -- and in a scrolled folder it paints off-screen, so the
 * menu click reads as a no-op while a live destructive control sits parked out
 * of sight.
 */
function TileConfirm({ label, error, pending, onCancel, onConfirm, action }: {
  label: string
  error: string
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
  action: string
}) {
  return (
    <div className="mt-1 w-full border-t border-border pt-2" data-testid="drive-grid-confirm">
      <p className="mb-2 text-[12px] leading-snug text-text">{label}</p>
      {error && <p className="mb-2 text-[11px] text-danger" data-testid="drive-grid-confirm-error">{error}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Btn onClick={onCancel} data-testid="drive-grid-confirm-cancel">
          {i18nT('apps.awsControl.console.cancel')}
        </Btn>
        <Btn danger disabled={pending} onClick={onConfirm} data-testid="drive-grid-confirm-action">
          <Trash2 size={13} />{action}
        </Btn>
      </div>
    </div>
  )
}

/** One artifact that IS in the cloud, as a preview card. */
function LibraryCloudCard({ slug, local, localAnswered }: {
  slug: string
  local: LibraryArtifact | undefined
  localAnswered: boolean
}) {
  /* With no local artifact behind it the name IS the slug, so printing both puts
     the same string on the card twice. */
  const title = local?.name || slug
  const showSlug = title !== slug
  const body = (
    <>
      {/* The preview must not eat the click: with the card now a link, letting
          pointer events through is what makes the whole tile clickable. */}
      <div className="pointer-events-none">
        {local ? (
          <ArtifactPreview slug={slug} kind={local.kind} />
        ) : localAnswered ? (
          <OrphanThumb />
        ) : (
          /* Not yet known to be cloud-only: say nothing rather than assert it. */
          <div className="h-[120px] bg-bg-elevated" />
        )}
      </div>
      <div className="p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13px] font-medium text-text-strong">{title}</div>
            {showSlug && <code className="text-[11px] text-muted">{slug}</code>}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {local && <Badge variant="muted">{i18nT(KIND_LABEL_KEY[local.kind])}</Badge>}
          </div>
        </div>
        {/* Only a card with a local copy gets a "where it is" footer. An orphan's
            thumb ALREADY says "in the cloud only", and adding "In the drive"
            underneath made the card contradict itself -- cloud and drive are the
            same place to a reader, so the card's one job read as two answers. */}
        {local && (
          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted">
            <span>
              {local.pushedAt
                ? i18nT('apps.awsControl.console.library_added', { when: fmtRelative(local.pushedAt) })
                : i18nT('apps.awsControl.console.library_in_cloud')}
            </span>
            {/* The preview above is rendered from the LOCAL artifact, because that
                is the only copy we can read without presigning and fetching the
                object. When the local copy has been edited since the push, that
                preview is NOT what the bucket holds -- and this card's whole
                contract is that it shows what IS in the cloud. So say which
                version is stored rather than letting the picture imply it. */}
            {local.pushedVersion !== null && local.pushedVersion !== local.version && (
              <span className="text-warn" data-testid="library-card-stale">
                {i18nT('apps.awsControl.console.library_stale_preview', { version: local.pushedVersion })}
              </span>
            )}
          </div>
        )}
      </div>
    </>
  )

  const SHELL = 'mb-3 mr-3 block overflow-hidden rounded-lg border border-border bg-card'
  /* A card backed by a local copy has somewhere to go: the artifact's own page.
     A cloud-only card does NOT -- there is no local artifact to open -- so it
     stays inert rather than offering a link that would 404, and it keeps the flat
     border, because a hover affordance on a card that cannot be opened promises
     an interaction that does not exist.

     A real <Link> rather than a click handler: middle-click, cmd-click and
     keyboard activation all come for free, and there is no nested-control
     hijack to guard against. */
  if (!local) {
    return (
      <div className={SHELL} data-testid="library-card">
        {body}
      </div>
    )
  }
  return (
    <Link
      to={`/artifacts/${slug}`}
      aria-label={i18nT('apps.awsControl.console.library_open', { name: title })}
      className={`${SHELL} transition-colors hover:border-border-strong hover:bg-bg-hover`}
      data-testid="library-card"
    >
      {body}
    </Link>
  )
}

/**
 * The picker: the local artifact library, with an action that copies one INTO
 * the drive.
 *
 * This is where the local artifacts went when the Library folder stopped
 * listing them. It is the drive's Upload equivalent, so it lives behind a
 * button rather than occupying a folder: a reader browsing the drive is looking
 * at what they have stored, and a list of candidates for storage is a different
 * question that they ask deliberately.
 */
function AddFromArtifactsDialog({ account, onClose }: { account: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [kind, setKind] = useState<ArtifactKind | 'all'>('all')
  const [q, setQ] = useState('')
  const [gridRef, cols] = useColumnCount(258)
  const backdropDown = useRef(false)

  /**
   * Escape, the Tab ring, the IME claim ordering and focus restore all come from
   * the shared hook.
   *
   * This dialog originally hand-rolled all four, and that was wrong twice over:
   * `useDialogFocusTrap` exists precisely so no dialog re-implements them, and
   * the copy had already drifted -- its focusable selector used a bare `select`,
   * omitted `summary`, and lacked the hook's `offsetParent` visibility filter, so
   * hidden controls were trappable in this one dialog and nowhere else. Reaching
   * for `useDocumentImeLatch` out of the same module while missing the hook next
   * to it is what made the drift invisible.
   */
  const panelRef = useRef<HTMLDivElement>(null)
  useDialogFocusTrap(panelRef, onClose)

  const libQ = useQuery({
    queryKey: ['aws-control', 'library', account],
    queryFn: () => awsControlApi.library(account),
  })
  /**
   * Adds are tracked per slug, and deliberately NOT through `useMutation`.
   *
   * A `useMutation` observes ONE mutation at a time. Reading state off it
   * (`variables === slug`) meant a second Add reassigned the first card's
   * "Adding..."; keying that state by slug fixes the labels but NOT the outcome,
   * because starting a second add makes the observer stop tracking the first --
   * so the first add's rejection never reaches the hook's `onError` at all, and
   * a failed add stays invisible however carefully the label is keyed. Bulk
   * adding is this dialog's whole job (its own empty state advertises a count),
   * so two adds in flight is the common path, not an edge.
   *
   * Awaiting each add on its own is what actually makes one add's outcome
   * independent of every other's.
   */
  const [addingSlugs, setAddingSlugs] = useState<ReadonlySet<string>>(new Set())
  const [failedSlugs, setFailedSlugs] = useState<ReadonlySet<string>>(new Set())
  const withoutSlug = (set: ReadonlySet<string>, slug: string) => {
    const next = new Set(set)
    next.delete(slug)
    return next
  }
  const addOne = async (slug: string) => {
    // A retry clears the previous failure, so one card never shows both states.
    setAddingSlugs((prev) => new Set(prev).add(slug))
    setFailedSlugs((prev) => withoutSlug(prev, slug))
    try {
      await awsControlApi.libraryPush(account, slug)
      // Both keys: the ledger changed (so the picker's rows restate their state)
      // and the PREFIX changed (so the folder behind this dialog has a new object
      // in it). Invalidating only the library key left the folder stale until a
      // remount.
      qc.invalidateQueries({ queryKey: ['aws-control', 'library', account] })
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })
    } catch {
      setFailedSlugs((prev) => new Set(prev).add(slug))
    } finally {
      setAddingSlugs((prev) => withoutSlug(prev, slug))
    }
  }

  const artifacts = libQ.data?.artifacts ?? []
  const counts: Record<string, number> = { all: artifacts.length }
  for (const k of KIND_KEYS) counts[k] = artifacts.filter((a) => a.kind === k).length
  const needle = q.trim().toLowerCase()
  const shown = artifacts
    .filter((a) => (kind === 'all' ? true : a.kind === kind))
    .filter((a) => (needle ? a.name.toLowerCase().includes(needle) || a.slug.includes(needle) : true))

  return (
    // The SCRIM is presentational and owns click-to-dismiss; the panel inside it
    // is the dialog. Putting the dialog role and the mouse handlers on one
    // element made a non-interactive element carry mouse listeners with no
    // keyboard path of its own, and a scrim keydown handler is unreachable
    // anyway because focus never lands there -- Escape (above) is the keyboard
    // route. Same shape as UpdateFoundModal.
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-4 sm:p-8"
      data-testid="library-add-dialog"
      role="presentation"
      /* Dismiss only when the press both started and ended on the scrim: a drag
         that begins on a card and releases outside it is not a request to close,
         and treating it as one loses whatever the reader was doing. */
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) onClose(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-border bg-card shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('apps.awsControl.console.library_add')}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold text-text-strong">
            {i18nT('apps.awsControl.console.library_add')}
          </h3>
          <button
            onClick={onClose}
            className="cursor-pointer border-none bg-transparent p-0 text-muted hover:text-text"
            aria-label={i18nT('apps.awsControl.console.close')}
            data-testid="library-add-close"
          >
            <X size={16} />
          </button>
        </div>

        {/* The disclosure has to be HERE, where the adding happens.
            Adding fills storage the user pays for and there is no in-app
            removal yet, and the folder's own hint was the only place that said
            so -- which the empty-state button skips entirely, opening this
            dialog directly. A warning the reader never passes is not a warning.

            Its OWN string, not the folder's: that one ends by pointing at the
            "Where this lives" drawer, which sits behind this overlay rather than
            in it, so reusing it here would name something the reader cannot
            find -- the same dangling pointer that copy was just fixed to
            remove. */}
        <p className="border-b border-border px-4 py-2 text-[12px] leading-snug text-muted" data-testid="library-add-oneway">
          {i18nT('apps.awsControl.console.library_add_oneway')}
        </p>

        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
          {/* The cue lives on this WRAPPER, not the bare input: the visible
              control a reader sees is the whole bordered box (magnifier + field),
              so lighting the box is what reads as "the search has focus". An
              outline on the inner input alone would paint inside the border and
              leave the box itself looking inert. */}
          <div className="flex min-w-[180px] flex-1 items-center gap-2 rounded-md border border-border bg-bg px-2.5 py-1.5 focus-within:border-accent focus-within:ring-1 focus-within:ring-accent/40">
            <Search size={13} className="shrink-0 text-muted" aria-hidden="true" />
            {/* focus-cue-ok: the cue is the parent's focus-within border+ring above. */}
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={i18nT('apps.awsControl.console.library_search')}
              aria-label={i18nT('apps.awsControl.console.library_search')}
              /* Takes focus on open: otherwise focus stays on the trigger BEHIND
                 the overlay and Tab walks the occluded page (cards, Load more,
                 the CLI drawer) before reaching this dialog. It also gives mouse
                 users type-to-filter immediately. */
              autoFocus
              className="min-w-0 flex-1 border-none bg-transparent text-[13px] text-text outline-none"
              data-testid="library-add-search"
            />
          </div>
        </div>

        {/* Not while the read failed. The counts come from an empty-array
            fallback, so a failed lookup rendered "All 0 | Widget 0 | Markdown
            0..." above "Could not read your artifacts" -- a confident zero-count
            library built on nothing, which is the one answer a failure cannot
            support. Filtering is meaningless with nothing to filter anyway. */}
        {!libQ.isError && (
        <div className="flex flex-wrap gap-1.5 border-b border-border px-4 py-2.5" data-testid="library-chips">
          {(['all', ...KIND_KEYS] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              aria-pressed={kind === k}
              className={`cursor-pointer rounded-full border px-2.5 py-1 text-[12px] transition-colors ${
                kind === k
                  ? 'border-accent bg-accent/10 text-accent'
                  : 'border-border bg-transparent text-muted hover:text-text'
              }`}
              data-testid={`library-chip-${k}`}
            >
              {k === 'all' ? i18nT('apps.awsControl.console.library_all') : i18nT(KIND_LABEL_KEY[k])}{' '}
              <span className="font-mono opacity-70">{fmtNumber(counts[k] ?? 0)}</span>
            </button>
          ))}
        </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {libQ.isLoading && <ContentSkeleton rows={3} />}
          {/* A failed read of your own artifacts is not an empty library. Without
              this the picker's body rendered blank -- no skeleton, no message, no
              way back -- which reads as "you have nothing to add", the one
              conclusion a failure cannot support. Same mistake the cloud listing
              beside it already guards against. */}
          {libQ.isError && (
            <div className="py-6 text-center" data-testid="library-add-error">
              <p className="mb-3 text-[13px] text-text">{i18nT('apps.awsControl.console.library_local_failed')}</p>
              <Btn onClick={() => libQ.refetch()} data-testid="library-add-retry">
                <RefreshCw size={13} />
                {i18nT('apps.awsControl.console.retry')}
              </Btn>
            </div>
          )}
          {libQ.data && shown.length === 0 && (
            <p className="py-6 text-center text-[13px] text-muted" data-testid="library-add-none">
              {i18nT('apps.awsControl.console.library_add_none')}
            </p>
          )}
          {shown.length > 0 && (
            <div ref={gridRef} className="-mr-3">
              <div className="grid items-start" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
                {shown.map((a) => (
                  <PickerCard
                    key={a.slug}
                    artifact={a}
                    onPush={() => { void addOne(a.slug) }}
                    pushing={addingSlugs.has(a.slug)}
                    failed={failedSlugs.has(a.slug)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** One candidate in the picker: a real preview, and one action. */
function PickerCard({ artifact, onPush, pushing, failed }: {
  artifact: LibraryArtifact
  onPush: () => void
  pushing: boolean
  failed: boolean
}) {
  const synced = artifact.pushedVersion !== null
  const upToDate = artifact.pushedVersion === artifact.version
  /* An image cannot be pushed yet: the backend's kind -> extension map carries
     no image entry, so `push_artifact` refuses one. The card SAYS that rather
     than only grey out its button, because images are the bulk of a real
     library and a disabled control with no reason reads as a bug. */
  const notPushable = artifact.kind === 'image'
  return (
    <div className="mb-3 mr-3 overflow-hidden rounded-lg border border-border bg-card" data-testid="library-tile">
      <div className="pointer-events-none">
        <ArtifactPreview slug={artifact.slug} kind={artifact.kind} />
      </div>
      <div className="p-3">
        <div className="flex items-start justify-between gap-2">
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-text-strong">{artifact.name}</span>
          <Badge variant="muted">{i18nT(KIND_LABEL_KEY[artifact.kind])}</Badge>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-muted">
          <span className="font-mono">v{artifact.version}</span>
          <span>{fmtRelative(artifact.updatedAt)}</span>
        </div>
        {notPushable && (
          <p className="mt-2 text-[11px] leading-snug text-muted" data-testid="library-not-pushable">
            {i18nT('apps.awsControl.console.library_not_pushable')}
          </p>
        )}
        {failed && !notPushable && (
          <p className="mt-2 text-[11px] leading-snug text-danger" data-testid="library-push-error">
            {i18nT('apps.awsControl.console.library_push_failed')}
          </p>
        )}
        {!notPushable && (
          <div className="mt-2.5">
            {/* Sync state is now a LABEL, not a locked door. The ledger is local,
                so a cloud object deleted outside this app -- the S3 console, a
                lifecycle rule, another machine -- leaves it still claiming the
                version matches. Disabling on `upToDate` then removed the only way
                to put the copy back, and this page makes that contradiction
                visible for the first time: the Library folder lists the real
                prefix, so it shows the object GONE while the picker insisted it
                was up to date. A same-version push is idempotent (it rewrites the
                same key plus its sidecar), so the worst case is one redundant
                upload and the best case is recovering a copy you cannot
                otherwise restore. */}
            {upToDate && (
              <p className="mb-1.5 text-[11px] text-muted" data-testid="library-already">
                {i18nT('apps.awsControl.console.library_in_cloud')}
              </p>
            )}
            <Btn onClick={onPush} disabled={pushing} data-testid="library-push">
              <Upload size={13} />
              {pushing
                ? i18nT('apps.awsControl.console.library_adding')
                : upToDate
                  ? i18nT('apps.awsControl.console.library_add_again')
                  : synced
                    ? i18nT('apps.awsControl.console.library_update')
                    : i18nT('apps.awsControl.console.library_add_one')}
            </Btn>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Section 5: Drive (folder browser) ───────────────────────────────────── */

/** Client-side key-segment validation, matching the backend's charset rule. */
/**
 * The drive's columns.
 *
 * An S3 object has no slug, source, version or tags, so the artifact library's
 * nine columns cannot be reused as they are - these four plus the pinned Actions
 * cell are what a stored object actually has. None is sortable (see the head
 * call), which is why every `key` is empty.
 */
const DRIVE_COLUMNS: LibraryColumn[] = [
  { key: '', label: 'apps.awsControl.console.col_name', className: 'min-w-[200px]' },
  { key: '', label: 'apps.awsControl.console.col_kind', className: 'w-[110px]' },
  { key: '', label: 'apps.awsControl.console.col_size', className: 'w-[90px]' },
  { key: '', label: 'apps.awsControl.console.col_modified', className: 'w-[120px]' },
]

/**
 * The Kind cell for a stored object: its extension, upper-cased.
 *
 * NOT the shared `docFileType`, which answers only 'markdown' or 'text' because
 * it classifies session DOCUMENTS - it labelled a .pdf and an .mp4 'markdown'.
 * An S3 object can be anything, and the extension is the only kind information
 * `ListObjectsV2` actually returns, so it is what the column shows. A key with
 * no extension gets a dash rather than an invented category.
 */
function objectKind(key: string): string {
  const name = key.split('/').pop() ?? key
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || dot === name.length - 1) return '-'
  return name.slice(dot + 1).toUpperCase()
}

/**
 * Did this event start inside a control NESTED in the clickable card?
 *
 * A card that is itself a control but also holds buttons (an overflow trigger, a
 * confirm's Cancel and Delete) sees their events bubble up. Acting on them opens
 * the folder the reader was trying to act WITHIN, and on the keyboard path the
 * card's own `preventDefault` would additionally cancel the nested control's
 * activation -- so the menu and the confirm would stop answering the keyboard at
 * all. `role="button"` is deliberately NOT in the selector: that is what the card
 * itself carries, and matching it would make every event look nested.
 */
function fromNestedControl(e: React.SyntheticEvent): boolean {
  const target = e.target as HTMLElement | null
  if (!target || target === e.currentTarget) return false
  return !!target.closest('button, a, input, select, textarea, [role="menuitem"]')
}

/** No column is sortable, so the shared head never calls this. */
const noSort = () => {}

const KEY_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9 ._()+@=-]*$/

function DriveSectionView({ account, bucket }: { account: string; bucket: string }) {
  const qc = useQueryClient()
  const [mode, setMode] = useViewMode('drive', 'list')
  const [path, setPath] = useState('')
  const [share, setShare] = useState<{ key: string } | null>(null)
  const [uploadError, setUploadError] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const [crumbMenu, setCrumbMenu] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [confirmFolder, setConfirmFolder] = useState<string | null>(null)
  const [newFolder, setNewFolder] = useState('')
  const [folderError, setFolderError] = useState('')
  /* How many objects the last folder delete actually removed. One click can
     remove far more than one file, and the count is only knowable AFTER the
     fact - the response carries it, while a figure shown BEFORE consent would
     cost a second full recursive listing of the prefix. So the page reports
     what was removed rather than pretending to predict it. */
  const [deletedCount, setDeletedCount] = useState<number | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const [filesGridRef, fileCols] = useColumnCount(258)
  /* The pinned Actions cell paints its seam only when the table actually
     overflows, so the edge is measured rather than assumed. */
  const [attachScroller, edges] = useScrollEdges<HTMLDivElement>()

  /* One listing per PATH, ACCUMULATED across pages -- the same shape Library
     uses above. A continuation token is a page OF one listing, not a different
     listing, so it belongs in the page params and never in the query key:
     keying by token would make every "Load more" a brand-new query that
     REPLACES the rows already on screen. Navigation resets fall out of the key itself (a
     new path is a new query), and the explicit 'drive' segment keeps a folder
     literally named "library" from colliding with the Library section's own
     ['aws-control','drive',account,'list','library'] key. The key stays under
     the ['aws-control','drive',account] prefix every mutation invalidates, and
     invalidating an infinite query refetches every page it holds, so a deep
     list survives an upload or delete instead of collapsing to page one. */
  const listQ = useInfiniteQuery({
    queryKey: ['aws-control', 'drive', account, 'list', 'drive', path],
    queryFn: ({ pageParam }) => awsControlApi.driveList(account, 'drive', path, pageParam),
    initialPageParam: '',
    getNextPageParam: (last) => last.nextToken || undefined,
  })
  /* Every fetched page's rows, in arrival order: the table and the grid render
     from these, so page boundaries stay invisible to the reader. */
  const folders = (listQ.data?.pages ?? []).flatMap((pg) => pg.folders)
  const files = (listQ.data?.pages ?? []).flatMap((pg) => pg.files)
  const invalidate = () => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })

  const uploadMut = useMutation({
    mutationFn: (file: File) =>
      awsControlApi.driveUpload(account, 'drive', path ? `${path}/${file.name}` : file.name, file),
    onSuccess: invalidate,
  })
  const deleteMut = useMutation({
    mutationFn: (key: string) => awsControlApi.driveDelete(account, 'drive', key),
    onSuccess: invalidate,
  })
  const folderCreateMut = useMutation({
    mutationFn: (name: string) =>
      awsControlApi.driveFolderCreate(account, 'drive', path ? `${path}/${name}` : name),
    onSuccess: () => { setNewFolder(''); invalidate() },
  })
  const folderDeleteMut = useMutation({
    mutationFn: (folder: string) => awsControlApi.driveFolderDelete(account, 'drive', folder),
    onSuccess: (res) => { setDeletedCount(res.objects); invalidate() },
  })

  const onCreateFolder = () => {
    const name = newFolder.trim()
    setFolderError('')
    if (!name) return
    // Same segment rule an uploaded file name is held to: the backend runs the
    // path through the key validator every object key goes through, and
    // checking here means the reader is told which character is the problem
    // instead of reading a 400.
    if (!KEY_SEGMENT.test(name)) {
      // Its own message: the shared one names a FILE, and the reader just typed
      // a folder name.
      setFolderError(i18nT('apps.awsControl.console.folder_bad_name'))
      return
    }
    folderCreateMut.mutate(name)
  }

  const onPick = (file: File | undefined) => {
    if (!file) return
    setUploadError('')
    if (!KEY_SEGMENT.test(file.name)) {
      setUploadError(i18nT('apps.awsControl.console.drive_bad_name'))
      return
    }
    uploadMut.mutate(file)
  }

  const download = async (key: string) => {
    // Open the tab SYNCHRONOUSLY, inside the click's user activation, then
    // navigate it once the presign returns. Awaiting first and calling
    // window.open afterwards spends the activation on the await, and Safari
    // (and Chrome, with popups restricted) blocks the resulting window - the
    // Download button silently does nothing.
    //
    // Deliberately NO 'noopener' feature here: per the HTML standard a
    // window.open carrying it returns NULL, which made the earlier version of
    // this fix a no-op -- the handle was always null, so every download fell
    // through to the post-await open it was written to avoid, and the test that
    // covered it passed only because it MOCKED window.open into returning a
    // tab. The isolation noopener buys is restored on the next line by nulling
    // `opener` on the window we just got: same guarantee, handle kept.
    setDownloadError('')
    const tab = window.open('', '_blank')
    if (tab) tab.opener = null
    try {
      const { url } = await awsControlApi.driveDownload(account, 'drive', key)
      if (tab) tab.location.href = url
      else window.open(url, '_blank', 'noopener')
    } catch {
      // Never leave an orphaned blank tab behind, and never rethrow: this runs
      // from an onClick with no catch, so a rethrow becomes an unhandled
      // rejection that tells the USER nothing. Report it in the row instead.
      tab?.close()
      setDownloadError(i18nT('apps.awsControl.console.download_failed'))
    }
  }

  const crumbs = path.split('/').filter(Boolean)

  return (
    <section data-testid="drive-section">
      <SectionHeader icon={<FolderClosed size={15} />} title={i18nT('apps.awsControl.console.section_files')} actions={
        <div className="flex flex-wrap items-center gap-2">
        <ViewModeToggle section="drive" mode={mode} onChange={setMode} />
        <Input
          value={newFolder}
          onChange={(e) => setNewFolder(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') onCreateFolder() }}
          placeholder={i18nT('apps.awsControl.console.folder_name')}
          aria-label={i18nT('apps.awsControl.console.folder_new')}
          className="w-full min-w-0 basis-full sm:w-[160px] sm:flex-none sm:basis-auto"
          data-testid="drive-folder-name"
        />
        <Btn onClick={onCreateFolder} disabled={folderCreateMut.isPending || !newFolder.trim()} data-testid="drive-folder-create">
          <FolderPlus size={13} />
          {i18nT('apps.awsControl.console.folder_new')}
        </Btn>
        <Btn onClick={() => fileRef.current?.click()} disabled={uploadMut.isPending} data-testid="drive-upload-btn">
          <Upload size={13} />
          {uploadMut.isPending ? i18nT('apps.awsControl.console.drive_uploading') : i18nT('apps.awsControl.console.drive_upload')}
        </Btn>
        </div>
      } />
      <input
        ref={fileRef}
        type="file"
        className="hidden"
        aria-label={i18nT('apps.awsControl.console.drive_upload')}
        data-testid="drive-file-input"
        onChange={(e) => onPick(e.target.files?.[0])}
      />

      {uploadError && <p className="mb-2 text-[12px] text-danger" data-testid="drive-upload-error">{uploadError}</p>}
      {folderError && <p className="mb-2 text-[12px] text-danger" data-testid="drive-folder-error">{folderError}</p>}
      {folderCreateMut.isError && <p className="mb-2 text-[12px] text-danger" data-testid="drive-folder-create-error">{i18nT('apps.awsControl.console.folder_create_failed')}</p>}
      {deletedCount !== null && (
        <p className="mb-2 text-[12px] text-muted" data-testid="drive-folder-deleted">
          {i18nT('apps.awsControl.console.folder_deleted', { objects: deletedCount })}
        </p>
      )}
      {downloadError && <p className="mb-2 text-[12px] text-danger" data-testid="drive-download-error">{downloadError}</p>}

      {/* Breadcrumb within the section. Root plus one overflow is two sibling
          controls; the folder you are IN is text, not a third button. The
          ancestors go into the same inline overflow the file rows use, which
          keeps the jump-to-an-ancestor navigation that rendering the whole path
          as flat text would have removed. */}
      {crumbs.length > 0 && (
      <div className="mb-2 flex flex-wrap items-center gap-1 text-[12px] text-muted" data-testid="drive-crumbs">
        <button className="hover:text-text cursor-pointer bg-transparent border-none p-0" onClick={() => setPath('')}>
          {i18nT('apps.awsControl.console.section_files')}
        </button>
        {crumbs.length > 1 && (
          <span className="relative flex items-center gap-1">
            {' / '}
            <IconButton
              aria-label={i18nT('apps.awsControl.console.parent_folders')}
              onClick={() => setCrumbMenu((v) => !v)}
              data-testid="drive-crumb-more"
            >
              <MoreHorizontal size={14} />
            </IconButton>
            {crumbMenu && (
              <div className="absolute left-0 top-full z-10 mt-1 flex flex-col gap-1 rounded-md border border-border bg-card p-1 shadow-md" data-testid="drive-crumb-menu">
                {crumbs.slice(0, -1).map((c, i) => (
                  <Btn
                    key={i}
                    onClick={() => {
                      setCrumbMenu(false)
                      setPath(crumbs.slice(0, i + 1).join('/'))
                    }}
                  >
                    {c}
                  </Btn>
                ))}
              </div>
            )}
          </span>
        )}
        <span data-testid="drive-crumb-current">{' / '}{crumbs[crumbs.length - 1]}</span>
      </div>
      )}

      {listQ.isLoading && <ContentSkeleton rows={2} />}

      {/* Empty, and said properly. "This folder is empty." inside a table with
          five headers left a reader who had just come from a Library folder
          holding 212 rows unable to tell what the two folders were FOR -- the
          question was asked in exactly those words. So the empty state names
          what belongs here and how it differs from Library, and carries the
          upload action rather than making the reader find it in the header. */}
      {listQ.isSuccess && folders.length === 0 && files.length === 0 && (
        <div className="rounded-lg border border-dashed border-border p-8 text-center" data-testid="drive-empty">
          <div className="mb-1.5 text-[13px] font-medium text-text-strong">
            {i18nT('apps.awsControl.console.files_empty_title')}
          </div>
          <p className="mx-auto mb-4 max-w-[56ch] text-[12px] leading-relaxed text-muted">
            {i18nT('apps.awsControl.console.files_empty_body')}
          </p>
          <Btn primary onClick={() => fileRef.current?.click()} data-testid="drive-empty-upload">
            <Upload size={13} />
            {i18nT('apps.awsControl.console.drive_upload')}
          </Btn>
        </div>
      )}

      {/* Grid mode. A stored object has no preview we can draw without a presign
          and a fetch PER CARD, so a tile is a type glyph, its name, and its size
          -- the same thing a file manager shows for a format it cannot render.
          Every action a LIST row carries is carried here too: the view mode is a
          way of LOOKING at a folder, not a capability tier, and because the
          choice persists per section a reader who preferred tiles would
          otherwise lose Share and Delete on every future visit with nothing to
          tell them the controls existed. */}
      {mode === 'grid' && (folders.length > 0 || files.length > 0) && (
        <div ref={filesGridRef} className="-mr-3" data-testid="drive-grid">
          <div className="grid items-start" style={{ gridTemplateColumns: `repeat(${fileCols}, minmax(0, 1fr))` }}>
            {folders.map((name) => {
              const open = () => { setPath(name); setDeletedCount(null) }
              return (
              <div
                key={`gf-${name}`}
                role="button"
                tabIndex={0}
                onClick={(e) => { if (fromNestedControl(e)) return; open() }}
                onKeyDown={(e) => {
                  if (e.key !== 'Enter' && e.key !== ' ') return
                  /* An Enter or Space that belongs to a control INSIDE the tile is
                     not a request to open the folder, and swallowing it here is
                     worse than the stray navigation: preventDefault would also
                     cancel that control's own activation, so the overflow menu and
                     the confirm's Cancel / Delete would stop responding to the
                     keyboard entirely. stopPropagation on the trigger's onClick
                     only ever covered the pointer path. */
                  if (fromNestedControl(e)) return
                  e.preventDefault()
                  open()
                }}
                aria-label={i18nT('apps.awsControl.console.folder_open', { name: name.split('/').pop() ?? name })}
                className="mb-3 mr-3 flex cursor-pointer flex-col items-start gap-2 rounded-lg border border-border bg-card p-3 text-left transition-colors hover:border-border-strong hover:bg-bg-hover"
                data-testid="drive-grid-folder"
              >
                <div className="flex w-full items-start justify-between gap-2">
                  <FolderClosed size={22} className="text-accent" aria-hidden="true" />
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button
                        type="button"
                        onClick={(e) => e.stopPropagation()}
                        className="cursor-pointer rounded border-none bg-transparent p-1 text-muted transition-colors hover:text-text"
                        aria-label={i18nT('apps.awsControl.console.folder_actions')}
                        data-testid="drive-grid-folder-more"
                      >
                        <MoreHorizontal size={14} />
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => setConfirmFolder(name)} data-testid="drive-grid-folder-delete">
                        <Trash2 size={13} />{i18nT('apps.awsControl.console.folder_delete_action')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                <span className="w-full truncate text-[13px] font-medium text-text-strong">
                  {name.split('/').pop()}
                </span>
                <span className="text-[11px] text-muted">{i18nT('apps.awsControl.console.kind_folder')}</span>
                {confirmFolder === name && (
                  <TileConfirm
                    label={i18nT('apps.awsControl.console.folder_delete_confirm', { name: name.split('/').pop() ?? name })}
                    error={folderDeleteMut.isError ? i18nT('apps.awsControl.console.folder_delete_failed') : ''}
                    pending={folderDeleteMut.isPending}
                    onCancel={() => setConfirmFolder(null)}
                    onConfirm={() => folderDeleteMut.mutate(name, { onSuccess: () => setConfirmFolder(null) })}
                    action={i18nT('apps.awsControl.console.folder_delete_action')}
                  />
                )}
              </div>
              )
            })}
            {files.map((f) => (
              <div
                key={`go-${f.key}`}
                /* No hover lift. A file card's actions live in its overflow menu;
                   the card body itself does nothing, and lighting its border on
                   hover promises a click that does not exist -- the same rule the
                   cloud-only Library card follows, which this was contradicting.
                   The folder tile beside it keeps its hover because it IS
                   clickable. */
                className="mb-3 mr-3 flex flex-col items-start gap-2 rounded-lg border border-border bg-card p-3"
                data-testid="drive-grid-file"
              >
                <div className="flex w-full items-start justify-between gap-2">
                  <FileText size={22} className="text-muted" aria-hidden="true" />
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => download(f.key)}
                      className="cursor-pointer rounded border-none bg-transparent p-1 text-muted transition-colors hover:text-text"
                      title={i18nT('apps.awsControl.console.download')}
                      aria-label={i18nT('apps.awsControl.console.download')}
                      data-testid="drive-grid-download"
                    >
                      <Download size={13} />
                    </button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          className="cursor-pointer rounded border-none bg-transparent p-1 text-muted transition-colors hover:text-text"
                          aria-label={i18nT('apps.awsControl.console.file_actions')}
                          data-testid="drive-grid-more"
                        >
                          <MoreHorizontal size={14} />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onSelect={() => setShare({ key: f.key })} data-testid="drive-grid-share">
                          <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                        </DropdownMenuItem>
                        <DropdownMenuItem onSelect={() => setConfirmDelete(f.key)} data-testid="drive-grid-delete">
                          <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
                <span className="w-full truncate text-[13px] font-medium text-text-strong">{f.key.split('/').pop()}</span>
                <span className="text-[11px] text-muted">
                  {/* A dash is the ABSENCE of a kind, not a kind -- do not print
                      it as one beside the size. */}
                  {objectKind(f.key) === '-' ? fmtBytes(f.size) : `${objectKind(f.key)} · ${fmtBytes(f.size)}`}
                </span>
                {confirmDelete === f.key && (
                  <TileConfirm
                    label={i18nT('apps.awsControl.console.delete_confirm', { name: f.key.split('/').pop() ?? f.key })}
                    error={deleteMut.isError ? i18nT('apps.awsControl.console.delete_failed') : ''}
                    pending={deleteMut.isPending}
                    onCancel={() => setConfirmDelete(null)}
                    onConfirm={() => deleteMut.mutate(f.key, { onSuccess: () => setConfirmDelete(null) })}
                    action={i18nT('apps.awsControl.console.delete_confirm_action')}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {mode === 'list' && (folders.length > 0 || files.length > 0) && (
        <div ref={attachScroller} className="overflow-x-auto rounded-md border border-border bg-card" data-testid="drive-listing">
          <table className="w-full border-collapse text-[13px]">
            {/* Shared head, drive columns. No column is sortable and `sort` is
                null on purpose: the listing is paged server-side and S3 returns
                keys in lexicographic order only, so a client-side sort would
                reorder just the page already loaded while the rest of the
                folder stayed where it was - a control that looks global and is
                not. Folders sort before files, which the render order does. */}
            <LibraryTableHead
              sort={null}
              onSort={noSort}
              edgeRight={edges.right}
              columns={DRIVE_COLUMNS}
              actionsLabelKey="apps.awsControl.console.col_actions"
            />
            <tbody>
              {folders.map((name) => (
                /* The WHOLE row opens the folder, which is both what the
                   artifact table's own folder row does (onClick on the <tr>)
                   and what a file browser is expected to do - when only the
                   name text carried the handler, the Kind, Size and Modified
                   cells and all the empty space in between were dead. The inner
                   button stays as the real focusable control so the row is
                   still reachable and operable from the keyboard. */
                <Fragment key={`f-${name}`}>
                <tr
                  onClick={() => { setPath(name); setDeletedCount(null) }}
                  className="cursor-pointer border-b border-border last:border-0 hover:bg-bg-hover"
                  data-testid="drive-folder"
                >
                  <td className="px-2.5 py-2">
                    <button
                      onClick={(e) => { e.stopPropagation(); setPath(name) }}
                      className="flex min-w-0 items-center gap-2 text-left text-text cursor-pointer bg-transparent border-none p-0"
                      data-testid="drive-folder-open"
                    >
                      <FolderClosed size={14} className="shrink-0 text-muted" />
                      <span className="truncate">{name.split('/').pop()}</span>
                    </button>
                  </td>
                  <td className="px-2.5 py-2 text-muted">{i18nT('apps.awsControl.console.kind_folder')}</td>
                  <td className="px-2.5 py-2 text-muted">-</td>
                  <td className="px-2.5 py-2 text-muted">-</td>
                  <td className="sticky right-0 bg-card px-2.5 py-2">
                    {/* The seam is spelled exactly as the shared rows spell it:
                        a 1px child div plus a `right-full` gradient, both gated
                        on the measured overflow. Not `border-l` (under
                        `border-collapse: collapse` a border paints at the cell's
                        layout slot and stays behind the scrolling columns), and
                        not a box-shadow either - a third spelling of the same
                        seam is how the two drift apart, which is the whole
                        reason the head is shared rather than copied. */}
                    {edges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                    {edges.right && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
                    {/* One overflow trigger, and the menu comes from
                        `ui/dropdown-menu`, which portals its content to the body
                        - a hand-rolled `absolute` menu is CLIPPED here, because
                        the scroll container the pinned Actions column needs is
                        `overflow-x-auto` and that computes `overflow-y` to auto
                        too: the items sat in the DOM with a real box and were
                        unclickable. Same reason `CronRowActions` uses this
                        component for a row inside a scrolling table. Keeping the
                        destructive act behind the trigger also means a
                        slightly-off click on a row that OPENS on click cannot
                        land on it. */}
                    <div className="flex items-center justify-end">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <button
                            type="button"
                            onClick={(e) => e.stopPropagation()}
                            className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                            aria-label={i18nT('apps.awsControl.console.folder_actions')}
                            data-testid="drive-folder-more"
                          >
                            <MoreHorizontal size={14} />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
                          <DropdownMenuItem
                            onSelect={() => setConfirmFolder(name)}
                            data-testid="drive-folder-delete"
                          >
                            <Trash2 size={13} />{i18nT('apps.awsControl.console.folder_delete_action')}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </td>
                </tr>
                {/* The confirm belongs to THIS folder, so it renders as this
                    row's own next row. Rendered once after the whole list, it
                    appeared under the LAST folder while naming the first - and
                    that name is the only guard before an irreversible recursive
                    delete. */}
                {confirmFolder === name && (
                  <tr className="border-b border-border bg-bg-elevated" data-testid="drive-folder-delete-confirm">
                    <td colSpan={5} className="px-2.5 py-2">
                      {/* A colSpan cell is as wide as the TABLE, so at 320px
                          Cancel and Delete folder sat past the right edge and
                          needed a horizontal scroll to reach - on an
                          irreversible act. Pinned to the scroll container's left
                          edge and wrapping within the VIEWPORT instead. */}
                      <div className="sticky left-0 flex max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-2 pr-4">
                        <span className="min-w-0 flex-1 text-text">
                          {i18nT('apps.awsControl.console.folder_delete_confirm', { name: name.split('/').pop() ?? name })}
                        </span>
                        {folderDeleteMut.isError && (
                          <span className="text-danger" data-testid="drive-folder-delete-error">
                            {i18nT('apps.awsControl.console.folder_delete_failed')}
                          </span>
                        )}
                        <Btn onClick={() => setConfirmFolder(null)} data-testid="drive-folder-delete-cancel">
                          {i18nT('apps.awsControl.console.cancel')}
                        </Btn>
                        <Btn
                          danger
                          disabled={folderDeleteMut.isPending}
                          onClick={() => folderDeleteMut.mutate(name, { onSuccess: () => setConfirmFolder(null) })}
                          data-testid="drive-folder-delete-action"
                        >
                          <Trash2 size={13} />{i18nT('apps.awsControl.console.folder_delete_action')}
                        </Btn>
                      </div>
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
              {files.map((f) => (
                /* A file is TWO rows when its delete is being confirmed, so the
                   key belongs on the fragment - on the inner <tr> React has
                   nothing to reconcile the pair by. */
                <Fragment key={`o-${f.key}`}>
                  <tr className="border-b border-border last:border-0 hover:bg-bg-hover" data-testid="drive-file">
                    <td className="px-2.5 py-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <FileText size={14} className="shrink-0 text-muted" />
                        <span className="truncate text-text">{f.key.split('/').pop()}</span>
                      </div>
                    </td>
                    <td className="px-2.5 py-2 text-muted">{objectKind(f.key)}</td>
                    <td className="px-2.5 py-2 text-muted">{fmtBytes(f.size)}</td>
                    <td className="px-2.5 py-2 text-muted">{fmtRelative(f.modified)}</td>
                    <td className="sticky right-0 bg-card px-2.5 py-2">
                    {/* The seam is spelled exactly as the shared rows spell it:
                        a 1px child div plus a `right-full` gradient, both gated
                        on the measured overflow. Not `border-l` (under
                        `border-collapse: collapse` a border paints at the cell's
                        layout slot and stays behind the scrolling columns), and
                        not a box-shadow either - a third spelling of the same
                        seam is how the two drift apart, which is the whole
                        reason the head is shared rather than copied. */}
                    {edges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                    {edges.right && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
                      {/* Two controls: the one action a reader takes per
                          glance, plus one overflow for the rest. */}
                      <div className="flex items-center justify-end gap-1">
                        <button
                          type="button"
                          onClick={() => download(f.key)}
                          className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                          title={i18nT('apps.awsControl.console.download')}
                          aria-label={i18nT('apps.awsControl.console.download')}
                          data-testid="drive-download"
                        >
                          <Download size={13} />
                        </button>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <button
                              type="button"
                              className="p-1 rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none"
                              aria-label={i18nT('apps.awsControl.console.file_actions')}
                              data-testid="drive-more"
                            >
                              <MoreHorizontal size={14} />
                            </button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onSelect={() => setShare({ key: f.key })} data-testid="drive-share">
                              <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => setConfirmDelete(f.key)} data-testid="drive-delete">
                              <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </td>
                  </tr>
                  {confirmDelete === f.key && (
                    <tr className="border-b border-border bg-bg-elevated" data-testid="drive-delete-confirm">
                      <td colSpan={5} className="px-2.5 py-2">
                        {/* Same viewport pinning as the folder strip above. */}
                        <div className="sticky left-0 flex max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-2 pr-4">
                          <span className="min-w-0 flex-1 text-text">
                            {i18nT('apps.awsControl.console.delete_confirm', { name: f.key.split('/').pop() ?? f.key })}
                          </span>
                          {deleteMut.isError && (
                            <span className="text-danger" data-testid="drive-delete-error">
                              {i18nT('apps.awsControl.console.delete_failed')}
                            </span>
                          )}
                          <Btn onClick={() => setConfirmDelete(null)} data-testid="drive-delete-cancel">
                            {i18nT('apps.awsControl.console.cancel')}
                          </Btn>
                          <Btn
                            danger
                            disabled={deleteMut.isPending}
                            onClick={() => deleteMut.mutate(f.key, { onSuccess: () => setConfirmDelete(null) })}
                            data-testid="drive-delete-confirm-action"
                          >
                            <Trash2 size={13} />{i18nT('apps.awsControl.console.delete_confirm_action')}
                          </Btn>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {listQ.hasNextPage && (
        <div className="mt-2">
          <Btn
            onClick={() => listQ.fetchNextPage()}
            disabled={listQ.isFetchingNextPage}
            data-testid="drive-load-more"
          >
            {i18nT('apps.awsControl.console.load_more')}
          </Btn>
        </div>
      )}

      <CliDrawer bucket={bucket} prefix="drive/" />

      {share && (
        <ShareDialog account={account} section="drive" fileKey={share.key} onClose={() => setShare(null)} />
      )}
    </section>
  )
}

/* ── Share dialog ────────────────────────────────────────────────────────── */

const EXPIRY_OPTIONS: Array<{ key: string; secs: number }> = [
  { key: '1h', secs: 3600 },
  { key: '1d', secs: 86400 },
  { key: '7d', secs: 604800 },
]

function ShareDialog({ account, section, fileKey, onClose }: { account: string; section: DriveSection; fileKey: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [secs, setSecs] = useState(3600)
  const [note, setNote] = useState('')
  const shareMut = useMutation({
    mutationFn: () => awsControlApi.driveShare(account, section, fileKey, secs, note),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'shares', account] }),
  })
  const url = shareMut.data?.url

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" data-testid="share-dialog" role="dialog" aria-modal="true">
      <div className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-lg">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-text-strong">{i18nT('apps.awsControl.console.share_title')}</h3>
          <button onClick={onClose} className="text-muted hover:text-text cursor-pointer bg-transparent border-none p-0" aria-label={i18nT('apps.awsControl.console.close')} data-testid="share-close"><X size={16} /></button>
        </div>

        {!url ? (
          <>
            <span className="mb-1 block text-[12px] text-muted">{i18nT('apps.awsControl.console.share_expiry')}</span>
            <div className="mb-3 flex gap-1.5" data-testid="share-expiry" role="group" aria-label={i18nT('apps.awsControl.console.share_expiry')}>
              {EXPIRY_OPTIONS.map((o) => (
                <button
                  key={o.key}
                  onClick={() => setSecs(o.secs)}
                  aria-pressed={secs === o.secs}
                  className={`rounded-md border px-2.5 py-1 text-[13px] cursor-pointer transition-colors ${secs === o.secs ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-transparent text-muted hover:text-text'}`}
                  data-testid={`share-expiry-${o.key}`}
                >
                  {i18nT(EXPIRY_LABEL_KEY[o.key])}
                </button>
              ))}
            </div>
            {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
            <label htmlFor="aws-share-note" className="mb-1 block text-[12px] text-muted">{i18nT('apps.awsControl.console.share_note')}</label>
            <Input id="aws-share-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={i18nT('apps.awsControl.console.share_note_placeholder')} className="mb-3 w-full" data-testid="share-note" />
            <Btn primary onClick={() => shareMut.mutate()} disabled={shareMut.isPending} data-testid="share-create">
              {shareMut.isPending ? i18nT('apps.awsControl.console.share_creating') : i18nT('apps.awsControl.console.share_create')}
            </Btn>
          </>
        ) : (
          <div data-testid="share-result">
            <div className="mb-2 flex items-center gap-2">
              <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">{url}</code>
              <CopyBtn text={url} testId="share-copy" />
            </div>
            <p className="text-[12px] text-muted">{i18nT('apps.awsControl.console.share_expires_note')}</p>
            <p className="mt-1 text-[12px] text-muted">{i18nT('apps.awsControl.console.share_credentials_caveat')}</p>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Section 6: Backup ───────────────────────────────────────────────────── */

const BACKUP_KINDS: BackupKind[] = ['snapshot', 'sessions']

function BackupSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const [showRemote, setShowRemote] = useState(false)
  const backupQ = useQuery({
    queryKey: ['aws-control', 'backup', account],
    queryFn: () => awsControlApi.backup(account),
  })
  const invalidate = () => qc.invalidateQueries({ queryKey: ['aws-control', 'backup', account] })
  const runMut = useMutation({
    mutationFn: (kind: BackupKind) => awsControlApi.backupRun(account, kind),
    onSuccess: invalidate,
  })
  const nightlyMut = useMutation({
    mutationFn: (enabled: boolean) => awsControlApi.backupNightly(account, enabled),
    onSuccess: invalidate,
  })
  const restoreMut = useMutation({
    mutationFn: (key: string) => awsControlApi.backupRestore(account, key),
  })

  const data = backupQ.data

  return (
    <section data-testid="backup-section">
      <SectionHeader icon={<Archive size={15} />} title={i18nT('apps.awsControl.console.backup_title')} />
      {backupQ.isLoading && <ContentSkeleton rows={2} />}
      {data && (
        <div className="rounded-md border border-border bg-card divide-y divide-border">
          {BACKUP_KINDS.map((kind) => {
            const run = data.runs[kind]
            const running = runMut.isPending && runMut.variables === kind
            return (
              <div key={kind} className="flex items-center gap-3 px-3 py-2.5" data-testid={`backup-row-${kind}`}>
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] font-medium text-text">{i18nT(BACKUP_KIND_LABEL_KEY[kind])}</div>
                  <div className="text-[12px] text-muted">
                    {run
                      ? i18nT('apps.awsControl.console.backup_last_run', { when: fmtRelative(run.at), size: fmtBytes(run.bytes) })
                      : i18nT('apps.awsControl.console.backup_never')}
                  </div>
                  {kind === 'sessions' && (
                    // The archive takes BOTH halves of a session, and the CLI
                    // half lives in a directory shared with any kiro-cli chat
                    // started outside Kiro Crew. Say so where the button is:
                    // the owner is choosing what leaves their machine.
                    <div className="text-[12px] text-muted" data-testid="backup-sessions-scope">
                      {i18nT('apps.awsControl.console.backup_sessions_scope')}
                    </div>
                  )}
                </div>
                <Btn onClick={() => runMut.mutate(kind)} disabled={running} data-testid={`backup-run-${kind}`}>
                  <RefreshCw size={13} className={running ? 'animate-spin' : ''} />
                  {running ? i18nT('apps.awsControl.console.backup_running') : i18nT('apps.awsControl.console.backup_run_now')}
                </Btn>
              </div>
            )
          })}
          <div className="flex items-center justify-between px-3 py-2.5" data-testid="backup-nightly">
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-text">{i18nT('apps.awsControl.console.backup_nightly')}</div>
              <div className="text-[12px] text-muted">{i18nT('apps.awsControl.console.backup_nightly_hint')}</div>
            </div>
            <Toggle checked={data.nightly} onChange={(v) => nightlyMut.mutate(v)} label={i18nT('apps.awsControl.console.backup_nightly')} />
          </div>
        </div>
      )}

      {data?.remoteError && (
        <p className="mt-2 text-[12px] text-muted" data-testid="backup-remote-error">{i18nT('apps.awsControl.console.backup_remote_error')}</p>
      )}

      {data?.remote && (
        <div className="mt-2">
          <button
            onClick={() => setShowRemote((v) => !v)}
            className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
            aria-expanded={showRemote}
            data-testid="backup-remote-toggle"
          >
            {i18nT('apps.awsControl.console.backup_archive')}
            <ChevronDown size={12} className={`transition-transform ${showRemote ? 'rotate-180' : ''}`} />
          </button>
          {showRemote && (
            <div className="mt-1.5 rounded-md border border-border bg-card divide-y divide-border" data-testid="backup-archive">
              {BACKUP_KINDS.flatMap((kind) => (data.remote?.[kind] ?? []).slice(0, 5).map((f) => (
                <div key={f.key} className="flex items-center gap-2 px-3 py-2 text-[12px]" data-testid="backup-archive-row">
                  <span className="min-w-0 flex-1 truncate font-mono text-text">{f.key}</span>
                  <span className="hidden shrink-0 text-muted sm:inline">{fmtBytes(f.size)}</span>
                  <Btn onClick={() => restoreMut.mutate(f.key)} disabled={restoreMut.isPending} data-testid="backup-restore"><Download size={13} />{i18nT('apps.awsControl.console.backup_restore')}</Btn>
                </div>
              )))}
            </div>
          )}
          {showRemote && (
            // The recommended least-privilege policy makes the backup prefix
            // write-only on purpose, so Restore is denied for anyone who pasted
            // exactly that tier. Say so where the button is instead of letting
            // them discover it as an AccessDenied.
            <p className="mt-1.5 text-[12px] text-muted" data-testid="backup-restore-caveat">
              {i18nT('apps.awsControl.console.backup_restore_caveat')}
            </p>
          )}
        </div>
      )}

      {restoreMut.data && (
        <div className="mt-2 rounded-md border border-border bg-bg-elevated p-2.5 text-[12px]" data-testid="backup-restored">
          <div className="mb-1 text-muted">{i18nT('apps.awsControl.console.backup_restored_note')}</div>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">{restoreMut.data.path}</code>
            <CopyBtn text={restoreMut.data.path} />
          </div>
        </div>
      )}
    </section>
  )
}

/* ── Section 7: Access (shares ledger) ───────────────────────────────────── */

function AccessSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const sharesQ = useQuery({
    queryKey: ['aws-control', 'shares', account],
    queryFn: () => awsControlApi.shares(account),
  })
  const forgetMut = useMutation({
    mutationFn: (id: string) => awsControlApi.shareForget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'shares', account] }),
  })
  const shares = sharesQ.data?.shares ?? []

  return (
    <section data-testid="access-section">
      <SectionHeader icon={<Share2 size={15} />} title={i18nT('apps.awsControl.console.access_title')} />
      {sharesQ.isLoading && <ContentSkeleton rows={1} />}
      {sharesQ.data && shares.length === 0 && (
        <p className="text-[13px] text-muted" data-testid="access-empty">{i18nT('apps.awsControl.console.access_empty')}</p>
      )}
      {shares.length > 0 && (
        <div className="rounded-md border border-border bg-card divide-y divide-border" data-testid="access-list">
          {shares.map((s: Share) => (
            <div key={s.id} className="flex items-center gap-3 px-3 py-2.5 text-[13px]" data-testid="access-row">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-text">{s.key}</span>
                  <Badge variant="muted">{i18nT(SECTION_LABEL_ON_PAGE[s.section])}</Badge>
                </div>
                <div className="text-[12px] text-muted">
                  {s.note ? `${s.note} · ` : ''}
                  {i18nT('apps.awsControl.console.access_expires_in', { when: fmtRelative(s.expiresAt) })}
                </div>
              </div>
              <Btn onClick={() => forgetMut.mutate(s.id)} disabled={forgetMut.isPending} data-testid="access-forget">{i18nT('apps.awsControl.console.access_forget')}</Btn>
            </div>
          ))}
        </div>
      )}
      <p className="mt-2 text-[12px] text-muted">{i18nT('apps.awsControl.console.access_footer')}</p>
    </section>
  )
}

/* ── The page ─────────────────────────────────────────────────────────────── */

/** The three sections of the bucket, in the order a reader meets them. */
const SECTIONS: DriveSection[] = ['drive', 'library', 'backup']

/**
 * Section names AS SEEN ON THIS PAGE.
 *
 * The bucket's `drive/` prefix is called "Drive" elsewhere, but this page is
 * itself the drive - so inside it that section is "Files". This is the ONLY
 * naming map left: a second one saying "Drive" printed that name as the page
 * title, the section row, the section header AND an access row's badge, so one
 * folder answered to two names on a page whose whole job is telling the folders
 * apart. There is no longer anywhere on this page that calls it "Drive".
 */
const SECTION_LABEL_ON_PAGE: Record<DriveSection, string> = {
  drive: 'apps.awsControl.console.section_files',
  library: 'apps.awsControl.console.section_library',
  backup: 'apps.awsControl.console.section_backup',
}

const SECTION_ICON: Record<DriveSection, typeof HardDrive> = {
  drive: FolderClosed,
  library: Library,
  backup: Archive,
}

/* Literal-key map from section → its one-line "what belongs here" description,
 * so no i18nT() call assembles a key by interpolation (dynamicKeys gate).
 * Mirrors SECTION_LABEL_ON_PAGE above. */
const SECTION_DESC_KEY: Record<DriveSection, string> = {
  drive: 'apps.awsControl.console.root_section_desc_drive',
  library: 'apps.awsControl.console.root_section_desc_library',
  backup: 'apps.awsControl.console.root_section_desc_backup',
}

/**
 * Each section's meter colour, as a SEMANTIC token — never a hex.
 *
 * The three must be visually distinct AND survive a theme switch (including the
 * light themes), so each is one of the palette's own role tokens rather than a
 * literal: `accent` for the drive, `info` for the library, `warn` for backups.
 * The legend swatch and the bar segment read the SAME token, so a segment and
 * its legend entry can never drift to different colours.
 */
/* Three CATEGORICAL colours, telling the segments apart -- this meter reports no
   health, so it must not borrow a status colour. `bg-warn` sat on Backup and read
   as "something is wrong with your backups" on a bar that only states sizes. */
const SECTION_TONE: Record<DriveSection, string> = {
  drive: 'bg-accent',
  library: 'bg-info',
  backup: 'bg-muted',
}

/**
 * The storage meter: total usage, and one horizontal bar split by section.
 *
 * The bar is proportional to each section's BYTES, but a section with zero
 * bytes still gets a legible legend row (its swatch and a `0` size) — a section
 * that exists is worth naming even when empty, and a 0-width bar segment alone
 * would silently drop it. When the whole drive is empty the bar renders as a
 * single muted track so the card is never a bare outline.
 */
function StorageMeter({ usage }: { usage: DriveUsage }) {
  const total = usage.bytes
  return (
    <div className="mb-4 rounded-lg border border-border bg-card p-4" data-testid="drive-storage-meter">
      {/* Heading only. This card used to restate the grand total on its right,
          ~50px below the header's identical "size | items" -- and both read
          `usage.bytes` / `usage.objects`, the SAME fields, so they could never
          disagree. I twice defended the pair as a cross-check of whole against
          sum-of-parts; that was wrong. The parts live in the legend below, which
          is the information this card actually adds. The total belongs to the
          header, once. */}
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-1">
        <span className="text-[13px] font-medium text-text-strong">
          {i18nT('apps.awsControl.console.root_storage_used')}
        </span>
      </div>

      {/* The bar. Proportional segments when there is anything to show; a single
          muted track when the drive is empty, so it is never a bare outline. */}
      <div
        className="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-bg-hover"
        data-testid="drive-meter-bar"
      >
        {total > 0 &&
          SECTIONS.map((s) => {
            const pct = (usage.sections[s].bytes / total) * 100
            if (pct <= 0) return null
            return (
              <div
                key={s}
                className={`h-full ${SECTION_TONE[s]}`}
                style={{ width: `${pct}%` }}
                data-testid={`drive-meter-segment-${s}`}
              />
            )
          })}
      </div>

      {/* Legend — every section, including 0-byte ones, with its own size. */}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5" data-testid="drive-meter-legend">
        {SECTIONS.map((s) => (
          <div key={s} className="flex items-center gap-1.5 text-[12px]" data-testid={`drive-meter-legend-${s}`}>
            <span className={`h-2.5 w-2.5 shrink-0 rounded-sm ${SECTION_TONE[s]}`} aria-hidden="true" />
            <span className="text-muted">{i18nT(SECTION_LABEL_ON_PAGE[s])}</span>
            <span className="font-mono text-text">{fmtBytes(usage.sections[s].bytes)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * One section as a folder card.
 *
 * The whole card is the control that opens the section — the same navigation
 * the old row carried on `setSection`. It stays a real `<button>`, so it keeps
 * button semantics (focusable, Enter/Space activation) for free rather than
 * re-implementing role/tabIndex/onKeyDown by hand. The `data-testid` the tests
 * resolve (`drive-section-<s>`) stays on that button.
 */
function SectionCard({ section, usage, onOpen }: {
  section: DriveSection
  usage: DriveSectionUsage
  onOpen: () => void
}) {
  const Icon = SECTION_ICON[section]
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4 text-left cursor-pointer hover:bg-bg-hover hover:border-accent/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      data-testid={`drive-section-${section}`}
    >
      <div className="flex items-center gap-2">
        <Icon size={16} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate text-[14px] font-semibold text-text-strong">
          {i18nT(SECTION_LABEL_ON_PAGE[section])}
        </span>
        <span className="shrink-0 font-mono text-[13px] text-text" data-testid={`drive-section-size-${section}`}>
          {fmtBytes(usage.bytes)}
        </span>
      </div>
      <p className="text-[12px] leading-snug text-muted">
        {i18nT(SECTION_DESC_KEY[section])}
      </p>
      <div className="mt-auto pt-1 text-[12px] text-muted" data-testid={`drive-section-count-${section}`}>
        {/* `count` selects the plural form; `objects` is what actually renders.
            Passing the raw count as the visible value printed "1234 items" under
            the header's "1,234 items" -- the same number two ways, one line
            apart, which is the defect this card was just cleaned up for. */}
        {i18nT('apps.awsControl.console.root_section_objects', {
          count: usage.objects,
          objects: fmtNumber(usage.objects),
        })}
      </div>
    </button>
  )
}

/**
 * A drive that EXISTS.
 *
 * `DriveStatus` is a union whose `exists: false` arm carries no bucket, and this
 * page is unreachable without one - so it takes the narrowed arm rather than
 * re-checking `exists` on every read of `drive.bucket`.
 */
export type LiveDrive = Extract<DriveStatus, { exists: true }>

export default function DrivePage({ account, drive: opened, onBack }: {
  account: AwsAccount
  /** The drive as it was when the reader opened this page. Initial data only -
   *  the live figure comes from the query below. */
  drive: LiveDrive
  onBack: () => void
}) {
  /**
   * Which section is open, or null at the drive's root.
   *
   * The bucket's three prefixes are the drive's top level, so the root is a
   * three-row folder listing rather than a rail or a set of tabs: one reader
   * gesture (open a folder) covers both this level and every level below it,
   * and the breadcrumb that returns is the same breadcrumb the file browser
   * already builds for its own subfolders.
   */
  const [section, setSection] = useState<DriveSection | null>(null)
  const id = account.account

  /**
   * Subscribe to the drive rather than render the snapshot we were handed.
   *
   * Every mutation on this page invalidates `['aws-control', 'drive', id]`; a
   * frozen prop would keep showing the size and object count from the moment the
   * page opened, so an upload or a folder delete would visibly change the
   * listing while the header kept the old totals. The snapshot is `initialData`,
   * so the header still paints immediately on arrival.
   */
  const driveQ = useQuery({
    queryKey: ['aws-control', 'drive', id],
    queryFn: () => awsControlApi.drive(id),
    initialData: opened,
  })
  const drive = driveQ.data.exists ? driveQ.data : opened

  return (
    <div className="flex h-full flex-col">
      <div className="px-4 pt-2 pb-3 md:px-6">
        <button
          onClick={section ? () => setSection(null) : onBack}
          className="mb-1 inline-flex items-center gap-1 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
          data-testid="drive-crumb-back"
        >
          <ChevronLeft size={14} />
          {/* The crumb states where the reader IS, so at the drive's root it
              names the account then the drive, and inside a section it names the
              drive then that section. Rendering the console's own crumb here
              (Accounts / <account>) said nothing about having changed page. */}
          {section ? i18nT('apps.awsControl.console.drive_title') : accountNameOf(account)}
          {' / '}
          <span className="text-text">
            {section ? i18nT(SECTION_LABEL_ON_PAGE[section]) : i18nT('apps.awsControl.console.drive_title')}
          </span>
        </button>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <HardDrive size={16} className="text-accent" />
          <span className="text-lg font-semibold text-text-strong">
            {i18nT('apps.awsControl.console.drive_title')}
          </span>
          <span className="font-mono text-[13px] text-muted" data-testid="drive-bucket">{drive.bucket}</span>
          <CopyBtn text={drive.bucket} testId="drive-copy-bucket" />
          <span className="text-[13px] text-muted">{drive.region}</span>
          <span className="text-[13px] text-muted" data-testid="drive-usage">
            {/* fmtNumber, not the raw count: the meter one line below already
                formats its own totals, so an unformatted number here rendered
                "100000" directly above "100,000" for the same quantity. */}
            {i18nT('apps.awsControl.console.stat_stored_value', {
              size: fmtBytes(drive.usage.bytes),
              objects: fmtNumber(drive.usage.objects),
            })}
          </span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
        {section === null && (
          <>
            {/* The storage meter: totals, plus one bar split by section. */}
            <StorageMeter usage={drive.usage} />

            {/* The bucket's three prefixes, as folder cards that state their own
                size. A responsive auto-fill grid so the cards reflow instead of
                stretching to a full-bleed row with a dead gap at wide widths. */}
            <div
              className="grid gap-3"
              style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}
              data-testid="drive-sections"
            >
              {SECTIONS.map((s) => (
                <SectionCard
                  key={s}
                  section={s}
                  usage={drive.usage.sections[s]}
                  onOpen={() => setSection(s)}
                />
              ))}
            </div>

            {/* The share ledger governs links into all three sections, so it
                belongs at the drive's root rather than inside one of them. */}
            <div className="mt-8">
              <AccessSection account={id} />
            </div>
          </>
        )}

        {section === 'drive' && <DriveSectionView account={id} bucket={drive.bucket} />}
        {section === 'library' && <LibrarySection account={id} bucket={drive.bucket} />}
        {section === 'backup' && <BackupSection account={id} />}
      </div>
    </div>
  )
}
