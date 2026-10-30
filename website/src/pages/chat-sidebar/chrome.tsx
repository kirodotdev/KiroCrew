/** The sidebar's shared row and folder chrome: the open-in-tab gesture, the
 *  disclosure chevron, the pinned-section divider, the folder body geometry and
 *  text-control classes that the session row, the folder blocks and the lanes all
 *  draw with, and the date-segment label the flat lane and the Older Sessions pane
 *  head their groups with. */
import { ChevronRight } from 'lucide-react'
import { IS_MAC } from '../../hooks/useKeyboardShortcuts'
import { i18nT } from '../../i18n/t'
import { fmtDateFields } from '../../i18n/format'

/** Is this click the "open as a tab" modifier gesture? One predicate for every
 *  surface that offers it (session rows, the New button, the folder create
 *  entries), so the platform split cannot drift between them. The split is deliberate: Ctrl+click IS a
 *  right-click on macOS, so honouring it there would fire this and the context
 *  menu from one press; Cmd is the tab modifier there. Shift and Alt are
 *  excluded because both carry other meanings in the sidebar (range/reorder). */
export function isOpenInTabModifierClick(e: React.MouseEvent): boolean {
  return (IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey) && !e.shiftKey && !e.altKey
}

/** Tab mode for a click on a CREATE control (the header New button, a folder
 *  `+`, an empty-folder row). Cmd/Ctrl-click opens the new chat as a background
 *  tab, the same as on a session row. Cmd/Ctrl+Shift-click opens it as the
 *  active tab, the browser's convention for a foreground open, and the only
 *  one-step foreground gesture on a trackpad. Any other click returns false.
 *  See docs/decisions/2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md. */
export function createTabModeForClick(e: React.MouseEvent): 'foreground' | 'background' | false {
  if (isOpenInTabModifierClick(e)) return 'background'
  const tabModifier = IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey
  return tabModifier && e.shiftKey && !e.altKey ? 'foreground' : false
}

/** Quiet boundary between manually ordered pins and automatically sorted rows.
 *  Starts 2px left of the row content column: `mx-3` 12 against the rows' 14px
 *  pad (`ROW_BOX_CLS` `pl-3.5`), which is the same pad inside a folder body, so
 *  no per-body override is needed. `mr-3` keeps the right edge on the rows'
 *  `pr-3`.
 *
 *  When a tag filter or status chip narrows the list, `caption` is set to
 *  "Pinned · always shown" (or its locale equivalent). The captioned form labels
 *  the band so the viewer knows why the pinned rows are exempt from the active
 *  filter. The plain form (no caption) renders `aria-hidden` because it is purely
 *  decorative; the captioned form omits it so the text is reachable. */
export function PinnedSessionDivider({ caption }: { caption?: string } = {}) {
  if (caption) {
    return (
      <div
        data-testid="pinned-session-divider"
        data-pinned-divider=""
        className="mx-3 my-1 shrink-0 flex items-center gap-1.5"
      >
        <span className="flex-1 h-px bg-border-strong opacity-70" aria-hidden="true" />
        <span className="text-[11px] text-text/70 select-none leading-none">{caption}</span>
        <span className="flex-1 h-px bg-border-strong opacity-70" aria-hidden="true" />
      </div>
    )
  }
  return (
    <div
      data-testid="pinned-session-divider"
      data-pinned-divider=""
      aria-hidden="true"
      className="mx-3 my-1 h-[4px] shrink-0 border-y border-border-strong opacity-70"
    />
  )
}

/** The sidebar's ONE disclosure-chevron grammar (#2887): a ChevronRight that
 * rotates 90° when its section is open — animated at one shared duration —
 * and sits unrotated when closed. Every stateful disclosure in this pane
 * renders through here, which rules out the drift modes by construction:
 * Right/Down glyph swaps, counter-rotation when closed (the pre-#2884
 * defect), inline-style rotation, and divergent durations. Position is the
 * one deliberate asymmetry (the Older Sessions section header trails; row
 * disclosures lead) — see the comment at the header call site. */
export function DisclosureChevron({ open, size, className = '' }: { open: boolean; size: number; className?: string }) {
  return <ChevronRight size={size} className={`shrink-0 transition-transform duration-200 ${open ? 'rotate-90' : ''} ${className}`.trimEnd()} />
}

/** Animated collapsible for unknown-height content (folder bodies).
 *  Uses CSS grid `1fr`/`0fr` trick so we can animate to intrinsic height
 *  without measuring. For fixed-height panels use Framer Motion instead. */
/** The nested folder body's own left inset, in px — and the `D` term in the
 *  sidebar's alignment algebra (see renderFolderHeader).
 *
 *  It exists for the collapse animation: the body animates through
 *  `grid-template-rows` with `overflow: hidden`, and without a little padding the
 *  children's focus rings and the connector's rounded corner clip against that
 *  edge. The LEFT component is the load-bearing one — it shifts the whole nested
 *  subtree right by this much relative to the folder header that sits above it,
 *  which is why the header's own pad has to be `D + ml-3` to keep the folder glyph
 *  on the connector line.
 *
 *  Named and exported rather than inlined because that offset is what has broken
 *  the sidebar's alignment guides four times: it is invisible in the class list, so
 *  every attempt to derive the geometry from Tailwind classes alone has been 2px
 *  out. ChatSidebar.folderAlignment.test.tsx imports THIS constant for its
 *  arithmetic and asserts the rendered padding against it, so a change here fails a
 *  test instead of silently moving three guides. */
export const FOLDER_BODY_INSET_PX = 2

/** Padding the folder body carries while open. The LEFT term is the alignment
 *  algebra's `D`; the vertical 2px keeps focus rings off the clip edge. */
export const FOLDER_BODY_OPEN_PADDING = `2px 0 2px ${FOLDER_BODY_INSET_PX}px`

/** Stacking base for pinned folder headers. A header at depth d gets
 *  `FOLDER_ROW_STICKY_Z - d`, so a parent's header paints over its child's as the
 *  child's block scrolls out beneath it. Kept small: it only has to beat the
 *  session rows in the lane, and every menu and popover renders in a portal. */
export const FOLDER_ROW_STICKY_Z = 20

/** The list-view folder body: the connector line (`border-l`) plus the gap after
 *  it. `ml-3` 12 puts the line under the header's glyph (guide 1: P = D + M,
 *  14 = 2 + 12) and `pl-1` 4 closes guide 2 (name == content). Rows filed inside
 *  a folder keep the row's own pad (`ROW_BOX_CLS` `pl-3.5`, R = 14 = P), so
 *  there is no per-body row-pad override and depth does not enter the algebra.
 *  This is the geometry decided in docs/decisions/
 *  2026-10-09-folder-glyph-sits-on-its-connector-line.md; `relative` is the
 *  containing block for `FolderRail`'s hit strip and draws nothing. Shared by
 *  every list-view folder body. */
export const FOLDER_BODY_CLS = 'relative border-l border-border mb-1 ml-3 pl-1 rounded-bl-md'

/** The board-view folder body: `ml-3` (12) + 1px border + `pl-1` (4) lands the
 *  rows at the same x as #3905's `ml-4` (16 + 1), and the 4px pad is what keeps
 *  `FolderRail`'s hit strip (-5..3 across the border) off the rows: with no pad
 *  the strip sits over the first 3px of every row, and a click there folds the
 *  folder instead of opening the session. The list body has the same clearance.
 *  Rows inside it carry their own `ROW_BOX_CLS` pad. */
export const BOARD_FOLDER_BODY_CLS = 'relative border-l border-border ml-3 pl-1'

/** The look shared by the sidebar's in-list text controls: the per-folder dormant
 *  toggle and the "Show all older sessions" row. Both are buttons that read as
 *  plain text, so they get one size and one hover fill; at a smaller size with a
 *  fainter fill the dormant toggle read as a caption, not a control. Padding and
 *  margins stay per row: the toggle's `px-3` 12 puts its chevron 2px left of the
 *  session rows' 14px content column, the same relation as the pinned divider,
 *  in the root lane and inside every folder body alike. */
export const SIDEBAR_TEXT_CONTROL_CLS = 'text-left text-[12px] text-muted hover:text-accent hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors'
/** The dormant toggle: the shared look plus a full-width flex row for the chevron. */
export const STALE_TOGGLE_CLS = `w-full flex items-center gap-1.5 px-3 py-0.5 leading-4 ${SIDEBAR_TEXT_CONTROL_CLS}`

/** Compute a date segment label for a session timestamp. Mirrors ChatGPT/Claude.
 *  Accepts either a Unix epoch (seconds) from backend `modified` or an ISO `created` string. */
export function dateSegment(ts: number | string | undefined): string {
  if (ts == null) return i18nT('pages.chatSidebar.older')
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return i18nT('pages.chatSidebar.older')
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const daysAgo7 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7)
  const daysAgo30 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 30)
  if (d >= startOfToday) return i18nT('pages.chatSidebar.today')
  if (d >= startOfYesterday) return i18nT('pages.chatSidebar.yesterday')
  if (d >= daysAgo7) return i18nT('pages.chatSidebar.last_7_days')
  if (d >= daysAgo30) return i18nT('pages.chatSidebar.last_30_days')
  if (d.getFullYear() === now.getFullYear()) return fmtDateFields(d, { month: 'long' })
  return fmtDateFields(d, { year: 'numeric', month: 'long' })
}
