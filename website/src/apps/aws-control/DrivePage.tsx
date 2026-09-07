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
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Trans } from 'react-i18next'
import {
  ChevronDown, RefreshCw, Library, Archive, Share2,
  Download, Trash2, Upload, FolderClosed, FolderOpen, FolderPlus, FolderInput, FileText, X,
  MoreHorizontal, Code, LayoutGrid, List, Search, CloudOff, Plus, AlertTriangle, Pencil,
  Database, Link2,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  Btn, Badge, Toggle, Input, ContentSkeleton, IconButton,
  Card, EmptyState, FilteredEmpty, PanelSectionHeader, SearchInput,
} from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import SegmentedControl from '../../components/SegmentedControl'
import { LibraryTableHead, PINNED_SURFACE } from '../../components/library/LibraryTable'
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
import { CopyBtn, PaneHeader, AwsErrorNotice, StorageBar, QuickTile } from './shared'

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
 *
 * `iconOnly`, where the gallery shows labels: this toggle shares a pane header
 * with two more actions (New folder, Upload) that the gallery's toolbar does
 * not carry, and the header sits in a pane the rail has already narrowed. Two
 * labelled segments plus two labelled buttons wrap to a second toolbar row at
 * ordinary desktop widths; the glyphs are the same ones the gallery draws, so
 * the control is recognised from there rather than re-learned.
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
/* A FAILURE is a slug plus the value its request rejected with, so the card that
   reports it can hand the agent the real refusal rather than only the sentence.
   Same copy-on-write shape as the sets above; `null` marks a rejection that
   carried nothing, so membership alone still answers "did this card fail". */
type Failures = ReadonlyMap<string, unknown>
const withFailure = (map: Failures, slug: string, err: unknown): Failures =>
  new Map(map).set(slug, err ?? null)
const withoutFailure = (map: Failures, slug: string): Failures => {
  const next = new Map(map)
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
  const [failedSlugs, setFailedSlugs] = useState<Failures>(new Map())
  const removeMut = useMutation({
    mutationFn: (slug: string) => awsControlApi.libraryRemove(account, slug),
    onMutate: (slug: string) => {
      setPendingSlugs((prev) => withSlug(prev, slug))
      // A retry retires its OWN previous failure, so one card never shows both
      // states -- and never touches another card's.
      setFailedSlugs((prev) => withoutFailure(prev, slug))
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
    onError: (err, slug: string) => setFailedSlugs((prev) => withFailure(prev, slug, err)),
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
    // Nothing typed lives on this pane, so the strip's own notice may hand off.
    askAgent: true,
    pending: pendingSlugs.has(slug),
    // Cancel retires this row's failure with the attempt it describes, and only
    // this row's: backing out should not leave a standing red beside a copy that
    // is still there, nor touch a sibling's.
    onCancel: () => { setConfirmSlug(null); setFailedSlugs((prev) => withoutFailure(prev, slug)) },
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
      {/* Not the pane header's `subtitle` slot, although one exists: that slot
          is unconditional, and this sentence must leave when the empty state
          arrives. */}
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
        <Card className="flex flex-col items-center gap-3 p-6" data-testid="library-error">
          <AwsErrorNotice
            askAgent
            error={listQ.error}
            message={i18nT('apps.awsControl.console.library_list_failed')}
            className="w-full"
          />
          <Btn onClick={() => listQ.refetch()} data-testid="library-retry">
            <RefreshCw size={13} />
            {i18nT('apps.awsControl.console.retry')}
          </Btn>
        </Card>
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
        <Card
          className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5"
          data-testid="library-local-error"
        >
          <AwsErrorNotice
            askAgent
            error={localQ.error}
            message={i18nT('apps.awsControl.console.library_local_failed')}
            variant="inline"
            className="min-w-0 flex-1"
          />
          <Btn onClick={() => localQ.refetch()} data-testid="library-local-retry">
            <RefreshCw size={13} />
            {i18nT('apps.awsControl.console.retry')}
          </Btn>
        </Card>
      )}

      {listQ.isSuccess && slugs.length === 0 && (
        <EmptyState
          icon={<Library className="h-10 w-10" aria-hidden="true" />}
          title={i18nT('apps.awsControl.console.library_empty_title')}
          subtitle={i18nT('apps.awsControl.console.library_empty_body')}
          testId="library-empty"
          /* With nothing addable the count read "(0 ready)" on a button that
             opens a picker refusing everything in it. Say that instead.

             Every branch here asserts something about the LOCAL library, so
             none may render until it answered -- otherwise a failed lookup
             produces a confident "everything is already here" built on nothing.
             Same mistake as reading orphan-hood out of an empty map. */
          action={
            !localAnswered || nothingLocalYet ? undefined : pushable.length > 0 ? (
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
            )
          }
        />
      )}

      {slugs.length > 0 && mode === 'grid' && (
        <div data-testid="library-grid">
          <div className="grid items-start gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}>
            {slugs.map((slug) => (
              <LibraryCloudCard
                key={slug}
                slug={slug}
                local={bySlug.get(slug)}
                localAnswered={localAnswered}
                confirm={confirmSlug === slug ? confirmFor(slug, bySlug.get(slug)?.name || slug) : null}
                failed={failedSlugs.has(slug)}
                failedWith={failedSlugs.get(slug)}
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
                      <IconButton
                        aria-label={i18nT('apps.awsControl.console.library_actions')}
                        data-testid="library-more"
                      >
                        <MoreHorizontal className="h-3.5 w-3.5" />
                      </IconButton>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => askRemove(slug)} data-testid="library-remove">
                        <Trash2 size={13} />{i18nT('apps.awsControl.console.library_remove')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                {failedSlugs.has(slug) && (
                  <div className="px-3 pb-2">
                    <AwsErrorNotice
                      askAgent
                      error={failedSlugs.get(slug)}
                      message={i18nT('apps.awsControl.console.library_remove_failed')}
                      variant="inline"
                      testId="library-remove-error"
                    />
                  </div>
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
export function TileConfirm({ label, error, errorSource, askAgent, pending, onCancel, onConfirm, action, testId = 'drive-grid-confirm' }: {
  /* A NODE, not a string: the library's removal names the cloud folder it will
     empty, and a bucket path belongs in a <code> chip inside the sentence rather
     than flattened into it. Every existing caller passes a string, which is a
     ReactNode. */
  label: React.ReactNode
  error: string
  /** The value the failed request rejected with, so the notice can carry it to the agent. */
  errorSource?: unknown
  /** Whether the failure notice offers the hand-off. The CALLER knows whether
      anything typed is on screen around this strip; the strip does not. */
  askAgent: boolean
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
      <AwsErrorNotice askAgent={askAgent} error={errorSource} message={error} variant="inline" className="mb-2" testId={`${testId}-error`} />
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
function LibraryCloudCard({ slug, local, localAnswered, confirm, failed, failedWith, onAskRemove }: {
  slug: string
  local: LibraryArtifact | undefined
  localAnswered: boolean
  /** The open confirm's props, or null when this card's confirm is closed. */
  confirm: React.ComponentProps<typeof TileConfirm> | null
  /** Whether THIS card's own removal failed. Keyed by slug, so a sibling's
      failure never renders here and opening a sibling never erases it. */
  failed: boolean
  /** What that removal rejected with, for the agent hand-off on the notice. */
  failedWith?: unknown
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

  const SHELL = 'relative overflow-hidden rounded-lg border border-border bg-card'
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
            <IconButton
              className="bg-card/85 backdrop-blur-sm"
              aria-label={i18nT('apps.awsControl.console.library_actions')}
              data-testid="library-more"
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </IconButton>
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
        <div className="px-3 pb-2">
          <AwsErrorNotice
            askAgent
            error={failedWith}
            message={i18nT('apps.awsControl.console.library_remove_failed')}
            variant="inline"
            testId="library-remove-error"
          />
        </div>
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
  const [failedSlugs, setFailedSlugs] = useState<Failures>(new Map())
  const addOne = async (slug: string) => {
    // A retry clears the previous failure, so one card never shows both states.
    setAddingSlugs((prev) => withSlug(prev, slug))
    setFailedSlugs((prev) => withoutFailure(prev, slug))
    try {
      await awsControlApi.libraryPush(account, slug)
      // Both keys: the ledger changed (so the picker's rows restate their state)
      // and the PREFIX changed (so the folder behind this dialog has a new object
      // in it). Invalidating only the library key left the folder stale until a
      // remount.
      qc.invalidateQueries({ queryKey: ['aws-control', 'library', account] })
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })
    } catch (err) {
      setFailedSlugs((prev) => withFailure(prev, slug, err))
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
        className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-label={i18nT('apps.awsControl.console.library_add')}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <h3 className="text-sm font-semibold text-text-strong">
            {i18nT('apps.awsControl.console.library_add')}
          </h3>
          <IconButton
            onClick={onClose}
            aria-label={i18nT('apps.awsControl.console.close')}
            data-testid="library-add-close"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </div>

        {/* The disclosure has to be HERE, where the adding happens: adding fills
            storage the account pays for, and the empty-state button opens this
            dialog directly, so a warning the reader never passes is not a
            warning.

            The COST is all it says, and that constraint is load-bearing: the
            cards below carry a Remove control, so any claim about removal being
            unavailable would be disproved by a button in the same dialog on
            every open, and a banner a button contradicts costs the reader their
            trust in both.

            Body tone, not muted: this is the dialog's only cost disclosure, and
            it was the most de-emphasised line in it. */}
        <p className="border-b border-border px-4 py-2 text-[12px] leading-snug text-text" data-testid="library-add-oneway">
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
            <div className="flex flex-col items-center gap-3 py-6" data-testid="library-add-error">
              <AwsErrorNotice
                askAgent
                error={libQ.error}
                message={i18nT('apps.awsControl.console.library_local_failed')}
                className="w-full"
              />
              <Btn onClick={() => libQ.refetch()} data-testid="library-add-retry">
                <RefreshCw size={13} />
                {i18nT('apps.awsControl.console.retry')}
              </Btn>
            </div>
          )}
          {/* Two different "nothing": an EMPTY library (the first-run path,
              nothing was searched) and a search or chip that matched nothing.
              "No artifacts match." told a reader with zero artifacts that a
              search they never ran had failed. */}
          {libQ.data && artifacts.length === 0 && (
            <p className="py-6 text-center text-[13px] text-muted" data-testid="library-add-empty">
              {i18nT('apps.awsControl.console.library_add_empty')}
            </p>
          )}
          {libQ.data && artifacts.length > 0 && shown.length === 0 && (
            <p className="py-6 text-center text-[13px] text-muted" data-testid="library-add-none">
              {i18nT('apps.awsControl.console.library_add_none')}
            </p>
          )}
          {shown.length > 0 && (
            <div>
              <div className="grid items-start gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}>
                {shown.map((a) => (
                  <PickerCard
                    key={a.slug}
                    artifact={a}
                    onPush={() => { void addOne(a.slug) }}
                    pushing={addingSlugs.has(a.slug)}
                    failed={failedSlugs.has(a.slug)}
                    failedWith={failedSlugs.get(a.slug)}
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
  artifact, onPush, pushing, failed, failedWith,
}: {
  artifact: LibraryArtifact
  onPush: () => void
  pushing: boolean
  failed: boolean
  /** What the failed add rejected with, for the agent hand-off on the notice. */
  failedWith?: unknown
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
    <div className="overflow-hidden rounded-lg border border-border bg-card" data-testid="library-tile">
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
          <AwsErrorNotice
            askAgent
            error={failedWith}
            message={i18nT('apps.awsControl.console.library_push_failed')}
            variant="inline"
            className="mt-2"
            testId="library-push-error"
          />
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

/** Search hits carry no Kind column: the full relative key already names the
 *  extension, and the header's job here is to keep the result rows on the same
 *  grid the folder listing uses so a search does not read as a different page. */
const SEARCH_COLUMNS: LibraryColumn[] = [
  { key: '', label: 'apps.awsControl.console.col_name', className: 'min-w-[200px]' },
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

/** How long the row a search hit landed on stays marked, counted from the
 *  moment the row APPEARS (the listing behind "Open containing folder" is a
 *  CLI round-trip, so counting from the click could spend the window before
 *  there is anything to mark). Long enough to find with the eye, short enough
 *  that it never reads as a selection. */
const HIT_HIGHLIGHT_MS = 4000

/** A sentence for the reader plus, when a request failed, what it rejected
 *  with — so the notice can hand the agent the real refusal. A client-side
 *  name check has no `error`; the sentence is the whole story. */
type Failure = { message: string; error?: unknown }

/* Preview routes by EXTENSION, not by fetching first: the two transports
   differ. Media (img/video/audio/iframe tags) load the presigned URL directly
   — those tags are exempt from CORS, which a browser fetch of the same URL is
   not (the bucket carries no CORS config). Text goes through the gateway's
   preview endpoint for the same reason. Anything else gets an honest
   "download to view" instead of a broken pane. */
const PREVIEW_IMAGE = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif'])
const PREVIEW_VIDEO = new Set(['mp4', 'webm', 'm4v', 'mov'])
const PREVIEW_AUDIO = new Set(['mp3', 'wav', 'm4a', 'ogg', 'flac'])
const PREVIEW_TEXT = new Set([
  'txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'log', 'yaml', 'yml', 'xml', 'html', 'css',
  'js', 'ts', 'tsx', 'jsx', 'py', 'sh', 'toml', 'ini', 'cfg', 'sql', 'go', 'rs', 'java', 'rb',
])

type PreviewKind = 'image' | 'video' | 'audio' | 'pdf' | 'text' | 'none'

function previewKind(key: string): PreviewKind {
  const ext = key.includes('.') ? (key.split('.').pop() ?? '').toLowerCase() : ''
  if (PREVIEW_IMAGE.has(ext)) return 'image'
  if (PREVIEW_VIDEO.has(ext)) return 'video'
  if (PREVIEW_AUDIO.has(ext)) return 'audio'
  if (ext === 'pdf') return 'pdf'
  if (PREVIEW_TEXT.has(ext)) return 'text'
  return 'none'
}

/** In-place file preview. Same scrim/panel/focus-trap shape as
 *  AddFromArtifactsDialog — no third dialog grammar. */
function PreviewDialog({
  account, entry, onDownload, downloadError, onClose,
}: {
  account: string
  entry: { key: string; size: number }
  onDownload: (key: string) => void
  /** The pane's download failure, when the click came from THIS dialog's
   *  header: the pane's own notice renders behind the dialog's scrim, so a
   *  failed download from here would show only a tab flashing closed. */
  downloadError: Failure | null
  onClose: () => void
}) {
  const kind = previewKind(entry.key)
  const panelRef = useRef<HTMLDivElement>(null)
  const backdropDown = useRef(false)
  useDialogFocusTrap(panelRef, onClose)
  const [mediaError, setMediaError] = useState(false)
  const isMedia = kind === 'image' || kind === 'video' || kind === 'audio' || kind === 'pdf'
  const urlQ = useQuery({
    queryKey: ['aws-control', 'drive-preview-url', account, entry.key],
    queryFn: () => awsControlApi.driveDownload(account, 'drive', entry.key),
    enabled: isMedia,
    // The presign is minted per open on purpose: it is short-lived, and a
    // cached URL that outlives its signature renders as a broken image.
    gcTime: 0,
    staleTime: 0,
    retry: false,
  })
  /* The download presign is a 60-second grant sized for one click, and a
     <video>/<audio> keeps issuing ranged GETs for as long as it plays -- so a
     clip longer than the grant hits S3 403 mid-file. The element reports that
     as a media error; the response is to re-mint the URL (another short grant,
     not a longer one) and resume from where playback stopped. Each successful
     resume re-arms the re-mint, so a clip spanning many grants keeps playing;
     what stops it is two errors in a row with no media loaded between them --
     that is a URL that never worked, not a grant that ran out. */
  const remintPendingRef = useRef(false)
  const resumeAtRef = useRef(0)
  const onMediaError = (el?: HTMLMediaElement) => {
    if (remintPendingRef.current) { setMediaError(true); return }
    remintPendingRef.current = true
    resumeAtRef.current = el?.currentTime ?? 0
    void urlQ.refetch()
  }
  const onMediaReady = (e: { currentTarget: HTMLMediaElement }) => {
    remintPendingRef.current = false
    if (resumeAtRef.current > 0) {
      e.currentTarget.currentTime = resumeAtRef.current
      resumeAtRef.current = 0
    }
  }
  const textQ = useQuery({
    queryKey: ['aws-control', 'drive-preview-text', account, entry.key],
    queryFn: () => awsControlApi.drivePreview(account, 'drive', entry.key),
    enabled: kind === 'text',
    retry: false,
  })
  const name = entry.key.split('/').pop() ?? entry.key
  const dir = entry.key.includes('/') ? entry.key.slice(0, entry.key.lastIndexOf('/')) : ''
  const loading = kind === 'text' ? textQ.isLoading : isMedia ? urlQ.isLoading : false
  const failed = mediaError || (kind === 'text' ? textQ.isError : isMedia ? urlQ.isError : false)
  /* A `.pdf` key is only renderable when S3 serves it as a PDF. Objects
     uploaded before content types were set come back as octet-stream, which
     the sandboxed iframe (no allow-downloads) can neither render nor hand to
     the browser -- it shows an empty frame and fires no error. The HEAD behind
     the presign carries the stored type, so those route to the same "cannot
     be previewed, download it" fallback an unknown extension gets. A missing
     type (null) is left to the frame: S3 always records one, so null means
     the field was not reported, not that the object is wrong. */
  const pdfNotRenderable =
    kind === 'pdf' && typeof urlQ.data?.contentType === 'string' &&
    !urlQ.data.contentType.toLowerCase().startsWith('application/pdf')
  const unsupported = kind === 'none' || pdfNotRenderable
  const url = urlQ.data?.url

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 sm:p-8"
      data-testid="drive-preview-dialog"
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) onClose(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-label={name}
      >
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <FileText size={14} className="shrink-0 text-muted" aria-hidden="true" />
            <h3 className="flex min-w-0 items-baseline gap-1.5 text-sm font-semibold text-text-strong">
              <span className="truncate">{name}</span>
              {/* Opened from search, two same-named hits from different folders
                  would be indistinguishable once the dialog is up; the folder
                  rides along muted whenever the key has one. */}
              {dir && (
                <span className="truncate text-[11px] font-normal text-muted" data-testid="drive-preview-dir">{dir}/</span>
              )}
            </h3>
            <span className="shrink-0 text-[11px] text-muted">{fmtBytes(entry.size)}</span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Btn onClick={() => onDownload(entry.key)} data-testid="drive-preview-download">
              <Download size={13} />{i18nT('apps.awsControl.console.download')}
            </Btn>
            <IconButton
              onClick={onClose}
              aria-label={i18nT('apps.awsControl.console.close')}
              data-testid="drive-preview-close"
            >
              <X className="h-4 w-4" />
            </IconButton>
          </div>
        </div>
        <div className="min-h-[160px] overflow-auto p-4">
          <AwsErrorNotice
            error={downloadError?.error}
            message={downloadError?.message}
            askAgent
            className="mb-3"
            testId="drive-preview-download-error"
          />
          {loading && <ContentSkeleton rows={4} />}
          {/* Two different "nothing to show". An unsupported TYPE is a status,
              not a failure — nothing was tried — so it stays plain text. A
              failed load is an error and goes through the shared notice like
              every other failure in this app: Try again re-issues the read
              (the presign for media, the gateway read for text), and the
              hand-off carries the thrown value when there is one. The dialog
              holds no draft, so the hand-off is always offered. */}
          {!loading && !failed && unsupported && (
            <p className="text-[13px] text-muted" data-testid="drive-preview-fallback">
              {i18nT('apps.awsControl.console.preview_unsupported')}
            </p>
          )}
          {!loading && failed && (
            <AwsErrorNotice
              error={kind === 'text' ? textQ.error : urlQ.error}
              message={i18nT('apps.awsControl.console.preview_failed')}
              askAgent
              onRetry={() => {
                setMediaError(false)
                remintPendingRef.current = false
                void (kind === 'text' ? textQ.refetch() : urlQ.refetch())
              }}
              testId="drive-preview-error"
            />
          )}
          {!loading && !failed && kind === 'image' && url && (
            // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is a load-failure hook, not an interaction
            <img
              src={url}
              alt={name}
              className="mx-auto max-h-[70vh] max-w-full object-contain"
              onError={() => onMediaError()}
              data-testid="drive-preview-image"
            />
          )}
          {!loading && !failed && kind === 'video' && url && (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- user files carry no caption track
            <video src={url} controls aria-label={name} className="mx-auto max-h-[70vh] max-w-full" onError={(e) => onMediaError(e.currentTarget)} onLoadedMetadata={onMediaReady} data-testid="drive-preview-video" />
          )}
          {!loading && !failed && kind === 'audio' && url && (
            // eslint-disable-next-line jsx-a11y/media-has-caption -- user files carry no caption track
            <audio src={url} controls aria-label={name} className="w-full" onError={(e) => onMediaError(e.currentTarget)} onLoadedMetadata={onMediaReady} data-testid="drive-preview-audio" />
          )}
          {!loading && !failed && kind === 'pdf' && !pdfNotRenderable && url && (
            // Only reached when the stored Content-Type says PDF (or was not
            // reported): an octet-stream `.pdf` is routed to the unsupported
            // fallback above instead of an empty frame. The empty sandbox is
            // load-bearing: the extension picks this branch, so a `.pdf` key
            // holding HTML would otherwise run script and could navigate the
            // top window. Rendering a PDF needs no sandbox permission.
            <iframe src={url} title={name} sandbox="" className="h-[70vh] w-full rounded border border-border" data-testid="drive-preview-pdf" />
          )}
          {!loading && !failed && kind === 'text' && textQ.data && (
            <>
              {textQ.data.truncated && (
                <p className="mb-2 text-[11px] text-muted" data-testid="drive-preview-truncated">
                  {i18nT('apps.awsControl.console.preview_truncated')}
                </p>
              )}
              {/* The redactor rewrites the text silently; a reader checking a
                  config file would otherwise take the masked value for the
                  file's own bytes. NOT the truncation note's muted weight: that
                  one says "there is more below", this one changes the meaning
                  of what is shown, so it reads at body weight. */}
              {textQ.data.redacted && (
                <p className="mb-2 text-[12px] font-medium text-text" data-testid="drive-preview-redacted">
                  {i18nT('apps.awsControl.console.preview_redacted')}
                </p>
              )}
              <pre className="whitespace-pre-wrap break-words text-[12px] leading-relaxed text-text" data-testid="drive-preview-text">
                {textQ.data.content}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export function DriveSectionView({ account, bucket }: { account: string; bucket: string }) {
  const qc = useQueryClient()
  const [mode, setMode] = useViewMode('drive', 'list')
  const [path, setPath] = useState('')
  const [share, setShare] = useState<{ key: string } | null>(null)
  const [uploadError, setUploadError] = useState<Failure | null>(null)
  /** Keyed by the object whose download failed, so the preview dialog shows
   *  only ITS failure: a lingering error from file A must not surface inside
   *  a later preview of file B as if B's download had failed. */
  const [downloadError, setDownloadError] = useState<(Failure & { key: string }) | null>(null)
  const [preview, setPreview] = useState<{ key: string; size: number } | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  /** Mirror of `renaming` for the rename mutation's callbacks, which fire
   *  after the request and would otherwise read the row that was open when
   *  the mutation was created, not the one open now. */
  const renamingRef = useRef<string | null>(null)
  useEffect(() => {
    renamingRef.current = renaming
  }, [renaming])
  const [renameValue, setRenameValue] = useState('')
  const [renameError, setRenameError] = useState('')
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  useEffect(() => {
    // Debounced, not immediate: each keystroke would otherwise fire a full
    // section walk on the backend. An EMPTIED box is the exception -- there is
    // no walk to save, and waiting would leave the old query's hits sitting
    // under an empty field for a beat.
    if (!query.trim()) {
      setDebouncedQuery('')
      return
    }
    const t = setTimeout(() => setDebouncedQuery(query.trim()), 300)
    return () => clearTimeout(t)
  }, [query])
  /** The file a search hit's "Open containing folder" landed on. The listing
   *  scrolls it into view and marks it for a few seconds so the reader is not
   *  left re-finding it by eye in a large folder. Cleared on a timer that runs
   *  from the moment the ROW appears, by the next search, and by navigating to
   *  a folder that is not the file's own. */
  const [highlightKey, setHighlightKey] = useState<string | null>(null)
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const highlightRef = useCallback((node: HTMLElement | null) => {
    // A callback ref rather than an effect: the row mounts only once the
    // folder's listing has loaded -- a CLI round-trip that can take longer
    // than the whole window -- so both the scroll and the clock start when
    // the element appears, not when the key was set. A clock started at the
    // click would run out during a slow load and drop the marker before it
    // was ever seen.
    if (!node) return
    node.scrollIntoView?.({ block: 'center' })
    // Focus follows too: the menu trigger the reader activated unmounted with
    // the search view, so keyboard focus has dropped to <body>, and the accent
    // ring is a cue assistive technology never announces. The row carries
    // tabIndex={-1} (see highlightProps) so it can take focus without joining
    // the tab order.
    node.focus({ preventScroll: true })
    if (highlightTimer.current) clearTimeout(highlightTimer.current)
    highlightTimer.current = setTimeout(() => setHighlightKey(null), HIT_HIGHLIGHT_MS)
  }, [])
  useEffect(
    () => () => {
      if (highlightTimer.current) clearTimeout(highlightTimer.current)
    },
    [],
  )
  useEffect(() => {
    // The marker belongs to one folder. Landing anywhere else (crumbs, a
    // folder tile) retires it, so a listing that never loaded cannot leave a
    // stale ring waiting for a later visit.
    if (highlightKey && highlightKey.split('/').slice(0, -1).join('/') !== path) {
      setHighlightKey(null)
    }
  }, [path, highlightKey])
  const highlighted = (key: string) => highlightKey === key
  const highlightProps = (key: string) =>
    highlighted(key) ? { ref: highlightRef, 'data-highlighted': 'true', tabIndex: -1 } : {}
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  /** Mirror of `confirmDelete` for the delete mutation's callbacks, which fire
   *  after the request and must know which row's strip is open NOW. */
  const confirmDeleteRef = useRef<string | null>(null)
  useEffect(() => {
    confirmDeleteRef.current = confirmDelete
  }, [confirmDelete])
  const [confirmFolder, setConfirmFolder] = useState<string | null>(null)
  const [newFolder, setNewFolder] = useState('')
  /** Folder-name input is a disclosure: visible only after "New folder". */
  const [creatingFolder, setCreatingFolder] = useState(false)
  const [folderError, setFolderError] = useState('')
  /** The "New folder" button the disclosure replaces. When the disclosure
   *  closes it unmounts the input and both buttons, and focus falls to `<body>`
   *  unless it is handed somewhere — so it goes back to the control that opened
   *  it, which is where the reader was. The effect runs on the close EDGE
   *  only: on the first render nothing was open, and a reader who never used
   *  the keyboard must not have their focus yanked to a toolbar button. */
  const folderToggleRef = useRef<HTMLButtonElement>(null)
  const wasCreatingFolder = useRef(false)
  useEffect(() => {
    if (wasCreatingFolder.current && !creatingFolder) {
      folderToggleRef.current?.focus({ preventScroll: true })
    }
    wasCreatingFolder.current = creatingFolder
  }, [creatingFolder])
  /** The file a "Move to folder…" picker is open for, or null. */
  const [moveTarget, setMoveTarget] = useState<string | null>(null)
  /** Who opened the row menu that is closing, and whether one of its items
   *  opened a dialog. Two Radix behaviours meet here. Item select is
   *  dispatched with `flushSync`, so a dialog set from `onSelect` mounts in a
   *  commit where the menu is STILL trapping focus — the dialog focuses itself
   *  and the trap yanks focus straight back into a menu that then unmounts,
   *  stranding it on `body`. So the open is deferred one macrotask, past the
   *  menu's own close commit. Then Radix restores focus to the trigger one
   *  macrotask after the content unmounts — by which time the dialog owns
   *  focus — so when an item opened a dialog that restore is suppressed, and
   *  the dialog hands focus back to the opener itself on close. */
  const menuOpenerRef = useRef<HTMLElement | null>(null)
  const menuOpenedDialogRef = useRef(false)
  /** Recorded from the trigger's OWN pointer/keyboard event, not from
   *  `document.activeElement` at open time: Safari does not focus a button on
   *  pointer click, so the active element there would still be whatever had
   *  focus before, and the dialog would hand focus back to the wrong place. */
  const rememberMenuOpener = (e: React.SyntheticEvent<HTMLElement>) => {
    menuOpenerRef.current = e.currentTarget
  }
  const skipRestoreIfDialog = (e: Event) => {
    if (!menuOpenedDialogRef.current) return
    e.preventDefault()
    menuOpenedDialogRef.current = false
  }
  const openShare = (key: string) => {
    menuOpenedDialogRef.current = true
    setTimeout(() => setShare({ key }), 0)
  }
  const openMove = (key: string) => {
    menuOpenedDialogRef.current = true
    moveDialogOpenRef.current = true
    // A refusal from an earlier drag belongs to that drag, not to the picker
    // that is about to open.
    setMoveError(null)
    setTimeout(() => setMoveTarget(key), 0)
  }
  const returnFocusToOpener = () => menuOpenerRef.current?.focus({ preventScroll: true })
  /** Whether the Move picker is on screen, readable from a mutation callback
   *  that may fire after the reader has already dismissed it. */
  const moveDialogOpenRef = useRef(false)
  /** The one way the Move picker closes — Escape, X, backdrop, or a landed
   *  move. Never refused: a move still in flight keeps running without it. */
  const closeMoveDialog = () => {
    moveDialogOpenRef.current = false
    setMoveTarget(null)
    setMoveError(null)
    returnFocusToOpener()
  }
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
  /** Whether a notice on THIS pane may hand off to the agent. The folder-name
   *  field and an open rename editor are the two drafts the pane can hold, and
   *  every notice here — a failed listing, search, upload, move, download or
   *  delete — shares the screen with them while either is open. Gated on the
   *  disclosure / editor being open rather than on the field having text, so
   *  the button does not flicker in and out as the reader types. */
  const handOff = !creatingFolder && renaming === null
  /* How many objects the last folder delete actually removed. One click can
     remove far more than one file, and the count is only knowable AFTER the
     fact - the response carries it, while a figure shown BEFORE consent would
     cost a second full recursive listing of the prefix. So the page reports
     what was removed rather than pretending to predict it. */
  const [deleted, setDeleted] = useState<{ count: number; name: string; inPath: string } | null>(null)
  /** The "Deleted N files" line belongs to the folder it happened in — the
   *  deleted folder's PARENT, taken from the request rather than from the path
   *  at response time, so a reader who navigated while the delete ran does not
   *  find the count in the folder they landed in. It shows only while that
   *  folder is the current path, and leaving the folder RETIRES it (rather than
   *  merely hiding it), so coming back later does not replay it. */
  const deletedHere = deleted && deleted.inPath === path ? deleted : null
  useEffect(() => {
    setDeleted((d) => (d && d.inPath !== path ? null : d))
  }, [path])
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
  /* A search hit can sit past the first listing page. The marker and the
     scroll fire when its ROW mounts, and a row on page three never mounts
     until the reader presses Load more -- so while a marker is pending for
     this folder, pages are pulled one at a time until the row is in hand or
     the folder runs out. Bounded by the folder itself; a key that is not in
     it (deleted between the search and the click) stops at the last page. */
  useEffect(() => {
    if (!highlightKey || !listQ.data) return
    if (highlightKey.split('/').slice(0, -1).join('/') !== path) return
    if (files.some((f) => f.key === highlightKey)) return
    if (listQ.hasNextPage && !listQ.isFetchingNextPage) void listQ.fetchNextPage()
    // `files` is derived from listQ.data; listing it would re-run this on every
    // render for the same pages.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightKey, path, listQ.data, listQ.hasNextPage, listQ.isFetchingNextPage])
  // Search results are a second view of the same objects: a delete, upload
  // or folder change from the results must refresh them too, or the hit the
  // reader just removed stays on screen.
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })
    qc.invalidateQueries({ queryKey: ['aws-control', 'drive-search', account] })
  }

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
    onError: (e: unknown, vars: { file: File; key: string }) => {
      setUploadError({
        message: i18nT('apps.awsControl.console.drive_upload_failed', {
          name: vars.key.split('/').pop() ?? vars.key,
        }),
        error: e,
      })
    },
  })
  const deleteMut = useMutation({
    mutationFn: (key: string) => awsControlApi.driveDelete(account, 'drive', key),
    onMutate: () => setPageError(null),
    onSuccess: invalidate,
    onError: (e: unknown, key) => {
      // The inline notice lives in the confirmation strip under the row. When
      // that strip is gone by the time the request fails -- the view swapped
      // to search results, or the reader moved the confirmation to another
      // row -- the failure has no surface of its own and goes to the page-level
      // notice, named, so a file that is still there never looks deleted.
      if (confirmDeleteRef.current === key) return
      setPageError({
        message: i18nT('apps.awsControl.console.delete_failed_named', {
          name: key.split('/').pop() ?? key,
        }),
        error: e,
      })
    },
  })
  /** The one way a delete confirmation strip opens. The strip renders
   *  `deleteMut.error` inline, and the mutation is shared by every row, so a
   *  failed delete on file A would otherwise sit pre-rendered under a strip
   *  opened later on file B -- "Delete failed" before anything was tried. The
   *  reset is skipped while a delete is still flying: it would drop that
   *  request's pending state, not the stale error this is for. */
  const openConfirmDelete = (key: string) => {
    if (!deleteMut.isPending) deleteMut.reset()
    setConfirmDelete(key)
  }
  /** Whether the shared delete mutation's error belongs to THIS row. Opening a
   *  strip on file B while A's delete is still flying is allowed; when A then
   *  fails, its error goes to the page-level notice (onError above) and must
   *  not also surface under B's strip just because the mutation is shared. */
  const deleteFailedFor = (key: string) => deleteMut.isError && deleteMut.variables === key
  const folderCreateMut = useMutation({
    mutationFn: (name: string) =>
      awsControlApi.driveFolderCreate(account, 'drive', path ? `${path}/${name}` : name),
    onSuccess: () => { setNewFolder(''); setCreatingFolder(false); invalidate() },
  })
  const folderDeleteMut = useMutation({
    mutationFn: (folder: string) => awsControlApi.driveFolderDelete(account, 'drive', folder),
    // The count belongs where the folder WAS -- its parent -- not wherever the
    // reader is when the response lands.
    onSuccess: (res, folder) => {
      const parts = folder.split('/')
      setDeleted({ count: res.objects, name: parts[parts.length - 1] ?? folder, inPath: parts.slice(0, -1).join('/') })
      invalidate()
    },
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
    setUploadError(null)
    if (!KEY_SEGMENT.test(file.name)) {
      setUploadError({ message: i18nT('apps.awsControl.console.drive_bad_name') })
      return
    }
    uploadMut.mutate({ file, key: path ? `${path}/${file.name}` : file.name })
  }

  /** Upload dropped OS files into `folder` ('' = the open folder's own path).
   *  Invalid names surface through the same strip the picker uses — but NAMED:
   *  a 10-file drop can fail on one file while the rest upload, and the
   *  picker's anonymous "that file name" would not say which one. */
  const uploadDropped = (list: FileList, folder: string) => {
    setUploadError(null)
    for (const file of Array.from(list)) {
      if (!KEY_SEGMENT.test(file.name)) {
        setUploadError({ message: i18nT('apps.awsControl.console.drive_bad_name_named', { name: file.name }) })
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
  /** The most recent refused move. One slot is enough: moves are serialized
   *  (see `moveBusy`), so a refusal can only ever belong to the last move. */
  const [moveError, setMoveError] = useState<Failure | null>(null)
  /** A delete or rename that failed after its row's own strip was gone. Kept
   *  apart from `moveError` because the Move picker renders that slot as its
   *  refusal: sharing it would show "could not delete X" inside a picker that
   *  is moving Y. The page strip carries this one whether or not the picker
   *  is open. */
  const [pageError, setPageError] = useState<Failure | null>(null)

  const moveMut = useMutation({
    mutationFn: ({ fromKey, toKey }: { fromKey: string; toKey: string }) =>
      awsControlApi.driveMove(account, 'drive', fromKey, toKey),
    onSuccess: () => {
      setMoveError(null)
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive-list', account] })
      invalidate()
    },
    onError: (e: unknown) => {
      // Two refusals worth their own sentences: share_active (the source has
      // a live share link — moving would 404 it) and destination_exists (the
      // destination folder already holds this name; never overwritten).
      const err = e instanceof AwsControlError ? e : null
      setMoveError({
        message: i18nT(
          err?.message === 'share_active'
            ? 'apps.awsControl.console.move_shared'
            : err?.status === 409
              ? 'apps.awsControl.console.move_conflict'
              : 'apps.awsControl.console.move_failed'),
        error: e,
      })
    },
  })

  /** ONE move at a time. While a copy runs, the other rows' "Move to
   *  folder…" items are disabled, rows are not draggable, and a drop is
   *  ignored — so there is never a second in-flight move whose busy marker,
   *  refusal, or completion could be confused with the first one's. The
   *  original ask was a visible busy state for the move in flight, not
   *  concurrent moves. */
  const moveBusy = moveMut.isPending
  /** The source key of the move in flight, or null. Exact, because at most
   *  one move runs at a time and `variables` is read only while pending. */
  const movingKey = moveBusy ? moveMut.variables?.fromKey ?? null : null

  /** The wire format an internal file drag travels as. A custom MIME keeps OS
   *  file drops (types includes 'Files') and internal moves distinguishable. */
  const DRAG_MIME = 'application/x-drive-object-key'

  /* Rename IS a move with the directory held fixed — the backend endpoint is
     the same one, so every move guarantee (no overwrite, live-share refusal)
     applies to a rename for free; only the refusal WORDING is rename's own.
     Both callbacks are scoped to the row that STARTED the rename: the editor
     may have moved on to another row while this one was in flight, and an
     unconditional close would throw away the name being typed there, while
     an unconditional error would land under the wrong file. */
  const renameMut = useMutation({
    mutationFn: ({ fromKey, toKey }: { fromKey: string; toKey: string }) =>
      awsControlApi.driveMove(account, 'drive', fromKey, toKey),
    onMutate: () => setPageError(null),
    onSuccess: (_data, { fromKey }) => {
      if (renamingRef.current === fromKey) {
        setRenaming(null)
        setRenameError('')
      }
      qc.invalidateQueries({ queryKey: ['aws-control', 'drive-list', account] })
      // A rename from a search hit must re-run the search so the new name
      // (or the hit's disappearance, if it no longer matches) shows.
      invalidate()
    },
    onError: (e: unknown, { fromKey }) => {
      const err = e instanceof AwsControlError ? e : null
      // Same error CODES as move (it is the move endpoint), but the sentences
      // name the verb the user pressed: a failed rename that talks about a
      // "destination folder" reads as a move they never made.
      const reason = i18nT(
        err?.message === 'share_active'
          ? 'apps.awsControl.console.rename_shared'
          : err?.status === 409
            ? 'apps.awsControl.console.rename_conflict'
            : 'apps.awsControl.console.rename_failed')
      if (renamingRef.current === fromKey) {
        setRenameError(reason)
        return
      }
      // The editor has moved to another row, so there is no strip under this
      // file to carry the message; the page-level notice names the file
      // instead, because a rename that fails without a word leaves the old
      // name in the listing looking like it was never attempted.
      setPageError({
        message: i18nT('apps.awsControl.console.rename_failed_named', {
          name: fromKey.split('/').pop() ?? fromKey,
          reason,
        }),
        error: e,
      })
    },
  })

  const openRename = (key: string) => {
    setRenaming(key)
    setRenameValue(key.split('/').pop() ?? key)
    setRenameError('')
  }

  const closeRename = () => {
    // Same in-flight guard the folder disclosure carries, scoped to the row
    // being committed: closing THAT editor mid-flight would discard the name
    // on its way to the server, but an editor opened on another row while it
    // flies is the reader's own, and closes freely.
    if (renameMut.isPending && renameMut.variables?.fromKey === renaming) return
    setRenaming(null)
    setRenameError('')
    // Another row's rename may still be flying; resetting the observer would
    // drop its pending state while the request runs on.
    if (!renameMut.isPending) renameMut.reset()
  }

  /* A query change swaps the whole view (folder listing <-> search hits), and
     any in-place editor open under a row goes with it. Left as-is, `renaming`
     would stay set for a row that no longer renders: the half-typed name is
     gone with no word, and `handOff` keeps every later notice from offering the
     agent. So the editors close with the view they belonged to -- WITHOUT the
     in-flight guard: a rename or delete still on the wire finishes regardless,
     and because its editor is gone its outcome is routed by the mutation
     callbacks to the page-level notice, where a late failure is still seen. */
  useEffect(() => {
    setRenaming(null)
    setRenameError('')
    setConfirmDelete(null)
    // A new search also retires the marker the last "Open containing folder"
    // left; the goto itself clears the query to '' which is a no-op here.
    if (debouncedQuery) setHighlightKey(null)
    // Keyed on the debounced query alone: that is the value the view switches on.
  }, [debouncedQuery])

  const commitRename = (fromKey: string) => {
    // The name is committed AS TYPED. Trimming it would silently move a file
    // whose name legitimately ends in a space (the key grammar allows one) the
    // moment its owner opens Rename and saves without touching anything — a
    // no-op that changes the key. Whitespace-only is the one shape refused,
    // and the key grammar below rejects a leading space on its own.
    const name = renameValue
    if (!name.trim()) return
    const base = fromKey.split('/').pop() ?? fromKey
    if (name === base) {
      closeRename()
      return
    }
    if (!KEY_SEGMENT.test(name)) {
      // Rename's own wording: the shared upload message says "Rename it and
      // try again", which inside the rename editor tells the reader to do the
      // thing they are already doing.
      setRenameError(i18nT('apps.awsControl.console.rename_bad_name'))
      return
    }
    const dir = fromKey.split('/').slice(0, -1).join('/')
    setRenameError('')
    renameMut.mutate({ fromKey, toKey: dir ? `${dir}/${name}` : name })
  }

  const searching = debouncedQuery.length > 0
  const searchQ = useQuery({
    queryKey: ['aws-control', 'drive-search', account, debouncedQuery],
    queryFn: () => awsControlApi.driveSearch(account, 'drive', debouncedQuery),
    enabled: searching,
    // Each refinement is a new key; without this "rep" -> "report" blanks the
    // list the user is scanning back to a skeleton for the round-trip.
    placeholderData: keepPreviousData,
  })

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
   *  the folder the file already lives in is a no-op, not an error; so is a
   *  drop while another move is still running (rows are not draggable then,
   *  but a drag that began before the gate closed can still land). */
  const moveInto = (fromKey: string, folder: string) => {
    if (moveBusy) return
    const base = fromKey.split('/').pop() ?? fromKey
    const fromDir = fromKey.split('/').slice(0, -1).join('/')
    if (fromDir === folder) return
    setMoveError(null)
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

  /** What the section carries while a search is active: the drop is swallowed
   *  (preventDefault, so the browser never navigates to a dropped file) and
   *  nothing is uploaded, because the folder it would land in is off-screen.
   *  The drag-over highlight stays off too -- a highlighted target that does
   *  nothing on release would read as a failed upload. */
  const inertDropProps = {
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault()
      e.stopPropagation()
      dragKeyRef.current = null
    },
  }

  /** Draggable wiring for a file row/tile. Not draggable while a move runs:
   *  one move at a time. */
  const dragProps = (key: string) => ({
    draggable: !moveBusy,
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
    setDownloadError(null)
    const tab = window.open('', '_blank')
    if (tab) tab.opener = null
    try {
      const { url } = await awsControlApi.driveDownload(account, 'drive', key)
      if (tab) tab.location.href = url
      else window.open(url, '_blank', 'noopener')
    } catch (e) {
      // Never leave an orphaned blank tab behind, and never rethrow: this runs
      // from an onClick with no catch, so a rethrow becomes an unhandled
      // rejection that tells the USER nothing. Report it in the row instead.
      tab?.close()
      setDownloadError({ message: i18nT('apps.awsControl.console.download_failed'), error: e, key })
    }
  }

  const crumbs = path.split('/').filter(Boolean)

  return (
    <section
      data-testid="drive-section"
      /* The section-level drop targets the OPEN folder, which search hides
         along with its crumbs -- the same invisible-destination problem the
         toolbar's Upload button has, so the drop goes INERT while searching.
         Inert, not absent: this section is the page's only dragover/drop
         preventDefault, and without one the browser answers a dropped OS file
         by navigating the tab to it, tearing down the dashboard for a gesture
         this same surface trained. */
      {...(searching ? inertDropProps : dropProps(path))}
      className={!searching && dropTarget === path ? 'rounded-lg ring-1 ring-inset ring-accent' : undefined}
    >
      <PaneHeader icon={<FolderClosed size={18} />} title={i18nT('apps.awsControl.console.section_files')} actions={
        <div className="flex flex-wrap items-center gap-2">
        {/* The shared `SearchInput`, not a hand-rolled Input plus an absolutely
            positioned Search glyph: the glyph, its offset and the field metrics
            live in one place, and a second spelling of them is how this toolbar
            drifted from every other search box in the app. The clear button
            stays a sibling in the relative wrapper, which is the same idiom
            ChatSidebar's search boxes use -- `[&>input]:pr-8` buys it the room,
            because the primitive owns the input's own padding. */}
        <div className="relative w-full sm:w-[260px]">
          <SearchInput
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') setQuery('') }}
            placeholder={i18nT('apps.awsControl.console.search_files')}
            aria-label={i18nT('apps.awsControl.console.search_files')}
            className="w-full [&>input]:pr-8"
            data-testid="drive-search-input"
          />
          {query && (
            <IconButton
              onClick={() => setQuery('')}
              className="absolute right-1.5 top-1/2 -translate-y-1/2"
              aria-label={i18nT('apps.awsControl.console.search_clear')}
              data-testid="drive-search-clear"
            >
              <X className="h-3 w-3" />
            </IconButton>
          )}
        </div>
        {/* Search replaces the folder view and hides the crumbs, so the two
            folder-scoped WRITE controls go with them: an upload or a new folder
            mid-search would land in a folder the reader cannot see, succeed,
            and show nothing. The view toggle goes too -- results are always a
            table, so a toggle that visibly does nothing reads as broken. The
            search box stays: clearing it is how the reader gets back. */}
        {!searching && <ViewModeToggle section="drive" mode={mode} onChange={setMode} />}
        {/* The name field appears when the reader ASKS to create a folder.
            Parked permanently in the toolbar it was two dead controls (an empty
            input and a disabled button) on every visit that isn't about
            folders — which is most of them. Escape, Cancel, or blurring the
            empty field puts the toolbar back; Upload hides while creating so
            the expanded row stays one action group of two buttons. */}
        {searching ? null : creatingFolder ? (
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
            <Btn ref={folderToggleRef} onClick={() => setCreatingFolder(true)} data-testid="drive-folder-toggle">
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

      {/* A rejected NAME is a client-side check that never reached AWS: there is
          no report and nothing for the agent to read, so those two carry no
          hand-off. A wire failure on the same strip (`error` set) keeps it. */}
      <AwsErrorNotice
        error={uploadError?.error}
        message={uploadError?.message}
        askAgent={handOff && uploadError?.error !== undefined}
        className="mb-2"
        testId="drive-upload-error"
      />
      {/* While the Move picker is open it reports the refusal itself; the strip
          would only double the same sentence behind the modal. Moves are
          serialized, so the refusal on screen is always the picker's own. */}
      <AwsErrorNotice
        askAgent={handOff}
        error={moveTarget ? undefined : moveError?.error}
        message={moveTarget ? null : moveError?.message}
        className="mb-2"
        testId="drive-move-error"
      />
      {/* A delete or rename failure has its own notice: it is never held back by
          an open picker, and a stale move refusal above never stands in for it --
          the two are different failures and each keeps its own line. */}
      <AwsErrorNotice
        askAgent={handOff}
        error={pageError?.error}
        message={pageError?.message ?? null}
        className="mb-2"
        testId="drive-page-error"
      />
      <AwsErrorNotice message={folderError} askAgent={false} className="mb-2" testId="drive-folder-error" />
      {/* Also no hand-off: a failed create leaves the typed name in the still-open
          input, and the hand-off navigates away from it. */}
      <AwsErrorNotice
        error={folderCreateMut.error}
        message={folderCreateMut.isError ? i18nT('apps.awsControl.console.folder_create_failed') : null}
        askAgent={false}
        className="mb-2"
        testId="drive-folder-create-error"
      />
      {deletedHere && (
        <p className="mb-2 text-[12px] text-muted" data-testid="drive-folder-deleted">
          {i18nT('apps.awsControl.console.folder_deleted', { count: deletedHere.count, name: deletedHere.name })}
        </p>
      )}
      <AwsErrorNotice askAgent={handOff} error={downloadError?.error} message={downloadError?.message} className="mb-2" testId="drive-download-error" />

      {/* Breadcrumb within the section. Root plus one overflow is two sibling
          controls; the folder you are IN is text, not a third button. The
          ancestors go into the same inline overflow the file rows use, which
          keeps the jump-to-an-ancestor navigation that rendering the whole path
          as flat text would have removed. */}
      {!searching && crumbs.length > 0 && (
      <div className="mb-2 flex flex-wrap items-center gap-1 text-[12px] text-muted" data-testid="drive-crumbs">
        <button className="hover:text-text cursor-pointer bg-transparent border-none p-0" onClick={() => setPath('')}>
          {i18nT('apps.awsControl.console.section_files')}
        </button>
        {crumbs.length > 1 && (
          <span className="flex items-center gap-1">
            {' / '}
            {/* The same `ui/dropdown-menu` the row menus use, for the same
                reason: it dismisses on Escape and on an outside click and
                returns focus to its trigger. A hand-rolled `absolute` popover
                here did none of that, so this one menu behaved unlike the
                menus a few rows below it. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <IconButton
                  aria-label={i18nT('apps.awsControl.console.parent_folders')}
                  data-testid="drive-crumb-more"
                >
                  <MoreHorizontal size={14} />
                </IconButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" data-testid="drive-crumb-menu">
                {crumbs.slice(0, -1).map((c, i) => (
                  <DropdownMenuItem
                    key={i}
                    onSelect={() => setPath(crumbs.slice(0, i + 1).join('/'))}
                  >
                    <FolderClosed size={13} />{c}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </span>
        )}
        <span data-testid="drive-crumb-current">{' / '}{crumbs[crumbs.length - 1]}</span>
      </div>
      )}

      {/* Search replaces the folder view wholesale: results span the WHOLE
          section (the full relative key is shown), so rendering them beside
          one folder's crumbs would claim a scope the listing does not have. */}
      {searching && (
        <div data-testid="drive-search-results">
          {searchQ.isLoading && <ContentSkeleton rows={2} />}
          {/* A refinement ("rep" -> "report") keeps the PREVIOUS query's rows on
              screen through `keepPreviousData`, so without a pending signal
              they read as the new answer for the whole section walk. The stale
              set dims and the line below names what is happening. */}
          {searchQ.isFetching && !searchQ.isLoading && (
            <p className="mb-2 flex items-center gap-1.5 text-[11px] text-muted" data-testid="drive-search-pending" role="status">
              <RefreshCw size={11} className="animate-spin" aria-hidden="true" />
              {i18nT('apps.awsControl.console.search_pending')}
            </p>
          )}
          {/* A failed search is a READ the reader can re-issue, so it carries
              Try again like the listing's own failure, and hands off to the
              agent under the same draft gate every notice on this pane uses. */}
          <AwsErrorNotice
            error={searchQ.error}
            message={searchQ.isError ? i18nT('apps.awsControl.console.search_failed') : null}
            askAgent={handOff}
            onRetry={() => searchQ.refetch()}
            className="mb-2"
            testId="drive-search-error"
          />
          <div className={searchQ.isFetching && !searchQ.isLoading ? 'opacity-60 transition-opacity' : undefined} aria-busy={searchQ.isFetching && !searchQ.isLoading ? true : undefined} data-testid="drive-search-body">
          {/* Not an empty FOLDER: the objects exist and the query hid them, which
              is exactly the distinction `FilteredEmpty` draws (no big icon, the
              query echoed back, and a clear control right there). The clear
              callback is the same one the toolbar's X calls, so there is one way
              back rather than two. */}
          {searchQ.isSuccess && searchQ.data.results.length === 0 && (
            <FilteredEmpty
              query={debouncedQuery}
              onClear={() => setQuery('')}
              noun={i18nT('apps.awsControl.console.section_files')}
              testId="drive-search-empty"
            />
          )}
          {searchQ.isSuccess && searchQ.data.capped && (
            <p className="mb-2 text-[11px] text-muted" data-testid="drive-search-capped">
              {i18nT('apps.awsControl.console.search_capped', { count: searchQ.data.limit })}
            </p>
          )}
          {searchQ.isSuccess && searchQ.data.results.length > 0 && (
            <div className="overflow-x-auto" data-testid="drive-search-table">
              <table className="w-full border-collapse text-[13px]">
                <LibraryTableHead
                  sort={null}
                  onSort={noSort}
                  columns={SEARCH_COLUMNS}
                  actionsLabelKey="apps.awsControl.console.col_actions"
                  surface="bg"
                />
                <tbody>
                  {searchQ.data.results.map((hit) => (
                    <Fragment key={hit.key}>
                    <tr className="border-b border-border last:border-0 hover:bg-bg-hover" data-testid="drive-search-hit">
                      <td className="px-2.5 py-2">
                        <button
                          type="button"
                          onClick={() => setPreview({ key: hit.key, size: hit.size })}
                          className="flex min-w-0 max-w-full cursor-pointer items-center gap-2 border-none bg-transparent p-0 text-left text-text hover:underline"
                          title={hit.key}
                          data-testid="drive-search-open"
                        >
                          <FileText size={14} className="shrink-0 text-muted" aria-hidden="true" />
                          {/* The FULL relative key, not the basename: results
                              come from the whole section, and the path is what
                              tells two same-named files apart. The FOLDER half
                              is what truncates on a long path -- the basename
                              is what the reader searched for, so it stays
                              whole -- and the title carries the untruncated key. */}
                          <span className="flex min-w-0">
                            {hit.key.includes('/') && (
                              <span className="truncate text-muted">{hit.key.slice(0, hit.key.lastIndexOf('/') + 1)}</span>
                            )}
                            <span className="shrink-0">{hit.key.slice(hit.key.lastIndexOf('/') + 1)}</span>
                          </span>
                        </button>
                      </td>
                      <td className="px-2.5 py-2 text-muted">{fmtBytes(hit.size)}</td>
                      <td className="px-2.5 py-2 text-muted">{fmtRelative(hit.modified)}</td>
                      <td className="px-2.5 py-2">
                        {/* Same one-overflow grammar as the file rows, and the
                            SAME items: a reader who searched in order to rename
                            or delete a file must not have to leave the results
                            and re-find it. Rename, Share and Delete all key off
                            the object's own path, so they need no folder
                            context; Open containing folder is the one hit-only
                            item. The go-to action is a WORDED item -- a bare
                            folder icon on this page means "a folder object",
                            not a verb. */}
                        <div className="flex items-center justify-end gap-1">
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <IconButton
                                aria-label={i18nT('apps.awsControl.console.file_actions')}
                                onPointerDown={rememberMenuOpener}
                                onKeyDown={rememberMenuOpener}
                                data-testid="drive-search-more"
                              >
                                <MoreHorizontal className="h-3.5 w-3.5" />
                              </IconButton>
                            </DropdownMenuTrigger>
                            {/* Third of the three menus that open ShareDialog: the
                                dialog hands focus back to whichever trigger was
                                remembered, so this one must remember itself too. */}
                            <DropdownMenuContent align="end" onCloseAutoFocus={skipRestoreIfDialog}>
                              <DropdownMenuItem onSelect={() => download(hit.key)} data-testid="drive-search-download">
                                <Download size={13} />{i18nT('apps.awsControl.console.download')}
                              </DropdownMenuItem>
                              <DropdownMenuItem onSelect={() => openRename(hit.key)} data-testid="drive-search-rename">
                                <Pencil size={13} />{i18nT('apps.awsControl.console.rename')}
                              </DropdownMenuItem>
                              <DropdownMenuItem onSelect={() => openShare(hit.key)} data-testid="drive-search-share">
                                <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                onSelect={() => {
                                  setPath(hit.key.split('/').slice(0, -1).join('/'))
                                  // Mark the file the reader came for: the folder
                                  // may hold hundreds of rows, and landing on the
                                  // listing with no pointer to the hit makes them
                                  // re-find by eye what they just searched for.
                                  setHighlightKey(hit.key)
                                  setQuery('')
                                }}
                                data-testid="drive-search-goto"
                              >
                                <FolderOpen size={13} />{i18nT('apps.awsControl.console.search_goto')}
                              </DropdownMenuItem>
                              {/* The destructive item sits alone below a rule: the
                                  likeliest action from a hit (go to its folder) must
                                  not be a slip away from Delete at identical weight. */}
                              <DropdownMenuSeparator />
                              <DropdownMenuItem onSelect={() => openConfirmDelete(hit.key)} data-testid="drive-search-delete">
                                <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </td>
                    </tr>
                    {/* The same in-place editors the folder listing opens under
                        its rows, so the flow from a search hit is identical:
                        the rename commits against the hit's OWN directory, and
                        both mutations refresh the results. The editor strips
                        carry the folder rows' sticky-left viewport-width wrapper
                        too: the search table is wider (a path column), so on a
                        narrow, horizontally scrolled viewport an unpinned strip
                        would put Cancel/Rename off-screen. */}
                    {renaming === hit.key && (
                      // eslint-disable-next-line jsx-a11y/control-has-associated-label -- the row's control is the Input, which carries its own aria-label; the rule cannot see through the component wrapper
                      <tr className="border-b border-border bg-bg-elevated" data-testid="drive-rename-row">
                        <td colSpan={SEARCH_COLUMNS.length + 1} className="px-2.5 py-2">
                          <div className="sticky left-0 flex max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-2 pr-4">
                            <Input
                              value={renameValue}
                              onChange={(e) => setRenameValue(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') commitRename(hit.key)
                                if (e.key === 'Escape') closeRename()
                              }}
                              autoFocus
                              aria-label={i18nT('apps.awsControl.console.rename')}
                              className="w-full min-w-0 sm:w-[260px]"
                              data-testid="drive-rename-input"
                            />
                            {/* No hand-off: beside a live input, and the agent
                                navigation would take the half-typed name with it. */}
                            <AwsErrorNotice message={renameError} askAgent={false} variant="inline" testId="drive-rename-error" />
                            <Btn onClick={closeRename} disabled={renameMut.isPending} data-testid="drive-rename-cancel">
                              {i18nT('apps.awsControl.console.cancel')}
                            </Btn>
                            <Btn
                              primary
                              disabled={renameMut.isPending || !renameValue.trim()}
                              onClick={() => commitRename(hit.key)}
                              data-testid="drive-rename-save"
                            >
                              <Pencil size={13} />{i18nT('apps.awsControl.console.rename')}
                            </Btn>
                          </div>
                        </td>
                      </tr>
                    )}
                    {confirmDelete === hit.key && (
                      // eslint-disable-next-line jsx-a11y/control-has-associated-label -- the row's text is the confirmation sentence in the span below, one level deeper than the rule's default search depth
                      <tr className="border-b border-border bg-bg-elevated" data-testid="drive-delete-confirm">
                        <td colSpan={SEARCH_COLUMNS.length + 1} className="px-2.5 py-2">
                          <div className="sticky left-0 flex max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-2 pr-4">
                            <span className="min-w-0 flex-1 text-text">
                              {/* The FULL relative key, unlike the folder rows: search
                                  is the one view where same-named files from different
                                  folders sit side by side, and the basename alone would
                                  not say which one is about to go. */}
                              {i18nT('apps.awsControl.console.delete_confirm', { name: hit.key })}
                            </span>
                            <AwsErrorNotice
                              askAgent={handOff}
                              error={deleteFailedFor(hit.key) ? deleteMut.error : null}
                              message={deleteFailedFor(hit.key) ? i18nT('apps.awsControl.console.delete_failed') : null}
                              variant="inline"
                              className="basis-full"
                              testId="drive-delete-error"
                            />
                            <Btn onClick={() => setConfirmDelete(null)} data-testid="drive-delete-cancel">
                              {i18nT('apps.awsControl.console.cancel')}
                            </Btn>
                            <Btn
                              danger
                              disabled={deleteMut.isPending}
                              onClick={() => deleteMut.mutate(hit.key, { onSuccess: () => setConfirmDelete((cur) => (cur === hit.key ? null : cur)) })}
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
          </div>
        </div>
      )}

      {!searching && listQ.isLoading && <ContentSkeleton rows={2} />}

      {/* A failed listing is not an empty folder — the Library folder beside
          this one already says so, and this one rendered NOTHING: no skeleton,
          no rows, no empty state, no sentence. */}
      <AwsErrorNotice
        askAgent={handOff}
        error={listQ.error}
        message={listQ.isError ? i18nT('apps.awsControl.console.files_list_failed') : null}
        onRetry={() => listQ.refetch()}
        testId="drive-list-error"
      />

      {/* Empty, and said properly. "This folder is empty." inside a table with
          five headers left a reader who had just come from a Library folder
          holding 212 rows unable to tell what the two folders were FOR -- the
          question was asked in exactly those words. So the empty state names
          what belongs here and how it differs from Library, and carries the
          upload action rather than making the reader find it in the header. */}
      {!searching && listQ.isSuccess && folders.length === 0 && files.length === 0 && (
        <EmptyState
          icon={<FolderClosed className="h-10 w-10" aria-hidden="true" />}
          title={i18nT('apps.awsControl.console.files_empty_title')}
          subtitle={i18nT('apps.awsControl.console.files_empty_body')}
          testId="drive-empty"
          action={
            <Btn primary onClick={() => fileRef.current?.click()} data-testid="drive-empty-upload">
              <Upload size={13} />
              {i18nT('apps.awsControl.console.drive_upload')}
            </Btn>
          }
        />
      )}

      {/* Grid mode. A stored object has no preview we can draw without a presign
          and a fetch PER CARD, so a tile is a type glyph, its name, and its size
          -- the same thing a file manager shows for a format it cannot render.
          Every action a LIST row carries is carried here too: the view mode is a
          way of LOOKING at a folder, not a capability tier, and because the
          choice persists per section a reader who preferred tiles would
          otherwise lose Share and Delete on every future visit with nothing to
          tell them the controls existed. */}
      {!searching && mode === 'grid' && (folders.length > 0 || files.length > 0) && (
        /* `gap-3` rather than the old `-mr-3` bleed plus `mr-3 mb-3` on every
           tile: the negative margin existed only to cancel a per-tile right
           margin, and `gap` states the same gutter once without pulling the grid
           past its container. The auto-fill TEMPLATE is untouched (the source-level
           pin in DrivePage.gridAutoFill.test.tsx reads it byte-for-byte). */
        <div data-testid="drive-grid">
          <div className="grid items-start gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))' }}>
            {folders.map((name) => {
              const open = () => setPath(name)
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
                className={`flex cursor-pointer flex-col items-start gap-2 rounded-lg border border-border bg-card p-3 text-left transition-colors hover:border-border-strong hover:bg-bg-hover ${dropTarget === name ? 'border-accent bg-bg-hover' : ''}`}
                data-testid="drive-grid-folder"
              >
                <div className="flex w-full items-start justify-between gap-2">
                  {/* The glyph sits on its own `bg-bg-elevated` block, which is
                      what gives a tile its recognisable left edge in the mockup
                      -- and what tells a folder tile from a file tile at a
                      glance, since both are otherwise one flat card. */}
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-bg-elevated text-accent">
                    <FolderClosed className="h-4 w-4" aria-hidden="true" />
                  </span>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <IconButton
                        onClick={(e) => e.stopPropagation()}
                        aria-label={i18nT('apps.awsControl.console.folder_actions')}
                        data-testid="drive-grid-folder-more"
                      >
                        <MoreHorizontal className="h-3.5 w-3.5" />
                      </IconButton>
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
                <span className="text-[12px] text-muted">{i18nT('apps.awsControl.console.kind_folder')}</span>
                {confirmFolder === name && (
                  <TileConfirm
                    label={i18nT('apps.awsControl.console.folder_delete_confirm', { name: name.split('/').pop() ?? name })}
                    error={folderDeleteMut.isError ? i18nT('apps.awsControl.console.folder_delete_failed') : ''}
                    errorSource={folderDeleteMut.error}
                    askAgent={handOff}
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
                {...highlightProps(f.key)}
                className={`flex flex-col items-start gap-2 rounded-lg border bg-card p-3 ${highlighted(f.key) ? 'border-accent' : 'border-border'} ${movingKey === f.key ? 'opacity-50' : ''}`}
                aria-busy={movingKey === f.key || undefined}
                data-testid="drive-grid-file"
              >
                <div className="flex w-full items-start justify-between gap-2">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-bg-elevated text-muted">
                    <FileText className="h-4 w-4" aria-hidden="true" />
                  </span>
                  {/* ONE home for per-item actions. Download used to sit outside
                      the menu as a bare button while Share and Delete were
                      inside it, so a card offered two different grammars for
                      "act on this item" and a reader had to learn both. It is
                      the same inconsistency the Library folder's visible Remove
                      was rejected for, one component over, so it is fixed in the
                      same change rather than left to recreate the problem. */}
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <IconButton
                        aria-label={i18nT('apps.awsControl.console.file_actions')}
                        data-testid="drive-grid-more"
                        onPointerDown={rememberMenuOpener}
                        onKeyDown={rememberMenuOpener}
                      >
                        <MoreHorizontal className="h-3.5 w-3.5" />
                      </IconButton>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" onCloseAutoFocus={skipRestoreIfDialog}>
                      {/* `onSelect` is dispatched synchronously from the item's
                          own click handler, so the window.open inside
                          `download` still runs within the user gesture and is
                          not treated as an unattended popup. */}
                      <DropdownMenuItem onSelect={() => download(f.key)} data-testid="drive-grid-download">
                        <Download size={13} />{i18nT('apps.awsControl.console.download')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => openRename(f.key)} data-testid="drive-grid-rename">
                        <Pencil size={13} />{i18nT('apps.awsControl.console.rename')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => openShare(f.key)} data-testid="drive-grid-share">
                        <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => openMove(f.key)} disabled={moveBusy} data-testid="drive-grid-move">
                        <FolderInput size={13} />{i18nT('apps.awsControl.console.move_to')}
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => openConfirmDelete(f.key)} data-testid="drive-grid-delete">
                        <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                {/* The name is the preview trigger; while its rename editor is
                    open the editor IS the name, and a preview opened over it
                    would stack two editing contexts on one tile. */}
                {renaming !== f.key && (
                  <button
                    type="button"
                    onClick={() => setPreview({ key: f.key, size: f.size })}
                    className="w-full cursor-pointer truncate border-none bg-transparent p-0 text-left text-[13px] font-medium text-text-strong hover:underline"
                    data-testid="drive-grid-preview-open"
                  >
                    {f.key.split('/').pop()}
                  </button>
                )}
                <span className="text-[12px] text-muted tabular-nums">
                  {/* Size and modified time, the two facts a file manager puts
                      under a tile name -- the kind is already carried by the
                      glyph and by the extension in the name itself, so printing
                      it a third time crowded out the date the list view shows.
                      While the tile's move runs, the state goes here in words
                      (the list row does the same). */}
                  {movingKey === f.key
                    ? <span data-testid="drive-moving">{i18nT('apps.awsControl.console.move_moving')}</span>
                    : `${fmtBytes(f.size)} · ${fmtRelative(f.modified)}`}
                </span>
                {confirmDelete === f.key && (
                  <TileConfirm
                    label={i18nT('apps.awsControl.console.delete_confirm', { name: f.key.split('/').pop() ?? f.key })}
                    error={deleteFailedFor(f.key) ? i18nT('apps.awsControl.console.delete_failed') : ''}
                    errorSource={deleteFailedFor(f.key) ? deleteMut.error : null}
                    askAgent={handOff}
                    pending={deleteMut.isPending}
                    onCancel={() => setConfirmDelete(null)}
                    onConfirm={() => deleteMut.mutate(f.key, { onSuccess: () => setConfirmDelete((cur) => (cur === f.key ? null : cur)) })}
                    action={i18nT('apps.awsControl.console.delete_confirm_action')}
                  />
                )}
                {renaming === f.key && (
                  <div className="flex w-full flex-wrap items-center gap-2" data-testid="drive-grid-rename-row">
                    <Input
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') commitRename(f.key)
                        if (e.key === 'Escape') closeRename()
                      }}
                      autoFocus
                      aria-label={i18nT('apps.awsControl.console.rename')}
                      className="w-full min-w-0"
                      data-testid="drive-grid-rename-input"
                    />
                    {/* No hand-off: beside a live input, and the agent
                        navigation would take the half-typed name with it. */}
                    <AwsErrorNotice message={renameError} askAgent={false} variant="inline" testId="drive-grid-rename-error" />
                    <Btn onClick={closeRename} disabled={renameMut.isPending} data-testid="drive-grid-rename-cancel">
                      {i18nT('apps.awsControl.console.cancel')}
                    </Btn>
                    <Btn
                      primary
                      disabled={renameMut.isPending || !renameValue.trim()}
                      onClick={() => commitRename(f.key)}
                      data-testid="drive-grid-rename-save"
                    >
                      <Pencil size={13} />{i18nT('apps.awsControl.console.rename')}
                    </Btn>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {!searching && mode === 'list' && (folders.length > 0 || files.length > 0) && (
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
              surface="bg"
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
                  onClick={() => setPath(name)}
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
                  <td className={`sticky right-0 ${PINNED_SURFACE.bg.fill} px-2.5 py-2`}>
                    {/* The seam is spelled exactly as the shared rows spell it:
                        a 1px child div plus a `right-full` gradient, both gated
                        on the measured overflow. Not `border-l` (under
                        `border-collapse: collapse` a border paints at the cell's
                        layout slot and stays behind the scrolling columns), and
                        not a box-shadow either - a third spelling of the same
                        seam is how the two drift apart, which is the whole
                        reason the head is shared rather than copied. The fill
                        and the gradient paint the PAGE surface: this table has
                        no card fill, so a `bg-card` pin here is a stripe. */}
                    {edges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                    {edges.right && <div aria-hidden="true" className={`pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l ${PINNED_SURFACE.bg.seam} to-transparent`} />}
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
                          <IconButton
                            onClick={(e) => e.stopPropagation()}
                            aria-label={i18nT('apps.awsControl.console.folder_actions')}
                            data-testid="drive-folder-more"
                          >
                            <MoreHorizontal className="h-3.5 w-3.5" />
                          </IconButton>
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
                        {/* `basis-full`: the strip is a wrapping flex row holding
                            Cancel and Delete, and an inline notice with its
                            hand-off would be a third action on that row. On its
                            own line it stays inline-shaped without joining them. */}
                        <AwsErrorNotice
                          askAgent={handOff}
                          error={folderDeleteMut.error}
                          message={folderDeleteMut.isError ? i18nT('apps.awsControl.console.folder_delete_failed') : null}
                          variant="inline"
                          className="basis-full"
                          testId="drive-folder-delete-error"
                        />
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
                  <tr
                    {...dragProps(f.key)}
                    {...highlightProps(f.key)}
                    /* Dimmed while ITS move is in flight: a server-side copy of a
                       large object leaves the row otherwise inert until the
                       listing refetches, and a reader re-drags or assumes the
                       drop missed. */
                    className={`border-b border-border last:border-0 hover:bg-bg-hover ${highlighted(f.key) ? 'ring-2 ring-inset ring-accent' : ''} ${movingKey === f.key ? 'opacity-50' : ''}`}
                    aria-busy={movingKey === f.key || undefined}
                    data-testid="drive-file"
                  >
                    <td className="px-2.5 py-2">
                      <button
                        type="button"
                        onClick={() => setPreview({ key: f.key, size: f.size })}
                        className="flex min-w-0 max-w-full cursor-pointer items-center gap-2 border-none bg-transparent p-0 text-left"
                        data-testid="drive-preview-open"
                      >
                        <FileText size={14} className="shrink-0 text-muted" aria-hidden="true" />
                        <span className="truncate text-text hover:underline">{f.key.split('/').pop()}</span>
                      </button>
                    </td>
                    <td className="px-2.5 py-2 text-muted">{objectKind(f.key)}</td>
                    <td className="px-2.5 py-2 text-muted">{fmtBytes(f.size)}</td>
                    {/* While its move runs the row is dimmed and `aria-busy`, but
                        opacity alone reads as "unavailable, somehow" -- say the
                        state in words where the picker's own "Moving…" was. */}
                    <td className="px-2.5 py-2 text-muted">
                      {movingKey === f.key ? <span data-testid="drive-moving">{i18nT('apps.awsControl.console.move_moving')}</span> : fmtRelative(f.modified)}
                    </td>
                    <td className={`sticky right-0 ${PINNED_SURFACE.bg.fill} px-2.5 py-2`}>
                    {/* Same seam, same page-surface fill as the folder row above. */}
                    {edges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                    {edges.right && <div aria-hidden="true" className={`pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l ${PINNED_SURFACE.bg.seam} to-transparent`} />}
                      {/* ONE overflow, holding every per-item action. Download
                          used to sit beside it as a bare button while Share and
                          Delete were inside, which is the same split the grid
                          card above just lost. Move lives here too, so a
                          keyboard or touch reader has a path to it that is not
                          a pointer drag. */}
                      <div className="flex items-center justify-end gap-1">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <IconButton
                              aria-label={i18nT('apps.awsControl.console.file_actions')}
                              data-testid="drive-more"
                              onPointerDown={rememberMenuOpener}
                              onKeyDown={rememberMenuOpener}
                            >
                              <MoreHorizontal className="h-3.5 w-3.5" />
                            </IconButton>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end" onCloseAutoFocus={skipRestoreIfDialog}>
                            <DropdownMenuItem onSelect={() => download(f.key)} data-testid="drive-download">
                              <Download size={13} />{i18nT('apps.awsControl.console.download')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => openRename(f.key)} data-testid="drive-rename">
                              <Pencil size={13} />{i18nT('apps.awsControl.console.rename')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => openShare(f.key)} data-testid="drive-share">
                              <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => openMove(f.key)} disabled={moveBusy} data-testid="drive-move">
                              <FolderInput size={13} />{i18nT('apps.awsControl.console.move_to')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => openConfirmDelete(f.key)} data-testid="drive-delete">
                              <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </td>
                  </tr>
                  {renaming === f.key && (
                    // eslint-disable-next-line jsx-a11y/control-has-associated-label -- the row's control is the Input, which carries its own aria-label; the rule cannot see through the component wrapper
                    <tr className="border-b border-border bg-bg-elevated" data-testid="drive-rename-row">
                      <td colSpan={5} className="px-2.5 py-2">
                        <div className="sticky left-0 flex max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-2 pr-4">
                          <Input
                            value={renameValue}
                            onChange={(e) => setRenameValue(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') commitRename(f.key)
                              if (e.key === 'Escape') closeRename()
                            }}
                            autoFocus
                            aria-label={i18nT('apps.awsControl.console.rename')}
                            className="w-full min-w-0 sm:w-[260px]"
                            data-testid="drive-rename-input"
                          />
                          {/* Beside a live input, so no agent hand-off: the
                              navigation would take the half-typed name with it. */}
                          <AwsErrorNotice message={renameError} askAgent={false} variant="inline" testId="drive-rename-error" />
                          <Btn onClick={closeRename} disabled={renameMut.isPending} data-testid="drive-rename-cancel">
                            {i18nT('apps.awsControl.console.cancel')}
                          </Btn>
                          <Btn
                            primary
                            disabled={renameMut.isPending || !renameValue.trim()}
                            onClick={() => commitRename(f.key)}
                            data-testid="drive-rename-save"
                          >
                            <Pencil size={13} />{i18nT('apps.awsControl.console.rename')}
                          </Btn>
                        </div>
                      </td>
                    </tr>
                  )}
                  {confirmDelete === f.key && (
                    <tr className="border-b border-border bg-bg-elevated" data-testid="drive-delete-confirm">
                      <td colSpan={5} className="px-2.5 py-2">
                        {/* Same viewport pinning as the folder strip above. */}
                        <div className="sticky left-0 flex max-w-[calc(100vw-2.5rem)] flex-wrap items-center gap-2 pr-4">
                          <span className="min-w-0 flex-1 text-text">
                            {i18nT('apps.awsControl.console.delete_confirm', { name: f.key.split('/').pop() ?? f.key })}
                          </span>
                          {/* Same `basis-full` reason as the folder strip above. */}
                          <AwsErrorNotice
                            askAgent={handOff}
                            error={deleteFailedFor(f.key) ? deleteMut.error : null}
                            message={deleteFailedFor(f.key) ? i18nT('apps.awsControl.console.delete_failed') : null}
                            variant="inline"
                            className="basis-full"
                            testId="drive-delete-error"
                          />
                          <Btn onClick={() => setConfirmDelete(null)} data-testid="drive-delete-cancel">
                            {i18nT('apps.awsControl.console.cancel')}
                          </Btn>
                          <Btn
                            danger
                            disabled={deleteMut.isPending}
                            onClick={() => deleteMut.mutate(f.key, { onSuccess: () => setConfirmDelete((cur) => (cur === f.key ? null : cur)) })}
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

      {!searching && listQ.hasNextPage && (
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
        <ShareDialog
          account={account}
          section="drive"
          fileKey={share.key}
          onClose={() => { setShare(null); returnFocusToOpener() }}
        />
      )}

      {/* The keyboard / touch path to a move. Drag is a convention a pointer
          user may discover; this is the one every reader can reach from the
          row's own menu. Destinations are the folders this listing already
          knows -- the parent, the top level, and the sub-folders on screen --
          which is the same set a drop could have landed on. The dialog closes
          on success; a refused move keeps it open with the same sentence the
          drop path reports, so the reader can pick another folder. Dismissing
          it MID-MOVE is allowed: the move keeps going, the source row stays
          dimmed until it lands, and the page strip reports a refusal once the
          picker is gone — so a slow or hung copy never traps the reader. */}
      {moveTarget && (
        <MoveDialog
          fileKey={moveTarget}
          currentPath={path}
          folders={folders}
          pending={moveBusy}
          error={moveError}
          askAgent={handOff}
          onMove={(folder) => {
            const base = moveTarget.split('/').pop() ?? moveTarget
            setMoveError(null)
            moveMut.mutate(
              { fromKey: moveTarget, toKey: folder ? `${folder}/${base}` : base },
              // The reader may have dismissed the picker while this was in
              // flight; only a picker that is still open hands focus back.
              // Moves are serialized, so a picker open when this lands can
              // only be this move's own — no other could have opened meanwhile.
              { onSuccess: () => { if (moveDialogOpenRef.current) closeMoveDialog() } },
            )
          }}
          onClose={closeMoveDialog}
        />
      )}
      {preview && (
        <PreviewDialog
          account={account}
          entry={preview}
          onDownload={download}
          downloadError={downloadError?.key === preview.key ? downloadError : null}
          onClose={() => setPreview(null)}
        />
      )}
    </section>
  )
}

/* ── Move dialog ─────────────────────────────────────────────────────────── */

/**
 * A folder picker for one file. Small on purpose: the destinations a reader
 * can see from the current folder, one button each, no tree walk. Modal
 * keyboard behaviour (focus in, Tab ring, Escape, focus restore) comes from the
 * shared hook, exactly as the picker dialog above.
 */
function MoveDialog({ fileKey, currentPath, folders, pending, error, askAgent, onMove, onClose }: {
  fileKey: string
  /** The folder the file lives in ('' = top level). */
  currentPath: string
  /** Full paths of the sub-folders in the current listing. */
  folders: string[]
  pending: boolean
  /** The refused move, reported inside the picker so the reader can pick again. */
  error: Failure | null
  /** The picker holds no draft of its own, so the hand-off is the page's call. */
  askAgent: boolean
  onMove: (folder: string) => void
  onClose: () => void
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  // `restoreFocus: false`: at mount the previously focused element is a menu
  // item that unmounted in the same commit, so the hook would capture `body`.
  // The caller knows who opened the menu and focuses them on close.
  useDialogFocusTrap(panelRef, onClose, { restoreFocus: false })
  const backdropDown = useRef(false)
  const name = fileKey.split('/').pop() ?? fileKey
  const crumbs = currentPath.split('/').filter(Boolean)
  const parent = crumbs.length > 1 ? crumbs.slice(0, -1).join('/') : null
  // Up-links first (the way a file manager orders them), then the sub-folders
  // in listing order. Top level appears only when the file is not already
  // there; the parent only when it is not the top level (which the first
  // entry already covers).
  const options: Array<{ folder: string; label: string; testId: string }> = []
  if (currentPath) options.push({ folder: '', label: i18nT('apps.awsControl.console.move_root'), testId: 'move-root' })
  if (parent) options.push({ folder: parent, label: i18nT('apps.awsControl.console.move_up', { name: crumbs[crumbs.length - 2] }), testId: 'move-up' })
  for (const f of folders) options.push({ folder: f, label: f.split('/').pop() ?? f, testId: 'move-folder' })

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) onClose(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="w-full max-w-sm rounded-xl border border-border bg-card p-4 shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-labelledby="aws-move-title"
        aria-busy={pending || undefined}
        data-testid="move-dialog"
      >
        <div className="mb-3 flex items-center justify-between gap-3">
          <h3 id="aws-move-title" className="min-w-0 truncate text-sm font-semibold text-text-strong">
            {i18nT('apps.awsControl.console.move_title', { name })}
          </h3>
          <IconButton
            onClick={onClose}
            aria-label={i18nT('apps.awsControl.console.close')}
            data-testid="move-close"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </div>
        {options.length === 0 ? (
          <p className="text-[13px] text-muted" data-testid="move-no-folders">
            {i18nT('apps.awsControl.console.move_no_folders')}
          </p>
        ) : (
          <div className="flex max-h-72 flex-col gap-1 overflow-y-auto" data-testid="move-options">
            {options.map((o) => (
              <button
                key={o.testId + o.folder}
                onClick={() => onMove(o.folder)}
                disabled={pending}
                className="flex w-full items-center gap-2 rounded-md border-none bg-transparent px-2.5 py-2 text-left text-[13px] text-text cursor-pointer hover:bg-bg-hover focus-ring disabled:opacity-50"
                data-testid={o.testId}
                data-folder={o.folder}
              >
                <FolderClosed size={14} className="shrink-0 text-muted" aria-hidden="true" />
                <span className="min-w-0 truncate">{o.label}</span>
              </button>
            ))}
          </div>
        )}
        {pending && (
          <p className="mt-2 text-[12px] text-muted" data-testid="move-pending">
            {i18nT('apps.awsControl.console.move_moving')}
          </p>
        )}
        <AwsErrorNotice
          error={error?.error}
          message={error?.message}
          askAgent={askAgent}
          className="mt-2"
          testId="move-error"
        />
      </div>
    </div>
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

  /* Modal keyboard behaviour from the shared hook, as every dialog in this
     file: focus moves in on open, Tab cycles inside, Escape closes. Escape is
     refused while the link is being minted -- closing then would discard a
     share that is about to exist with no way to see its URL. Focus RETURN is
     the opener's job (`restoreFocus: false`): this dialog is opened from a
     Radix menu item that unmounts in the same commit, so the element focused
     at mount is `body`, and the drive section knows the real opener. */
  const panelRef = useRef<HTMLDivElement>(null)
  const backdropDown = useRef(false)
  const close = () => { if (!shareMut.isPending) onClose() }
  useDialogFocusTrap(panelRef, close, { restoreFocus: false })

  return (
    // Scrim and panel are two elements: the scrim owns click-to-dismiss (a
    // press that starts AND ends on it), the panel is the dialog. Same shape
    // as the picker dialog above.
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="presentation"
      onMouseDown={(e) => { if (e.target === e.currentTarget) backdropDown.current = true }}
      onClick={(e) => { if (e.target === e.currentTarget && backdropDown.current) close(); backdropDown.current = false }}
    >
      <div
        ref={panelRef}
        className="w-full max-w-md rounded-xl border border-border bg-card p-4 shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-labelledby="aws-share-title"
        data-testid="share-dialog"
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 id="aws-share-title" className="min-w-0 truncate text-sm font-semibold text-text-strong">
            {/* Names the file: a link publishes it externally, so the reader
                must see WHICH file before creating one — the Move picker
                beside this dialog already does the same. */}
            {i18nT('apps.awsControl.console.share_title', { name: fileKey.split('/').pop() ?? fileKey })}
          </h3>
          <IconButton onClick={close} disabled={shareMut.isPending} aria-label={i18nT('apps.awsControl.console.close')} data-testid="share-close"><X className="h-4 w-4" /></IconButton>
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
            {/* A refused share (publishing denied by policy, a dead key) used to
                leave the button simply re-enabled, as if nothing had been asked.
                No hand-off here: the note field above still holds whatever was
                typed, and the hand-off navigates away from it. The refusal is
                journaled regardless, so the agent can still be asked afterwards. */}
            <AwsErrorNotice
              error={shareMut.error}
              message={shareMut.isError ? i18nT('apps.awsControl.console.share_failed') : null}
              askAgent={false}
              className="mb-3"
              testId="share-error"
            />
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

/** The glyph each backup kind is recognised by. A snapshot is an archive of the
 *  workspace; the sessions archive is a database dump, so they are not the same
 *  picture. */
const BACKUP_KIND_ICON: Record<BackupKind, React.ReactNode> = {
  snapshot: <Archive className="h-4 w-4" aria-hidden="true" />,
  sessions: <Database className="h-4 w-4" aria-hidden="true" />,
}

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
    <div className="flex flex-wrap items-center gap-3 px-3 py-2.5" data-testid={`backup-row-${kind}`}>
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-bg-elevated text-muted">
        {BACKUP_KIND_ICON[kind]}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-text">{i18nT(BACKUP_KIND_LABEL_KEY[kind])}</div>
        <div className="text-[12px] text-muted">
          {run
            ? i18nT('apps.awsControl.console.backup_last_run', { when: fmtRelative(run.at), size: fmtBytes(run.bytes) })
            : i18nT('apps.awsControl.console.backup_never')}
        </div>
        {/* Two different failures share one line: a START the route refused
            (its thrown error rides along) and a RUN the server reports as its
            last outcome, whose reason arrives as text inside a 200 — so that
            one has only its sentence to hand over. */}
        {(failed || startError) && (
          <AwsErrorNotice
            askAgent
            error={startError ? runMut.error : undefined}
            message={startError || i18nT('apps.awsControl.console.backup_failed', { reason: failed?.error || '' })}
            variant="inline"
            testId={`backup-error-${kind}`}
          />
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
      {/* The badge states ONLY what the ledger records: a recorded run, or none.
          It is deliberately not a health verdict -- the ledger holds successes,
          so "a run exists" is the whole claim, and the failure line above is
          where a bad outcome is reported. A third state would be invented.

          Badge and Run travel together in one shrink-0 cluster, and the ROW
          wraps: at 320px the label + meta + badge + button do not fit on one
          line, and a non-wrapping row would push the button past the viewport
          edge instead of dropping the pair beneath. */}
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <Badge variant={run ? 'ok' : 'muted'} data-testid={`backup-state-${kind}`}>
          {run
            ? i18nT('apps.awsControl.console.backup_state_current')
            : i18nT('apps.awsControl.console.backup_state_never')}
        </Badge>
        <Btn onClick={() => runMut.mutate()} disabled={busy} data-testid={`backup-run-${kind}`}>
          <RefreshCw size={13} className={busy ? 'animate-spin' : ''} />
          {busy ? i18nT('apps.awsControl.console.backup_running') : i18nT('apps.awsControl.console.backup_run_now')}
        </Btn>
      </div>
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
      {/* A status read that fails must not leave the pane as a bare title — that
          reads as "no backups configured", the one thing it cannot mean. */}
      <AwsErrorNotice
        askAgent
        error={backupQ.error}
        message={backupQ.isError ? i18nT('apps.awsControl.console.backup_status_failed') : null}
        onRetry={() => backupQ.refetch()}
        testId="backup-status-error"
      />
      {data && (
        <Card className="px-2 py-3 md:px-3">
          <PanelSectionHeader
            label={i18nT('apps.awsControl.console.backup_title')}
            count={BACKUP_KINDS.length}
            className="mb-1 px-1"
          />
          <div className="divide-y divide-border">
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
          <div className="flex items-center justify-between gap-3 px-3 py-2.5" data-testid="backup-nightly">
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-text">{i18nT('apps.awsControl.console.backup_nightly')}</div>
              <div className="text-[12px] text-muted">{i18nT('apps.awsControl.console.backup_nightly_hint')}</div>
            </div>
            <Toggle checked={data.nightly} onChange={(v) => nightlyMut.mutate(v)} label={i18nT('apps.awsControl.console.backup_nightly')} />
          </div>
          {/* The toggle snaps back on a failed write; without a line under it the
              snap-back reads as a flaky control rather than a refused request.
              Directly under the row it explains, so the snap-back and its reason
              share one glance. */}
          {nightlyMut.isError && (
            <div className="px-3 py-2">
              <AwsErrorNotice
                askAgent
                error={nightlyMut.error}
                message={i18nT('apps.awsControl.console.backup_nightly_failed')}
                testId="backup-nightly-error"
              />
            </div>
          )}
          </div>
        </Card>
      )}

      {/* The remote half failed INSIDE a 200: the backend read the local ledger
          fine and reports why the archive listing did not come back. The reason
          is the message and the sentence is the lead, so the hand-off carries the
          text AWS actually returned rather than only "couldn't read". */}
      {data?.remoteError && (
        <AwsErrorNotice
          askAgent
          title={i18nT('apps.awsControl.console.backup_remote_error')}
          message={data.remoteError}
          className="mt-2"
          testId="backup-remote-error"
        />
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

      {/* Restore is the call the recommended write-only policy denies (the
          caveat above says so) — and its failure used to be invisible, the one
          outcome that caveat exists to explain. */}
      <AwsErrorNotice
        askAgent
        error={restoreMut.error}
        message={restoreMut.isError ? i18nT('apps.awsControl.console.backup_restore_failed') : null}
        className="mt-2"
        testId="backup-restore-error"
      />

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
      {/* A failed ledger read is not "no links": this pane's whole job is naming
          what is exposed, so silence here is the worst possible answer. */}
      <AwsErrorNotice
        askAgent
        error={sharesQ.error}
        message={sharesQ.isError ? i18nT('apps.awsControl.console.access_list_failed') : null}
        onRetry={() => sharesQ.refetch()}
        testId="access-list-error"
      />
      <AwsErrorNotice
        askAgent
        error={forgetMut.error}
        message={forgetMut.isError ? i18nT('apps.awsControl.console.access_forget_failed') : null}
        className="mb-2"
        testId="access-forget-error"
      />
      {sharesQ.data && shares.length === 0 && (
        <EmptyState
          icon={<Share2 className="h-10 w-10" aria-hidden="true" />}
          title={i18nT('apps.awsControl.console.access_empty')}
          testId="access-empty"
        />
      )}
      {shares.length > 0 && (
        <Card className="px-2 py-3 md:px-3" data-testid="access-list">
          <PanelSectionHeader
            label={i18nT('apps.awsControl.console.access_title')}
            count={shares.length}
            className="mb-1 px-1"
          />
          <div className="divide-y divide-border">
          {shares.map((s: Share) => (
            <div key={s.id} className="flex flex-wrap items-center gap-3 px-3 py-2.5 text-[13px]" data-testid="access-row">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-bg-elevated text-muted">
                <Link2 className="h-4 w-4" aria-hidden="true" />
              </span>
              <div className="min-w-0 flex-1">
                {/* WRAPS, and the key keeps a floor. A narrow row puts a
                  * truncating filename and its badges in one cluster beside a
                  * fixed-width Remove button: with neither rule the badges win
                  * the space outright and at 320px the key box measured 0px --
                  * the row then says a share is dead without saying WHICH, on
                  * the one surface whose job is naming what is exposed. The
                  * floor makes the line unable to fit both, so the badges wrap
                  * beneath instead of consuming the name. Nothing changes on a
                  * wide row, where all three still fit on one line. */}
                <div className="flex flex-wrap items-center gap-2">
                  <span className="min-w-[8rem] flex-1 truncate font-mono text-text">{s.key}</span>
                  <Badge variant="muted">{i18nT(SECTION_LABEL_ON_PAGE[s.section])}</Badge>
                  {/* The object behind this link is gone. The row stays, because
                    * the link itself is unexpired and would resolve again if the
                    * key were recreated -- what changed is that this row can no
                    * longer be read as live exposure. */}
                  {s.objectMissing && (
                    <Badge variant="warn" data-testid="access-object-missing">{i18nT('apps.awsControl.console.access_object_missing')}</Badge>
                  )}
                </div>
                <div className="text-[12px] text-muted">
                  {s.note ? `${s.note} · ` : ''}
                  {i18nT('apps.awsControl.console.access_expires_in', { when: fmtRelative(s.expiresAt) })}
                </div>
              </div>
              {/* `ml-auto shrink-0`, and the ROW wraps: the glyph block this
                * change adds costs 44px the old row did not spend, which at
                * 320px is exactly enough to push a fixed-width "Remove from
                * list" past the viewport edge. Wrapping drops it beneath
                * instead; nothing moves at any ordinary width. */}
              <Btn className="ml-auto shrink-0" onClick={() => forgetMut.mutate(s.id)} disabled={forgetMut.isPending} data-testid="access-forget">{i18nT('apps.awsControl.console.access_forget')}</Btn>
            </div>
          ))}
          </div>
        </Card>
      )}
      {/* Only with rows to qualify: an unchecked EMPTY ledger has no claim to
        * qualify, and saying so would be noise on the empty state. Without this
        * line a missing badge would read as "every object is still there" on a
        * render where the drive was never read.
        *
        * WARN-toned, unlike the permanent footer directly below it. Rendered in
        * the same muted 12px as that footer, this line reads as more boilerplate
        * and gets skimmed -- and a reader who skims it takes the absence of a
        * "file deleted" badge for "objects verified present", which is the
        * under-reporting this whole change exists to prevent. */}
      {shares.length > 0 && sharesQ.data?.checked === false && (
        <p className="mt-2 flex items-start gap-1.5 text-[12px] text-warn" data-testid="access-unchecked">
          <AlertTriangle className="lucide-inline" />
          <span>{i18nT('apps.awsControl.console.access_unchecked')}</span>
        </p>
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

/** The glyph each section is recognised by, the same one its pane header wears. */
const SECTION_ICON: Record<DriveSection, LucideIcon> = {
  drive: FolderClosed,
  library: Library,
  backup: Archive,
}

/**
 * The one section → glyph + label map every section tile draws from. The
 * storage meter here and the Overview's drive card render the same three
 * tiles, and two maps for one fact is how a renamed section ends up with two
 * icons.
 */
export const SECTION_TILES: { section: DriveSection; icon: LucideIcon; labelKey: string }[] =
  SECTIONS.map((section) => ({ section, icon: SECTION_ICON[section], labelKey: SECTION_LABEL_ON_PAGE[section] }))

/**
 * The storage meter: one horizontal bar split by section, and a quick tile per
 * section.
 *
 * The bar is proportional to each section's BYTES, but a section with zero
 * bytes still gets a legible legend row (its swatch and a `0` size) — a section
 * that exists is worth naming even when empty, and a 0-width bar segment alone
 * would silently drop it. When the whole drive is empty the bar renders as a
 * single muted track so the card is never a bare outline.
 *
 * Exported because the pane that owns "what is using storage" places it; this
 * file only owns what it looks like. Its section readings are read-only rows:
 * the meter sits on the Usage pane, and every section it names is one rail
 * click away already.
 */
export function StorageMeter({ usage }: { usage: DriveUsage }) {
  return (
    <Card data-testid="drive-storage-meter">
      <PanelSectionHeader label={i18nT('apps.awsControl.console.root_storage_used')} />

      {/* No headline figure here: the Usage pane's stat cards own the byte
          total and the object count, and this card owns the SPLIT — one
          rendering per fact per pane. */}
      {/* The bar and its legend are the shared `StorageBar`: the Overview card
          draws the same split, and two drawings of one figure would drift. */}
      <div className="mt-3">
        <StorageBar usage={usage} testId="drive-meter" />
      </div>

      {/* One reading per section: the glyph it is recognised by, its name, and
          how many objects are in it. Read-only here — this pane IS the usage
          view and every section is one rail click away — so the tiles draw as
          flat rows rather than as the bordered targets the Overview uses.
          Stacked on a phone, three across from `sm`. */}
      <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3" data-testid="drive-meter-tiles">
        {SECTION_TILES.map(({ section, icon: Icon, labelKey }) => (
          <QuickTile
            key={section}
            testId={`drive-meter-tile-${section}`}
            icon={<Icon className="h-4 w-4" aria-hidden="true" />}
            label={i18nT(labelKey)}
            count={i18nT('apps.awsControl.console.root_section_objects', {
              count: usage.sections[section].objects,
              objects: fmtNumber(usage.sections[section].objects),
            })}
          />
        ))}
      </div>
    </Card>
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
