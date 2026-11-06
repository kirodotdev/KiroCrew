/**
 * The left dock's Mates group: every starred mate, in the order the person
 * dragged them into (see `lib/dockMates.ts` for the order and the cap).
 *
 * Rows are the rail's own `NavItem`, so a mate row looks, lights and collapses
 * like every other destination; the face sits in the same 16px icon box the
 * page and app icons use. Dragging reuses the app list's sortable row and
 * sensors, so the two lists reorder with one gesture.
 */
import { useQuery, useQueryClient, useMutation, type QueryClient } from '@tanstack/react-query'
import { DndContext, DragOverlay, closestCenter, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { useCallback, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ComponentProps } from 'react'
import { shallowEqual } from 'react-redux'
import { useLocation } from 'react-router-dom'

import { api, type MemberRosterRow } from '../../api/client'
import { ApiError, apiErrorCode } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY, membersRosterQuery } from '../../api/membersQuery'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { OverlayPortal } from '../../components/overlayRoot'
import { useDndSensors } from '../../hooks/useDndSensors'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { railWidthFor } from '../../hooks/useRailWidth'
import { busySlotKeys, mateNeedsYouByKey } from '../../lib/crewMode'
import { haptic } from '../../lib/haptic'
import { DOCK_MATES_LIMIT, dockMateOrder, starOrderWrites } from '../../lib/dockMates'
import { i18nT } from '../../i18n/t'
import { useAppSelector } from '../../store'
import { reportForError, type ErrorReport } from '../../utils/errorReport'
import { slotRowByKey } from '../../store/slotIndex'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { NAV_ITEMS } from './navItems'
import { surfaceLabel } from '../../surfaces/registry'
import { NavToggle, SortableAppNavRow } from './appRail'
import { NavItem } from './navRow'

/** Whether the dock's Mates group is unfolded past `DOCK_MATES_LIMIT`,
 *  persisted like the Apps list's `mc-apps-expanded`. */
const DOCK_MATES_EXPANDED_KEY = 'mc-dock-mates-expanded'

/** The member the Mates page has open, or null off that page. */
function useOpenMember(): string | null {
  const loc = useLocation()
  if (loc.pathname !== '/members' && !loc.pathname.startsWith('/members/')) return null
  return new URLSearchParams(loc.search).get('member')
}

/**
 * Starred mates in dock order, the folded slice, and the open one.
 *
 * `top` is the first `DOCK_MATES_LIMIT` rows. `pulledIn` is the open mate when
 * it sits past them: like the Apps list's active app, it stays on screen while
 * the group is folded, so a docked mate's chat always has a lit row and the
 * Mates page row never has to stand in for it (`openIsDocked`).
 */
export function useDockMates() {
  const { data, isError, error, errorUpdatedAt } = useQuery(membersRosterQuery)
  // A guest session (Shared Crew, a presigned link) is refused the owner's roster.
  // That is "no mates to dock", not a failure: no rows, no notice. The client does
  // not journal it either (`jMembers`). A session-expiry 403 still shows the notice.
  const notOwner = isNotOwnerRefusal(error)
  const ordered = useMemo(() => (notOwner ? [] : dockMateOrder(data ?? [])), [data, notOwner])
  const open = useOpenMember()
  const top = ordered.slice(0, DOCK_MATES_LIMIT)
  const pulledIn = open && !top.some(m => m.name === open) ? ordered.filter(m => m.name === open) : []
  return { ordered, top, pulledIn, open, openIsDocked: !!open && ordered.some(m => m.name === open), loadFailed: isError && !notOwner, loadError: error, errorUpdatedAt }
}

function isNotOwnerRefusal(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403 && !error.authRequired && apiErrorCode(error) === 'owner_only'
}

type LoadNoticeState = { listeners: Map<symbol, () => void>; dismissedAt: number | null }
const loadNoticeStates = new WeakMap<QueryClient, LoadNoticeState>()

/** The first mounted rail owns the notice until it leaves. Sharing dismissal
 *  per query client prevents the other drawer from repeating the same error. */
function useDockMatesLoadNotice(errorUpdatedAt: number) {
  const client = useQueryClient()
  const state = useMemo(() => {
    const shared: LoadNoticeState = loadNoticeStates.get(client) ?? { listeners: new Map(), dismissedAt: null }
    loadNoticeStates.set(client, shared)
    return shared
  }, [client])
  const [id] = useState(() => Symbol('dock-mates-notice'))
  const notify = useCallback(() => {
    for (const listener of [...state.listeners.values()]) listener()
  }, [state])
  const subscribe = useCallback((listener: () => void) => {
    state.listeners.set(id, listener)
    notify()
    return () => {
      state.listeners.delete(id)
      notify()
    }
  }, [id, notify, state])
  const getSnapshot = useCallback(() =>
    state.listeners.keys().next().value === id && state.dismissedAt !== errorUpdatedAt,
  [errorUpdatedAt, id, state])
  const visible = useSyncExternalStore(subscribe, getSnapshot)
  const dismiss = () => {
    state.dismissedAt = errorUpdatedAt
    notify()
  }
  return { visible, dismiss }
}

/** Why a dock mate row carries its dot, most pressing first. The dot itself is
 *  the same one mark for every reason; the reason only names it. */
type MateDot = 'needs_you' | 'unread' | 'working'
const MATE_DOT_LABEL: Record<MateDot, string> = {
  needs_you: 'pages.membersPage.filter_status_needs_you',
  unread: 'pages.membersPage.unread_message',
  working: 'pages.membersPage.drawer_working',
}

/** A per-mate live reading drives both the face and dot on either rail. */
function MateNavItem({ m, ...props }: { m: MemberRosterRow } & Omit<ComponentProps<typeof NavItem>, 'navId' | 'path' | 'label' | 'icon' | 'badge'>) {
  const { running, dot } = useAppSelector(s => {
    const slot = (m.slot_key ? slotRowByKey(s.dashboard.slots, m.slot_key) : undefined)
      ?? s.dashboard.slots.find(row => row.mode === 'member' && row.agent === m.name)
    // Match the Mates page: live idle overrides the HTTP snapshot, while
    // child work (including a queued child) and a session the mate dispatched
    // still count as working.
    const key = slot?.key || m.slot_key
    const working = key && busySlotKeys(s.dashboard.slots, s.chat.subagentQueued).has(key) ? true : (slot ? false : !!m.running)
    // Needs you and unread read the Mates page row's own sources.
    const needsYou = !!key && !!mateNeedsYouByKey(s.dashboard.slots)[key]
    const unread = !!key && s.dashboard.unreadSlots.includes(key)
    const d: MateDot | null = needsYou ? 'needs_you' : unread ? 'unread' : working ? 'working' : null
    return { running: working, dot: d }
  }, shallowEqual)
  const dotLabel = dot ? i18nT(MATE_DOT_LABEL[dot]) : ''
  return (
    <NavItem
      {...props}
      navId={`mate-${m.slug}`}
      path={`/members?member=${encodeURIComponent(m.name)}`}
      label={crewDisplayName(m)}
      icon={<CrewAvatar seed={m.name} avatar={(m as { avatar?: unknown }).avatar} size={20} working={running ? 'subtle' : undefined} />}
      badge={dot
        ? <span role="status" aria-label={dotLabel} className={props.touch ? 'absolute top-1 right-2 w-2 h-2 bg-accent rounded-full z-10' : props.collapsed ? 'absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10' : 'absolute top-1/2 -translate-y-1/2 right-2 w-2 h-2 bg-accent rounded-full z-10'} />
        : undefined}
    />
  )
}

function DockMatesNotice({ collapsed, isMobile, message, report, testId, onDismiss }: {
  collapsed: boolean; isMobile: boolean; message: string
  /** The failed request behind `message`, which is our own wording and so
   *  cannot be matched back to its journal entry by text. */
  report?: ErrorReport
  testId: string; onDismiss: () => void
}) {
  return (
    // Beside the rail, not inside it: the collapsed rail is one icon wide.
    <OverlayPortal>
      <div
        className="fixed bottom-safe-offset-4 z-[9999] w-[320px] max-w-[calc(100vw-2rem)] rounded-lg shadow-lg"
        // The notice's tint needs a solid fill over the page text.
        style={{ left: isMobile ? 16 : railWidthFor({ isMobile: false, collapsed }) + 8, background: 'var(--bg)' }}
        data-testid={testId}
      >
        <ErrorNotice message={message} report={report} askAgent actionPlacement="below" onDismiss={onDismiss} />
      </div>
    </OverlayPortal>
  )
}

/**
 * The same starred mates as tiles on the phone rail, which the chat and Mates
 * pages show beside their drawer. They follow the app tiles in that rail's
 * Apps scroller, behind a divider: the rail already guards a floor of app
 * tiles (`APPS_FLOOR_PX`), and putting mates first would spend it. The Mates
 * tile in the pinned top already opens the full roster, so there is no
 * "N more" tile here; the open mate's tile joins the three when it sits past
 * them, as on the wide rail. No drag either: the order is the one set on a
 * wider screen, and press-and-hold on a phone tile already means "scroll".
 */
export function DockMatesTiles({ onActivate }: { onActivate: () => void }) {
  const { top, pulledIn, open, loadFailed, loadError, errorUpdatedAt } = useDockMates()
  const shown = [...top, ...pulledIn]
  const loadNotice = useDockMatesLoadNotice(errorUpdatedAt)
  const notice = loadFailed && loadNotice.visible
    ? <DockMatesNotice collapsed isMobile message={i18nT('components.matesDock.load_failed')} report={reportForError(loadError)} testId="dock-mates-load-error" onDismiss={loadNotice.dismiss} />
    : null
  if (shown.length === 0) return notice
  return (
    <>
      <div aria-hidden="true" className="w-8 h-px bg-border my-1 shrink-0" />
      {shown.map(m => (
        <div key={m.name} data-testid={`rail-mate-${m.slug}`} className="[&_.app-icon-nav]:opacity-100">
          <MateNavItem
            m={m}
            active={open === m.name}
            collapsed
            touch
            replace
            onClickOverride={open === m.name ? onActivate : undefined}
          />
        </div>
      ))}
      {notice}
    </>
  )
}

export function DockMatesSection({ collapsed, isMobile, onNavigate }: { collapsed: boolean; isMobile: boolean; onNavigate?: () => void }) {
  const queryClient = useQueryClient()
  const { ordered, top, pulledIn, open, loadFailed, loadError, errorUpdatedAt } = useDockMates()
  // Folded behind the same "N more" / "Show less" toggle as the Apps list.
  // Unfolded, every starred mate is on screen and every row is sortable, so a
  // mate starred late (sorted to the bottom) can be dragged into the top.
  const [expanded, setExpanded] = useState(() => safeGetItem(DOCK_MATES_EXPANDED_KEY) === '1')
  const toggleExpanded = useCallback(() => setExpanded(v => { const next = !v; safeSetItem(DOCK_MATES_EXPANDED_KEY, next ? '1' : '0'); return next }), [])
  const overflowing = !expanded && ordered.length > DOCK_MATES_LIMIT
  // Folded, the pulled-in open mate is a static row, as the Apps list's
  // pulled-in active app is: a drop onto it would resolve to a position past
  // the fold and hide the dragged mate.
  const sortableRows = overflowing ? top : ordered
  const staticRows = overflowing ? pulledIn : []
  const hiddenCount = ordered.length - sortableRows.length - staticRows.length
  // A short window shrinks the group to its header and one row; the rest
  // scroll under a hidden scrollbar, so fade the bottom edge while anything
  // is below the fold. Rows changing inside an already-clipped box resize
  // nothing the observer watches, hence the explicit remeasure.
  const [attachScroller, edges, remeasure] = useScrollEdgesY<HTMLDivElement>()
  useLayoutEffect(() => { remeasure() }, [remeasure, sortableRows.length, staticRows.length, collapsed])
  const sensors = useDndSensors({ distance: 8 })
  const [dragId, setDragId] = useState<string | null>(null)
  const loadNotice = useDockMatesLoadNotice(errorUpdatedAt)
  // Also closes the gap before React publishes the pending mutation state.
  const reorderInFlight = useRef(false)

  const reorder = useMutation({
    // One write per moved row, in order: each is its own crew-record save
    // under the config lock, so running them in parallel would only queue.
    mutationFn: async (writes: { name: string; star_order: number }[]) => {
      for (const w of writes) await api.updateKirocrewAgent(w.name, { star_order: w.star_order })
    },
    onMutate: async (writes) => {
      await queryClient.cancelQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      const byName = new Map(writes.map(w => [w.name, w.star_order]))
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, rows =>
        rows?.map(r => (byName.has(r.name) ? { ...r, star_order: byName.get(r.name) } : r)))
    },
    // Some writes may have landed before a failure. Keep drops blocked until
    // the roster has reconciled with the server on either outcome.
    onSettled: async () => {
      try {
        await queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      } finally {
        reorderInFlight.current = false
      }
    },
  })

  const onDragStart = useCallback((e: DragStartEvent) => {
    if (reorder.isPending || reorderInFlight.current) return
    setDragId(String(e.active.id))
  }, [reorder.isPending])
  const onDragCancel = useCallback(() => setDragId(null), [])
  const onDragEnd = useCallback((e: DragEndEvent) => {
    setDragId(null)
    if (reorder.isPending || reorderInFlight.current) return
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = ordered.findIndex(m => m.name === active.id)
    const to = ordered.findIndex(m => m.name === over.id)
    const writes = starOrderWrites(ordered, from, to)
    if (writes.length === 0) return
    reorderInFlight.current = true
    haptic('light')
    reorder.mutate(writes)
  }, [ordered, reorder])

  const notice = loadFailed && loadNotice.visible
    ? <DockMatesNotice collapsed={collapsed} isMobile={isMobile} message={i18nT('components.matesDock.load_failed')} report={reportForError(loadError)} testId="dock-mates-load-error" onDismiss={loadNotice.dismiss} />
    : reorder.isError
      ? <DockMatesNotice collapsed={collapsed} isMobile={isMobile} message={i18nT('components.matesDock.reorder_failed')} report={reportForError(reorder.error)} testId="dock-mates-reorder-error" onDismiss={() => reorder.reset()} />
      : null

  if (ordered.length === 0) return notice

  const header = NAV_ITEMS.find(n => n.id === 'members')
  const row = (m: MemberRosterRow) => (
    <MateNavItem m={m} active={open === m.name} collapsed={collapsed} onClick={onNavigate} />
  )
  const dragged = dragId ? ordered.find(m => m.name === dragId) : undefined
  // The drag ghost renders inside this box; a mask would fade or clip it.

  return (
    <div ref={attachScroller} data-testid="dock-mates" aria-busy={reorder.isPending} className={`flex flex-col gap-0.5 px-2 shrink ${collapsed ? 'min-h-[56px]' : 'min-h-[76px]'} overflow-y-auto overflow-x-hidden overscroll-y-none scrollbar-none [&_.app-icon-nav]:opacity-100 ${edges.bottom && !dragId ? MORE_BELOW_MASK : ''}`} style={{ scrollbarWidth: 'none' }}>
      {collapsed
        ? <div aria-hidden="true" className="h-px bg-border mx-2 my-2" />
        : (
          <div className="nav-section flex items-center pl-3 pr-1 pt-3 pb-1">
            <span className="text-[13px] font-medium text-muted whitespace-nowrap overflow-hidden">
              {header ? surfaceLabel(header) : null}
            </span>
          </div>
        )}
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={onDragCancel}>
        <SortableContext items={sortableRows.map(m => m.name)} strategy={verticalListSortingStrategy}>
          {sortableRows.map(m => (
            <div key={m.name} role="presentation" data-testid={`dock-mate-${m.slug}`}>
              <SortableAppNavRow id={m.name}>{row(m)}</SortableAppNavRow>
            </div>
          ))}
        </SortableContext>
        {staticRows.map(m => (
          <div key={m.name} role="presentation" data-testid={`dock-mate-${m.slug}`}>{row(m)}</div>
        ))}
        <DragOverlay>{dragged ? row(dragged) : null}</DragOverlay>
      </DndContext>
      {/* Present whenever the group can fold, as on the Apps list, so opening
          the one hidden mate changes the label rather than removing the row. */}
      {ordered.length > DOCK_MATES_LIMIT && (
        <div data-testid="dock-mates-more">
          <NavToggle collapsed={collapsed} expanded={expanded} hiddenCount={hiddenCount} onClick={toggleExpanded} genericTitles />
        </div>
      )}
      {notice}
    </div>
  )
}
