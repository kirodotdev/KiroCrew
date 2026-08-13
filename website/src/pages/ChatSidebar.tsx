import { useState, useRef, useEffect, useLayoutEffect, memo, useMemo, useCallback, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { LayoutGroup, AnimatePresence, motion } from 'framer-motion'
import { Plus, X, Pin, Monitor, Eye, EyeOff, VenetianMask, Droplet, FolderPlus, MessageSquare, MessageSquarePlus, MessagesSquare, Folder, ChevronRight, ChevronDown, ChevronUp, Clock, Pencil, BrushCleaning, Link2, Circle, MoreVertical, Tag as TagIcon, Columns3, GripVertical, Zap, Check, Copy, ListFilter, List, Loader, Loader2, Settings, RotateCcw, Bot, ExternalLink, Cpu, GitMerge, Workflow, CircleDot, Users, TriangleAlert, Goal, MessageCircleQuestionMark, ShieldCheck, Repeat } from 'lucide-react'
import GithubLogo from '../components/icons/GithubLogo'
import GitlabLogo from '../components/icons/GitlabLogo'
import JiraLogo from '../components/icons/JiraLogo'
import FolderGlyph from '../components/FolderGlyph'
import { DndContext, closestCenter, pointerWithin, KeyboardSensor, MouseSensor, TouchSensor, useSensor, useSensors, useDroppable, DragOverlay, MeasuringStrategy, type DragEndEvent, type DragStartEvent, type DragOverEvent, type CollisionDetection } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, useSortable, sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { shallowEqual } from 'react-redux'
import { useAppDispatch, useAppSelector } from '../store'
import { useConnected } from '../hooks/useConnected'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent } from '../components/ui/context-menu'
import { offlineProps } from '../utils/offline'
import { switchSlot, createSlot, deleteSlot, fetchHistory, resumeFromHistory, deleteHistorySession, clearSlotReveal } from '../store/chatSlice'
import { sseSlotTitle, setSidebarOrder } from '../store/dashboardSlice'
import { useDigitModifierHeld } from '../hooks/useKeyboardShortcuts'
import { api, SEARCH_MIN_CHARS } from '../api/client'
import { computeReorderedFolders } from '../utils/reorderFolders'
import { computeRecentRank, recencyTintShadow, clampTintCount } from '../utils/recencyTint'
import { computeActiveSubtree, folderIsHidden, folderOffersHide } from '../utils/folderVisibility'
import { groupHistoryByFolder } from '../utils/groupHistoryByFolder'
import { boardCollapseKey, boardColumnFromDroppableId, loadBoardFolderCollapse, persistBoardOverride, persistClearFolderOverrides, clearFolderOverrides } from '../utils/boardFolderCollapse'
import { slotChannelLabel, slotChannelNamespace } from '../utils/channelOrigin'
import { toolStatusLabel } from '../utils/toolStatusLabel'
import { sessionRefBlockReason, type SessionRefBlockReason } from '../utils/sessionRefs'
import { SearchInput, Input, Btn, IconButton, IconButtonGroup, Badge } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import FolderConfigModal from '../components/FolderConfigModal'
import ModelDropdownList from '../components/ModelDropdownList'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import { useSelectInstance } from '../hooks/useSelectInstance'
import { useSimplifiedToolNames } from '../hooks/useSimplifiedToolNames'
import { useLanguage } from '../i18n/LanguageProvider'
import { useSessionActions } from '../hooks/useSessionActions'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'
import { useChatPopouts } from '../hooks/useChatPopouts'
import { platformShortcut } from '../utils/platform'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { usePointerDrag } from '../hooks/usePointerDrag'
import { safeSetItem } from '../utils/safeStorage'
import { LAYOUT } from '../components/layout'
import { resolveFolderAgent, resolveFolderProjectDir } from '../utils/folderAgent'
import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import SessionActionsMenu from '../components/SessionActionsMenu'
import { ChannelBrandIcon, hasChannelBrandIcon } from '../components/ChannelBrandIcon'
import TagManagerList from '../components/TagManagerList'
import { DndDraggable, DndDroppable } from '../components/dnd'
import { collectFolderSubtreeIds } from '../utils/folderTree'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import { sanitizeLlmOutput } from '../utils/sanitize'
import type { ChatFolder, ChatTag, TagColumn, TagColumnMode, SubagentActivity, SessionLink } from '../types'
import { SESSION_LANES, inferLane } from './chat/sessionLane'
import { decideUnreadDrain } from './unreadDrain'
import {
  type RecentUnit,
  DEFAULT_RECENT_WINDOW_MS,
  RECENT_WINDOW_PRESETS,
  decomposeRecentWindow,
  formatRecentWindow,
  clampRecentAmount,
  customRecentWindowMs,
  recentTickIntervalMs,
  isWithinRecentWindow,
} from './recentWindow'
import { loadChatConfig, saveChatConfig } from './chat/ChatSettings'
import { focusSiblingSessionRow } from './chat/sessionRowNav'
import { focusComposer } from './chat/composerFocus'
import { compareBySort, comparePinnedThenSort, fmtRelativeTime, slotActivityTs } from './chat/sessionOrder'
import type { SortKey } from './chat/sessionOrder'

import { i18nT } from '../i18n/t'
import { fmtDateFields, fmtList } from '../i18n/format'

/** Max height (px) of the inline session-rename <textarea> before it scrolls.
 *  ~6 lines at the row's `ROW_TITLE_CLS` type. Shared by the auto-grow hook
 *  (grows while typing) and the open effect (sizes on every open). */
const RENAME_MAX_H = 120

/**
 * Session-row type scale, quantized to a 4px baseline grid.
 *
 * Every line box is a multiple of 4 and the inter-line gaps are zero — the
 * leading carries the breathing room — so a row is a whole number of grid
 * units (12px padding + 12 + 20 + 16 = 60) and consecutive rows stack on the
 * grid instead of drifting. The previous scale mixed three RATIOS
 * (`leading-tight` / `leading-snug`) over 11/13/12px text, which produced
 * 13.75 / 17.875 / 16.5px boxes: no line landed on the grid and the row height
 * was an arbitrary 62.125px.
 *
 * The three sizes are also spread far enough apart to READ as a hierarchy.
 * 11/13/12 sat within 2px of each other, and CJK glyphs fill their em box, so
 * the secondary line competed with the headline instead of yielding to it.
 *
 * The three boxes need NOT be equal to each other. Row-centring the status
 * marker did require the first and last to match — it is the only way
 * headline-centre can coincide with row-centre — and that constraint is gone
 * because the marker now leads the secondary line and centres on IT.
 * Which is what buys the meta line its 12px box: the tightest of the three,
 * spent on the least important line.
 */
const ROW_META_CLS = 'text-[10px] leading-[12px]'
const ROW_TITLE_CLS = 'text-[13px] leading-[20px]'
const ROW_STATUS_CLS = 'text-[11px] leading-[16px]'

/* A SECOND surface now tracks these three sizes: the Notes app's left rail
 * (`apps/md-notebook/constants.ts`, `RAIL_TYPE`) mirrors them so the two
 * sidebars read as one scale. The agreement is by copied value, not a shared
 * token — nothing goes red if these move. Change a size here and update
 * `RAIL_TYPE` in the same commit, or the rail silently diverges. */

/** The secondary line's three shapes, as whole class strings. The eight status
 *  branches that render this line each used to spell the type classes out, so a
 *  ninth state was one copy-paste away from re-introducing a size the grid does
 *  not contain — which is how the line ended up at 12px against an 11px meta
 *  line in the first place. Colour is what actually differs between them.
 *
 *  All three are FLEX rows, because all three lead with the row's status marker
 *  (the muted one carries the `unread` dot). A `w-2 h-2` dot only gets its box as
 *  a flex item — as an inline child both dimensions are dropped and it vanishes. */
const ROW_STATUS_LINE_CLS = `${ROW_STATUS_CLS} flex items-center gap-1.5 min-w-0`
const ROW_STATUS_LINE_ACCENT_CLS = `${ROW_STATUS_CLS} text-accent truncate flex items-center gap-1`
const ROW_STATUS_LINE_MUTED_CLS = `${ROW_STATUS_CLS} text-muted flex items-center gap-1.5 min-w-0`

/** Every glyph in a session row is drawn at ONE size — the status marker, the
 *  meta line's mode/channel markers, and the pin. Three sizes (9 / 10 / 12) read
 *  as accidental variation rather than as a hierarchy, since none of these
 *  glyphs outranks another. */
const ROW_ICON_PX = 10

/** Translate a slot's running-status line. The status `text` is stored as a raw
 *  English literal by the websocket layer (a plain `.ts` module the i18n codemod
 *  never scans), so it must be localized at render time. The two fixed phases
 *  (`thinking`/`streaming`) map to catalog keys; a `tool` phase or a
 *  server-supplied status carries its own dynamic text and is passed through.
 *
 *  A `tool` phase honors the user's `simplifiedToolNames` preference (purpose vs
 *  raw tool title) via toolStatusLabel, so the row agrees with the inline tool
 *  pill in the transcript rather than always showing the purpose. */
function slotStatusText(detail: { kind?: string; text?: string; toolName?: string } | undefined, simplifiedToolNames: boolean, uiLang: string): string {
  if (detail?.kind === 'streaming') return i18nT('pages.chatSidebar.streaming')
  if (detail?.kind === 'thinking' && detail.text === 'Thinking…') return i18nT('pages.chatSidebar.thinking')
  return toolStatusLabel(detail, simplifiedToolNames, uiLang) || i18nT('pages.chatSidebar.thinking')
}

/** Sortable wrapper for a folder block — enables drag-to-reorder */
/**
 * Folder reordering and session-to-folder assignment share one DndContext but
 * want different collision behavior:
 *  - Dragging a folder: restrict collisions to folder sortable containers so
 *    verticalListSortingStrategy animates cleanly and `over.id` is a folder id.
 *  - Dragging a session: prefer the innermost droppable under the pointer
 *    (folder/root drop target), falling back to closestCenter.
 */
// Exported for a call-site unit test (ChatSidebar.folderNestBandCallSite.test.tsx):
// asserts the collision uses the MEASURED header height, not
// FOLDER_HEADER_DROP_BAND — the specific regression codex flagged in review.
export const sidebarCollision: CollisionDetection = (args) => {
  const activeData = args.active?.data?.current as { type?: string; nested?: boolean; subtree?: string[] } | undefined
  const activeType = activeData?.type
  if (activeType === 'folder') {
    const subtree = new Set(activeData?.subtree ?? [])
    if (activeData?.nested) {
      // Nested subfolder drag: the gesture is re-parenting, not reordering.
      // Target the innermost folder-drop zone under the pointer (or the root
      // lane to move to top level), excluding the dragged folder's own
      // subtree so it can never be dropped into itself or a descendant.
      const dropContainers = args.droppableContainers.filter(c => {
        const d = c.data?.current as { type?: string; folderId?: string | null } | undefined
        return d?.type === 'folder-drop' && !(d.folderId && subtree.has(d.folderId))
      })
      return pointerWithin({ ...args, droppableContainers: dropContainers })
    }
    // Root folder drag: two gestures share the drag, disambiguated by where
    // the pointer sits on the target — the "thirds" pattern from VS Code /
    // Notion tree DnD. The MIDDLE band of another folder's header row
    // re-parents INTO it (folder-drop collision, ring highlight); the
    // header's top/bottom edges and everything below fall through to the
    // sortable reorder, so even a collapsed folder (whose whole block is
    // just the header) can still be reordered against at its edges.
    //
    // Band width is a DISCOVERABILITY lever: the original middle-50% (0.25–0.75)
    // of the header was easy to miss, so users concluded folder nesting did not
    // exist. Widening to the middle-60% (0.2–0.8, via FOLDER_HEADER_NEST_BAND_*)
    // makes the nest gesture — and its ring cue — easier to land, while the
    // top/bottom 20% of the header plus the entire folder BODY below it stay
    // reorder targets (closestCenter fallback), so reordering siblings is
    // preserved. The band is taken from the MEASURED header height below, not a
    // px constant, so it is correct for both the taller list header and the
    // shorter board header.
    if (args.pointerCoordinates) {
      const dropContainers = args.droppableContainers.filter(c => {
        const d = c.data?.current as { type?: string; folderId?: string | null } | undefined
        return d?.type === 'folder-drop' && !!d.folderId && !subtree.has(d.folderId)
      })
      const within = pointerWithin({ ...args, droppableContainers: dropContainers })
      const first = within[0]
      const rect = first?.data?.droppableContainer?.rect?.current
      if (rect) {
        // Anchor the nest band to the MEASURED header height, not a constant.
        // The droppable rect spans the whole folder BLOCK (header + expanded
        // body), so a fraction of rect.height would balloon the nest zone on an
        // expanded folder. The header is the block's first child; its real
        // height differs by layout — list headers (text-sm py-1.5) are taller
        // than board headers (text-[12px] py-1) — so a single px constant that
        // fit one layout mis-sized the other (the board over-nest bug). Reading
        // the header rect makes the middle-60% band correct for both. Falls back
        // to FOLDER_HEADER_DROP_BAND, clamped to the block, if the node is
        // unavailable (e.g. before first measure).
        const node = first?.data?.droppableContainer?.node?.current
        const headerEl = node?.firstElementChild as HTMLElement | null
        const headerH = headerEl?.getBoundingClientRect().height
          || Math.min(rect.height, FOLDER_HEADER_DROP_BAND)
        const offsetY = args.pointerCoordinates.y - rect.top
        if (isFolderNestBand(offsetY, headerH)) {
          return [first]
        }
      }
    }
    const folderContainers = args.droppableContainers.filter(
      c => (c.data?.current as { type?: string } | undefined)?.type === 'folder'
    )
    return closestCenter({ ...args, droppableContainers: folderContainers })
  }
  const within = pointerWithin(args)
  if (within.length) return within
  // Session drag that is inside no droppable: fall back to the nearest one, but
  // NEVER to the chat-pane zone. That zone is a pane-sized rect living outside
  // the sidebar, so by center-distance it would routinely beat the folder row
  // the user was actually aiming at and steal near-miss drops. A pointer
  // genuinely inside it still wins above, via `within`.
  const fallback = args.droppableContainers.filter(
    c => (c.data?.current as { type?: string } | undefined)?.type !== CHAT_PANE_DROP_TYPE
  )
  return closestCenter({ ...args, droppableContainers: fallback })
}

/** Droppable `type` for the chat-pane target that stages a session reference in
 *  the composer. Lives outside the sidebar's DOM (portaled into ChatPage's pane)
 *  but inside its DndContext, so React context reaches it while `useDroppable`
 *  measures its real on-screen rect. */
const CHAT_PANE_DROP_TYPE = 'chat-pane-ref'

/**
 * Full-pane drop affordance for "drag a session into the open chat".
 *
 * The HIT AREA is the whole pane — it is ~10x the composer's area and a shorter
 * travel from the session list, and a release over the transcript that silently
 * did nothing would read as a broken feature rather than a near-miss. But the
 * CUE is anchored on the composer, because that is where the chip actually
 * lands; a label floating mid-transcript taught the wrong mental model (that the
 * session drops into the conversation itself).
 *
 * Rendered only while a session drag is live, so it never sits invisibly over
 * the transcript at rest. `pointer-events-none` is safe *and* required: dnd-kit
 * resolves collisions from measured rects, not DOM hit-testing, so the zone
 * still receives the drop while the chat underneath stays fully interactive.
 *
 * When the dragged session may not be referenced the zone renders a refusal state
 * instead of an invitation, and the two refusals are NOT interchangeable:
 * incognito/temporary is a guard stated plainly, while dropping a session onto
 * its own pane is a harmless mis-aim answered with a recursive joke rather than a
 * warning. Explaining the block beats silently ignoring the drop — and the drop
 * handler refuses independently, so this is the visible half of a guard that does
 * not depend on the UI being reached.
 */
function ChatPaneDropZone({ refusal }: { refusal: SessionRefBlockReason | null }) {
  const refused = refusal !== null
  const { setNodeRef, isOver } = useDroppable({ id: 'chat-pane-ref', data: { type: CHAT_PANE_DROP_TYPE } })
  const zoneRef = useRef<HTMLDivElement | null>(null)
  /** The composer's box in zone-local coordinates (plus the zone's own height, so
   *  the pill's offset is plain arithmetic rather than a `calc()` string — a CSS
   *  template literal here is exactly the shape the i18n gate flags). */
  const [target, setTarget] = useState<
    { left: number; top: number; width: number; height: number; zoneH: number } | null
  >(null)
  const attach = useCallback((el: HTMLDivElement | null) => {
    zoneRef.current = el
    setNodeRef(el)
  }, [setNodeRef])
  // Measured ONCE at mount rather than hardcoded as an offset from the bottom:
  // the composer band's height moves with the attachment strip, the session-ref
  // strip, and the approval bar, so any constant would drift. The zone exists
  // only for the duration of one drag and the pointer is held down throughout,
  // so a single read cannot go stale.
  useLayoutEffect(() => {
    const el = zoneRef.current
    const composer = el?.parentElement?.querySelector('[data-testid="input-wrapper"]')
    if (!el || !composer) return
    const z = el.getBoundingClientRect()
    const c = composer.getBoundingClientRect()
    setTarget({
      left: c.left - z.left,
      top: c.top - z.top,
      width: c.width,
      height: c.height,
      zoneH: z.height,
    })
  }, [])
  const active = isOver && !refused
  // Two refusals, told apart deliberately. 'private' is a GUARD — the user asked
  // for something the product will not do, so it keeps the warn tone. 'self' is
  // not a guard at all: dropping a session onto its own pane is a no-op the user
  // reached by aiming badly, and dressing a harmless gesture in warning colour
  // teaches them they broke something. It gets the resting neutral tone and a
  // joke that IS the explanation — the sentence recurses the way the drop would.
  const tone = refusal === 'private'
    ? 'border-warn bg-bg-elevated/90 text-warn'
    : refusal === 'self'
      ? 'border-border bg-bg-elevated/90 text-muted'
      : active
        ? 'border-accent bg-bg-elevated/90 text-accent ring-2 ring-accent'
        : 'border-border bg-bg-elevated/90 text-muted'
  const pill = (
    <div className={`inline-flex items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-[12px] shadow-lg backdrop-blur-sm ${tone}`}>
      {refusal === 'private'
        ? <EyeOff size={14} className="shrink-0" />
        : refusal === 'self'
          ? <Repeat size={14} className="shrink-0" />
          : <MessagesSquare size={14} className="shrink-0" />}
      <span>
        {refusal === 'private'
          ? i18nT('pages.chatSidebar.private_session_cannot_be_referenced')
          : refusal === 'self'
            ? i18nT('pages.chatSidebar.session_dropped_into_itself')
            : i18nT('pages.chatSidebar.drop_to_reference_session')}
      </span>
    </div>
  )
  return (
    <div
      ref={attach}
      data-testid="chat-pane-drop-zone"
      data-refused={refusal ?? undefined}
      aria-hidden="true"
      className={`absolute inset-0 z-30 pointer-events-none transition-colors ${
        active ? 'bg-accent/[0.06]' : isOver && refusal === 'private' ? 'bg-warn/[0.06]' : 'bg-transparent'
      }`}
    >
      {target ? (
        <>
          {/* Outline the destination itself, matching the treatment the existing
              file drop puts on the composer, so both drags land the same way.
              Suppressed when refused: outlining a destination while the label
              says the drop is not allowed contradicts itself — a refusal has no
              destination. The pill still sits over the composer, because that is
              the context of what was refused. */}
          {!refused && (
            <div
              data-testid="chat-pane-drop-target"
              className={`absolute rounded-2xl border-2 border-dashed transition-colors ${
                active ? 'border-accent' : 'border-border-strong'
              }`}
              style={{ left: target.left, top: target.top, width: target.width, height: target.height }}
            />
          )}
          {/* Pill sits directly above the composer, pointing at where the chip
              will appear. 10px of air between the two. */}
          <div
            className="absolute flex justify-center"
            style={{ left: target.left, width: target.width, bottom: target.zoneH - target.top + 10 }}
          >
            {pill}
          </div>
        </>
      ) : (
        // Measurement unavailable (no composer on screen — e.g. an empty state).
        // Fall back to a centered pill rather than rendering no affordance at all.
        <div className="absolute inset-0 flex items-center justify-center">{pill}</div>
      )}
    </div>
  )
}

/** Approximate height (px) of a folder header row. For root folder drags the
 *  MIDDLE 25%–75% of this band re-parents INTO the folder; the top/bottom
 *  edges (and everything below the header) stay sortable-reorder gestures —
 *  the VS Code / Notion "thirds" tree-DnD pattern. */
const FOLDER_HEADER_DROP_BAND = 34
/** Fraction of the MEASURED header height that re-parents INTO the folder (the
 *  nest zone). The middle 60% (0.2–0.8) is a modest widening of the original
 *  middle-50% — enough to make the nest gesture reliably hittable (its ring cue
 *  discoverable) without starving reorder: the top/bottom 20% of the header stay
 *  reorder edges, and the whole folder BODY below the header is always reorder.
 *  sidebarCollision multiplies these by the measured header height (not a px
 *  constant) so the same fractions are correct for both the taller list header
 *  and the shorter board header. */
const FOLDER_HEADER_NEST_BAND_LO = 0.2
const FOLDER_HEADER_NEST_BAND_HI = 0.8

/** True when a pointer at `offsetY` px below a folder header's top falls in the
 *  NEST band (re-parent INTO the folder); false means the top/bottom edge, which
 *  falls through to sortable REORDER. `headerH` is the MEASURED header height so
 *  the same fractions work for the taller list header and the shorter board
 *  header. Extracted + exported so the reorder-vs-nest boundary is unit-tested
 *  directly (the DOM-marker tests can't reach this math). */
export function isFolderNestBand(offsetY: number, headerH: number): boolean {
  return offsetY >= headerH * FOLDER_HEADER_NEST_BAND_LO && offsetY <= headerH * FOLDER_HEADER_NEST_BAND_HI
}


/** Dashed always-reachable drop target shown in the root lane while dragging
 *  a foldered item — the explicit escape hatch out of a folder. Shared by
 *  session drags and nested-folder drags so the affordance (and wording)
 *  stays identical for both. */
function RootDropHint() {
  const { setNodeRef, isOver } = useDroppable({ id: 'root-unnest-hint', data: { type: 'folder-drop', folderId: null } })
  return (
    <div ref={setNodeRef} className={`m-1 min-h-[72px] flex items-center justify-center rounded-md border border-dashed transition-all ${isOver ? 'border-accent bg-accent/10 ring-2 ring-accent text-accent' : 'border-border text-muted'}`}>
      <span className="text-[12px]">{i18nT('pages.chatSidebar.drop_here_to_remove_from_folder')}</span>
    </div>
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
function DisclosureChevron({ open, size, className = '' }: { open: boolean; size: number; className?: string }) {
  return <ChevronRight size={size} className={`shrink-0 transition-transform duration-200 ${open ? 'rotate-90' : ''} ${className}`.trimEnd()} />
}

function SortableFolderBlock({ folder, subtree, renderFolderBlock }: { folder: ChatFolder; subtree?: readonly string[]; renderFolderBlock: (f: ChatFolder, depth: number, visited?: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode[] }) {
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id, data: { type: 'folder', subtree } })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1, position: 'relative' as const }
  // The whole folder header is the drag handle (pointer + touch): dragging the
  // row reorders the folder — no grip, consistent with session-card drag. Only
  // pointer listeners are forwarded (not attributes) so the header keeps
  // its inner collapse/action buttons valid. The MouseSensor activation
  // distance lets clicks through, and the TouchSensor's press-and-hold delay
  // lets touch swipes pan the list. setNodeRef stays on the block for sortable
  // positioning. While dragging, the body is force-collapsed so the source
  // shrinks to a single row — the drop-target gap (and the DragOverlay ghost)
  // stay compact.
  return (
    <div ref={setNodeRef} style={style} className="relative" data-folder-sortable={folder.id}>
      {renderFolderBlock(folder, 0, undefined, listeners as unknown as React.HTMLAttributes<HTMLElement>, isDragging)}
    </div>
  )
}

/** Sortable wrapper for a board/column-view folder — the board sibling of
 *  SortableFolderBlock. Each column owns its own DndContext, so the bare folder
 *  id is a unique sortable id within that column even though every column
 *  renders the same root folders. Only pointer listeners are forwarded (the
 *  folder header becomes the drag handle); setNodeRef wraps the whole block for
 *  sortable positioning — identical to the list-view pattern. Reorders route
 *  through the same global reorderFolders() path, so order stays consistent
 *  across every column and the list view. */
function SortableColumnFolder({ folder, columnId, colSlotKeys, subtree, renderColumnFolder }: {
  folder: ChatFolder
  columnId: string
  colSlotKeys: Set<string>
  subtree?: readonly string[]
  renderColumnFolder: (f: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode
}) {
  // `subtree` mirrors the list-view SortableFolderBlock: sidebarCollision reads it
  // to exclude the dragged folder's own descendants from the nest drop targets, so
  // a folder can never be dropped into itself or a child (moveFolderTo guards this
  // too, but excluding them up front keeps the highlight honest).
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id, data: { type: 'folder', subtree } })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1, position: 'relative' as const }
  // While dragging, the body is force-collapsed so the source shrinks to a
  // single row — the drop-target gap (and the DragOverlay ghost) stay compact,
  // matching the list-view drag feel.
  return (
    <div ref={setNodeRef} style={style} data-col-folder-sortable={folder.id}>
      {renderColumnFolder(folder, columnId, colSlotKeys, listeners as unknown as React.HTMLAttributes<HTMLElement>, isDragging)}
    </div>
  )
}

/** Compact drag-preview ghost for a folder, rendered inside a DragOverlay.
 *  Shared by the list-view overlay and each board-column overlay so the drag
 *  visual is identical in both layouts. */
function FolderDragGhost({ folder }: { folder?: ChatFolder }) {
  return (
    <div data-testid="folder-drag-ghost" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none flex items-center gap-2">
      <FolderGlyph color={folder?.color} size={14} />{folder?.name ?? i18nT('pages.chatSidebar.folder')}
    </div>
  )
}

/** Compact drag-preview ghost for a session row, rendered inside a DragOverlay.
 *  Shared by the folder-tree and flat-lane overlays. Falls back to the slot key
 *  when the session carries no distinct title. */
function SessionDragGhost({ slot, fallbackLabel }: { slot?: Slot; fallbackLabel: string }) {
  const label = slot?.title && slot.title !== slot.key ? slot.title : (slot?.key ?? fallbackLabel)
  return (
    <div data-testid="session-drag-ghost" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none">{label}</div>
  )
}

interface Slot {
  key: string
  title?: string
  running: boolean
  unread?: boolean
  // `pending_approval` rides on every ChatSlot payload; the sidebar reads it to
  // suppress the "your turn" dot and show the yellow "Needs approval" subtitle.
  pending_approval?: boolean
  // An unanswered question card the turn is parked on. Its own subtitle, and it
  // suppresses the "your turn" dot for the same reason an approval does.
  needs_input?: boolean
  // The transcript shows the last turn ending without a reply (trailing error
  // row or unanswered user row) — the state behind the composer's Resume
  // button. Always false while a turn runs. Read by the goal-loop subtitle so a
  // stalled loop stops pulsing as if it were working.
  interrupted?: boolean
  mode?: string
  agent?: string
  model?: string  // '' / absent = provider-default ("auto")
  // Message count from the slot payload. Already carried by every ChatSlot
  // (redux seeds it in addSlotOptimistic and SessionGridView renders it); it was
  // simply never declared on this local view of the type.
  messages?: number
  workspace?: string
  created?: string
  last_ts?: string
  // Settled activity instant: the last prompt or turn completion, NOT every
  // streamed row. What the list is ordered, segmented and labelled by — see
  // `slotActivityTs`.
  last_turn_ts?: string
  last_message?: string
  slack_linked?: boolean
  links?: SessionLink[]
  color_index?: number | null
  color_hex?: string | null
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  clean_mode?: boolean
  folder_id?: string
  pinned?: boolean
  tags?: string[]
  forked_from?: string | null
  source_links?: Array<{
    provider: 'github' | 'gitlab' | 'jira'
    number: number
    url: string
    repo?: string
    ci?: 'running' | 'passed' | 'failed' | null
    state?: 'open' | 'draft' | 'merged' | 'closed'
    // Owner-gated chips spread the whole cached chip-status entry, which also
    // carries the settled merge pair. Present only once the provider settled it.
    mergeable?: string
    mergeStateStatus?: string
    // What the link points at. OPTIONAL on the wire — absent means 'change', so
    // older payloads and existing fixtures keep rendering as PR/MR chips.
    kind?: 'change' | 'issue'
  }>
  source_links_total?: number
}

type SourceLinkState = NonNullable<NonNullable<Slot['source_links']>[number]['state']>
/** One sidebar chip's payload, as the slot serializer sends it. */
type SidebarSourceLink = NonNullable<Slot['source_links']>[number]

/** Lifecycle states after which a pull request can never merge, so its CI
 * rollup carries no actionable information and the lifecycle glyph is the only
 * meaningful signal. Named ONCE here because the vocabulary is shared by three
 * sibling conditionals; an inline literal per glyph is how `closed` came to be
 * covered by the badge but not by the CI gate.
 *
 * `closed` matters as much as `merged`: a closed pull request's check rollup can
 * stay pending FOREVER (GitHub parks fork-PR checks in PENDING /
 * ACTION_REQUIRED when the PR is closed before a maintainer approves the run),
 * so a chip gated only on `merged` spins its "checks running" spinner
 * indefinitely on a PR nobody is waiting for. Must stay in step with
 * `PullRequestPanel.tsx::SourceTabState`, which applies the same rule to the
 * source-strip tab — the chip and the tab describe one pull request and may not
 * disagree about its lifecycle. */
const TERMINAL_SOURCE_LINK_STATES: ReadonlySet<SourceLinkState> = new Set<SourceLinkState>([
  'merged',
  'closed',
])

/** Whether a chip should show its CI rollup or its merge state. Both are moot
 * once the pull request is terminal, so they share one gate. An ABSENT state
 * means the provider status has not been read yet (or the payload predates the
 * field), which is not terminal — such a chip keeps rendering CI exactly as it
 * always did. */
function showsChipCi(state: SourceLinkState | undefined): boolean {
  return state === undefined || !TERMINAL_SOURCE_LINK_STATES.has(state)
}

/** The single status glyph a change chip shows, or null for none.
 *
 * One function rather than sibling conditionals because the interesting part is
 * the PRECEDENCE, and precedence expressed as four independent `&&` guards is
 * how a chip comes to render two glyphs — or none — for a state nobody
 * enumerated.
 *
 * A conflict outranks a pending or passing rollup: green-check-on-unmergeable
 * is the reason this exists, since it reads as "ready" on a branch that cannot
 * land. It does NOT outrank a failed rollup — with both blockers live the worse
 * outcome is the one worth surfacing, and a red chip already says "do not
 * expect this to merge".
 *
 * `blocked` is deliberately not a conflict. On a repo with required reviews it
 * is the normal state of every open pull request, so treating it as a blocker
 * would decorate the whole session list and mean nothing.
 */
function chipStatusGlyph(
  link: SidebarSourceLink,
): 'failed' | 'conflict' | 'running' | 'passed' | null {
  if (!showsChipCi(link.state)) return null
  if (link.ci === 'failed') return 'failed'
  // GitHub can settle `mergeStateStatus: dirty` while `mergeable` is still
  // `unknown` (the two fields are recorded independently, each only once it is
  // real), and GitLab's `conflict` normalizes into both — so either field
  // alone is a real conflict answer.
  if (link.mergeable === 'conflicting' || link.mergeStateStatus === 'dirty') return 'conflict'
  if (link.ci === 'running') return 'running'
  if (link.ci === 'passed') return 'passed'
  return null
}

/** A session row's pull-request / issue chip strip, including the expandable
 *  "+N" overflow chip.
 *
 *  A component rather than a block inside the row's render callback because the
 *  overflow chip is interactive and therefore needs per-row state. The slots
 *  payload deliberately serializes at most three links PER KIND (state.py's
 *  `_SERIALIZED_SOURCE_LINKS_PER_SLOT`) so a broadcast carrying dozens of rows
 *  stays small, which means the links behind "+N" are not on the client at all
 *  and expanding has to fetch them. */
function SessionSourceChips({ slotKey, links, total, connected, isActive, onOpenSource, onActivateSlot }: {
  slotKey: string
  /** The budgeted links from the slots payload — what the collapsed strip shows. */
  links: SidebarSourceLink[]
  /** `source_links_total`: how many the session actually has, budget aside. */
  total?: number
  connected: boolean
  isActive: boolean
  onOpenSource?: (slotKey: string, ref: { url: string; kind: 'change' | 'issue' }) => boolean
  /** Switch to this session — the chip reveals into ITS side panel, so the
   *  session has to be the active one first. */
  onActivateSlot: () => void
}) {
  const [wantsExpanded, setWantsExpanded] = useState(false)

  /** What the slots payload currently says this row's links are.
   *
   *  Part of the query key, so it is the LINK IDENTITY that decides whether a
   *  fetched list still applies — not the count. A session can drop one pull
   *  request as it gains another, leaving `total` unchanged, and a count-keyed
   *  cache would serve that superseded set forever. */
  const signature = `${total ?? ''}|${links.map(link => link.url).join(' ')}`
  // React Query rather than useState + fetch (website/AUTOSDE.yaml `use-react-query`):
  // the same session can be rendered by more than one column, and a shared cache
  // is what stops each copy issuing its own GET for the same slot. `enabled`
  // makes the read lazy — nothing is fetched until the row is actually expanded —
  // and `retry: false` keeps a failed expand immediate, because the user's next
  // click IS the retry.
  const { data: fetchedLinks, isFetching, isError, refetch } = useQuery<SidebarSourceLink[]>({
    queryKey: ['session-source-links', slotKey, signature],
    queryFn: async () => {
      const payload = await api.chatSlotSourceLinks(slotKey)
      // Shape-check rather than trust: a malformed 200 (a proxy, an older
      // gateway) would otherwise put `undefined` where the render filters an
      // array, and an exception in render unmounts the whole sidebar.
      if (!Array.isArray(payload?.links)) throw new Error('malformed source-links response')
      return payload.links
    },
    enabled: wantsExpanded,
    retry: false,
    // Owned by the query rather than inherited from the provider: collapsing and
    // re-expanding within the window must not re-issue the GET, and that
    // guarantee should not depend on a global default someone may retune.
    staleTime: 30_000,
  })

  // Expanded only while a list for THIS payload is in hand. Because the payload
  // is in the query key, a slots push that changes the links switches to a key
  // with no data yet: the row falls back to the live budgeted strip and re-offers
  // "+N" while the new list loads, instead of freezing on a snapshot that
  // silently omits the new link.
  const isExpanded = wantsExpanded && fetchedLinks !== undefined
  const failed = wantsExpanded && isError

  // Toggling REPLACES the button that was activated, so without this the
  // keyboard user is dropped to the top of the document mid-row. Armed only by
  // the two click handlers, so a re-render from a slots push never steals focus.
  const pendingFocus = useRef<'expand' | 'collapse' | null>(null)
  const expandRef = useRef<HTMLButtonElement>(null)
  const collapseRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const want = pendingFocus.current
    if (!want) return
    pendingFocus.current = null
    ;(want === 'collapse' ? collapseRef : expandRef).current?.focus()
  }, [isExpanded])

  const shown = isExpanded && fetchedLinks ? fetchedLinks : links
  // Derived from what is actually on screen, so it lands on 0 once expanded and
  // self-corrects if a payload ever reports a total below the links it carries.
  const hidden = typeof total === 'number' ? Math.max(0, total - shown.length) : 0
  const changeLinks = shown.filter(link => (link.kind ?? 'change') !== 'issue')
  const issueLinks = shown.filter(link => (link.kind ?? 'change') === 'issue')

  const expand = () => {
    if (isFetching) return
    pendingFocus.current = 'collapse'
    // Already enabled means this is a retry after a failure (or a re-expand of a
    // key whose fetch never landed): flipping the flag again would not re-issue
    // the query, so ask for it explicitly.
    if (wantsExpanded) void refetch()
    else setWantsExpanded(true)
  }

  const overflowLabel = issueLinks.length
    ? i18nT('pages.chatSidebar.more_pull_request_or_issue_in_this_session', { count: hidden })
    : i18nT('pages.chatSidebar.more_pull_request_in_this_session', { count: hidden })
  /** Chip tooltip. A plain click now reveals in-panel, so a bare "Open <url>"
   *  would promise the browser and mislead; naming the modifier is also the only
   *  way that escape hatch is discoverable rather than found by accident. */
  const chipTitle = (link: SidebarSourceLink) => i18nT('pages.chatSidebar.open_source_link_in_side_panel', {
    url: link.url,
    modifier: platformShortcut('Cmd+click'),
  })
  /** Chip click: switch to the session the chip belongs to and reveal its pull
   *  request / issue in that session's side panel, rather than sending the user
   *  out to the provider's website.
   *
   *  The chip stays a real anchor with a real href, so four cases deliberately
   *  fall through to plain link navigation instead:
   *    - `onOpenSource` unset — the surface has no side panel to reveal into
   *      (the `/embed/sessions` list).
   *    - a modifier click — the user asked for a new tab/window explicitly, and
   *      "Copy link address" still yields the PR url.
   *    - offline — the panel loads a PR through the LOCAL provider CLI, so with
   *      the gateway down the provider's own page is the only thing that can
   *      answer at all.
   *    - `onOpenSource` returning false — the panel could not resolve this url,
   *      so the provider's page is better than a dead click.
   *  Middle-click never reaches a click handler (it fires auxclick), so it opens
   *  a background tab natively without a case here.
   *
   *  `preventDefault` comes LAST on purpose: the default action runs only after
   *  every handler returns, so suppressing navigation after the reveal is still
   *  effective — and it means the reveal decides, rather than being assumed to
   *  succeed. */
  const revealInPanel = (link: SidebarSourceLink) => (e: React.MouseEvent<HTMLAnchorElement>) => {
    // The row is a click-to-switch button; never let a chip click reach it,
    // whichever branch we take below.
    e.stopPropagation()
    if (!onOpenSource || !connected || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    if (!isActive) onActivateSlot()
    if (!onOpenSource(slotKey, { url: link.url, kind: link.kind ?? 'change' })) return
    e.preventDefault()
  }

  return (
    <div className="flex flex-wrap gap-1.5 mt-1">
      {changeLinks.map(link => (
        // `link.url` is always an `https://` URL on an allowlisted host
        // (state.py scans for the literal "https://" then validates via
        // parse_source_url), so no scheme sanitising is needed for the href.
        //
        // The row is a dnd-kit draggable as well as a button, so the anchor also
        // disables its own native HTML5 drag — that would otherwise put the URL
        // on the dataTransfer instead of the slot key in the board/flat scopes
        // that use native drag.
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
          draggable={false}
          onClick={revealInPanel(link)}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted no-underline border border-border bg-bg-elevated/60 hover:text-text hover:border-accent"
          title={chipTitle(link)}>
          {link.provider === 'github' ? <GithubLogo size={10} className="shrink-0" /> : link.provider === 'jira' ? <JiraLogo size={10} className="shrink-0" /> : <GitlabLogo size={10} className="shrink-0" />}
          {link.provider === 'github' ? `#${link.number}` : link.provider === 'jira' ? `${link.repo}-${link.number}` : `!${link.number}`}
          {link.state === 'merged' && (
            <span className="inline-flex shrink-0 text-aim" aria-label={i18nT('pages.chatSidebar.merged')} title={i18nT('pages.chatSidebar.merged')}>
              <GitMerge className="lucide-inline" aria-hidden="true" />
            </span>
          )}
          {link.state === 'closed' && <span className="capitalize text-danger">{link.state}</span>}
          {/* One status glyph, chosen by `chipStatusGlyph` — CI is moot
              once the PR is terminal (merged or closed), where the
              lifecycle glyph is the signal, and a merge conflict
              outranks a pending or passing rollup. */}
          {/* Pending CI is a STATIC amber dot (the provider's own pending
              convention), never a spinner: an animated glyph on a session
              card reads as "the agent is working on this session", which is
              a stronger claim than "this PR's checks haven't finished".
              Motion on the card stays reserved for session activity. */}
          {(() => {
            switch (chipStatusGlyph(link)) {
              case 'running':
                return <Circle className="lucide-inline shrink-0 text-warn scale-75" fill="currentColor" strokeWidth={0} aria-label={i18nT('pages.chatSidebar.checks_running')} />
              case 'passed':
                return <Check className="lucide-inline shrink-0 text-ok" aria-label={i18nT('pages.chatSidebar.checks_passed')} />
              case 'failed':
                return <X className="lucide-inline shrink-0 text-danger" aria-label={i18nT('pages.chatSidebar.checks_failed')} />
              case 'conflict':
                // The panel's own conflict-banner key, reused rather than
                // duplicated: the chip and the banner describe one pull
                // request, so they must not word it differently in any
                // locale.
                return <TriangleAlert className="lucide-inline shrink-0 text-danger" aria-label={i18nT('components.pullRequestPanel.merge_conflicts')} />
              default:
                return null
            }
          })()}
        </a>
      ))}
      {issueLinks.map(link => (
        // Issue chip: the same anchor discipline (reveal in panel, no native
        // drag) but deliberately NO ci / state / merge decoration — the
        // chip-status cache is pull-request-only in this phase, so an issue chip
        // has nothing truthful to colour and a borrowed glyph would assert state
        // we never fetched. Both providers number issues with '#'.
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
          data-testid={`session-issue-chip-${link.number}`}
          draggable={false}
          onClick={revealInPanel(link)}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted no-underline border border-border bg-bg-elevated/60 hover:text-text hover:border-accent"
          title={chipTitle(link)}>
          {link.provider === 'github' ? <GithubLogo size={10} className="shrink-0" /> : link.provider === 'jira' ? <JiraLogo size={10} className="shrink-0" /> : <GitlabLogo size={10} className="shrink-0" />}
          {link.provider !== 'jira' && <CircleDot className="lucide-inline shrink-0" aria-hidden="true" />}
          {link.provider === 'jira' ? `${link.repo}-${link.number}` : `#${link.number}`}
        </a>
      ))}
      {hidden > 0 && (
        // Gated on `hidden`, NOT on the expand intent: a row whose payload moved
        // under an open expansion renders the live budgeted strip again, and
        // must re-offer the overflow rather than hide it behind a stale state.
        //
        // `onMouseDown` stops the row's drag from claiming the press, matching
        // the row's other in-place controls; without it a click on the chip can
        // be swallowed as a drag activation. Deliberately NOT `disabled` while
        // loading — disabling the focused button blurs it to <body>, and the
        // `if (loading) return` guard in `expand` already prevents a double
        // fetch.
        <button type="button"
          ref={expandRef}
          data-testid="session-source-overflow"
          draggable={false}
          aria-expanded={false}
          onMouseDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); expand() }}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60 cursor-pointer hover:text-text hover:border-accent"
          title={failed ? i18nT('pages.chatSidebar.source_links_expand_failed') : overflowLabel}
          // An aria-label OUTRANKS the title in the accessible-name computation,
          // so the failure has to be named here too or a screen reader still
          // announces "2 more pull requests…" on a button that just failed.
          aria-label={failed ? i18nT('pages.chatSidebar.source_links_expand_failed') : overflowLabel}>
          {/* This spinner is exempt from the "no motion on a session card" rule
              that governs the CI glyph above: it is transient feedback for the
              user's OWN click on this button, not an ambient status claim about
              the session. It exists only while their expand is in flight. */}
          {isFetching
            ? <Loader2 className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
            : failed && <RotateCcw className="lucide-inline shrink-0 text-warn" aria-hidden="true" />}
          +{hidden}
        </button>
      )}
      {isExpanded && (
        <button type="button"
          ref={collapseRef}
          data-testid="session-source-collapse"
          draggable={false}
          aria-expanded={true}
          onMouseDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); pendingFocus.current = 'expand'; setWantsExpanded(false) }}
          className="inline-flex items-center px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60 cursor-pointer hover:text-text hover:border-accent"
          title={i18nT('pages.chatSidebar.collapse_source_links')}
          aria-label={i18nT('pages.chatSidebar.collapse_source_links')}>
          <ChevronUp className="lucide-inline shrink-0" aria-hidden="true" />
        </button>
      )}
    </div>
  )
}

interface HistoryItem {
  key: string
  title?: string
  created?: string
  modified?: number  // unix epoch seconds; backend's mtime — used for segmenting + display
  agent?: string  // persisted in JSONL metadata (set on session create + agent switch)
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  clean_mode?: boolean
  folder_id?: string  // folder the session was filed in; used to group search results
}

interface AgentInfo {
  name: string
  source: string
}

type SessionFilterKey = 'unread' | 'running' | 'pinned' | 'recent'

// Recency window for the "Recent" filter: surfaces sessions whose last activity
// is within the selected window (default one hour), keyed off the same
// last-activity timestamp the date sort uses. The window is user-selectable
// (presets + custom) and persisted under RECENT_WINDOW_LS_KEY. The pure window
// math lives in ./recentWindow so it can be unit-tested without a render.
const RECENT_WINDOW_LS_KEY = 'mc-session-recent-window-ms'

/** Read the persisted Recent window (ms), falling back to the default. Runs in
 *  a useState initializer during render, so a throwing localStorage (private
 *  mode / disabled storage) must not crash the component — fall back instead. */
function readStoredRecentWindow(): number {
  try {
    const saved = Number(localStorage.getItem(RECENT_WINDOW_LS_KEY))
    return Number.isFinite(saved) && saved > 0 ? saved : DEFAULT_RECENT_WINDOW_MS
  } catch {
    return DEFAULT_RECENT_WINDOW_MS
  }
}

/** Folders excluded from the flat lane (see `filterHiddenFolders`). Stored as a JSON
 *  array of folder ids under this key. */
const HIDDEN_FOLDERS_LS_KEY = 'mc-flat-hidden-folders'

/** Whether the filter menu's Folders section is rolled up to its heading. */
const FOLDERS_SHELVED_LS_KEY = 'mc-filter-folders-shelved'

/** Read the persisted hidden-folder ids. Runs in a useState initializer during
 *  render, so a throwing localStorage (private mode / disabled storage) or a
 *  hand-corrupted value must fall back to "nothing hidden", never crash. */
function readStoredHiddenFolders(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_FOLDERS_LS_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

/** Tag ids the list is filtered DOWN TO, as a JSON array under this key.
 *
 *  Inclusive, unlike the folder filter above, which stores the ids it HIDES.
 *  The asymmetry is deliberate and follows what a new item should do by default:
 *  a newly created folder must stay visible, whereas a newly created tag must
 *  not silently start narrowing the list. So empty here means "no tag filter",
 *  and selecting Blocked means "show only Blocked". */
const TAG_FILTER_LS_KEY = 'mc-session-tag-filter'

/** Read the persisted tag-filter ids. Runs in a useState initializer during
 *  render, so a throwing localStorage (private mode / disabled storage) or a
 *  hand-corrupted value must fall back to "no filter", never crash. */
function readStoredTagFilter(): Set<string> {
  try {
    const raw = localStorage.getItem(TAG_FILTER_LS_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

interface SessionFilterDef {
  key: SessionFilterKey
  storageKey: string
  color: string
  icon: (active: boolean) => React.ReactNode
}

/**
 * Catalog keys for the filter rows, chips and tooltips.
 *
 * Keys, not copy: these tables are module-level, so an `i18nT()` call here would
 * resolve once at boot and never follow a language switch — the lookup happens
 * where each label renders. Shaped as flat `Record`s of full literal keys and
 * indexed inline at the `i18nT()` call, because that is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically; a key it cannot resolve
 * is a key it cannot verify exists.
 */
export const FILTER_LABEL_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread',
  running: 'pages.chatSidebar.filter_running',
  pinned: 'pages.chatSidebar.filter_pinned',
  recent: 'pages.chatSidebar.filter_recent',
}
export const FILTER_DESCRIPTION_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread_description',
  running: 'pages.chatSidebar.filter_running_description',
  pinned: 'pages.chatSidebar.filter_pinned_description',
  recent: 'pages.chatSidebar.filter_recent_description',
}

const SESSION_FILTERS: SessionFilterDef[] = [
  {
    key: 'unread', storageKey: 'mc-session-unread-only',
    color: 'var(--accent)',
    icon: (active) => <Circle size={12} className={active ? 'text-accent' : 'text-muted'} {...(active ? { strokeWidth: 0, fill: 'var(--accent)' } : {})} />,
  },
  {
    key: 'running', storageKey: 'mc-session-running-only',
    color: 'var(--warn)',
    icon: (active) => <Zap size={12} className={active ? 'text-[var(--warn)]' : 'text-muted'} {...(active ? { fill: 'var(--warn)', stroke: 'none' } : {})} />,
  },
  {
    key: 'pinned', storageKey: 'mc-session-pinned-only',
    color: 'var(--accent)',
    icon: (active) => <Pin size={12} className={active ? 'text-accent' : 'text-muted'} {...(active ? { fill: 'var(--accent)', stroke: 'none' } : {})} />,
  },
  {
    key: 'recent', storageKey: 'mc-session-recent-only',
    color: 'var(--ok)',
    icon: (active) => <Clock size={12} className={active ? 'text-[var(--ok)]' : 'text-muted'} />,
  },
]

/**
 * Debounced backend session-content search.  Returns `null` until the first
 * response arrives (or whenever the query drops below `SEARCH_MIN_CHARS`),
 * and keeps the previous result visible while a new query is in flight so
 * the list doesn't blank out between keystrokes.
 *
 * `revalidateSignal` re-runs the search for the SAME query whenever it changes.
 * Callers feed a digest of the session set + titles, so a rename — which mutates
 * a title but not the query — refreshes the backend result set. Keep the digest
 * scoped to key/title and NOT status, or idle status ticks spam `sessionsSearch`.
 */
function useDebouncedSessionSearch<T>(
  query: string,
  transform: (sessions: { key: string; title?: string; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary'; clean_mode?: boolean; folder_id?: string; instance_id?: string; instance_name?: string }[]) => T,
  revalidateSignal?: string,
  federated = false,
): T | null {
  const [result, setResult] = useState<T | null>(null)
  const token = useRef(0)
  const queryRef = useRef(query)
  queryRef.current = query
  const debounceActive = useRef(false)
  // Read via ref so a connect/disconnect mid-debounce doesn't re-fire the
  // keystroke effect; the NEXT search simply takes the new route.
  const federatedRef = useRef(federated)
  federatedRef.current = federated

  // One fetch for both effects below: the federated endpoint merges the local
  // gateway with every connected remote instance (rank-interleaved, remote rows
  // tagged instance_id/_name); any failure — including the 403 when the
  // instances feature is off — falls back to the plain local search, which is
  // always the floor. Unreachable peers are logged, not surfaced: only
  // CONNECTED peers are fanned out, so this is a rare transient, and the local
  // results still render.
  const fetchSessions = async (q: string) => {
    if (!federatedRef.current) return api.sessionsSearch(q)
    try {
      const d = await api.instancesSearchSessions(q)
      if (Array.isArray(d?.unreachable) && d.unreachable.length) {
        console.warn('[sidebar] federated session search: unreachable instances', d.unreachable)
      }
      return d
    } catch {
      return api.sessionsSearch(q)
    }
  }

  // Debounced: fires 250ms after the last query keystroke.
  useEffect(() => {
    const q = query.trim()
    const myToken = ++token.current
    if (q.length < SEARCH_MIN_CHARS) { setResult(null); debounceActive.current = false; return }
    debounceActive.current = true
    let cancelled = false
    const t = setTimeout(async () => {
      try {
        const d = await fetchSessions(q)
        if (cancelled || myToken !== token.current) return
        setResult(transform(d.sessions || []))
      } catch { /* keep previous result on error */ }
      // Cleared AFTER the await: clearing first leaves a window where the debounce
      // has "finished" but the fetch is outstanding, so the effect below duplicates it.
      finally { debounceActive.current = false }
    }, 250)
    return () => { cancelled = true; clearTimeout(t); debounceActive.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  // Trailing-throttled re-run on signal change. Compares values rather than
  // flipping a flag so it survives StrictMode's double-mount.
  const prevSignal = useRef(revalidateSignal)
  useEffect(() => {
    if (prevSignal.current === revalidateSignal) return
    prevSignal.current = revalidateSignal
    let cancelled = false
    const t = setTimeout(async () => {
      // Preconditions are re-read here, not at effect time: only a signal change or
      // unmount clears this timer, so a keystroke would otherwise scan a stale query.
      const q = queryRef.current.trim()
      if (q.length < SEARCH_MIN_CHARS) return
      // A pending debounce or in-flight fetch serves this same query already.
      if (debounceActive.current) return
      const myToken = ++token.current
      try {
        const d = await fetchSessions(q)
        if (cancelled || myToken !== token.current) return
        setResult(transform(d.sessions || []))
      } catch { /* keep previous result on error */ }
    }, 100)
    return () => { cancelled = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revalidateSignal])

  return result
}

/** Compute a date segment label for a session timestamp. Mirrors ChatGPT/Claude.
 *  Accepts either a Unix epoch (seconds) from backend `modified` or an ISO `created` string. */
function dateSegment(ts: number | string | undefined): string {
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

function FolderBody({ open, children }: { open: boolean; children: React.ReactNode }) {
  return (
    <div
      aria-hidden={!open}
      // @ts-expect-error inert is a valid HTML attribute but TS types may lag
      inert={!open ? '' : undefined}
      style={{
        display: 'grid',
        gridTemplateRows: open ? '1fr' : '0fr',
        transition: 'grid-template-rows 0.15s ease-out',
      }}
    >
      <div style={{ overflow: 'hidden', visibility: open ? 'visible' : 'hidden', padding: open ? `2px 0 2px ${FOLDER_BODY_INSET_PX}px` : 0 }}>{children}</div>
    </div>
  )
}

interface ChatSidebarProps {
  slots: Slot[]
  activeSlot: string | null
  unreadSlots: string[]
  history: HistoryItem[]
  historyHasMore: boolean
  defaultAgent: string
  installedAgents: AgentInfo[]
  mode?: string
  onWidthChange?: (w: number) => void
  onDragChange?: (dragging: boolean) => void
  /** Optional callback fired when the user explicitly clicks a slot.
   *  When provided, this fires AFTER the switchSlot dispatch so consumers
   *  can react to user-driven selection (e.g. to navigate the URL). */
  onSelectSlot?: (key: string) => void
  /** Reveal a session's pull request / issue in the side panel instead of
   *  leaving for the provider's website.
   *
   *  Fires AFTER the row's own switchSlot dispatch, so the consumer can address
   *  the panel of the session the chip belongs to. Returns whether the panel took
   *  the link: FALSE (or an omitted callback) falls back to plain link
   *  navigation, which is the correct behaviour both on a surface with no side
   *  panel (the `/embed/sessions` list) and for a url the panel cannot resolve. */
  onOpenSource?: (slotKey: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  /** When true, ChatPage floats a hide-sidebar button over this header's
   *  top-left (open state), so the header reserves left space for it.
   *  Omitted in embed/sessions mode where the sidebar is the whole view. */
  collapsible?: boolean
  /** Element to portal the "drag a session into the chat" drop zone into —
   *  ChatPage's chat-pane wrapper. The zone renders inside this component's
   *  DndContext (so dnd-kit sees it) but measures against the pane's rect, which
   *  is what makes the whole pane a valid target rather than just the composer.
   *  Omit to disable the gesture (embed/sessions mode has no chat pane). */
  chatDropTarget?: HTMLElement | null
  /** Called when a session is dropped on the chat pane. Receives a snapshot,
   *  not a live slot, because the composer stages it until send. Never fired for
   *  incognito/temporary sessions or for the already-active session. */
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
}

/** Sort options, in menu order. The label lives in `SORT_LABEL_KEY`. */
const SORT_OPTIONS: { value: SortKey }[] = [
  { value: 'date-desc' },
  { value: 'date-asc' },
  { value: 'created-desc' },
  { value: 'created-asc' },
  { value: 'name-asc' },
  { value: 'name-desc' },
]
/** Catalog key per sort option — same resolvable shape as `FILTER_LABEL_KEY`. */
export const SORT_LABEL_KEY: Record<SortKey, string> = {
  'date-desc': 'pages.chatSidebar.sort_newest',
  'date-asc': 'pages.chatSidebar.sort_oldest',
  'created-desc': 'pages.chatSidebar.sort_created_newest',
  'created-asc': 'pages.chatSidebar.sort_created_oldest',
  'name-asc': 'pages.chatSidebar.sort_name_asc',
  'name-desc': 'pages.chatSidebar.sort_name_desc',
}
const SORT_LS_KEY = 'mc-session-sort'
/** Flat view ("explode chats out of folders") persistence key. */
const FLAT_VIEW_LS_KEY = 'mc-sidebar-flat-view'

export const SIDEBAR_MIN = 180
export const SIDEBAR_MAX = 1400
const SIDEBAR_LS_KEY = 'mc-sidebar-width'
/** The width the user had before a board auto-widen, so switching back to list
 *  view restores it instead of stranding the automatic value. */
const SIDEBAR_PRE_BOARD_LS_KEY = 'mc-sidebar-width-pre-board'

/** Board column geometry, mirrored from the column strip's own classes:
 *  `min-w-[220px]` per column, `gap-2` between them, `p-2` around the strip. */
const BOARD_COL_MIN_W = 220
const BOARD_COL_GAP = 8
const BOARD_STRIP_PAD = 16
/** Leave this much for the chat pane when widening the sidebar for a board, so
 *  a wide board never squeezes the conversation out of the window. */
const BOARD_CHAT_RESERVE = 520

/** How wide the sidebar must be for `count` board columns to fit without
 *  horizontal scrolling — clamped to the sidebar's own ceiling and to what the
 *  viewport can spare once the nav rail and a usable chat pane are subtracted.
 *  Returns the CURRENT width when nothing wider is available, so the caller can
 *  only ever widen. On a narrow window the strip keeps a little horizontal
 *  scroll rather than burying the conversation: four 220px lanes and a readable
 *  chat pane genuinely do not both fit below roughly 1700px.
 */
export function boardSidebarWidth(count: number, current: number, viewport: number): number {
  if (count <= 0) return current
  const wanted = count * BOARD_COL_MIN_W + (count - 1) * BOARD_COL_GAP + BOARD_STRIP_PAD
  const spare = viewport - LAYOUT.NAV_WIDTH - BOARD_CHAT_RESERVE
  const ceiling = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, spare))
  return Math.max(current, Math.min(wanted, ceiling))
}
/** Reveal-in-sidebar retry budget: ancestor expansion and filter resets land
 *  through mutations and re-renders, so the target row can enter the DOM
 *  several frames after the request is consumed. 20 × 100 ms ≈ 2 s, then the
 *  reveal gives up (the row genuinely isn't renderable, e.g. board lane with
 *  no matching column). */
const REVEAL_RETRY_MS = 100
const REVEAL_MAX_ATTEMPTS = 20
/** How long the reveal confirmation outline holds before fading out. */
const REVEAL_FLASH_HOLD_MS = 1600
/** Must cover the CSS fade on .session-reveal-flash-fade in index.css (.4s):
 *  the classes are removed at HOLD + FADE + slack, so shortening this below
 *  the CSS duration snaps the outline off mid-fade. */
const REVEAL_FLASH_FADE_MS = 500
/** One filter dimension that can hide a reveal target: whether it hides THIS
 *  row, and how to drop it. `clear` receives the row because the folder filter
 *  un-hides that row's own ancestor chain rather than clearing globally. */
interface RevealBlockingFilter {
  hides: (slot: Slot) => boolean
  clear: (slot: Slot) => void
}
/** One sidebar filter dimension, declared exactly once (in the component's
 *  `filterDimensions` memo) and consumed by the three sites that must agree on
 *  which filters exist: `filteredSlots` (which rows render at all),
 *  `listNarrowed` (is anything filtering right now), and
 *  `revealBlockingFilters` (does THIS row fail an active filter). Every field
 *  is required, so adding a dimension forces a decision for each consumer —
 *  `null` records "deliberately not consulted here", never an omission. */
interface FilterDimension {
  /** Row predicate applied by `filteredSlots`. `null` = this dimension does
   *  not filter the flat slot list (the folder filter drops whole folder
   *  blocks/lanes at the render sites instead of filtering rows). */
  filtersRow: ((slot: Slot) => boolean) | null
  /** Is this dimension narrowing the list right now? Consulted by
   *  `listNarrowed`. `null` = deliberately excluded from that question (the
   *  folder filter: counting it would strand every folder as an empty
   *  "New chat in <name>" shell while one is hidden). */
  narrows: (() => boolean) | null
  /** Does this dimension hide THIS row from a reveal? `excluded` reports list
   *  membership, for dimensions (search, status) that rank against backend
   *  state a single row cannot answer for alone. Non-nullable on purpose,
   *  together with `clear`: every dimension can hide a reveal target today.
   *  If one ever genuinely cannot, make the PAIR nullable in one move —
   *  never stub `hides: () => false` beside a real `clear` (or a real
   *  `hides` beside a no-op `clear`, which is silent reveal breakage). */
  hides: (slot: Slot, excluded: (slot: Slot) => boolean) => boolean
  /** Drop this dimension so the reveal target renders. Receives the row
   *  because the folder filter un-hides that row's own ancestor chain rather
   *  than clearing globally. */
  clear: (slot: Slot) => void
}

function ChatSidebar({
  slots, activeSlot, unreadSlots, history, historyHasMore,
  defaultAgent, installedAgents, mode, onWidthChange, onDragChange, onSelectSlot, onOpenSource, collapsible,
  chatDropTarget, onDropSessionRef,
}: ChatSidebarProps) {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const ime = useImeGuard()
  const isMobile = useIsMobile()

  // Sidebar width (self-managed, reported to parent)
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = localStorage.getItem(SIDEBAR_LS_KEY)
    const n = saved ? parseInt(saved, 10) : NaN
    return !isNaN(n) && n >= SIDEBAR_MIN && n <= SIDEBAR_MAX ? n : 260
  })

  // Sidebar-only state
  const [seedError, setSeedError] = useState('')
  const [slotFilter, setSlotFilter] = useState('')
  const [historyFilter, setHistoryFilter] = useState('')
  // Digest of session keys + titles (NOT status), fed to both searches as their
  // revalidate signal. Sorted+joined so reordering `slots` alone cannot refetch.
  const slotTitleDigest = useMemo(
    () => slots.map(s => s.key + '\u0000' + (s.title || '')).sort().join('\u0001'),
    [slots],
  )
  // The Older Sessions pane renders `history`, so this slots-derived signal is a
  // proxy: it moves for every rename reachable today, all of which start on a live row.
  // Federated when any remote instance holds a live connection: the endpoint
  // then also covers every connected instance's sessions (rows tagged with
  // instance_id/_name render a badge and activate that instance's pane).
  // Guarded read: ChatSidebar is rendered by dozens of test harnesses whose
  // partial stores omit the instances slice entirely (unlike the instances-own
  // components, which only ever mount with it).
  const hasWarmInstances = useAppSelector(s => Object.keys(s.instances?.warm ?? {}).length > 0)
  const historySearchResults = useDebouncedSessionSearch(
    historyFilter, s => s, slotTitleDigest, hasWarmInstances,
  )
  // Shared ['instances'] cache + shared select-and-maybe-reconnect semantics for
  // activating a remote row; enabled only while a warm connection exists so a
  // peerless install never issues the query.
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: hasWarmInstances,
  })
  const instancesData = instancesQuery.data?.instances
  const instancesList = useMemo(() => instancesData ?? [], [instancesData])
  const { selectInstance } = useSelectInstance(instancesList)
  // Which folder groups are collapsed in the grouped search-results view.
  // Ephemeral: reset on every query change so a fresh search shows all groups.
  const [collapsedHistoryGroups, setCollapsedHistoryGroups] = useState<Set<string>>(() => new Set())
  useEffect(() => { setCollapsedHistoryGroups(new Set()) }, [historyFilter])
  // Backend relevance rank per slot key (0 = best). A Map instead of a Set so
  // `filteredSlots` can ORDER matches by the backend's ranking (title matches
  // carry a strong field boost server-side) rather than re-sorting them by
  // date, which buries a title match below every fresher session that merely
  // mentions the query in its body. First-wins on canonical-key collisions so
  // a duplicate file cannot demote the better-ranked entry.
  const slotSearchRanks = useDebouncedSessionSearch(
    slotFilter,
    sessions => {
      const ranks = new Map<string, number>()
      sessions.forEach((s, i) => {
        const key = s.key.replace(/^dashboard_/, '')
        if (!ranks.has(key)) ranks.set(key, i)
      })
      return ranks
    },
    slotTitleDigest,
  )
  const [renamingSlot, setRenamingSlot] = useState<string | null>(null)
  // In board view a multi-tag chat renders once per matching column, so
  // `renamingSlot === s.key` alone is true in every copy at once — the rename
  // input would mount in all columns and the shared ref would bind to the last.
  // renameScope pins the edit to the clicked render instance (the row's `scope`:
  // 'list' or the column id) so exactly one input mounts. Same idea as the
  // Framer layoutId `scope` note below.
  const [renameScope, setRenameScope] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const cancelRenameRef = useRef(false)
  const renameInputRef = useRef<HTMLTextAreaElement | null>(null)
  // The rename field is a wrapping, auto-growing <textarea> (not a single-line
  // <input>) so a long session title is fully visible while editing instead of
  // being clipped at the right edge — you can see and edit words that a
  // single-line box would scroll out of view. Enter still commits (bindEnter
  // preventDefaults it, so no newline is inserted). Caps at ~6 lines, then
  // scrolls. Only one row renames at a time (renamingSlot), so the single
  // shared ref always points at the one mounted textarea.
  useAutoGrowTextarea(renameInputRef, renameValue, RENAME_MAX_H)
  // Set by any menu's Rename item (session rows + folder headers) so the closing
  // menu's onCloseAutoFocus knows to skip Radix's trigger-focus-restore for this
  // one close (see the menu Content handlers below). One-shot: read and cleared
  // on the next close.
  const suppressMenuRestoreRef = useRef(false)
  // Input modality tracker for menu-close focus handling: true while the most
  // recent interaction was a keyboard press. Capture-phase listeners so Radix's
  // own handlers can't reorder around us.
  const lastInputKeyboardRef = useRef(false)
  useEffect(() => {
    const onPointer = () => { lastInputKeyboardRef.current = false }
    const onKey = () => { lastInputKeyboardRef.current = true }
    document.addEventListener('pointerdown', onPointer, true)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('pointerdown', onPointer, true)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [])
  // Folder create / settings modal target. One modal instance is rendered at the
  // sidebar root, so — unlike the inline inputs it replaced — it needs no column
  // scope: a folder rendered in several board columns can only have one modal.
  // `parentId` is the fixed destination for 'create' ('' = top level).
  const [folderModal, setFolderModal] = useState<
    { mode: 'create'; parentId: string } | { mode: 'edit'; folderId: string } | null
  >(null)  // The rename menus are Radix (ContextMenu/DropdownMenu). On close, Radix's
  // FocusScope restores focus to its trigger (the card) AFTER the input mounts.
  // That restore blurs the freshly-mounted input, firing its onBlur, which
  // cancels the edit before you can type — so the box flickers open and reverts.
  // The trigger-restore is suppressed on the rename path via onCloseAutoFocus
  // (below); this effect then focuses + selects the input on the next frame so
  // the caret lands ready to overtype (same rAF pattern as the new-chat textarea).
  // Keyed on both the slot AND its scope: a same-slot, scope-only change (retarget
  // the rename to a different column before the first column's blur-commit fires)
  // must re-run so focus lands in the newly-mounted column's input, not stay on
  // the old one. Re-running when only the scope changes is harmless (idempotent
  // focus+select). When the slot clears (commit/cancel/escape/blur), also clear
  // renameScope so no stale column identity lingers.
  useEffect(() => {
    if (!renamingSlot) { setRenameScope(null); return }
    const raf = requestAnimationFrame(() => {
      const el = renameInputRef.current
      if (el) {
        el.focus({ preventScroll: true }); el.select()
        // Size the box on OPEN too, not only when renameValue changes: after a
        // save, reopening the same slot sets renameValue to the identical title,
        // so useAutoGrowTextarea's value-keyed effect never fires and the freshly
        // mounted textarea would otherwise sit at its 1-line resting height and
        // clip a long name. Mirror the hook's measure here so every open shows
        // the full name.
        el.style.height = 'auto'
        el.style.height = `${Math.min(el.scrollHeight, RENAME_MAX_H)}px`
        el.style.overflowY = el.scrollHeight > RENAME_MAX_H ? 'auto' : 'hidden'
      }
    })
    return () => cancelAnimationFrame(raf)
  }, [renamingSlot, renameScope])
  // Folder rename ref; the focus effect lives after the editingId useState
  // declarations below (it can't be referenced here — TDZ). See that effect for
  // why the rAF re-grab is needed.
  const folderEditInputRef = useRef<HTMLInputElement | null>(null)
  // Shared onCloseAutoFocus for every rename-hosting menu (session row context +
  // ⋯ dropdowns, and both folder-header ⋯ dropdowns). When Rename was the chosen
  // item it armed suppressMenuRestoreRef, so we preventDefault to stop Radix from
  // yanking focus back to the trigger — that restore would otherwise blur the
  // just-mounted rename input and cancel the edit. Every other item keeps the
  // default focus-restore intact.
  const onMenuCloseAutoFocus = useCallback((e: Event) => {
    if (suppressMenuRestoreRef.current) { suppressMenuRestoreRef.current = false; e.preventDefault(); return }
    // Pointer dismissals (outside click / mouse item pick) skip Radix's
    // focus-restore-to-trigger: the trigger lives inside a focus-within-revealed
    // hover group (folder headers AND session rows), so restoring focus pins
    // the action strip visible after the pointer has left the row. Keyboard
    // closes (Esc / Enter on an item) keep the restore — focus returning to
    // the trigger is exactly right for keyboard users (a11y).
    if (!lastInputKeyboardRef.current) e.preventDefault()
  }, [])
  const [sortKey, setSortKey] = useState<SortKey>(() => {
    const saved = localStorage.getItem(SORT_LS_KEY)
    return SORT_OPTIONS.some(o => o.value === saved) ? saved as SortKey : 'date-desc'
  })
  // Flat view: temporarily explode every chat out of its folder into one
  // recency-sorted list, for working temporally across many folders ("what's
  // the latest?"). Pure view projection — folder membership is untouched, and
  // toggling back restores the folder tree exactly as it was.
  const [flatView, setFlatView] = useState(() => localStorage.getItem(FLAT_VIEW_LS_KEY) === '1')
  const toggleFlatView = useCallback(() => {
    setFlatView(v => { const next = !v; safeSetItem(FLAT_VIEW_LS_KEY, next ? '1' : '0'); return next })
  }, [])
  const [activeFilters, setActiveFilters] = useState<Set<SessionFilterKey>>(() => {
    const initialFilters = new Set<SessionFilterKey>()
    for (const filterDef of SESSION_FILTERS) { if (localStorage.getItem(filterDef.storageKey) === '1') initialFilters.add(filterDef.key) }
    return initialFilters
  })
  // Which folders are excluded from the flat lane, chosen from the filter
  // menu's folder checkboxes. We persist the HIDDEN ids (not the visible ones)
  // so a folder created later defaults to visible instead of silently
  // vanishing. Purely a view preference — folder membership and the folder
  // tree's own collapse state are untouched.
  const [filterHiddenFolders, setFilterHiddenFolders] = useState<Set<string>>(() => readStoredHiddenFolders())
  const toggleFolderFilter = useCallback((id: string) => {
    setFilterHiddenFolders(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      safeSetItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])
  const showAllFolders = useCallback(() => {
    setFilterHiddenFolders(new Set())
    safeSetItem(HIDDEN_FOLDERS_LS_KEY, '[]')
  }, [])
  /** Tag ids the list is narrowed to. Selecting several is a UNION ("Blocked or
   *  Waiting"), matching how a board column with several tags already behaves, so
   *  the two surfaces cannot disagree about what a multi-tag selection means. */
  const [filterTagIds, setFilterTagIds] = useState<Set<string>>(() => readStoredTagFilter())
  const toggleTagFilter = useCallback((id: string) => {
    setFilterTagIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      safeSetItem(TAG_FILTER_LS_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])
  const clearTagFilter = useCallback(() => {
    setFilterTagIds(new Set())
    safeSetItem(TAG_FILTER_LS_KEY, '[]')
  }, [])
  // Shelved = the Folders section is rolled up to its heading, so a long folder
  // list stops crowding the Filter and Sort rows. Purely cosmetic: shelving
  // changes nothing about which folders are hidden, and the heading keeps
  // showing the hidden count so the state stays visible while rolled up.
  const [foldersShelved, setFoldersShelved] = useState(() => {
    try { return localStorage.getItem(FOLDERS_SHELVED_LS_KEY) === '1' } catch { return false }
  })
  const toggleFoldersShelved = useCallback(() => {
    setFoldersShelved(v => { const next = !v; safeSetItem(FOLDERS_SHELVED_LS_KEY, next ? '1' : '0'); return next })
  }, [])
  const toggleFilter = useCallback((key: SessionFilterKey) => {
    setActiveFilters(prev => {
      const next = new Set(prev)
      const filterDef = SESSION_FILTERS.find(sf => sf.key === key)!
      if (next.has(key)) { next.delete(key); safeSetItem(filterDef.storageKey, '0') }
      else { next.add(key); safeSetItem(filterDef.storageKey, '1') }
      return next
    })
  }, [])
  const disableFilter = useCallback((key: SessionFilterKey) => {
    setActiveFilters(prev => {
      if (!prev.has(key)) return prev
      const next = new Set(prev)
      next.delete(key)
      const filterDef = SESSION_FILTERS.find(sf => sf.key === key)!
      safeSetItem(filterDef.storageKey, '0')
      return next
    })
  }, [])
  // Signal from the SSE/data-fetch layer indicating the initial slot list
  // has arrived. Used by the auto-drain effect to distinguish "data not yet
  // loaded" from "data loaded and genuinely empty".
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  // shallowEqual: this is a whole-map subscription read only for each row's
  // `.text`, so re-render when some slot's detail object actually changed —
  // not merely because a reducer produced a new map wrapper. Without it, any
  // write to one slot's detail re-renders the entire sidebar.
  const slotStatusDetail = useAppSelector(s => s.chat.slotStatusDetail, shallowEqual)
  // Purpose-vs-raw-tool-title preference, shared with the inline tool pills.
  const simplifiedToolNames = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  // Presence in this map means "this session is in an active goal loop".
  const goalLoops = useAppSelector(s => s.chat.goalLoops)
  // Live subagent activity per slot, for the sidebar row's "N agents running"
  // subtitle. Mirrors SubagentProgressBar's source of truth: chatSlice.subagents
  // for the store's active slot, slotActivity[slot].subagents for background
  // slots (both populated by the globally-subscribed subagent_spawn/tool/done WS
  // events). We deliberately do NOT use dashboardSlice.subagentRunning — that
  // count is only broadcast on the subagent_status "done" event
  // (gateway._broadcast_subagent_status), never on spawn, so it under-reports
  // while agents are still running.
  const storeActiveSlot = useAppSelector(s => s.chat.activeSlot)
  const activeSlotSubagents = useAppSelector(s => s.chat.subagents)
  const slotActivity = useAppSelector(s => s.chat.slotActivity)
  // Queued-but-not-started agents have no entry in the per-slot subagents map,
  // so a slot whose whole wave is still behind the concurrency cap counted 0
  // and showed no subtitle at all — the window in which a user is most likely
  // to wonder whether their spawn did anything. Fold the queue depth in.
  const subagentQueued = useAppSelector(s => s.chat.subagentQueued)
  const subagentCounts = useMemo(() => {
    const countActive = (m?: Record<string, SubagentActivity>) => {
      if (!m) return 0
      let n = 0
      for (const a of Object.values(m)) {
        if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') n++
      }
      return n
    }
    const counts: Record<string, number> = {}
    if (storeActiveSlot) { const n = countActive(activeSlotSubagents); if (n > 0) counts[storeActiveSlot] = n }
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // Load-bearing: on switchSlot the active slot's subagents map is aliased
      // into BOTH state.subagents and slotActivity[active].subagents (same
      // object reference), so skipping the active slot here is what prevents
      // double-counting it. Do not drop this guard.
      if (slot === storeActiveSlot) continue
      const n = countActive(act.subagents)
      if (n > 0) counts[slot] = n
    }
    for (const [slot, q] of Object.entries(subagentQueued ?? {})) {
      if (q > 0) counts[slot] = (counts[slot] || 0) + q
    }
    return counts
  }, [storeActiveSlot, activeSlotSubagents, slotActivity, subagentQueued])
  // Sub-agents blocked on a SPAWN approval, per slot. Mirrors
  // selectSlotPendingSpawnApprovals (status 'pending' + an approval_id), but
  // across every slot rather than the viewed one: a spawn approval raised by a
  // background chat has no inline prompt and no notification, so without this
  // the sidebar was the only place it could have surfaced and it showed
  // "N agents running" instead — an owed decision rendered as work in
  // progress. Counted separately from `subagentCounts` so the running subtitle
  // can subtract them (an agent waiting on approval is not running).
  const subagentApprovalCounts = useMemo(() => {
    const countPending = (m?: Record<string, SubagentActivity>) => {
      if (!m) return 0
      let n = 0
      for (const a of Object.values(m)) {
        if (a.status === 'pending' && a.approval_id) n++
      }
      return n
    }
    const counts: Record<string, number> = {}
    if (storeActiveSlot) { const n = countPending(activeSlotSubagents); if (n > 0) counts[storeActiveSlot] = n }
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // Same aliasing guard as countActive above: the active slot's map is the
      // same object in both places, so skipping it here avoids double-counting.
      if (slot === storeActiveSlot) continue
      const n = countPending(act.subagents)
      if (n > 0) counts[slot] = n
    }
    return counts
  }, [storeActiveSlot, activeSlotSubagents, slotActivity])
  // Live dynamic-workflow runs per slot, for the sidebar row's "workflow
  // running" subtitle. Mirrors WorkflowProgressBar's source of truth:
  // chatSlice.workflowRuns (populated by the globally-subscribed
  // workflow_run_event WS broadcasts, so background slots are covered too),
  // scoped to each slot via runBelongsToSlot — the same ownership rule that
  // keeps a run pinned to ITS chat and not every open chat.
  const workflowRuns = useAppSelector(s => s.chat.workflowRuns)
  const workflowActive = useMemo(() => {
    const out: Record<string, { count: number; label: string }> = {}
    const running = Object.values(workflowRuns ?? {}).filter(r => r.status === 'running')
    if (running.length === 0) return out
    for (const s of slots) {
      const mine = running.filter(r => runBelongsToSlot(r.sessionKey, s.key))
      if (mine.length === 0) continue
      const first = mine[0]
      const name = sanitizeLlmOutput(first.name || first.run_id).slice(0, 60)
      const phase = first.phase ? sanitizeLlmOutput(first.phase).slice(0, 40) : ''
      out[s.key] = {
        count: mine.length,
        label: mine.length > 1
          ? `${mine.length} workflows running`
          : `${name}${phase ? ` · ${phase}` : ''}`,
      }
    }
    return out
  }, [workflowRuns, slots])
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  const connected = useConnected()
  // O(1) lookup set for the filter predicate (mirrors the `pinned` and
  // `slotSearchRanks` patterns elsewhere in this file).
  const unreadSet = useMemo(() => new Set(unreadSlots), [unreadSlots])
  // Heartbeat that re-evaluates recency even when nothing else re-renders.
  // Sidebar interactions (new messages, status changes, opening the menu) all
  // recompute the recency lookup for free, so this only matters when the sidebar
  // sits idle with the Recent filter on — without it a stale session would
  // never age out of the list. Gated on the filter being active so we don't
  // wake an idle tab needlessly, mirroring the `staleTick` pattern in App.tsx.
  const recentFilterActive = activeFilters.has('recent')
  // User-selectable recency window (ms), persisted. Presets + custom value live
  // in the filter submenu; the chip and menu row show the current window.
  const [recentWindowMs, setRecentWindowMs] = useState(readStoredRecentWindow)
  const setRecentWindow = useCallback((ms: number) => {
    setRecentWindowMs(ms)
    safeSetItem(RECENT_WINDOW_LS_KEY, String(ms))
  }, [])
  // Custom-picker draft state. The amount is a raw string (not derived from the
  // committed window) so the field can be cleared / partially edited without
  // snapping to 1 on every keystroke, and the unit stays exactly as the user
  // picked it rather than being re-derived (24 "hours" must not flip to 1 "day").
  // We commit + clamp to `recentWindowMs` only on blur / Enter / unit change; a
  // preset click re-seeds both drafts so the boxes track the chosen preset.
  const [recentAmountDraft, setRecentAmountDraft] = useState(() => String(decomposeRecentWindow(recentWindowMs).value))
  const [recentUnitDraft, setRecentUnitDraft] = useState<RecentUnit>(() => decomposeRecentWindow(recentWindowMs).unit)
  const selectRecentPreset = useCallback((ms: number) => {
    setRecentWindow(ms)
    const { value, unit } = decomposeRecentWindow(ms)
    setRecentAmountDraft(String(value))
    setRecentUnitDraft(unit)
  }, [setRecentWindow])
  const commitRecentAmount = useCallback(() => {
    const clamped = clampRecentAmount(recentAmountDraft)
    setRecentAmountDraft(String(clamped))
    setRecentWindow(customRecentWindowMs(clamped, recentUnitDraft))
  }, [recentAmountDraft, recentUnitDraft, setRecentWindow])
  const changeRecentUnit = useCallback((unit: RecentUnit) => {
    setRecentUnitDraft(unit)
    setRecentWindow(customRecentWindowMs(recentAmountDraft, unit))
  }, [recentAmountDraft, setRecentWindow])
  const [recentTick, setRecentTick] = useState(0)
  useEffect(() => {
    if (!recentFilterActive) return
    // Tick often enough that a slot ages out promptly relative to its window
    // (~1/10th the window), but never faster than every 30s and never slower
    // than RECENT_TICK_MS — a short custom window shouldn't wake the tab every
    // few seconds, and a long one shouldn't lag by more than ~10 minutes.
    const id = setInterval(() => setRecentTick(t => t + 1), recentTickIntervalMs(recentWindowMs))
    return () => clearInterval(id)
  }, [recentFilterActive, recentWindowMs])
  // Wider than the payload's `s.running`: a live workflow run or an active goal
  // loop counts as in progress, so neither drops out of the filter or its count.
  const runningSet = useMemo<Set<string>>(() => {
    const out = new Set<string>()
    for (const s of slots) {
      // Own-property read: the store normalizes writes through `safeKey`, so a
      // bare index read could resolve a `__proto__`-like key to a truthy value.
      const looping = Object.prototype.hasOwnProperty.call(goalLoops ?? {}, s.key)
      if (s.running || !!workflowActive[s.key] || looping) out.add(s.key)
    }
    return out
  }, [slots, workflowActive, goalLoops])
  // A running turn is recent BY DEFINITION: the ordering key stops advancing
  // mid-turn, so a long turn would age out while it is the busiest row on screen.
  const recentSet = useMemo<Set<string>>(() => {
    // One `now` per recompute, so every slot is measured against the same instant.
    // The last-activity timestamp mirrors the date-sort comparator.
    const now = Date.now()
    const out = new Set<string>()
    for (const s of slots) {
      if (runningSet.has(s.key) || isWithinRecentWindow(slotActivityTs(s), now, recentWindowMs)) out.add(s.key)
    }
    return out
    // `recentTick` is an intentional dep: it forces recency to re-evaluate on
    // the heartbeat above so idle sessions age out of the Recent filter.
  }, [slots, runningSet, recentWindowMs, recentTick]) // eslint-disable-line react-hooks/exhaustive-deps
  // Exhaustive over `SessionFilterKey` on purpose: a new filter key becomes a
  // type error here instead of a predicate that silently matches nothing.
  const _derivedLookup = useMemo<Record<SessionFilterKey, (slot: Slot) => boolean>>(() => ({
    unread: slot => unreadSet.has(slot.key),
    running: slot => runningSet.has(slot.key),
    pinned: slot => !!slot.pinned,
    recent: slot => recentSet.has(slot.key),
  }), [unreadSet, runningSet, recentSet])
  const filterCounts = useMemo(() => {
    const counts = {} as Record<SessionFilterKey, number>
    for (const filterDef of SESSION_FILTERS) counts[filterDef.key] = slots.filter(_derivedLookup[filterDef.key]).length
    return counts
  }, [slots, _derivedLookup])
  // Ref mirror of `activeFilters` so the auto-drain effect can read the
  // current toggle state without depending on it. Keeps the effect from
  // re-firing on its own setState output.
  const activeFiltersRef = useRef(activeFilters)
  activeFiltersRef.current = activeFilters
  // Auto-disable the unread filter when the inbox drains, so the user doesn't
  // end up staring at an empty list. Decision logic lives in the pure helper
  // `decideUnreadDrain` so it can be unit-tested in isolation — see
  // `src/test/unreadDrain.test.ts`. The null-sentinel on `prevUnreadCount`
  // distinguishes "data not yet loaded" from "data loaded and genuinely empty"
  // so the persisted=true + loads-empty case fires on the first post-load
  // tick. See the helper's docstring for the known accepted batched-update
  // edge case.
  const prevUnreadCount = useRef<number | null>(null)
  useEffect(() => {
    // Guard the ENTIRE body on slotsLoaded: without this, the unconditional
    // `prevUnreadCount.current = unreadSlots.length` assignment below would
    // destroy the null sentinel on the pre-load effect run, breaking the
    // case-2 "loadedEmpty" branch in `decideUnreadDrain`. The helper's own
    // !slotsLoaded check stays as defense-in-depth.
    if (!slotsLoaded) return
    const action = decideUnreadDrain({
      prev: prevUnreadCount.current,
      current: unreadSlots.length,
      slotsLoaded,
      showUnreadOnly: activeFiltersRef.current.has('unread'),
    })
    if (action === 'disable') disableFilter('unread')
    prevUnreadCount.current = unreadSlots.length
  }, [unreadSlots.length, slotsLoaded, disableFilter])
  const [historyOpen, setHistoryOpen] = useState(false)
  // History pane height (persisted). Drag handle adjusts this while open.
  const HISTORY_HEIGHT_LS_KEY = 'mc-history-height'
  const HISTORY_MIN_HEIGHT = 120
  const HISTORY_MAX_HEIGHT = 800
  const [historyHeight, setHistoryHeight] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem(HISTORY_HEIGHT_LS_KEY) || '', 10)
    return Number.isFinite(saved) && saved >= HISTORY_MIN_HEIGHT && saved <= HISTORY_MAX_HEIGHT ? saved : 240
  })
  useEffect(() => { safeSetItem(HISTORY_HEIGHT_LS_KEY, String(historyHeight)) }, [historyHeight])
  const [historyDragging, setHistoryDragging] = useState(false)
  const historyStartHRef = useRef(0)
  const historyDraggingRef = useRef(false)
  const historyResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      historyStartHRef.current = historyHeight
      historyDraggingRef.current = true
      setHistoryDragging(true)
      document.body.style.cursor = 'ns-resize'
      document.body.style.userSelect = 'none'
    },
    onMove: ({ dy }) => {
      // Drag handle is ABOVE the pane, so dragging UP (dy < 0) grows the pane.
      setHistoryHeight(Math.max(HISTORY_MIN_HEIGHT, Math.min(HISTORY_MAX_HEIGHT, historyStartHRef.current - dy)))
    },
    onEnd: () => {
      historyDraggingRef.current = false
      setHistoryDragging(false)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    },
  })
  // Unmount guard: onEnd can't fire if the sidebar unmounts mid-drag
  // (setPointerCapture dies with the element), so restore the global body styles
  // here to avoid leaving the resize cursor / text-selection lock stuck.
  useEffect(() => () => {
    if (historyDraggingRef.current) {
      historyDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [manageTagsOpen, setManageTagsOpen] = useState(false)  // header ⋮ → "Manage tags…" panel (list-view tag CRUD)
  const [filterSortOpen, setFilterSortOpen] = useState(false)
  const [cleanupDays, setCleanupDays] = useState(3)
  const [cleanupExpanded, setCleanupExpanded] = useState(false)
  const [cleanupError, setCleanupError] = useState('')
  const { data: cleanupPreviewData, isLoading: cleanupPreviewLoading, isError: cleanupPreviewError } = useQuery({
    queryKey: ['cleanup-preview', cleanupDays, activeSlot],
    queryFn: () => api.cleanupSessions(cleanupDays, activeSlot || '', true),
    enabled: cleanupOpen,
    gcTime: 0,
  })
  const cleanupPreview = cleanupPreviewData?.keys ?? null
  const activeIsStale = cleanupPreviewData?.active_is_stale ?? false
  const cleanupMutation = useMutation({
    mutationFn: () => api.cleanupSessions(cleanupDays, activeSlot || ''),
    onSuccess: (res) => {
      if (res.keys?.length) {
        for (const key of res.keys) dispatch(deleteSlot(key))
        dispatch(fetchHistory(false))
      }
      if (res.failed?.length) {
        setCleanupError(`${res.failed.length} session(s) failed to archive`)
      } else {
        setCleanupOpen(false)
      }
      queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })
    },
    onError: (e) => setCleanupError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.archive_failed')),
  })

  // Bulk model switch — apply one model to every live session at once.
  const [bulkModelOpen, setBulkModelOpen] = useState(false)
  const [bulkModel, setBulkModel] = useState('')        // pending pick ('auto' = provider default)
  const [bulkSkipRunning, setBulkSkipRunning] = useState(true)
  const [bulkModelError, setBulkModelError] = useState('')
  const bulkModelOptions = useAvailableModels({ enabled: bulkModelOpen })
  const bulkRunningCount = useMemo(() => slots.filter(s => s.running).length, [slots])
  // Count only slots that would actually change: model differs from the target
  // (the backend leaves already-on-target slots as `unchanged`), minus running
  // slots when skipping. Keeps the "Switch N" label + disable guard honest.
  const bulkAffectedCount = useMemo(() => {
    return slots.filter(s => (s.model ?? '') !== bulkModel && (!bulkSkipRunning || !s.running)).length
  }, [slots, bulkModel, bulkSkipRunning])
  const bulkModelMutation = useMutation({
    // 'auto' goes on the wire verbatim (not collapsed to ''): '' doubles as the
    // "never chosen" state that every reader re-resolves to the agent template's
    // model, so it cannot express an explicit Auto pick.
    mutationFn: ({ model, skipRunning }: { model: string; skipRunning: boolean }) =>
      api.chatSlotsModel(model, skipRunning),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
      // Partial failure: the endpoint returns 200 with a non-empty `failed`
      // list when some slots' resets raised. Surface it and keep the panel
      // open instead of silently closing on a partial success.
      if (res.failed?.length) {
        setBulkModelError(`${res.failed.length} session${res.failed.length !== 1 ? 's' : ''} failed to switch`)
      } else {
        setBulkModelOpen(false)
        setBulkModel('')
        setBulkModelError('')
      }
    },
    onError: (e) => setBulkModelError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.switch_failed')),
  })
  // Roving-focus keyboard nav for the model list (WAI-ARIA listbox). No filter
  // input here, so the hook moves focus into the list on open; Escape/Tab close.
  const bulkListRef = useRef<HTMLDivElement>(null)
  const bulkInputRef = useRef<HTMLInputElement>(null)
  const { onListKeyDown: bulkOnListKeyDown } = useListboxKeyboard({
    open: bulkModelOpen,
    dropdownRef: bulkListRef,
    inputRef: bulkInputRef,
    hasFilterInput: false,
    filteredCount: bulkModelOptions.length,
    onEnterSingleMatch: () => {},
    closeToTrigger: () => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') },
  })

  // Pinned: derived from server-persisted slot.pinned
  const pinned = useMemo(() => new Set(slots.filter(s => s.pinned).map(s => s.key)), [slots])
  // Ranks up to the configured count of sessions by settled recency for the sidebar tint —
  // see ../utils/recencyTint. Count = server-side dashboard.recent_tint_count (shared
  // kirocrewConfig query); recomputes when the slots or the configured count change.
  const { data: mcCfg } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const recentTintCount = clampTintCount(mcCfg?.dashboard?.recent_tint_count)
  const recentRank = useMemo(() => computeRecentRank(slots, recentTintCount), [slots, recentTintCount])

  // Folder editing state
  const [editingId, setEditingId] = useState<string | null>(null)
  // Board view renders a folder once per column, so `editingId === folder.id`
  // is true in every column at once — the input would mount in all of them and
  // the shared ref would bind to the last. This scope pins the folder rename to
  // the clicked column's render instance (the columnId, or 'list' in list view)
  // so exactly one input mounts. renderFolderHeader passes 'list';
  // renderColumnFolder passes columnId. Folder CREATION needs no such scope —
  // it is a single root-level modal, not a per-column inline input.
  const [editScope, setEditScope] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  // Folder rename (renderFolderHeader + board renderColumnFolder) mounts its
  // input from a Radix menu, so plain autoFocus loses the same race as the
  // session rename: focus lands on the trigger/body after the menu tears down
  // (caret never in the box) and the default scroll-into-view yanks the
  // horizontally-scrolling board sideways. Re-grab focus on the next frame with
  // preventScroll so the board doesn't jump, selecting the text for overtype.
  // Keyed on both the id AND editScope: a same-id, scope-only change (retarget
  // to a different column before the first column's commit fires) must re-run so
  // focus lands in the newly-mounted column's input. The re-focus is idempotent
  // so re-running is harmless. When the id clears (commit/cancel/escape/blur),
  // clear the scope so no stale column identity lingers.
  useEffect(() => {
    if (!editingId) { setEditScope(null); return }
    const raf = requestAnimationFrame(() => {
      const el = folderEditInputRef.current
      if (el) { el.focus({ preventScroll: true }); el.select() }
    })
    return () => cancelAnimationFrame(raf)
  }, [editingId, editScope])
  // Belt-and-suspenders disarm of the one-shot suppress ref. It's normally
  // consumed by the very next onCloseAutoFocus, but if a menu is ever dismissed
  // without firing that (an outside-dismiss race), the ref would stay armed and
  // wrongly preventDefault the NEXT menu close. Whenever the sidebar is idle (no
  // edit open), force-disarm: no legitimate pending suppression can exist then.
  // Safe against the normal flow — during a live edit an id is non-null, so this
  // hasn't run yet; by the time all ids clear the real close already consumed it.
  useEffect(() => {
    if (!renamingSlot && !editingId) suppressMenuRestoreRef.current = false
  }, [renamingSlot, editingId])

  // Resize logic — Pointer Events (mouse + touch + pen) via usePointerDrag, so
  // the handle works on touch devices too, e.g. a tablet at desktop width where
  // the sidebar is a side-by-side panel (the mouse-only handler ignored touch).
  // setPointerCapture keeps move/up firing when the pointer leaves the thin
  // handle, replacing the old window-level mousemove/mouseup listeners.
  const sidebarStartW = useRef(0)
  const sidebarDraggingRef = useRef(false)
  const sidebarWidthRef = useRef(sidebarWidth)
  sidebarWidthRef.current = sidebarWidth
  const onWidthChangeRef = useRef(onWidthChange)
  onWidthChangeRef.current = onWidthChange
  const onDragChangeRef = useRef(onDragChange)
  onDragChangeRef.current = onDragChange
  useEffect(() => { onWidthChangeRef.current?.(sidebarWidth) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // threshold 0: a dedicated edge affordance resizes immediately on press (no
  // 10px hysteresis), matching the original mouse resizer's feel.
  const sidebarResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      sidebarStartW.current = sidebarWidthRef.current
      sidebarDraggingRef.current = true
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      onDragChangeRef.current?.(true)
    },
    onMove: ({ dx }) => {
      const newW = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, sidebarStartW.current + dx))
      setSidebarWidth(newW)
      onWidthChangeRef.current?.(newW)
    },
    onEnd: () => {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
      const w = sidebarWidthRef.current
      safeSetItem(SIDEBAR_LS_KEY, String(w))
      onWidthChangeRef.current?.(w)
    },
  })

  // Unmount guard: if the sidebar unmounts mid-drag (collapse / route change),
  // onEnd never fires — setPointerCapture dies with the element — so the global
  // body styles and the parent's dragging state would stay stuck. Restore them
  // on teardown. The old mouse-only handler did this in its listener cleanup;
  // the pointer migration must preserve it.
  useEffect(() => () => {
    if (sidebarDraggingRef.current) {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
    }
  }, [])

  // Folders via React Query
  const { data: folders = [] } = useQuery<ChatFolder[]>({ queryKey: ['chat-folders'], queryFn: () => api.chatFolders() })

  // Tags via React Query (dynamic vocabulary, defaults seeded server-side)
  const { data: tags = [] } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags() })
  const tagById = useMemo(() => {
    const m: Record<string, ChatTag> = {}
    for (const t of tags) m[t.id] = t
    return m
  }, [tags])
  /** Selected ids narrowed to tags that STILL EXIST. Deleting a tag leaves its id
   *  in localStorage, and an unresolvable id matches no session — so without this
   *  guard, deleting the last selected tag would hide every session with no
   *  control left on screen to explain why. Unresolvable ids are ignored rather
   *  than pruned: the tag list is a server query, so an id absent from a slow or
   *  failed fetch must not silently destroy a valid selection. */
  const activeTagIds = useMemo(
    () => new Set([...filterTagIds].filter(id => tagById[id])),
    [filterTagIds, tagById],
  )
  /** Rows for the filter menu's Tags section, in the tag vocabulary's own order.
   *  Counts come from all `slots`, NOT `filteredSlots`, so they describe the
   *  vocabulary rather than the current selection — otherwise every unselected tag
   *  would read 0 the moment any tag was selected, which is the number a user
   *  consults precisely when deciding what to select next. */
  const tagFilterRows = useMemo(
    () => [...tags]
      .sort((a, b) => a.order - b.order)
      .map(t => ({
        tag: t,
        count: slots.filter(s => (s.tags ?? []).includes(t.id)).length,
        selected: filterTagIds.has(t.id),
      })),
    [tags, slots, filterTagIds],
  )
  /** Names of the selected tags, in vocabulary order. Disjunction, not a comma
   *  join: selection is a union, so a screen reader should hear "Blocked or
   *  Idea", and `fmtList` is what makes that read correctly in every language. */
  const activeTagNames = useMemo(
    () => tagFilterRows.filter(({ tag: t }) => activeTagIds.has(t.id)).map(({ tag: t }) => t.name),
    [tagFilterRows, activeTagIds],
  )
  // Sidebar column layout (flat list; empty = legacy single-lane UX)
  const { data: rawColumns = [] } = useQuery<TagColumn[]>({ queryKey: ['tag-columns'], queryFn: () => api.tagColumns() })
  const [tagColumnsEnabled, setTagColumnsEnabled] = useState(() => loadChatConfig().tagColumnsEnabled)
  useEffect(() => {
    const onChange = () => setTagColumnsEnabled(loadChatConfig().tagColumnsEnabled)
    window.addEventListener('mc-config-changed', onChange)
    return () => window.removeEventListener('mc-config-changed', onChange)
  }, [])
  // When feature is disabled, treat it as zero columns → sidebar falls back to legacy layout.
  // Derive the effective column list inside the memo so its identity only changes
  // when the stable inputs (rawColumns / tagColumnsEnabled) change, not every render.
  const orderedColumns = useMemo(() => {
    const columns: TagColumn[] = tagColumnsEnabled ? rawColumns : []
    return [...columns].sort((a, b) => a.order - b.order)
  }, [rawColumns, tagColumnsEnabled])
  const [columnEditId, setColumnEditId] = useState<string | null>(null)  // column whose popover is open
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null)
  // The column-filter popover is portaled to <body>, so it is outside the trigger's
  // DOM tab-order and never receives focus on open. columnPopoverRef + the effect
  // below move focus into it, and closeColumnPopover returns focus to the trigger —
  // together with the onKeyDown (Escape + Tab-trap) on the popover, this makes the
  // portaled overlay fully keyboard-operable.
  const columnPopoverRef = useRef<HTMLDivElement>(null)
  const closeColumnPopover = useCallback((colId: string) => {
    setColumnEditId(null)
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-testid="column-edit-${colId}"]`)?.focus())
  }, [])
  // Anchor the popover to the edit button's bounding rect so it stays put even
  // though it renders in a portal outside the (overflow-hidden) column ancestor.
  useEffect(() => {
    if (!columnEditId) { setPopoverPos(null); return }
    const updatePos = () => {
      const btn = document.querySelector<HTMLElement>(`[data-testid="column-edit-${columnEditId}"]`)
      if (!btn) return
      const r = btn.getBoundingClientRect()
      setPopoverPos({ top: r.bottom + 4, left: r.left })
    }
    updatePos()
    window.addEventListener('resize', updatePos)
    window.addEventListener('scroll', updatePos, true)
    return () => {
      window.removeEventListener('resize', updatePos)
      window.removeEventListener('scroll', updatePos, true)
    }
  }, [columnEditId])
  // Close column-filter popover on outside click
  useEffect(() => {
    if (!columnEditId) return
    const handler = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (!t) return
      if (t.closest(`[data-column-popover="${columnEditId}"]`)) return
      if (t.closest(`[data-testid="column-edit-${columnEditId}"]`)) return
      setColumnEditId(null)
    }
    // Defer one tick so the same click that opened the popover doesn't immediately close it
    const id = setTimeout(() => document.addEventListener('mousedown', handler), 0)
    return () => { clearTimeout(id); document.removeEventListener('mousedown', handler) }
  }, [columnEditId])
  // Move focus into the portaled column-filter popover once it is positioned. We
  // focus the dialog container itself (tabIndex=-1) — not its first control — so the
  // screen reader announces the dialog and Tab then walks its fields in order; this
  // avoids landing on the Close button (first in DOM) or stealing focus into a text field.
  useEffect(() => {
    if (!columnEditId || !popoverPos) return
    // Focus only on initial open. popoverPos gets a fresh object on every
    // resize/scroll reflow, re-running this effect — so bail if focus is already
    // inside the popover (e.g. the user is typing in the rename input) to avoid
    // yanking it back to the container.
    if (columnPopoverRef.current?.contains(document.activeElement)) return
    const raf = requestAnimationFrame(() => columnPopoverRef.current?.focus())
    return () => cancelAnimationFrame(raf)
  }, [columnEditId, popoverPos])


  const updateColumnMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; tag_ids?: string[]; mode?: TagColumnMode; order?: number; include_untagged?: boolean } }) => api.updateTagColumn(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
    // The server rejects a tag_ids payload naming an unknown tag (400
    // invalid_column_payload) instead of silently dropping it — e.g. the tag
    // was deleted from another window while this popover's cache was stale.
    // Re-sync both caches so the popover redraws from reality (the stale tag
    // disappears) rather than leaving a selection that looks applied but isn't.
    onError: () => {
      queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
    },
  })
  const deleteColumnMutation = useMutation({
    mutationFn: (id: string) => api.deleteTagColumn(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const reorderColumnsMutation = useMutation({
    mutationFn: (ids: string[]) => api.reorderTagColumns(ids),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const addColumnAfterMutation = useMutation({
    mutationFn: async (afterColId: string) => {
      const created = await api.createTagColumn({ name: '', tag_ids: [], mode: 'any' })
      const ids = orderedColumns.map(c => c.id)
      const idx = ids.indexOf(afterColId)
      ids.splice(idx + 1, 0, created.id)
      const uniqIds: string[] = []
      for (const id of ids) { if (!uniqIds.includes(id)) uniqIds.push(id) }
      await api.reorderTagColumns(uniqIds)
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const dropSlotMutation = useMutation({
    mutationFn: ({ slot, columnId }: { slot: string; columnId: string }) => api.dropSlotToColumn(slot, columnId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-slots'] }),
  })
  /** Lanes the board does not have yet. Drives the seeding write and the menu
   *  affordance, so the offer to add lanes appears exactly when there is
   *  something to add — including after a partial failure left the set
   *  incomplete, which is what makes "click again to finish" a real recovery
   *  path rather than a claim.
   *
   *  This is an AFFORDANCE, not the uniqueness rule. It reads a cached column
   *  list, so two dashboards can both compute the same missing lane; the backend
   *  decides uniqueness by `state_key` under its write lock and returns the
   *  existing lane instead of creating a second one. */
  const missingLanes = useMemo(() => {
    const present = new Set(rawColumns.filter(c => c.source === 'state').map(c => c.state_key))
    return SESSION_LANES.filter(lane => !present.has(lane.key))
  }, [rawColumns])

  /** Add the four derived state lanes to the board.
   *
   *  Purely ADDITIVE and IDEMPOTENT: it creates only the lanes that are missing
   *  and never deletes a column. That is the invariant, not an implementation
   *  detail — an additive action that also disposes of persisted rows has to
   *  guess which ones are disposable, and the unnamed match-all shape the view
   *  toggle once created is byte-identical to a bare column the user added
   *  themselves via "Add column after". No predicate can separate them, so the
   *  only safe answer is to delete neither.
   *
   *  Consequences of the invariant, all of them deliberate:
   *  - There is no two-write ordering to get wrong, so a mid-flight failure
   *    leaves fewer lanes rather than a board stripped of its columns; clicking
   *    again completes the set because creation is keyed on what is missing.
   *  - A pre-existing bare column survives and sits beside the lanes, showing
   *    every session. It is one click to remove and is not ours to delete.
   *  - Re-running is harmless, which is what makes the pending-guard on the
   *    menu items a second line of defence rather than the only one.
   */
  const seedStateLanesMutation = useMutation({
    mutationFn: async () => {
      for (const lane of missingLanes) {
        await api.createTagColumn({ source: 'state', state_key: lane.key, tag_ids: [], mode: 'any' })
      }
      return rawColumns.length + missingLanes.length
    },
    onSuccess: (columnCount: number) => {
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
      // A board is a horizontal strip inside a 260px-default sidebar, so lanes
      // that do not fit are reachable only by discovering the resize handle.
      // Widen once to fit them; never shrink, so a width the user chose stands.
      const next = boardSidebarWidth(columnCount, sidebarWidthRef.current, window.innerWidth)
      if (next !== sidebarWidthRef.current) {
        // Remember what the user had, so leaving board view can give it back.
        // Persisting the automatic width without this destroys their chosen
        // width permanently and strands a ~900px sidebar in list view.
        safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, String(sidebarWidthRef.current))
        setSidebarWidth(next)
        onWidthChangeRef.current?.(next)
        safeSetItem(SIDEBAR_LS_KEY, String(next))
      }
    },
    onError: (err) => {
      // Without this the toggle has already flipped to board view and nothing
      // renders: no board, no message, no way to tell it failed from an empty
      // one. Report it and hand back list view when nothing was created.
      setSeedError(err instanceof Error ? err.message : String(err))
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
    },
  })
  // Filter predicate for a single column. Takes the whole slot, not just its
  // tags: a state column's membership is derived from live runtime fields, and
  // a lane needs the same extras the row status chain uses (a parent whose
  // sub-agent is blocked owes an approval even though the parent is idle).
  const columnMatches = useCallback((col: TagColumn, slot: Slot): boolean => {
    if (col.source === 'state') {
      if (!col.state_key) return false
      // Clamped against the running count exactly as the row status chain does:
      // an approval count above the live agent count is stale, and unclamped it
      // would pin an otherwise-idle session to Needs Approval indefinitely.
      const running = subagentCounts[slot.key] || 0
      // `slot` here is the raw payload, whose `running` covers only the slot's
      // own turn. A dynamic workflow and a goal loop are both live work that
      // outlive that flag, and the row status chain already reads them from the
      // store — so the lane must too, or a session renders a workflow spinner
      // while sitting in Idle.
      return inferLane(slot, {
        subagentAwaiting: Math.min(subagentApprovalCounts[slot.key] || 0, running),
        backgroundWork: !!workflowActive[slot.key]
          || Object.prototype.hasOwnProperty.call(goalLoops ?? {}, slot.key),
      }) === col.state_key
    }
    const slotTags = slot.tags || []
    // "include untagged" OR'd on top of any tag filter
    if (col.include_untagged && slotTags.length === 0) return true
    if (!col.tag_ids || col.tag_ids.length === 0) return true
    const set = new Set(slotTags)
    if (col.mode === 'all') return col.tag_ids.every(t => set.has(t))
    if (col.mode === 'none') return !col.tag_ids.some(t => set.has(t))
    return col.tag_ids.some(t => set.has(t))  // 'any'
  }, [subagentApprovalCounts, subagentCounts, goalLoops, workflowActive])

  const slotFolders = useMemo(() => {
    const valid = new Set(folders.map(f => f.id))
    const m: Record<string, string> = {}
    for (const s of slots) { if (s.folder_id && valid.has(s.folder_id)) m[s.key] = s.folder_id }
    return m
  }, [slots, folders])

  // Folder IDs that hold at least one ACTIVE slot, directly or via any
  // descendant folder. Computed from all `slots` (not filteredSlots) so a
  // search/filter never spuriously hides a folder that still holds work.
  const foldersWithActiveSubtree = useMemo(() => {
    const direct: string[] = []
    for (const s of slots) { const fid = slotFolders[s.key]; if (fid) direct.push(fid) }
    return computeActiveSubtree(folders, direct)
  }, [folders, slots, slotFolders])

  // A folder drops out of the active list only when the user hid it AND it is
  // currently empty (no active session in its subtree). Re-engaging a session
  // clears `hidden` server-side, so visibility is `!hidden || hasActive`.
  const isFolderHidden = useCallback(
    (f: ChatFolder) => folderIsHidden(f, foldersWithActiveSubtree),
    [foldersWithActiveSubtree],
  )

  // Folder IDs whose sessions are excluded from the flat lane because the
  // folder — or any ancestor — is unchecked in the filter menu's folder list.
  // Unchecking a parent hides its whole subtree, matching what the user sees
  // in the tree. Cycle-guarded: a hand-edited folders.json can contain a
  // parent_id loop and must not freeze the tab.
  const filterHiddenSubtree = useMemo(() => {
    if (filterHiddenFolders.size === 0) return new Set<string>()
    const byId = new Map(folders.map(f => [f.id, f]))
    const hidden = new Set<string>()
    for (const f of folders) {
      let cur: ChatFolder | undefined = f
      const visited = new Set<string>()
      while (cur && !visited.has(cur.id)) {
        visited.add(cur.id)
        if (filterHiddenFolders.has(cur.id)) { hidden.add(f.id); break }
        cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
      }
    }
    return hidden
  }, [folders, filterHiddenFolders])

  // The backend relevance ranking, live only while the query is long enough to
  // have been sent. Shared by the search dimension's row predicate and by
  // filteredSlots' sort, so the two cannot disagree about when ranking is on.
  const searchRanked = useMemo(
    () => (slotFilter.trim().length >= SEARCH_MIN_CHARS ? slotSearchRanks : null),
    [slotFilter, slotSearchRanks],
  )

  /**
   * THE single declaration of every filter dimension. `filteredSlots`,
   * `listNarrowed`, and `revealBlockingFilters` all derive from this list, so
   * adding a dimension is one entry here — the required fields force a
   * decision per consumer, and THOSE THREE consumers cannot drift because
   * none of them enumerates dimensions itself any more. The guard's limit:
   * this declaration cannot see filtering done at the render sites (the
   * folder dimension works that way), so a dimension that acts there must
   * still answer `narrows` for real — writing `null` while narrowing the
   * visible list at a render site re-creates the under-count this exists to
   * prevent.
   *
   * The consumers legitimately answer different questions, and the per-field
   * differences below are deliberate, not drift:
   * - the folder dimension filters no rows (`filtersRow: null` — it drops
   *   whole folder blocks/lanes at the render sites) and never narrows
   *   (`narrows: null` — see the field docs on `FilterDimension`);
   * - tags narrow by the RESOLVED `activeTagIds` but hide by the raw
   *   `filterTagIds`, so a reveal arriving while the tag vocabulary is still
   *   loading (when nothing is filtered yet) still clears the tag filter
   *   instead of leaving the row to be re-hidden mid-flight.
   *
   * Bundling every consumer's state into one memo couples them: a change to
   * reveal-only state (`filterTagIds`, `filterHiddenSubtree`, `folders`)
   * re-derives `filteredSlots` — one extra filter+sort with content-identical
   * rows. Accepted: no effect keys on `filteredSlots`, and its downstream
   * memos already depend on that state themselves.
   */
  const filterDimensions = useMemo<FilterDimension[]>(() => {
    const activeFilterDefs = SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key))
    return [
      {
        // Tags. Unlike the folder filter this does NOT go inert while
        // searching: it is a session property, so it behaves like the
        // Unread/Pinned status chips.
        filtersRow: slot => activeTagIds.size === 0 || (slot.tags ?? []).some(id => activeTagIds.has(id)),
        narrows: () => activeTagIds.size > 0,
        // Raw `filterTagIds`, not resolved `activeTagIds`, and not behind
        // `excluded`: mid-flight nothing is filtered, so the row is re-hidden.
        hides: slot => filterTagIds.size > 0 && !(slot.tags ?? []).some(id => filterTagIds.has(id)),
        clear: () => clearTagFilter(),
      },
      {
        // Text search. Scoped to title while the backend ranking is live: that
        // is the field a rename mutates, and widening it to key/agent appends
        // rows the backend's content search deliberately excluded.
        filtersRow: slot => {
          if (!slotFilter) return true
          const titleMatch = (slot.title || '').toLowerCase().includes(slotFilter.toLowerCase())
          if (searchRanked) return searchRanked.has(slot.key) || titleMatch
          return ((slot.title || '') + slot.key + (slot.agent || '')).toLowerCase().includes(slotFilter.toLowerCase())
        },
        narrows: () => Boolean(slotFilter),
        hides: (slot, excluded) => Boolean(slotFilter) && excluded(slot),
        clear: () => setSlotFilter(''),
      },
      {
        // Status chips (SESSION_FILTERS). Active chips OR together: a row
        // passes when any active chip's predicate matches it.
        filtersRow: slot => activeFilterDefs.length === 0 || activeFilterDefs.some(filterDef => _derivedLookup[filterDef.key](slot)),
        narrows: () => activeFilters.size > 0,
        hides: (slot, excluded) => activeFilters.size > 0 && excluded(slot),
        clear: () => {
          // Persisted like toggleFilter: remount re-reads the stored '1' and
          // would silently restore the filter that hides this row.
          for (const filterDef of SESSION_FILTERS) {
            if (activeFilters.has(filterDef.key)) safeSetItem(filterDef.storageKey, '0')
          }
          setActiveFilters(new Set())
        },
      },
      {
        // Folder filter. It filters no rows and never narrows (see the memo
        // doc above). The folder-EXPANSION step lives outside the reveal
        // registry on purpose: it runs whether or not this filter was hiding
        // anything.
        filtersRow: null,
        narrows: null,
        hides: slot => !!slot.folder_id && filterHiddenSubtree.has(slot.folder_id),
        clear: slot => {
          // Un-hide the target's ancestor chain (persisted, mirroring
          // toggleFolderFilter). Cycle-guarded like filterHiddenSubtree.
          setFilterHiddenFolders(prev => {
            const next = new Set(prev)
            const visited = new Set<string>()
            let curId: string | undefined = slot.folder_id
            while (curId && !visited.has(curId)) {
              visited.add(curId)
              next.delete(curId)
              const cid = curId
              curId = folders.find(f => f.id === cid)?.parent_id
            }
            safeSetItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify([...next]))
            return next
          })
        },
      },
    ]
  }, [activeFilters, activeTagIds, filterTagIds, clearTagFilter, slotFilter, searchRanked, _derivedLookup, filterHiddenSubtree, folders])

  // State and in the memo deps on purpose, not a ref: a frozen run caches its
  // stale list against new deps, so clearing a ref would invalidate nothing.
  const [dragFrozen, setDragFrozen] = useState(false)
  const frozenSlotsRef = useRef<Slot[]>([])

  const filteredSlots = useMemo(() => {
    if (dragFrozen) return frozenSlotsRef.current
    const next = slots
      // Derived from filterDimensions — the single declaration above — so this
      // site cannot hold a filter dimension the other consumers miss.
      .filter(slot => filterDimensions.every(d => d.filtersRow === null || d.filtersRow(slot)))
      // Active content search: order by the backend's relevance ranking instead
      // of the sidebar sort (mirrors the Older Sessions lane and the command
      // palette). Pinning stays a reachability promise for browsing, not a
      // ranking hint inside explicit search results.
      .sort((a, b) => searchRanked
        ? (searchRanked.get(a.key) ?? Infinity) - (searchRanked.get(b.key) ?? Infinity)
        : comparePinnedThenSort(a, b, sortKey, pinned))
    frozenSlotsRef.current = next
    return next
  },
    [slots, filterDimensions, searchRanked, pinned, sortKey, dragFrozen]
  )

  // Which lane the sidebar is actually rendering. Mirrors the render branches
  // below exactly: flat wins when there are folders to flatten, otherwise the
  // tag-column board when columns exist, otherwise the folder tree. The folder
  // filter applies to the flat lane and the tree, NOT to the board.
  const flatLaneActive = flatView && folders.length > 0
  const boardLaneActive = !flatLaneActive && orderedColumns.length > 0

  // The folder filter goes inert while searching, in BOTH views: a query must
  // reach every match, so an unchecked folder can never become a search dead
  // end. Everything that consults the filter routes through this flag.
  const folderFilterActive = slotFilter.trim() === '' && filterHiddenFolders.size > 0

  // Is the list narrowed at all? Derived from filterDimensions: a dimension
  // participates through its required `narrows` field, so this site cannot
  // silently miss one (a missed dimension used to strand the folder lane's
  // folders as empty "New chat in <name>" shells).
  const listNarrowed = filterDimensions.some(d => d.narrows !== null && d.narrows())

  /** Every filter that can hide a reveal target, derived from
   *  `filterDimensions`: the reveal effect iterates this list instead of
   *  naming the dimensions by hand. Deliberately NOT `listNarrowed` above —
   *  that asks "is anything filtering?", this asks "does THIS row fail a
   *  filter?", and each dimension answers the two questions separately
   *  (`narrows` vs `hides`) in its one declaration. */
  const revealBlockingFilters = useMemo<RevealBlockingFilter[]>(() => {
    // Search and status defer to list membership: both rank against backend
    // state (relevance, unread) that a single row cannot answer for alone.
    const excluded = (slot: Slot) => !filteredSlots.some(s => s.key === slot.key)
    return filterDimensions.map(d => ({
      hides: (slot: Slot) => d.hides(slot, excluded),
      clear: d.clear,
    }))
  }, [filterDimensions, filteredSlots])

  // List view (the folder tree) drops an unchecked folder's whole block —
  // header and sessions together. Only the folder's OWN id is checked here:
  // removing a parent block already takes its descendants with it.
  const isFolderFilteredOut = useCallback(
    (f: ChatFolder) => folderFilterActive && filterHiddenFolders.has(f.id),
    [folderFilterActive, filterHiddenFolders],
  )

  // Which reveal rows are peeked open. Deliberately EPHEMERAL (not persisted):
  // a reveal is a "let me look" gesture, not a preference — the folder is still
  // hidden, and the durable way back is the row's ⋯ → Show folder. Keyed by
  // container: 'root' for the top level, 'flat' for the flat lane, else the
  // parent folder's id.
  const [revealedContainers, setRevealedContainers] = useState<Set<string>>(new Set())
  const toggleReveal = useCallback((key: string) => {
    setRevealedContainers(prev => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })
  }, [])
  // Collapse every peek the moment nothing is hidden any more, so a stale open
  // row can't linger after "Show all folders".
  useEffect(() => {
    if (!folderFilterActive) setRevealedContainers(prev => (prev.size === 0 ? prev : new Set()))
  }, [folderFilterActive])

  // Folders the filter is hiding, grouped by the container they would have
  // rendered in — 'root' for top-level, else the parent's id. A folder whose
  // ANCESTOR is hidden is deliberately absent: that whole block is already gone,
  // so its container is not on screen to host a row. That is what keeps the
  // announcement at exactly one level per hide.
  const hiddenByContainer = useMemo(() => {
    const m = new Map<string, ChatFolder[]>()
    if (!folderFilterActive) return m
    for (const f of folders) {
      if (isFolderHidden(f) || !filterHiddenFolders.has(f.id)) continue
      // An ancestor already hidden ⇒ this folder's container is not rendered.
      let cur = f.parent_id ? folders.find(p => p.id === f.parent_id) : undefined
      const seen = new Set<string>([f.id])
      let coveredByAncestor = false
      while (cur && !seen.has(cur.id)) {
        seen.add(cur.id)
        if (filterHiddenFolders.has(cur.id)) { coveredByAncestor = true; break }
        cur = cur.parent_id ? folders.find(p => p.id === cur!.parent_id) : undefined
      }
      if (coveredByAncestor) continue
      const key = f.parent_id || 'root'
      const list = m.get(key)
      if (list) list.push(f); else m.set(key, [f])
    }
    for (const list of m.values()) list.sort((a, b) => a.order - b.order)
    return m
  }, [folders, folderFilterActive, filterHiddenFolders, isFolderHidden])

  // Every folder the filter is hiding, flattened — the flat lane has no
  // containers to anchor to, so all hides collapse into its single row.
  const allHiddenFolders = useMemo(
    () => [...hiddenByContainer.values()].flat().sort((a, b) => a.order - b.order),
    [hiddenByContainer],
  )

  // Flat-view slot list: filteredSlots minus sessions in hidden folders —
  // EXCEPT while searching, where every match must stay reachable so a hidden
  // folder never becomes a search dead-end.
  const flatSlots = useMemo(() => {
    if (!folderFilterActive) return filteredSlots
    return filteredSlots.filter(s => {
      const fid = slotFolders[s.key]
      return !(fid && filterHiddenSubtree.has(fid))
    })
  }, [filteredSlots, folderFilterActive, filterHiddenSubtree, slotFolders])

  // The order the chat-jump/cycle shortcuts should follow — what this sidebar
  // displays. Flat view: the flat lane's exact row order. Folder tree and
  // column layouts: filteredSlots (pinned-first + the user's sort); folder
  // grouping can visually interleave rows, so those views rely on the held-
  // modifier badges below to make the digit mapping visible rather than on a
  // full tree walk here.
  const shortcutOrderKeys = useMemo(
    () => (flatView && folders.length > 0 ? flatSlots : filteredSlots).map(s => s.key),
    [flatView, folders.length, flatSlots, filteredSlots],
  )
  // Publish to the store for useKeyboardShortcuts (which reads at keypress
  // time). Diff-guarded so slot-detail churn that doesn't reorder rows never
  // dispatches. Deliberately not cleared on unmount: a last-known display
  // order beats falling back to backend insertion order while the sidebar is
  // collapsed.
  const lastPublishedOrderRef = useRef('')
  useEffect(() => {
    const joined = shortcutOrderKeys.join('\n')
    if (joined === lastPublishedOrderRef.current) return
    lastPublishedOrderRef.current = joined
    dispatch(setSidebarOrder(shortcutOrderKeys))
  }, [shortcutOrderKeys, dispatch])

  // First nine sessions in shortcut order → their digit, shown as row badges
  // while the jump modifier is held (Ctrl on Mac in Ctrl+digit mode, Alt
  // elsewhere — mirrors the Digit1..9 chords).
  const digitModifierHeld = useDigitModifierHeld()
  const shortcutDigitByKey = useMemo(() => {
    const m = new Map<string, number>()
    shortcutOrderKeys.slice(0, 9).forEach((k, i) => m.set(k, i + 1))
    return m
  }, [shortcutOrderKeys])

  // Folder rows for the filter menu: every folder in tree order, each with the
  // count of flat-lane sessions filed directly in it, and whether an unchecked
  // ancestor is already hiding it (that row renders inert).
  const folderFilterRows = useMemo(() => {
    const directCounts = new Map<string, number>()
    for (const s of filteredSlots) {
      const fid = slotFolders[s.key]
      if (fid) directCounts.set(fid, (directCounts.get(fid) ?? 0) + 1)
    }
    // Same roots + childrenOf walk the "New chat in folder" menu uses, with a
    // visited set so a parent_id cycle terminates instead of recursing forever.
    const byOrder = (a: ChatFolder, b: ChatFolder) => a.order - b.order
    const roots = folders.filter(f => !f.parent_id).sort(byOrder)
    const childrenOf = (pid: string) => folders.filter(f => f.parent_id === pid).sort(byOrder)
    const rows: { folder: ChatFolder; depth: number; count: number; hidden: boolean; hiddenByAncestor: boolean }[] = []
    const visited = new Set<string>()
    const walk = (list: ChatFolder[], depth: number) => {
      for (const f of list) {
        if (visited.has(f.id)) continue
        visited.add(f.id)
        rows.push({
          folder: f,
          depth,
          count: directCounts.get(f.id) ?? 0,
          hidden: filterHiddenFolders.has(f.id),
          hiddenByAncestor: !filterHiddenFolders.has(f.id) && filterHiddenSubtree.has(f.id),
        })
        walk(childrenOf(f.id), depth + 1)
      }
    }
    walk(roots, 0)
    // Orphans (parent_id pointing at a deleted folder, or inside a cycle) are
    // unreachable from the roots — append them so no folder is unlistable.
    for (const f of folders) {
      if (visited.has(f.id)) continue
      visited.add(f.id)
      rows.push({
        folder: f,
        depth: 0,
        count: directCounts.get(f.id) ?? 0,
        hidden: filterHiddenFolders.has(f.id),
        hiddenByAncestor: !filterHiddenFolders.has(f.id) && filterHiddenSubtree.has(f.id),
      })
    }
    return rows
  }, [folders, filteredSlots, slotFolders, filterHiddenFolders, filterHiddenSubtree])

  // Folder mutations
  const createFolderMutation = useMutation({
    mutationFn: (v: { name: string; parentId?: string; projectDir?: string; defaultAgent?: string; color?: string }) =>
      api.createChatFolder(v.name.trim(), v.parentId, {
        project_dir: v.projectDir || undefined,
        default_agent: v.defaultAgent || undefined,
        color: v.color || undefined,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const deleteFolderMutation = useMutation({
    mutationFn: (id: string) => api.deleteChatFolder(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const updateFolderMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: object }) => api.updateChatFolder(id, body),
    onMutate: async ({ id, body }) => {
      await queryClient.cancelQueries({ queryKey: ['chat-folders'] })
      const prev = queryClient.getQueryData<ChatFolder[]>(['chat-folders'])
      queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old => (old ?? []).map(f => f.id === id ? { ...f, ...body } : f))
      return { prev }
    },
    onError: (_err, _vars, ctx) => { if (ctx?.prev) queryClient.setQueryData(['chat-folders'], ctx.prev) },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const toggleCollapse = useCallback((id: string) => {
    const f = folders.find(x => x.id === id)
    if (f) updateFolderMutation.mutate({ id, body: { collapsed: !f.collapsed } })
  }, [folders, updateFolderMutation])

  // Board-view collapse is per (column, folder): the same root folders render
  // once per column, and the shared server flag would collapse a folder in
  // every column at once. Overrides are client-local (localStorage) and layer
  // over the server flag, which stays the default for untouched columns and
  // the sole state for the list view.
  const [boardCollapse, setBoardCollapse] = useState<Map<string, boolean>>(loadBoardFolderCollapse)
  const boardFolderCollapsed = useCallback((columnId: string, folder: ChatFolder): boolean => {
    return boardCollapse.get(boardCollapseKey(columnId, folder.id)) ?? !!folder.collapsed
  }, [boardCollapse])
  const toggleColumnCollapse = useCallback((columnId: string, folder: ChatFolder) => {
    setBoardCollapse(prev => {
      const next = new Map(prev)
      const value = !(prev.get(boardCollapseKey(columnId, folder.id)) ?? !!folder.collapsed)
      next.set(boardCollapseKey(columnId, folder.id), value)
      // Delta write: another tab's overrides must survive this tab's toggle.
      persistBoardOverride(columnId, folder.id, value)
      return next
    })
  }, [])

  // ── Folder drag-to-reorder ──
  // Mouse and touch are split on purpose — a single PointerSensor with a
  // distance constraint swallows touch swipes on WebKit: past the activation
  // distance dnd-kit preventDefault()s every move via its non-passive window
  // touchmove listener ("required for iOS Safari", TouchSensor.setup), so a
  // swipe that begins on a row cannot pan the list. Chromium ignores
  // preventDefault() on pointermove for panning, which is why it only shows on
  // WebKit. The TouchSensor's DELAY constraint inverts the contention: moving
  // past the tolerance CANCELS the sensor and hands the gesture back to the
  // browser; only a stationary 250ms hold arms a drag. Same split as the Apps
  // nav rail (App.tsx) and the artifact library.
  const dndSensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )
  // Tracks the item currently being dragged, for the DragOverlay preview.
  const [activeDrag, setActiveDrag] = useState<{ type: string; id: string } | null>(null)
  const reorderFolders = useCallback((activeId: string, overId: string) => {
    if (activeId === overId) return
    // Read latest from cache to avoid stale-closure ordering on rapid successive drags
    const current = queryClient.getQueryData<ChatFolder[]>(['chat-folders']) ?? []
    const rootOnly = current.filter(f => !f.parent_id)
    const changes = computeReorderedFolders(rootOnly, activeId, overId)
    if (!changes.length) return
    // Optimistic update
    queryClient.setQueryData<ChatFolder[]>(['chat-folders'], old =>
      (old ?? []).map(f => {
        const c = changes.find(ch => ch.id === f.id)
        return c ? { ...f, order: c.order } : f
      })
    )
    // Persist
    changes.forEach(c => api.updateChatFolder(c.id, { order: c.order }))
  }, [queryClient])
  // Re-parent a folder: move it into `parentId`, or to the top level (null).
  // Client-side guards mirror the server (self/descendant targets rejected)
  // so an invalid pick or drop is a silent no-op instead of a 400 round-trip.
  const moveFolderTo = useCallback((folderId: string, parentId: string | null) => {
    const current = queryClient.getQueryData<ChatFolder[]>(['chat-folders']) ?? []
    const folder = current.find(f => f.id === folderId)
    if (!folder) return
    const target = parentId ?? ''
    if ((folder.parent_id || '') === target) return
    if (target && collectFolderSubtreeIds(current, folderId).has(target)) return
    updateFolderMutation.mutate({ id: folderId, body: { parent_id: target } })
  }, [queryClient, updateFolderMutation])
  // Subtree sets for every folder, recomputed only when the folder list
  // changes — the render paths below (menu target filters + drag data)
  // do map lookups instead of re-walking the tree on every render pass.
  const folderSubtrees = useMemo(() => {
    const m = new Map<string, Set<string>>()
    for (const f of folders) m.set(f.id, collectFolderSubtreeIds(folders, f.id))
    return m
  }, [folders])

  // Reveal-in-sidebar: consume the pending request held in the store (set by
  // the session header menu). Store state rather than a window event on
  // purpose: this component is unmounted while the drawer is collapsed, so an
  // event dispatched before the mount commits had no listener and was silently
  // dropped — the request waiting here is picked up by this effect on mount as
  // well as on change (#912 D1). The nonce makes repeat reveals of the same
  // row distinct requests, so the effect re-fires even when the key repeats.
  const revealRequest = useAppSelector(s => s.chat.revealRequest)
  // Serial + pending timer for the in-flight reveal: a newer reveal cancels
  // the older retry loop, and unmount stops the pending timer outright.
  const revealRunRef = useRef<{ seq: number; timer: number | null }>({ seq: 0, timer: null })
  // Row currently flashing as reveal confirmation, keyed by slot. Rendered
  // into the row's className (not imperative classList mutation) so the
  // highlight survives row remounts — list reorders and re-keyed renders
  // would silently drop a manually-added DOM class.
  const [revealFlash, setRevealFlash] = useState<{ key: string; fading: boolean } | null>(null)
  const revealFlashTimersRef = useRef<number[]>([])
  useEffect(() => () => {
    const run = revealRunRef.current
    if (run.timer != null) clearTimeout(run.timer)
    revealFlashTimersRef.current.forEach(clearTimeout)
  }, [])
  const sidebarRootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!revealRequest) return
    const { key } = revealRequest
    // Consume immediately: the request must not survive to a later remount.
    dispatch(clearSlotReveal())
    const slot = slots.find(s => s.key === key)
    if (!slot) {
      // Stale key or a session outside this surface's slot list. Not user-visible
      // (there is nothing to highlight), so leave a trace for bug reports.
      console.debug('reveal-in-sidebar: no session for key', key)
      return
    }
    // Reveal means "show me this row", so drop every filter hiding the target
    // rather than scrolling to nothing (#912 D5). Registered in one list above.
    for (const dim of revealBlockingFilters) if (dim.hides(slot)) dim.clear(slot)
    if (slot.folder_id) {
      // Expand all collapsed ancestor folders. Cycle-guarded: a hand-edited
      // folders.json can contain a parent_id loop and must not hang the tab.
      const visited = new Set<string>()
      const expand = (fid: string) => {
        if (visited.has(fid)) return
        visited.add(fid)
        const f = folders.find(x => x.id === fid)
        if (f?.collapsed) updateFolderMutation.mutate({ id: fid, body: { collapsed: false } })
        // A reveal must win in every board column too: drop any per-column
        // collapsed overrides, or the revealed row stays hidden in columns
        // whose local state keeps this ancestor collapsed.
        setBoardCollapse(prev => clearFolderOverrides(prev, fid))
        persistClearFolderOverrides(fid)
        if (f?.parent_id) expand(f.parent_id)
      }
      expand(slot.folder_id)
    }
    // The row may not be in the DOM yet: ancestor expansion and the filter
    // resets above land through mutations and re-renders. Retry until the row
    // exists (bounded), instead of one fixed-delay attempt that silently gives
    // up whenever the re-render loses the race (#912 D3).
    const run = revealRunRef.current
    run.seq += 1
    const seq = run.seq
    if (run.timer != null) { clearTimeout(run.timer); run.timer = null }
    let attempt = 0
    const tryScroll = () => {
      if (revealRunRef.current.seq !== seq) return
      // Scoped to this sidebar, not `document`: other surfaces (and board-view
      // duplicate renders) can carry the same data-slot-key (#912 D5).
      const el = sidebarRootRef.current?.querySelector<HTMLElement>(`[data-slot-key="${window.CSS.escape(key)}"]`)
      if (!el) {
        attempt += 1
        if (attempt <= REVEAL_MAX_ATTEMPTS) run.timer = window.setTimeout(tryScroll, REVEAL_RETRY_MS)
        // Row never appeared (e.g. board lane with no matching column). Not
        // user-visible, so leave a trace for bug reports instead of vanishing.
        else console.debug('reveal-in-sidebar: row never rendered for', key)
        return
      }
      const reduce = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' })
      // Visible confirmation even when the row never moved (#912 D4): an
      // accent outline that fades out (classes in index.css, rendered via
      // revealFlash state). Outline, not background — the target is usually
      // the ACTIVE row, which already carries the accent-subtle background —
      // and not box-shadow, which the recency tint drives inline. The fade is
      // a non-spatial color transition, so it needs no reduced-motion branch
      // (same treatment as MarkdownPanel's flashCommentRow); the scroll above
      // handles the spatial half. A newer flash replaces the older one
      // immediately, so two rows are never highlighted at once.
      revealFlashTimersRef.current.forEach(clearTimeout)
      setRevealFlash({ key, fading: false })
      const t1 = window.setTimeout(() => setRevealFlash(f => (f && f.key === key ? { key, fading: true } : f)), REVEAL_FLASH_HOLD_MS)
      const t2 = window.setTimeout(() => setRevealFlash(f => (f && f.key === key ? null : f)), REVEAL_FLASH_HOLD_MS + REVEAL_FLASH_FADE_MS)
      revealFlashTimersRef.current = [t1, t2]
    }
    tryScroll()
  }, [revealRequest, dispatch, slots, folders, revealBlockingFilters, updateFolderMutation])
  const renameCommit = useCallback((id: string, name: string) => {
    if (name.trim()) updateFolderMutation.mutate({ id, body: { name: name.trim() } })
    setEditingId(null)
  }, [updateFolderMutation])
  // Shared optimistic move (also used by the session-header dropdown and
  // drag-to-folder) — single source of truth for slot→folder assignment. Both
  // the menu "Move to folder" submenus and drag-to-folder route through this.
  const assignToFolder = useMoveSlotToFolder()
  // Surface-agnostic session actions (duplicate/read/pin/copy/move/close) shared
  // by all three row menus AND the row's non-menu buttons (Duplicate/Close) so
  // each behaviour has one definition. Rename + Tags stay local (they drive this
  // component's inline-edit + tag-popover state).
  const sessionActions = useSessionActions(mode)
  // Which sessions are currently open in a popped-out window (shared singleton).
  const { poppedOut } = useChatPopouts()
  // Unified dnd-kit handlers for the legacy single-lane layout. One DndContext
  // owns both folder reordering (sortable) and session drag-to-assign
  // (draggable rows + droppable folder/root targets); the active item's
  // data.type routes the drop.
  const handleSidebarDragStart = useCallback((e: DragStartEvent) => {
    setDragFrozen(true)
    const d = e.active.data.current as { type?: string; key?: string } | undefined
    if (d?.type === 'session' && d.key) setActiveDrag({ type: 'session', id: d.key })
    else if (d?.type === 'folder') setActiveDrag({ type: 'folder', id: e.active.id as string })
  }, [])
  const handleSidebarDragEnd = useCallback((event: DragEndEvent) => {
    setActiveDrag(null)
    setDragFrozen(false)
    if (dragExpandTimer.current) { clearTimeout(dragExpandTimer.current.timer); dragExpandTimer.current = null }
    const { active, over } = event
    if (!over) return
    const a = active.data.current as { type?: string; key?: string; nested?: boolean } | undefined
    const o = over.data.current as { type?: string; folderId?: string | null } | undefined
    if (a?.type === 'folder') {
      if (a.nested) {
        // Nested subfolder drag = re-parent: into the folder-drop target, or
        // to the top level when dropped on the root lane (folderId null).
        if (o?.type === 'folder-drop') moveFolderTo(active.id as string, o.folderId ?? null)
        return
      }
      // Root folder drag: a folder-drop hit only occurs via the header-band
      // gesture in sidebarCollision = re-parent INTO that folder. A sortable
      // hit (over.id = folder id) is the reorder-among-siblings gesture.
      if (o?.type === 'folder-drop') {
        if (o.folderId) moveFolderTo(active.id as string, o.folderId)
        return
      }
      reorderFolders(active.id as string, over.id as string)
      return
    }
    if (a?.type === 'session' && a.key) {
      // Drop targets, innermost-first via pointerWithin:
      //  chat-pane-ref → stage a LINK to this session in the open chat's composer
      //  folder-drop  → assign to that folder (folderId may be null for root lane)
      //  folder       → sortable folder container (whole block) → assign to its id
      if (o?.type === CHAT_PANE_DROP_TYPE) {
        const src = slots.find(x => x.key === a.key)
        // Re-decide at drop time rather than trusting the drag-start snapshot:
        // the refusal must not depend on the affordance having been rendered,
        // and memory_mode can change mid-drag. Same function the zone uses.
        if (sessionRefBlockReason({ key: a.key, activeSlot, memoryMode: src?.memory_mode })) return
        onDropSessionRef?.({
          key: a.key,
          title: src?.title && src.title !== src.key ? src.title : a.key,
          messages: src?.messages,
        })
        return
      }
      if (o?.type === 'folder-drop') assignToFolder(a.key, o.folderId ?? null)
      else if (o?.type === 'folder') assignToFolder(a.key, over.id as string)
    }
  }, [reorderFolders, assignToFolder, moveFolderTo, slots, activeSlot, onDropSessionRef])
  const handleSidebarDragCancel = useCallback(() => { setActiveDrag(null); setDragFrozen(false); if (dragExpandTimer.current) { clearTimeout(dragExpandTimer.current.timer); dragExpandTimer.current = null } }, [])
  // Auto-expand collapsed folders when a dragged item hovers over them for 500ms.
  const dragExpandTimer = useRef<{ id: string; timer: ReturnType<typeof setTimeout> } | null>(null)
  const handleSidebarDragOver = useCallback((event: DragOverEvent) => {
    const over = event.over
    const overData = over?.data.current as { type?: string; folderId?: string | null } | undefined
    const targetFolderId = overData?.type === 'folder-drop' ? overData.folderId : null
    // If hovering a collapsed folder, blink ring twice then expand. In a board
    // column, "collapsed" is that column's effective state (server flag +
    // column override), and the expansion must clear the column's override —
    // the server flag alone can read expanded while the hovered copy is
    // collapsed by its override, which would leave the drop target shut.
    if (targetFolderId) {
      const overColumnId = over ? boardColumnFromDroppableId(String(over.id)) : null
      const f = folders.find(x => x.id === targetFolderId)
      const effectiveCollapsed = f ? (overColumnId ? boardFolderCollapsed(overColumnId, f) : !!f.collapsed) : false
      const expandTarget = () => {
        if (f?.collapsed) updateFolderMutation.mutate({ id: targetFolderId, body: { collapsed: false } })
        if (overColumnId) {
          setBoardCollapse(prev => clearFolderOverrides(prev, targetFolderId, overColumnId))
          persistClearFolderOverrides(targetFolderId, overColumnId)
        }
      }
      if (effectiveCollapsed) {
        if (dragExpandTimer.current?.id !== targetFolderId) {
          if (dragExpandTimer.current) clearTimeout(dragExpandTimer.current.timer)
          dragExpandTimer.current = {
            id: targetFolderId,
            timer: setTimeout(() => {
              // Blink the folder ring twice before expanding
              const el = document.querySelector(`[data-folder-drop="${targetFolderId}"]`) as HTMLElement | null
              if (el) {
                const ring = 'inset 0 0 0 2px var(--accent)'
                const dim = () => { el.style.boxShadow = ring; el.style.opacity = '0.4' }
                const bright = () => { el.style.boxShadow = ring; el.style.opacity = '1' }
                bright(); setTimeout(dim, 100); setTimeout(bright, 200); setTimeout(dim, 300)
                setTimeout(() => {
                  el.style.boxShadow = ''; el.style.opacity = ''
                  expandTarget()
                  dragExpandTimer.current = null
                }, 450)
              } else {
                expandTarget()
                dragExpandTimer.current = null
              }
            }, 500),
          }
        }
        return
      }
    }
    // Moved away from the folder or it's already expanded — clear timer
    if (dragExpandTimer.current) {
      clearTimeout(dragExpandTimer.current.timer)
      dragExpandTimer.current = null
    }
  }, [folders, updateFolderMutation, boardFolderCollapsed])
  const createChatInFolderMutation = useMutation({
    mutationFn: ({ folderId }: { folderId: string; columnId?: string; focus?: boolean }) => {
      const agent = resolveFolderAgent(folders, folderId, defaultAgent)
      const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
      // Carry folder membership in the create payload so createSlot publishes
      // the new slot to Redux in its final location. Assigning it after create
      // lets the sidebar render one frame at root before moving it.
      //
      // Folder linked to a project directory (directly or via an ancestor):
      // carry it in the create payload so the slot starts on the linked
      // project — createSlot applies it before the slot activates, so the
      // first message can't race a late project switch.
      const project = resolveFolderProjectDir(folders, folderId)
      return dispatch(createSlot({ agent, mode: effectiveMode, folder_id: folderId, project })).unwrap()
    },
    onSuccess: (slot: Slot, { columnId, focus }: { folderId: string; columnId?: string; focus?: boolean }) => {
      // Focus only after the create fulfils: the composer is bound to the
      // active slot, so focusing while createSlot is still in flight puts the
      // caret on the OLD session and anything typed lands in its draft.
      if (focus) focusComposer()
      if (slot?.key && columnId) {
        // Board view: also drop the new session into the column it was created
        // from, so a status-lane column shows it immediately instead of the
        // untagged session vanishing from a tag-filtered column. Mirrors a
        // drag-drop and is a harmless no-op for filter-only / non-status columns.
        dropSlotMutation.mutate({ slot: slot.key, columnId })
      }
    },
    onError: (err: unknown) => {
      // eslint-disable-next-line no-console -- surface chat-creation failures for diagnostics
      console.error('Failed to create chat in folder:', err)
    },
  })
  const createChatInFolder = useCallback((folderId: string, opts?: { columnId?: string; focus?: boolean }) => {
    // A nested folder selected from the create menu may be hidden behind one
    // or more collapsed ancestors. Expand the complete path optimistically so
    // the destination and its new session are visible as creation begins.
    const visited = new Set<string>()
    let currentId: string | undefined = folderId
    while (currentId && !visited.has(currentId)) {
      visited.add(currentId)
      const folder = folders.find(f => f.id === currentId)
      if (!folder) break
      if (folder.collapsed) updateFolderMutation.mutate({ id: folder.id, body: { collapsed: false } })
      // Board columns keep their own collapse overrides; drop them for the
      // whole ancestor path so the destination is visible in the clicked
      // column (and every other) as creation begins.
      setBoardCollapse(prev => clearFolderOverrides(prev, folder.id))
      persistClearFolderOverrides(folder.id)
      currentId = folder.parent_id || undefined
    }
    createChatInFolderMutation.mutate({ folderId, columnId: opts?.columnId, focus: opts?.focus })
  }, [createChatInFolderMutation, folders, updateFolderMutation])

  // Create autopilot session mutation (consistent with useMutation pattern)
  const createAutopilotMutation = useMutation({
    mutationFn: () => {
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: 'orchestrator' })).unwrap()
    },
    onSuccess: focusComposer,
  })

  // Crew Mode: multi-topic chat — the agent runs only in sub-sessions
  // (topics); the session itself is an engineered routing pipeline.
  const createCrewMutation = useMutation({
    mutationFn: () => {
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: 'crew' })).unwrap()
    },
    onSuccess: focusComposer,
  })

  // Create default chat session mutation
  const createChatMutation = useMutation({
    mutationFn: () => {
      const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: effectiveMode })).unwrap()
    },
    onSuccess: focusComposer,
  })

  // Create a PLAIN chat, ignoring the `defaultAutopilot` preference.
  // The caret menu lists "New chat" and "New autopilot chat" side by side, so
  // each must name exactly what it makes. Routing the plain entry through
  // createChatMutation would hand an autopilot session to anyone who turned the
  // default on — the one case where they picked the non-default on purpose.
  // The button's main segment keeps honouring the preference; only this explicit
  // entry pins the mode.
  const createPlainChatMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ agent: defaultAgent || undefined, mode: mode || '' })).unwrap(),
    onSuccess: focusComposer,
  })

  // Session colors
  const { paletteColors, boost, boostFor, colorMode } = useSessionPalette()

  // ── Session row (reference-style: color palette, memory_mode, rename on right-click) ──
  // Does any descendant (direct or nested) of `folderId` contain a slot from `slots`?
  function descendantMatch(fs: ChatFolder[], folderId: string, slots: Slot[], slotFolderMap: Record<string, string>, visited = new Set<string>()): boolean {
    if (visited.has(folderId)) return false // cycle guard
    visited.add(folderId)
    for (const child of fs) {
      if (child.parent_id !== folderId) continue
      if (slots.some(s => slotFolderMap[s.key] === child.id)) return true
      if (descendantMatch(fs, child.id, slots, slotFolderMap, visited)) return true
    }
    return false
  }

  // Render a folder block scoped to a single column: only slots matching the column predicate.
  // Always render the folder header (even with 0 matches) so users can see + drop into it.
  const renderColumnFolder = (folder: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean): React.ReactNode => {
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => colSlotKeys.has(s.key) && slotFolders[s.key] === folder.id)
    const deepChildren = childFolders
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    const count = childSlots.length + deepChildren.filter(cf => {
      const cfSlots = filteredSlots.filter(s => colSlotKeys.has(s.key) && slotFolders[s.key] === cf.id)
      return cfSlots.length > 0 || descendantMatch(folders, cf.id, filteredSlots.filter(s => colSlotKeys.has(s.key)), slotFolders)
    }).length
    // Board-view folders become sortable only when a drag handle is supplied
    // (root folders wrapped in SortableColumnFolder). Subfolders render without
    // it (parity with list view, where only root folders reorder). Disabled
    // while renaming in THIS column (rename is per-column via editScope) so
    // the inline input stays usable.
    const draggable = !!dragHandleProps && !(editingId === folder.id && editScope === columnId)
    return (
      // Two drop mechanisms coexist on this block, one per drag SOURCE:
      //  • Native HTML5 onDrop (below) — SESSION cards drag natively (they set
      //    dataTransfer text/plain), so a session dropped here is assigned to
      //    this folder via assignToFolder.
      //  • dnd-kit DndDroppable (this wrapper) — FOLDERS drag via the pointer
      //    sensor (SortableColumnFolder), never via native DnD, so their active
      //    data lives in active.data.current, unreadable by onDrop. The
      //    folder-drop droppable is what lets handleSidebarDragEnd re-parent a
      //    folder dropped here (moveFolderTo). The two never collide: a native
      //    drag never fires dnd-kit's onDragEnd and a dnd-kit drag never fires
      //    the DOM drop event. Id is column-scoped because a root folder renders
      //    once per board column and dnd-kit droppable ids must be unique.
      <DndDroppable key={`col-${columnId}-folder-drop-${folder.id}`} id={`col-${columnId}-folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
      // The drag handlers below make this a mouse-only drop target with no
      // keyboard analogue, so scope-disable the static-interaction rule.
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions
      <div ref={setNodeRef}
        data-testid={`col-${columnId}-folder-${folder.id}`}
        data-folder-drop={folder.id}
        className={`rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}`}
        onDragOver={e => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.add('ring-1', 'ring-accent') }}
        onDragLeave={e => { e.stopPropagation(); e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
        onDrop={e => {
          e.preventDefault(); e.stopPropagation()
          e.currentTarget.classList.remove('ring-1', 'ring-accent')
          const k = e.dataTransfer.getData('text/plain')
          if (k) assignToFolder(k, folder.id)
        }}
      >
        <div
          className={`group relative flex items-center gap-2 pr-2 py-1 rounded-md ${draggable ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer'} text-[12px] text-muted hover:text-text hover:bg-bg-hover transition-all`}
          style={{ paddingLeft: '6px' }}
          role="button"
          tabIndex={0}
          aria-expanded={!boardFolderCollapsed(columnId, folder)}
          aria-label={boardFolderCollapsed(columnId, folder) ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name })}
          {...(draggable ? dragHandleProps : {})}
          onClick={() => toggleColumnCollapse(columnId, folder)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleColumnCollapse(columnId, folder) } }}
        >
          <FolderGlyph color={folder.color} size={11} open={!boardFolderCollapsed(columnId, folder)} />
          {editingId === folder.id && editScope === columnId ? (
            /* Inline rename input — board-view parity with renderFolderHeader.
             *  Without this branch the ⋯-menu "Rename" set editingId but no
             *  field ever appeared, so rename silently did nothing here. The
             *  collapse handler is on the OUTER div, so the input's onClick +
             *  onMouseDown stopPropagation are load-bearing (they keep typing/
             *  clicking the field from bubbling to toggleCollapse). */
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[12px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
          ) : (
            // Double-click rename is a mouse-only power shortcut; the accessible
            // path is the ⋯-menu Rename item, so scope-disable the interaction rule.
            // eslint-disable-next-line jsx-a11y/no-static-element-interactions
            <span className="flex-1 truncate" title={i18nT('pages.chatSidebar.double_click_to_rename')} onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}>{folder.name}</span>
          )}
          <span className="text-[10px] text-muted shrink-0">{count}</span>
          {!(editingId === folder.id && editScope === columnId) && (
          <span className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 has-[[data-state=open]]:opacity-100 transition-opacity flex items-center gap-0.5">
            {/* ⋯ menu + a primary "new chat in folder" action, mirroring the
             *  list-view folder header (renderFolderHeader) so board view has
             *  the same one-click way to start a session inside a folder. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-menu`} className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" onMouseDown={e => { e.stopPropagation() }} onClick={e => { e.stopPropagation() }} onKeyDown={e => { e.stopPropagation() }}>
                  <MoreVertical size={11} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
                <DropdownMenuItem onClick={() => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</DropdownMenuItem>
                <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-new-sub`} onClick={() => { setFolderModal({ mode: 'create', parentId: folder.id }) }}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</DropdownMenuItem>
                {/* Re-parent: board-view parity with the list-view folder menu. */}
                <FolderMoveSubmenu variant="dropdown" label={i18nT('pages.chatSidebar.move_folder_to')}
                  folders={reparentTargets}
                  currentFolderId={folder.parent_id || null}
                  onPick={pid => moveFolderTo(folder.id, pid)} />
                <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-settings`} onClick={() => { setFolderModal({ mode: 'edit', folderId: folder.id }) }}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem className="text-danger focus:text-danger" onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-chat`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { columnId }) }} onMouseDown={e => { e.stopPropagation() }} onKeyDown={e => { e.stopPropagation() }}>
              <MessageSquarePlus size={11} />
            </button>
          </span>
          )}
        </div>
        <FolderBody open={!boardFolderCollapsed(columnId, folder) && !forceCollapsed}>
          {/* ml-4 + no pl: flush-connector treatment matching the list-view
           *  folder body (renderFolderBlock) so nested rows sit identically
           *  against the connector line in both views. */}
          <div className="border-l border-border ml-4">
            {/* Empty-folder affordance — list-view parity (see renderFolderBlock). */}
            {deepChildren.length === 0 && childSlots.length === 0 && (
              <button key={`col-${columnId}-newchat-${folder.id}`} type="button"
                onClick={() => createChatInFolder(folder.id, { columnId })}
                title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
                className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-[11px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
                <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={11} className="shrink-0 ml-auto" />
              </button>
            )}
            {deepChildren.map(cf => renderColumnFolder(cf, columnId, colSlotKeys))}
            {childSlots.map((s, i) => {
              const isActive = activeSlot === s.key
              const nextIsActive = i < childSlots.length - 1 && activeSlot === childSlots[i + 1].key
              const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
              // `scope` stays per-folder so the Framer layoutId and the inline
              // rename target remain unique, but the arrow rove is scoped to the
              // COLUMN: a board column's foldered and ungrouped rows are one
              // visible list, so ArrowDown has to cross the folder boundary.
              return renderSessionRow(s, 1, showDivider, `${columnId}:${folder.id}`, columnId)
            })}
          </div>
        </FolderBody>
      </div>
        )}
      </DndDroppable>
    )
  }

  // scope namespaces the Framer layoutId per render location. A multi-tag slot
  // can render in several columns at once; same layoutId in one LayoutGroup
  // collides (Framer paints one, hides the rest). Distinct scope = distinct id.
  const renderSessionRow = (s: Slot, _indent: number, showDivider: boolean, scope = 'list', navScope = scope) => {
    // Flat view shares the tree's layoutId namespace so Framer Motion treats a
    // row as the SAME element across the view toggle and animates it from its
    // tree position into the flat lane (and back). Safe: the two views are
    // ternary branches — never mounted simultaneously — so IDs can't collide.
    // Behavior stays keyed on the real scope.
    const layoutScope = scope === 'flat' ? 'list' : scope
    // dnd-kit pickup, in the tree and in the flat lane. Flat view's own
    // DndContext registers no folder or sortable targets, so the gesture there
    // can only reach the chat pane: dragging a session into the open chat
    // works, while manual reordering stays unavailable by construction.
    // Board columns keep the separate native-HTML5 drag (their own scope).
    const dndRow = scope === 'list' || scope === 'flat'
    const agentName = s.agent || defaultAgent || ''
    const agentMeta = installedAgents.find(a => a.name === agentName)
    const isPackageAgent = agentMeta?.source === 'package'
    const isBuiltin = agentMeta?.source === 'builtin'
    const agentColor = isPackageAgent ? 'text-[var(--aim)]' : isBuiltin ? 'text-muted' : 'text-muted'
    // The meta line's second slot shows the session's TAGS, not a value derived
    // from the project path. The auto-tagger already labels each session with its
    // project, so those tags ARE the context the row needs; deriving a label
    // would just print the same word again. ALL tags render, each as tinted plain
    // text after a "·", in tag `order` so the sequence is stable.
    const resolvedSlotTags = (s.tags ?? [])
      .map(tid => tagById[tid])
      .filter((t): t is ChatTag => !!t)
      .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    const isActive = activeSlot === s.key
    const isOut = poppedOut.has(s.key)
    const recent = recentRank.get(s.key)
    const subagentCount = subagentCounts[s.key] || 0
    // Sub-agents held at the spawn gate. Excluded from the running/queued
    // arithmetic below: "4 agents running" while 2 of them are blocked on your
    // click is both wrong and the reason the owed approval went unnoticed.
    const subagentAwaiting = Math.min(subagentApprovalCounts[s.key] || 0, subagentCount)
    const subagentActive = subagentCount - subagentAwaiting
    // Distinguish started from queued: "3 agents running" is wrong for a wave
    // that is still entirely behind the concurrency cap.
    const subagentQueuedCount = Math.min(subagentQueued?.[s.key] || 0, subagentActive)
    const subagentStarted = subagentActive - subagentQueuedCount
    const subagentLabel = subagentStarted === 0
      ? `${subagentQueuedCount} agent${subagentQueuedCount === 1 ? '' : 's'} queued`
      : subagentQueuedCount > 0
        ? i18nT('pages.chatSidebar.running_queued', { started: subagentStarted, queued: subagentQueuedCount })
        : `${subagentStarted} agent${subagentStarted === 1 ? '' : 's'} running`
    // Plain literal, like the running/queued label above it: `en.json` is
    // codemod-generated and carries no interpolated values, so a counted
    // string belongs in code until both sibling labels are localized together.
    const subagentApprovalLabel = subagentAwaiting === 1
      ? '1 sub-agent needs approval'
      : `${subagentAwaiting} sub-agents need approval`
    const wfActive = workflowActive[s.key]
    // The agent's own ask: a question card the user has not answered yet. The
    // turn is parked on it, so this replaces a "Thinking…" that would otherwise
    // never change rather than annotating a finished turn.
    const needsInputLabel = i18nT('pages.chatSidebar.needs_your_answer')
    // Goal loop (auto-nudge). A loop is a MODE, not a turn state, so it is not
    // gated on `s.running` — a looping session spends most of its life mid-turn,
    // and hiding the indicator then would hide it almost always.
    // Own-property read only. The store normalizes writes through `safeKey`
    // (`__proto__`/`constructor`/`prototype` are rerouted to an inert key), so a
    // bare `goalLoops[s.key]` would disagree with it — returning a truthy
    // `Object.prototype` for such a key and rendering "Loop · undefined" while
    // suppressing the row's unread dot.
    const goalLoop = Object.prototype.hasOwnProperty.call(goalLoops ?? {}, s.key)
      ? goalLoops[s.key]
      : undefined
    // `max_cycles === 0` means unlimited (autonudge.py NudgeLoop default), so
    // there is no denominator to show — fall back to a bare count.
    const goalLoopLabel = !goalLoop
      ? ''
      : goalLoop.max_cycles > 0
        ? i18nT('pages.chatSidebar.loop', { count: goalLoop.cycle_count, total: goalLoop.max_cycles })
        : i18nT('pages.chatSidebar.loop_2', { count: goalLoop.cycle_count })
    // The loop is armed but its session's last turn died — a trailing error row
    // or an unanswered user row, the state behind the composer's Resume button —
    // and nothing is executing on its behalf. The pulsing dot below would claim
    // active work for the whole gap until the user resumes or the next
    // idle-timer cycle fires (up to idle_secs away), so this renders as a static
    // warn dot with an explicit "interrupted" instead. Guarded on the raw turn
    // flag plus workflow/subagent activity: while any of those run, the loop IS
    // working and `s.interrupted` only describes a superseded turn.
    const goalLoopStalled = !!goalLoop && !!s.interrupted && !s.running && !wfActive && subagentCount === 0
    // Whatever this row would have said if no loop were running, reused as the
    // loop line's trailing detail. This is why the loop branch can outrank the
    // working signals below without swallowing them: live workflow/subagent/tool
    // status still shows, and between cycles it falls back to the last message.
    // Reads the RAW `s.running`, not `runningSet`: the widened flag includes this
    // very loop, and an idle-between-cycles row must show its last message.
    const goalLoopDetail = wfActive
      ? wfActive.label
      : subagentCount > 0
        ? subagentLabel
        : s.running
          ? slotStatusText(slotStatusDetail[s.key], simplifiedToolNames, uiLang)
          : (s.last_message || '')
    const ci = s.color_index != null && s.color_index >= 0 && s.color_index < paletteColors.length ? s.color_index : null
    // The row's ONE status marker and the words beside it, resolved together: the
    // glyph is built INSIDE the branch's `subtitle`, immediately in front of the
    // label that names it, so a branch cannot ship a glyph without its phrase or a
    // phrase without its glyph.
    //
    // ── One ordered state resolver (#3830) ────────────────────────────────
    //
    // The marker and the subtitle line encode the SAME precedence. They used to
    // be two independent ternary chains a few hundred lines apart, with comments
    // asserting they "can never disagree" and nothing enforcing it: editing a
    // branch in one silently desynchronised the glyph from the subtitle. They are
    // now ONE node per branch, so the ordering exists once and a new state is
    // added in one place.
    //
    // Order is the contract. Owed decisions outrank every "working" signal —
    // a blocking card keeps `s.running` true, so without that ranking the row
    // would read "Thinking…" while nothing can advance until the user acts.
    //
    // `when` is a plain boolean, evaluated in order; the first truthy entry
    // wins. Everything else is behind `build()` and is called ONLY for that
    // winner. That laziness is load-bearing, not a style choice: the chain runs
    // for every row, and `slotStatusDetail` is only meaningful for a running
    // one — eagerly resolving the running label threw on rows where it is
    // absent. The ternary chain this replaces got the same property for free by
    // being a ternary; here it has to be explicit.
    //
    // The tail is `last_message`, and the `unread` dot rides on it (below).
    const rowState = ([
      {
        // Pending approval outranks running (mirrors the Board's inferLane,
        // which returns its approval lane before the running check), so an owed
        // approval is never hidden behind a "Thinking…" spinner.
        key: 'pending_approval',
        when: !!s.pending_approval,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS}>
            <ShieldCheck size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
            <span className="truncate"><span className="font-medium" style={{ color: 'var(--warn)' }}>{i18nT('pages.chatSidebar.needs_approval')}</span>{s.last_message ? <span className="text-muted"> · {s.last_message}</span> : null}</span>
          </div>
        ),
      },
      {
        // Sub-agents blocked on a spawn approval. Directly below the slot's own
        // pending approval and above every "working" signal, for the same
        // reason: an owed decision must not read as work in progress. The bot
        // glyph is static, not pulsing — nothing is running — and warn-coloured
        // to match the row above.
        key: 'subagent_awaiting',
        when: subagentAwaiting > 0,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={subagentApprovalLabel}>
            <Bot size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
            <span className="truncate font-medium" style={{ color: 'var(--warn)' }}>{subagentApprovalLabel}</span>
          </div>
        ),
      },
      {
        // An unanswered question card. Above every "working" signal for the
        // same reason as the approval branches — and a blocking card keeps
        // `s.running` true, so without this the row would show "Thinking…"
        // while nothing can advance. Info-coloured and static-glyphed to stay
        // distinct from the warn-coloured approval rows above.
        //
        // A card is a websocket broadcast with no transcript row, so
        // `last_message` is whatever the agent last said BEFORE the ask — not
        // the question. Trailing it after "Needs your answer ·" would read as
        // the question itself, so the label stands alone.
        key: 'needs_input',
        when: !!s.needs_input,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={needsInputLabel}>
            <MessageCircleQuestionMark size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--info)' }} aria-hidden />
            <span className="truncate font-medium" style={{ color: 'var(--info)' }}>{needsInputLabel}</span>
          </div>
        ),
      },
      {
        // An active goal loop outranks every "working" signal below it but
        // stays under both approval branches: an owed decision must never read
        // as unattended progress. Nothing is lost by ranking it high —
        // `goalLoopDetail` carries whatever the lower branch would have shown,
        // so this reads "Loop 7/24 · 3 agents running". Stalled (see
        // `goalLoopStalled`): warn + "interrupted" rather than accent.
        key: 'goal_loop',
        when: !!goalLoop,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={goalLoopStalled ? i18nT('pages.chatSidebar.goal_loop_interrupted_title') : goalLoop && goalLoop.max_cycles > 0 ? i18nT('pages.chatSidebar.goal_loop_cycle', { count: goalLoop.cycle_count, total: goalLoop.max_cycles }) : i18nT('pages.chatSidebar.goal_loop_cycle_no_cap', { count: goalLoop?.cycle_count ?? 0 })}>
            <Goal size={ROW_ICON_PX} className={`shrink-0 ${goalLoopStalled ? 'text-warn' : 'text-accent animate-pulse'}`} aria-hidden />
            <span className="truncate"><span className={`font-medium ${goalLoopStalled ? 'text-warn' : 'text-accent'}`}>{goalLoopLabel}{goalLoopStalled ? ` — ${i18nT('pages.chatSidebar.loop_interrupted')}` : ''}</span>{goalLoopDetail ? <span className="text-muted"> · {goalLoopDetail}</span> : null}</span>
          </div>
        ),
      },
      {
        // A dynamic-workflow run launched from this session is still executing
        // — surface it even though the parent turn has ended (`s.running` is
        // false while the run executes in the background). Outranks the
        // subagent count: workflow track agents may also register as
        // subagents, and "which workflow / phase" is the stronger signal.
        key: 'workflow',
        when: !!wfActive,
        build: () => (
          <div className={ROW_STATUS_LINE_ACCENT_CLS} title={`${wfActive?.count ?? 0} workflow${(wfActive?.count ?? 0) > 1 ? 's' : ''} running`}>
            <Workflow size={ROW_ICON_PX} className="shrink-0 text-accent animate-pulse" aria-hidden />
            <span className="truncate">{wfActive?.label}</span>
          </div>
        ),
      },
      {
        // A spawned subagent is still running (or queued behind the concurrency
        // cap) — surface it even if the parent turn has ended (`s.running` is
        // false while it waits for completion events), so the sidebar shows
        // live activity instead of a stale last message.
        key: 'subagents',
        when: subagentCount > 0,
        build: () => (
          <div className={ROW_STATUS_LINE_ACCENT_CLS} title={subagentLabel}>
            <Bot size={ROW_ICON_PX} className="shrink-0 text-accent animate-pulse" aria-hidden />
            <span className="truncate">{subagentLabel}</span>
          </div>
        ),
      },
      {
        // A spinner, not a pulsing dot: "actively working" is the one state
        // with a definite direction, and rotation reads as progress where a
        // fading dot reads as a mere marker.
        key: 'running',
        when: runningSet.has(s.key),
        build: () => {
          const text = slotStatusText(slotStatusDetail[s.key], simplifiedToolNames, uiLang)
          // `title` because this is the one status text that is unbounded — a tool
          // phase can name a long command — and the line truncates. The gutter
          // glyph used to carry that tooltip, so it has to move with it, or a
          // truncated tool status becomes unreadable rather than abbreviated.
          return (
            <div className={ROW_STATUS_LINE_ACCENT_CLS} title={text}>
              <Loader size={ROW_ICON_PX} className="shrink-0 text-accent animate-spin" aria-hidden />{text}
            </div>
          )
        },
      },
    ] as const).find(entry => entry.when)?.build() ?? null

    // `unread` sits LAST, so it lights only when nothing else claims the slot.
    // That is stricter than the dot it replaces, which coexisted with the
    // workflow and sub-agent states; with one marker, showing two for one row is
    // not available and the more specific state is the useful one.
    //
    // It is the ONE state whose marker is not accompanied by its own words: the
    // secondary line it leads is `last_message`, which says what the agent said,
    // not that you have not read it. So unlike every glyph above — each of which
    // sits directly in front of the label naming it, and is therefore
    // `aria-hidden` — this dot keeps a real accessible name and a tooltip.
    const unreadDot = !rowState && unreadSet.has(s.key)
      // A DOT, so it keeps its own size: `ROW_ICON_PX` sizes the lucide glyphs,
      // whose ink covers a fraction of their box, while a filled disc covers all
      // of it. At 10px it reads as heavier than every state that outranks it.
      ? <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--accent)' }}
        role="img" aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
        title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
      : null
    // Custom hex (color_hex) wins over the palette index. It is deliberately
    // theme-independent: palette swatches re-derive from the theme accent,
    // a custom color is frozen. Muted-text legibility still goes through the
    // same APCA boost via boostFor.
    const customHex = typeof s.color_hex === 'string' && s.color_hex ? s.color_hex : null
    const rowColor = customHex ?? (ci != null ? paletteColors[ci] : null)
    const boostStyle: Record<string, string> = {}
    if (customHex) {
      boostStyle['--session-color'] = customHex
      const cb = boostFor(customHex)
      if (cb.mutedColors[0]) boostStyle['--session-muted'] = cb.mutedColors[0]
    } else if (rowColor && ci != null) {
      boostStyle['--session-color'] = rowColor
      if (boost.mutedColors[ci]) boostStyle['--session-muted'] = boost.mutedColors[ci]
    }
    if (recent) boostStyle.boxShadow = recencyTintShadow(recent, recentTintCount)
    // A session that's open in its own window is dimmed here so the main
    // sidebar reads as "handed off" (skipped while active — you may be viewing it).
    if (isOut && !isActive) boostStyle.opacity = '0.6'
    // The shared menu is connected: it pulls read/pin/move/copy/colour/close/tags
    // straight from the store keyed on slotKey (Tags opens the shared popover via
    // the TagPopover context). This row only supplies the one genuinely
    // surface-specific bit — Rename drives this component's inline row-edit state.
    const rowMenuProps = {
      slotKey: s.key,
      mode,
      onRename: () => { const sl = slots.find(x => x.key === s.key); suppressMenuRestoreRef.current = true; setRenamingSlot(s.key); setRenameScope(scope); setRenameValue(sl?.title && sl.title !== sl.key ? sl.title : '') },
    }
    return (
      <motion.div key={s.key} layout="position" layoutId={`slot-${layoutScope}-${s.key}`}
        data-slot-key={s.key}
        initial={{ opacity: 0, x: -12 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ layout: { type: 'spring', stiffness: 500, damping: 35 }, opacity: { duration: 0.2 }, x: { duration: 0.2 } }}>
        <DndDraggable id={`session:${s.key}`} data={{ type: 'session', key: s.key }} disabled={!dndRow || renamingSlot === s.key}>
          {({ setNodeRef, listeners, isDragging }) => (
        <ContextMenu>
          <ContextMenuTrigger asChild>
        <div ref={dndRow ? setNodeRef : undefined} {...(dndRow ? listeners : {})}
          data-draggable={(renamingSlot !== s.key).toString()}
          className={`session-row group relative flex items-start pl-3.5 pr-3 py-2 rounded-md text-sm transition-all select-none ${isActive ? !connected ? 'session-active text-text-strong bg-accent-subtle cursor-not-allowed' : 'session-active text-text-strong bg-accent-subtle cursor-pointer' : !connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'} ${rowColor ? 'session-colored' : ''} ${rowColor && colorMode === 'gradient' ? 'session-gradient' : ''} ${isDragging ? 'opacity-40' : ''} ${revealFlash?.key === s.key ? `session-reveal-flash${revealFlash.fading ? ' session-reveal-flash-fade' : ''}` : ''}`}
          style={boostStyle as React.CSSProperties}
          draggable={(!dndRow && renamingSlot !== s.key) && (connected || isActive)}
          {...offlineProps(connected, 'switch sessions')}
          role="button"
          tabIndex={0}
          data-session-row={s.key}
          data-session-scope={navScope}
          aria-current={isActive ? 'true' : undefined}
          aria-disabled={!connected}
          onKeyDown={e => {
            // ArrowUp/ArrowDown rove focus through the rows of THIS list (see
            // chat/sessionRowNav for why the rove is scope-bounded and clamped).
            // Focus-only, so walking the list doesn't load every session on the
            // way — Enter/Space below still switches. Bare arrows only: the
            // modified forms belong to other gestures (Alt+←/→ cycles sessions,
            // ⌘/Ctrl+arrow is OS text/scroll movement), and Shift is left free.
            // Skipped while a drag is in flight so dnd-kit keeps the arrows for
            // moving the dragged row, and skipped for a keystroke aimed at an
            // inner control so the rename input keeps its own caret keys.
            const roveStep = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0
            if (roveStep !== 0 && !activeDrag && !e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey
                && (e.target as HTMLElement) === e.currentTarget) {
              // Only claim the keystroke when focus actually moved; at the list
              // edge it falls through and still scrolls the list.
              if (focusSiblingSessionRow(e.currentTarget as HTMLElement, roveStep)) {
                e.preventDefault()
                e.stopPropagation()
              }
              return
            }
            // WCAG 2.1.1: session rows must be operable via keyboard.
            // Enter/Space activates the row (same as click). Other keys are
            // forwarded to dnd-kit's listener (this prop appears after the
            // {...listeners} spread, so last-prop-wins would otherwise clobber
            // it) — useful for continuing a pointer-initiated drag via arrow
            // keys. Note: keyboard-initiated drag pickup was never functional
            // for these rows (plain useDraggable without SortableContext), so
            // consuming Enter/Space here does not regress it.
            if (e.key !== 'Enter' && e.key !== ' ') {
              if (dndRow) (listeners as Record<string, (e: React.KeyboardEvent) => void> | undefined)?.onKeyDown?.(e)
              return
            }
            if ((e.target as HTMLElement) !== e.currentTarget) return // don't hijack inner buttons
            e.preventDefault()
            if (!connected) return
            dispatch(switchSlot(s.key))
            onSelectSlot?.(s.key)
          }}
          onDragStart={!dndRow ? (e => { e.dataTransfer.setData('text/plain', s.key); e.dataTransfer.effectAllowed = 'move' }) : undefined}
          onClick={e => {
            if ((e.target as HTMLElement).closest?.('[data-fork]')) { sessionActions.duplicate(s.key); return }
            if ((e.target as HTMLElement).closest?.('[data-close]')) { sessionActions.close(s.key); return }
            // When the gateway is offline, switching sessions silently fails
            // (the HTTP fetch never returns) and the user is stuck staring at
            // the previous session's transcript. Block ALL session clicks so
            // the banner + cursor-not-allowed cue make the offline state obvious.
            // Previously only non-active rows were blocked, but re-clicking the
            // already-active row also dispatches switchSlot → fetchSlotDetail
            // fails offline → switchSlot.rejected clears messages to [] → the
            // ChatPage falls into its WelcomeView branch (activeSlot truthy +
            // messages empty) showing "What can I do for you?". Closing/deleting
            // /forking still works — those are local ops (or short-circuit) that
            // don't depend on gateway state.
            if (!connected) return
            dispatch(switchSlot(s.key))
            onSelectSlot?.(s.key)
          }}>
          {/* Held-modifier digit badge: while the chat-jump modifier is down,
           *  the first nine sessions in shortcut order show the digit that
           *  jumps to them. Overlays the row's right edge; pointer-events-none
           *  so it never intercepts the click it is describing, aria-hidden
           *  because the shortcuts modal is the accessible reference. */}
          {digitModifierHeld && shortcutDigitByKey.has(s.key) && (
            <span aria-hidden="true" data-testid="digit-jump-badge"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 z-10 min-w-[18px] h-[18px] px-1 rounded flex items-center justify-center text-[11px] font-semibold tabular-nums bg-bg-elevated border border-border text-text shadow-sm pointer-events-none">
              {shortcutDigitByKey.get(s.key)}
            </span>
          )}
          {/* NO STATUS GUTTER. The row's one status marker — spinner, bot, shield,
           *  loop, question, unread dot — leads the SECONDARY LINE, immediately in
           *  front of the words it marks ("Thinking…", "3 agents running", "Needs
           *  approval"), and it is built inside each branch's `subtitle` above so a
           *  branch cannot supply one without the other.
           *
           *  It used to sit in an absolutely-positioned gutter inside the row's
           *  `pl-3.5`, occupying x 1..13 with the content column starting at 14.
           *  That band is not free: the recency tint paints an opaque accent stripe
           *  up to 7px wide at this same left edge (`recencyTintShadow`), and the
           *  session-colour bar takes the first 2px (`.session-colored::before`).
           *  An accent spinner drawn over an accent stripe is a 1:1 contrast, so on
           *  a recent session the glyph lost its left half and read as clipped and
           *  mis-placed rather than tinted.
           *
           *  Inline, the glyph starts at the content column (14px) — clear of both
           *  markers by construction, at every tint rank, with no coordination
           *  between the two features. It also drops the gutter's `role="img"` +
           *  `aria-label` for every state except `unread`: a glyph sitting in front
           *  of its own visible label is decorative, so it is `aria-hidden` and the
           *  label is read once instead of twice.
           *
           *  The alignment guides are untouched: the gutter was out of flow and
           *  contributed nothing to the content column, so removing it moves no x —
           *  see ChatSidebar.folderAlignment.test.tsx, which still asserts the
           *  row's `pl-3.5` is the content column's whole left offset. */}
          <div className="flex-1 min-w-0 overflow-hidden">
            <div className={`session-agent-label ${ROW_META_CLS} font-semibold truncate flex items-center gap-1 ${agentColor}`}>
              <AnimatePresence mode="wait">
                <motion.span key={agentName || 'empty'} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className={`truncate shrink-0 ${resolvedSlotTags.length > 0 ? 'max-w-[50%]' : ''}`}>{agentName || '\u00A0'}</motion.span>
              </AnimatePresence>
              {resolvedSlotTags.length > 0 && (
                // Every tag, each as `· <name>` tinted with the tag's own colour
                // and NO border — plain text sitting as context beside the agent
                // name, not an actionable pill. The group is the only node here
                // allowed to truncate (min-w-0), so a long tag run clips before it
                // pushes the timestamp off the row; the agent name and trailing
                // group stay shrink-0.
                //
                // It is a plain inline block (`truncate` = whitespace-nowrap +
                // overflow-hidden + text-overflow-ellipsis), NOT a flex row:
                // ellipsis does not render across flex children, so an inline-flex
                // group hard-clipped mid-word ("KiroC", "kc-them") instead of
                // showing "…". The children stay inline `<span>`s so a multi-tag
                // run ellipsizes as one line while each tag keeps its own colour
                // (applied inline, since it is per-tag data, not a theme token).
                <span className="truncate min-w-0 font-normal" title={resolvedSlotTags.map(t => t.name).join(' · ')}>
                  {resolvedSlotTags.map(t => (
                    <span key={t.id} data-testid={`slot-tag-${t.id}`}>
                      <span aria-hidden>{'\u00A0·\u00A0'}</span>
                      <span style={{ color: t.color }}>{t.name}</span>
                    </span>
                  ))}
                </span>
              )}
              {isOut && <span className="text-accent" title={i18nT('pages.chatSidebar.popped_out_to_a_separate_window')}><ExternalLink size={10} /></span>}
              {slotChannelNamespace(s.key) && (() => {
                // PROVENANCE ONLY: where this conversation started. That is
                // history, so it stays true after the channel is disconnected —
                // which is exactly why this glyph must not describe delivery.
                // It previously said the session was "two-way" with the channel
                // and that replies "are delivered there", a claim the disconnect
                // makes false while this glyph still renders. Current delivery is
                // the separate set of glyphs below, which filter on `paused`.
                //
                // `unified` gets its own key rather than an interpolated label:
                // it has no proper noun, and an English article fragment inside
                // a translated sentence is not something a locale can repair.
                const ns = slotChannelNamespace(s.key)
                const label = ns === 'unified'
                  ? i18nT('pages.chatSidebar.started_in_direct_message')
                  : i18nT('pages.chatSidebar.started_in_channel', { channel: slotChannelLabel(s.key) })
                // Brand mark rather than a generic bubble: the row already tells
                // you a chat happened, so the only new information this glyph can
                // carry is WHICH app it came from. Namespaces with no mark of
                // their own keep the bubble — ChannelBrandIcon would fall through
                // to its `Link2` default, which reads as live mirroring and would
                // collide with the link glyphs rendered just below.
                return (
                  <span className="text-muted shrink-0 inline-flex items-center" title={label} aria-label={label}>
                    {hasChannelBrandIcon(ns) ? <ChannelBrandIcon channel={ns} size={10} /> : <MessageSquare size={10} />}
                  </span>
                )
              })()}
              {/* Live mirroring, per channel. The origin glyph above is derived
               *  from the slot KEY (channelOrigin.ts) and already says where the
               *  conversation STARTED, so this renders only channels currently
               *  DELIVERING and never double-badges an origin. It replaces a
               *  `linked_to_slack` Link glyph that fired for ANY channel, because
               *  every non-Slack transport writes its id into slack_channel_id.
               *
               *  `both` counts as delivering: a two-way binding is strictly MORE
               *  connected than a one-way mirror, and filtering on `out` alone left
               *  a session with messages flowing both ways looking unlinked. A
               *  disconnected channel is excluded — it keeps its direction, so
               *  without the `paused` check the sidebar promised delivery for a
               *  session whose own menu one row away reads "Connect to X". */}
              {(s.links ?? [])
                .filter(link => link.direction !== 'origin' && !link.paused)
                .map((link, index) => (
                  <span
                    key={`${link.channel}:${link.direction}:${index}`}
                    className="inline-flex text-[10px]"
                    role="img"
                    aria-label={i18nT('pages.chatSidebar.connected_to', { label: link.label })}
                    title={i18nT('pages.chatSidebar.connected_to', { label: link.label })}
                  >
                    <ChannelBrandIcon channel={link.channel} size={10} />
                  </span>
                ))}
              {s.clean_mode
                ? <span className="text-accent" title={i18nT('pages.chatSidebar.clean_agent_only_no_kirocrew_context_or_mcp')}><Droplet size={10} /></span>
                : <>
                    {s.memory_mode === 'incognito' && <span className="text-muted" title={i18nT('pages.chatSidebar.incognito_no_memory_writes')}><EyeOff size={10} /></span>}
                    {s.memory_mode === 'temporary' && <span className="text-aim" title={i18nT('pages.chatSidebar.temporary_no_memory_reads_or_writes')}><VenetianMask size={10} /></span>}
                  </>}
              {s.mode === 'orchestrator' && <span className="px-1 py-0 rounded bg-accent/15 text-accent font-medium" title={i18nT('pages.chatSidebar.autopilot_mode')}>{i18nT('pages.chatSidebar.autopilot')}</span>}
              {/* The row badge stays just "Crew": this line already carries several
               *  chips, and by the time a session exists the mode is no longer a
               *  decision, so a second visible tag costs more room than it earns.
               *  The experimental status leads the tooltip here, and is carried
               *  visibly on the create menu, which is where the choice is made. */}
              {s.mode === 'crew' && <Badge variant="warn" className="px-1 py-0 rounded font-sans" title={`${i18nT('pages.chatSidebar.experimental')} · ${i18nT('pages.chatSidebar.crew_mode')}`}>{i18nT('pages.chatSidebar.crew')}</Badge>}
              {/* Trailing meta grouped under ONE ml-auto: two sibling auto
               *  margins would split the free space and strand the timestamp
               *  mid-row.
               *
               *  No folder chip here. The meta line already names the session's
               *  REPO, which is the more precise of the two facts — a folder is a
               *  grouping the user chose, a repo is where the work actually is —
               *  and in practice the two names coincide often enough that showing
               *  both read as a stutter. Folder membership is carried by the tree
               *  itself in folder view; in flat view the row's own context menu
               *  still names it. */}
              {slotActivityTs(s) || pinned.has(s.key) ? (
                <span className="ml-auto inline-flex items-center gap-1 shrink-0">
                  {slotActivityTs(s) && <span className="text-muted font-normal shrink-0">{fmtRelativeTime(slotActivityTs(s))}</span>}
                  {/* Last in the row: the pin is a state marker, not a label, so
                   *  it sits after the text that reads left-to-right rather than
                   *  pushing the agent name off its own start edge. */}
                  {pinned.has(s.key) && <span className="shrink-0" title={i18nT('pages.chatSidebar.pinned')}><Pin size={10} className="text-accent" /></span>}
                </span>
              ) : null}
            </div>
            {/* NEVER wraps. `truncate` rather than a two-line clamp, so every row
                is the same height. A clamped title also moved the whole
                secondary line down by a full line box on some rows, which is what
                made the list read as ragged. The full string stays reachable
                through the `title` attribute, and the rename box below is the one
                place it is shown in full. */}
            <div className={`${ROW_TITLE_CLS} font-semibold text-text ${renamingSlot === s.key && renameScope === scope ? '' : 'truncate'}`} title={s.title && s.title !== s.key ? s.title : s.key}>
              {/* No separate fork glyph: forked titles already carry the
                  persisted "↳ " marker (chat_fork.py _FORK_TITLE_MARKER). Keeping
                  the arrow in the title text — rather than as a UI-only glyph —
                  means it pre-fills the rename box (setRenameValue at the
                  onRename handler) so users can edit or drop it when they rename.
                  A separate ↳ glyph also double-stacked into "↳↳ Fork of …". */}
              {renamingSlot === s.key && renameScope === scope ? (
                <textarea ref={renameInputRef} rows={1} className={`w-full bg-transparent border border-accent rounded px-1 py-0 ${ROW_TITLE_CLS} text-text-strong outline-none select-text resize-none block overflow-hidden focus-ring`} value={renameValue} onChange={e => setRenameValue(e.target.value.replace(/[\r\n]+/g, ' '))} {...ime.bindEnter<HTMLTextAreaElement>({ onEnter: () => { (document.activeElement as HTMLTextAreaElement)?.blur() }, onEscape: () => { cancelRenameRef.current = true; setRenamingSlot(null) }, onBlur: () => { if (!cancelRenameRef.current && renameValue.trim()) { dispatch(sseSlotTitle({ key: s.key, title: renameValue.trim() })); api.renameSlot(s.key, renameValue.trim()).catch(() => { queryClient.invalidateQueries({ queryKey: ['chat-slots'] }) }) } cancelRenameRef.current = false; setRenamingSlot(null) } })} onMouseDown={e => e.stopPropagation()} />
              ) : (s.title && s.title !== s.key ? s.title : s.key)}
            </div>
            {/* Secondary line: one ordered resolver decides both the words and the
                marker leading them (#3830), so the two can no longer disagree.
                The tail is `last_message`, which is also where the `unread` dot
                lands — the one marker with no state branch of its own. A row that
                is unread with nothing said yet still renders the line, because the
                dot IS the content then. */}
            {rowState ?? ((s.last_message || unreadDot) ? (
              <div className={ROW_STATUS_LINE_MUTED_CLS}>
                {unreadDot}
                {/* `min-w-0` or the ellipsis never renders: this is a flex child, and
                    a flex item's `min-width: auto` floor keeps it at content width
                    instead of letting `truncate` clip it (i18n render gate,
                    layout/ellipsis-with-flex-parent). */}
                {s.last_message ? <span className="truncate min-w-0">{s.last_message}</span> : null}
              </div>
            ) : null)}
            {s.source_links && s.source_links.length > 0 && (
              <SessionSourceChips
                slotKey={s.key}
                links={s.source_links}
                total={s.source_links_total}
                connected={connected}
                isActive={isActive}
                onOpenSource={onOpenSource}
                onActivateSlot={() => { dispatch(switchSlot(s.key)); onSelectSlot?.(s.key) }}
              />
            )}
            {/* No tag chips here: every tag renders in the meta line above as
             *  tinted `· name` text. A chip row would print each tag twice. */}
          </div>
          {/* Hide the hover action popup (⋯ / duplicate / close) while THIS slot
           *  is being renamed: it is absolute-positioned at right-1.5 and reveals
           *  on focus-within, so the focused rename input would otherwise make it
           *  pop up and overlap the input's right edge. Mirrors the folder-header
           *  guard below (!(editingId === folder.id && editScope === 'list')). */}
          {!(renamingSlot === s.key && renameScope === scope) && (isMobile ? (
            <div className="absolute top-1/2 -translate-y-1/2 right-1.5 flex items-center gap-0.5">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button type="button" className="text-muted/50 active:text-text p-1 cursor-pointer bg-transparent border-none" aria-label={i18nT('pages.chatSidebar.more_options')} onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}><MoreVertical size={14} /></button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
                  <SessionActionsMenu variant="dropdown" {...rowMenuProps} />
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          ) : (
            <IconButtonGroup reveal className="absolute top-1/2 -translate-y-1/2 right-1.5 has-[[data-state=open]]:opacity-100">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <IconButton title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.more_options')} onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}><MoreVertical size={12} /></IconButton>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
                  <SessionActionsMenu variant="dropdown" {...rowMenuProps} />
                </DropdownMenuContent>
              </DropdownMenu>
              <IconButton variant="accent" title={i18nT('pages.chatSidebar.duplicate')} aria-label={i18nT('pages.chatSidebar.duplicate')} onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); sessionActions.duplicate(s.key) }}><Copy size={12} /></IconButton>
              <IconButton variant="danger" title={i18nT('pages.chatSidebar.close')} aria-label={i18nT('pages.chatSidebar.close_session')} onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); sessionActions.close(s.key) }}><X size={12} /></IconButton>
            </IconButtonGroup>
          ))}
        </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
            <SessionActionsMenu variant="context" {...rowMenuProps} />
          </ContextMenuContent>
        </ContextMenu>
          )}
        </DndDraggable>
        {/* The divider starts at the CONTENT x, not the row's edge, so it
         *  underlines the text block rather than boxing the whole row — the row's
         *  left pad reads as a margin, and a rule running through it would box
         *  the row instead. Matches the Figma, which carries this border on the
         *  `content` frame rather than on the row.
         *
         *  14px is the row's content offset: the row's whole `pl-3.5`, since
         *  nothing else lives in that pad. The right inset is the row's own
         *  padding. */}
        {/* `-mt-px` so the rule does NOT add a row of layout height. In flow it made
         *  the row-to-row pitch row-height + 1, and since the active row suppresses
         *  its neighbours' dividers the pitch also VARIED down the list (measured
         *  60 and 61 on one list), which no fixed row height can compensate for.
         *  Overlaying the row's last pixel keeps the pitch equal to the row height.
         *  The left inset is unchanged — it still starts at the content x. */}
        {showDivider && <div className="ml-[14px] mr-3 -mt-px border-b border-border" />}
      </motion.div>
    )
  }

  // ── Folder row: matches session-row width (full width minus drawer padding) ──
  // Recursively check if a folder or any descendant contains an unread slot.
  const folderTreeHasUnread = (folderId: string, visited = new Set<string>()): boolean => {
    if (visited.has(folderId)) return false
    visited.add(folderId)
    for (const k of unreadSet) { if (slotFolders[k] === folderId) return true }
    return folders.some(f => f.parent_id === folderId && folderTreeHasUnread(f.id, visited))
  }

  const renderFolderHeader = (folder: ChatFolder, dragHandleProps?: React.HTMLAttributes<HTMLElement>) => {
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => slotFolders[s.key] === folder.id)
    const count = childSlots.length + childFolders.length
    const hasUnread = folderTreeHasUnread(folder.id)
    const draggable = !!dragHandleProps && editingId !== folder.id
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    return (
      <div key={`folder-header-${folder.id}`}
        // Non-interactive container (role="group"): the row holds a collapse
        // toggle button + action buttons, so it must NOT itself be a button —
        // an interactive element can't legally contain other interactive
        // elements (invalid ARIA), and a folder row is a grouping, not an action.
        role="group"
        aria-label={i18nT('pages.chatSidebar.folder_2', { name: folder.name })}
        // The whole header is the drag-to-reorder handle (pointer listeners only,
        // no role override). 8px activation distance keeps the collapse toggle
        // and action buttons clickable; drag is off while renaming.
        {...(draggable ? dragHandleProps : {})}
        // Symmetric `px-3.5` (14px), with no inline left-pad override. This is the
        // SAME left pad the session rows use — that equality is the mechanism, not
        // a coincidence, and it is what makes a nested folder read as a peer of the
        // sessions filed beside it rather than sitting a couple of px to their
        // left. The pad is therefore NOT free: #3903 raised it to 18px to open a
        // gutter for an absolutely-positioned unread dot, which broke guide 3. That
        // dot is back inline on the right, where it does not compete for the pad.
        //
        // With H = this header's box left, D = `FOLDER_BODY_INSET_PX` 2 — the
        // nested body's own left inset, applied by `FolderBody` so its collapse
        // animation does not clip. It is invisible in the class list, which is
        // exactly why four revisions derived this geometry from Tailwind classes
        // and each landed 2px out. It is now a named, exported constant that the
        // alignment test imports and asserts against the rendered padding, so it
        // is no longer a free empirical term.
        // P = this pad 14,
        // G = glyph 14, g = `gap-[5px]`, M = body `ml-3` 12, B = 1px border,
        // p = body `pl-1` 4, R = row `pl-3.5` 14:
        //
        //   GUIDE 1  glyph == connector line                P = D + M
        //   GUIDE 2  name == agent / title / tool-call sub   P + G + g = D+M+B+p+R
        //   GUIDE 3  nested glyph == parent's content column P = R
        //
        //   14 = 2 + 12      14 + 14 + 5 = 2 + 12 + 1 + 4 + 14      14 = 14
        //
        // All three hold at EVERY depth and in the root lane: the algebra has no
        // per-depth term, so depth 3 nests exactly as depth 2 does. Guide 3 is why
        // the glyph→name gap is 5 and not 8 — at 8 the name overshoots the content
        // column by 3px.
        //
        // Measured on the built SPA (x in CSS px), NOT derived — a paper estimate
        // of these same numbers was 3px out: depth 1 glyph/connector 263, name and
        // all three text lines 282; depth 2 glyph/connector 282 (== depth 1's
        // content column), name/content 301; root-lane session content 263 (== the
        // root folder's glyph, so guide 3 holds outside a folder too).
        //
        // Four revisions have broken these guides by computing from class names
        // without D: #1211 (changed 9/17/7 at once), #3766 (status gutter in flow,
        // +18px to the content column), #3903 (name 1px past content, nested glyph
        // 2px short), and a `px-2` attempt during this fix. Re-measure with
        // `website/scripts/capture-folder-glyph.mjs` under MEASURE=1 — never
        // re-derive on paper.
        className={`group relative flex items-center gap-2 px-3.5 py-1.5 rounded-md text-sm text-muted hover:text-text hover:bg-bg-hover transition-all ${draggable ? 'cursor-grab active:cursor-grabbing' : ''}`}>
        {editingId === folder.id && editScope === 'list' ? (
          <>
            <FolderGlyph color={folder.color} size={14} open={!folder.collapsed} />
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[13px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
            <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
          </>
        ) : (
          <>
            {/* The collapse toggle is the real interactive control — a native
             *  <button> (keyboard-operable for free), filling the row so clicking
             *  the folder glyph/name still toggles.  Double-click the name renames. */}
            <button type="button"
              className="flex items-center gap-[5px] flex-1 min-w-0 bg-transparent border-none cursor-pointer text-left text-inherit p-0"
              aria-expanded={!folder.collapsed}
              aria-label={folder.collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name })}
              onClick={() => toggleCollapse(folder.id)}>
              <FolderGlyph color={folder.color} size={14} open={!folder.collapsed} testId={`folder-collapse-${folder.id}`} />
              {/* Double-click rename is a mouse-only power shortcut; the accessible
               *  path is the ⋯-menu Rename item, so scope-disable the interaction rule. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              <span className="flex-1 text-[13px] font-medium text-text truncate text-left" title={i18nT('pages.chatSidebar.double_click_to_rename')} onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}>{folder.name}</span>
              {/* Channel-owned folder (created by per-channel session filing):
               *  show the channel's brand mark so the folder reads as "these are
               *  the Discord conversations" at a glance. Guarded the same way the
               *  session rows are — a channel with no brand asset shows nothing
               *  rather than ChannelBrandIcon's generic Link2 fallback, which
               *  means "live mirroring" elsewhere in this sidebar. */}
              {folder.channel && hasChannelBrandIcon(folder.channel) && (
                <span className="shrink-0 opacity-80" aria-hidden><ChannelBrandIcon channel={folder.channel} size={11} /></span>
              )}
              {folder.project_dir && <span className="text-[10px] text-accent/60 shrink-0" title={folder.project_dir}><Link2 size={9} /></span>}
              {/* Unread dot on the RIGHT, inline before the count — a state marker
               *  reading after the text, not a gutter marker. #3903 moved it into an
               *  absolute LEFT gutter, which forced the header's pad to 18px; that
               *  pad is load-bearing for the alignment guides (it must equal the
               *  session row's), so the dot goes back where it does not compete with
               *  it. Only when collapsed: an expanded folder's child rows carry
               *  their own markers. */}
              {hasUnread && folder.collapsed && (
                // Carries the same accessible name as a session row's unread
                // marker, and the SAME i18n key: a colour-only dot is invisible to
                // a screen reader and indistinguishable from decoration, and this
                // one sits beside a count where that reads as styling. The session
                // row's gutter marker has had `role="img"` + a label since #3766;
                // this one had neither.
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--accent)' }}
                  role="img"
                  aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
                  title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
              )}
              <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
            </button>
            {folder.default_agent && <span className="text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded-full shrink-0 truncate max-w-[60px]" title={i18nT('pages.chatSidebar.default_agent', { name: folder.default_agent })}>{folder.default_agent}</span>}
          </>
        )}
        {!(editingId === folder.id && editScope === 'list') && (
        <div className="absolute top-1/2 -translate-y-1/2 right-1.5 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 has-[[data-state=open]]:opacity-100">
          {/* ⋯ menu first, then the primary "new chat" action.  Sibling
           *  <button>s of the collapse toggle (valid ARIA — no nesting). */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" data-testid={`folder-menu-${folder.id}`} onMouseDown={e => { e.stopPropagation() }}><MoreVertical size={12} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
              <DropdownMenuItem data-testid={`folder-rename-${folder.id}`} onClick={() => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setFolderModal({ mode: 'create', parentId: folder.id }) }}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</DropdownMenuItem>
              {/* Re-parent: move this folder under another folder or back to the
               *  top level. Self + descendants are excluded (cycle guard). */}
              <FolderMoveSubmenu variant="dropdown" label={i18nT('pages.chatSidebar.move_folder_to')}
                folders={reparentTargets}
                currentFolderId={folder.parent_id || null}
                onPick={pid => moveFolderTo(folder.id, pid)} />
              <DropdownMenuItem data-testid={`folder-settings-${folder.id}`} onClick={() => { setFolderModal({ mode: 'edit', folderId: folder.id }) }}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</DropdownMenuItem>
              {/* Hide this folder from the session lists (flat lane + tree).
               *  Same state the filter menu's checkboxes drive, reached from the
               *  folder itself — which is where the user is looking when they
               *  decide a folder is noise. Distinct from "Hide when empty"
               *  below, which is a server-persisted archive affordance. */}
              <DropdownMenuItem data-testid={`folder-visibility-${folder.id}`} onClick={() => { toggleFolderFilter(folder.id) }}>
                {filterHiddenFolders.has(folder.id)
                  ? <><Eye size={13} /> {i18nT('pages.chatSidebar.show_folder')}</>
                  : <><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_folder')}</>}
              </DropdownMenuItem>
              {folderOffersHide(folder, foldersWithActiveSubtree) && (
                <DropdownMenuItem data-testid={`folder-hide-${folder.id}`} onClick={() => { updateFolderMutation.mutate({ id: folder.id, body: { hidden: true } }) }}><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_when_empty')}</DropdownMenuItem>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem className="text-danger focus:text-danger" data-testid={`folder-delete-${folder.id}`} onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} onClick={e => { e.stopPropagation(); createChatInFolder(folder.id) }}><MessageSquarePlus size={12} /></button>
        </div>
        )}
      </div>
    )
  }

  // One row announcing the folders this container is hiding, rendered at the
  // BOTTOM of that container's folder list and indented to its depth. Peeking it
  // open renders those folders' real blocks (dimmed), so every normal
  // affordance — including ⋯ → Show folder, the durable undo — still works.
  // `containerKey` is 'root' | 'flat' | parent folder id.
  const renderHiddenReveal = (containerKey: string, hidden: readonly ChatFolder[], depth: number): React.ReactNode => {
    if (hidden.length === 0) return null
    const open = revealedContainers.has(containerKey)
    const n = hidden.length
    return (
      <div key={`hidden-reveal-${containerKey}`} data-testid={`hidden-reveal-${containerKey}`}>
        <button
          type="button"
          onClick={() => toggleReveal(containerKey)}
          aria-expanded={open}
          title={open ? i18nT('pages.chatSidebar.collapse_hidden_folders') : `Show ${n} hidden folder${n === 1 ? '' : 's'}`}
          className="w-full flex items-center gap-1.5 py-1 pr-2 text-left text-[11px] text-muted hover:text-fg hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
          style={{ paddingLeft: `${8 + depth * 12}px` }}
        >
          <DisclosureChevron open={open} size={11} />
          <span>{n} {n === 1 ? i18nT('pages.chatSidebar.hidden_folder') : i18nT('pages.chatSidebar.hidden_folders')}</span>
        </button>
        {open && (
          <div className="opacity-70">
            {hidden.map(f => (
              <Fragment key={`revealed-${f.id}`}>{renderFolderBlock(f, depth)}</Fragment>
            ))}
          </div>
        )}
      </div>
    )
  }

  const renderFolderBlock = (folder: ChatFolder, depth: number, visited = new Set<string>(), dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed = false): React.ReactNode[] => {
    if (depth > 10 || visited.has(folder.id)) return []
    visited.add(folder.id)
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => slotFolders[s.key] === folder.id)
    const childNodes: React.ReactNode[] = []
    // Nested subfolders are plain draggables (not sortables): dragging one
    // re-parents it — drop on another folder to move inside, or on the root
    // lane to move to the top level. The subtree ids ride along in the drag
    // data so collision detection can exclude self/descendants as targets.
    for (const cf of childFolders.filter(cf => !isFolderHidden(cf) && !isFolderFilteredOut(cf))) {
      childNodes.push(
        <DndDraggable key={`subfolder-drag-${cf.id}`} id={cf.id}
          data={{ type: 'folder', nested: true, subtree: [...(folderSubtrees.get(cf.id) ?? collectFolderSubtreeIds(folders, cf.id))] }}
          disabled={editingId === cf.id}>
          {({ setNodeRef, listeners, isDragging }) => (
            <div ref={setNodeRef} style={{ opacity: isDragging ? 0.5 : 1 }}>
              {/* This children function runs during DndDraggable's OWN render —
               *  deferred and re-invoked (StrictMode, isDragging flips). Pass a
               *  CLONE of the ancestor path: sharing the mutated `visited` set
               *  makes the second invocation hit the cycle guard and render the
               *  subfolder as [] (folder vanishes; drags die at drag-start).
               *  The source collapses while dragging (same UX as root-folder
               *  reorder); the layout shift this causes is compensated by the
               *  drag-scoped droppable re-measure polling on the DndContext. */}
              {renderFolderBlock(cf, depth + 1, new Set(visited), listeners as unknown as React.HTMLAttributes<HTMLElement>, isDragging)}
            </div>
          )}
        </DndDraggable>
      )
    }
    // Bottom of THIS container's folder list: announce what the filter is
    // hiding here, at this depth. Sits after the sibling folders and before the
    // new-subfolder input, so it reads as part of the folder list.
    const hiddenHere = hiddenByContainer.get(folder.id)
    if (hiddenHere?.length) childNodes.push(renderHiddenReveal(folder.id, hiddenHere, depth + 1))
    childSlots.forEach((s, i) => {
      const isActive = activeSlot === s.key
      const nextIsActive = i < childSlots.length - 1 && activeSlot === childSlots[i + 1].key
      const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
      childNodes.push(renderSessionRow(s, depth + 1, showDivider))
    })
    // Hide folders with no matching children while the list is narrowed
    if (listNarrowed && childNodes.length === 0) return []
    // Wrap children in a bordered container so the folder's extent is visually
    // clear when multiple folders are open. Only wrap when there's content,
    // otherwise the FolderBody would render an empty 1px-tall strip with a line.
    const wrapped = childNodes.length > 0 ? (
      <div key={`folder-children-${folder.id}`} className="border-l border-border mb-1 ml-3 pl-1 rounded-bl-md">
        {childNodes}
      </div>
    ) : !listNarrowed ? (
      // Empty-folder affordance: a newly created (or emptied) expanded folder
      // would otherwise render nothing, leaving the hover ⊕ on the header as
      // the only (invisible-at-rest) way to start a session in it.
      <div key={`folder-children-${folder.id}`} className="border-l border-border mb-1 ml-3 pl-1 rounded-bl-md">
        <button key={`folder-newchat-${folder.id}`} type="button"
          onClick={() => createChatInFolder(folder.id)}
          title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
          className="w-full flex items-center gap-2.5 pl-3.5 pr-3 py-2 rounded-md text-[12px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
          <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={13} className="shrink-0 ml-auto" />
        </button>
      </div>
    ) : null
    // Outer container wraps header + body so the entire folder block is a
    // single drag-drop target. Dropping anywhere inside (header, children,
    // empty space) assigns the dragged session to this folder.
    // Uses a dragEnter counter instead of contains() checks — nested child
    // folders fire enter/leave pairs that balance to zero when the drag
    // moves into a subfolder, so the parent highlight clears correctly.
    return [
      <DndDroppable key={`folder-drop-${folder.id}`} id={`folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
          <div ref={setNodeRef} data-folder-drop={folder.id} className={`rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}`}>
            {renderFolderHeader(folder, dragHandleProps)}
            <FolderBody key={`folder-body-${folder.id}`} open={!folder.collapsed && !forceCollapsed}>{wrapped}</FolderBody>
          </div>
        )}
      </DndDroppable>,
    ]
  }

  const rootFolders = useMemo(() => folders.filter(f => !f.parent_id).sort((a, b) => a.order - b.order), [folders])
  const visibleRootFolders = useMemo(() => rootFolders.filter(f => !isFolderHidden(f) && !isFolderFilteredOut(f)), [rootFolders, isFolderHidden, isFolderFilteredOut])
  const rootFolderIds = useMemo(() => visibleRootFolders.map(f => f.id), [visibleRootFolders])
  const ungroupedSlots = filteredSlots.filter(s => !slotFolders[s.key])
  // True while actively dragging a session that currently lives in a folder.
  // Used to reveal the empty-state drop placeholder inside the "No folder"
  // group so there's always a reachable ungroup target.
  const draggingFolderedSession = activeDrag?.type === 'session' && !!slotFolders[activeDrag.id]
  // WHY the session being dragged may not be referenced into the open chat, or
  // null when it may be. Carries the reason rather than a boolean because the two
  // refusals read differently to the user (a privacy guard vs a self-drop no-op).
  // Drives the drop zone's refusal state; the drop handler re-decides with the
  // same function.
  const draggingRefRefusal = activeDrag?.type === 'session'
    ? sessionRefBlockReason({
      key: activeDrag.id,
      activeSlot,
      memoryMode: slots.find(x => x.key === activeDrag.id)?.memory_mode,
    })
    : null
  // True while dragging a folder that currently has a parent — the only case
  // where "drop on the root lane to move to top level" applies.
  const draggingNestedFolder = activeDrag?.type === 'folder' && !!folders.find(f => f.id === activeDrag.id)?.parent_id

  // Droppable rects are normally snapshotted once at drag-start, but these
  // lanes ANIMATE during drags (the dragged folder's body collapses over 150ms;
  // hovered collapsed folders auto-expand; the chat-pane zone mounts mid-drag),
  // so the snapshot goes stale and drop targets diverge from the cursor. While a
  // drag is live, poll re-measurement (dnd-kit's numeric `frequency`
  // self-reschedules a measure loop) so rects track the animating layout. Idle
  // sessions keep the plain strategy — no background measuring.
  const dndMeasuring = activeDrag
    ? { droppable: { strategy: MeasuringStrategy.Always, frequency: 100 } }
    : { droppable: { strategy: MeasuringStrategy.Always } }
  /** The follow-the-cursor preview for whatever is being dragged. */
  const dragGhost = activeDrag
    ? activeDrag.type === 'folder'
      ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} />
      : <SessionDragGhost slot={slots.find(x => x.key === activeDrag.id)} fallbackLabel={activeDrag.id} />
    : null
  /**
   * The drag preview is PORTALED to `document.body`.
   *
   * dnd-kit positions the overlay `fixed`, which normally escapes ancestor
   * overflow — but the sidebar rides inside OverlayDrawer's morph `clip-path`,
   * and a clip-path clips every descendant including fixed ones. Rendered in
   * place, the ghost therefore vanished the instant the cursor crossed out of
   * the sidebar and into the chat pane, i.e. for the whole second half of the
   * one gesture that aims there. Portaling keeps it visible until release; it
   * stays inside the DndContext because React portals preserve context.
   */
  const dragOverlay = createPortal(
    <DragOverlay dropAnimation={null}>{dragGhost}</DragOverlay>,
    document.body,
  )

  // Narrow-sidebar header responsiveness: below ~256px the full "New chat"
  // label no longer fits next to the label + kebab, so collapse the create
  // button to icon-only; below ~200px also drop the "Sessions" label.
  const compactHeader = sidebarWidth < 256
  const tinyHeader = sidebarWidth < 200

  return (
    // stable theming hook 'sidebar' — see website/docs/theming-contract.md
    <div ref={sidebarRootRef} className="sidebar sidebar-inner bg-bg-elevated border border-border rounded-xl shadow-sm flex flex-col shrink-0 relative h-full" style={{ width: sidebarWidth }}>
      {/* Drag handle — Pointer-Events column resize (mouse + touch + pen).
          role="separator" gives it correct ARIA; touch-action:none so a touch
          drag resizes the panel instead of scrolling the page. Pointer capture
          (in usePointerDrag) continues the drag off the thin handle. No
          keyboard analogue for a drag splitter. */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label={i18nT('pages.chatSidebar.resize_sidebar')}
        className="sidebar-resize-handle absolute top-0 -right-[2px] w-[5px] h-full cursor-col-resize z-10 group/drag flex items-center justify-center"
        style={{ touchAction: 'none' }}
        {...sidebarResize}
      >
        <div className="w-[2px] h-full bg-transparent group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-colors duration-200" />
      </div>

      {/* Header — all elements ("Sessions" title, kebab, New button) centered
          on one line 23px from the panel top (1px card border + mt-0.5, then
          centered in a 40px row) — the shared control baseline: the nav rail
          header, chat title row, and activity strip icons center on the same
          line.
          px-2 is symmetric so the New button ends 9px from the card's right
          edge (8 + 1px border) — the same as its 9px gap to the top edge
          (1px border + mt-0.5 + 6px of the h-10 row around the h-7 button). */}
      <div className="flex justify-between items-center px-2 mt-0.5 h-10">
        <div className={`flex items-center gap-1.5 min-w-0 flex-1 ${collapsible && !isMobile ? 'pl-9' : 'pl-1.5'}`}>
          {!tinyHeader && <span className="sessions-panel-title text-sm font-semibold text-text-strong tracking-[.04em] truncate">{i18nT('pages.chatSidebar.sessions')}</span>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" title={i18nT('pages.chatSidebar.more_options')} aria-label={i18nT('pages.chatSidebar.more_options')}><MoreVertical size={14} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[180px]">
              <DropdownMenuItem disabled={seedStateLanesMutation.isPending} onClick={() => {
                if (seedStateLanesMutation.isPending) return
                const isActive = tagColumnsEnabled && rawColumns.length > 0
                const next = !isActive
                const cfg = loadChatConfig()
                saveChatConfig({ ...cfg, tagColumnsEnabled: next })
                setSeedError('')
                if (!next) {
                  // Leaving board view: give back the width the user chose before
                  // the lanes were auto-widened, rather than stranding a ~900px
                  // sidebar in list view.
                  const prior = parseInt(localStorage.getItem(SIDEBAR_PRE_BOARD_LS_KEY) || '', 10)
                  if (!isNaN(prior) && prior >= SIDEBAR_MIN && prior <= SIDEBAR_MAX) {
                    setSidebarWidth(prior)
                    onWidthChangeRef.current?.(prior)
                    safeSetItem(SIDEBAR_LS_KEY, String(prior))
                    safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, '')
                  }
                }
                // Seed when the board has no lanes and nothing configured worth
                // keeping. Seeding is additive and idempotent, so a repeat click
                // cannot duplicate lanes; the pending guard above only stops a
                // second request racing the first before the cache refreshes.
                if (next && !rawColumns.some(c => c.source === 'state' || c.name || (c.tag_ids || []).length || c.include_untagged)) {
                  seedStateLanesMutation.mutate()
                }
              }}>
                <Columns3 size={14} className={tagColumnsEnabled && rawColumns.length > 0 ? 'text-accent' : 'text-muted'} />
                {tagColumnsEnabled && rawColumns.length > 0 ? i18nT('pages.chatSidebar.switch_to_list_view') : i18nT('pages.chatSidebar.switch_to_board_view')}
              </DropdownMenuItem>
              {tagColumnsEnabled && rawColumns.length > 0 && missingLanes.length > 0 && (
                <DropdownMenuItem
                  data-testid="add-state-lanes"
                  disabled={seedStateLanesMutation.isPending}
                  onClick={() => { if (!seedStateLanesMutation.isPending) seedStateLanesMutation.mutate() }}
                >
                  <Columns3 size={14} className="text-muted" />
                  {i18nT('pages.chatSidebar.add_state_lanes')}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onClick={() => { setCleanupOpen(!cleanupOpen); setCleanupExpanded(false); setCleanupError('') }}>
                <BrushCleaning size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_sessions')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setBulkModelOpen(true); setBulkModel(''); setBulkSkipRunning(true); setBulkModelError('') }}>
                <Cpu size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.switch_all_to_model')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setManageTagsOpen(o => !o)}>
                <TagIcon size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.manage_tags')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Split create-button: main segment = one-click New chat; caret
           *  opens a menu grouping New folder + New chat in folder (flat
           *  folder flyout). Replaces the old standalone New-folder + New-chat
           *  header buttons. Menu is portaled to <body> so the right-side
           *  folder flyout escapes the sidebar's overflow clip. */}
          <div className="relative flex items-center rounded-md bg-accent text-accent-fg overflow-hidden shrink-0" data-create-menu>
            <button
              disabled={creatingSlot}
              className={`flex items-center h-7 cursor-pointer bg-transparent border-none text-accent-fg hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-70 disabled:cursor-wait disabled:active:scale-100 ${compactHeader ? 'justify-center w-7' : 'gap-1.5 pl-2 pr-2.5 text-[12px] font-semibold'}`}
              onClick={() => { createChatMutation.mutate() }}
              title={i18nT('pages.chatSidebar.new_chat')}
              aria-label={i18nT('pages.chatSidebar.new_chat_session')}
              aria-busy={creatingSlot}
            >{creatingSlot ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}{!compactHeader && <span className="whitespace-nowrap">{creatingSlot ? i18nT('pages.chatSidebar.creating') : i18nT('pages.chatSidebar.new')}</span>}</button>
            <span className="w-px h-4 bg-accent-fg opacity-30" aria-hidden="true" />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  className="flex items-center justify-center w-6 h-7 cursor-pointer bg-transparent border-none text-accent-fg hover:bg-black/10 active:scale-95 transition-all"
                  title={i18nT('pages.chatSidebar.create')} aria-label={i18nT('pages.chatSidebar.more_create_options')}><ChevronDown size={13} /></button>
              </DropdownMenuTrigger>
              {/* max-w bounds the menu: the mode descriptions below are full
               *  sentences, and without an upper bound a flex item's automatic
               *  min-width lets the longest one stretch the menu across the
               *  session list instead of wrapping. */}
              <DropdownMenuContent align="end" className="min-w-[200px] max-w-[264px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                {/* The plain chat is what the button's main segment does, but a
                 *  menu that lists every OTHER way to create and omits the
                 *  ordinary one reads as if autopilot were the only kind of
                 *  chat the caret can make. Listed first so the default stays
                 *  the default. */}
                <DropdownMenuItem disabled={creatingSlot} onClick={() => { createPlainChatMutation.mutate() }}>
                  <MessageSquarePlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat')}
                </DropdownMenuItem>
                {/* The two engineered modes carry a one-line description, because the
                 *  moment a user cannot tell them apart is the moment this menu opens
                 *  — and until now the only explanation lived in a native title= on
                 *  the sidebar badge, i.e. after the session already existed. The
                 *  plain entries stay single-line: "New chat" and "New folder" need
                 *  no gloss, and describing them would bury the contrast that
                 *  actually needs drawing. `items-start` so the icon aligns to the
                 *  label, not to the middle of the two-line block. */}
                <DropdownMenuItem className="items-start" disabled={creatingSlot} onClick={() => { createAutopilotMutation.mutate() }}>
                  <Zap size={14} className="text-muted mt-[3px] shrink-0" />
                  <span className="flex min-w-0 flex-col gap-px">
                    <span>{i18nT('pages.chatSidebar.new_autopilot_chat')}</span>
                    <span className="whitespace-normal text-[11px] leading-snug text-muted">{i18nT('pages.chatSidebar.autopilot_desc')}</span>
                  </span>
                </DropdownMenuItem>
                <DropdownMenuItem className="items-start" data-testid="new-crew-chat" onClick={() => { createCrewMutation.mutate() }}>
                  <Users size={14} className="text-muted mt-[3px] shrink-0" />
                  <span className="flex min-w-0 flex-col gap-px">
                    {/* The tag rides the TITLE row, not the gloss below it: this menu
                     *  is the only point at which the mode is chosen, so a caution
                     *  placed in the description is read after the click rather than
                     *  before it. `flex-wrap` so a longer localised label drops the
                     *  tag onto its own line instead of widening the row past the
                     *  menu's max-w-[264px] and clipping whichever renders last. */}
                    <span className="flex flex-wrap items-center gap-x-1.5">
                      <span>{i18nT('pages.chatSidebar.new_crew_chat')}</span>
                      <Badge variant="warn" className="px-1 py-0 text-[10px] rounded font-sans" data-testid="crew-experimental-tag">{i18nT('pages.chatSidebar.experimental')}</Badge>
                    </span>
                    <span className="whitespace-normal text-[11px] leading-snug text-muted">{i18nT('pages.chatSidebar.crew_desc')}</span>
                  </span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }}>
                  <FolderPlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_folder')}
                </DropdownMenuItem>
                {folders.length > 0 && (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                      <Folder size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat_in_folder')}
                      <ChevronRight size={13} className="ml-auto text-muted" />
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent className="max-h-[300px] overflow-y-auto">
                      {(() => {
                        const roots = folders.filter(f => !f.parent_id)
                        const childrenOf = (pid: string) => folders.filter(f => f.parent_id === pid)
                        const items: { f: ChatFolder; depth: number }[] = []
                        const walk = (list: ChatFolder[], depth: number) => { for (const f of list) { items.push({ f, depth }); walk(childrenOf(f.id), depth + 1) } }
                        walk(roots, 0)
                        return items.map(({ f, depth }) => (
                          <DropdownMenuItem key={f.id} style={{ paddingLeft: `${12 + depth * 16}px` }} onClick={() => createChatInFolder(f.id, { focus: true })}>
                            <Folder size={14} className={depth === 0 ? 'text-muted' : 'text-muted/60'} /> {f.name}
                          </DropdownMenuItem>
                        ))
                      })()}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>

      {/* Split View (session grid) has no entry here on purpose: this sidebar is a
       *  navigation surface, and the grid's own affordances live next to the
       *  transcript they act on — the chat header's Columns2 button (⌘D) opens it,
       *  and the header's "in split" badge is the way back into a live split. */}

      {/* Clean Up dialog */}
      {cleanupOpen && (() => {
        const archivable = cleanupPreview ? cleanupPreview.map(k => slots.find(s => s.key === k)).filter(Boolean) as Slot[] : []
        const noStale = cleanupPreview != null && cleanupPreview.length === 0 && !activeIsStale
        return (
          <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
            <div className="font-medium text-text-strong mb-2"><BrushCleaning size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.clean_up_sessions_2')}</div>
            <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.archive_sessions_with_no_activity_in_the_last')}</div>
            <div className="flex items-center gap-2 mb-3">
              {[1, 3, 7].map(d => (
                <button key={d} className={`px-2.5 py-1 rounded-md text-[12px] border transition-all cursor-pointer ${
                  cleanupDays === d ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
                }`} onClick={() => setCleanupDays(d)}>{i18nT('pages.chatSidebar.day', { count: d })}</button>
              ))}
            </div>
            <div className="text-[12px] text-muted mb-3">
              {cleanupPreviewLoading
                ? i18nT('pages.chatSidebar.checking')
                : cleanupPreviewError
                  ? <>{i18nT('pages.chatSidebar.failed_to_load_preview')} <button className="text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })}>{i18nT('pages.chatSidebar.retry')}</button></>
                  : noStale
                    ? i18nT('pages.chatSidebar.no_inactive_sessions_to_archive')
                    : cleanupPreview != null && <>
                      {i18nT('pages.chatSidebar.session', { count: archivable.length })} {i18nT('pages.chatSidebar.will_be_moved_to_older_sessions')}{activeIsStale ? ` ${i18nT('pages.chatSidebar.1_skipped_currently_selected')}` : ''} {i18nT('pages.chatSidebar.pinned_sessions_are_kept')}
                      {archivable.length > 0 && (
                        <button className="ml-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => setCleanupExpanded(!cleanupExpanded)}>
                          {cleanupExpanded ? i18nT('pages.chatSidebar.hide') : i18nT('pages.chatSidebar.show')} {i18nT('pages.chatSidebar.session', { count: archivable.length })} ▸
                        </button>
                      )}
                      {cleanupExpanded && archivable.length > 0 && (
                        <div className="mt-2 max-h-32 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5">
                          {archivable.map(s => (
                            <div key={s.key} className="text-[12px] text-muted truncate py-0.5 px-1">
                              {s.title && s.title !== s.key ? s.title : s.key}
                              {slotActivityTs(s) && <span className="ml-1 text-[11px] opacity-60">{fmtRelativeTime(slotActivityTs(s))}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      </>
              }
            </div>
            <div className="flex items-center gap-2 justify-end">
              {cleanupError && <span className="text-[11px] text-danger flex-1">{cleanupError}</span>}
              <Btn className="text-[12px] px-3 py-1" onClick={() => setCleanupOpen(false)}>{i18nT('pages.chatSidebar.cancel')}</Btn>
              <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={archivable.length === 0 || cleanupMutation.isPending || cleanupPreviewLoading} onClick={() => {
                setCleanupError('')
                cleanupMutation.mutate()
              }}>{cleanupMutation.isPending ? i18nT('pages.chatSidebar.archiving') : `Archive ${archivable.length} session${archivable.length !== 1 ? 's' : ''}`}</Btn>
            </div>
          </div>
        )
      })()}

      {/* Switch-all-to-model dialog — mirrors the Clean Up panel. Picking a
       *  model applies it to every live session (each switch resets that
       *  session); running sessions are skipped by default. */}
      {bulkModelOpen && (
        <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="font-medium text-text-strong mb-2"><Cpu size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.switch_all_sessions')}</div>
          <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.pick_a_model_for_every_session_switching_a_sessi')} <span className="text-danger">{i18nT('pages.chatSidebar.resets_its_conversation')}</span>.</div>
          <div ref={bulkListRef} role="listbox" aria-label={i18nT('pages.chatSidebar.model_list')} tabIndex={-1} onKeyDown={bulkOnListKeyDown} className="max-h-[220px] overflow-y-auto rounded-md border border-border bg-bg-elevated p-1 mb-2 outline-none">
            <ModelDropdownList models={bulkModelOptions} activeModel={bulkModel} onSelect={setBulkModel} />
          </div>
          {bulkRunningCount > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              <input type="checkbox" checked={bulkSkipRunning} onChange={e => setBulkSkipRunning(e.target.checked)} />
              {i18nT('pages.chatSidebar.skip')} {i18nT('pages.chatSidebar.running_session', { count: bulkRunningCount })}
            </label>
          )}
          <div className="flex items-center gap-2 justify-end">
            {bulkModelError && <span className="text-[11px] text-danger flex-1">{bulkModelError}</span>}
            <Btn className="text-[12px] px-3 py-1" onClick={() => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') }}>{i18nT('pages.chatSidebar.cancel')}</Btn>
            <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={!bulkModel || bulkAffectedCount === 0 || bulkModelMutation.isPending} onClick={() => { setBulkModelError(''); bulkModelMutation.mutate({ model: bulkModel, skipRunning: bulkSkipRunning }) }}>{bulkModelMutation.isPending ? i18nT('pages.chatSidebar.switching') : `Switch ${bulkAffectedCount} session${bulkAffectedCount !== 1 ? 's' : ''}`}</Btn>
          </div>
        </div>
      )}

      {/* Manage-tags panel — mirrors the Clean Up / Switch All panels. Renders
       *  the shared TagManagerList in 'manage' mode (no column context), so tag
       *  CRUD is reachable in list view too, not only from a board column. */}
      {manageTagsOpen && (
        <div data-testid="manage-tags-panel" className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="flex items-center justify-between mb-2">
            <div className="font-medium text-text-strong"><TagIcon size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.manage_tags_2')}</div>
            <button type="button" className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 leading-none" onClick={() => setManageTagsOpen(false)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
          </div>
          <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.rename_flag_as_status_or_delete_tags_changes_app')}</div>
          <TagManagerList mode="manage" />
        </div>
      )}

      {/* Search with inline sort/filter control */}
      <div className="px-2 pt-2 pb-1">
        <div className="relative">
          <SearchInput className={`w-full ${slotFilter ? (folders.length > 0 ? '[&>input]:pr-[76px]' : '[&>input]:pr-14') : (folders.length > 0 ? '[&>input]:pr-14' : '[&>input]:pr-9')}`} placeholder={i18nT('pages.chatSidebar.search_sessions')} value={slotFilter} onChange={e => setSlotFilter(e.target.value)} />
          {slotFilter && (
            <button type="button" className={`absolute ${folders.length > 0 ? 'right-[56px]' : 'right-8'} top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors`} onClick={() => setSlotFilter('')} aria-label={i18nT('pages.chatSidebar.clear_search')}><X size={13} /></button>
          )}
          <div className="absolute right-1 inset-y-0 flex items-center gap-0.5">
            {/* Flat-view toggle only makes sense when folders exist — without
             *  them the list is already flat. */}
            {folders.length > 0 && (
            <button
              type="button"
              className={`relative w-6 h-6 rounded flex items-center justify-center cursor-pointer transition-colors border-none ${flatView ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`}
              onClick={toggleFlatView}
              title={flatView ? i18nT('pages.chatSidebar.back_to_folder_view') : i18nT('pages.chatSidebar.flat_view_all_chats_without_folders')}
              aria-label={flatView ? i18nT('pages.chatSidebar.switch_to_folder_view') : i18nT('pages.chatSidebar.switch_to_flat_view_all_chats_without_folders')}
              aria-pressed={flatView}
              data-testid="flat-view-toggle"
            >
              <List size={14} />
            </button>
            )}
            <DropdownMenu open={filterSortOpen} onOpenChange={setFilterSortOpen}>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="relative w-6 h-6 rounded text-muted flex items-center justify-center cursor-pointer transition-colors hover:text-text hover:bg-bg-hover bg-transparent border-none"
                  title={i18nT('pages.chatSidebar.sort_filter_sessions')}
                  aria-label={i18nT('pages.chatSidebar.sort_and_filter_sessions')}
                >
                  <ListFilter size={14} />
                  {filterCounts['unread'] > 0 && (
                    <span
                      aria-hidden="true"
                      className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-[3px] rounded-full bg-accent text-accent-fg text-[10px] font-semibold leading-[14px] text-center pointer-events-none shadow-[0_0_4px_var(--accent-glow)]"
                    >
                      {filterCounts['unread'] > 99 ? '99+' : filterCounts['unread']}
                    </span>
                  )}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[180px] max-h-[70vh] overflow-y-auto">
                <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em]">{i18nT('pages.chatSidebar.filter')}</DropdownMenuLabel>
                {SESSION_FILTERS.map(filterDef => {
                  const active = activeFilters.has(filterDef.key)
                  const slotCount = filterCounts[filterDef.key] ?? 0
                  const isRecent = filterDef.key === 'recent'
                  if (isRecent) {
                    // Recent gets a nested submenu (flyout) for choosing the
                    // window. The whole row is a single SubTrigger (one focusable
                    // menu item with correct roving-tabindex). Toggling the
                    // filter must be reachable by every input modality:
                    //  - pointer/touch: onClick toggles; we deliberately do NOT
                    //    preventDefault so Radix's own click-to-open still fires
                    //    (touch/coarse pointers have no hover path to the picker).
                    //  - keyboard: Radix routes Enter/Space/ArrowRight to open the
                    //    submenu and the SubTrigger is a div (no synthetic click),
                    //    so onClick never fires for keys. onKeyDown toggles on
                    //    Enter/Space (preventDefault suppresses Radix's open for
                    //    just those keys); ArrowRight falls through and opens.
                    return (
                      <DropdownMenuSub key={filterDef.key}>
                        <DropdownMenuSubTrigger
                          title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                          onClick={() => toggleFilter('recent')}
                          onKeyDown={e => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              toggleFilter('recent')
                            }
                          }}
                        >
                          {filterDef.icon(active)}
                          <span className="flex-1 truncate">
                            {i18nT(FILTER_LABEL_KEY[filterDef.key])}
                            <span className="text-muted"> · {formatRecentWindow(recentWindowMs)}</span>
                            {slotCount > 0 ? ` (${slotCount})` : ''}
                          </span>
                          {active && <Check size={14} className="text-accent shrink-0" />}
                          <ChevronRight size={13} className="text-muted shrink-0" />
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent className="min-w-[190px] p-2">
                          {/* Non-menu-item controls: stop click/keydown from
                              reaching Radix so choosing a window doesn't dismiss
                              the menu (mirrors the folder-rename input pattern). */}
                          <div
                            onClick={e => e.stopPropagation()}
                            onMouseDown={e => e.stopPropagation()}
                            onKeyDown={e => e.stopPropagation()}
                          >
                            <div className="px-1 pb-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.within')}</div>
                            <div className="flex flex-wrap gap-1 px-1 mb-2">
                              {RECENT_WINDOW_PRESETS.map(preset => (
                                <button
                                  key={preset.ms}
                                  type="button"
                                  aria-pressed={recentWindowMs === preset.ms}
                                  className="px-2 py-0.5 rounded-full text-[11px] cursor-pointer border transition-colors"
                                  style={recentWindowMs === preset.ms
                                    ? { background: 'color-mix(in srgb, var(--ok) 12%, transparent)', color: 'var(--ok)', borderColor: 'color-mix(in srgb, var(--ok) 35%, transparent)' }
                                    : { background: 'transparent', color: 'var(--muted)', borderColor: 'var(--border)' }}
                                  onClick={() => selectRecentPreset(preset.ms)}
                                >
                                  {preset.label}
                                </button>
                              ))}
                            </div>
                            <div className="px-1 text-[12px] text-muted">
                              <div className="mb-1">{i18nT('pages.chatSidebar.custom')}</div>
                              <div className="flex items-center gap-1.5">
                                {/* Draft-string value so the field can be cleared
                                    / partially typed; commit + clamp on blur or
                                    Enter. Unit changes commit immediately but keep
                                    the amount as-typed (no re-derivation flip). */}
                                <input
                                  type="number"
                                  min={1}
                                  max={9999}
                                  value={recentAmountDraft}
                                  onChange={e => setRecentAmountDraft(e.target.value)}
                                  onBlur={commitRecentAmount}
                                  onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commitRecentAmount() } }}
                                  aria-label={i18nT('pages.chatSidebar.custom_recency_amount')}
                                  className="w-12 shrink-0 px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text text-[12px]"
                                />
                                <SimpleSelect
                                  value={recentUnitDraft}
                                  onChange={v => changeRecentUnit(v as RecentUnit)}
                                  className="px-1.5 py-0.5 text-[12px] rounded"
                                  options={['minutes', 'hours', 'days']}
                                  optionLabels={[i18nT('pages.chatSidebar.min'), i18nT('pages.chatSidebar.hours'), i18nT('pages.chatSidebar.days')]}
                                  aria-label={i18nT('pages.chatSidebar.custom_recency_unit')}
                                  // Was `flex-1 min-w-0` on the old <select>; the
                                  // trigger's chrome is fixed inside ui/select.tsx,
                                  // but the flex sizing has to survive on the
                                  // wrapper div that replaces it as the flex item.
                                  style={{ flex: '1 1 0%', minWidth: 0 }}
                                />
                              </div>
                            </div>
                          </div>
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                    )
                  }
                  return (
                    <DropdownMenuItem
                      key={filterDef.key}
                      title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                      // Keep the menu open so multiple filters can be toggled.
                      onSelect={e => { e.preventDefault(); toggleFilter(filterDef.key) }}
                    >
                      {filterDef.icon(active)}
                      <span className="flex-1 truncate">{i18nT(FILTER_LABEL_KEY[filterDef.key])}{slotCount > 0 ? ` (${slotCount})` : ''}</span>
                      {active && <Check size={14} className="text-accent shrink-0" />}
                    </DropdownMenuItem>
                  )
                })}
                <DropdownMenuSeparator />
                <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em]">{i18nT('pages.chatSidebar.sort_by')}</DropdownMenuLabel>
                {SORT_OPTIONS.map(o => (
                  <DropdownMenuItem
                    key={o.value}
                    onSelect={() => { setSortKey(o.value); safeSetItem(SORT_LS_KEY, o.value) }}
                  >
                    <span className="flex-1">{i18nT(SORT_LABEL_KEY[o.value])}</span>
                    {sortKey === o.value && <Check size={14} className="text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
                {/* Tags. Placed above Folders and NOT gated on the lane: tags are
                    a property of the session, so they mean the same thing in the
                    flat list, the folder tree and the board — and the board is
                    exactly where a phone user is most likely to want this, since
                    the columns scroll sideways there. Folders, by contrast, are a
                    list-view structure and stay hidden on the board. */}
                {tagFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em]">
                      {i18nT('pages.chatSidebar.tags')}
                    </DropdownMenuLabel>
                    {tagFilterRows.map(({ tag: t, count, selected }) => (
                      <DropdownMenuItem
                        key={t.id}
                        title={selected
                          ? i18nT('pages.chatSidebar.stop_filtering_by_tag', { name: t.name })
                          : i18nT('pages.chatSidebar.show_only_sessions_tagged', { name: t.name })}
                        // Keep the menu open so several tags can be selected.
                        onSelect={e => { e.preventDefault(); toggleTagFilter(t.id) }}
                        data-testid={`tag-filter-${t.id}`}
                        role="menuitemcheckbox"
                        aria-checked={selected}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={selected
                            ? { borderColor: t.color, background: t.color }
                            : { borderColor: 'var(--border)', background: 'transparent' }}
                        >
                          {selected && <Check size={10} strokeWidth={3} style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }} />}
                        </span>
                        <span className="flex-1 truncate">{t.name}</span>
                        {/* 0 is rendered, not omitted: a zero-count tag is exactly
                            the one that blanks the list when selected. */}
                        <span className="text-muted text-[11px] shrink-0">{count}</span>
                      </DropdownMenuItem>
                    ))}
                  </>
                )}
                {/* Folders sit LAST on purpose: the list grows with the user's
                    folder count, so anything below it would get pushed out of
                    easy reach. Being last, it can simply overflow into the
                    menu's own scroll (max-h on DropdownMenuContent) with no
                    inner scroll region of its own. */}
                {!boardLaneActive && folderFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    {/* The heading doubles as the shelve control: activating it
                        rolls the folder list up or down. It stays a menu item so
                        keyboard users reach it with the same arrow keys as every
                        other row, and preventDefault keeps the menu open. */}
                    <DropdownMenuItem
                      onSelect={e => { e.preventDefault(); toggleFoldersShelved() }}
                      data-testid="folder-filter-shelve"
                      aria-expanded={!foldersShelved}
                      title={foldersShelved ? i18nT('pages.chatSidebar.show_the_folder_list') : i18nT('pages.chatSidebar.roll_the_folder_list_up')}
                      className="text-[11px] uppercase tracking-[.04em] text-muted"
                    >
                      <DisclosureChevron open={!foldersShelved} size={12} />
                      <span className="flex-1">
                        {i18nT('pages.chatSidebar.folders')}
                        {filterHiddenFolders.size > 0 && (
                          <span className="normal-case tracking-normal"> · {filterHiddenFolders.size} {i18nT('pages.chatSidebar.hidden')}</span>
                        )}
                      </span>
                    </DropdownMenuItem>
                    {!foldersShelved && (
                      <>
                    {filterHiddenFolders.size > 0 && (
                      <DropdownMenuItem onSelect={e => { e.preventDefault(); showAllFolders() }} data-testid="folder-filter-show-all">
                        <RotateCcw size={12} className="text-muted shrink-0" />
                        <span className="flex-1">{i18nT('pages.chatSidebar.show_all_folders')}</span>
                      </DropdownMenuItem>
                    )}
                    {folderFilterRows.map(({ folder: f, depth, count, hidden, hiddenByAncestor }) => (
                      <DropdownMenuItem
                        key={f.id}
                        style={{ paddingLeft: `${8 + depth * 14}px` }}
                        title={hiddenByAncestor
                          ? i18nT('pages.chatSidebar.hidden_because_parent_hidden', { name: f.name })
                          : hidden ? i18nT('pages.chatSidebar.show_in_flat_view', { name: f.name }) : i18nT('pages.chatSidebar.hide_from_flat_view', { name: f.name })}
                        // Keep the menu open so several folders can be toggled.
                        onSelect={e => { e.preventDefault(); toggleFolderFilter(f.id) }}
                        data-testid={`folder-filter-${f.id}`}
                        role="menuitemcheckbox"
                        aria-checked={!hidden && !hiddenByAncestor}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={hidden || hiddenByAncestor
                            ? { borderColor: 'var(--border)', background: 'transparent' }
                            : { borderColor: 'var(--accent)', background: 'var(--accent)' }}
                        >
                          {!hidden && !hiddenByAncestor && <Check size={10} className="text-accent-fg" strokeWidth={3} />}
                        </span>
                        <FolderGlyph color={f.color} size={12} className="shrink-0 text-muted" />
                        <span className={`flex-1 truncate${hiddenByAncestor ? ' opacity-50' : ''}`}>{f.name}</span>
                        {count > 0 && <span className="text-muted text-[11px] shrink-0">{count}</span>}
                      </DropdownMenuItem>
                    ))}
                      </>
                    )}
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>
      {/* One aggregate chip in its OWN row, never per-tag chips in the row below.
          AUTOSDE max-two-buttons-per-row grandfathers that row's existing filter
          chips but forbids growing it, and per-tag chips grow it without bound.
          Tag colours survive as spans inside this single control. */}
      {activeTagIds.size > 0 && (
        <div className="px-3 pb-1">
          <button
            type="button"
            data-testid="tag-filter-chip"
            className="inline-flex items-center gap-1 max-w-full pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors bg-bg-elevated/60 border border-border text-muted hover:text-text"
            onClick={clearTagFilter}
            title={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
            aria-label={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
          >
            {/* Swatch carries the colour, the name stays in body text: a pale
                tag on this surface can fall near 2:1 contrast at 11px. */}
            <span className="truncate inline-flex items-center gap-1.5">
              {tagFilterRows.filter(({ tag: t }) => activeTagIds.has(t.id)).map(({ tag: t }) => (
                <span key={t.id} className="inline-flex items-center gap-1">
                  <span
                    aria-hidden="true"
                    className="w-2 h-2 shrink-0 rounded-full border border-border"
                    style={{ background: t.color }}
                  />
                  {t.name}
                </span>
              ))}
            </span>
            <X size={11} className="shrink-0" />
          </button>
        </div>
      )}
      {activeFilters.size > 0 && (
        <div className="px-3 pb-1 flex items-center gap-1.5 flex-wrap">
          {SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key)).map(filterDef => {
            const slotCount = filterCounts[filterDef.key] ?? 0
            const filterLabel = i18nT(FILTER_LABEL_KEY[filterDef.key])
            // The label goes in as-is. It used to be `.toLowerCase()`d to read as
            // mid-sentence English, which does not survive translation: German
            // nouns are capitalised, CJK has no case, and Turkish lowercases `I`
            // to a dotless `ı`.
            const clearLabel = i18nT('pages.chatSidebar.clear_named_filter', { filter: filterLabel })
            return (
              <button
                key={filterDef.key}
                type="button"
                className="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors"
                style={{ background: `color-mix(in srgb, ${filterDef.color} 10%, transparent)`, color: filterDef.color, borderWidth: 1, borderColor: `color-mix(in srgb, ${filterDef.color} 30%, transparent)` }}
                onClick={() => toggleFilter(filterDef.key)}
                title={clearLabel}
                aria-label={clearLabel}
              >
                {filterLabel}{filterDef.key === 'recent' ? ` · ${formatRecentWindow(recentWindowMs)}` : ''}{slotCount > 0 ? ` (${slotCount})` : ''}
                <X size={11} />
              </button>
            )
          })}
        </div>
      )}
      {seedError && (
        /* Outside the layout branches on purpose. A TOTAL seed failure leaves
         * zero columns, so the board branch never renders — a banner inside it
         * would be invisible in exactly the case it exists for, while the
         * toggle has already flipped and the user is looking at a list. */
        <div
          data-testid="lane-seed-error"
          role="status"
          className="mx-2 mt-2 px-2.5 py-1.5 rounded-md border text-[12px] shrink-0"
          style={{ borderColor: 'var(--warn)', color: 'var(--warn)' }}
        >
          {i18nT('pages.chatSidebar.lane_seed_failed')}
          <button
            type="button"
            className="ml-2 underline bg-transparent border-none cursor-pointer p-0"
            style={{ color: 'var(--warn)' }}
            onClick={() => { setSeedError(''); seedStateLanesMutation.mutate() }}
          >
            {i18nT('pages.chatSidebar.lane_seed_retry')}
          </button>
        </div>
      )}
      <LayoutGroup id="chat-slots">
        {flatView && folders.length > 0 ? (
          // Flat view: every chat exploded out of its folder into one lane.
          // Removes only the folder rendering hierarchy — sort, pin priority,
          // filters, and search all apply as usual (filteredSlots). No folder
          // tree. Takes precedence over the tag-columns layout.
          // Inactive without folders (the toggle is hidden then too), so a
          // persisted flat preference can never strand the user.
          //
          // Its DndContext carries EXACTLY ONE target: the chat pane. No
          // SortableContext and no folder droppables are registered, so
          // dragging a session into the open chat works here just as it does in
          // the tree, while row order stays a pure function of the sort key —
          // there is nothing for a drop inside the lane to land on. (Order is
          // the reason: a flat lane spans every folder, so a manual position
          // would have no place to be stored.) `sidebarCollision` also keeps the
          // pane out of its closestCenter fallback, so a release inside the
          // sidebar resolves to no target rather than snapping to the pane.
          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
            measuring={dndMeasuring}
            onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
            {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
              && createPortal(
                <ChatPaneDropZone refusal={draggingRefRefusal} />,
                chatDropTarget,
              )}
            <motion.div layoutScroll className="flex-1 min-h-0 overflow-y-auto scrollbar-none p-2 flex flex-col" style={{ scrollbarWidth: 'none' }} data-testid="flat-view-lane">
              {(() => {
                // Date segments (Today / Yesterday / Last 7 Days / …) between
                // rows — resurrects the 9bb0f71 active-list pattern: only for
                // date sorts (segments mislead on name/created order, same
                // guard as the history pane), and pinned rows render first
                // without segments since pinning overrides date order.
                const isDateSort = sortKey === 'date-desc' || sortKey === 'date-asc'
                const segOf = (s: Slot) => isDateSort && !pinned.has(s.key) ? dateSegment(slotActivityTs(s)) : ''
                let prevSeg = ''
                return flatSlots.map((s, i) => {
                  const seg = segOf(s)
                  const showHeader = seg !== '' && seg !== prevSeg
                  if (seg) prevSeg = seg
                  const next = i < flatSlots.length - 1 ? flatSlots[i + 1] : null
                  const nextIsActive = next != null && activeSlot === next.key
                  const isActive = activeSlot === s.key
                  // No divider before a segment header — the header separates.
                  const nextSeg = next ? segOf(next) : seg
                  const showDivider = next != null && !isActive && !nextIsActive && nextSeg === seg
                  return (
                    <Fragment key={s.key}>
                      {showHeader && (
                        <div data-testid="date-segment-header" className="px-3 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {renderSessionRow(s, 0, showDivider, 'flat')}
                    </Fragment>
                  )
                })
              })()}
              {flatSlots.length === 0 && (
                <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
              )}
              {/* Flat view has no containers to anchor to — every hide, top-level
               *  or nested, collapses into this one row at the bottom of the lane. */}
              {renderHiddenReveal('flat', allHiddenFolders, 0)}
            </motion.div>
            {dragOverlay}
          </DndContext>
        ) : orderedColumns.length === 0 ? (
          // Legacy single-lane layout (identical to pre-columns behavior)
          // Scrollbar hidden (scrollbar-none + inline scrollbarWidth covers
          // Firefox, modern WebKit, and Safari <16) to match the app rail in
          // App.tsx: on macOS with "always show scrollbars" this lane is
          // permanently scrollable, so the 6px track was a fixed stripe down
          // the sidebar rather than a transient hint. Scrolling itself is
          // untouched — wheel, trackpad, keyboard, and drag-autoscroll all
          // still work, and the list's own overflow is still the affordance.
          <motion.div layoutScroll className="flex-1 min-h-0 overflow-y-auto scrollbar-none p-2 flex flex-col" style={{ scrollbarWidth: 'none' }}>
            {/* One DndContext owns folder reorder (sortable) + session drag-to-
             *  assign (draggable rows + droppable folder/root targets). */}
            <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
              measuring={dndMeasuring}
              onDragStart={handleSidebarDragStart} onDragOver={handleSidebarDragOver} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
              {/* "Drag a session into the open chat" target. Portaled into
               *  ChatPage's pane so it covers the WHOLE conversation area (not
               *  just the composer), while staying inside this DndContext —
               *  React portals preserve context, and useDroppable measures the
               *  node where it actually renders. Mounted only during a session
               *  drag, and only when a pane and a handler exist. */}
              {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
                && createPortal(
                  <ChatPaneDropZone refusal={draggingRefRefusal} />,
                  chatDropTarget,
                )}
              {/* Root lane is the fallback drop target: dropping a session on
               *  empty space (not over a folder) ungroups it (folderId: null). */}
              <DndDroppable id="root-lane" data={{ type: 'folder-drop', folderId: null }}>
                {({ setNodeRef }) => (
                  <div ref={setNodeRef} className="flex flex-col flex-1 min-h-0">
                    <SortableContext items={rootFolderIds} strategy={verticalListSortingStrategy}>
                      {visibleRootFolders.map(f => <SortableFolderBlock key={f.id} folder={f} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} renderFolderBlock={renderFolderBlock} />)}
                    </SortableContext>
                    {/* Bottom of the ROOT folder list. For a top-level hide this
                     *  is the sidebar's own bottom, which is exactly the "single
                     *  footer row" shape — the nested case is what needs depth. */}
                    {renderHiddenReveal('root', hiddenByContainer.get('root') ?? [], 0)}
                    {/* Every folder block and the ungrouped bucket read
                        filteredSlots, so an empty one means nothing can render
                        below — say so rather than leaving a blank lane. */}
                    {filteredSlots.length === 0 && listNarrowed && (
                      <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
                    )}
                    {/* Ungrouped sessions live in a headerless droppable bucket
                     *  (folderId: null) that fills the remaining height below the
                     *  folders, so the whole empty lower area is a drop target —
                     *  dropping a session here ungroups it. The ring only lights up
                     *  while dragging a foldered session (when ungrouping applies). */}
                    {(rootFolders.length > 0 || ungroupedSlots.length > 0) && (
                      <DndDroppable id="root-group" data={{ type: 'folder-drop', folderId: null }}>
                        {({ setNodeRef: setRootGroupRef, isOver }) => (
                          <div ref={setRootGroupRef} className={`flex flex-col flex-1 min-h-0 rounded-md transition-all ${isOver && (draggingFolderedSession || draggingNestedFolder) ? 'ring-1 ring-accent' : ''}`}>
                            {/* Explicit un-nest target while dragging a subfolder —
                             *  same escape hatch (and wording) as the session zone
                             *  below, always reachable even when the root lane has
                             *  no empty space. */}
                            {draggingNestedFolder && <RootDropHint />}
                            {ungroupedSlots.map((s, i) => {
                              const nextIsActive = i < ungroupedSlots.length - 1 && activeSlot === ungroupedSlots[i + 1].key
                              const isActive = activeSlot === s.key
                              const showDivider = i < ungroupedSlots.length - 1 && !isActive && !nextIsActive
                              return renderSessionRow(s, 0, showDivider)
                            })}
                            {ungroupedSlots.length === 0 && draggingFolderedSession && <RootDropHint />}
                          </div>
                        )}
                      </DndDroppable>
                    )}
                  </div>
                )}
              </DndDroppable>
              {dragOverlay}
            </DndContext>
          </motion.div>
        ) : (
          // Trello-style horizontal column strip
          <div className="flex-1 min-h-0 flex flex-col">
          <div className="flex-1 overflow-x-auto overflow-y-hidden flex gap-2 p-2" data-testid="column-strip">
            {orderedColumns.map((col, colIdx) => {
              const colSlots = filteredSlots.filter(s => columnMatches(col, s))
              const colTags = col.tag_ids.map(tid => tagById[tid]).filter(Boolean) as ChatTag[]
              const laneDef = col.source === 'state' ? SESSION_LANES.find(l => l.key === col.state_key) : undefined
              // Only a single-status-tag column can accept a card: dropping onto a
              // derived lane has nothing to write (the backend refuses it too).
              const isStatusLane = !laneDef && colTags.length === 1 && !!colTags[0].status
              return (
                // Board column is a drag-and-drop drop zone (column reorder + session
                // card drop); mouse-only drag handlers, so scope-disable the rule.
                // eslint-disable-next-line jsx-a11y/no-static-element-interactions
                <div key={col.id} data-testid={`column-${col.id}`} className="flex flex-col flex-1 min-w-[220px] bg-card border border-border rounded-md overflow-hidden"
                  onDragOver={e => {
                    const types = e.dataTransfer.types
                    // Accept column reorder on the entire column surface
                    if (types.includes('application/mc-column')) {
                      e.preventDefault()
                      return
                    }
                    // Accept session-card drop only on status lanes
                    if (isStatusLane && types.includes('text/plain')) {
                      e.preventDefault()
                      e.currentTarget.classList.add('ring-1', 'ring-accent')
                    }
                  }}
                  onDragLeave={e => { e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
                  onDrop={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    // Column reorder takes priority
                    const draggedCol = e.dataTransfer.getData('application/mc-column')
                    if (draggedCol && draggedCol !== col.id) {
                      e.preventDefault()
                      const ids = orderedColumns.map(c => c.id).filter(id => id !== draggedCol)
                      ids.splice(colIdx, 0, draggedCol)
                      reorderColumnsMutation.mutate(ids)
                      return
                    }
                    if (!isStatusLane) return
                    e.preventDefault()
                    const k = e.dataTransfer.getData('text/plain')
                    if (k) dropSlotMutation.mutate({ slot: k, columnId: col.id })
                  }}>
                  <div className="flex items-center gap-1 p-2 border-b border-border bg-bg-elevated">
                    {/* Reorder handle: mouse-only drag source for column reordering. */}
                    {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
                    <span draggable
                      className="cursor-grab text-muted hover:text-text shrink-0"
                      onDragStart={e => { e.dataTransfer.setData('application/mc-column', col.id); e.dataTransfer.effectAllowed = 'move' }}
                      title={i18nT('pages.chatSidebar.drag_to_reorder')}>
                      <GripVertical size={12} />
                    </span>
                    <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
                      {laneDef ? (
                        // A lane's identity is its runtime state, so it shows a
                        // fixed name and accent rather than tag chips — there is
                        // no filter behind it for the user to edit.
                        <span className="inline-flex items-center gap-1.5 min-w-0" title={i18nT('pages.chatSidebar.lane_derived_hint')}>
                          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: laneDef.color }} aria-hidden />
                          <span className="text-[11px] font-semibold uppercase tracking-wider truncate" style={{ color: laneDef.color }}>{i18nT(laneDef.labelKey)}</span>
                        </span>
                      ) : colTags.length === 0 ? (
                        <span className="text-[11px] text-muted font-semibold uppercase tracking-wider">{col.name || (col.include_untagged ? i18nT('pages.chatSidebar.untagged_2') : i18nT('pages.chatSidebar.all_sessions'))}</span>
                      ) : (
                        <>
                          {colTags.map(t => (
                            <span key={t.id} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>{t.name}</span>
                          ))}
                          {col.include_untagged && <span className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border border-dashed border-muted text-muted" title={i18nT('pages.chatSidebar.also_shows_untagged_sessions')}>{i18nT('pages.chatSidebar.untagged')}</span>}
                        </>
                      )}
                      {col.name && !laneDef && colTags.length > 0 && <span className="text-[11px] text-muted ml-1">· {col.name}</span>}
                      {/* A bare match-all column beside the lanes shows every
                        * session again, so the counts stop summing and cards
                        * appear twice. Seeding deliberately does not delete it
                        * (it is indistinguishable from a column the user added),
                        * so say what it is and let them decide. */}
                      {!laneDef && colTags.length === 0 && !col.name && !col.include_untagged
                        && orderedColumns.some(c => c.source === 'state') && (
                        <span data-testid={`column-duplicates-hint-${col.id}`} className="text-[10px] text-muted ml-1 truncate">
                          · {i18nT('pages.chatSidebar.lane_legacy_column_hint')}
                        </span>
                      )}
                    </div>
                    <span className="text-[11px] text-muted shrink-0">{colSlots.length}</span>
                    <button type="button" data-testid={`column-new-folder-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.new_folder')} aria-label={i18nT('pages.chatSidebar.new_folder')} onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }}><FolderPlus size={12} /></button>
                    {!laneDef && <button type="button" data-testid={`column-edit-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.filter_manage_tags')} aria-label={i18nT('pages.chatSidebar.filter_manage_tags')} onClick={() => setColumnEditId(columnEditId === col.id ? null : col.id)}><TagIcon size={12} /></button>}
                    <button
                      type="button"
                      data-testid={`column-add-after-${col.id}`}
                      className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px] disabled:cursor-wait disabled:opacity-50"
                      title={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      aria-label={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      disabled={addColumnAfterMutation.isPending}
                      onClick={() => addColumnAfterMutation.mutate(col.id)}
                    ><Plus size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-delete-${col.id}`}
                      className="text-muted hover:text-danger bg-transparent border-none cursor-pointer shrink-0 p-[2px]"
                      title={i18nT('pages.chatSidebar.delete_column')}
                      aria-label={i18nT('pages.chatSidebar.delete_column')}
                      onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_this_column'))) deleteColumnMutation.mutate(col.id) }}
                    ><X size={12} /></button>
                  </div>
                  {/* Column filter popover — portaled to <body> so the column's
                      overflow-hidden ancestor cannot clip it; viewport-anchored
                      to the edit button via popoverPos. */}
                  {columnEditId === col.id && popoverPos && createPortal(
                    /* Non-modal disclosure: role=dialog + a Tab-trap contains keyboard
                       focus, but we deliberately omit aria-modal — the popover has no
                       backdrop and is outside-click-dismissible, so claiming the rest of
                       the page is inert would mislead screen readers. */
                    <div ref={columnPopoverRef} role="dialog" aria-label={i18nT('pages.chatSidebar.filter_tags', { name: col.name || 'column' })} tabIndex={-1} data-column-popover={col.id}
                      className="fixed z-[9100] bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px] outline-none"
                      style={{ top: popoverPos.top, left: popoverPos.left }}
                      onClick={e => e.stopPropagation()}
                      onKeyDown={e => {
                        if (e.key === 'Escape') { e.stopPropagation(); closeColumnPopover(col.id); return }
                        if (e.key !== 'Tab') return
                        // Trap Tab within the dialog — portal content sits at the end of
                        // <body>, so without this Tab would jump into unrelated page chrome.
                        const root = columnPopoverRef.current
                        if (!root) return
                        const f = Array.from(root.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])'))
                        if (f.length === 0) return
                        const first = f[0], last = f[f.length - 1]
                        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
                        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
                      }}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">{i18nT('pages.chatSidebar.column_filter')}</span>
                        <button className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0" onClick={() => closeColumnPopover(col.id)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
                      </div>
                      <Input className="w-full py-1 text-[12px] mb-2" placeholder={i18nT('pages.chatSidebar.column_name_optional')} defaultValue={col.name} onBlur={e => { const v = e.target.value.trim(); if (v !== col.name) updateColumnMutation.mutate({ id: col.id, body: { name: v } }) }} />
                      <div className="flex items-center gap-1 mb-2" role="radiogroup" aria-label={i18nT('pages.chatSidebar.match_mode')}>
                        {(['any', 'all', 'none'] as const).map(m => (
                          <button key={m} role="radio" aria-checked={col.mode === m} className={`text-[11px] px-2 py-0.5 rounded cursor-pointer border transition-all ${col.mode === m ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text'}`} onClick={() => updateColumnMutation.mutate({ id: col.id, body: { mode: m } })}>{m}</button>
                        ))}
                      </div>
                      <label htmlFor={`column-include-untagged-${col.id}`} className="flex items-center gap-2 px-1 py-1 mb-2 text-[11px] text-muted cursor-pointer select-none hover:text-text" title={i18nT('pages.chatSidebar.also_show_sessions_that_have_no_tags_at_all')}>
                        <input
                          type="checkbox"
                          id={`column-include-untagged-${col.id}`}
                          data-testid={`column-include-untagged-${col.id}`}
                          aria-label={i18nT('pages.chatSidebar.include_untagged_sessions')}
                          checked={!!col.include_untagged}
                          onChange={e => updateColumnMutation.mutate({ id: col.id, body: { include_untagged: e.target.checked } })}
                          className="cursor-pointer"
                        />
                        {i18nT('pages.chatSidebar.include_untagged_sessions')}
                      </label>
                      <TagManagerList
                        mode="column-filter"
                        selectedIds={col.tag_ids}
                        onToggleTag={(_tagId, nextIds) => updateColumnMutation.mutate({ id: col.id, body: { tag_ids: nextIds } })}
                        createTestId={`tag-create-${col.id}`}
                      />
                      <div className="mt-2 flex justify-end">
                        <button className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { updateColumnMutation.mutate({ id: col.id, body: { tag_ids: [] } }) }}>{i18nT('pages.chatSidebar.clear_filter')}</button>
                      </div>
                    </div>,
                    document.body
                  )}
                  <div className="flex-1 overflow-y-auto scrollbar-none p-1.5 flex flex-col" style={{ scrollbarWidth: 'none' }}>
                    {/* No onDrop here: folder assignment only changes via folder-header drop.
                        Cross-column drops are handled by the OUTER column onDrop
                        (which only mutates status tags, keeping folder_id intact). */}
                    {(() => {
                      const colSlotKeys = new Set(colSlots.map(s => s.key))
                      // Show ALL root folders as drop targets, not only those with matching slots.
                      // Empty folders render with "0" count so users see the structure they built.
                      // Root folders in explicit `order`-field order (the sorted
                      // rootFolders memo, same source as list view). Rendering the
                      // raw cache array here made drops appear to revert: a reorder
                      // only rewrites `order` values (array positions are
                      // unchanged), so an unsorted render ignored the new order.
                      const relevantFolders = rootFolders
                      const ungrouped = colSlots.filter(s => !slotFolders[s.key] || !folders.find(f => f.id === slotFolders[s.key]))
                      const hasAny = colSlots.length > 0 || folders.length > 0
                      return (
                        <>
                          {/* Folder reorder in board view: one DndContext per
                           *  column (folder ids stay unique within it) + the
                           *  header as drag handle. Reorders flow through the
                           *  same global reorderFolders() as list view, so order
                           *  is consistent across columns. Native session-card
                           *  drop (HTML5 DnD) is untouched — it uses drag events,
                           *  not the pointer sensor. */}
                          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }} onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
                            <SortableContext items={relevantFolders.map(f => f.id)} strategy={verticalListSortingStrategy}>
                              {relevantFolders.map(f => <SortableColumnFolder key={f.id} folder={f} columnId={col.id} colSlotKeys={colSlotKeys} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} renderColumnFolder={renderColumnFolder} />)}
                            </SortableContext>
                            {/* Compact ghost follows the pointer while a folder drags —
                             *  same visual as the list-view overlay. DragOverlay renders
                             *  null unless THIS column's DndContext has an active drag,
                             *  so per-column overlays never stack. Portaled to
                             *  document.body: the sidebar rides inside OverlayDrawer's
                             *  morph clip-path, and a clip-path clips fixed-position
                             *  descendants too, so an in-place overlay is erased the
                             *  moment the ghost strays past the drawer edge. React
                             *  portals preserve context, so the overlay still reads
                             *  THIS column's active drag. */}
                            {createPortal(
                              <DragOverlay dropAnimation={null}>
                                {activeDrag?.type === 'folder' ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} /> : null}
                              </DragOverlay>,
                              document.body,
                            )}
                          </DndContext>
                          {ungrouped.map((s, i) => {
                            const isActive = activeSlot === s.key
                            const nextIsActive = i < ungrouped.length - 1 && activeSlot === ungrouped[i + 1].key
                            const showDivider = i < ungrouped.length - 1 && !isActive && !nextIsActive
                            return renderSessionRow(s, 0, showDivider, col.id)
                          })}
                          {!hasAny && <div className="text-muted text-[12px] text-center py-4">{i18nT('pages.chatSidebar.no_sessions')}</div>}
                        </>
                      )
                    })()}
                  </div>
                </div>
              )
            })}
          </div>
          </div>
        )}
      </LayoutGroup>

      {/* When expanded: doubles as the resize handle (accent on hover, drag to resize, dbl-click to collapse).
          When collapsed: just a static 1px divider between sessions and the Older Sessions footer. */}
      {historyOpen ? (
        // Separator that doubles as a Pointer-Events resize handle (drag,
        // mouse/touch/pen) / collapse (double-click); no keyboard analogue.
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label={i18nT('pages.chatSidebar.resize_history_pane')}
          {...historyResize}
          onDoubleClick={() => setHistoryOpen(false)}
          className="relative h-[6px] cursor-ns-resize z-10 group/drag flex items-center justify-center select-none"
          style={{ touchAction: 'none' }}
        >
          <div className={`w-full transition-all duration-200 ${historyDragging ? 'h-[2px] bg-accent-hover' : 'h-px bg-border group-hover/drag:h-[2px] group-hover/drag:bg-accent'}`} />
        </div>
      ) : (
        <div className="border-t border-border" />
      )}
      {/* Older Sessions footer — the persistent collapse/expand header for the
          history pane. Whole row is the click target; the Clear button stops
          propagation. */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) dispatch(fetchHistory(false)) }}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setHistoryOpen(!historyOpen); if (!historyOpen) dispatch(fetchHistory(false)) } }}
        /* pt/pb are 14px, not py-3, so this row's top border lands on the same
           baseline as the nav rail's community row ("Star us · Report issue"):
           both cards sit 8px off the shell floor, the rail spends 8+2+24+10 =
           44px below its own hairline, and 14+16+14 matches that exactly. The
           symmetric padding is what keeps the clock and label optically centred
           in the band. */
        className="flex justify-between items-center px-3 pt-[14px] pb-[14px] cursor-pointer select-none"
        aria-expanded={historyOpen}
        aria-controls="history-pane"
        aria-label={i18nT('pages.chatSidebar.older_sessions')}
      >
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong leading-none">
          <Clock size={14} className="shrink-0" />
          <span className="leading-none">{i18nT('pages.chatSidebar.older_sessions_2')}</span>
        </span>
        {/* Chevron trails the Clear button so the disclosure glyph is the
            rightmost control, and Clear shifts left by the gap rather than
            being pushed off the row's 12px right inset. The gap is 12px, wider
            than the row's other spacing: Clear is destructive (it wipes closed
            sessions behind a single confirm), so a pointer aimed at the collapse
            glyph must not land on it. This trailing position is the pane's ONE
            deliberate exception to the sidebar's leading-chevron grammar
            (#2887): a section header ends with its own disclosure glyph, while
            row-level disclosures (group headers, hidden-folders reveal, the
            folders filter row) lead with theirs like tree rows everywhere else.
            All four share the same mechanic: a ChevronRight that rotates 90°
            when open — never a Right/Down glyph swap, never a counter-rotation
            when closed. */}
        <span className="flex items-center gap-3 shrink-0">
          {historyOpen && history.length > 0 && (
            <button
              className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all"
              onClick={async e => { e.stopPropagation(); if (confirm(i18nT('pages.chatSidebar.clear_closed_sessions_active_tabs_and_pinned_ses'))) { await api.clearSessions(); dispatch(fetchHistory(false)) } }}
            >{i18nT('pages.chatSidebar.clear')}</button>
          )}
          <DisclosureChevron open={historyOpen} size={16} className="text-text-strong" />
        </span>
      </div>
      <AnimatePresence initial={false}>
        {historyOpen && (
          <motion.div
            id="history-pane"
            key="history-pane"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <div className="px-2 pb-1">
              <div className="relative">
                <SearchInput className="w-full" placeholder={i18nT('pages.chatSidebar.search_older_sessions')} value={historyFilter} onChange={e => setHistoryFilter(e.target.value)} />
                {historyFilter && (
                  <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setHistoryFilter('')} aria-label={i18nT('pages.chatSidebar.clear_search')}><X size={13} /></button>
                )}
              </div>
            </div>
            {/* scroll-shadow already fades the top/bottom edge as its
             *  scrollability cue, so the bar itself is redundant here. */}
            <div className="overflow-y-auto scrollbar-none p-2 scroll-shadow" style={{ height: `${historyHeight}px`, scrollbarWidth: 'none' }}>
              {(() => {
                const historyLocalMatch = (s: { title?: string; key: string }) =>
                  ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                // Additive rather than a boolean OR: here the backend result IS the
                // source list, so filtering `history` instead would drop backend-only hits.
                const filteredHistory = (() => {
                  if (!historyFilter) return history
                  if (historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults) {
                    const seen = new Set(historySearchResults.map(s => s.key))
                    return [...historySearchResults,
                            ...history.filter(s => !seen.has(s.key) && historyLocalMatch(s))]
                  }
                  return (historySearchResults ?? history).filter(historyLocalMatch)
                })()
                // One definition of "search active" for every site below: results
                // are present AND the query is still at/above the search threshold.
                // The compound check matters on the clear-X frame: historyFilter
                // empties synchronously but useDebouncedSessionSearch nulls its
                // result in a passive effect (one render later), so a bare
                // `historySearchResults` test would treat that stale frame as an
                // active search and paint date segment headers over a
                // relevance-ordered list.
                const searchActive = historyFilter.trim().length >= SEARCH_MIN_CHARS && !!historySearchResults
                // Hide date segments when the user has an active search — results are
                // Segments only make sense when the list is date-ordered. For name/created
                // sorts (or active search, which is relevance-ranked) they'd interleave.
                const showSegments = !searchActive
                  && (sortKey === 'date-desc' || sortKey === 'date-asc')
                // Active search: keep the backend's relevance ranking (title-boosted;
                // see search_sessions in history.py). Re-sorting search results by the
                // sidebar sort key buried an exact title match under fresher sessions
                // that merely mention the query in their content — and defeated
                // groupHistoryByFolder's documented order-preserving contract. The
                // command palette's Sessions tab already preserves backend order.
                // No search: skip the sort only when the backend already returns
                // date-desc order.
                const sortedHistory = (searchActive || sortKey === 'date-desc') ? filteredHistory : [...filteredHistory].sort((a, b) => compareBySort(a, b, sortKey))
                // An empty pane is reachable whenever every session on disk is
                // already open as a tab (the common case for a light user), so it
                // needs to say so rather than render a search box over blank space.
                // A filtered-to-nothing list is a different statement and reuses the
                // wording the two sibling panes already use for it.
                if (sortedHistory.length === 0) {
                  return (
                    <div className="px-3 py-4 text-[12px] text-muted text-center">
                      {historyFilter
                        ? i18nT('pages.chatSidebar.no_sessions_match')
                        : i18nT('pages.chatSidebar.no_older_sessions')}
                    </div>
                  )
                }
                let prevSeg = ''
                // Derive agent color the same way renderSessionRow does so history rows
                // match the session-row visual language (agent name tinted by source).
                const agentColorFor = (agentName: string): string => {
                  const meta = installedAgents.find(a => a.name === agentName)
                  if (meta?.source === 'package') return 'text-[var(--aim)]'
                  if (meta?.source === 'builtin') return 'text-muted'
                  return 'text-muted'
                }
                const historyRow = (s: (typeof sortedHistory)[number]) => {
                  const displayDate = fmtRelativeTime(s.modified ?? s.created)
                  const agentName = s.agent || defaultAgent || ''
                  const agentColor = agentColorFor(agentName)
                  const isDashboard = s.key.startsWith('dashboard')
                  const channel = slotChannelNamespace(s.key)
                  const surfaceLabel = isDashboard
                    ? i18nT('pages.chatSidebar.dashboard_source')
                    : slotChannelLabel(s.key) || i18nT('pages.chatSidebar.session_source')
                  // Federated-search row from a connected remote instance: its
                  // transcript lives on the other gateway, so activation switches
                  // to that instance's pane instead of resuming a (same-keyed but
                  // unrelated) local session, and the local delete action is
                  // hidden — deleteHistorySession would target the LOCAL file.
                  const remoteInstanceId = (s as { instance_id?: string }).instance_id
                  const remoteInstanceName = (s as { instance_name?: string }).instance_name
                  const activateRow = () => {
                    if (remoteInstanceId) { selectInstance(remoteInstanceId); return }
                    dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
                  }
                  return (
                    <div className={`group relative flex items-start gap-2.5 pr-4 py-2 rounded-md text-sm transition-all select-none ${!connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'}`} style={{ paddingLeft: '10px' }} title={s.title || s.key} {...offlineProps(connected, 'resume sessions')} role="button" tabIndex={0} aria-disabled={!connected} onKeyDown={e => {
                      // WCAG 2.1.1: history rows must be resumable via keyboard.
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      if ((e.target as HTMLElement) !== e.currentTarget) return
                      e.preventDefault()
                      if (!connected) return
                      activateRow()
                    }} onMouseDown={e => {
                      // NOTE: pointer activation lives on onMouseDown (not onClick). For a
                      // div[role="button"], browsers do NOT synthesize a click from Enter
                      // (that only happens for native buttons/links — hence the onKeyDown
                      // handler above), and AT activation (e.g. VoiceOver VO+Space)
                      // synthesizes a click INSTEAD of key events. So each path activates
                      // exactly once. Do NOT add an e.detail === 0 guard here or in any
                      // future onClick: AT-synthesized clicks have detail 0 and would be
                      // silently dropped, breaking screen-reader activation.
                      e.preventDefault()
                      if ((e.target as HTMLElement).closest?.('[data-close]')) { if (!remoteInstanceId && confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(s.key)); return }
                      if (!connected) return
                      activateRow()
                    }}>
                      {/* Platform glyph — fills the left column that session rows reserve for the unread dot */}
                      <span role="img" className="shrink-0 flex items-center justify-center self-center text-muted" title={surfaceLabel} aria-label={surfaceLabel}>
                        {isDashboard
                          ? <Monitor size={12} />
                          : channel === 'unified'
                            ? <MessageSquare size={12} />
                            : <ChannelBrandIcon channel={channel ?? ''} size={12} />
                        }
                      </span>
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <div className={`session-agent-label text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
                          <span className="truncate">{agentName || '\u00A0'}</span>
                          {remoteInstanceName && <span className="shrink-0 text-[10px] px-1 rounded bg-bg-muted text-muted border border-border" title={remoteInstanceName}>{remoteInstanceName}</span>}
                          {s.clean_mode
                            ? <span className="text-accent" title={i18nT('pages.chatSidebar.clean_agent_only_no_kirocrew_context_or_mcp')}><Droplet size={10} /></span>
                            : <>
                                {s.memory_mode === 'incognito' && <span className="text-muted" title={i18nT('pages.chatSidebar.incognito_no_memory_writes')}><EyeOff size={10} /></span>}
                                {s.memory_mode === 'temporary' && <span className="text-aim" title={i18nT('pages.chatSidebar.temporary_no_memory_reads_or_writes')}><VenetianMask size={10} /></span>}
                              </>}
                          {displayDate && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{displayDate}</span>}
                        </div>
                        <div className="text-[13px] leading-snug line-clamp-2 break-words">{s.title || s.key}</div>
                      </div>
                      {/* Floating hover button group — matches session-row pattern.
                          Hidden for remote rows: deleteHistorySession targets the
                          LOCAL session file, which for a remote row is at best a
                          same-keyed unrelated conversation. */}
                      {!remoteInstanceId && <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
                        <button type="button" title={i18nT('pages.chatSidebar.delete_history_session')} aria-label={i18nT('pages.chatSidebar.delete_history_session')} className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger hover:bg-danger-subtle transition-all bg-transparent border-none" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); if (confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(s.key)) }}><X size={12} /></button>
                      </div>}
                    </div>
                  )
                }
                // Folder-grouped view: during an active content search, regroup the
                // relevance-ranked results under collapsible folder headers (+ Unfiled)
                // by the folder each session was filed in, instead of date segments.
                if (searchActive) {
                  return groupHistoryByFolder(sortedHistory, folders).map(({ key: gid, folder, rows }) => {
                    const collapsed = collapsedHistoryGroups.has(gid)
                    const groupName = folder ? folder.name : i18nT('pages.chatSidebar.unfiled')
                    return (
                      <Fragment key={gid}>
                        <button type="button" aria-expanded={!collapsed} aria-label={collapsed ? i18nT('pages.chatSidebar.expand_group_results', { group: groupName }) : i18nT('pages.chatSidebar.collapse_group_results', { group: groupName })} className="w-full flex items-center gap-1.5 px-2 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none cursor-pointer hover:text-text first:pt-1" onClick={() => setCollapsedHistoryGroups(prev => { const next = new Set(prev); if (next.has(gid)) next.delete(gid); else next.add(gid); return next })}>
                          <DisclosureChevron open={!collapsed} size={12} />
                          {folder ? <FolderGlyph color={folder.color} size={12} open={!collapsed} /> : <Folder size={12} className="text-muted shrink-0" />}
                          <span className="truncate">{folder ? folder.name : i18nT('pages.chatSidebar.unfiled')}</span>
                          <span className="ml-0.5 text-muted font-normal tabular-nums">· {rows.length}</span>
                        </button>
                        {!collapsed && rows.map((s, i) => (
                          <Fragment key={s.key}>
                            {historyRow(s)}
                            {i < rows.length - 1 && <div className="mx-3 border-b border-border" />}
                          </Fragment>
                        ))}
                      </Fragment>
                    )
                  })
                }
                return sortedHistory.map((s, idx) => {
                  const tsForSegment = s.modified ?? s.created
                  const seg = dateSegment(tsForSegment)
                  const showHeader = showSegments && seg !== prevSeg
                  prevSeg = seg
                  // Divider between consecutive rows — but not before a segment header
                  // (the header itself separates), and not after the last row.
                  const isLast = idx === sortedHistory.length - 1
                  const nextSeg = !isLast ? dateSegment(sortedHistory[idx + 1].modified ?? sortedHistory[idx + 1].created) : seg
                  const showDivider = !isLast && (!showSegments || nextSeg === seg)
                  return (
                    <Fragment key={s.key}>
                      {showHeader && (
                        <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {historyRow(s)}
                      {showDivider && <div className="mx-3 border-b border-border" />}
                    </Fragment>
                  )
                })
              })()}
              {/* Load-more uses onMouseDown+preventDefault to trigger without stealing
                  focus from the transcript; scope-disable the static-interaction rule. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              {historyHasMore && <div className="flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md" onMouseDown={e => { e.preventDefault(); dispatch(fetchHistory(true)) }}>{i18nT('pages.chatSidebar.load_more')}</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* One folder create/settings modal for the whole sidebar. Rendered here
       *  rather than per-row so a folder shown in several board columns can only
       *  ever open one, and so the ProjectPicker it hosts has a single owner. */}
      {folderModal && (
        <FolderConfigModal
          open={true}
          mode={folderModal.mode}
          parentId={folderModal.mode === 'create' ? folderModal.parentId : undefined}
          folder={folderModal.mode === 'edit' ? folders.find(f => f.id === folderModal.folderId) : undefined}
          folders={folders}
          installedAgents={installedAgents}
          globalDefaultAgent={defaultAgent}
          onClose={() => setFolderModal(null)}
          onSubmit={async draft => {
            // AWAIT the mutation and only close on success. The backend rejects a
            // free-typed project_dir (not absolute / not an existing directory /
            // sensitive) and a multi-emoji icon with a 400; closing optimistically
            // discarded the whole draft with no feedback. Rethrowing lets the modal
            // stay open and render the reason.
            if (folderModal.mode === 'create') {
              await createFolderMutation.mutateAsync({
                name: draft.name,
                parentId: folderModal.parentId || undefined,
                projectDir: draft.projectDir,
                defaultAgent: draft.defaultAgent,
                color: draft.color,
              })
            } else {
              // Build the PATCH from what the USER edited (draft.touched, measured
              // against what the modal opened with) — NOT from a diff against live
              // cache, whose shape would revert any field another client changed
              // mid-edit.
              const touched = new Set(draft.touched)
              const body: Record<string, unknown> = {}
              if (touched.has('name')) body.name = draft.name
              if (touched.has('projectDir')) body.project_dir = draft.projectDir
              if (touched.has('defaultAgent')) body.default_agent = draft.defaultAgent
              // '' is a legitimate color instruction: it clears back to gray.
              if (touched.has('color')) body.color = draft.color
              if (Object.keys(body).length > 0) {
                await updateFolderMutation.mutateAsync({ id: folderModal.folderId, body })
              }
            }
            setFolderModal(null)
          }}
        />
      )}
    </div>
  )
}

export default memo(ChatSidebar)
