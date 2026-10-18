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
 *
 * This file is the import path every consumer uses. It keeps the page's own
 * section map (`SECTION_TILES`), the storage meter and the share ledger
 * (`AccessSection`), and re-exports each pane from its owner:
 * `DrivePageLibrary.tsx` (Library); `DrivePageFiles.tsx`, `useDrivePageFiles.ts`,
 * `DrivePageFilesViews.tsx` and `DrivePageFileDialogs.tsx` (Files);
 * `DrivePageBackup.tsx` (Backup); and `DrivePageControls.tsx`, the controls
 * more than one pane draws. Owners never import this file.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
// By path, not from the app-sdk barrel: that barrel is published to third-party
// apps through the vendor stub, and a host-namespaced cache client is not part
// of that contract.
import { useAppQuery, useAppQueryKey } from '../../app-sdk/appQuery'
import { Library, Archive, Share2, FolderClosed, AlertTriangle, Link2 } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  Btn, Badge, ContentSkeleton, Card, EmptyState, PanelSectionHeader,
} from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtNumber, fmtRelative } from '../../i18n/format'
import { awsControlApi } from './api'
import type { DriveSection, DriveStatus, Share, DriveUsage } from './types'
import { PaneHeader, AwsErrorNotice, StorageBar, QuickTile } from './shared'
export { LibrarySection } from './DrivePageLibrary'
export { TileConfirm } from './DrivePageControls'
export { DriveSectionView } from './DrivePageFiles'
export { BackupSection } from './DrivePageBackup'

/* ── Section 7: Access (shares ledger) ───────────────────────────────────── */

export function AccessSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const appKey = useAppQueryKey()
  const sharesQ = useAppQuery(['shares', account], {
    queryFn: () => awsControlApi.shares(account),
  })
  const forgetMut = useMutation({
    mutationFn: (id: string) => awsControlApi.shareForget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: appKey(['shares', account]) }),
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
