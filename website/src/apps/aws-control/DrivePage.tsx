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
import { Trans } from 'react-i18next'
import {
  ChevronDown, RefreshCw, Library, Archive, Share2,
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
import { usePersistedString } from '../../hooks/usePersistedString'
import { api } from '../../api/client'
import type { Artifact } from '../../types'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useNearViewport } from '../../hooks/useNearViewport'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtNumber, fmtRelative } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import type {
  DriveSection, DriveStatus, ArtifactKind, LibraryArtifact,
  BackupKind, BackupRun, BackupJobState, Share, DriveUsage,
} from './types'
import { CopyBtn, PaneHeader } from './shared'

/* Literal-key maps from enum → full catalog key, so no i18nT() call assembles a
 * key by interpolation (dynamicKeys gate): extractors and unused-key tooling
 * can then see every key, and a missing one fails the parity gate rather than
 * rendering raw. Mirrors UPDATE_ERROR_KEYS in pages/settings/AboutPanel.tsx. */

/* Refusals that name a cause the owner can act on, mapped rather than collapsed
 * into one line. The route answers these with codes precisely so the UI can
 * localise them; discarding the code and rendering a single generic string makes
 * the owner guess which of several different repairs to attempt.
 *
 * `invalid_account` is deliberately absent, and the asymmetry with `drive_missing`
 * is the point rather than an exception to it: the account comes from the page, so
 * the start button structurally cannot produce a malformed one -- an IMPOSSIBLE
 * state. `drive_missing` past the pane gate is a RACE (the drive deleted between
 * the pane rendering and the click), and rare is not impossible. It falls to the
 * generic line with any code we do not recognise. */
const START_ERROR_KEYS: Record<string, string> = {
  aws_consent_required: 'apps.awsControl.console.backup_start_consent',
  drive_missing: 'apps.awsControl.console.backup_start_no_drive',
  jobs_unavailable: 'apps.awsControl.console.backup_start_unavailable',
}
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
      iconOnly
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

/* Per-card state in this file is held as SET MEMBERSHIP rather than as a shared
   scalar, so N cards get N slots and no card can read another's state. Both the
   Library folder's removals and the picker's pushes use these, rather than each
   spelling the copy-on-write by hand. */
const withSlug = (set: ReadonlySet<string>, slug: string) => new Set(set).add(slug)
const withoutSlug = (set: ReadonlySet<string>, slug: string) => {
  const next = new Set(set)
  next.delete(slug)
  return next
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
export function LibrarySection({ account, bucket }: { account: string; bucket: string }) {
  const [mode, setMode] = useViewMode('library', 'grid')
  const [picking, setPicking] = useState(false)
  const qc = useQueryClient()

  /* Removal lives HERE, on the listing of what is actually in the bucket, and
     the state for it is the SECTION's rather than each card's.

     Both halves of that are load-bearing. The listing's rows come from
     `driveList`, so a row IS a cloud folder and removing its slug empties the
     object the reader was shown -- identity by construction. The picker beside
     it cannot say that: its rows are local artifacts joined to a slug-keyed
     ledger, so a reused slug lends a never-pushed artifact another one's push
     record and a removal there empties a DIFFERENT artifact's copy (#6987).

     Holding `confirmSlug` at the section makes only one confirm openable at a
     time and, more importantly, leaves nowhere for a per-card confirm flag to
     go stale: a card exists exactly while its object is listed, and a
     successful removal invalidates the listing, so the card and its strip
     unmount together. What keeps one row's outcome off another row is NOT this
     state but the per-slug SETS below -- see `pendingSlugs`. */
  const [confirmSlug, setConfirmSlug] = useState<string | null>(null)
  /* Per-card state as SET MEMBERSHIP, not as a slug-keyed scalar.
     
     One `useMutation` is right for FIRING the request. What cannot work is any
     single slot owning per-card state, and three rounds of this PR each fixed one
     more layer of that same mistake instead of the shape:
     
       1. `confirmSlug` cleared unconditionally, so a completing removal closed a
          different card's confirm. Fixed with the ownership guard below.
       2. `removeMut.reset()` on open threw away an in-flight removal's state, so
          a failure had nowhere to report. Fixed by keying the failure to a slug.
       3. that key was itself ONE slug, so of two overlapping failures the later
          hid the earlier -- and the observer's own pending flag is likewise one
          boolean, with its `variables` holding only the most recent slug, so a
          "Removing" label and a disabled Cancel could light up on a card nothing
          is happening to.
     
     Layers 1-3 are all the same defect: per-card state held in a value shared
     across cards. N cards need N slots, so these are sets and every per-card
     indicator is derived from membership. Nothing per-card reads a shared value,
     which is why there is no layer four to find. The check is mechanical: the
     only member this section reads off the mutation observer is `.mutate`, so
     `grep 'removeMut\.'` returns that call and this paragraph, nothing else.
     
     This is also the shape the picker 600 lines below has always used for its own
     per-card push state (`addingSlugs` / `failedSlugs`); the regression was mine,
     in moving the affordance without carrying that lesson across.
     
     `confirmSlug` stays a scalar deliberately: only one confirm strip is open at a
     time, which is a design constraint on the surface rather than a shared slot
     standing in for N. */
  const [pendingSlugs, setPendingSlugs] = useState<ReadonlySet<string>>(new Set())
  const [failedSlugs, setFailedSlugs] = useState<ReadonlySet<string>>(new Set())
  const removeMut = useMutation({
    mutationFn: (slug: string) => awsControlApi.libraryRemove(account, slug),
    onMutate: (slug: string) => {
      setPendingSlugs((prev) => withSlug(prev, slug))
      // A retry retires its OWN previous failure, so one card never shows both
      // states -- and never touches another card's.
      setFailedSlugs((prev) => withoutSlug(prev, slug))
    },
    onSuccess: (_data, slug) => {
      /* Clear the confirm ONLY if it is still this removal's. A `delete_prefix`
         sweep over several S3 objects is not instant, so the interleave needs
         nothing unusual: remove A, open B's confirm while A is still running, and
         an unconditional `setConfirmSlug(null)` closes B when A lands -- a
         removal the reader was about to confirm silently vanishes because an
         unrelated one finished. */
      setConfirmSlug((cur) => (cur === slug ? null : cur))
      // Both keys, for the same reason the push path invalidates both: the
      // PREFIX lost its objects (this listing) and the ledger forgot the record
      // (the local join that names these cards). Unconditional: the bucket
      // changed whether or not this row's confirm is still open.
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })
      qc.invalidateQueries({ queryKey: ['aws-control', 'library', account] })
    },
    onError: (_err, slug: string) => setFailedSlugs((prev) => withSlug(prev, slug)),
    // Settled, not success: a failed removal has to stop claiming to be running.
    onSettled: (_data, _err, slug: string) => setPendingSlugs((prev) => withoutSlug(prev, slug)),
  })
  // No reset: opening a confirm must not discard another row's in-flight state.
  const askRemove = (slug: string) => setConfirmSlug(slug)
  /** The strip's props for one slug, so grid and list cannot drift apart. */
  const confirmFor = (slug: string, title: string) => ({
    label: (
      <>
        {i18nT('apps.awsControl.console.library_remove_confirm', { name: title })}{' '}
        {/* NOT text-muted. This sentence is the identity the reader checks the
            delete against -- it names the bucket folder that will actually be
            emptied, which is the whole mitigation for a slug the local ledger
            cannot vouch for. Muting it styles the load-bearing half of the
            confirm as an aside and undercuts the argument the placement rests
            on (UX review on #7026). */}
        <span className="text-text">
          {/* One key carries the whole sentence and names the monospace chip with
              a `<folder>` tag, rather than a lead-in string plus a sibling
              <code>: a split hands the translator a fragment they cannot reorder
              around their own word order. Kept from #7026 verbatim.

              The literal is the BUCKET key prefix (`artifacts/`, what
              SECTION_PREFIXES['library'] resolves to), not the `library/` API
              route segment: naming a path that does not exist in the bucket
              would be unresolvable against `aws s3 ls` or the S3 console.

              What this chip does NOT do is establish WHOSE copy is being
              removed. The prefix is derived from the slug, and the slug is
              exactly what a later artifact reuses, so `artifacts/<slug>/` reads
              the same whether the bytes are this artifact's or an earlier one's.
              It names the folder truthfully; it cannot disambiguate its owner.
              That takes the pushed `meta.json` sidecar -- #6987. */}
          <Trans
            i18nKey="apps.awsControl.console.library_remove_confirm_slug"
            values={{ folder: `artifacts/${slug}/` }}
            components={{ folder: <code className="text-[11px] text-text" /> }}
          />
        </span>
      </>
    ),
    /* The failure renders on the CARD, not here, so it belongs to the row that
       asked for it whichever strip happens to be open -- see `failedSlugs`. */
    error: '',
    pending: pendingSlugs.has(slug),
    // Cancel retires this row's failure with the attempt it describes, and only
    // this row's: backing out should not leave a standing red beside a copy that
    // is still there, nor touch a sibling's.
    onCancel: () => { setConfirmSlug(null); setFailedSlugs((prev) => withoutSlug(prev, slug)) },
    onConfirm: () => removeMut.mutate(slug),
    action: pendingSlugs.has(slug)
      ? i18nT('apps.awsControl.console.library_removing')
      : i18nT('apps.awsControl.console.library_remove_action'),
    testId: 'library-remove-confirm',
  })

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
     the pushed meta.json sidecar instead of inferring from local state (#6987).

     READ THE SCOPE OF THIS GATE LITERALLY, because it is narrower than it
     sounds. `pushedAt` is `pushed.get("pushedAt")` in `list_pushable`, read from
     a ledger keyed `account -> slug`, and `ArtifactStore.delete` never prunes it.
     So the gate excludes an artifact with NO record under its slug -- the
     cross-machine "notes"/"readme" collision above -- and does NOT exclude one
     that INHERITED a record from a deleted predecessor: push A, delete A
     locally, create B on A's slug, and B reads A's `pushedAt`, passes here, and
     lends its name to A's object. No ledger-derived field can separate those two
     (`pushedVersion` and a `synced` flag are contaminated identically), which is
     why the fix is the sidecar and not a better predicate here. */
  const bySlug = new Map<string, LibraryArtifact>()
  for (const a of localQ.data?.artifacts ?? []) {
    if (a.pushedAt !== null) bySlug.set(a.slug, a)
  }
  /* Whether the local library ACTUALLY answered. Until it has, `bySlug` is empty
     and every cloud object looks orphaned -- so a card would assert "in the
     cloud only" on the strength of a lookup that has not returned, and borrow
     neither name, kind nor preview from a copy that does exist. A pending or
     failed lookup is not the answer "there is no local copy", so nothing may be
     concluded from a miss until this is true.

     It gates only what a card CLAIMS, never whether it can be removed. Removal
     targets the slug this listing returned, which is in the bucket whatever the
     local lookup did, and gating it on local state is what made a copy pushed
     from another machine unremovable in the first place. */
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
      <PaneHeader
        icon={<Library size={18} />}
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
        {i18nT('apps.awsControl.console.library_blurb')}
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
        <div className="-mr-3" data-testid="library-grid">
          <div className="grid items-start" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}>
            {slugs.map((slug) => (
              <LibraryCloudCard
                key={slug}
                slug={slug}
                local={bySlug.get(slug)}
                localAnswered={localAnswered}
                confirm={confirmSlug === slug ? confirmFor(slug, bySlug.get(slug)?.name || slug) : null}
                failed={failedSlugs.has(slug)}
                onAskRemove={() => askRemove(slug)}
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
            const title = local?.name || slug
            /* Same split as the cards: only a row with a local copy behind it has
               an artifact page to open, so a cloud-only row stays inert rather
               than linking somewhere that would 404. */
            const RowBody = local ? (
              <Link
                to={`/artifacts/${slug}`}
                aria-label={i18nT('apps.awsControl.console.library_open', { name: title })}
                className={`${ROW} min-w-0 flex-1 transition-colors hover:bg-bg-hover`}
                data-testid="library-list-row"
              >
                {RowInner}
              </Link>
            ) : (
              <div className={`${ROW} min-w-0 flex-1`} data-testid="library-list-row">
                {RowInner}
              </div>
            )
            /* A view is a way of LOOKING at this folder, not a capability tier,
               so the row carries the same overflow menu the card carries -- and
               the choice PERSISTS per section, so leaving it out would mean a
               reader who once switched to the list can never remove a copy
               again. Same trigger shape as the Files folder's own rows
               (`drive-more`), and the trigger sits OUTSIDE the row's link for
               the reason the card's does: interactive content inside an anchor
               is invalid, and nesting it would put a destructive path inside the
               navigation the rest of the row performs. */
            return (
              <div key={slug}>
                <div className="flex items-center gap-1 pr-2">
                  {RowBody}
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button
                        type="button"
                        className="cursor-pointer rounded border-none bg-transparent p-1 text-muted transition-colors hover:text-text"
                        aria-label={i18nT('apps.awsControl.console.library_actions')}
                        data-testid="library-more"
                      >
                        <MoreHorizontal size={14} />
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => askRemove(slug)} data-testid="library-remove">
                        <Trash2 size={13} />{i18nT('apps.awsControl.console.library_remove')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                {failedSlugs.has(slug) && (
                  <p className="px-3 pb-2 text-[11px] leading-snug text-danger" data-testid="library-remove-error">
                    {i18nT('apps.awsControl.console.library_remove_failed')}
                  </p>
                )}
                {confirmSlug === slug && (
                  <div className="px-3 pb-2.5">
                    <TileConfirm {...confirmFor(slug, title)} />
                  </div>
                )}
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
function TileConfirm({ label, error, pending, onCancel, onConfirm, action, testId = 'drive-grid-confirm' }: {
  /* A NODE, not a string: the library's removal names the cloud folder it will
     empty, and a bucket path belongs in a <code> chip inside the sentence rather
     than flattened into it. Every existing caller passes a string, which is a
     ReactNode. */
  label: React.ReactNode
  error: string
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
  action: string
  /* The base for this strip's test ids, so a second caller is addressable as
     itself. Defaults to the drive's, which keeps every existing id unmoved. */
  testId?: string
}) {
  return (
    <div className="mt-1 w-full border-t border-border pt-2" data-testid={testId}>
      <p className="mb-2 text-[12px] leading-snug text-text">{label}</p>
      {error && <p className="mb-2 text-[11px] text-danger" data-testid={`${testId}-error`}>{error}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {/* Disabled while the request runs, and that is a correctness rule rather
            than polish: this strip is the ONLY place the outcome can render, so
            dismissing it mid-flight throws away the answer to a destructive
            request that is still in progress -- the reader is told nothing, and a
            failure that left the copy in place looks identical to a success. */}
        <Btn onClick={onCancel} disabled={pending} data-testid={`${testId}-cancel`}>
          {i18nT('apps.awsControl.console.cancel')}
        </Btn>
        <Btn danger disabled={pending} onClick={onConfirm} data-testid={`${testId}-action`}>
          <Trash2 size={13} />{action}
        </Btn>
      </div>
    </div>
  )
}

/** One artifact that IS in the cloud, as a preview card. */
function LibraryCloudCard({ slug, local, localAnswered, confirm, failed, onAskRemove }: {
  slug: string
  local: LibraryArtifact | undefined
  localAnswered: boolean
  /** The open confirm's props, or null when this card's confirm is closed. */
  confirm: React.ComponentProps<typeof TileConfirm> | null
  /** Whether THIS card's own removal failed. Keyed by slug, so a sibling's
      failure never renders here and opening a sibling never erases it. */
  failed: boolean
  onAskRemove: () => void
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

  const SHELL = 'relative mb-3 mr-3 overflow-hidden rounded-lg border border-border bg-card'
  /* A card backed by a local copy has somewhere to go: the artifact's own page.
     A cloud-only card does NOT -- there is no local artifact to open -- so it
     stays inert rather than offering a link that would 404, and it keeps the flat
     border, because a hover affordance on a card that cannot be opened promises
     an interaction that does not exist.

     A real <Link> rather than a click handler: middle-click, cmd-click and
     keyboard activation all come for free, and there is no nested-control
     hijack to guard against. */
  const openable = local ? (
    <Link
      to={`/artifacts/${slug}`}
      aria-label={i18nT('apps.awsControl.console.library_open', { name: title })}
      className="block transition-colors hover:bg-bg-hover"
    >
      {body}
    </Link>
  ) : (
    <div>{body}</div>
  )
  /* The shell is a <div> and the LINK is inside it, rather than the shell being
     the link. That is what lets the overflow trigger be a real <button>: a button
     inside an anchor is invalid content, and a trigger that had to
     `preventDefault` its way out of the surrounding navigation is the kind of
     nested-control hijack the file avoids elsewhere. Splitting them keeps
     cmd-click and middle-click working on everything a reader would click to
     OPEN the artifact, and keeps the destructive path out of that target. */
  return (
    <div className={SHELL} data-testid="library-card">
      {openable}
      {/* Per-item actions live in ONE overflow menu, the same shape the Files
          folder's own cards use (`drive-grid-more`): same MoreHorizontal
          trigger, same DropdownMenu primitives, same align="end", and the
          destructive item still lands as a TileConfirm strip below rather than
          firing from the menu. A visible danger button on every card of a
          browse surface was the design this replaces -- a reader moving between
          the two folders met two different grammars for "act on this item".

          Positioned over the preview rather than in the body row, because the
          body is inside the link and this must not be. It is always visible
          rather than hover-revealed: a hover-only control is unreachable by
          touch and invisible to anyone scanning the card for what they can do.

          The menu holds one item today. That is deliberate -- Share and
          Download for a library object would need presigned reads on the
          library prefix, which this change does not add -- and the menu is
          where they land when they exist, instead of a second control
          appearing beside it. */}
      <div className="absolute right-2 top-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="cursor-pointer rounded border-none bg-card/85 p-1 text-muted backdrop-blur-sm transition-colors hover:text-text"
              aria-label={i18nT('apps.awsControl.console.library_actions')}
              data-testid="library-more"
            >
              <MoreHorizontal size={14} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {/* Offered on EVERY listed object, including one with no local row.
                Those are the copies pushed from another machine, and they are
                the reason this control had to move off the picker: the picker
                could only ever reach local artifacts, so a copy from elsewhere
                was unremovable while a locally reused slug could remove the
                wrong one -- exactly backwards from what the spec asks of a
                console "that must be able to remove it". Nothing here is gated
                on local state, because the object's presence in this listing is
                not. */}
            {/* Offered even when a `local` twin exists, and that is the boundary
                against the picker rather than an oversight. A row here exists if
                and only if `artifacts/<slug>/` is in the bucket -- the listing
                enumerates the prefix -- so the removal empties exactly the
                object that was observed, never an object inferred from the
                ledger. On the picker the row IS a local artifact and no cloud
                object has been observed at all, so there the target would be a
                ledger inference; that surface gets no removal, pinned by its own
                test.

                What is NOT settled here, and must not be read into the above:
                WHOSE copy this is. `local` comes from a ledger keyed
                `account -> slug`, so a later artifact that reuses the slug
                inherits the earlier one's push record and lends this card its
                label. The confirm cannot resolve it either -- the folder name is
                built from that same shared slug, so it reads identically for
                both artifacts. So the delete target is correct and the DISPLAYED
                IDENTITY may belong to a different artifact, on an irreversible
                action. Proving identity requires the pushed `meta.json` sidecar
                and is #6987's; this placement neither introduces that hole nor
                closes it (main offers the same slug-targeted removal from
                `PickerCard`). Do not "fix" it by hiding this item when `local`
                is defined: that is the common case, the picker's removal is gone
                in this change, and the two together would leave no way to remove
                a cloud copy at all. */}
            <DropdownMenuItem onSelect={onAskRemove} data-testid="library-remove">
              <Trash2 size={13} />{i18nT('apps.awsControl.console.library_remove')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {/* On the CARD rather than inside the strip, because the failure belongs to
          this copy whichever confirm happens to be open -- a reader who started
          this removal and then looked at another card still sees that it did not
          happen, instead of being told nothing at all. */}
      {failed && (
        <p className="px-3 pb-2 text-[11px] leading-snug text-danger" data-testid="library-remove-error">
          {i18nT('apps.awsControl.console.library_remove_failed')}
        </p>
      )}
      {confirm && (
        <div className="px-3 pb-3">
          <TileConfirm {...confirm} />
        </div>
      )}
    </div>
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
  const addOne = async (slug: string) => {
    // A retry clears the previous failure, so one card never shows both states.
    setAddingSlugs((prev) => withSlug(prev, slug))
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
      setFailedSlugs((prev) => withSlug(prev, slug))
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

        {/* The disclosure has to be HERE, where the adding happens: adding fills
            storage the account pays for, and the empty-state button opens this
            dialog directly, so a warning the reader never passes is not a
            warning.

            The COST is all it says, and that constraint is load-bearing: the
            cards below carry a Remove control, so any claim about removal being
            unavailable would be disproved by a button in the same dialog on
            every open, and a banner a button contradicts costs the reader their
            trust in both. */}
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
          {/* A chip you cannot usefully press is noise: a zero-count kind
              filters the grid to nothing. 'All' always renders, and the
              currently-selected kind stays visible even at zero so the reader
              can see (and undo) an active filter that no longer matches. */}
          {(['all', ...KIND_KEYS] as const)
            .filter((k) => k === 'all' || k === kind || (counts[k] ?? 0) > 0)
            .map((k) => (
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
            <div className="-mr-3">
              <div className="grid items-start" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}>
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
function PickerCard({
  artifact, onPush, pushing, failed,
}: {
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
  /* NO removal control here, deliberately, and this is the defect the change
     fixes rather than an omission.

     This card is a LOCAL artifact joined to a ledger keyed `account -> slug`.
     `ArtifactStore.delete` does not prune that ledger, and a new artifact starts
     at version 1 -- so pushing A, deleting A locally and creating a B that takes
     A's slug leaves B's card wearing A's push record. A removal offered here
     would empty `artifacts/<slug>/`, which is A's copy, under B's name. No
     predicate available on this card can tell the two apart: `synced` is the
     inherited record itself, and `pushedVersion === version` is satisfied by a
     never-pushed B at v1 against A's pushed v1. Naming the folder in the confirm
     narrows the blast radius but cannot fix it -- the reader is still being asked
     to vouch for an identity this machine cannot establish.

     Removal therefore belongs to the Library folder behind this dialog, whose
     rows come from the bucket listing, so removing one empties the object that
     was LISTED instead of one the ledger merely implies. That is a better
     TARGET, not a proof of ownership: the label there is still the slug-keyed
     join, and the confirm's folder name is built from the same shared slug, so
     neither can separate two artifacts that took turns holding it. Until #6987
     reads the pushed meta.json sidecar, no surface can. What the move buys is
     that the object being emptied is one the bucket actually reported, and that
     a cloud copy with no local row at all becomes reachable. */
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
                otherwise restore.

                Neither the backend reconcile nor Remove makes the door safe to
                lock. Reconcile is slug-granular: it prunes a record only when the
                WHOLE `artifacts/<slug>/` prefix is absent, so a copy that lost
                its content key but kept its sidecar still lists as present and
                still reads as up to date. And Remove empties the prefix -- it
                never puts the content back, so it is not the restore path a
                disabled Push would need. */}
            {upToDate && (
              <p className="mb-1.5 text-[11px] text-muted" data-testid="library-already">
                {i18nT('apps.awsControl.console.library_in_cloud')}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
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

export function DriveSectionView({ account, bucket }: { account: string; bucket: string }) {
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
  /** Folder-name input is a disclosure: visible only after "New folder". */
  const [creatingFolder, setCreatingFolder] = useState(false)
  const [folderError, setFolderError] = useState('')
  /** The ONE way out of the folder disclosure (Escape, Cancel, blur-on-empty),
   *  carrying the whole close invariant: (1) it refuses while a create is in
   *  flight — collapsing mid-request would erase the very name being created,
   *  so a failure comes back to a wiped input; (2) when it does close, it
   *  clears ALL creation state — the name, the validation error, and the
   *  mutation's own error — because any of the three left behind renders as an
   *  orphan under a toolbar whose input is gone. */
  const closeFolderDisclosure = () => {
    if (folderCreateMut.isPending) return
    setNewFolder('')
    setFolderError('')
    folderCreateMut.reset()
    setCreatingFolder(false)
  }
  /* How many objects the last folder delete actually removed. One click can
     remove far more than one file, and the count is only knowable AFTER the
     fact - the response carries it, while a figure shown BEFORE consent would
     cost a second full recursive listing of the prefix. So the page reports
     what was removed rather than pretending to predict it. */
  const [deletedCount, setDeletedCount] = useState<number | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
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
    // The KEY is the caller's, not derived from the browse path here: the
    // Upload button targets the open folder, while a drag-drop names the
    // folder row it landed on — two callers, one mutation.
    mutationFn: ({ file, key }: { file: File; key: string }) =>
      awsControlApi.driveUpload(account, 'drive', key, file),
    onSuccess: invalidate,
    // A dropped file whose put fails on the wire would otherwise vanish
    // silently — nothing renders it, so the user believes it uploaded. The
    // name is interpolated because a multi-file drop shares one error line.
    onError: (_e: unknown, vars: { file: File; key: string }) => {
      setUploadError(i18nT('apps.awsControl.console.drive_upload_failed', {
        name: vars.key.split('/').pop() ?? vars.key,
      }))
    },
  })
  const deleteMut = useMutation({
    mutationFn: (key: string) => awsControlApi.driveDelete(account, 'drive', key),
    onSuccess: invalidate,
  })
  const folderCreateMut = useMutation({
    mutationFn: (name: string) =>
      awsControlApi.driveFolderCreate(account, 'drive', path ? `${path}/${name}` : name),
    onSuccess: () => { setNewFolder(''); setCreatingFolder(false); invalidate() },
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
    uploadMut.mutate({ file, key: path ? `${path}/${file.name}` : file.name })
  }

  /** Upload dropped OS files into `folder` ('' = the open folder's own path).
   *  Invalid names surface through the same strip the picker uses — but NAMED:
   *  a 10-file drop can fail on one file while the rest upload, and the
   *  picker's anonymous "that file name" would not say which one. */
  const uploadDropped = (list: FileList, folder: string) => {
    setUploadError('')
    for (const file of Array.from(list)) {
      if (!KEY_SEGMENT.test(file.name)) {
        setUploadError(i18nT('apps.awsControl.console.drive_bad_name_named', { name: file.name }))
        continue
      }
      const prefix = folder || path
      uploadMut.mutate({ file, key: prefix ? `${prefix}/${file.name}` : file.name })
    }
  }

  /** Which drop target the pointer is over: '' is the listing itself (the open
   *  folder), a folder's full path names that folder. Null = no drag in
   *  flight. Drives the highlight only — the drop handlers re-derive their own
   *  target so a missed dragleave cannot misroute a drop. */
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const [moveError, setMoveError] = useState('')

  const moveMut = useMutation({
    mutationFn: ({ fromKey, toKey }: { fromKey: string; toKey: string }) =>
      awsControlApi.driveMove(account, 'drive', fromKey, toKey),
    onSuccess: () => {
      setMoveError('')
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive-list', account] })
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })
    },
    onError: (e: unknown) => {
      // Two refusals worth their own sentences: share_active (the source has
      // a live share link — moving would 404 it) and destination_exists (the
      // destination folder already holds this name; never overwritten).
      const err = e instanceof AwsControlError ? e : null
      setMoveError(i18nT(
        err?.message === 'share_active'
          ? 'apps.awsControl.console.move_shared'
          : err?.status === 409
            ? 'apps.awsControl.console.move_conflict'
            : 'apps.awsControl.console.move_failed'))
    },
  })

  /** The wire format an internal file drag travels as. A custom MIME keeps OS
   *  file drops (types includes 'Files') and internal moves distinguishable. */
  const DRAG_MIME = 'application/x-drive-object-key'

  /** The key of the drag THIS component started, or null. The drop handler
   *  trusts this ref, never the DataTransfer payload: drag data is
   *  attacker-writable (any external page can start a drag carrying our MIME
   *  with a real key), and a drop on a folder here would then run an
   *  authenticated move of the owner's file. The payload is still written for
   *  the OS drag image / other targets, but a drop only moves what our own
   *  onDragStart recorded — cleared on dragend so a stale key can never
   *  outlive its gesture. */
  const dragKeyRef = useRef<string | null>(null)

  /** Move `fromKey` into `folder` (full path, '' = section root). A drop onto
   *  the folder the file already lives in is a no-op, not an error. */
  const moveInto = (fromKey: string, folder: string) => {
    const base = fromKey.split('/').pop() ?? fromKey
    const fromDir = fromKey.split('/').slice(0, -1).join('/')
    if (fromDir === folder) return
    setMoveError('')
    moveMut.mutate({ fromKey, toKey: folder ? `${folder}/${base}` : base })
  }

  /** Shared drop-target wiring: accepts OS files (upload into `folder`) and
   *  internal drags (move into `folder`). */
  const dropProps = (folder: string) => ({
    onDragOver: (e: React.DragEvent) => {
      const t = e.dataTransfer.types
      if (!t.includes('Files') && !t.includes(DRAG_MIME)) return
      e.preventDefault()
      e.stopPropagation()
      setDropTarget(folder)
    },
    onDragLeave: (e: React.DragEvent) => {
      e.stopPropagation()
      setDropTarget((cur) => (cur === folder ? null : cur))
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      setDropTarget(null)
      // Trust boundary: the ref, not the DataTransfer. A cross-page drag
      // carrying our MIME reaches here with types matching, but no
      // onDragStart of OURS ran, so the ref is null and the drop is inert.
      const key = dragKeyRef.current
      dragKeyRef.current = null
      if (key && e.dataTransfer.types.includes(DRAG_MIME)) moveInto(key, folder)
      else if (e.dataTransfer.files.length > 0) uploadDropped(e.dataTransfer.files, folder)
    },
  })

  /** Draggable wiring for a file row/tile. */
  const dragProps = (key: string) => ({
    draggable: true,
    onDragStart: (e: React.DragEvent) => {
      dragKeyRef.current = key
      e.dataTransfer.setData(DRAG_MIME, key)
      e.dataTransfer.effectAllowed = 'move'
    },
    onDragEnd: () => {
      dragKeyRef.current = null
    },
  })

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
    <section data-testid="drive-section" {...dropProps(path)} className={dropTarget === path ? 'rounded-lg ring-1 ring-inset ring-accent' : undefined}>
      <PaneHeader icon={<FolderClosed size={18} />} title={i18nT('apps.awsControl.console.section_files')} actions={
        <div className="flex flex-wrap items-center gap-2">
        <ViewModeToggle section="drive" mode={mode} onChange={setMode} />
        {/* The name field appears when the reader ASKS to create a folder.
            Parked permanently in the toolbar it was two dead controls (an empty
            input and a disabled button) on every visit that isn't about
            folders — which is most of them. Escape, Cancel, or blurring the
            empty field puts the toolbar back; Upload hides while creating so
            the expanded row stays one action group of two buttons. */}
        {creatingFolder ? (
          <>
            <Input
              value={newFolder}
              onChange={(e) => setNewFolder(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onCreateFolder()
                if (e.key === 'Escape') closeFolderDisclosure()
              }}
              onBlur={() => { if (!newFolder.trim()) closeFolderDisclosure() }}
              autoFocus
              placeholder={i18nT('apps.awsControl.console.folder_name')}
              aria-label={i18nT('apps.awsControl.console.folder_new')}
              className="w-full min-w-0 basis-full sm:w-[160px] sm:flex-none sm:basis-auto"
              data-testid="drive-folder-name"
            />
            <Btn onClick={onCreateFolder} disabled={folderCreateMut.isPending || !newFolder.trim()} data-testid="drive-folder-create">
              <FolderPlus size={13} />
              {i18nT('apps.awsControl.console.folder_new')}
            </Btn>
            <Btn onClick={closeFolderDisclosure} disabled={folderCreateMut.isPending} data-testid="drive-folder-cancel">
              {i18nT('apps.awsControl.console.cancel')}
            </Btn>
          </>
        ) : (
          <>
            <Btn onClick={() => setCreatingFolder(true)} data-testid="drive-folder-toggle">
              <FolderPlus size={13} />
              {i18nT('apps.awsControl.console.folder_new')}
            </Btn>
            <Btn onClick={() => fileRef.current?.click()} disabled={uploadMut.isPending} data-testid="drive-upload-btn">
              <Upload size={13} />
              {uploadMut.isPending ? i18nT('apps.awsControl.console.drive_uploading') : i18nT('apps.awsControl.console.drive_upload')}
            </Btn>
          </>
        )}
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
      {moveError && <p className="mb-2 text-[12px] text-danger" role="alert" data-testid="drive-move-error">{moveError}</p>}
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
        <div className="-mr-3" data-testid="drive-grid">
          <div className="grid items-start" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}>
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
                {...dropProps(name)}
                className={`mb-3 mr-3 flex cursor-pointer flex-col items-start gap-2 rounded-lg border border-border bg-card p-3 text-left transition-colors hover:border-border-strong hover:bg-bg-hover ${dropTarget === name ? 'ring-1 ring-inset ring-accent bg-bg-hover' : ''}`}
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
                {...dragProps(f.key)}
                className="mb-3 mr-3 flex flex-col items-start gap-2 rounded-lg border border-border bg-card p-3"
                data-testid="drive-grid-file"
              >
                <div className="flex w-full items-start justify-between gap-2">
                  <FileText size={22} className="text-muted" aria-hidden="true" />
                  {/* ONE home for per-item actions. Download used to sit outside
                      the menu as a bare button while Share and Delete were
                      inside it, so a card offered two different grammars for
                      "act on this item" and a reader had to learn both. It is
                      the same inconsistency the Library folder's visible Remove
                      was rejected for, one component over, so it is fixed in the
                      same change rather than left to recreate the problem. */}
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
                      {/* `onSelect` is dispatched synchronously from the item's
                          own click handler, so the window.open inside
                          `download` still runs within the user gesture and is
                          not treated as an unattended popup. */}
                      <DropdownMenuItem onSelect={() => download(f.key)} data-testid="drive-grid-download">
                        <Download size={13} />{i18nT('apps.awsControl.console.download')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => setShare({ key: f.key })} data-testid="drive-grid-share">
                        <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => setConfirmDelete(f.key)} data-testid="drive-grid-delete">
                        <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
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
        /* Borderless, the stock shadcn table posture: row dividers only, no
           frame and no card fill — the heavy outer border read as chrome on a
           page that is mostly this one table. The div stays: it is the
           horizontal scroll container the pinned Actions seam measures. */
        <div ref={attachScroller} className="overflow-x-auto" data-testid="drive-listing">
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
                  {...dropProps(name)}
                  className={`cursor-pointer border-b border-border last:border-0 hover:bg-bg-hover ${dropTarget === name ? 'bg-bg-hover ring-1 ring-inset ring-accent' : ''}`}
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
                  <tr {...dragProps(f.key)} className="border-b border-border last:border-0 hover:bg-bg-hover" data-testid="drive-file">
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
                      {/* ONE overflow, holding every per-item action. Download
                          used to sit beside it as a bare button while Share and
                          Delete were inside, which is the same split the grid
                          card above just lost. */}
                      <div className="flex items-center justify-end gap-1">
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
                            <DropdownMenuItem onSelect={() => download(f.key)} data-testid="drive-download">
                              <Download size={13} />{i18nT('apps.awsControl.console.download')}
                            </DropdownMenuItem>
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

/**
 * One backup kind's row.
 *
 * Its own component, rather than a `.map` body inside `BackupSection`, so each
 * row's mutation state stays its own -- a click on one kind must not disable the
 * other.
 *
 * `busy` comes from the SERVER, not from `runMut.isPending`. That is the whole
 * change: the previous indicator lived in this component, so unmounting it
 * destroyed the only record that a backup was in flight, and coming back showed
 * an idle row while the upload continued. Under the rail that unmount is no
 * longer hypothetical -- switching panes genuinely unmounts this subtree.
 */
function BackupRow({
  account,
  kind,
  run,
  job,
  onStarted,
}: {
  account: string
  kind: BackupKind
  run: BackupRun | undefined
  job: BackupJobState | undefined
  onStarted: () => void
}) {
  const runMut = useMutation({
    mutationFn: () => awsControlApi.backupRun(account, kind),
    // The claim is durable the moment the POST returns, but the next poll may be
    // seconds away. Re-read now so the row turns over immediately instead of
    // looking like the click did nothing.
    onSuccess: onStarted,
  })
  // The server owns the answer, scoped to THIS account. `isPending` still matters
  // for the window between the click and the claim landing: the server does not
  // know about the run yet, and a row that ignored it would accept a second click
  // that starts nothing (the SDK would dedupe it).
  const busy = job?.active != null || runMut.isPending
  // A run that ended `failed` must not render identically to one that succeeded.
  // The app's ledger records only successes, so without this the row would simply
  // stop spinning -- which is itself a false statement about what happened.
  const failed = !busy ? (job?.lastFailed ?? null) : null
  // Which refusals a retry can actually clear, enumerated from the route rather
  // than special-cased one at a time. `aws_call_failed` is a 502 from a live AWS
  // call and is the only genuinely transient one; a transport-level `http_5xx`
  // is the same shape. Everything else the start path answers needs the owner to
  // do something ELSE, so it gets the cause named (START_ERROR_KEYS) or, for a
  // code we do not recognise, a line that promises nothing. Defaulting to "try
  // again" and excepting one code was the wrong way round.
  const code = (runMut.error as Error | null)?.message ?? ''
  const retryable = code === 'aws_call_failed' || /^http_5\d\d$/.test(code)
  const startError = runMut.isError
    ? i18nT(
        START_ERROR_KEYS[code] ??
          (retryable
            ? 'apps.awsControl.console.backup_start_retry'
            : 'apps.awsControl.console.backup_start_failed'),
      )
    : ''

  return (
    <div className="flex items-center gap-3 px-3 py-2.5" data-testid={`backup-row-${kind}`}>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-text">{i18nT(BACKUP_KIND_LABEL_KEY[kind])}</div>
        <div className="text-[12px] text-muted">
          {run
            ? i18nT('apps.awsControl.console.backup_last_run', { when: fmtRelative(run.at), size: fmtBytes(run.bytes) })
            : i18nT('apps.awsControl.console.backup_never')}
        </div>
        {(failed || startError) && (
          <div className="text-[12px] text-danger" data-testid={`backup-error-${kind}`}>
            {startError || i18nT('apps.awsControl.console.backup_failed', { reason: failed?.error || '' })}
          </div>
        )}
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
      <Btn onClick={() => runMut.mutate()} disabled={busy} data-testid={`backup-run-${kind}`}>
        <RefreshCw size={13} className={busy ? 'animate-spin' : ''} />
        {busy ? i18nT('apps.awsControl.console.backup_running') : i18nT('apps.awsControl.console.backup_run_now')}
      </Btn>
    </div>
  )
}

export function BackupSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const [showRemote, setShowRemote] = useState(false)
  const backupQ = useQuery({
    // `showRemote` is part of the key on purpose: the remote listing costs paid
    // AWS calls, so it is fetched only while the stored-archive list is open, and
    // opening it is a deliberate refetch rather than a hidden cost on every poll.
    queryKey: ['aws-control', 'backup', account, showRemote],
    queryFn: () => awsControlApi.backup(account, { remote: showRemote }),
    // Poll only while a run is actually in flight: an idle section needs no
    // timer, and a start goes through `invalidate`, which is deterministic
    // rather than a wait for the next tick. A remount must ADOPT the server's
    // answer rather than render a cached idle state -- that cached-stale render
    // is the original bug wearing a different hat.
    refetchInterval: (query) =>
      BACKUP_KINDS.some((k) => query.state.data?.jobs?.[k]?.active != null) ? 3000 : false,
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const invalidate = () => qc.invalidateQueries({ queryKey: ['aws-control', 'backup', account] })
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
      <PaneHeader icon={<Archive size={18} />} title={i18nT('apps.awsControl.console.backup_title')} />
      {backupQ.isLoading && <ContentSkeleton rows={2} />}
      {data && (
        <div className="rounded-md border border-border bg-card divide-y divide-border">
          {BACKUP_KINDS.map((kind) => (
            <BackupRow
              key={kind}
              account={account}
              kind={kind}
              run={data.runs[kind]}
              // Account-scoped: this payload answers "is a backup running for THIS
              // account", which the app-scoped `_jobs/active` surface cannot.
              job={data.jobs?.[kind]}
              // Re-read immediately after a start, rather than waiting out the
              // poll gap and looking like the click did nothing.
              onStarted={invalidate}
            />
          ))}
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

      {/* Gated on status data existing, NOT on `data.remote`. The remote half is
        * opt-in behind `?remote=1`, which only this disclosure can request -- so
        * gating the disclosure on remote data made the control that enables
        * remote fetching wait for the fetch it enables, and the archive and
        * Restore became unreachable. The rows below are already null-safe. */}
      {data && (
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

export function AccessSection({ account }: { account: string }) {
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
      <PaneHeader icon={<Share2 size={18} />} title={i18nT('apps.awsControl.console.access_title')} />
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
export function StorageMeter({ usage }: { usage: DriveUsage }) {
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
 * A drive that EXISTS.
 *
 * `DriveStatus` is a union whose `exists: false` arm carries no bucket, and this
 * page is unreachable without one - so it takes the narrowed arm rather than
 * re-checking `exists` on every read of `drive.bucket`.
 */
export type LiveDrive = Extract<DriveStatus, { exists: true }>
