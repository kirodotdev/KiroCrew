import { useEffect } from 'react'
import { motion } from 'framer-motion'
import { CornerDownRight } from 'lucide-react'

import FolderGlyph from './FolderGlyph'
import { i18nT } from '../i18n/t'
import { isMac, platformShortcut } from '../utils/platform'

/**
 * How long a drag-move stays undoable.
 *
 * Long enough to notice the bar, read where the item went, and aim for the
 * button; short enough that the offer never outlives the mistake it belongs to.
 *
 * The DEADLINE is enforced by the owner of the offer ({@link useMoveUndo}), not
 * here: an offer whose optimistic move never became visible has no bar to run a
 * timer, and must still die on this same clock. This component only draws the
 * remaining time.
 */
export const MOVE_UNDO_MS = 8000

/**
 * The inverse of one drag-move, i.e. everything undo needs to put it back.
 *
 * Deliberately names the moved thing `item` rather than `session`: the sidebar's
 * session drag is the first caller, not the only intended one, and a field
 * called `slotKey` would be a lie the moment an artifact or a folder subtree
 * arms the same offer.
 */
export type MovedItem = {
  /** Item that moved — a slot key, artifact slug, or folder id. */
  itemKey: string
  /** Folder it came FROM — where undo puts it back (`null` = unfiled root). */
  fromFolderId: string | null
  /** Folder it landed in. Compared against live state to drop a stale offer. */
  toFolderId: string | null
  /** Destination folder name; `null` renders the unfiled label instead. */
  toFolderName: string | null
  /** Destination folder color, for the glyph tint. */
  toFolderColor?: string
  /** Destination folder emoji icon, so the glyph matches the sidebar's. */
  toFolderIcon?: string
  /** Title of the moved item — the hover tooltip, since the row has no room. */
  itemTitle: string
}

type Props = {
  moved: MovedItem
  onUndo: () => void
  /** Reports whether the pointer is over the bar or focus is inside it, so the
   *  owner can suspend the expiry deadline rather than pull the button out from
   *  under a hand that is already reaching for it. */
  onHoldChange?: (held: boolean) => void
  /**
   * Time left on the offer, and whether that clock is currently suspended.
   *
   * Both come from the owner because the owner enforces the deadline: a
   * countdown that ran on its own mount clock would keep draining while a hover
   * had frozen the real deadline, so the bar would read "expired" while Undo was
   * still live — and it is the hover path, the slow reader the hold exists for,
   * that would see it. Drawing the owner's own remainder makes the two agree by
   * construction rather than by two timers happening to match.
   */
  remainingMs?: number
  paused?: boolean
  /**
   * Drop the fixed "Moved to" prefix so the DESTINATION survives in a narrow
   * sidebar.
   *
   * The sidebar resizes down to `SIDEBAR_MIN` (180px), where the prefix plus the
   * button plus the shortcut consume the whole row and truncate the folder name
   * to nothing: the one thing this bar exists to say would be the first thing to
   * go. Mirrors the header's own `compactHeader` / `tinyHeader` steps, which drop
   * the "Sessions" label at narrow widths for the same reason.
   */
  compact?: boolean
  /**
   * Also name the MOVED ITEM in the visible row, not only in the hover tooltip.
   *
   * The sidebar never needs this: the moved row visibly relocates, so the bar
   * only has to say where it went. In the artifacts LIBRARY the card vanishes
   * from the current view on drop, so "Moved to Archive" alone leaves the user
   * guessing which card went — and the wide page has the room the sidebar's
   * `compact` mode was rationing. Renders the tooltip's own " — <item>"
   * composition, so no new catalog strings are introduced.
   */
  showItemTitle?: boolean
}

/**
 * Confirmation + undo for an item dragged into a folder.
 *
 * A drag is a coarse gesture: drop an item one row off and it vanishes into a
 * folder the user never sees, with nothing on screen saying where it went. This
 * bar names the destination and offers the inverse move back for
 * {@link MOVE_UNDO_MS}.
 *
 * The `session*` i18n keys and `session-move-undo*` test ids are retained rather
 * than renamed with the component: the strings are translated in every locale
 * catalogue and the ids are what the screenshot capture script selects on, so
 * renaming them would be churn and breakage for no behavioural gain.
 *
 * ## Why it lives in the flow rather than floating
 *
 * It renders as a sibling BELOW the session lanes and ABOVE the "Older
 * Sessions" footer, so it occludes nothing: not the footer (a persistent
 * control) and not the list row that just moved — which is exactly the row the
 * user needs to see to judge whether the drop was right. The cost is that the
 * footer shifts down by the bar's height while it is up; that is paid back by
 * the 150ms height transition, and by never covering the evidence.
 *
 * ## Keyboard
 *
 * ⌘Z / Ctrl+Z fires undo while the bar is up — the platform undo chord, NOT
 * ⌘C (copy). The handler stands down while focus is in a text field so the
 * composer keeps its own undo history, and ignores ⇧⌘Z (redo).
 *
 * ## The offer's lifecycle (owned by {@link useMoveUndo}, documented here)
 *
 * This component only renders an offer; the hook decides when one exists. That
 * state machine is the part a future editor is most likely to break, so it is
 * written down once, here, rather than inferred from scattered comments:
 *
 * ```
 *   drag drop ──► PENDING ──ack──► LIVE ──► (undo | expiry | mismatch) ──► gone
 *                    │                                                      ▲
 *                    └── another placement observed → superseded ───────────┘
 * ```
 *
 *  - **PENDING** — the move is optimistic, so the store shows the destination
 *    before the server has agreed. The bar is NOT rendered yet: arming here
 *    would let undo fire against stale server state and be silently reversed
 *    when the original write lands.
 *  - **superseded** — while pending, the only legitimate placements are the
 *    origin (write not applied / rolled back) and the destination (applied).
 *    Any third value is another client's move landing inside the window; it is
 *    latched, because by ack time live state may match the destination again
 *    (a move away and back) and nothing later could tell.
 *  - **LIVE** — the server acknowledged and nothing was latched. The bar
 *    renders; an 8s deadline owned by the hook retires it.
 *  - **gone** — one-way. An offer is dropped, never re-validated, so a retired
 *    offer cannot come back and replay its inverse over a newer move.
 */
export default function MoveUndoBar({
  moved,
  onUndo,
  onHoldChange,
  compact = false,
  showItemTitle = false,
  remainingMs = MOVE_UNDO_MS,
  paused = false,
}: Props) {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== 'z' || e.shiftKey || e.altKey) return
      if (!(isMac ? e.metaKey : e.ctrlKey)) return
      // Typing surfaces own the chord: ChatInput keeps its own undo history,
      // and hijacking it would silently revert a folder move while the user
      // was only trying to un-type a word.
      const el = e.target as HTMLElement | null
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return
      e.preventDefault()
      onUndo()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onUndo])

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
      className="shrink-0 overflow-hidden"
      data-testid="session-move-undo"
      onMouseEnter={() => onHoldChange?.(true)}
      onMouseLeave={() => onHoldChange?.(false)}
      onFocusCapture={() => onHoldChange?.(true)}
      onBlurCapture={() => onHoldChange?.(false)}
    >
      {/* role=status: the whole row is the announcement, so the destination and
          the undo affordance are read together without a duplicate sr-only copy. */}
      <div className="flex items-center gap-1.5 border-t border-border bg-accent-subtle px-2.5 py-1.5" role="status" aria-live="polite">
        <CornerDownRight size={12} className="shrink-0 text-accent" />
        {/* title carries the full sentence, so the compact row is still readable
            on hover when the prefix and the shortcut are dropped. */}
        <span
          className="flex min-w-0 flex-1 items-center gap-1 text-[12px] text-text"
          title={moved.toFolderName === null
            ? `${i18nT('components.sessionMoveUndoBar.removed_from_folder')} — ${moved.itemTitle}`
            : `${i18nT('components.sessionMoveUndoBar.moved_to')} ${moved.toFolderName} — ${moved.itemTitle}`}
        >
          {/* The root case is its own sentence, not "Moved to <noun>": the sidebar
              calls this "remove from folder" on its own drop zone, and "Unfiled"
              is Artifacts vocabulary this surface never shows. */}
          {moved.toFolderName === null ? (
            <span className="truncate font-medium text-text-strong">{i18nT('components.sessionMoveUndoBar.removed_from_folder')}</span>
          ) : (
            <>
              {!compact && <span className="shrink-0">{i18nT('components.sessionMoveUndoBar.moved_to')}</span>}
              <FolderGlyph color={moved.toFolderColor} icon={moved.toFolderIcon} size={12} className="shrink-0" />
              <span className="truncate font-medium text-text-strong">{moved.toFolderName}</span>
            </>
          )}
          {/* Same " — <item>" composition the tooltip uses, promoted into the
              visible row for surfaces where the moved item disappears on drop
              (the library card leaves the current view; a sidebar row merely
              relocates). Punctuation-joined, so no new catalog string. */}
          {showItemTitle && (
            <span className="truncate text-muted" data-testid="session-move-undo-item">— {moved.itemTitle}</span>
          )}
        </span>
        {/* Face reads "Undo" and nothing else — the chord is a power shortcut, not
            part of the label. It stays discoverable in the tooltip and declared to
            assistive tech via aria-keyshortcuts, so dropping the visible hint
            costs no capability. */}
        <button
          type="button"
          data-testid="session-move-undo-button"
          onClick={onUndo}
          title={`${i18nT('components.sessionMoveUndoBar.undo')} ${platformShortcut('Cmd+Z')}`}
          aria-keyshortcuts={isMac ? 'Meta+Z' : 'Control+Z'}
          className="shrink-0 cursor-pointer rounded-[5px] border border-border-strong bg-transparent px-1.5 py-px text-[11px] text-accent hover:bg-bg-hover focus-ring"
        >
          {i18nT('components.sessionMoveUndoBar.undo')}
        </button>
      </div>
      {/* Time left, as motion rather than a CSS animation: the global
          prefers-reduced-motion rule clamps every CSS animation to 0.01ms,
          which would empty this bar instantly and read as "already expired".

          Driven by the owner's remainder, so scaleX is always
          `remaining / MOVE_UNDO_MS`: a linear run from that fraction over
          exactly that remainder keeps the identity true at every instant, and a
          hold freezes the width where it stands instead of draining under a
          deadline that has stopped. */}
      <motion.div
        aria-hidden
        data-testid="session-move-undo-countdown"
        className="h-[2px] origin-left bg-accent"
        initial={{ scaleX: 1 }}
        animate={{ scaleX: paused ? remainingMs / MOVE_UNDO_MS : 0 }}
        transition={paused ? { duration: 0 } : { duration: remainingMs / 1000, ease: 'linear' }}
      />
    </motion.div>
  )
}
