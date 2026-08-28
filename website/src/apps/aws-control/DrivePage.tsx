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
/* eslint-disable jsx-a11y/control-has-associated-label --
   The two inline confirm strips render as a <td colSpan={5}>, which jsx-a11y
   treats as a gridcell needing an accessible name and then searches only two
   levels deep for one. The strips DO name their controls (Cancel, Delete file,
   Delete folder, each with its own i18n text) - they just sit one level further
   in, behind the flex wrapper the strip needs, because a <td> cannot itself be
   the flex container: display:flex drops display:table-cell and the strip stops
   spanning the row. */
import { Fragment, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ChevronLeft, ChevronDown, RefreshCw, HardDrive, Library, Archive, Share2,
  Download, Trash2, Upload, FolderClosed, FolderPlus, FileText, X,
  MoreHorizontal, Code, ChevronRight,
} from 'lucide-react'
import { Btn, Badge, Toggle, Input, ContentSkeleton, IconButton } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { LibraryTableHead } from '../../components/library/LibraryTable'
import type { LibraryColumn } from '../../components/library/LibraryTable'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtRelative } from '../../i18n/format'
import { awsControlApi } from './api'
import type {
  AwsAccount, DriveSection, DriveStatus, ArtifactKind, LibraryArtifact,
  BackupKind, Share,
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
  json: 'apps.awsControl.console.kind_json',
  webapp: 'apps.awsControl.console.kind_webapp',
  image: 'apps.awsControl.console.kind_image',
}

const EXPIRY_LABEL_KEY: Record<string, string> = {
  '1h': 'apps.awsControl.console.expiry_1h',
  '1d': 'apps.awsControl.console.expiry_1d',
  '7d': 'apps.awsControl.console.expiry_7d',
}

const SECTION_LABEL_KEY: Record<DriveSection, string> = {
  drive: 'apps.awsControl.console.section_drive',
  library: 'apps.awsControl.console.section_library',
  backup: 'apps.awsControl.console.section_backup',
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

const KIND_KEYS: ArtifactKind[] = ['widget', 'markdown', 'html', 'json', 'webapp', 'image']

function LibrarySection({ account, bucket }: { account: string; bucket: string }) {
  const qc = useQueryClient()
  const [kind, setKind] = useState<ArtifactKind | 'all'>('all')
  const libQ = useQuery({
    queryKey: ['aws-control', 'library', account],
    queryFn: () => awsControlApi.library(account),
  })
  const pushMut = useMutation({
    mutationFn: (slug: string) => awsControlApi.libraryPush(account, slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'library', account] }),
  })

  const artifacts = libQ.data?.artifacts ?? []
  const counts: Record<string, number> = { all: artifacts.length }
  for (const k of KIND_KEYS) counts[k] = artifacts.filter((a) => a.kind === k).length
  const shown = kind === 'all' ? artifacts : artifacts.filter((a) => a.kind === kind)

  return (
    <section data-testid="library-section">
      <SectionHeader icon={<Library size={15} />} title={i18nT('apps.awsControl.console.library_title')} />
      <div className="mb-3 flex flex-wrap gap-1.5" data-testid="library-chips">
        {(['all', ...KIND_KEYS] as const).map((k) => (
          <button
            key={k}
            onClick={() => setKind(k)}
            className={`rounded-full border px-2.5 py-1 text-[12px] cursor-pointer transition-colors ${
              kind === k ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-transparent text-muted hover:text-text'
            }`}
            data-testid={`library-chip-${k}`}
          >
            {k === 'all' ? i18nT('apps.awsControl.console.library_all') : i18nT(KIND_LABEL_KEY[k])}{' '}
            <span className="font-mono opacity-70">{counts[k] ?? 0}</span>
          </button>
        ))}
      </div>

      {libQ.isLoading && <ContentSkeleton rows={2} />}

      {libQ.data && shown.length === 0 && (
        <p className="text-[13px] text-muted" data-testid="library-empty">
          {i18nT('apps.awsControl.console.library_empty')}
        </p>
      )}

      {shown.length > 0 && (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" data-testid="library-tiles">
          {shown.map((a) => (
            <LibraryTile key={a.slug} artifact={a} onPush={() => pushMut.mutate(a.slug)} pushing={pushMut.isPending && pushMut.variables === a.slug} />
          ))}
        </div>
      )}

      <CliDrawer bucket={bucket} prefix="artifacts/" />
    </section>
  )
}

function LibraryTile({ artifact, onPush, pushing }: { artifact: LibraryArtifact; onPush: () => void; pushing: boolean }) {
  const synced = artifact.pushedVersion !== null
  const upToDate = artifact.pushedVersion === artifact.version
  const notPushable = artifact.kind === 'image'
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2.5" data-testid="library-tile">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium text-text">{artifact.name}</span>
            <Badge variant="muted">{i18nT(KIND_LABEL_KEY[artifact.kind])}</Badge>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
            <span className="font-mono">v{artifact.version}</span>
            <span>{fmtRelative(artifact.updatedAt)}</span>
            <span className={synced ? 'text-ok' : 'text-muted'}>
              {synced
                ? i18nT('apps.awsControl.console.library_synced', { version: artifact.pushedVersion })
                : i18nT('apps.awsControl.console.library_not_synced')}
            </span>
          </div>
        </div>
        <Btn
          onClick={onPush}
          disabled={pushing || upToDate || notPushable}
          data-testid="library-push"
          title={notPushable ? i18nT('apps.awsControl.console.library_not_pushable') : undefined}
        >
          <Upload size={13} />
          {upToDate
            ? i18nT('apps.awsControl.console.library_up_to_date')
            : i18nT('apps.awsControl.console.library_push')}
        </Btn>
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

/** No column is sortable, so the shared head never calls this. */
const noSort = () => {}

const KEY_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9 ._()+@=-]*$/

function DriveSectionView({ account, bucket }: { account: string; bucket: string }) {
  const qc = useQueryClient()
  const [path, setPath] = useState('')
  const [token, setToken] = useState('')
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
  /* The pinned Actions cell paints its seam only when the table actually
     overflows, so the edge is measured rather than assumed. */
  const [attachScroller, edges] = useScrollEdges<HTMLDivElement>()

  const listQ = useQuery({
    queryKey: ['aws-control', 'drive', account, 'list', path, token],
    queryFn: () => awsControlApi.driveList(account, 'drive', path, token),
  })
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
        <button className="hover:text-text cursor-pointer bg-transparent border-none p-0" onClick={() => { setPath(''); setToken('') }}>
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
                      setToken('')
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

      {listQ.data && (
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
              {listQ.data.folders.map((name) => (
                /* The WHOLE row opens the folder, which is both what the
                   artifact table's own folder row does (onClick on the <tr>)
                   and what a file browser is expected to do - when only the
                   name text carried the handler, the Kind, Size and Modified
                   cells and all the empty space in between were dead. The inner
                   button stays as the real focusable control so the row is
                   still reachable and operable from the keyboard. */
                <Fragment key={`f-${name}`}>
                <tr
                  onClick={() => { setPath(name); setToken(''); setDeletedCount(null) }}
                  className="cursor-pointer border-b border-border last:border-0 hover:bg-bg-hover"
                  data-testid="drive-folder"
                >
                  <td className="px-2.5 py-2">
                    <button
                      onClick={(e) => { e.stopPropagation(); setPath(name); setToken('') }}
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
              {listQ.data.files.map((f) => (
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
              {listQ.data.folders.length === 0 && listQ.data.files.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-2.5 py-3 text-muted" data-testid="drive-empty">
                    {i18nT('apps.awsControl.console.drive_empty')}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {listQ.data?.nextToken && (
        <div className="mt-2">
          <Btn onClick={() => setToken(listQ.data!.nextToken!)} data-testid="drive-load-more">{i18nT('apps.awsControl.console.load_more')}</Btn>
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
                  <Badge variant="muted">{i18nT(SECTION_LABEL_KEY[s.section])}</Badge>
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
 * itself the drive - so inside it that section is "Files". Reusing
 * `SECTION_LABEL_KEY` here printed "Drive" as the page title, the section row
 * and the section header all at once.
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
            {i18nT('apps.awsControl.console.stat_stored_value', {
              size: fmtBytes(drive.usage.bytes),
              objects: drive.usage.objects,
            })}
          </span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
        {section === null && (
          <>
            {/* The bucket's three prefixes, as the folders they are. */}
            <div className="overflow-hidden rounded-lg border border-border bg-card divide-y divide-border" data-testid="drive-sections">
              {SECTIONS.map((s) => {
                const Icon = SECTION_ICON[s]
                return (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setSection(s)}
                    className="flex w-full items-center gap-3 px-4 py-3 text-left cursor-pointer bg-transparent border-none hover:bg-bg-hover"
                    data-testid={`drive-section-${s}`}
                  >
                    <Icon size={15} className="shrink-0 text-accent" />
                    <span className="text-[13px] font-medium text-text-strong">{i18nT(SECTION_LABEL_ON_PAGE[s])}</span>
                    <span className="flex-1" />
                    <ChevronRight size={14} className="shrink-0 text-muted" />
                  </button>
                )
              })}
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
