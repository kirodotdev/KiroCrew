/** Per-machine groups in the Sessions list: a `Local` header over the caller's
 *  own lane, then one collapsible group per crew (`crewGroupsFor`). Rendered only
 *  while at least one crew group exists, so a single-machine sidebar draws none
 *  of this. Collapsing a group hides its rows and never changes the selection.
 *  Inside a group the crew's own folders (its spaces) nest its rows, unfiled
 *  rows last: a read-only mirror of the crew's tree, edited on the crew. */
import { Loader, MessageCircleQuestionMark, Server, ShieldCheck } from 'lucide-react'
import { createContext, useCallback, useMemo, useState, type ReactNode } from 'react'
import { FolderBody } from '../../components/FolderBody'
import FolderGlyph from '../../components/FolderGlyph'
import { orderFoldersWithPaths } from '../../utils/folderTree'
import type { ChatFolder } from '../../types'
import type { CrewBadge, CrewGroup, PeerFolder } from '../../hooks/useInstanceSessions'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { tabStatus } from '../../lib/sessionTabs'
import { useAppDispatch } from '../../store'
import { switchSlot } from '../../store/chatSlice'
import { safeSetItem } from '../../utils/safeStorage'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { CREW_COLLAPSED_LS_KEY, CREW_SPACE_COLLAPSED_LS_KEY, readCollapsedCrews } from './persistence'
import { opensElsewhereOf } from './rows'
import type { Slot } from './types'

const HEADER_BOX_CLS = 'flex items-center gap-1.5 pl-2 pr-3 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none text-left'
const HEADER_CLS = `w-full ${HEADER_BOX_CLS}`

/** True inside an offline crew group's body, so a row can say it is not
 *  reachable. A context, not a scope suffix: the row scope must stay stable
 *  across a disconnect, or an open rename on that row is dropped. */
export const CrewOfflineContext = createContext(false)

const BADGE_CLS: Record<CrewBadge, string> = {
  online: 'text-ok',
  reconnecting: 'text-warn',
  error: 'text-danger',
  offline: 'text-muted',
  disabled: 'text-muted',
}

const badgeLabel = (badge: CrewBadge): string => (
  badge === 'online' ? i18nT('pages.chatSidebar.crew_status_online')
    : badge === 'reconnecting' ? i18nT('pages.chatSidebar.crew_status_reconnecting')
      : badge === 'error' ? i18nT('pages.chatSidebar.crew_status_error')
        : badge === 'disabled' ? i18nT('pages.chatSidebar.crew_status_disabled')
          : i18nT('pages.chatSidebar.crew_status_offline')
)

/** Collapsed crew ids, persisted in localStorage so a reload keeps the user's
 *  choice. `expand` opens the given ids and is a no-op for those already open.
 *  `key` picks the store: the crews themselves, or their spaces. */
export function useCollapsedCrews(key = CREW_COLLAPSED_LS_KEY): [ReadonlySet<string>, (id: string) => void, (...ids: string[]) => void] {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => readCollapsedCrews(key))
  const toggle = useCallback((id: string) => setCollapsed(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    safeSetItem(key, JSON.stringify([...next]))
    return next
  }), [key])
  const expand = useCallback((...ids: string[]) => setCollapsed(prev => {
    if (!ids.some(id => prev.has(id))) return prev
    const next = new Set(prev)
    for (const id of ids) next.delete(id)
    safeSetItem(key, JSON.stringify([...next]))
    return next
  }), [key])
  return [collapsed, toggle, expand]
}

/** The collapsed spaces inside every crew group, one store for the sidebar. */
export const useCollapsedCrewSpaces = () => useCollapsedCrews(CREW_SPACE_COLLAPSED_LS_KEY)

/** One space's id in that store: the crew, then the crew's own folder id. */
export const crewSpaceId = (crew: string, folder: string): string => JSON.stringify([crew, folder])

/** The crew's folders that hold one of `rows`, as a tree, plus the rows each
 *  holds and the unfiled rest. A row whose folder the crew did not list is
 *  unfiled; a folder whose parent it did not list sits at the top. Folders with
 *  no row anywhere below them are left out, as an empty space shows nothing. */
export function crewSpaces<R extends { peer_folder_id?: string }>(folders: readonly PeerFolder[], rows: readonly R[]) {
  const byId = new Map(folders.map(f => [f.id, f]))
  const rowsIn = new Map<string, R[]>()
  const unfiled: R[] = []
  for (const r of rows) {
    const id = r.peer_folder_id
    if (id && byId.has(id)) {
      const list = rowsIn.get(id)
      if (list) list.push(r); else rowsIn.set(id, [r])
    } else unfiled.push(r)
  }
  // A folder is shown when it or a descendant holds a row. Walk up from every
  // filled folder; `seen` stops a parent cycle a peer could send.
  const shown = new Set<string>()
  const total = new Map<string, number>()
  for (const [id, list] of rowsIn) {
    const seen = new Set<string>()
    for (let cur: PeerFolder | undefined = byId.get(id); cur && !seen.has(cur.id); cur = cur.parent_id ? byId.get(cur.parent_id) : undefined) {
      seen.add(cur.id)
      shown.add(cur.id)
      total.set(cur.id, (total.get(cur.id) ?? 0) + list.length)
    }
  }
  // The sidebar's own tree walk (`orderFoldersWithPaths`): stored order, then
  // name; orphans and cycle members at the top; depth-guarded. It returns the
  // shown folders in pre-order with a depth, rebuilt here into the parent ->
  // children map the group renders.
  const children = new Map<string, PeerFolder[]>()
  const atDepth: string[] = []
  const shownFolders = [...byId.values()].filter(f => shown.has(f.id)) as ChatFolder[]
  for (const { folder, depth } of orderFoldersWithPaths(shownFolders)) {
    atDepth.length = depth
    const parent = depth > 0 ? atDepth[depth - 1] ?? '' : ''
    const list = children.get(parent)
    if (list) list.push(folder as PeerFolder); else children.set(parent, [folder as PeerFolder])
    atDepth[depth] = folder.id
  }
  return { children, rowsIn, total, unfiled }
}

/** The ids to open so a row filed in `folder` shows: that space and every space
 *  above it. */
export function crewSpaceChain(crew: string, folders: readonly PeerFolder[], folder: string | undefined): string[] {
  const byId = new Map(folders.map(f => [f.id, f]))
  const out: string[] = []
  for (let cur = folder ? byId.get(folder) : undefined; cur && !out.includes(crewSpaceId(crew, cur.id)); cur = cur.parent_id ? byId.get(cur.parent_id) : undefined) {
    out.push(crewSpaceId(crew, cur.id))
  }
  return out
}

/** A group's rows in the order it draws them: inside each space its subspaces
 *  first, then its own rows, with the unfiled rows last (`CrewSpaceTree`); flat
 *  when the group draws no spaces. Every row appears once. */
export function crewDrawOrder<R extends { peer_folder_id?: string }>(folders: readonly PeerFolder[] | undefined, rows: readonly R[]): readonly R[] {
  if (!folders?.length) return rows
  const tree = crewSpaces(folders, rows)
  const out: R[] = []
  const walk = (parent: string) => {
    for (const f of tree.children.get(parent) ?? []) {
      walk(f.id)
      out.push(...(tree.rowsIn.get(f.id) ?? []))
    }
  }
  walk('')
  out.push(...tree.unfiled)
  return out
}

/** What a row asks of the user, in urgency order. */
const CREW_ATTENTION_KINDS = ['approval', 'question', 'working'] as const
type CrewAttentionKind = typeof CREW_ATTENTION_KINDS[number]

const NO_UNREAD: readonly string[] = []

/** The one kind a row counts toward, in the session tabs' own status order
 *  (`tabStatus`): an owed approval, then an unanswered question, then a running
 *  turn. A blocking card keeps the turn running, so the order is what stops one
 *  row from counting twice. */
function crewAttentionOf(row: Slot, isRunning: (row: Slot) => boolean): CrewAttentionKind | null {
  const status = tabStatus({ pending_approval: row.pending_approval, needs_input: row.needs_input, running: isRunning(row) }, NO_UNREAD, row.key)
  return status === 'permission' ? 'approval' : status === 'question' ? 'question' : status === 'running' ? 'working' : null
}

export interface CrewAttention {
  counts: Record<CrewAttentionKind, number>
  /** The most urgent kind with a row, and that kind's first row in draw order:
   *  the chat the header's button opens. */
  kind: CrewAttentionKind
  target: Slot
}

/** The header's read-out over the group's shown rows, or null when no row asks
 *  for anything. */
export function crewAttention(rows: readonly Slot[], folders: readonly PeerFolder[] | undefined, isRunning: (row: Slot) => boolean): CrewAttention | null {
  const counts: Record<CrewAttentionKind, number> = { approval: 0, question: 0, working: 0 }
  const first: Partial<Record<CrewAttentionKind, Slot>> = {}
  for (const row of crewDrawOrder(folders, rows)) {
    const kind = crewAttentionOf(row, isRunning)
    if (!kind) continue
    counts[kind] += 1
    first[kind] ??= row
  }
  const kind = CREW_ATTENTION_KINDS.find(k => counts[k] > 0)
  return kind ? { counts, kind, target: first[kind]! } : null
}

/** What the header's attention button needs from the sidebar: the row model's
 *  running signal and the actions a row's own click calls, so the button opens
 *  its chat exactly as that click does. */
interface CrewAttentionWiring {
  isRunning: (row: Slot) => boolean
  /** The gateway connection. While it is down a row click does nothing. */
  connected: boolean
  actions: {
    onOpenPeerSession: (instanceId: string, remoteSlot: string) => void
    openElsewhere: (path: string) => void
    onSelectSlot?: (key: string) => void
  }
}

const ATTENTION_GLYPH = {
  approval: <ShieldCheck size={10} className="shrink-0 text-warn" aria-hidden="true" />,
  question: <MessageCircleQuestionMark size={10} className="shrink-0 text-info" aria-hidden="true" />,
  // Static: the button names a state, it does not animate one.
  working: <Loader size={10} className="shrink-0 text-accent" aria-hidden="true" />,
} satisfies Record<CrewAttentionKind, ReactNode>

const attentionCount = (kind: CrewAttentionKind, count: number): string => (
  kind === 'approval' ? i18nT('pages.chatSidebar.crew_attention_approval', { count })
    : kind === 'question' ? i18nT('pages.chatSidebar.crew_attention_question', { count })
      : i18nT('pages.chatSidebar.crew_attention_working', { count })
)

/** Where the button goes, named by the kind of chat it opens. */
const attentionTarget = (kind: CrewAttentionKind): string => (
  kind === 'approval' ? i18nT('pages.chatSidebar.crew_attention_go_approval')
    : kind === 'question' ? i18nT('pages.chatSidebar.crew_attention_go_question')
      : i18nT('pages.chatSidebar.crew_attention_go_working')
)

/** Enable for a disabled crew, plus the notice for a failed one. The sidebar
 *  holds it, not the group: once the flag is cleared an unreachable crew with no
 *  rows drops out of the groups, and its failure must still be on screen. */
export function useCrewEnable(enable: (id: string) => Promise<unknown>, nameOf: (id: string) => string) {
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null)
  const onEnable = useCallback((id: string) => {
    setFailure(null)
    enable(id).catch((e: unknown) => setFailure({ id, message: (e as Error)?.message || String(e) }))
  }, [enable])
  const notice = failure && (
    <ErrorNotice
      key="crew-enable-error"
      title={i18nT('pages.chatSidebar.crew_enable_failed', { name: nameOf(failure.id) })}
      message={failure.message}
      askAgent
      actionPlacement="below"
      className="mx-2 mb-1"
      testId="crew-enable-error"
      onDismiss={() => setFailure(null)}
    />
  )
  return { onEnable, notice }
}

export function LocalGroupHeader() {
  return (
    <div className={HEADER_CLS} data-testid="machine-group-local">
      <span className="min-w-0 truncate">{i18nT('pages.chatSidebar.machine_group_local')}</span>
    </div>
  )
}

/** One crew's group: header with chevron, name, badge and chat count, then its
 *  rows. An offline crew's rows are the last cached answer, drawn dimmed. Beside
 *  the header's toggle, one button says what the shown rows ask of the user and
 *  opens the most urgent of them. */
export function CrewGroupSection({ group, rows, collapsed, onToggle, hideWhenEmpty, chevron, renderRows, onEnable, spaces, attention }: {
  group: CrewGroup
  /** The crew's spaces: which are closed, how to toggle one, and the sidebar's
   *  own folder body (its connector line and inset), so a space nests exactly
   *  as a local folder does. Absent, the group lists its rows flat. */
  spaces?: { collapsed: ReadonlySet<string>; onToggle: (id: string) => void; body: (open: boolean, children: ReactNode) => ReactNode }
  /** Turns a disabled crew back on; it reconnects. */
  onEnable?: (id: string) => void
  rows: Slot[]
  collapsed: boolean
  /** The sidebar's own disclosure chevron, passed in so this owner draws none. */
  chevron: ReactNode
  onToggle: (id: string) => void
  /** True while a filter or search narrows the list: an empty group then hides. */
  hideWhenEmpty: boolean
  renderRows: (rows: Slot[], scope: string) => ReactNode
  /** Absent, the header draws no attention button. */
  attention?: CrewAttentionWiring
}) {
  const dispatch = useAppDispatch()
  const drawnFolders = spaces ? group.folders : undefined
  const isRunning = attention?.isRunning
  // From the same rows the chat count counts, so a filter moves both together.
  // No read-out while offline: like the count, the cached rows are not current.
  const asks = useMemo(
    () => (isRunning && !group.offline ? crewAttention(rows, drawnFolders, isRunning) : null),
    [rows, drawnFolders, isRunning, group.offline],
  )
  if (hideWhenEmpty && rows.length === 0) return null
  const regionId = `crew-group-rows-${group.id}`
  const shown = asks ? CREW_ATTENTION_KINDS.filter(k => asks.counts[k] > 0) : []
  // The name is the visible text with its pauses: the target, then the counts.
  const askLabel = asks
    ? i18nT('pages.chatSidebar.crew_attention_name', {
      target: attentionTarget(asks.kind),
      counts: fmtList(shown.map(k => attentionCount(k, asks.counts[k])), { type: 'unit' }),
    })
    : ''
  // A plain click on the target row (SessionRow's `onClick`), through the same row
  // actions: nothing while the gateway is down, a peer row as a window onto its
  // crew, a row whose session lives on another page there, else the local slot.
  const openTarget = (row: Slot, wiring: CrewAttentionWiring) => {
    // Through the header's own toggle, so the open group is stored as a click on
    // the header stores it.
    if (collapsed) onToggle(group.id)
    if (!wiring.connected) return
    if (row.peer_id) { wiring.actions.onOpenPeerSession(row.peer_id, row.key); return }
    const elsewhere = opensElsewhereOf(row)
    if (elsewhere) { wiring.actions.openElsewhere(elsewhere); return }
    dispatch(switchSlot({ key: row.key, announceOnMissing: true }))
    wiring.actions.onSelectSlot?.(row.key)
  }
  return (
    <section data-testid={`crew-group-${group.id}`} data-offline={group.offline ? '' : undefined}>
      {/* The row's inset is the crew name's own (the toggle's `pl-2`, the 11px
       *  chevron and server glyph and their gaps); the toggle cancels it, so where
       *  the attention button wraps onto a second line it starts under the name. */}
      <div className="flex flex-wrap items-end pl-[42px]">
        <button type="button" aria-expanded={!collapsed} aria-controls={regionId}
          data-testid={`crew-group-toggle-${group.id}`}
          onClick={() => onToggle(group.id)}
          className={`${HEADER_BOX_CLS} -ml-[42px] flex-auto min-w-0 cursor-pointer hover:text-accent`}>
          {chevron}
          <Server size={11} aria-hidden="true" className="shrink-0" />
          <span className="min-w-0 truncate">{group.name}</span>
          {group.badge && (
            <span className={`shrink-0 font-normal ${BADGE_CLS[group.badge]}`}
              data-testid={`crew-group-badge-${group.id}`} data-badge={group.badge}>
              {badgeLabel(group.badge)}
            </span>
          )}
          {/* No count while offline: the cached rows are not a current tally. */}
          {!group.offline && (
            <span className="ml-auto shrink-0 font-normal tabular-nums" data-testid={`crew-group-count-${group.id}`}>
              {i18nT('pages.chatSidebar.crew_group_chats', { count: rows.length })}
            </span>
          )}
        </button>
        {asks && attention && (
          // The visible text names the chat the click opens, so it cannot read as
          // answering the approval it counts; the counts sit behind a rule, as
          // status, and keep their muted ink on hover while only the target
          // underlines. Each count leads with its rows' glyph. The target may
          // wrap: a long translation breaks onto a second line rather than
          // overflowing.
          <Btn type="button" onClick={() => openTarget(asks.target, attention)}
            aria-label={askLabel}
            data-testid={`crew-group-attention-${group.id}`}
            className="group mr-3 mb-0.5 min-w-0 flex-wrap gap-x-2 gap-y-0 px-1.5 py-px text-left text-[11px] text-muted tabular-nums">
            <span className="font-medium text-accent group-hover:underline" data-testid={`crew-group-attention-target-${group.id}`}>
              {attentionTarget(asks.kind)}
            </span>
            <span className="inline-flex flex-wrap items-center gap-x-2 border-l border-border pl-2">
              {shown.map(k => (
                <span key={k} className="inline-flex items-center gap-0.5 whitespace-nowrap" data-testid={`crew-group-attention-${k}-${group.id}`}>
                  {ATTENTION_GLYPH[k]}
                  {attentionCount(k, asks.counts[k])}
                </span>
              ))}
            </span>
          </Btn>
        )}
      </div>
      {/* FolderBody keeps the rows mounted while closed, as folders do, so
       *  keyboard navigation and reveal can still reach them. */}
      <div id={regionId}>
        <FolderBody open={!collapsed}>
          {/* The tunnel's own error, through the shared error surface. A list read
           *  holds no draft, so the agent hand-off is on. */}
          {group.error && (
            <ErrorNotice
              title={i18nT('pages.chatSidebar.sessions_from_instance_unavailable', { names: group.name })}
              message={group.error}
              askAgent
              actionPlacement="below"
              className="mx-2 mb-1"
              testId={`crew-group-error-${group.id}`}
            />
          )}
          {/* The crew's folder read failed: its chats still list, unfiled. */}
          {!group.error && group.foldersError && (
            <ErrorNotice
              title={i18nT('pages.chatSidebar.crew_spaces_unavailable', { name: group.name })}
              message={group.foldersError}
              askAgent
              actionPlacement="below"
              className="mx-2 mb-1"
              testId={`crew-group-spaces-error-${group.id}`}
            />
          )}
          {group.disabled ? (
            <div className="px-3 pb-1 flex items-center gap-2 text-[11px] text-muted">
              <span className="min-w-0">{i18nT('pages.chatSidebar.crew_group_disabled')}</span>
              {onEnable && (
                <Btn onClick={() => onEnable(group.id)}
                  data-testid={`crew-group-enable-${group.id}`}
                  aria-label={i18nT('pages.chatSidebar.crew_enable_named', { name: group.name })}
                  className="shrink-0 text-[11px] py-0.5 px-2">
                  {i18nT('pages.chatSidebar.crew_enable')}
                </Btn>
              )}
            </div>
          ) : group.offline && (
            <div className="px-3 pb-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.crew_group_offline')}</div>
          )}
          {rows.length === 0
            ? <div className="px-3 py-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.crew_group_empty')}</div>
            : (
              <div className={group.offline ? 'opacity-50' : undefined} data-testid={`crew-group-body-${group.id}`}>
                <CrewOfflineContext.Provider value={group.offline}>
                  {spaces && group.folders?.length
                    ? <CrewSpaceTree crew={group.id} folders={group.folders} rows={rows} spaces={spaces} renderRows={renderRows} />
                    : renderRows(rows, `crew:${group.id}`)}
                </CrewOfflineContext.Provider>
              </div>
            )}
        </FolderBody>
      </div>
    </section>
  )
}

/** A local folder header's geometry (`px-3.5`, a 14px glyph, `gap-[5px]`), so the
 *  crew folder's glyph sits on its connector line and its name on its chats'
 *  text: docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md.
 *  No menu and no drag: the folder is edited on the crew. */
const SPACE_HEADER_CLS = 'w-full flex items-center gap-[5px] px-3.5 py-1.5 rounded-md text-sm text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none text-left cursor-pointer transition-all'

/** The crew's spaces, nested, then its unfiled rows. Every row keeps the
 *  group's one scope, so opening or closing a space never remounts a row. */
function CrewSpaceTree({ crew, folders, rows, spaces, renderRows }: {
  crew: string
  folders: readonly PeerFolder[]
  rows: Slot[]
  spaces: NonNullable<Parameters<typeof CrewGroupSection>[0]['spaces']>
  renderRows: (rows: Slot[], scope: string) => ReactNode
}) {
  const scope = `crew:${crew}`
  const tree = crewSpaces(folders, rows)
  const renderSpace = (f: PeerFolder): ReactNode => {
    const id = crewSpaceId(crew, f.id)
    const open = !spaces.collapsed.has(id)
    const regionId = `crew-space-rows-${crew}-${f.id}`
    const own = tree.rowsIn.get(f.id) ?? []
    return (
      <div key={f.id} data-testid={`crew-space-${crew}-${f.id}`}>
        <Btn type="button" aria-expanded={open} aria-controls={regionId}
          aria-label={open
            ? i18nT('pages.chatSidebar.collapse_folder_name', { name: f.name })
            : i18nT('pages.chatSidebar.expand_folder_name', { name: f.name })}
          data-testid={`crew-space-toggle-${crew}-${f.id}`}
          onClick={() => spaces.onToggle(id)}
          className={SPACE_HEADER_CLS}>
          <FolderGlyph size={14} open={open} />
          <span className="flex-1 min-w-0 text-[13px] font-medium text-text truncate">{f.name}</span>
          <span className="text-[11px] text-muted tabular-nums shrink-0">{tree.total.get(f.id) ?? 0}</span>
        </Btn>
        <div id={regionId}>
          {spaces.body(open, <>
            {(tree.children.get(f.id) ?? []).map(renderSpace)}
            {own.length > 0 && renderRows(own, scope)}
          </>)}
        </div>
      </div>
    )
  }
  return (
    <>
      {(tree.children.get('') ?? []).map(renderSpace)}
      {tree.unfiled.length > 0 && renderRows(tree.unfiled, scope)}
    </>
  )
}
