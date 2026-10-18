/**
 * AWS Control drive - the Files pane (the bucket's `drive/` prefix): its
 * toolbar, notices, breadcrumb, empty state and dialogs, around whichever of the
 * three views in `DrivePageFilesViews.tsx` is showing. Its state comes from
 * `useDrivePageFiles` (`useDrivePageFiles.ts`). `DrivePage.tsx` re-exports
 * `DriveSectionView`.
 */
import { Upload, FolderClosed, FolderPlus, X, MoreHorizontal } from 'lucide-react'
import {
  Btn, Input, ContentSkeleton, IconButton, EmptyState, SearchInput,
} from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { PaneHeader, AwsErrorNotice } from './shared'
import { CliDrawer, ViewModeToggle } from './DrivePageControls'
import { useDrivePageFiles } from './useDrivePageFiles'
import { DriveFilesSearchResults, DriveFilesGrid, DriveFilesTable } from './DrivePageFilesViews'
import { PreviewDialog, MoveDialog, ShareDialog } from './DrivePageFileDialogs'

export function DriveSectionView({ account, bucket }: { account: string; bucket: string }) {
  const pane = useDrivePageFiles(account)
  const {
    mode, setMode, path, setPath, share, setShare, uploadError, downloadError, preview, setPreview,
    query, setQuery, newFolder, setNewFolder, creatingFolder, setCreatingFolder, folderError,
    folderToggleRef, moveTarget, returnFocusToOpener, moveDialogOpenRef, closeMoveDialog,
    closeFolderDisclosure, handOff, deletedHere, fileRef, listQ, folders, files, uploadMut,
    folderCreateMut, onCreateFolder, onPick, dropTarget, moveError, setMoveError, pageError,
    moveMut, moveBusy, searching, dropProps, inertDropProps, download, crumbs,
  } = pane

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
        <DriveFilesSearchResults pane={pane} />
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
        <DriveFilesGrid pane={pane} />
      )}

      {!searching && mode === 'list' && (folders.length > 0 || files.length > 0) && (
        <DriveFilesTable pane={pane} />
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
