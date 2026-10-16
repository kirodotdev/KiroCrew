/**
 * AWS Control drive - the Files pane's state: the open folder (restored from the
 * app's view state), the paged listing, search, and the requests the pane's own
 * controls make (upload, delete, folder create and delete, move, rename, download),
 * together with the drag-and-drop wiring and the menu-to-dialog focus hand-back.
 * The preview and share dialogs (`DrivePageFileDialogs.tsx`) make their own.
 *
 * `DriveSectionView` (`DrivePageFiles.tsx`) calls `useDrivePageFiles` once per
 * render, as its only hook, so the pane registers its hooks in one fixed order.
 * The pane's views (`DrivePageFilesViews.tsx`) read the same object,
 * `DriveFilesPane`.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  useInfiniteQuery, useMutation, useQueryClient, keepPreviousData,
} from '@tanstack/react-query'
// By path, not from the app-sdk barrel: that barrel is published to third-party
// apps through the vendor stub, and a host-namespaced cache client is not part
// of that contract.
import { useAppQuery, useAppQueryKey } from '../../app-sdk/appQuery'
// Imported by path, not from the app-sdk barrel: that barrel is the surface published to
// third-party apps, and this store is builtin-only.
import { useAppViewState, isViewString, type ViewStateDecl } from '../../app-sdk/viewState'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { i18nT } from '../../i18n/t'
import { awsControlApi, AwsControlError } from './api'
import { useViewMode } from './DrivePageControls'

/** Client-side key-segment validation, matching the backend's charset rule. */
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
export type Failure = { message: string; error?: unknown }

/**
 * What the drive restores when you come back to it.
 *
 * One field. The rest of "where I was" is already durable elsewhere and deliberately
 * stays there: the pane is in the URL (`usePaneFromPath`), the grid/list choice is in
 * `useViewMode`, and the selected account is in `awsControl.selectedAccount` — which is
 * this record's SCOPE rather than one of its fields, so a prefix can never be restored
 * into a bucket it was not taken in.
 *
 * The listing itself is not here and cannot be: `contents` is not a declared field, so
 * `pickDeclared` drops it on write even if a caller passes it in. Server-owned data is
 * refetched, per the design's three-tier ownership split.
 *
 * Module-level, so the hook sees one stable declaration rather than a new object per
 * render.
 */
const DRIVE_VIEW: ViewStateDecl<{ path: string }> = {
  // Names this SURFACE, not the app: the library and backup panes are separate consumers
  // and get their own records, so neither can erase this one's folder.
  name: 'drive',
  revision: 1,
  fields: { path: isViewString },
  defaults: { path: '' },
}

/**
 * The Files pane's state for one account's drive, as one object. The body is the
 * pane's own: the order of its hooks is the order the pane registers them in.
 */
export function useDrivePageFiles(account: string) {
  const qc = useQueryClient()
  const appKey = useAppQueryKey()
  const [mode, setMode] = useViewMode('drive', 'list')
  // The current folder is the app's view state, restored on mount from the host's
  // namespace. `path` is read from the store rather than held in local state so there is
  // one source of truth for it, and it is available on the FIRST render — the infinite
  // query below is keyed on it, so a restored folder is what gets fetched, with no
  // wasted listing of the root and no skeleton flash on the way to the right place.
  //
  // Scoped to `account`, because a prefix only means anything inside the bucket it was
  // taken in. The prop is always a resolved, non-empty account id by the time this
  // renders: `AwsControlPage` returns the accounts pane while `!selected`, and
  // `DrivePaneGate` yields children only once `drive?.exists`. Were it briefly `''`, the
  // scope would never match and a restore would silently never happen.
  const [view, setView] = useAppViewState(DRIVE_VIEW, { scope: account })
  const path = view.path
  const setPath = useCallback((next: string) => setView({ path: next }), [setView])
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

  /* One listing per PATH, ACCUMULATED across pages -- the same shape the Library
     pane uses (`DrivePageLibrary.tsx`). A continuation token is a page OF one listing, not a different
     listing, so it belongs in the page params and never in the query key:
     keying by token would make every "Load more" a brand-new query that
     REPLACES the rows already on screen. Navigation resets fall out of the key itself (a
     new path is a new query), and the explicit 'drive' segment keeps a folder
     literally named "library" from colliding with the Library section's own
     ['drive', account, 'list', 'library'] key. The key stays under
     the ['drive', account] prefix every mutation invalidates, and
     invalidating an infinite query refetches every page it holds, so a deep
     list survives an upload or delete instead of collapsing to page one. */
  const listQ = useInfiniteQuery({
    queryKey: appKey(['drive', account, 'list', 'drive', path]),
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
    qc.invalidateQueries({ queryKey: appKey(['drive', account]) })
    qc.invalidateQueries({ queryKey: appKey(['drive-search', account]) })
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
      qc.invalidateQueries({ queryKey: appKey(['drive-list', account]) })
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
      qc.invalidateQueries({ queryKey: appKey(['drive-list', account]) })
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
  const searchQ = useAppQuery(['drive-search', account, debouncedQuery], {
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

  return {
    mode, setMode, path, setPath, share, setShare, uploadError, downloadError, preview, setPreview,
    renaming, renameValue, setRenameValue, renameError, query, setQuery, debouncedQuery,
    setHighlightKey, highlighted, highlightProps, confirmDelete, setConfirmDelete, confirmFolder,
    setConfirmFolder, newFolder, setNewFolder, creatingFolder, setCreatingFolder, folderError,
    folderToggleRef, moveTarget, rememberMenuOpener, skipRestoreIfDialog, openShare, openMove,
    returnFocusToOpener, moveDialogOpenRef, closeMoveDialog, closeFolderDisclosure, handOff,
    deletedHere, fileRef, attachScroller, edges, listQ, folders, files, uploadMut, deleteMut,
    openConfirmDelete, deleteFailedFor, folderCreateMut, folderDeleteMut, onCreateFolder, onPick,
    dropTarget, moveError, setMoveError, pageError, moveMut, moveBusy, movingKey, renameMut,
    openRename, closeRename, commitRename, searching, searchQ, dropProps, inertDropProps, dragProps,
    download, crumbs,
  }
}

/** What `useDrivePageFiles` returns: everything the Files pane's markup reads. */
export type DriveFilesPane = ReturnType<typeof useDrivePageFiles>
