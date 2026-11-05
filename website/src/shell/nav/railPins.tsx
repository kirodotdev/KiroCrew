/**
 * Rail pins for sessions and Mates: `RailGroups` plus the rows the user
 * dragged onto the rail. A pin is drawn only while its session or Mate still
 * exists; one that is gone (a deleted session, a removed Mate, a roster still
 * loading) is skipped but kept in the layout, like an unavailable route.
 */
import { useCallback, useMemo, type ComponentProps, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { MessageSquare } from 'lucide-react'

import CrewAvatar from '../../components/CrewAvatar'
import { membersRosterQuery } from '../../api/membersQuery'
import { useAppDispatch, useAppSelector } from '../../store'
import { switchSlot } from '../../store/chatSlice'
import { MATE_PIN_PREFIX, SESSION_PIN_PREFIX, useRailLayout } from '../../lib/railLayout'
import { i18nT } from '../../i18n/t'
import { NavItem } from './navRow'
import { RailGroups } from './railGroups'
import type { RailItemMeta } from './railCustomize'

type GroupsProps = ComponentProps<typeof RailGroups>

/** The Members page's open-Mate query key (pages/members/membersPageModel's
 *  MEMBER_PARAM). Spelled here because the shell may not import the lazy
 *  page's owner modules. */
const MEMBER_PARAM = 'member'

/** A running session or Mate: the same small accent dot the rail uses for updates. */
function RunningDot({ collapsed }: { collapsed: boolean }) {
  return (
    <span
      role="status"
      aria-label={i18nT('nav.rail_pin_running')}
      className={collapsed ? 'absolute top-1 right-1 w-2 h-2 bg-ok rounded-full z-10' : 'absolute top-1/2 -translate-y-1/2 right-2 w-2 h-2 bg-ok rounded-full z-10'}
    />
  )
}

/** The section row a pin of each kind is a more specific spot inside. */
const PIN_PARENT = { session: 'chat', mate: 'members' } as const

export function PinnedRailGroups({ available, meta, renderItem, collapsed, onNavigate, ...rest }: Omit<GroupsProps, 'renderItem'> & {
  /** Draw a non-pin row. `covered`: a pin inside this row's section is the
   *  selected row, so this one must not paint selected too. */
  renderItem: (id: string, label?: string, covered?: boolean) => ReactNode
  /** Called after a pin opens its target (closes the phone drawer). */
  onNavigate?: () => void
}) {
  const layout = useRailLayout()
  const pinned = useMemo(() => layout.groups.flatMap(g => g.items), [layout.groups])
  const sessionKeys = useMemo(() => pinned.filter(id => id.startsWith(SESSION_PIN_PREFIX)), [pinned])
  const mateIds = useMemo(() => pinned.filter(id => id.startsWith(MATE_PIN_PREFIX)), [pinned])

  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // The roster is only read once a Mate is pinned; cached app-wide, 30s stale.
  const roster = useQuery({ ...membersRosterQuery, enabled: mateIds.length > 0 }).data
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const location = useLocation()

  const slotOf = useCallback((id: string) => slots.find(s => s.key === id.slice(SESSION_PIN_PREFIX.length)), [slots])
  const mateOf = useCallback((id: string) => roster?.find(m => m.slug === id.slice(MATE_PIN_PREFIX.length)), [roster])

  const live = useMemo(
    () => [...sessionKeys.filter(id => slotOf(id)), ...mateIds.filter(id => mateOf(id))],
    [sessionKeys, mateIds, slotOf, mateOf],
  )

  const pinMeta = useCallback((id: string): RailItemMeta | null => {
    const s = slotOf(id)
    if (s) return { label: s.title || s.key, icon: <MessageSquare size={16} /> }
    const m = mateOf(id)
    if (m) return { label: m.name, icon: <CrewAvatar seed={m.name} avatar={m.avatar} size={18} /> }
    return meta(id)
  }, [slotOf, mateOf, meta])

  const onMembers = location.pathname === '/members' || location.pathname.startsWith('/members/')
  const onChat = location.pathname === '/chat' || location.pathname.startsWith('/chat/')
  const openMate = new URLSearchParams(location.search).get(MEMBER_PARAM)
  // One selected row: when a pinned session / Mate is what is open, the
  // Sessions / Mates row yields its highlight to it.
  const covered = useMemo(() => {
    const out = new Set<string>()
    if (onChat && live.some(id => id.startsWith(SESSION_PIN_PREFIX) && slotOf(id)?.key === activeSlot)) out.add(PIN_PARENT.session)
    if (onMembers && live.some(id => id.startsWith(MATE_PIN_PREFIX) && mateOf(id)?.name === openMate)) out.add(PIN_PARENT.mate)
    return out
  }, [onChat, onMembers, live, slotOf, mateOf, activeSlot, openMate])

  const renderPin = useCallback((id: string, label?: string) => {
    const s = slotOf(id)
    if (s) {
      return (
        <NavItem
          path={`/chat?sid=${encodeURIComponent(s.key)}`}
          label={label ?? (s.title || s.key)}
          icon={<MessageSquare size={16} />}
          active={onChat && activeSlot === s.key}
          collapsed={collapsed}
          // `navigate()` alone does not switch sessions while /chat is open.
          onClickOverride={() => { dispatch(switchSlot({ key: s.key, announceOnMissing: true, gesture: true })); navigate('/chat'); onNavigate?.() }}
          badge={s.running ? <RunningDot collapsed={collapsed} /> : undefined}
        />
      )
    }
    const m = mateOf(id)
    if (m) return (
      <NavItem
        path={`/members?${MEMBER_PARAM}=${encodeURIComponent(m.name)}`}
        label={label ?? m.name}
        icon={<CrewAvatar seed={m.name} avatar={m.avatar} size={collapsed ? 20 : 18} state={m.running ? 'working' : 'idle'} />}
        active={onMembers && openMate === m.name}
        collapsed={collapsed}
        onClick={onNavigate}
        badge={m.running ? <RunningDot collapsed={collapsed} /> : undefined}
      />
    )
    return renderItem(id, label, covered.has(id))
  }, [slotOf, mateOf, onChat, activeSlot, collapsed, dispatch, navigate, onNavigate, onMembers, openMate, renderItem, covered])

  const allAvailable = useMemo(() => [...available, ...live], [available, live])
  return (
    <RailGroups
      {...rest}
      collapsed={collapsed}
      available={allAvailable}
      meta={pinMeta}
      renderItem={renderPin}
    />
  )
}
