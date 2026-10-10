/** Per-machine groups in the Sessions list: a `Local` header over the caller's
 *  own lane, then one collapsible group per crew (`crewGroupsFor`). Rendered only
 *  while at least one crew group exists, so a single-machine sidebar draws none
 *  of this. Collapsing a group hides its rows and never changes the selection. */
import { Server } from 'lucide-react'
import { createContext, useCallback, useState, type ReactNode } from 'react'
import { FolderBody } from '../../components/FolderBody'
import type { CrewBadge, CrewGroup } from '../../hooks/useInstanceSessions'
import { i18nT } from '../../i18n/t'
import { safeSetItem } from '../../utils/safeStorage'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { CREW_COLLAPSED_LS_KEY, readCollapsedCrews } from './persistence'
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
 *  choice. `expand` opens one group and is a no-op when it is already open. */
export function useCollapsedCrews(): [ReadonlySet<string>, (id: string) => void, (id: string) => void] {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(readCollapsedCrews)
  const toggle = useCallback((id: string) => setCollapsed(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    safeSetItem(CREW_COLLAPSED_LS_KEY, JSON.stringify([...next]))
    return next
  }), [])
  const expand = useCallback((id: string) => setCollapsed(prev => {
    if (!prev.has(id)) return prev
    const next = new Set(prev)
    next.delete(id)
    safeSetItem(CREW_COLLAPSED_LS_KEY, JSON.stringify([...next]))
    return next
  }), [])
  return [collapsed, toggle, expand]
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
export function CrewGroupSection({ group, rows, collapsed, onToggle, hideWhenEmpty, chevron, renderRows, onEnable }: {
  group: CrewGroup
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
                  {renderRows(rows, `crew:${group.id}`)}
                </CrewOfflineContext.Provider>
              </div>
            )}
        </FolderBody>
      </div>
    </section>
  )
}
