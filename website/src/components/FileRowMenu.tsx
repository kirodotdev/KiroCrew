/**
 * The row menu every file-listing surface in the side panel opens: the Files
 * tab's workspace tree (`PierreWorkspaceTreeImpl`) and the folder tab's flat
 * listing (`FolderPanel`, for a directory that is not the chat's project root,
 * where `/api/project/tree` cannot answer and the tree is not available).
 *
 * ONE component for both, lifted out of the tree (#43258): a row that offers
 * Download in one surface and the browser's own context menu in the other is
 * the divergence a second menu invites. The built-in rows are the same here as
 * they were in the tree -- "Add to chat" (gated on the host wiring it), a
 * file row's "Download", a directory row's "Upload files…" and "New file…" (each
 * gated on its host handler) -- followed by the rows installed apps contribute
 * for the surface in question.
 *
 * Hosts differ only in how a menu is opened: Pierre opens it on its own rows and
 * hands over an open context; the listing row opens it from right-click or its
 * hover button and builds the same shape (`FileRowMenuAnchor`). Every action
 * closes the menu itself so focus returns to the row.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { AtSign, Download, FilePlus, Upload } from 'lucide-react'
import { OverlayPortal } from './overlayRoot'
import { useMenuKeyboard } from '../hooks/useMenuKeyboard'
import { i18nT } from '../i18n/t'
import {
  FileMenuItemIcon,
  FileMenuItemLabel,
  invokeFileMenuItem,
  visibleFileMenuItems,
  type ContributedFileMenuItem,
  type ReportFileMenuError,
} from '../apps/fileMenuContributions'
import type { FileMenuSurface } from '../api/client'
import { downloadFileToDisk } from '../utils/fileReadUrl'
import { isUntokenizableDirPath } from '../utils/dropClassify'

/** The kind vocabulary the composer's `@`-mention plumbing speaks: a file is
 *  staged + tokenized, a folder becomes a bare `@rel/` reference. */
export type FileRowKind = 'file' | 'dir'

/** The row a menu is open for: its ABSOLUTE path on the gateway. */
export interface FileRowMenuNode {
  path: string
  kind: FileRowKind
}

/**
 * Where the menu hangs and how it closes. Structurally the subset of Pierre's
 * `ContextMenuOpenContext` this menu reads, so the tree passes its context
 * through unchanged and the listing builds one from the row it opened on.
 */
export interface FileRowMenuAnchor {
  anchorElement: HTMLElement
  anchorRect: Readonly<{ top: number; right: number; bottom: number; left: number; width: number; height: number }>
  close: () => void
}

/** App-contributed rows for one surface, resolved by the PARENT (which already
 *  holds the `['apps']` query) and passed down. A prop rather than a hook call
 *  here: this component mounts inside a menu slot, and a `useQuery` in the leaf
 *  would make every host -- and every test rendering just this menu -- require
 *  a `QueryClientProvider` it never needed. `root` goes into the dispatch
 *  payload when the surface carries one (the tree's `tree-context` does). */
export interface FileRowMenuContributions {
  surface: FileMenuSurface
  items: readonly ContributedFileMenuItem[]
  root?: string
}

const MENU_ITEM_CLASS =
  'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[12.5px] text-text ' +
  'cursor-pointer hover:bg-bg-hover focus:bg-bg-hover outline-hidden'

/** The folder reference the composer would write for `path`: project-relative
 *  inside `projectRoot`, the absolute path elsewhere (`makeRelative` in the
 *  composer host draws the same line). The token check has to see THAT string,
 *  not always the absolute one: a project root with a space in it would
 *  otherwise refuse every folder under it although `@src/` tokenizes fine. */
function mentionPath(path: string, projectRoot: string): string {
  const root = projectRoot.replace(/\/+$/, '')
  return root && path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path
}

export default function FileRowMenu({ node, anchor, projectRoot, onAddToContext, onUploadRequest, onNewFileRequest, hostFileActions = true, contrib, onError }: {
  node: FileRowMenuNode
  anchor: FileRowMenuAnchor
  /** The chat's project root, for the folder-token check above; '' when the
   *  chat has none. */
  projectRoot: string
  onAddToContext?: (absPath: string, kind: FileRowKind) => void
  /** Right-click "Upload files…" on a directory row: hands the host the
   *  ABSOLUTE directory path so it can open the OS file picker and upload
   *  the selection into it. Absent → the row is not rendered. */
  onUploadRequest?: (absDirPath: string) => void
  /** Right-click "New file…": hands the host the ABSOLUTE directory the new
   *  file goes in. Absent → the row is not rendered. */
  onNewFileRequest?: (absDirPath: string) => void
  /** Whether the host has the dashboard's own file actions (the tree client's
   *  `hostFileActions`): false drops the built-in Download, because an
   *  app-hosted tree reads through its own scoped client and
   *  `/api/file-download` is not among its reads. Defaults to true. */
  hostFileActions?: boolean
  contrib: FileRowMenuContributions
  /** Where a contributed row's dispatch failure goes. Owned by the parent because
   *  activating a row closes this menu, so it cannot render the notice itself. */
  onError: ReportFileMenuError
}) {
  const isDir = node.kind === 'dir'
  const abs = node.path
  const rows = visibleFileMenuItems(contrib.items, { path: abs, kind: node.kind })
  // "Add to chat" on a folder writes an `@path/` folder reference, which cannot
  // carry whitespace or `@`; for such a folder the row is shown disabled with
  // the reason rather than inserting a reference that never reaches the send
  // (the drag refuses the same folders with the same note).
  const folderRefused = !!onAddToContext && isDir && isUntokenizableDirPath(mentionPath(abs, projectRoot).replace(/\/+$/, ''))
  const addToChat = folderRefused ? undefined : onAddToContext
  const hasBuiltinChatRow = !!addToChat || folderRefused
  // role="menuitem" divs (an interactive ARIA role) with a keyboard handler:
  // the correct menu semantics inside the role="menu" container, and the role
  // is what makes an onClick div compliant rather than a static-element one.
  const activate = (run: () => void) => (e: React.MouseEvent | React.KeyboardEvent) => {
    if ('key' in e) {
      if (e.key !== 'Enter' && e.key !== ' ') return
      e.preventDefault()
    }
    anchor.close()
    run()
  }
  // "Upload files…" renders only on a directory row, and only when the host
  // supplied the handler: this same component instance answers every row the
  // host opens a menu for, and a FILE row must never show the directory-only
  // upload action even when the host wired it for other rows.
  const showUpload = isDir && !!onUploadRequest
  // "New file…" creates inside a directory row, and beside a file row (in its
  // folder, as editors do), which is how a file reaches the project root when
  // the root lists one: the tree has no row for the root itself.
  const newFileDir = isDir ? abs : abs.slice(0, abs.lastIndexOf('/') + 1)
  // Focus the first item on open. Pierre's own open path calls `item.focus()`
  // on the tree ROW, never on this slotted content, so a keyboard user who
  // opens the menu (Shift+F10 / the ContextMenu key) would otherwise be left
  // with focus on the row: the Enter/Space handler below sits on the
  // `tabIndex={-1}` menuitem and could never fire, and Enter would re-trigger
  // the row instead. Closing restores focus to the row (Pierre's
  // `restoreFocus`; the listing row's own close), so this does not strand
  // focus inside a dismissed menu.
  const firstItemRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // Focus the first row on open: the built-in "add to context" row when a host
    // supplies onAddToContext, otherwise the first app-contributed row. The
    // querySelector arm is not redundant with the ref: the built-in row is gated on
    // onAddToContext, so in an app-only menu `firstItemRef` is attached to a row that
    // may itself have been filtered out by its `when` predicate.
    ;(firstItemRef.current
      ?? menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]'))?.focus()
  }, [])
  // The shared role="menu" keyboard contract (#6231). The item count is not
  // fixed: the built-in "Add to chat" row is gated on `onAddToContext`, the
  // built-in "Upload files…" row is gated on `onUploadRequest` AND a directory
  // row, the built-in "New file…" row on `onNewFileRequest`, and an installed
  // app may contribute any number of rows its `when` admits — so this menu can
  // hold one row or several and the arrows do real navigation whenever it
  // holds more than one. Honouring the contract per-surface-by-item-count is
  // how surfaces drift anyway — an arrow inside an open menu must be consumed
  // rather than scrolling the list behind it, Tab must stay contained (#2533),
  // and IME composition keys must not reach the menu at all, all true whatever
  // the count. `enabled: true` unconditionally because a host only mounts this
  // component while the menu is open. focusFirstOnOpen: false — the
  // firstItemRef effect above already owns focus entry.
  const menuRef = useRef<HTMLDivElement>(null)
  useMenuKeyboard({ enabled: true, containerRef: menuRef, focusFirstOnOpen: false })
  // PORTALED to the body-level overlay root, positioned from the anchor rect
  // (#10100). Pierre's default slot placement puts the menu in a width-0 slot
  // hung at the row's trailing edge, inside the tree root -- and that root is
  // `overflow: hidden`, so with this app's `--trees-padding-inline-override:
  // 0px` the slot sits ~7px from the panel's right edge and the menu clips to
  // a sliver flush against the border at EVERY panel width (it reads as a
  // truncated, unclickable "..." control; the same in-slot growth is what shifted
  // the trigger vertically while open). The library documents the escape
  // hatch: a portaled menu marked `data-file-tree-context-menu-root="true"`
  // still counts as inside for Pierre's outside-click wash and Escape close.
  //
  // Placement: below the anchor, right edges aligned for the "..." button (its
  // rect has width; a right-click anchor is a zero-width point and aligns
  // left), clamped into the viewport and flipped above when the bottom would
  // overflow -- measured in a layout effect so the first painted frame is
  // already at its final position (hidden until measured).
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  useLayoutEffect(() => {
    const menu = menuRef.current
    if (!menu) return
    const a = anchor.anchorRect
    const m = menu.getBoundingClientRect()
    const margin = 8
    let left = a.width > 0 ? a.right - m.width : a.left
    left = Math.min(left, window.innerWidth - m.width - margin)
    left = Math.max(margin, left)
    let top = a.bottom + 2
    if (top + m.height > window.innerHeight - margin) {
      top = Math.max(margin, a.top - m.height - 2)
    }
    setPos({ top, left })
    // `rows.length` is a dep because a contributed-row refetch while the menu
    // is open changes the menu's height, and a stale measurement would let the
    // grown menu run past the bottom clamp.
  }, [anchor, rows.length])
  // DISMISS when the row can move out from under the fixed-position menu.
  // The tree renders inside a SHADOW ROOT and `scroll` is a non-composed
  // event, so a window listener never sees the virtualized tree's own
  // scroller (the drift source that matters) while it DOES fire for
  // unrelated light-DOM scrolls -- a streaming reply auto-scrolling the chat
  // transcript would snatch a just-opened menu with no action taken. So the
  // scroll listener goes capture-phase on the ANCHOR'S OWN root node (the
  // tree's shadow root; the document for a light-DOM listing row) and acts
  // only on a scroll whose container CONTAINS the anchor: inside a shadow root
  // every scroller does, while on the document the transcript's programmatic
  // scrolls (a streaming reply, a follow re-pin) target containers the row is
  // not in, and those must leave the menu alone. Row movement without a
  // scroll -- the rail or panel being drag-resized -- is covered by a
  // ResizeObserver on the shadow host (skipping its mandatory initial
  // delivery), and window resize stays as the cheap catch-all. Close rather
  // than re-track: it is the native context-menu convention, and a
  // right-click anchor is a pointer POINT that no element rect can re-derive
  // after the rows have moved.
  useEffect(() => {
    const onDismiss = () => anchor.close()
    const onScroll = (e: Event) => {
      // `document` itself scrolling (target is the Document) contains everything.
      if (e.target instanceof Node && e.target.contains(anchor.anchorElement)) onDismiss()
    }
    const root = anchor.anchorElement.getRootNode()
    root.addEventListener('scroll', onScroll, true)
    let ro: ResizeObserver | null = null
    const host = root instanceof ShadowRoot ? root.host : null
    if (host && typeof ResizeObserver !== 'undefined') {
      let initialDelivery = true
      ro = new ResizeObserver(() => {
        if (initialDelivery) {
          initialDelivery = false
          return
        }
        anchor.close()
      })
      ro.observe(host)
    }
    window.addEventListener('resize', onDismiss)
    return () => {
      root.removeEventListener('scroll', onScroll, true)
      ro?.disconnect()
      window.removeEventListener('resize', onDismiss)
    }
  }, [anchor])
  // Render nothing rather than an empty bordered popup: with no host row, no
  // Download (a directory with no upload handler), no upload row (absent
  // handler, or a file row), no New file row (absent handler), AND no app
  // row that its `when` admits for this node, there is nothing to show and no
  // menuitem for the focus effect to land on.
  // A file row also carries a built-in Download (retrieve the bytes onto the
  // machine running the browser). Directories do not: the ask is file rows
  // only, and /api/file-download serves a single file, not a folder.
  const canDownload = !isDir && hostFileActions
  if (!hasBuiltinChatRow && !canDownload && !showUpload && !onNewFileRequest && rows.length === 0) return null
  return <OverlayPortal>
    <div
      ref={menuRef}
      role="menu"
      data-file-tree-context-menu-root="true"
      style={pos ? { top: pos.top, left: pos.left } : { top: anchor.anchorRect.bottom + 2, left: anchor.anchorRect.left, visibility: 'hidden' }}
      className="fixed z-50 min-w-[176px] max-w-[min(420px,calc(100vw-2rem))] rounded-lg border border-border bg-bg-elevated p-1 shadow-lg"
    >
      {addToChat && (
        <div
          ref={firstItemRef}
          role="menuitem"
          tabIndex={-1}
          className={MENU_ITEM_CLASS}
          onClick={activate(() => addToChat(abs, node.kind))}
          onKeyDown={activate(() => addToChat(abs, node.kind))}
        >
          <AtSign className="lucide-inline text-muted" />
          {i18nT('pages.chat.fileBrowserRail.ctx_add_to_chat')}
        </div>
      )}
      {folderRefused && (
        <div
          ref={firstItemRef}
          role="menuitem"
          tabIndex={-1}
          aria-disabled="true"
          data-testid="file-tree-add-to-chat-refused"
          className="flex items-start gap-2 rounded-md px-2.5 py-1.5 text-[12.5px] text-muted cursor-default focus:bg-bg-hover outline-hidden"
        >
          <AtSign className="lucide-inline mt-0.5" aria-hidden="true" />
          <span className="flex flex-col">
            <span>{i18nT('pages.chat.fileBrowserRail.ctx_add_to_chat')}</span>
            <span className="text-[11.5px]">{i18nT('components.chatInput.tree_drop_folder_refused')}</span>
          </span>
        </div>
      )}
      {/* Built-in Download for a file row. Streams the raw bytes through the
          SAME /api/file-download path the viewer's Download uses; a failure is
          surfaced through the host's error notice.
          Its ref is `firstItemRef` only when there is no Add-to-chat row above
          it to own focus entry. */}
      {canDownload && (
        <div
          ref={hasBuiltinChatRow ? undefined : firstItemRef}
          role="menuitem"
          tabIndex={-1}
          className={MENU_ITEM_CLASS}
          onClick={activate(() => { void downloadFileToDisk(abs, onError) })}
          onKeyDown={activate(() => { void downloadFileToDisk(abs, onError) })}
        >
          <Download className="lucide-inline text-muted" />
          {i18nT('pages.chat.fileBrowserRail.ctx_download')}
        </div>
      )}
      {/* Built-in Upload for a directory row: hands the absolute directory
          path to the host, which opens the OS file picker and POSTs to
          /api/directory-upload. Directory rows only (mutually exclusive with
          Download above, which is file-only), so the firstItemRef handoff
          cannot collide. */}
      {showUpload && (
        <div
          ref={hasBuiltinChatRow ? undefined : firstItemRef}
          role="menuitem"
          tabIndex={-1}
          className={MENU_ITEM_CLASS}
          onClick={activate(() => onUploadRequest?.(abs))}
          onKeyDown={activate(() => onUploadRequest?.(abs))}
        >
          <Upload className="lucide-inline text-muted" />
          {i18nT('pages.chat.fileBrowserRail.ctx_upload_files')}
        </div>
      )}
      {/* No `firstItemRef`: when it is the first row, the focus effect's
          first-menuitem fallback lands on it. */}
      {onNewFileRequest && (
        <div
          role="menuitem"
          tabIndex={-1}
          className={MENU_ITEM_CLASS}
          onClick={activate(() => onNewFileRequest(newFileDir))}
          onKeyDown={activate(() => onNewFileRequest(newFileDir))}
        >
          <FilePlus className="lucide-inline text-muted" />
          {i18nT('pages.chat.fileBrowserRail.ctx_new_file')}
        </div>
      )}
      {/* App-contributed rows (contributes.fileMenuItems, for this host's
          surface). An installed app declares these in its manifest; core POSTs
          the node context to the app's endpoint on activation and never imports
          app code. Already filtered by each row's `when` predicate; the stock
          build (no declaring app) renders nothing. */}
      {rows.map((mi, idx) => {
        const dispatch = () =>
          invokeFileMenuItem(
            mi,
            {
              surface: contrib.surface,
              path: abs,
              kind: node.kind,
              ...(contrib.root !== undefined ? { root: contrib.root } : {}),
            },
            onError,
          )
        return (
          <div
            key={`${mi.app}:${mi.id}`}
            ref={!hasBuiltinChatRow && !canDownload && !showUpload && !onNewFileRequest && idx === 0 ? firstItemRef : undefined}
            role="menuitem"
            tabIndex={-1}
            className={MENU_ITEM_CLASS}
            onClick={activate(dispatch)}
            onKeyDown={activate(dispatch)}
          >
            <FileMenuItemIcon name={mi.icon} />
            <FileMenuItemLabel item={mi} />
          </div>
        )
      })}
    </div>
  </OverlayPortal>
}
