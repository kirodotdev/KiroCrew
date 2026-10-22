/**
 * AWS Control drive - the Library pane: what is in the bucket's `artifacts/`
 * prefix, and the picker that copies a local artifact into it.
 *
 * `LibrarySection` lists the prefix (one card or row per cloud folder), joins it
 * to the local library only for a name, kind and preview, and owns removal.
 * `AddFromArtifactsDialog` lists the LOCAL artifacts and pushes each one with its
 * own request. Both hold per-card state as set membership (`withSlug`,
 * `withFailure`).
 * `DrivePage.tsx` re-exports `LibrarySection`.
 */
import { useRef, useState } from 'react'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
// By path, not from the app-sdk barrel: that barrel is published to third-party
// apps through the vendor stub, and a host-namespaced cache client is not part
// of that contract.
import { useAppQuery, useAppQueryKey } from '../../app-sdk/appQuery'
import { Link } from 'react-router-dom'
import { Trans } from 'react-i18next'
import {
  RefreshCw, Library, Trash2, Upload, FileText, X, MoreHorizontal, Search, CloudOff, Plus,
} from 'lucide-react'
import { Btn, Badge, ContentSkeleton, IconButton, Card, EmptyState } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import {
  WidgetThumb, ContentThumb, ImageThumb, WebAppThumb,
} from '../../components/library/ArtifactThumbs'
import { api } from '../../api/client'
import type { Artifact } from '../../types'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'
import { useNearViewport } from '../../hooks/useNearViewport'
import { i18nT } from '../../i18n/t'
import { fmtNumber, fmtRelative } from '../../i18n/format'
import { awsControlApi } from './api'
import type { ArtifactKind, LibraryArtifact } from './types'
import { PaneHeader, AwsErrorNotice } from './shared'
import { CliDrawer, useViewMode, ViewModeToggle, TileConfirm } from './DrivePageControls'

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

const KIND_KEYS: ArtifactKind[] =
  ['widget', 'markdown', 'html', 'svg', 'json', 'text', 'webapp', 'image']

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
        <WidgetThumb content={content} slug={slug} goalReport={full.goal_report === true} />
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
  const appKey = useAppQueryKey()

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
      qc.invalidateQueries({ queryKey: appKey(['drive', account]) })
      qc.invalidateQueries({ queryKey: appKey(['library', account]) })
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
     ['drive', account] prefix every mutation invalidates. */
  const listQ = useInfiniteQuery({
    queryKey: appKey(['drive', account, 'list', 'library']),
    queryFn: ({ pageParam }) => awsControlApi.driveList(account, 'library', '', pageParam),
    initialPageParam: '',
    getNextPageParam: (last) => last.nextToken || undefined,
  })
  // The local library, used ONLY as a slug -> {name, kind} lookup for the cards
  // and as the picker's source. Never as the listing itself.
  const localQ = useAppQuery(['library', account], {
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
     nested-control hijack the drive page avoids elsewhere. Splitting them keeps
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
              className="bg-card/85 backdrop-blur-xs"
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
  const appKey = useAppQueryKey()
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

  const libQ = useAppQuery(['library', account], {
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
      qc.invalidateQueries({ queryKey: appKey(['library', account]) })
      qc.invalidateQueries({ queryKey: appKey(['drive', account]) })
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
              className="min-w-0 flex-1 border-none bg-transparent text-[13px] text-text outline-hidden"
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
