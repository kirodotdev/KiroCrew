/**
 * AWS Control drive - the three ways the Files pane draws objects: search hits
 * from the whole section, and the open folder as a grid of tiles or as a table.
 *
 * Each view reads the pane's state object (`DriveFilesPane`, from
 * `useDrivePageFiles.ts`). The grid and the table carry the same per-item actions:
 * a view mode is a way of looking at the folder, not a capability tier. A search
 * hit carries a file's Download, Rename, Share and Delete, plus Open containing
 * folder.
 */
import { Fragment } from 'react'
import {
  RefreshCw, Share2, Download, Trash2, FolderClosed, FolderOpen, FolderInput, FileText,
  MoreHorizontal, Pencil,
} from 'lucide-react'
import { Btn, Input, ContentSkeleton, IconButton, FilteredEmpty } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
} from '../../components/ui/dropdown-menu'
import { LibraryTableHead, PINNED_SURFACE } from '../../components/library/LibraryTable'
import type { LibraryColumn } from '../../components/library/LibraryTable'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtRelative } from '../../i18n/format'
import { AwsErrorNotice } from './shared'
import { TileConfirm } from './DrivePageControls'
import type { DriveFilesPane } from './useDrivePageFiles'

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

/** Search hits from the whole section, as a table: each hit carries Download, Rename, Share, Open containing folder and Delete. */
export function DriveFilesSearchResults({ pane }: { pane: DriveFilesPane }) {
  const {
    setPath, setPreview, renaming, renameValue, setRenameValue, renameError, setQuery,
    debouncedQuery, setHighlightKey, confirmDelete, setConfirmDelete, rememberMenuOpener,
    skipRestoreIfDialog, openShare, handOff, deleteMut, openConfirmDelete, deleteFailedFor,
    renameMut, openRename, closeRename, commitRename, searchQ, download,
  } = pane
  return (
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
  )
}

/** The open folder as a grid of tiles: folders first, then files. */
export function DriveFilesGrid({ pane }: { pane: DriveFilesPane }) {
  const {
    setPath, setPreview, renaming, renameValue, setRenameValue, renameError, highlighted,
    highlightProps, confirmDelete, setConfirmDelete, confirmFolder, setConfirmFolder,
    rememberMenuOpener, skipRestoreIfDialog, openShare, openMove, handOff, folders, files,
    deleteMut, openConfirmDelete, deleteFailedFor, folderDeleteMut, dropTarget, moveBusy, movingKey,
    renameMut, openRename, closeRename, commitRename, dropProps, dragProps, download,
  } = pane
  return (
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
  )
}

/** The open folder as a table on the shared library head: folders first, then files. */
export function DriveFilesTable({ pane }: { pane: DriveFilesPane }) {
  const {
    setPath, setPreview, renaming, renameValue, setRenameValue, renameError, highlighted,
    highlightProps, confirmDelete, setConfirmDelete, confirmFolder, setConfirmFolder,
    rememberMenuOpener, skipRestoreIfDialog, openShare, openMove, handOff, attachScroller, edges,
    folders, files, deleteMut, openConfirmDelete, deleteFailedFor, folderDeleteMut, dropTarget,
    moveBusy, movingKey, renameMut, openRename, closeRename, commitRename, dropProps, dragProps,
    download,
  } = pane
  return (
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
  )
}
