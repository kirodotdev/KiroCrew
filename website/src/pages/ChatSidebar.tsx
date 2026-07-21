import { useState, useRef, useEffect, memo, useMemo, useCallback, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { LayoutGroup, AnimatePresence, motion } from 'framer-motion'
import { Plus, X, Pin, Monitor, EyeOff, VenetianMask, Droplet, FolderPlus, MessageSquare, MessageSquarePlus, Folder, FolderOpen, ChevronRight, ChevronDown, Clock, Pencil, BrushCleaning, Link, Circle, MoreVertical, Tag as TagIcon, Columns2, Columns3, GripVertical, Zap, Check, Copy, ListFilter, List, Loader2, Smile, RotateCcw, Bot, ExternalLink, Cpu, GitPullRequest } from 'lucide-react'
import { DndContext, closestCenter, pointerWithin, KeyboardSensor, PointerSensor, useSensor, useSensors, useDroppable, DragOverlay, MeasuringStrategy, type DragEndEvent, type DragStartEvent, type DragOverEvent, type CollisionDetection } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, useSortable, sortableKeyboardCoordinates } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector } from '../store'
import { useConnected } from '../hooks/useConnected'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent } from '../components/ui/context-menu'
import { offlineProps } from '../utils/offline'
import { switchSlot, createSlot, deleteSlot, fetchHistory, resumeFromHistory, deleteHistorySession } from '../store/chatSlice'
import { sseSlotTitle } from '../store/dashboardSlice'
import { api, SEARCH_MIN_CHARS } from '../api/client'
import { computeReorderedFolders } from '../utils/reorderFolders'
import { computeRecentRank, recencyTintShadow, clampTintCount } from '../utils/recencyTint'
import { computeActiveSubtree, folderIsHidden, folderOffersHide } from '../utils/folderVisibility'
import { groupHistoryByFolder } from '../utils/groupHistoryByFolder'
import { SearchInput, Input, Btn, IconButton, IconButtonGroup } from '../components/ui'
import { useProvider } from '../providers'
import ModelDropdownList from '../components/ModelDropdownList'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import { useSessionActions } from '../hooks/useSessionActions'
import { useChatPopouts } from '../hooks/useChatPopouts'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { safeSetItem } from '../utils/safeStorage'
import { resolveFolderAgent } from '../utils/folderAgent'
import SessionActionsMenu from '../components/SessionActionsMenu'
import TagManagerList from '../components/TagManagerList'
import { DndDraggable, DndDroppable } from '../components/dnd'
import { collectFolderSubtreeIds } from '../utils/folderTree'
import type { ChatFolder, ChatTag, TagColumn, TagColumnMode, SubagentActivity } from '../types'
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

/** Telegram-style relative time: time today, "Yesterday hh:mm", weekday+time this week,
 *  short date this year, full date otherwise.
 *  Accepts ISO string (active slots) or Unix epoch seconds (history `modified`). */
function fmtRelativeTime(ts: string | number | undefined): string {
  if (ts == null) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const startOf6DaysAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d >= startOfToday) return time
  if (d >= startOfYesterday) return `Yesterday ${time}`
  if (d >= startOf6DaysAgo) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
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
const sidebarCollision: CollisionDetection = (args) => {
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
    if (args.pointerCoordinates) {
      const dropContainers = args.droppableContainers.filter(c => {
        const d = c.data?.current as { type?: string; folderId?: string | null } | undefined
        return d?.type === 'folder-drop' && !!d.folderId && !subtree.has(d.folderId)
      })
      const within = pointerWithin({ ...args, droppableContainers: dropContainers })
      const first = within[0]
      const rect = first?.data?.droppableContainer?.rect?.current
      if (rect) {
        const offsetY = args.pointerCoordinates.y - rect.top
        if (offsetY >= FOLDER_HEADER_DROP_BAND * 0.25 && offsetY <= FOLDER_HEADER_DROP_BAND * 0.75) {
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
  return within.length ? within : closestCenter(args)
}

/** Approximate height (px) of a folder header row. For root folder drags the
 *  MIDDLE 25%–75% of this band re-parents INTO the folder; the top/bottom
 *  edges (and everything below the header) stay sortable-reorder gestures —
 *  the VS Code / Notion "thirds" tree-DnD pattern. */
const FOLDER_HEADER_DROP_BAND = 34


/** Dashed always-reachable drop target shown in the root lane while dragging
 *  a foldered item — the explicit escape hatch out of a folder. Shared by
 *  session drags and nested-folder drags so the affordance (and wording)
 *  stays identical for both. */
function RootDropHint() {
  const { setNodeRef, isOver } = useDroppable({ id: 'root-unnest-hint', data: { type: 'folder-drop', folderId: null } })
  return (
    <div ref={setNodeRef} className={`m-1 min-h-[72px] flex items-center justify-center rounded-md border border-dashed transition-all ${isOver ? 'border-accent bg-accent/10 ring-2 ring-accent text-accent' : 'border-border text-muted'}`}>
      <span className="text-[12px]">Drop here to remove from folder</span>
    </div>
  )
}

function SortableFolderBlock({ folder, subtree, renderFolderBlock }: { folder: ChatFolder; subtree?: readonly string[]; renderFolderBlock: (f: ChatFolder, depth: number, visited?: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode[] }) {
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id, data: { type: 'folder', subtree } })
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1, position: 'relative' as const }
  // The whole folder header is the drag handle (pointer + touch): dragging the
  // row reorders the folder — no grip, consistent with session-card drag. Only
  // pointer listeners are forwarded (not attributes) so the header keeps
  // its inner collapse/action buttons valid. The PointerSensor activation
  // distance lets clicks through. setNodeRef stays on the block for sortable
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
function SortableColumnFolder({ folder, columnId, colSlotKeys, renderColumnFolder }: {
  folder: ChatFolder
  columnId: string
  colSlotKeys: Set<string>
  renderColumnFolder: (f: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean) => React.ReactNode
}) {
  const { listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: folder.id, data: { type: 'folder' } })
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
    <div className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none flex items-center gap-2">
      <FolderGlyph icon={folder?.icon} size={14} />{folder?.name ?? 'Folder'}
    </div>
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
  mode?: string
  agent?: string
  model?: string  // '' / absent = provider-default ("auto")
  workspace?: string
  created?: string
  last_ts?: string
  last_message?: string
  slack_linked?: boolean
  color_index?: number | null
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  clean_mode?: boolean
  folder_id?: string
  pinned?: boolean
  // Derived (not a payload field), like `unread`: true when the slot's last
  // activity falls inside `RECENT_WINDOW_MS`. Computed in `enrichedSlots`.
  recent?: boolean
  tags?: string[]
  forked_from?: string | null
  source_links?: Array<{
    provider: 'github' | 'gitlab'
    number: number
    url: string
    ci?: 'running' | 'passed' | 'failed' | null
    state?: 'open' | 'draft' | 'merged' | 'closed'
  }>
  source_links_total?: number
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

interface SessionFilterDef {
  key: SessionFilterKey
  storageKey: string
  label: string
  description: string
  color: string
  icon: (active: boolean) => React.ReactNode
}

const SESSION_FILTERS: SessionFilterDef[] = [
  {
    key: 'unread', storageKey: 'mc-session-unread-only', label: 'Unread',
    description: 'Show only sessions with unread messages',
    color: 'var(--info)',
    icon: (active) => <Circle size={12} className={active ? 'text-[var(--info)]' : 'text-muted'} {...(active ? { strokeWidth: 0, fill: 'var(--info)' } : {})} />,
  },
  {
    key: 'running', storageKey: 'mc-session-running-only', label: 'In progress',
    description: 'Show only sessions the agent is actively working on',
    color: 'var(--warn)',
    icon: (active) => <Zap size={12} className={active ? 'text-[var(--warn)]' : 'text-muted'} {...(active ? { fill: 'var(--warn)', stroke: 'none' } : {})} />,
  },
  {
    key: 'pinned', storageKey: 'mc-session-pinned-only', label: 'Pinned',
    description: 'Show only sessions you have pinned',
    color: 'var(--accent)',
    icon: (active) => <Pin size={12} className={active ? 'text-accent' : 'text-muted'} {...(active ? { fill: 'var(--accent)', stroke: 'none' } : {})} />,
  },
  {
    key: 'recent', storageKey: 'mc-session-recent-only', label: 'Recent',
    description: 'Show only sessions active within the selected window',
    color: 'var(--ok)',
    icon: (active) => <Clock size={12} className={active ? 'text-[var(--ok)]' : 'text-muted'} />,
  },
]

/**
 * Debounced backend session-content search.  Returns `null` until the first
 * response arrives (or whenever the query drops below `SEARCH_MIN_CHARS`),
 * and keeps the previous result visible while a new query is in flight so
 * the list doesn't blank out between keystrokes.
 */
function useDebouncedSessionSearch<T>(
  query: string,
  transform: (sessions: { key: string; title?: string; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary'; clean_mode?: boolean; folder_id?: string }[]) => T,
): T | null {
  const [result, setResult] = useState<T | null>(null)
  const token = useRef(0)
  useEffect(() => {
    const q = query.trim()
    const myToken = ++token.current
    if (q.length < SEARCH_MIN_CHARS) { setResult(null); return }
    const t = setTimeout(async () => {
      try {
        const d = await api.sessionsSearch(q)
        if (myToken !== token.current) return
        setResult(transform(d.sessions || []))
      } catch { /* keep previous result on error */ }
    }, 250)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])
  return result
}

/** Compute a date segment label for a session timestamp. Mirrors ChatGPT/Claude.
 *  Accepts either a Unix epoch (seconds) from backend `modified` or an ISO `created` string. */
function dateSegment(ts: number | string | undefined): string {
  if (ts == null) return 'Older'
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return 'Older'
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const daysAgo7 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7)
  const daysAgo30 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 30)
  if (d >= startOfToday) return 'Today'
  if (d >= startOfYesterday) return 'Yesterday'
  if (d >= daysAgo7) return 'Last 7 Days'
  if (d >= daysAgo30) return 'Last 30 Days'
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString([], { month: 'long' })
  return d.toLocaleDateString([], { year: 'numeric', month: 'long' })
}

// Folder icons are a deliberate emoji surface (see website/AGENTS.md exceptions
// + AUTOSDE no-emoji-as-icons): the backend auto-generates a single-emoji folder
// icon and FolderGlyph renders it, so this curated grid + free-input picker lets
// the user pick one. Not a status/UI icon — the emoji IS the folder's data.
/** Curated emoji set for the folder icon picker (folder / work / project themed). */
const FOLDER_EMOJIS = ['📁', '📂', '🗂️', '📋', '📝', '💼', '🚀', '⭐', '🔥', '💡', '🎯', '✅', '🐛', '🔧', '🧪', '📦', '🎨', '🔬', '🌟', '🧠', '⚙️', '🛠️', '📊', '🔒', '🌈', '🎉', '🤖', '☁️', '🧩', '📌'] as const

/** True if `s` is exactly one emoji grapheme — no letters, digits, or multiple emoji. */
function isSingleEmoji(s: string): boolean {
  if (!s) return false
  if (/[\p{L}\p{N}]/u.test(s)) return false // reject any letter/digit (i.e. text)
  const hasEmoji = /\p{Extended_Pictographic}/u.test(s) || /[\u{1F1E6}-\u{1F1FF}]/u.test(s)
  if (!hasEmoji) return false
  const Seg = (Intl as unknown as { Segmenter?: new (l?: string, o?: { granularity: string }) => { segment: (x: string) => Iterable<unknown> } }).Segmenter
  if (Seg) return [...new Seg(undefined, { granularity: 'grapheme' }).segment(s)].length === 1
  return true // older engines: backend remains authoritative
}

/** Folder icon picker: a Radix dropdown (fork idiom — the folder menu was
 *  migrated to Radix in the sidebar to fix viewport clipping) holding a curated
 *  emoji grid, a validated free-emoji input (single emoji only), and
 *  reset-to-auto. The trigger is a small Smile button that slots into the
 *  folder header's hover action group; picking closes the menu. Errors clear
 *  when the menu closes or the input changes. */
function FolderIconPicker({ currentIcon, onPick, onReset, size = 12 }: { currentIcon?: string; onPick: (icon: string) => void; onReset: () => void; size?: number }) {
  const [open, setOpen] = useState(false)
  const [iconErr, setIconErr] = useState(false)
  const pick = (em: string) => { onPick(em); setIconErr(false); setOpen(false) }
  return (
    <DropdownMenu open={open} onOpenChange={o => { setOpen(o); if (!o) setIconErr(false) }}>
      <DropdownMenuTrigger asChild>
        <button type="button" title="Folder icon" aria-label="Set folder icon"
          className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none"
          onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()}>
          <Smile size={size} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[240px] p-2" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-2 text-muted mb-1.5 px-1">
          <Smile size={13} className="shrink-0" />
          <span className="shrink-0 text-[12px]">Icon</span>
          <button type="button" title="Reset to an auto-generated emoji"
            className="ml-auto flex items-center gap-1 text-[11px] text-muted hover:text-accent bg-transparent border-none cursor-pointer p-0"
            onClick={() => { onReset(); setIconErr(false); setOpen(false) }}>
            <RotateCcw size={11} /> Reset
          </button>
        </div>
        <div className="grid grid-cols-6 gap-0.5">
          {FOLDER_EMOJIS.map(em => (
            <button key={em} type="button" aria-label={`Set folder icon to ${em}`}
              className={`h-7 flex items-center justify-center rounded cursor-pointer bg-transparent border-none text-[15px] leading-none hover:bg-bg-hover ${currentIcon === em ? 'bg-accent-subtle ring-1 ring-accent' : ''}`}
              onClick={() => pick(em)}>
              {em}
            </button>
          ))}
        </div>
        <input type="text" maxLength={16} placeholder="or type / paste an emoji" aria-label="Custom folder emoji"
          className={`mt-1.5 w-full text-[12px] text-text bg-bg border rounded px-2 py-1 outline-none ${iconErr ? 'border-danger focus:border-danger' : 'border-border focus:border-accent'}`}
          onClick={e => e.stopPropagation()}
          onChange={() => { if (iconErr) setIconErr(false) }}
          onKeyDown={e => { e.stopPropagation(); if (e.key !== 'Enter') return; const v = (e.target as HTMLInputElement).value.trim(); if (!v) return; if (!isSingleEmoji(v)) { setIconErr(true); return } pick(v) }} />
        {iconErr && <div className="mt-1 px-1 text-[11px] text-danger">Enter a single emoji (no text).</div>}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** A folder's icon: a Lucide folder glyph as a uniform base. When collapsed
 *  (resting) it shows the closed Folder glyph with the folder's custom emoji
 *  (if any) overlaid on its flat face. When expanded it swaps to FolderOpen and
 *  drops the emoji — the open shape plus the rotated chevron carry the state,
 *  and FolderOpen's angled flap has no flat face for the emoji to sit on cleanly.
 *  Every folder keeps the same fixed footprint so the sidebar stays aligned, and
 *  the board view shows the emoji too (it previously rendered only the bare glyph). */
function FolderGlyph({ icon, size = 14, open = false, className = 'text-muted shrink-0' }: { icon?: string; size?: number; open?: boolean; className?: string }) {
  const Glyph = open ? FolderOpen : Folder
  return (
    <span className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <Glyph size={size} className={className} />
      {icon && !open && (
        <span
          aria-hidden
          className="absolute inset-x-0 bottom-0 flex items-center justify-center leading-none pointer-events-none"
          style={{ top: Math.round(size * 0.42), fontSize: Math.max(7, Math.round(size * 0.52)) }}
        >{icon}</span>
      )}
    </span>
  )
}

/** Animated collapsible for unknown-height content (folder bodies).
 *  Uses CSS grid `1fr`/`0fr` trick so we can animate to intrinsic height
 *  without measuring. For fixed-height panels use Framer Motion instead. */
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
      <div style={{ overflow: 'hidden', visibility: open ? 'visible' : 'hidden', padding: open ? '2px' : 0 }}>{children}</div>
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
  /** When true, the sidebar is externally collapsible — a persistent toggle
   *  button lives in the chat container at the top-left, so the header
   *  reserves left space for it. Omitted in embed/sessions mode where the
   *  sidebar is the whole view. */
  collapsible?: boolean
  /** Split View (session grid) opt-in feature. When `splitEnabled`, a pinned
   *  "Split View" entry renders at the top; clicking it calls `onOpenSplit`.
   *  `splitActive` highlights it while the grid surface is showing. */
  splitEnabled?: boolean
  splitActive?: boolean
  onOpenSplit?: () => void
}

type SortKey = 'date-desc' | 'date-asc' | 'created-desc' | 'created-asc' | 'name-asc' | 'name-desc'
const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'date-desc', label: 'Newest' },
  { value: 'date-asc', label: 'Oldest' },
  { value: 'created-desc', label: 'Created (Newest)' },
  { value: 'created-asc', label: 'Created (Oldest)' },
  { value: 'name-asc', label: 'A → Z' },
  { value: 'name-desc', label: 'Z → A' },
]
const SORT_LS_KEY = 'mc-session-sort'
/** Flat view ("explode chats out of folders") persistence key. */
const FLAT_VIEW_LS_KEY = 'mc-sidebar-flat-view'

function compareSlots(a: Slot, b: Slot, key: SortKey): number {
  return compareBySort(a, b, key)
}

/** Shared comparator for both active sessions and history items. */
function compareBySort(a: { title?: string; key: string; created?: string; last_ts?: string; modified?: number }, b: typeof a, key: SortKey): number {
  if (key === 'name-asc' || key === 'name-desc') {
    const na = (a.title || a.key).toLowerCase()
    const nb = (b.title || b.key).toLowerCase()
    return key === 'name-asc' ? na.localeCompare(nb) : nb.localeCompare(na)
  }
  if (key === 'created-desc' || key === 'created-asc') {
    const ca = a.created || ''
    const cb = b.created || ''
    return key === 'created-desc' ? cb.localeCompare(ca) : ca.localeCompare(cb)
  }
  // date-desc / date-asc: use last activity (modified epoch, last_ts ISO, or created ISO)
  const toEpoch = (item: typeof a) => item.modified ?? (item.last_ts ? new Date(item.last_ts).getTime() / 1000 : item.created ? new Date(item.created).getTime() / 1000 : 0)
  const ta = toEpoch(a)
  const tb = toEpoch(b)
  return key === 'date-desc' ? tb - ta : ta - tb
}

export const SIDEBAR_MIN = 180
export const SIDEBAR_MAX = 1400
const SIDEBAR_LS_KEY = 'mc-sidebar-width'

function ChatSidebar({
  slots, activeSlot, unreadSlots, history, historyHasMore,
  defaultAgent, installedAgents, mode, onWidthChange, onDragChange, onSelectSlot, collapsible, splitEnabled, splitActive, onOpenSplit,
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
  const [slotFilter, setSlotFilter] = useState('')
  const [historyFilter, setHistoryFilter] = useState('')
  const historySearchResults = useDebouncedSessionSearch(historyFilter, s => s)
  // Which folder groups are collapsed in the grouped search-results view.
  // Ephemeral: reset on every query change so a fresh search shows all groups.
  const [collapsedHistoryGroups, setCollapsedHistoryGroups] = useState<Set<string>>(() => new Set())
  useEffect(() => { setCollapsedHistoryGroups(new Set()) }, [historyFilter])
  const slotSearchKeys = useDebouncedSessionSearch(
    slotFilter,
    sessions => new Set(sessions.map(s => s.key.replace(/^dashboard_/, ''))),
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
  const renameInputRef = useRef<HTMLInputElement | null>(null)
  // Set by any menu's Rename item (session rows + folder headers) so the closing
  // menu's onCloseAutoFocus knows to skip Radix's trigger-focus-restore for this
  // one close (see the menu Content handlers below). One-shot: read and cleared
  // on the next close.
  const suppressMenuRestoreRef = useRef(false)
  // The rename menus are Radix (ContextMenu/DropdownMenu). On close, Radix's
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
      if (el) { el.focus({ preventScroll: true }); el.select() }
    })
    return () => cancelAnimationFrame(raf)
  }, [renamingSlot, renameScope])
  // Folder rename + folder create refs; the focus effects live after the
  // editingId/creatingIn useState declarations below (they can't be referenced
  // here — TDZ). See those effects for why the rAF re-grab is needed.
  const folderEditInputRef = useRef<HTMLInputElement | null>(null)
  const folderCreateInputRef = useRef<HTMLInputElement | null>(null)
  // Shared onCloseAutoFocus for every rename-hosting menu (session row context +
  // ⋯ dropdowns, and both folder-header ⋯ dropdowns). When Rename was the chosen
  // item it armed suppressMenuRestoreRef, so we preventDefault to stop Radix from
  // yanking focus back to the trigger — that restore would otherwise blur the
  // just-mounted rename input and cancel the edit. Every other item keeps the
  // default focus-restore intact.
  const onMenuCloseAutoFocus = useCallback((e: Event) => {
    if (suppressMenuRestoreRef.current) { suppressMenuRestoreRef.current = false; e.preventDefault() }
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
  const slotStatusDetail = useAppSelector(s => s.chat.slotStatusDetail)
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
    return counts
  }, [storeActiveSlot, activeSlotSubagents, slotActivity])
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  const connected = useConnected()
  // O(1) lookup set for the filter predicate (mirrors the `pinned` and
  // `slotSearchKeys` patterns elsewhere in this file).
  const unreadSet = useMemo(() => new Set(unreadSlots), [unreadSlots])
  // Heartbeat that re-evaluates recency even when nothing else re-renders.
  // Sidebar interactions (new messages, status changes, opening the menu) all
  // recompute `enrichedSlots` for free, so this only matters when the sidebar
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
  const enrichedSlots = useMemo<Slot[]>(() => {
    // Snapshot `now` once per recompute so every slot's recency is measured
    // against the same instant. The last-activity timestamp mirrors the
    // date-sort comparator (`last_ts` ISO, else `created` ISO).
    const now = Date.now()
    return slots.map(s => {
      const recent = isWithinRecentWindow(s.last_ts || s.created, now, recentWindowMs)
      return { ...s, unread: unreadSet.has(s.key), recent }
    })
    // `recentTick` is an intentional dep: it forces recency to re-evaluate on
    // the heartbeat above so idle sessions age out of the Recent filter.
  }, [slots, unreadSet, recentWindowMs, recentTick]) // eslint-disable-line react-hooks/exhaustive-deps
  const filterCounts = useMemo(() => {
    const counts = {} as Record<SessionFilterKey, number>
    for (const filterDef of SESSION_FILTERS) counts[filterDef.key] = enrichedSlots.filter(slot => slot[filterDef.key]).length
    return counts
  }, [enrichedSlots])
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
  const onHistoryDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = historyHeight
    setHistoryDragging(true)
    const onMove = (ev: MouseEvent) => {
      // Drag handle is ABOVE the pane, so dragging UP grows the pane.
      const next = Math.max(HISTORY_MIN_HEIGHT, Math.min(HISTORY_MAX_HEIGHT, startH - (ev.clientY - startY)))
      setHistoryHeight(next)
    }
    const onUp = () => {
      setHistoryDragging(false)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.cursor = 'ns-resize'
    document.body.style.userSelect = 'none'
  }, [historyHeight])
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
    onError: (e) => setCleanupError(e instanceof Error ? e.message : 'Archive failed'),
  })

  // Bulk model switch — apply one model to every live session at once.
  const provider = useProvider()
  const [bulkModelOpen, setBulkModelOpen] = useState(false)
  const [bulkModel, setBulkModel] = useState('')        // pending pick ('auto' = provider default)
  const [bulkSkipRunning, setBulkSkipRunning] = useState(true)
  const [bulkModelError, setBulkModelError] = useState('')
  const { data: bulkModelOptions = [] } = useQuery({
    queryKey: ['available-models', provider.id],
    queryFn: async () => {
      const models = await provider.fetchAvailableModels()
      return [{ name: 'auto', description: 'Default' }, ...models.filter(m => m.name && m.name !== 'auto')]
    },
    enabled: bulkModelOpen,
    staleTime: 60_000,
  })
  const bulkRunningCount = useMemo(() => slots.filter(s => s.running).length, [slots])
  // Count only slots that would actually change: model differs from the target
  // (the backend leaves already-on-target slots as `unchanged`), minus running
  // slots when skipping. Keeps the "Switch N" label + disable guard honest.
  const bulkAffectedCount = useMemo(() => {
    const target = bulkModel === 'auto' ? '' : bulkModel
    return slots.filter(s => (s.model ?? '') !== target && (!bulkSkipRunning || !s.running)).length
  }, [slots, bulkModel, bulkSkipRunning])
  const bulkModelMutation = useMutation({
    mutationFn: ({ model, skipRunning }: { model: string; skipRunning: boolean }) =>
      api.chatSlotsModel(model === 'auto' ? '' : model, skipRunning),
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
    onError: (e) => setBulkModelError(e instanceof Error ? e.message : 'Switch failed'),
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
  // Ranks up to the configured count of sessions by recency (last_ts) for the sidebar tint —
  // see ../utils/recencyTint. Count = server-side dashboard.recent_tint_count (shared
  // kirocrewConfig query); recomputes when the slots or the configured count change.
  const { data: mcCfg } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const recentTintCount = clampTintCount(mcCfg?.dashboard?.recent_tint_count)
  const recentRank = useMemo(() => computeRecentRank(slots, recentTintCount), [slots, recentTintCount])

  // Folder editing state
  const [creatingIn, setCreatingIn] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  // Board view renders a folder once per column, so `editingId === folder.id`
  // (and the `creatingIn` gate) is true in every column at once — the input
  // would mount in all of them and the shared ref would bind to the last. These
  // scopes pin the folder rename / subfolder-create to the clicked column's
  // render instance (the columnId, or 'list' in list view) so exactly one input
  // mounts. renderFolderHeader passes 'list'; renderColumnFolder passes columnId.
  const [editScope, setEditScope] = useState<string | null>(null)
  const [createScope, setCreateScope] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const cancelledRef = useRef(false)
  // Folder rename (renderFolderHeader + board renderColumnFolder) and folder
  // creation (New folder / New subfolder) mount their inputs from a Radix menu,
  // so plain autoFocus loses the same race as the session rename: focus lands on
  // the trigger/body after the menu tears down (caret never in the box) and the
  // default scroll-into-view yanks the horizontally-scrolling board sideways.
  // Re-grab focus on the next frame with preventScroll so the board doesn't jump;
  // rename selects the text for overtype, create leaves the empty field as-is.
  // Each effect keys on both its id AND companion scope: a same-id, scope-only
  // change (retarget to a different column before the first column's commit
  // fires) must re-run so focus lands in the newly-mounted column's input. The
  // re-focus is idempotent so re-running is harmless. When the id clears
  // (commit/cancel/escape/blur), clear the scope so no stale column identity
  // lingers.
  useEffect(() => {
    if (!editingId) { setEditScope(null); return }
    const raf = requestAnimationFrame(() => {
      const el = folderEditInputRef.current
      if (el) { el.focus({ preventScroll: true }); el.select() }
    })
    return () => cancelAnimationFrame(raf)
  }, [editingId, editScope])
  useEffect(() => {
    if (!creatingIn) { setCreateScope(null); return }
    const raf = requestAnimationFrame(() => {
      folderCreateInputRef.current?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(raf)
  }, [creatingIn, createScope])
  // Belt-and-suspenders disarm of the one-shot suppress ref. It's normally
  // consumed by the very next onCloseAutoFocus, but if a menu is ever dismissed
  // without firing that (an outside-dismiss race), the ref would stay armed and
  // wrongly preventDefault the NEXT menu close. Whenever the sidebar is idle (no
  // edit open), force-disarm: no legitimate pending suppression can exist then.
  // Safe against the normal flow — during a live edit an id is non-null, so this
  // hasn't run yet; by the time all ids clear the real close already consumed it.
  useEffect(() => {
    if (!renamingSlot && !editingId && !creatingIn) suppressMenuRestoreRef.current = false
  }, [renamingSlot, editingId, creatingIn])

  // Resize logic
  const sidebarDragging = useRef(false)
  const sidebarStartX = useRef(0)
  const sidebarStartW = useRef(0)
  const sidebarWidthRef = useRef(sidebarWidth)
  sidebarWidthRef.current = sidebarWidth
  const onWidthChangeRef = useRef(onWidthChange)
  onWidthChangeRef.current = onWidthChange
  const onDragChangeRef = useRef(onDragChange)
  onDragChangeRef.current = onDragChange
  useEffect(() => { onWidthChangeRef.current?.(sidebarWidth) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!sidebarDragging.current) return
      const newW = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, sidebarStartW.current + e.clientX - sidebarStartX.current))
      setSidebarWidth(newW)
      onWidthChangeRef.current?.(newW)
    }
    const onUp = () => {
      if (!sidebarDragging.current) return
      sidebarDragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
      const w = sidebarWidthRef.current
      safeSetItem(SIDEBAR_LS_KEY, String(w))
      onWidthChangeRef.current?.(w)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      if (sidebarDragging.current) {
        sidebarDragging.current = false
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        onDragChangeRef.current?.(false)
      }
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


  const createColumnMutation = useMutation({
    mutationFn: (body: { name?: string; tag_ids?: string[]; mode?: TagColumnMode }) => api.createTagColumn(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
  })
  const updateColumnMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; tag_ids?: string[]; mode?: TagColumnMode; order?: number; include_untagged?: boolean } }) => api.updateTagColumn(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tag-columns'] }),
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
  // Filter predicate for a single column
  const columnMatches = useCallback((col: TagColumn, slotTags: string[]): boolean => {
    // "include untagged" OR'd on top of any tag filter
    if (col.include_untagged && slotTags.length === 0) return true
    if (!col.tag_ids || col.tag_ids.length === 0) return true
    const set = new Set(slotTags)
    if (col.mode === 'all') return col.tag_ids.every(t => set.has(t))
    if (col.mode === 'none') return !col.tag_ids.some(t => set.has(t))
    return col.tag_ids.some(t => set.has(t))  // 'any'
  }, [])

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

  const filteredSlots = useMemo(() => {
    const activeFilterDefs = SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key))
    return enrichedSlots
      .filter(slot => {
        if (activeFilterDefs.length > 0 && !activeFilterDefs.some(filterDef => slot[filterDef.key])) return false
        if (!slotFilter) return true
        if (slotFilter.trim().length >= SEARCH_MIN_CHARS) {
          if (slotSearchKeys) return slotSearchKeys.has(slot.key)
          return ((slot.title || '') + slot.key + (slot.agent || '')).toLowerCase().includes(slotFilter.toLowerCase())
        }
        return ((slot.title || '') + slot.key + (slot.agent || '')).toLowerCase().includes(slotFilter.toLowerCase())
      })
      .sort((a, b) => {
        const pa = pinned.has(a.key) ? 0 : 1
        const pb = pinned.has(b.key) ? 0 : 1
        if (pa !== pb) return pa - pb
        return compareSlots(a, b, sortKey)
      })
  },
    [enrichedSlots, slotFilter, slotSearchKeys, pinned, sortKey, activeFilters]
  )

  // Flat-view projection: every visible session (foldered + unfoldered) in one
  // list. Removes ONLY the folder rendering hierarchy — the user's sort
  // (incl. pin priority) and active filters/search apply exactly as in the
  // tree, via filteredSlots.
  const folderNameById = useMemo(() => {
    const m: Record<string, string> = {}
    for (const f of folders) m[f.id] = f.name
    return m
  }, [folders])

  // Folder mutations
  const createFolderMutation = useMutation({
    mutationFn: ({ name, parentId }: { name: string; parentId?: string }) => api.createChatFolder(name.trim(), parentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-folders'] }),
  })
  const createFolder = useCallback((name: string, parentId?: string) => {
    if (!name.trim()) return
    createFolderMutation.mutate({ name, parentId })
    setCreatingIn(null); setNewName('')
  }, [createFolderMutation])
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

  // ── Folder drag-to-reorder ──
  const dndSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
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

  // Reveal-in-sidebar: expand parent folder(s) then scroll to the slot
  useEffect(() => {
    const handler = (e: Event) => {
      const key = (e as CustomEvent).detail as string
      if (!key) return
      const slot = slots.find(s => s.key === key)
      if (slot?.folder_id) {
        // Expand all ancestor folders
        const expand = (fid: string) => {
          const f = folders.find(x => x.id === fid)
          if (f?.collapsed) updateFolderMutation.mutate({ id: fid, body: { collapsed: false } })
          if (f?.parent_id) expand(f.parent_id)
        }
        expand(slot.folder_id)
      }
      setTimeout(() => {
        const el = document.querySelector(`[data-slot-key="${key}"]`)
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }, 150)
    }
    window.addEventListener('reveal-slot', handler)
    return () => window.removeEventListener('reveal-slot', handler)
  }, [slots, folders, updateFolderMutation])
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
    const d = e.active.data.current as { type?: string; key?: string } | undefined
    if (d?.type === 'session' && d.key) setActiveDrag({ type: 'session', id: d.key })
    else if (d?.type === 'folder') setActiveDrag({ type: 'folder', id: e.active.id as string })
  }, [])
  const handleSidebarDragEnd = useCallback((event: DragEndEvent) => {
    setActiveDrag(null)
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
      //  folder-drop  → assign to that folder (folderId may be null for root lane)
      //  folder       → sortable folder container (whole block) → assign to its id
      if (o?.type === 'folder-drop') assignToFolder(a.key, o.folderId ?? null)
      else if (o?.type === 'folder') assignToFolder(a.key, over.id as string)
    }
  }, [reorderFolders, assignToFolder, moveFolderTo])
  const handleSidebarDragCancel = useCallback(() => { setActiveDrag(null); if (dragExpandTimer.current) { clearTimeout(dragExpandTimer.current.timer); dragExpandTimer.current = null } }, [])
  // Auto-expand collapsed folders when a dragged item hovers over them for 500ms.
  const dragExpandTimer = useRef<{ id: string; timer: ReturnType<typeof setTimeout> } | null>(null)
  const handleSidebarDragOver = useCallback((event: DragOverEvent) => {
    const over = event.over
    const overData = over?.data.current as { type?: string; folderId?: string | null } | undefined
    const targetFolderId = overData?.type === 'folder-drop' ? overData.folderId : null
    // If hovering a collapsed folder, blink ring twice then expand
    if (targetFolderId) {
      const f = folders.find(x => x.id === targetFolderId)
      if (f?.collapsed) {
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
                  updateFolderMutation.mutate({ id: targetFolderId, body: { collapsed: false } })
                  dragExpandTimer.current = null
                }, 450)
              } else {
                updateFolderMutation.mutate({ id: targetFolderId, body: { collapsed: false } })
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
  }, [folders, updateFolderMutation])
  const createChatInFolderMutation = useMutation({
    mutationFn: ({ folderId }: { folderId: string; columnId?: string }) => {
      const agent = resolveFolderAgent(folders, folderId, defaultAgent)
      const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
      return dispatch(createSlot({ agent, mode: effectiveMode })).unwrap()
    },
    onSuccess: (slot: Slot, { folderId, columnId }: { folderId: string; columnId?: string }) => {
      if (slot?.key) {
        assignToFolder(slot.key, folderId)
        // Board view: also drop the new session into the column it was created
        // from, so a status-lane column shows it immediately instead of the
        // untagged session vanishing from a tag-filtered column. Mirrors a
        // drag-drop and is a harmless no-op for filter-only / non-status columns.
        if (columnId) dropSlotMutation.mutate({ slot: slot.key, columnId })
      }
    },
    onError: (err: unknown) => {
      // eslint-disable-next-line no-console -- surface chat-creation failures for diagnostics
      console.error('Failed to create chat in folder:', err)
    },
  })
  const createChatInFolder = useCallback((folderId: string, columnId?: string) => { createChatInFolderMutation.mutate({ folderId, columnId }) }, [createChatInFolderMutation])

  // Create autopilot session mutation (consistent with useMutation pattern)
  const createAutopilotMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ agent: defaultAgent || undefined, mode: 'orchestrator' })).unwrap(),
    onSuccess: () => { requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()) },
  })

  // Create default chat session mutation
  const createChatMutation = useMutation({
    mutationFn: () => {
      const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: effectiveMode })).unwrap()
    },
    onSuccess: () => { requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()) },
  })

  // Session colors
  const { paletteColors, boost, colorMode } = useSessionPalette()

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
      // Drag-and-drop folder drop zone: the drag handlers make this a mouse-only
      // drop target with no keyboard analogue, so scope-disable the static-interaction rule.
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions
      <div key={`col-${columnId}-folder-${folder.id}`}
        data-testid={`col-${columnId}-folder-${folder.id}`}
        className="rounded-md transition-all mb-0.5"
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
          aria-expanded={!folder.collapsed}
          aria-label={`${folder.collapsed ? 'Expand' : 'Collapse'} folder ${folder.name}`}
          {...(draggable ? dragHandleProps : {})}
          onClick={() => toggleCollapse(folder.id)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCollapse(folder.id) } }}
        >
          <span className="shrink-0 text-muted transition-transform duration-150" style={{ transform: folder.collapsed ? 'rotate(0deg)' : 'rotate(90deg)' }}>
            <ChevronRight size={12} />
          </span>
          <FolderGlyph icon={folder.icon} size={11} open={!folder.collapsed} className="shrink-0 text-muted" />
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
            <span className="flex-1 truncate" title="Double-click to rename" onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}>{folder.name}</span>
          )}
          <span className="text-[10px] text-muted shrink-0">{count}</span>
          {!(editingId === folder.id && editScope === columnId) && (
          <span className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 transition-opacity flex items-center gap-0.5">
            <select className="text-[10px] text-muted bg-transparent border-none cursor-pointer outline-none max-w-[60px]" title="Default agent" value={folder.default_agent || ''} onClick={e => e.stopPropagation()} onChange={e => { e.stopPropagation(); updateFolderMutation.mutate({ id: folder.id, body: { default_agent: e.target.value } }) }}>
              <option value="">agent…</option>
              {installedAgents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
            </select>
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-chat`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title="New chat in folder" aria-label={`New chat in folder ${folder.name}`} onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, columnId) }} onMouseDown={e => { e.stopPropagation() }}>
              <MessageSquarePlus size={11} />
            </button>
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-sub`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title="New subfolder" aria-label="New subfolder" onClick={e => { e.stopPropagation(); setCreatingIn(folder.id); setCreateScope(columnId); setNewName('') }}>
              <FolderPlus size={10} />
            </button>
            <FolderIconPicker currentIcon={folder.icon} size={10} onPick={icon => updateFolderMutation.mutate({ id: folder.id, body: { icon } })} onReset={() => updateFolderMutation.mutate({ id: folder.id, body: { regenerate_icon: true } })} />
            <button type="button" className="text-muted hover:text-danger bg-transparent border-none cursor-pointer p-[2px]" title={`Delete folder "${folder.name}"`} aria-label={`Delete folder ${folder.name}`} onClick={e => { e.stopPropagation(); if (confirm(`Delete folder "${folder.name}"? Sessions will be ungrouped.`)) deleteFolderMutation.mutate(folder.id) }}>
              <X size={10} />
            </button>
          </span>
          )}
        </div>
        <FolderBody open={!folder.collapsed && !forceCollapsed}>
          <div className="border-l border-border ml-2 pl-1">
            {/* Inline "New chat" affordance at the top of the column folder's
             *  body, mirroring the list-view folder body. */}
            <button key={`col-${columnId}-newchat-${folder.id}`} type="button"
              onClick={() => createChatInFolder(folder.id, columnId)}
              title="New chat in folder" aria-label={`New chat in ${folder.name}`}
              className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-[11px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
              <MessageSquarePlus size={11} className="shrink-0" /><span>New chat in folder</span>
            </button>
            {(deepChildren.length > 0 || childSlots.length > 0) && (
              <div className="mx-3 border-b border-border" />
            )}
            {deepChildren.map(cf => renderColumnFolder(cf, columnId, colSlotKeys))}
            {creatingIn === folder.id && createScope === columnId && (
              <div className="px-2 py-1">
                <Input ref={folderCreateInputRef} className="w-full py-1 text-[12px]" placeholder="Subfolder name…"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  {...ime.bindEnter<HTMLInputElement>({
                    onEnter: () => createFolder(newName, folder.id),
                    onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') },
                    onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName, folder.id); else setCreatingIn(null) },
                  })} />
              </div>
            )}
            {childSlots.map((s, i) => {
              const isActive = activeSlot === s.key
              const nextIsActive = i < childSlots.length - 1 && activeSlot === childSlots[i + 1].key
              const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
              return renderSessionRow(s, 1, showDivider, `${columnId}:${folder.id}`)
            })}
          </div>
        </FolderBody>
      </div>
    )
  }

  // scope namespaces the Framer layoutId per render location. A multi-tag slot
  // can render in several columns at once; same layoutId in one LayoutGroup
  // collides (Framer paints one, hides the rest). Distinct scope = distinct id.
  const renderSessionRow = (s: Slot, _indent: number, showDivider: boolean, scope = 'list') => {
    // Flat view shares the tree's layoutId namespace so Framer Motion treats a
    // row as the SAME element across the view toggle and animates it from its
    // tree position into the flat lane (and back). Safe: the two views are
    // ternary branches — never mounted simultaneously — so IDs can't collide.
    // Behavior stays keyed on the real scope ('flat' disables DnD etc.).
    const layoutScope = scope === 'flat' ? 'list' : scope
    const agentName = s.agent || defaultAgent || ''
    const agentMeta = installedAgents.find(a => a.name === agentName)
    const isAim = agentMeta?.source === 'aim'
    const isBuiltin = agentMeta?.source === 'builtin'
    const agentColor = isAim ? 'text-[var(--aim)]' : isBuiltin ? 'text-muted' : 'text-accent'
    const isActive = activeSlot === s.key
    const isOut = poppedOut.has(s.key)
    const recent = recentRank.get(s.key)
    const subagentCount = subagentCounts[s.key] || 0
    const ci = s.color_index != null && s.color_index >= 0 && s.color_index < paletteColors.length ? s.color_index : null
    const rowColor = ci != null ? paletteColors[ci] : null
    const boostStyle: Record<string, string> = {}
    if (rowColor && ci != null) {
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
        <DndDraggable id={`session:${s.key}`} data={{ type: 'session', key: s.key }} disabled={scope !== 'list' || renamingSlot === s.key}>
          {({ setNodeRef, listeners, isDragging }) => (
        <ContextMenu>
          <ContextMenuTrigger asChild>
        <div ref={scope === 'list' ? setNodeRef : undefined} {...(scope === 'list' ? listeners : {})}
          data-draggable={(renamingSlot !== s.key).toString()}
          className={`session-row group relative flex items-start gap-2.5 px-4 py-2 rounded-md text-sm transition-all select-none ${isActive ? !connected ? 'session-active text-text-strong bg-accent-subtle cursor-not-allowed' : 'session-active text-text-strong bg-accent-subtle cursor-pointer' : !connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'} ${rowColor ? 'session-colored' : ''} ${rowColor && colorMode === 'gradient' ? 'session-gradient' : ''} ${isDragging ? 'opacity-40' : ''}`}
          style={boostStyle as React.CSSProperties}
          draggable={(scope !== 'list' && scope !== 'flat' && renamingSlot !== s.key) && (connected || isActive)}
          {...offlineProps(connected, 'switch sessions')}
          role="button"
          tabIndex={0}
          aria-current={isActive ? 'true' : undefined}
          aria-disabled={!connected}
          onKeyDown={e => {
            // WCAG 2.1.1: session rows must be operable via keyboard.
            // Enter/Space activates the row (same as click). Other keys are
            // forwarded to dnd-kit's listener (this prop appears after the
            // {...listeners} spread, so last-prop-wins would otherwise clobber
            // it) — useful for continuing a pointer-initiated drag via arrow
            // keys. Note: keyboard-initiated drag pickup was never functional
            // for these rows (plain useDraggable without SortableContext), so
            // consuming Enter/Space here does not regress it.
            if (e.key !== 'Enter' && e.key !== ' ') {
              if (scope === 'list') (listeners as Record<string, (e: React.KeyboardEvent) => void> | undefined)?.onKeyDown?.(e)
              return
            }
            if ((e.target as HTMLElement) !== e.currentTarget) return // don't hijack inner buttons
            e.preventDefault()
            if (!connected) return
            dispatch(switchSlot(s.key))
            onSelectSlot?.(s.key)
          }}
          onDragStart={scope !== 'list' && scope !== 'flat' ? (e => { e.dataTransfer.setData('text/plain', s.key); e.dataTransfer.effectAllowed = 'move' }) : undefined}
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
          {s.unread && !s.running && !s.pending_approval && (
            // Blue dot = "your turn": the agent finished its turn (not running)
            // and you haven't opened the session since (unread). Redefined from
            // the old "any unseen output" trigger so it no longer lights
            // mid-stream; a pending approval gets its own yellow subtitle
            // treatment instead. (CR-284529836)
            <span className="absolute right-1.5 top-1/2 -translate-y-1/2 w-2 h-2 rounded-full pointer-events-none" style={{ background: 'var(--info)' }} title="Agent finished — your turn" />
          )}
          <div className="flex-1 min-w-0 overflow-hidden">
            <div className={`text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
              {pinned.has(s.key) && <span className="shrink-0" title="Pinned"><Pin size={10} className="text-accent" /></span>}
              <AnimatePresence mode="wait">
                <motion.span key={agentName || 'empty'} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="truncate">{agentName || '\u00A0'}</motion.span>
              </AnimatePresence>
              {isOut && <span className="text-accent" title="Popped out to a separate window"><ExternalLink size={10} /></span>}
              {s.slack_linked && <span className="text-[10px]" title="Linked to Slack"><Link size={10} /></span>}
              {s.clean_mode
                ? <span className="text-accent" title="Clean — agent-only, no KiroCrew context or MCP"><Droplet size={10} /></span>
                : <>
                    {s.memory_mode === 'incognito' && <span className="text-muted" title="Incognito — no memory writes"><EyeOff size={10} /></span>}
                    {s.memory_mode === 'temporary' && <span className="text-aim" title="Temporary — no memory reads or writes"><VenetianMask size={10} /></span>}
                  </>}
              {s.mode === 'orchestrator' && <span className="text-[11px] px-1 py-0 rounded bg-accent/15 text-accent font-medium" title="Autopilot mode">Autopilot</span>}
              {/* Trailing meta grouped under ONE ml-auto: two sibling auto
               *  margins would split the free space and strand the folder
               *  chip mid-row (AutoSDE, CR-290079557). */}
              {(scope === 'flat' && slotFolders[s.key] && folderNameById[slotFolders[s.key]]) || s.last_ts || s.created ? (
                <span className="ml-auto inline-flex items-center gap-1 shrink-0">
                  {scope === 'flat' && slotFolders[s.key] && folderNameById[slotFolders[s.key]] && (
                    <span className="text-[10px] text-muted font-normal inline-flex items-center gap-0.5 max-w-[90px]" title={`In folder: ${folderNameById[slotFolders[s.key]]}`}>
                      <Folder size={9} className="shrink-0" aria-hidden />
                      <span className="truncate">{folderNameById[slotFolders[s.key]]}</span>
                    </span>
                  )}
                  {(s.last_ts || s.created) && <span className="text-[11px] text-muted font-normal shrink-0">{fmtRelativeTime(s.last_ts || s.created!)}</span>}
                </span>
              ) : null}
            </div>
            <div className="text-[13px] font-semibold leading-snug line-clamp-2 break-words text-text" title={s.title && s.title !== s.key ? s.title : s.key}>
              {s.forked_from && slots.some(x => x.key === s.forked_from!.replace(/^dashboard:/, '')) && (
                <span className="text-accent mr-0.5" title="Forked child session">↳</span>
              )}
              {renamingSlot === s.key && renameScope === scope ? (
                <Input ref={renameInputRef} className="w-full bg-transparent border border-accent rounded px-1 py-0 text-text-strong outline-none text-[13px] select-text" value={renameValue} onChange={e => setRenameValue(e.target.value)} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => { (document.activeElement as HTMLInputElement)?.blur() }, onEscape: () => { cancelRenameRef.current = true; setRenamingSlot(null) }, onBlur: () => { if (!cancelRenameRef.current && renameValue.trim()) { dispatch(sseSlotTitle({ key: s.key, title: renameValue.trim() })); api.renameSlot(s.key, renameValue.trim()).catch(() => { queryClient.invalidateQueries({ queryKey: ['chat-slots'] }) }) } cancelRenameRef.current = false; setRenamingSlot(null) } })} onMouseDown={e => e.stopPropagation()} />
              ) : (s.title && s.title !== s.key ? s.title : s.key)}
            </div>
            {s.pending_approval ? (
              // Pending approval outranks running (mirrors the Board's
              // inferLane, which returns its approval lane before the running
              // check): show the yellow dot + "Needs approval" even if the slot
              // still reports running, so an owed approval is never hidden
              // behind a "Thinking…" spinner.
              <div className="text-[12px] leading-snug mt-0.5 flex items-center gap-1.5 min-w-0">
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--warn)' }} title="Needs approval" />
                <span className="truncate"><span className="font-medium" style={{ color: 'var(--warn)' }}>Needs approval</span>{s.last_message ? <span className="text-muted"> · {s.last_message}</span> : null}</span>
              </div>
            ) : subagentCount > 0 ? (
              // A spawned subagent is still running — surface it even if the
              // parent turn has ended (s.running === false while it waits for
              // completion events), so the sidebar shows live activity instead
              // of a stale last message. Outranks the generic "Thinking…".
              <div className="text-[12px] text-accent leading-snug truncate mt-0.5 flex items-center gap-1" title={`${subagentCount} subagent${subagentCount > 1 ? 's' : ''} running`}>
                <Bot size={11} className="shrink-0 animate-pulse" aria-hidden />
                <span className="truncate">{subagentCount} agent{subagentCount > 1 ? 's' : ''} running</span>
              </div>
            ) : s.running ? (
              <div className="text-[12px] text-accent leading-snug truncate mt-0.5 flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse shrink-0" />{slotStatusDetail[s.key]?.text || 'Thinking…'}</div>
            ) : s.last_message ? (
              <div className="text-[12px] text-muted leading-snug truncate mt-0.5">{s.last_message}</div>
            ) : null}
            {s.source_links && s.source_links.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-1">
                {s.source_links.map(link => (
                  <span key={link.url} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60" title={link.url}>
                    <GitPullRequest className="lucide-inline shrink-0" />
                    {link.provider === 'github' ? `#${link.number}` : `!${link.number}`}
                    {(link.state === 'merged' || link.state === 'closed') && (
                      <span className={`capitalize ${link.state === 'merged' ? 'text-aim' : 'text-danger'}`}>{link.state}</span>
                    )}
                    {link.ci === 'running' && <Loader2 className="lucide-inline shrink-0 animate-spin" aria-label="Checks running" />}
                    {link.ci === 'passed' && <Check className="lucide-inline shrink-0 text-ok" aria-label="Checks passed" />}
                    {link.ci === 'failed' && <X className="lucide-inline shrink-0 text-danger" aria-label="Checks failed" />}
                  </span>
                ))}
                {typeof s.source_links_total === 'number' && s.source_links_total > s.source_links.length && (
                  <span className="inline-flex items-center px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60" title={`${s.source_links_total - s.source_links.length} more pull request${s.source_links_total - s.source_links.length === 1 ? '' : 's'} in this session`}>
                    +{s.source_links_total - s.source_links.length}
                  </span>
                )}
              </div>
            )}
            {s.tags && s.tags.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {s.tags.map(tid => {
                  const t = tagById[tid]
                  if (!t) return null
                  return (
                    <span key={tid} data-testid={`slot-tag-${t.id}`} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>
                      {t.name}
                    </span>
                  )
                })}
              </div>
            )}
          </div>
          {isMobile ? (
            <div className="absolute top-1/2 -translate-y-1/2 right-1.5 flex items-center gap-0.5">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button type="button" className="text-muted/50 active:text-text p-1 cursor-pointer bg-transparent border-none" aria-label="More options" onMouseDown={e => e.stopPropagation()}><MoreVertical size={14} /></button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[160px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                  <SessionActionsMenu variant="dropdown" {...rowMenuProps} />
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          ) : (
            <IconButtonGroup reveal className="absolute top-1/2 -translate-y-1/2 right-1.5 has-[[data-state=open]]:opacity-100">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <IconButton title="More" aria-label="More options" onMouseDown={e => e.stopPropagation()}><MoreVertical size={12} /></IconButton>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[160px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                  <SessionActionsMenu variant="dropdown" {...rowMenuProps} />
                </DropdownMenuContent>
              </DropdownMenu>
              <IconButton variant="accent" title="Duplicate" aria-label="Duplicate" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); sessionActions.duplicate(s.key) }}><Copy size={12} /></IconButton>
              <IconButton variant="danger" title="Close" aria-label="Close session" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); sessionActions.close(s.key) }}><X size={12} /></IconButton>
            </IconButtonGroup>
          )}
        </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="min-w-[160px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
            <SessionActionsMenu variant="context" {...rowMenuProps} />
          </ContextMenuContent>
        </ContextMenu>
          )}
        </DndDraggable>
        {showDivider && <div className="mx-3 border-b border-border" />}
      </motion.div>
    )
  }

  // ── Folder row: matches session-row width (full width minus drawer padding) ──
  // Recursively check if a folder or any descendant contains an unread slot.
  const folderTreeHasUnread = (folderId: string): boolean => {
    for (const k of unreadSet) { if (slotFolders[k] === folderId) return true }
    return folders.some(f => f.parent_id === folderId && folderTreeHasUnread(f.id))
  }

  const renderFolderHeader = (folder: ChatFolder, dragHandleProps?: React.HTMLAttributes<HTMLElement>) => {
    const childFolders = folders.filter(f => f.parent_id === folder.id)
    const childSlots = filteredSlots.filter(s => slotFolders[s.key] === folder.id)
    const count = childSlots.length + childFolders.length
    const hasUnread = folderTreeHasUnread(folder.id)
    // The whole header is the drag-to-reorder handle (pointer listeners forwarded
    // via dragHandleProps). The PointerSensor activation distance keeps the
    // collapse toggle + action buttons clickable; drag is off while renaming.
    const draggable = !!dragHandleProps && editingId !== folder.id
    return (
      // Folder header doubles as the dnd-kit drag handle (dragHandleProps) and a
      // collapse toggle; the drag-handle listeners + spread props make a native
      // <button> impractical, so scope-disable the interaction/click rules.
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
      <div key={`folder-header-${folder.id}`}
        {...(draggable ? dragHandleProps : {})}
        className={`group relative flex items-center gap-2 pr-2 py-1.5 rounded-md cursor-pointer text-sm text-muted hover:text-text hover:bg-bg-hover transition-all ${draggable ? 'cursor-grab active:cursor-grabbing' : ''}`}
        style={{ paddingLeft: '8px' }}
        onClick={() => toggleCollapse(folder.id)}>
        <span data-testid={`folder-collapse-${folder.id}`} className="shrink-0 text-muted transition-transform duration-150" style={{ transform: folder.collapsed ? 'rotate(0deg)' : 'rotate(90deg)' }}>
          <ChevronRight size={14} />
        </span>
        <FolderGlyph icon={folder.icon} size={14} open={!folder.collapsed} />
        {editingId === folder.id && editScope === 'list' ? (
          <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[13px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
        ) : (
          // Double-click rename is a mouse-only power shortcut; the hover Rename
          // button is the accessible path, so scope-disable the interaction rule.
          // eslint-disable-next-line jsx-a11y/no-static-element-interactions
          <span className="flex-1 text-[13px] font-medium text-text truncate" title="Double-click to rename" onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}>{folder.name}</span>
        )}
        {hasUnread && folder.collapsed && <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--info)' }} />}
        <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
        {folder.default_agent && <span className="text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded-full shrink-0 truncate max-w-[60px]" title={`Default agent: ${folder.default_agent}`}>{folder.default_agent}</span>}
        {!(editingId === folder.id && editScope === 'list') && (
        <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
          <select className="text-[10px] text-muted bg-transparent border-none cursor-pointer outline-none max-w-[70px]" title="Default agent for new chats" value={folder.default_agent || ''} onClick={e => e.stopPropagation()} onChange={e => { e.stopPropagation(); updateFolderMutation.mutate({ id: folder.id, body: { default_agent: e.target.value } }) }}>
            <option value="">agent…</option>
            {installedAgents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
          <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none" title="Rename folder" aria-label="Rename folder" data-testid={`folder-rename-${folder.id}`} onClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}><Pencil size={12} /></button>
          <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none" title="New chat in folder" aria-label="New chat in folder" onClick={e => { e.stopPropagation(); createChatInFolder(folder.id) }}><MessageSquarePlus size={12} /></button>
          <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none" title="New subfolder" aria-label="New subfolder" onClick={e => { e.stopPropagation(); setCreatingIn(folder.id); setCreateScope('list'); setNewName('') }}><FolderPlus size={12} /></button>
          <FolderIconPicker currentIcon={folder.icon} size={12} onPick={icon => updateFolderMutation.mutate({ id: folder.id, body: { icon } })} onReset={() => updateFolderMutation.mutate({ id: folder.id, body: { regenerate_icon: true } })} />
          {folderOffersHide(folder, foldersWithActiveSubtree) && (
            <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none" data-testid={`folder-hide-${folder.id}`} title="Hide when empty" aria-label="Hide when empty" onClick={e => { e.stopPropagation(); updateFolderMutation.mutate({ id: folder.id, body: { hidden: true } }) }}><EyeOff size={12} /></button>
          )}
          <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-danger hover:bg-danger-subtle transition-all bg-transparent border-none" data-testid={`folder-delete-${folder.id}`} title="Delete folder" aria-label="Delete folder" onClick={e => { e.stopPropagation(); if (confirm(`Delete "${folder.name}"?`)) deleteFolderMutation.mutate(folder.id) }}><X size={12} /></button>
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
    for (const cf of childFolders.filter(cf => !isFolderHidden(cf))) {
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
    // New-subfolder name input sits after the existing subfolders, just above the
    // sessions — a new folder is appended (order = folder count), so it lands at the
    // bottom of the sibling folders, above the chats. The placeholder matches that.
    if (creatingIn === folder.id && createScope === 'list') {
      childNodes.push(
        <div key={`new-sub-${folder.id}`} className="py-1 pr-2" style={{ paddingLeft: '8px' }}>
          <Input ref={folderCreateInputRef} className="w-full py-1 text-[13px]" placeholder="Folder name…" value={newName} onChange={e => setNewName(e.target.value)} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => createFolder(newName, folder.id), onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') }, onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName, folder.id); else setCreatingIn(null) } })} />
        </div>
      )
    }
    childSlots.forEach((s, i) => {
      const isActive = activeSlot === s.key
      const nextIsActive = i < childSlots.length - 1 && activeSlot === childSlots[i + 1].key
      const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
      childNodes.push(renderSessionRow(s, depth + 1, showDivider))
    })
    // Hide folders with no matching children when searching or filtering unreads
    if ((slotFilter || activeFilters.size > 0) && childNodes.length === 0) return []
    // Wrap children in a bordered container so the folder's extent is visually
    // clear when multiple folders are open. Only wrap when there's content,
    // otherwise the FolderBody would render an empty 1px-tall strip with a line.
    // Inline "New chat" affordance at the end of the slot list when the folder
    // is expanded — a discoverable, always-visible way to start a session in
    // this folder (complements the hover ⊕ on the header, which also works when
    // the folder is collapsed). Hidden while searching/filtering to keep results
    // clean; always present otherwise, so an empty folder is no longer a dead-end.
    const showInlineNewChat = !(slotFilter || activeFilters.size > 0)
    const inlineNewChatBtn = (
      <button key={`folder-newchat-${folder.id}`} type="button"
        onClick={() => createChatInFolder(folder.id)}
        title="New chat in folder" aria-label={`New chat in ${folder.name}`}
        className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-[12px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
        <MessageSquarePlus size={13} className="shrink-0" /><span>New chat in folder</span>
      </button>
    )
    const bodyNodes: React.ReactNode[] = showInlineNewChat
      ? (childNodes.length > 0
          ? [inlineNewChatBtn,
             <div key={`folder-newchat-sep-${folder.id}`} className="mx-3 border-b border-border" />,
             ...childNodes]
          : [inlineNewChatBtn])
      : childNodes
    const wrapped = bodyNodes.length > 0 ? (
      <div key={`folder-children-${folder.id}`} className="border-l border-border mb-1 ml-3 pl-1 rounded-bl-md">
        {bodyNodes}
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
  const visibleRootFolders = useMemo(() => rootFolders.filter(f => !isFolderHidden(f)), [rootFolders, isFolderHidden])
  const rootFolderIds = useMemo(() => visibleRootFolders.map(f => f.id), [visibleRootFolders])
  const ungroupedSlots = filteredSlots.filter(s => !slotFolders[s.key])
  // True while actively dragging a session that currently lives in a folder.
  // Used to reveal the empty-state drop placeholder inside the "No folder"
  // group so there's always a reachable ungroup target.
  const draggingFolderedSession = activeDrag?.type === 'session' && !!slotFolders[activeDrag.id]
  // True while dragging a folder that currently has a parent — the only case
  // where "drop on the root lane to move to top level" applies.
  const draggingNestedFolder = activeDrag?.type === 'folder' && !!folders.find(f => f.id === activeDrag.id)?.parent_id

  // Narrow-sidebar header responsiveness: below ~256px the full "New chat"
  // label no longer fits next to the label + kebab, so collapse the create
  // button to icon-only; below ~200px also drop the "Sessions" label.
  const compactHeader = sidebarWidth < 256
  const tinyHeader = sidebarWidth < 200

  return (
    <div className="sidebar-inner bg-bg-elevated border border-border rounded-xl shadow-sm flex flex-col shrink-0 relative h-full" style={{ width: sidebarWidth }}>
      {/* Drag handle — mouse-only column resize gesture; no keyboard analogue. */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        className="sidebar-resize-handle absolute top-0 -right-[2px] w-[5px] h-full cursor-col-resize z-10 group/drag flex items-center justify-center"
        onMouseDown={e => { e.preventDefault(); sidebarDragging.current = true; sidebarStartX.current = e.clientX; sidebarStartW.current = sidebarWidth; document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none'; onDragChange?.(true) }}
      >
        <div className="w-[2px] h-full bg-transparent group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-colors duration-200" />
      </div>

      {/* Header */}
      <div className="flex justify-between items-center px-2 h-12">
        <div className={`flex items-center gap-1.5 min-w-0 flex-1 ${collapsible && !isMobile ? 'pl-8' : ''}`}>
          {!tinyHeader && <span className="text-[13px] font-medium text-muted uppercase tracking-[.04em] truncate">Sessions</span>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" title="More options" aria-label="More options"><MoreVertical size={14} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[180px]">
              <DropdownMenuItem onClick={() => { const isActive = tagColumnsEnabled && rawColumns.length > 0; const next = !isActive; const cfg = loadChatConfig(); saveChatConfig({ ...cfg, tagColumnsEnabled: next }); if (next && rawColumns.length === 0) { createColumnMutation.mutate({ name: '', tag_ids: [], mode: 'any' }) } }}>
                <Columns3 size={14} className={tagColumnsEnabled && rawColumns.length > 0 ? 'text-accent' : 'text-muted'} />
                {tagColumnsEnabled && rawColumns.length > 0 ? 'Switch to list view' : 'Switch to board view'}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setCleanupOpen(!cleanupOpen); setCleanupExpanded(false); setCleanupError('') }}>
                <BrushCleaning size={14} className="text-muted" />
                Clean up sessions
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setBulkModelOpen(true); setBulkModel(''); setBulkSkipRunning(true); setBulkModelError('') }}>
                <Cpu size={14} className="text-muted" />
                Switch all to model…
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setManageTagsOpen(o => !o)}>
                <TagIcon size={14} className="text-muted" />
                Manage tags…
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
              title="New chat" aria-label="New chat session" aria-busy={creatingSlot}>{creatingSlot ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}{!compactHeader && <span className="whitespace-nowrap">{creatingSlot ? 'Creating…' : 'New'}</span>}</button>
            <span className="w-px h-4 bg-accent-fg opacity-30" aria-hidden="true" />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  className="flex items-center justify-center w-6 h-7 cursor-pointer bg-transparent border-none text-accent-fg hover:bg-black/10 active:scale-95 transition-all"
                  title="Create…" aria-label="More create options"><ChevronDown size={13} /></button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[200px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                <DropdownMenuItem onClick={() => { createAutopilotMutation.mutate() }}>
                  <Zap size={14} className="text-accent" /> New autopilot chat
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => { suppressMenuRestoreRef.current = true; setCreatingIn('__root__'); setNewName('') }}>
                  <FolderPlus size={14} className="text-muted" /> New folder
                </DropdownMenuItem>
                {folders.length > 0 && (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>
                      <Folder size={14} className="text-muted" /> New chat in folder
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
                          <DropdownMenuItem key={f.id} style={{ paddingLeft: `${12 + depth * 16}px` }} onClick={() => { createChatInFolder(f.id); requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()) }}>
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

      {/* Split View (session grid) — opt-in durable surface. Pinned entry that
       *  opens/restores the grid; highlighted while the grid is showing. Clicking
       *  a session below leaves the grid (onSelectSlot), so this is the way back. */}
      {splitEnabled && (
        <button
          type="button"
          onClick={onOpenSplit}
          className={`mx-2 mb-1 flex items-center gap-2 px-2.5 py-1.5 rounded-md text-[13px] cursor-pointer bg-transparent border transition-colors ${splitActive ? 'border-accent text-accent bg-accent/10' : 'border-border text-muted hover:text-text hover:bg-bg-hover'}`}
          title="Split View — multi-pane session grid (⌘D)"
          aria-label="Open Split View"
          aria-pressed={splitActive}
        >
          <Columns2 size={14} className="shrink-0" />
          <span className="flex-1 text-left truncate">Split View</span>
          {splitActive && <Circle size={7} className="shrink-0 fill-current" />}
        </button>
      )}

      {/* Clean Up dialog */}
      {cleanupOpen && (() => {
        const archivable = cleanupPreview ? cleanupPreview.map(k => slots.find(s => s.key === k)).filter(Boolean) as Slot[] : []
        const noStale = cleanupPreview != null && cleanupPreview.length === 0 && !activeIsStale
        return (
          <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
            <div className="font-medium text-text-strong mb-2"><BrushCleaning size={14} className="lucide-inline" /> Clean Up Sessions</div>
            <div className="text-muted text-[12px] mb-2">Archive sessions with no activity in the last:</div>
            <div className="flex items-center gap-2 mb-3">
              {[1, 3, 7].map(d => (
                <button key={d} className={`px-2.5 py-1 rounded-md text-[12px] border transition-all cursor-pointer ${
                  cleanupDays === d ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
                }`} onClick={() => setCleanupDays(d)}>{d} day{d > 1 ? 's' : ''}</button>
              ))}
            </div>
            <div className="text-[12px] text-muted mb-3">
              {cleanupPreviewLoading
                ? 'Checking…'
                : cleanupPreviewError
                  ? <>Failed to load preview. <button className="text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })}>Retry</button></>
                  : noStale
                    ? 'No inactive sessions to archive.'
                    : cleanupPreview != null && <>
                      {archivable.length} session{archivable.length !== 1 ? 's' : ''} will be moved to older sessions.{activeIsStale ? ' (1 skipped — currently selected)' : ''} Pinned sessions are kept.
                      {archivable.length > 0 && (
                        <button className="ml-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => setCleanupExpanded(!cleanupExpanded)}>
                          {cleanupExpanded ? 'Hide' : 'Show'} {archivable.length} session{archivable.length !== 1 ? 's' : ''} ▸
                        </button>
                      )}
                      {cleanupExpanded && archivable.length > 0 && (
                        <div className="mt-2 max-h-32 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5">
                          {archivable.map(s => (
                            <div key={s.key} className="text-[12px] text-muted truncate py-0.5 px-1">
                              {s.title && s.title !== s.key ? s.title : s.key}
                              {(s.last_ts || s.created) && <span className="ml-1 text-[11px] opacity-60">{fmtRelativeTime(s.last_ts || s.created!)}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      </>
              }
            </div>
            <div className="flex items-center gap-2 justify-end">
              {cleanupError && <span className="text-[11px] text-danger flex-1">{cleanupError}</span>}
              <Btn className="text-[12px] px-3 py-1" onClick={() => setCleanupOpen(false)}>Cancel</Btn>
              <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={archivable.length === 0 || cleanupMutation.isPending || cleanupPreviewLoading} onClick={() => {
                setCleanupError('')
                cleanupMutation.mutate()
              }}>{cleanupMutation.isPending ? 'Archiving…' : `Archive ${archivable.length} session${archivable.length !== 1 ? 's' : ''}`}</Btn>
            </div>
          </div>
        )
      })()}

      {/* Switch-all-to-model dialog — mirrors the Clean Up panel. Picking a
       *  model applies it to every live session (each switch resets that
       *  session); running sessions are skipped by default (Mesh-1080). */}
      {bulkModelOpen && (
        <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="font-medium text-text-strong mb-2"><Cpu size={14} className="lucide-inline" /> Switch All Sessions</div>
          <div className="text-muted text-[12px] mb-2">Pick a model for every session. Switching a session <span className="text-danger">resets its conversation</span>.</div>
          <div ref={bulkListRef} role="listbox" aria-label="Model list" tabIndex={-1} onKeyDown={bulkOnListKeyDown} className="max-h-[220px] overflow-y-auto rounded-md border border-border bg-bg-elevated p-1 mb-2 outline-none">
            <ModelDropdownList models={bulkModelOptions} activeModel={bulkModel} onSelect={setBulkModel} />
          </div>
          {bulkRunningCount > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              <input type="checkbox" checked={bulkSkipRunning} onChange={e => setBulkSkipRunning(e.target.checked)} />
              Skip {bulkRunningCount} running session{bulkRunningCount !== 1 ? 's' : ''}
            </label>
          )}
          <div className="flex items-center gap-2 justify-end">
            {bulkModelError && <span className="text-[11px] text-danger flex-1">{bulkModelError}</span>}
            <Btn className="text-[12px] px-3 py-1" onClick={() => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') }}>Cancel</Btn>
            <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={!bulkModel || bulkAffectedCount === 0 || bulkModelMutation.isPending} onClick={() => { setBulkModelError(''); bulkModelMutation.mutate({ model: bulkModel, skipRunning: bulkSkipRunning }) }}>{bulkModelMutation.isPending ? 'Switching…' : `Switch ${bulkAffectedCount} session${bulkAffectedCount !== 1 ? 's' : ''}`}</Btn>
          </div>
        </div>
      )}

      {/* Manage-tags panel — mirrors the Clean Up / Switch All panels. Renders
       *  the shared TagManagerList in 'manage' mode (no column context), so tag
       *  CRUD is reachable in list view too, not only from a board column. */}
      {manageTagsOpen && (
        <div data-testid="manage-tags-panel" className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="flex items-center justify-between mb-2">
            <div className="font-medium text-text-strong"><TagIcon size={14} className="lucide-inline" /> Manage Tags</div>
            <button type="button" className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 leading-none" onClick={() => setManageTagsOpen(false)} aria-label="Close"><X size={13} /></button>
          </div>
          <div className="text-muted text-[12px] mb-2">Rename, flag as status, or delete tags. Changes apply everywhere tags are shown.</div>
          <TagManagerList mode="manage" />
        </div>
      )}

      {/* Search with inline sort/filter control */}
      <div className="px-2 pt-2 pb-1">
        <div className="relative">
          <SearchInput className={`w-full ${slotFilter ? (folders.length > 0 ? '[&>input]:pr-[76px]' : '[&>input]:pr-14') : (folders.length > 0 ? '[&>input]:pr-14' : '[&>input]:pr-9')}`} placeholder="Search sessions…" value={slotFilter} onChange={e => setSlotFilter(e.target.value)} />
          {slotFilter && (
            <button type="button" className={`absolute ${folders.length > 0 ? 'right-[56px]' : 'right-8'} top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors`} onClick={() => setSlotFilter('')} aria-label="Clear search"><X size={13} /></button>
          )}
          <div className="absolute right-1 inset-y-0 flex items-center gap-0.5">
            {/* Flat-view toggle only makes sense when folders exist — without
             *  them the list is already flat. */}
            {folders.length > 0 && (
            <button
              type="button"
              className={`relative w-6 h-6 rounded flex items-center justify-center cursor-pointer transition-colors border-none ${flatView ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`}
              onClick={toggleFlatView}
              title={flatView ? 'Back to folder view' : 'Flat view — all chats without folders'}
              aria-label={flatView ? 'Switch to folder view' : 'Switch to flat view (all chats without folders)'}
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
                  title="Sort & filter sessions"
                  aria-label="Sort and filter sessions"
                >
                  <ListFilter size={14} />
                  {filterCounts['unread'] > 0 && (
                    <span
                      aria-hidden="true"
                      className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-[3px] rounded-full bg-[var(--info)] text-white text-[10px] font-semibold leading-[14px] text-center pointer-events-none shadow-[0_0_4px_rgba(59,130,246,.5)]"
                    >
                      {filterCounts['unread'] > 99 ? '99+' : filterCounts['unread']}
                    </span>
                  )}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[180px]">
                <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em]">Filter</DropdownMenuLabel>
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
                          title={filterDef.description}
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
                            {filterDef.label}
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
                            <div className="px-1 pb-1 text-[11px] text-muted">Within</div>
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
                              <div className="mb-1">Custom</div>
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
                                  aria-label="Custom recency amount"
                                  className="w-12 shrink-0 px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text text-[12px]"
                                />
                                <select
                                  value={recentUnitDraft}
                                  onChange={e => changeRecentUnit(e.target.value as RecentUnit)}
                                  aria-label="Custom recency unit"
                                  className="flex-1 min-w-0 px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text text-[12px] cursor-pointer"
                                >
                                  <option value="minutes">min</option>
                                  <option value="hours">hours</option>
                                  <option value="days">days</option>
                                </select>
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
                      title={filterDef.description}
                      // Keep the menu open so multiple filters can be toggled.
                      onSelect={e => { e.preventDefault(); toggleFilter(filterDef.key) }}
                    >
                      {filterDef.icon(active)}
                      <span className="flex-1 truncate">{filterDef.label}{slotCount > 0 ? ` (${slotCount})` : ''}</span>
                      {active && <Check size={14} className="text-accent shrink-0" />}
                    </DropdownMenuItem>
                  )
                })}
                <DropdownMenuSeparator />
                <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em]">Sort by</DropdownMenuLabel>
                {SORT_OPTIONS.map(o => (
                  <DropdownMenuItem
                    key={o.value}
                    onSelect={() => { setSortKey(o.value); safeSetItem(SORT_LS_KEY, o.value) }}
                  >
                    <span className="flex-1">{o.label}</span>
                    {sortKey === o.value && <Check size={14} className="text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>
      {activeFilters.size > 0 && (
        <div className="px-3 pb-1 flex items-center gap-1.5 flex-wrap">
          {SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key)).map(filterDef => {
            const slotCount = filterCounts[filterDef.key] ?? 0
            return (
              <button
                key={filterDef.key}
                type="button"
                className="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors"
                style={{ background: `color-mix(in srgb, ${filterDef.color} 10%, transparent)`, color: filterDef.color, borderWidth: 1, borderColor: `color-mix(in srgb, ${filterDef.color} 30%, transparent)` }}
                onClick={() => toggleFilter(filterDef.key)}
                title={`Clear ${filterDef.label.toLowerCase()} filter`}
                aria-label={`Clear ${filterDef.label.toLowerCase()} filter`}
              >
                {filterDef.label}{filterDef.key === 'recent' ? ` · ${formatRecentWindow(recentWindowMs)}` : ''}{slotCount > 0 ? ` (${slotCount})` : ''}
                <X size={11} />
              </button>
            )
          })}
        </div>
      )}
      <LayoutGroup id="chat-slots">
        {flatView && folders.length > 0 ? (
          // Flat view: every chat exploded out of its folder into one lane.
          // Removes only the folder rendering hierarchy — sort, pin priority,
          // filters, and search all apply as usual (filteredSlots). No folder
          // tree, no DnD. Takes precedence over the tag-columns layout.
          // Inactive without folders (the toggle is hidden then too), so a
          // persisted flat preference can never strand the user.
          <motion.div layoutScroll className="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col" data-testid="flat-view-lane">
            {(() => {
              // Date segments (Today / Yesterday / Last 7 Days / …) between
              // rows — resurrects the 9bb0f71 active-list pattern: only for
              // date sorts (segments mislead on name/created order, same
              // guard as the history pane), and pinned rows render first
              // without segments since pinning overrides date order.
              const isDateSort = sortKey === 'date-desc' || sortKey === 'date-asc'
              const segOf = (s: Slot) => isDateSort && !pinned.has(s.key) ? dateSegment(s.last_ts || s.created) : ''
              let prevSeg = ''
              return filteredSlots.map((s, i) => {
                const seg = segOf(s)
                const showHeader = seg !== '' && seg !== prevSeg
                if (seg) prevSeg = seg
                const next = i < filteredSlots.length - 1 ? filteredSlots[i + 1] : null
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
            {filteredSlots.length === 0 && (
              <div className="px-3 py-4 text-[12px] text-muted">No sessions match</div>
            )}
            {!historyOpen && (
              <button
                type="button"
                onClick={() => { setHistoryOpen(true); dispatch(fetchHistory(false)) }}
                className="mt-1 mx-1 px-2 py-1.5 text-left text-[12px] text-muted hover:text-accent hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
              >
                Show all older sessions
              </button>
            )}
          </motion.div>
        ) : orderedColumns.length === 0 ? (
          // Legacy single-lane layout (identical to pre-columns behavior)
          <motion.div layoutScroll className="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col">
            {/* One DndContext owns folder reorder (sortable) + session drag-to-
             *  assign (draggable rows + droppable folder/root targets). */}
            <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
              // Droppable rects are normally snapshotted once at drag-start, but
              // this tree ANIMATES during drags (the dragged folder's body
              // collapses over 150ms; hovered collapsed folders auto-expand), so
              // the snapshot goes stale and drop targets diverge from the
              // cursor. While a drag is live, poll re-measurement (dnd-kit's
              // numeric `frequency` self-reschedules a measure loop) so rects
              // track the animating layout. Idle sessions keep the plain
              // strategy — no background measuring.
              measuring={activeDrag
                ? { droppable: { strategy: MeasuringStrategy.Always, frequency: 100 } }
                : { droppable: { strategy: MeasuringStrategy.Always } }}
              onDragStart={handleSidebarDragStart} onDragOver={handleSidebarDragOver} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
              {/* Root lane is the fallback drop target: dropping a session on
               *  empty space (not over a folder) ungroups it (folderId: null). */}
              <DndDroppable id="root-lane" data={{ type: 'folder-drop', folderId: null }}>
                {({ setNodeRef }) => (
                  <div ref={setNodeRef} className="flex flex-col flex-1 min-h-0">
                    <SortableContext items={rootFolderIds} strategy={verticalListSortingStrategy}>
                      {visibleRootFolders.map(f => <SortableFolderBlock key={f.id} folder={f} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} renderFolderBlock={renderFolderBlock} />)}
                    </SortableContext>
                    {creatingIn === '__root__' && (
                      <div className="px-2 py-1">
                        <Input ref={folderCreateInputRef} className="w-full py-1 text-[13px]" placeholder="Folder name…" value={newName} onChange={e => setNewName(e.target.value)} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => createFolder(newName), onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') }, onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName); else setCreatingIn(null) } })} />
                      </div>
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
                            {/* In-flow discovery affordance: a text button just below the
                                last session that expands the Older Sessions pane. Hidden
                                once open. */}
                            {!historyOpen && (
                              <button
                                type="button"
                                onClick={() => { setHistoryOpen(true); dispatch(fetchHistory(false)) }}
                                className="mt-1 mx-1 px-2 py-1.5 text-left text-[12px] text-muted hover:text-accent hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
                              >
                                Show all older sessions
                              </button>
                            )}
                            {ungroupedSlots.length === 0 && draggingFolderedSession && <RootDropHint />}
                          </div>
                        )}
                      </DndDroppable>
                    )}
                  </div>
                )}
              </DndDroppable>
              <DragOverlay dropAnimation={null}>
                {activeDrag ? (() => {
                  if (activeDrag.type === 'folder') {
                    return <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} />
                  }
                  const ds = slots.find(x => x.key === activeDrag.id)
                  const label = ds?.title && ds.title !== ds.key ? ds.title : (ds?.key ?? activeDrag.id)
                  return <div className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-[13px] text-text shadow-lg max-w-[240px] truncate pointer-events-none">{label}</div>
                })() : null}
              </DragOverlay>
            </DndContext>
          </motion.div>
        ) : (
          // Trello-style horizontal column strip
          <div className="flex-1 overflow-x-auto overflow-y-hidden flex gap-2 p-2" data-testid="column-strip">
            {orderedColumns.map((col, colIdx) => {
              const colSlots = filteredSlots.filter(s => columnMatches(col, s.tags || []))
              const colTags = col.tag_ids.map(tid => tagById[tid]).filter(Boolean) as ChatTag[]
              const isStatusLane = colTags.length === 1 && !!colTags[0].status
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
                      title="Drag to reorder">
                      <GripVertical size={12} />
                    </span>
                    <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
                      {colTags.length === 0 ? (
                        <span className="text-[11px] text-muted font-semibold uppercase tracking-wider">{col.name || (col.include_untagged ? 'Untagged' : 'All sessions')}</span>
                      ) : (
                        <>
                          {colTags.map(t => (
                            <span key={t.id} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>{t.name}</span>
                          ))}
                          {col.include_untagged && <span className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border border-dashed border-muted text-muted" title="Also shows untagged sessions">+ untagged</span>}
                        </>
                      )}
                      {col.name && colTags.length > 0 && <span className="text-[11px] text-muted ml-1">· {col.name}</span>}
                    </div>
                    <span className="text-[11px] text-muted shrink-0">{colSlots.length}</span>
                    <button type="button" data-testid={`column-new-folder-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title="New folder" aria-label="New folder" onClick={() => { setCreatingIn(`__col_${col.id}__`); setNewName('') }}><FolderPlus size={12} /></button>
                    <button type="button" data-testid={`column-edit-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title="Filter & manage tags" aria-label="Filter & manage tags" onClick={() => setColumnEditId(columnEditId === col.id ? null : col.id)}><TagIcon size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-add-after-${col.id}`}
                      className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px] disabled:cursor-wait disabled:opacity-50"
                      title="Add column after this one"
                      aria-label="Add column after this one"
                      disabled={addColumnAfterMutation.isPending}
                      onClick={() => addColumnAfterMutation.mutate(col.id)}
                    ><Plus size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-delete-${col.id}`}
                      className="text-muted hover:text-danger bg-transparent border-none cursor-pointer shrink-0 p-[2px]"
                      title="Delete column"
                      aria-label="Delete column"
                      onClick={() => { if (confirm('Delete this column?')) deleteColumnMutation.mutate(col.id) }}
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
                    <div ref={columnPopoverRef} role="dialog" aria-label={`Filter tags: ${col.name || 'column'}`} tabIndex={-1} data-column-popover={col.id}
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
                        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">Column filter</span>
                        <button className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0" onClick={() => closeColumnPopover(col.id)} aria-label="Close"><X size={13} /></button>
                      </div>
                      <Input className="w-full py-1 text-[12px] mb-2" placeholder="Column name (optional)" defaultValue={col.name} onBlur={e => { const v = e.target.value.trim(); if (v !== col.name) updateColumnMutation.mutate({ id: col.id, body: { name: v } }) }} />
                      <div className="flex items-center gap-1 mb-2" role="radiogroup" aria-label="Match mode">
                        {(['any', 'all', 'none'] as const).map(m => (
                          <button key={m} role="radio" aria-checked={col.mode === m} className={`text-[11px] px-2 py-0.5 rounded cursor-pointer border transition-all ${col.mode === m ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text'}`} onClick={() => updateColumnMutation.mutate({ id: col.id, body: { mode: m } })}>{m}</button>
                        ))}
                      </div>
                      <label htmlFor={`column-include-untagged-${col.id}`} className="flex items-center gap-2 px-1 py-1 mb-2 text-[11px] text-muted cursor-pointer select-none hover:text-text" title="Also show sessions that have no tags at all">
                        <input
                          type="checkbox"
                          id={`column-include-untagged-${col.id}`}
                          data-testid={`column-include-untagged-${col.id}`}
                          aria-label="Include untagged sessions"
                          checked={!!col.include_untagged}
                          onChange={e => updateColumnMutation.mutate({ id: col.id, body: { include_untagged: e.target.checked } })}
                          className="cursor-pointer"
                        />
                        Include untagged sessions
                      </label>
                      <TagManagerList
                        mode="column-filter"
                        selectedIds={col.tag_ids}
                        onToggleTag={(_tagId, nextIds) => updateColumnMutation.mutate({ id: col.id, body: { tag_ids: nextIds } })}
                        createTestId={`tag-create-${col.id}`}
                      />
                      <div className="mt-2 flex justify-end">
                        <button className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { updateColumnMutation.mutate({ id: col.id, body: { tag_ids: [] } }) }}>Clear filter</button>
                      </div>
                    </div>,
                    document.body
                  )}
                  <div className="flex-1 overflow-y-auto p-1.5 flex flex-col">
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
                          <DndContext sensors={dndSensors} collisionDetection={closestCenter} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }} onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
                            <SortableContext items={relevantFolders.map(f => f.id)} strategy={verticalListSortingStrategy}>
                              {relevantFolders.map(f => <SortableColumnFolder key={f.id} folder={f} columnId={col.id} colSlotKeys={colSlotKeys} renderColumnFolder={renderColumnFolder} />)}
                            </SortableContext>
                            {/* Compact ghost follows the pointer while a folder drags —
                             *  same visual as the list-view overlay. DragOverlay renders
                             *  null unless THIS column's DndContext has an active drag,
                             *  so per-column overlays never stack. */}
                            <DragOverlay dropAnimation={null}>
                              {activeDrag?.type === 'folder' ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} /> : null}
                            </DragOverlay>
                          </DndContext>
                          {(creatingIn === `__col_${col.id}__` || (creatingIn === '__root__' && colIdx === 0)) && (
                            <div className="px-2 py-1">
                              <Input ref={folderCreateInputRef} className="w-full py-1 text-[12px]" placeholder="Folder name…"
                                value={newName}
                                onChange={e => setNewName(e.target.value)}
                                {...ime.bindEnter<HTMLInputElement>({
                                  onEnter: () => createFolder(newName),
                                  onEscape: () => { cancelledRef.current = true; setCreatingIn(null); setNewName('') },
                                  onBlur: () => { if (cancelledRef.current) { cancelledRef.current = false; return } if (newName.trim()) createFolder(newName); else setCreatingIn(null) },
                                })} />
                            </div>
                          )}
                          {ungrouped.map((s, i) => {
                            const isActive = activeSlot === s.key
                            const nextIsActive = i < ungrouped.length - 1 && activeSlot === ungrouped[i + 1].key
                            const showDivider = i < ungrouped.length - 1 && !isActive && !nextIsActive
                            return renderSessionRow(s, 0, showDivider, col.id)
                          })}
                          {!hasAny && <div className="text-muted text-[12px] text-center py-4">No sessions</div>}
                        </>
                      )
                    })()}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </LayoutGroup>

      {/* When expanded: doubles as the resize handle (accent on hover, drag to resize, dbl-click to collapse).
          When collapsed: just a static 1px divider between sessions and the Older Sessions footer. */}
      {historyOpen ? (
        // Separator that doubles as a mouse-only resize handle (drag) / collapse
        // (double-click); no keyboard analogue, so scope-disable the rule.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize history pane"
          onMouseDown={onHistoryDragStart}
          onDoubleClick={() => setHistoryOpen(false)}
          className="relative h-[6px] cursor-ns-resize z-10 group/drag flex items-center justify-center select-none"
        >
          <div className={`w-full transition-all duration-200 ${historyDragging ? 'h-[2px] bg-accent-hover' : 'h-px bg-border group-hover/drag:h-[2px] group-hover/drag:bg-accent'}`} />
        </div>
      ) : (
        <div className="border-t border-border" />
      )}
      {/* Older Sessions footer — the persistent collapse/expand header for the
          history pane. The inline "Show all older sessions" button (above, below
          the last session) is the in-flow discovery affordance. Whole row is the
          click target; the Clear button stops propagation. */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => { setHistoryOpen(!historyOpen); if (!historyOpen) dispatch(fetchHistory(false)) }}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setHistoryOpen(!historyOpen); if (!historyOpen) dispatch(fetchHistory(false)) } }}
        className="flex justify-between items-center px-3 py-3 cursor-pointer select-none"
        aria-expanded={historyOpen}
        aria-controls="history-pane"
        aria-label="Older sessions"
      >
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong leading-none">
          <ChevronRight size={16} className={`shrink-0 transition-transform duration-200 ${historyOpen ? 'rotate-90' : '-rotate-90'}`} />
          <Clock size={14} className="shrink-0" />
          <span className="leading-none">Older Sessions</span>
        </span>
        {historyOpen && history.length > 0 && (
          <button
            className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all"
            onClick={async e => { e.stopPropagation(); if (confirm('Clear closed sessions? Active tabs and pinned sessions will be kept.')) { await api.clearSessions(); dispatch(fetchHistory(false)) } }}
          >Clear</button>
        )}
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
                <SearchInput className="w-full" placeholder="Search older sessions…" value={historyFilter} onChange={e => setHistoryFilter(e.target.value)} />
                {historyFilter && (
                  <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setHistoryFilter('')} aria-label="Clear search"><X size={13} /></button>
                )}
              </div>
            </div>
            <div className="overflow-y-auto p-2 scroll-shadow" style={{ height: `${historyHeight}px` }}>
              {(() => {
                const filteredHistory = (historySearchResults ?? history).filter(s => {
                  if (!historyFilter) return true
                  if (historyFilter.trim().length >= SEARCH_MIN_CHARS) {
                    if (historySearchResults) return true
                    return ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                  }
                  return ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                })
                // Hide date segments when the user has an active search — results are
                // Segments only make sense when the list is date-ordered. For name/created
                // sorts (or active search, which is relevance-ranked) they'd interleave.
                const showSegments = !(historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults)
                  && (sortKey === 'date-desc' || sortKey === 'date-asc')
                // Skip the sort only when the backend already returns date-desc order, i.e.
                // no active search (search results are relevance-ranked, not date-ranked).
                const sortedHistory = (sortKey === 'date-desc' && !historySearchResults) ? filteredHistory : [...filteredHistory].sort((a, b) => compareBySort(a, b, sortKey))
                let prevSeg = ''
                // Derive agent color the same way renderSessionRow does so history rows
                // match the session-row visual language (agent name tinted by source).
                const agentColorFor = (agentName: string): string => {
                  const meta = installedAgents.find(a => a.name === agentName)
                  if (meta?.source === 'aim') return 'text-[var(--aim)]'
                  if (meta?.source === 'builtin') return 'text-muted'
                  return 'text-accent'
                }
                const historyRow = (s: (typeof sortedHistory)[number]) => {
                  const displayDate = fmtRelativeTime(s.modified ?? s.created)
                  const agentName = s.agent || defaultAgent || ''
                  const agentColor = agentColorFor(agentName)
                  const isDashboard = s.key.startsWith('dashboard')
                  return (
                    <div className={`group relative flex items-start gap-2.5 pr-4 py-2 rounded-md text-sm transition-all select-none ${!connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'}`} style={{ paddingLeft: '10px' }} title={s.title || s.key} {...offlineProps(connected, 'resume sessions')} role="button" tabIndex={0} aria-disabled={!connected} onKeyDown={e => {
                      // WCAG 2.1.1: history rows must be resumable via keyboard.
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      if ((e.target as HTMLElement) !== e.currentTarget) return
                      e.preventDefault()
                      if (!connected) return
                      dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
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
                      if ((e.target as HTMLElement).closest?.('[data-close]')) { if (confirm('Are you sure you want to delete this history session?')) dispatch(deleteHistorySession(s.key)); return }
                      if (!connected) return
                      dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
                    }}>
                      {/* Platform glyph — fills the left column that session rows reserve for the unread dot */}
                      <span role="img" className="shrink-0 flex items-center justify-center self-center text-muted" title={isDashboard ? 'Dashboard session' : 'Slack session'} aria-label={isDashboard ? 'Dashboard session' : 'Slack session'}>
                        {isDashboard
                          ? <Monitor size={12} />
                          : <MessageSquare size={12} />
                        }
                      </span>
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <div className={`text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
                          <span className="truncate">{agentName || '\u00A0'}</span>
                          {s.clean_mode
                            ? <span className="text-accent" title="Clean — agent-only, no KiroCrew context or MCP"><Droplet size={10} /></span>
                            : <>
                                {s.memory_mode === 'incognito' && <span className="text-muted" title="Incognito — no memory writes"><EyeOff size={10} /></span>}
                                {s.memory_mode === 'temporary' && <span className="text-aim" title="Temporary — no memory reads or writes"><VenetianMask size={10} /></span>}
                              </>}
                          {displayDate && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{displayDate}</span>}
                        </div>
                        <div className="text-[13px] leading-snug line-clamp-2 break-words">{s.title || s.key}</div>
                      </div>
                      {/* Floating hover button group — matches session-row pattern */}
                      <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
                        <button type="button" title="Delete history session" aria-label="Delete history session" className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger hover:bg-danger-subtle transition-all bg-transparent border-none" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); if (confirm('Are you sure you want to delete this history session?')) dispatch(deleteHistorySession(s.key)) }}><X size={12} /></button>
                      </div>
                    </div>
                  )
                }
                // Folder-grouped view: during an active content search, regroup the
                // relevance-ranked results under collapsible folder headers (+ Unfiled)
                // by the folder each session was filed in, instead of date segments.
                if (historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults) {
                  return groupHistoryByFolder(sortedHistory, folders).map(({ key: gid, folder, rows }) => {
                    const collapsed = collapsedHistoryGroups.has(gid)
                    return (
                      <Fragment key={gid}>
                        <button type="button" aria-expanded={!collapsed} aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${folder ? folder.name : 'Unfiled'} results`} className="w-full flex items-center gap-1.5 px-2 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none cursor-pointer hover:text-text first:pt-1" onClick={() => setCollapsedHistoryGroups(prev => { const next = new Set(prev); if (next.has(gid)) next.delete(gid); else next.add(gid); return next })}>
                          {collapsed ? <ChevronRight size={12} className="shrink-0" /> : <ChevronDown size={12} className="shrink-0" />}
                          {folder ? <FolderGlyph icon={folder.icon} size={12} open={!collapsed} /> : <Folder size={12} className="text-muted shrink-0" />}
                          <span className="truncate">{folder ? folder.name : 'Unfiled'}</span>
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
              {historyHasMore && <div className="flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md" onMouseDown={e => { e.preventDefault(); dispatch(fetchHistory(true)) }}>Load more…</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

    </div>
  )
}

export default memo(ChatSidebar)
