/** Per-machine groups in the Sessions list: a `Local` header over the caller's
 *  own lane, then one collapsible group per crew (`crewGroupsFor`). Rendered only
 *  while at least one crew group exists, so a single-machine sidebar draws none
 *  of this. Collapsing a group hides its rows and never changes the selection.
 *  Inside a group the crew's own folders (its spaces) nest its rows, unfiled
 *  rows last: a read-only mirror of the crew's tree, edited on the crew. */
import { Server } from 'lucide-react'
import { createContext, useCallback, useState, type ReactNode } from 'react'
import { FolderBody } from '../../components/FolderBody'
import FolderGlyph from '../../components/FolderGlyph'
import { orderFoldersWithPaths } from '../../utils/folderTree'
import type { ChatFolder } from '../../types'
import type { CrewBadge, CrewGroup, PeerFolder } from '../../hooks/useInstanceSessions'
import { i18nT } from '../../i18n/t'
import { safeSetItem } from '../../utils/safeStorage'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { CREW_COLLAPSED_LS_KEY, CREW_SPACE_COLLAPSED_LS_KEY, readCollapsedCrews } from './persistence'
import type { Slot } from './types'

const HEADER_CLS = 'w-full flex items-center gap-1.5 pl-2 pr-3 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none text-left'

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

/** One crew's group: header with chevron, name, badge and row count, then its
 *  rows. An offline crew's rows are the last cached answer, drawn dimmed. */
export function CrewGroupSection({ group, rows, collapsed, onToggle, hideWhenEmpty, chevron, renderRows, onEnable, spaces }: {
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
}) {
  if (hideWhenEmpty && rows.length === 0) return null
  const regionId = `crew-group-rows-${group.id}`
  return (
    <section data-testid={`crew-group-${group.id}`} data-offline={group.offline ? '' : undefined}>
      <button type="button" aria-expanded={!collapsed} aria-controls={regionId}
        data-testid={`crew-group-toggle-${group.id}`}
        onClick={() => onToggle(group.id)}
        className={`${HEADER_CLS} cursor-pointer hover:text-accent`}>
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
        {!group.offline && <span className="ml-auto shrink-0 font-normal tabular-nums">{rows.length}</span>}
      </button>
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
