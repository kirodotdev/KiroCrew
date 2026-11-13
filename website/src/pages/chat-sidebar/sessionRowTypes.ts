/** What a session row takes: its `view` (./rows) and its one `actions` object. */
import type { SessionRowView } from './rows'

/** What a session row can DO: every callback and ref it needs, built ONCE in the
 *  shell and handed to every row, so the row's memo compares one stable reference
 *  here and its data through `sameRowView`. */
export interface SessionRowActions {
  renameInputRef: React.MutableRefObject<HTMLTextAreaElement | null>
  onRenameStart: (key: string, scope: string, title: string, fromMenu: boolean) => void
  onRenameChange: (value: string) => void
  onRenameCommit: (key: string, value: string) => void
  slotRenameEnterRef: React.MutableRefObject<boolean>
  onRenameCancel: () => void
  /** The slot whose rename was refused offline, so only that row shows the
   *  neutral notice. Null when no refusal stands. */
  renameOfflineSlot: string | null
  /** The placement scope the refusal happened in, compared alongside the slot.
   *  A multi-tag chat renders one row per board column, so the slot alone
   *  matches every copy. Null when no refusal stands. */
  renameOfflineScope: string | null
  /** The row menu's "Regenerate title": re-derives the title from the
   *  conversation without making the session active. */
  onAutoTitle: (key: string) => void
  onDuplicate: (key: string) => void
  onCloseSession: (key: string) => void
  onMenuCloseAutoFocus: (e: Event) => void
  onSelectSlot?: (key: string) => void
  onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void
  onOpenSource?: (slotKey: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  /** Open a row whose session lives on a remote instance as a window onto the
   *  peer's own slot. Distinct from `onSelectSlot` because the session has no
   *  local slot: the peer owns it. */
  onOpenPeerSession: (instanceId: string, remoteSlot: string) => void
  /** A board card drags with native HTML5 DnD, outside every DndContext, so the
   *  shell learns of that drag only from the row: called with the slot key at
   *  `dragstart`. The end is read at the window (see `useNativeSessionDrag`). */
  onNativeDragStart: (key: string) => void
  /** The one end the window cannot hear: the row unmounting while its own
   *  native drag is still in flight (its lane changed under it), after which
   *  the browser's `dragend` lands on a detached node. */
  onNativeDragEnd: () => void
  onPinnedKeyboardReorder: (key: string, container: string, delta: -1 | 1, row: HTMLElement) => void
  /** Alt+Arrow on an UNPINNED row: the manual order's move, as a drop makes it. */
  onManualKeyboardReorder: (key: string, container: string, delta: -1 | 1, row: HTMLElement) => void
  /** Go where a row whose session belongs to another page lives
   *  (`view.opensElsewhere`): click and Enter land here instead of `switchSlot`. */
  openElsewhere: (path: string) => void
  /** The conductor lane's chevron: open or close the sessions this row opened. */
  toggleConductor: (identity: string) => void
  /** Stage a reference to a session in the active chat's composer — the same
   *  callback the drag-into-the-composer drop fires. The row menu's "Reference
   *  in chat" item calls it for THIS row's session after the row has checked
   *  `sessionRefBlockReason`; `canStageSessionRef` gates whether a composer
   *  exists to stage into at all. Undefined on a surface with no chat pane
   *  (the embed sessions list), which hides the item. */
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
  /** Whether a composer exists to stage a session reference into (not split
   *  view, an active slot present). The row combines it with the per-row block
   *  reason to decide whether to offer "Reference in chat". */
  canStageSessionRef?: boolean
}

export interface SessionRowProps {
  /** Everything the row renders from, already masked for its origin by the row
   *  model (./rows): a peer row's view carries no local state. */
  view: SessionRowView
  actions: SessionRowActions
}
