/**
 * InstanceTabBar — a thin, full-width strip at the very top of the dashboard
 * that switches between the local dashboard and connected remote instances.
 *
 * Modeled on the Electron desktop app's native tab bar: it appears ONLY when
 * at least one remote instance is connected, so the common single-instance
 * experience is pixel-identical to before. Everything *below* this bar is the
 * switchable "window" — the Local dashboard, or a remote instance's embedded
 * dashboard (see InstancesViewport). The bar intentionally carries no product
 * brand of its own; each pane shows its own brand, so switching never doubles
 * the icon/title.
 *
 * Tabs: [Local] + one chip per connected instance, horizontally scrollable
 * when they overflow a narrow window. A right-aligned cluster reflects the
 * ACTIVE remote pane's tunnel connection state + token auto-refresh countdown
 * (host SSH expiry lives in the title bar, not duplicated here).
 */
import { useCallback, useMemo, type CSSProperties } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Home, Server, Loader2 } from 'lucide-react'
import { api, ApiError, type InstanceView } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { setWarm, setActiveId, type WarmConn } from '../store/instancesSlice'
import { isEmbeddedPane } from '../lib/embedded'

/**
 * Instances that get a tab: sticky connect intent (`was_connected`, cleared
 * only on explicit disconnect) OR currently connected OR warm. Exported as the
 * single source of truth so App.tsx can decide whether the bar is visible
 * WITHOUT duplicating the rule — the bar's visibility drives the macOS
 * traffic-light clearance (when shown, the bar is the topmost strip the native
 * lights sit over, so the clearance moves off the header onto the bar).
 */
export function visibleInstanceTabs(
  instances: InstanceView[],
  warm: Record<string, WarmConn>,
): InstanceView[] {
  return instances.filter(
    i => i.was_connected || i.status?.state === 'connected' || !!warm[i.id],
  )
}

// Proactive token refresh fires once elapsed reaches this fraction of the TTL
// (must match InstancesViewport.REFRESH_AT_ELAPSED_FRAC). Drives the countdown
// to the next auto-refresh shown in the tunnel-status cluster.
const REFRESH_AT_ELAPSED_FRAC = 0.8

/** Parse a `<int>[hm]` TTL (e.g. "20h", "30m") to seconds; 0 if unparseable. */
function ttlToSeconds(ttl: string): number {
  const m = /^(\d+)([hm])$/.exec(ttl || '')
  if (!m) return 0
  const n = Number(m[1])
  return m[2] === 'h' ? n * 3600 : n * 60
}

/** Compact human duration: "4h 12m", "12m", or "<1m". */
function fmtDuration(secs: number): string {
  if (secs < 60) return '<1m'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`
}

// Shared tab pill styling. Selected state uses a tinted background ONLY — no
// highlighted border (item 3) — with a transparent border kept on every tab so
// the 24px height never shifts when selection changes.
function tabCls(active: boolean): string {
  return (
    'flex items-center gap-1.5 h-6 px-2.5 rounded-md text-[12px] whitespace-nowrap transition-colors border shrink-0 ' +
    (active
      ? 'bg-accent-subtle text-accent border-transparent font-bold'
      : 'bg-transparent text-muted border-transparent font-medium hover:text-text hover:bg-bg-hover')
  )
}

/** Map a live tunnel state to its per-tab status-dot color. */
function stateDotCls(state?: string): string {
  return state === 'connected'
    ? 'bg-[var(--ok)]'
    : state === 'error'
      ? 'bg-[var(--danger)]'
      : state === 'connecting'
        ? 'bg-[var(--warn)]'
        : 'bg-[var(--muted)]'
}

/** The "Local" tab — native dashboard. Shared by the local and embedded bars. */
function LocalTab({ active, onClick }: { active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={tabCls(active)}
      onClick={onClick}
      title="Local dashboard"
    >
      <Home size={13} /> Local
    </button>
  )
}

/** One remote-instance tab. Shared by the local and embedded bars. */
function InstanceTab({
  name,
  title,
  state,
  connecting,
  unread,
  active,
  onClick,
}: {
  name: string
  title: string
  state?: string
  connecting?: boolean
  unread?: number
  active: boolean
  onClick: () => void
}) {
  const badge = unread || 0
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={tabCls(active)}
      onClick={onClick}
      title={title}
    >
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${stateDotCls(state)}`} aria-hidden />
      {connecting ? <Loader2 size={13} className="animate-spin" /> : <Server size={13} />}
      <span className="max-w-[160px] truncate">{name}</span>
      {badge > 0 && (
        <span
          aria-label={`${badge} unread`}
          className="ml-0.5 min-w-[16px] h-4 px-1 rounded-full bg-accent text-accent-fg text-[10px] leading-4 text-center"
        >
          {badge}
        </span>
      )}
    </button>
  )
}

// Outer container classes per variant. Inline is h-full so its 24px pills sit
// vertically centered in the 42px header (item 3).
function barCls(variant: 'strip' | 'inline'): string {
  return variant === 'inline'
    ? 'instance-tab-bar-inline flex items-center h-full gap-1 min-w-0 overflow-x-auto no-scrollbar'
    : 'topbar-glass instance-tab-bar flex items-center gap-2 h-8 px-2 border-b border-border shrink-0 z-[46]'
}

/**
 * Embedded (remote pane) switcher: renders the SAME inline tab bar as the local
 * tab, driven by the model the parent relays into `instances.host` (option B),
 * and posts switch requests back up so the parent flips `activeId`. This is what
 * collapses the remote pane's two stacked bars into one consolidated header.
 */
function EmbeddedInstanceTabBar({ variant }: { variant: 'strip' | 'inline' }) {
  const host = useAppSelector(s => s.instances.host)
  const onLocal = useCallback(() => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage({ type: 'mc-switch-instance', v: 1, id: null }, '*')
  }, [])
  const onSelect = useCallback((id: string) => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage({ type: 'mc-switch-instance', v: 1, id }, '*')
  }, [])
  if (!host || host.tabs.length === 0) return null
  return (
    <div className={barCls(variant)} role="tablist" aria-label="Instances">
      <div className={`flex items-center gap-1 min-w-0 overflow-x-auto no-scrollbar ${variant === 'strip' ? 'flex-1' : ''}`}>
        <LocalTab active={host.activeId === null} onClick={onLocal} />
        {host.tabs.map(t => (
          <InstanceTab
            key={t.id}
            name={t.name}
            title={`${t.name} (${t.sshHost})`}
            state={t.state}
            connecting={t.state === 'connecting'}
            unread={t.unread}
            active={host.activeId === t.id}
            onClick={() => onSelect(t.id)}
          />
        ))}
      </div>
    </div>
  )
}

export default function InstanceTabBar({
  variant = 'strip',
  style,
}: { variant?: 'strip' | 'inline'; style?: CSSProperties } = {}) {
  const dispatch = useAppDispatch()
  const activeId = useAppSelector(s => s.instances.activeId)
  const warm = useAppSelector(s => s.instances.warm)
  const unread = useAppSelector(s => s.instances.unread)

  // Embedded instance panes are single-level: never run the instances poll or
  // render the local switcher. Instead they render the parent-relayed switcher
  // (EmbeddedInstanceTabBar) so the remote pane shows one consolidated header.
  const embedded = isEmbeddedPane()
  // Shared with InstancesViewport / InstancesPanel via the React Query cache.
  const instancesQuery = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances(), enabled: !embedded })
  const disabled = instancesQuery.error instanceof ApiError && instancesQuery.error.status === 403
  // Memoize so the `[] ` fallback doesn't produce a fresh array identity on every
  // render, which would otherwise churn the `onSelectInstance` useCallback deps.
  const instances = useMemo(() => instancesQuery.data?.instances ?? [], [instancesQuery.data?.instances])
  // A tab exists for every instance the user *intends* to be connected — i.e.
  // `was_connected` (sticky intent, cleared only on an explicit disconnect) or
  // one that is currently warm/live. Live `status.state` only drives the
  // per-tab visual state, NOT whether the tab exists, so a tab survives a
  // gateway restart or a failed auto-reconnect (rendered with an error dot)
  // instead of vanishing and forcing the user back to Settings → Instances.
  const tabInstances = visibleInstanceTabs(instances, warm)

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onSuccess: (st, id) => {
      if (st.state === 'connected' && st.local_port && st.token) {
        dispatch(setWarm({ id, conn: { port: st.local_port, token: st.token } }))
      }
      // The tab was already activated on click; on failure the active pane shows
      // the in-pane error/reconnect panel (see InstancesViewport).
    },
  })

  const onSelectInstance = useCallback(
    (id: string) => {
      // Always activate the clicked tab so its pane shows immediately — the warm
      // iframe if connected, otherwise the in-pane connecting/error panel. If it
      // isn't warm yet, kick off a (re)connect: success warms it, failure leaves
      // the error pane up. A failed connect never removes the tab.
      dispatch(setActiveId(id))
      // Reconnect when the tab has no warm iframe yet OR when its live tunnel is
      // no longer connected. The status check matters: a mid-session tunnel drop
      // flips status to error/disconnected but does NOT clear the stale `warm`
      // entry, so gating only on `!warm[id]` would skip the reconnect AND hide
      // the error panel — clicking the (red) tab would do nothing visible.
      const inst = instances.find(i => i.id === id)
      const live = !inst || inst.status?.state === 'connected'
      if (!warm[id] || !live) connectMutation.mutate(id)
    },
    [warm, instances, dispatch, connectMutation],
  )
  const onLocal = useCallback(() => dispatch(setActiveId(null)), [dispatch])

  // Embedded panes render the parent-relayed switcher (option B). Hooks above
  // still run unconditionally (rules-of-hooks); the instances poll is disabled
  // when embedded, so this is cheap.
  if (embedded) return <EmbeddedInstanceTabBar variant={variant} />

  // Single-instance experience is unchanged: no bar until a remote instance is
  // connected or remembered.
  if (disabled || tabInstances.length === 0) return null

  // Right-aligned tunnel-status cluster: the ACTIVE remote pane's connection
  // state + countdown to the next token auto-refresh. On the Local tab there is
  // no active tunnel, so the cluster is hidden.
  const activeInst = activeId ? instances.find(i => i.id === activeId) : null
  let tunnelDotCls = ''
  let tunnelLabel = ''
  let tunnelTitle = ''
  if (activeInst) {
    const st = activeInst.status?.state
    if (st === 'connected') {
      tunnelDotCls = 'bg-[var(--ok)]'
      const rem = activeInst.status?.token_ttl_remaining
      const total = ttlToSeconds(activeInst.ttl)
      if (typeof rem === 'number' && total > 0) {
        const untilRefresh = rem - total * (1 - REFRESH_AT_ELAPSED_FRAC)
        tunnelLabel = untilRefresh > 0 ? `connected · refresh ${fmtDuration(untilRefresh)}` : 'connected · refreshing…'
        tunnelTitle = `Tunnel connected. Token valid ${fmtDuration(rem)}; auto-refresh ${untilRefresh > 0 ? `in ${fmtDuration(untilRefresh)}` : 'imminent'}.`
      } else {
        tunnelLabel = 'connected'
        tunnelTitle = 'Tunnel connected.'
      }
    } else if (st === 'connecting') {
      tunnelDotCls = 'bg-[var(--warn)]'
      tunnelLabel = 'connecting…'
      tunnelTitle = 'Tunnel connecting…'
    } else {
      tunnelDotCls = 'bg-[var(--danger)]'
      tunnelLabel = st === 'error' ? 'tunnel error' : (st || 'disconnected')
      tunnelTitle = activeInst.status?.error || `Tunnel ${st || 'disconnected'}.`
    }
  }

  return (
    <div className={barCls(variant)} style={style} role="tablist" aria-label="Instances">
      <div className={`flex items-center gap-1 min-w-0 overflow-x-auto no-scrollbar ${variant === 'strip' ? 'flex-1' : ''}`}>
        <LocalTab active={activeId === null} onClick={onLocal} />
        {tabInstances.map(inst => {
          const st = inst.status?.state
          const isConnecting =
            (connectMutation.isPending && connectMutation.variables === inst.id) ||
            st === 'connecting'
          return (
            <InstanceTab
              key={inst.id}
              name={inst.name}
              title={`${inst.name} (${inst.ssh_host}) — ${st || 'disconnected'}`}
              state={st}
              connecting={isConnecting}
              unread={unread[inst.id] || 0}
              active={activeId === inst.id}
              onClick={() => onSelectInstance(inst.id)}
            />
          )
        })}
      </div>
      {variant === 'strip' && activeInst && (
        <div className="flex items-center gap-1.5 shrink-0 pl-2 pr-1" title={tunnelTitle}>
          <span className={`w-2 h-2 rounded-full ${tunnelDotCls}`} aria-hidden />
          <span className="text-[11px] text-[var(--muted)] hidden sm:inline">{tunnelLabel}</span>
        </div>
      )}
    </div>
  )
}
