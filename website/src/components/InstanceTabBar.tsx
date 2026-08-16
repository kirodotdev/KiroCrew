/**
 * InstanceTabBar — a thin, full-width strip at the very top of the dashboard
 * that switches between the local dashboard and connected remote crews.
 *
 * The switcher is a single dropdown by default: the number of crews a user
 * configures is unbounded, and a horizontal strip forced them to shrink the
 * window or scroll sideways to reach the last one, so the collapsed trigger
 * costs constant width no matter how many crews exist. A many-crew user can
 * PIN it open (see the Switcher pin) to trade that constant width for an
 * always-visible chip row, so switching costs no dropdown click.
 *
 * Unread counts survive that collapse in two places, because a count hidden
 * behind a closed menu would be invisible: the trigger carries an AGGREGATE
 * badge for every crew that is not on screen, and each menu row carries its
 * own. The bar appears ONLY when at least one remote crew is connected or
 * remembered, so the common single-crew experience is unchanged. Everything
 * *below* the bar is the switchable "window" — the Local dashboard, or a remote
 * crew's embedded dashboard (see InstancesViewport). The bar intentionally
 * carries no product brand of its own; each pane shows its own brand, so
 * switching never doubles the icon/title.
 *
 * A right-aligned cluster reflects the ACTIVE remote pane's tunnel connection
 * state + token auto-refresh countdown (host SSH expiry lives in the title bar,
 * not duplicated here).
 */
import { useCallback, useMemo, useState, useSyncExternalStore, Fragment, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Home, Server, Loader2, ChevronDown, Pin } from 'lucide-react'
import { api, ApiError, type InstanceView } from '../api/client'
import { useAppSelector } from '../store'
import { type WarmConn } from '../store/instancesSlice'
import { isEmbeddedPane } from '../lib/embedded'
import { useSelectInstance } from '../hooks/useSelectInstance'
import { safeSetItem } from '../utils/safeStorage'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
} from './ui/dropdown-menu'

import { i18nT } from '../i18n/t'
import { fmtDuration as fmtDurationParts, fmtUnit, fmtNumber } from '../i18n/format'
/**
 * Crews that get a switcher entry: sticky connect intent (`was_connected`,
 * cleared only on an explicit disconnect) OR currently connected OR warm.
 * Exported as the single source of truth so App.tsx can decide whether the bar
 * is visible WITHOUT duplicating the rule — the bar's visibility drives the
 * macOS traffic-light clearance (when shown, the bar is the topmost strip the
 * native lights sit over, so the clearance moves off the header onto the bar).
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

// Badges stop counting up at this point and render as "99+", so a runaway
// unread count cannot widen the trigger and push the header around.
const BADGE_MAX = 99

// The radio group needs a non-empty value for the Local destination, whose id is
// null; no crew id can collide with it (ids match ^[a-z0-9][a-z0-9-]{0,62}$).
const LOCAL_VALUE = '__local__'

// Persisted preference: when set, the switcher renders every crew as an
// always-visible chip row instead of collapsing behind the dropdown. Power
// users with many crews opt in so switching costs no extra click and each
// crew's live state is visible at a glance. Off by default — the compact
// dropdown stays the norm.
//
// Backed by a module-level store (not usePersistedBool) because several bars in
// THIS realm can be mounted at once and must flip together the instant the pin
// is toggled: the header's inline bar and the InstancesViewport loading/error
// overlay strips coexist and are hidden (display:none), not unmounted, so a
// per-instance hook would leave a hidden bar on its stale value until a remount.
// This module store cannot cross into a remote pane's embedded bar — that runs
// in a separate cross-origin iframe realm with its own localStorage — so the
// embedded bar does NOT read this store. Instead the parent relays the pin into
// each pane via the `expanded` field of `mc-host-model`, and the embedded pin
// toggle posts `mc-set-expanded` back up; the pin is thus one shared value
// across every pane (local header + all remote panes), not per-pane.
const EXPANDED_PREF_KEY = 'mc-crew-switcher-expanded'

let expandedState: boolean = (() => {
  try {
    return localStorage.getItem(EXPANDED_PREF_KEY) === '1'
  } catch {
    return false
  }
})()

const expandedListeners = new Set<() => void>()

export function setCrewSwitcherExpanded(next: boolean) {
  if (next === expandedState) return
  expandedState = next
  safeSetItem(EXPANDED_PREF_KEY, next ? '1' : '0')
  expandedListeners.forEach(l => l())
}

function subscribeExpanded(cb: () => void) {
  expandedListeners.add(cb)
  return () => {
    expandedListeners.delete(cb)
  }
}

/** Reactive read of the pin preference + a setter that broadcasts to every bar. */
export function useCrewSwitcherExpanded(): [boolean, (v: boolean) => void] {
  const expanded = useSyncExternalStore(subscribeExpanded, () => expandedState, () => expandedState)
  return [expanded, setCrewSwitcherExpanded]
}

/** Parse a `<int>[hm]` TTL (e.g. "20h", "30m") to seconds; 0 if unparseable. */
function ttlToSeconds(ttl: string): number {
  const m = /^(\d+)([hm])$/.exec(ttl || '')
  if (!m) return 0
  const n = Number(m[1])
  return m[2] === 'h' ? n * 3600 : n * 60
}

/** Compact human duration: "4h 12m", "12m", or "<1m". */
function fmtDuration(secs: number): string {
  // `<1m` keeps its literal shape: it is a threshold statement, not a duration,
  // and the sub-minute case has no unit value to format.
  if (secs < 60) return `<${fmtUnit(1, 'minute', { maximumFractionDigits: 0 })}`
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  // `dropZero` drops a leading `0h` and a trailing `0m`.
  return fmtDurationParts([[h, 'hour'], [m, 'minute']], { dropZero: true })
}

/**
 * Localized name for a live tunnel state. The raw values are backend enum
 * words, so they are mapped rather than interpolated: they reach the user in a
 * row's accessible name and its tooltip.
 */
function stateLabel(state?: string): string {
  if (state === 'connected') return i18nT('components.instanceTabBar.connected')
  if (state === 'connecting') return i18nT('components.instanceTabBar.connecting')
  if (state === 'error') return i18nT('components.instanceTabBar.tunnel_error')
  // A stopped tunnel is not the same as one that was never opened: the crew's
  // own machine may be running while the forward is down.
  if (state === 'stopped') return i18nT('components.instanceTabBar.stopped')
  return i18nT('components.instanceTabBar.disconnected')
}

/** Map a live tunnel state to its status-dot color. */
function stateDotCls(state?: string): string {
  return state === 'connected'
    ? 'bg-[var(--ok)]'
    : state === 'error'
      ? 'bg-[var(--danger)]'
      : state === 'connecting'
        ? 'bg-[var(--warn)]'
        : 'bg-[var(--muted)]'
}

/**
 * Text colour for the visible state word, mirroring :func:`stateDotCls`. The word
 * carries the state on its own — the colour is reinforcement, which is the point:
 * remove the colour and the row still says what it is.
 */
function stateTextCls(state?: string): string {
  return state === 'connected'
    ? 'text-[var(--ok)]'
    : state === 'error'
      ? 'text-[var(--danger)]'
      : state === 'connecting'
        ? 'text-[var(--warn)]'
        : 'text-muted'
}

/** Clamped badge text, so an unbounded count cannot stretch the trigger. */
function badgeText(count: number): string {
  return count > BADGE_MAX ? `${fmtNumber(BADGE_MAX)}+` : fmtNumber(count)
}

/** Shared unread pill. `aria-hidden` when an ancestor already names the count. */
function UnreadBadge({
  count,
  label,
  aggregate = false,
}: {
  count: number
  label?: string
  /** The trigger's roll-up of OTHER crews, styled apart from a per-row count so
   *  it does not read as the pane named beside it. */
  aggregate?: boolean
}) {
  return (
    <span
      className={
        'ml-0.5 min-w-[16px] h-4 px-1 rounded-full text-[10px] leading-4 text-center font-bold shrink-0 ' +
        (aggregate
          ? 'border border-accent text-accent bg-transparent'
          : 'bg-accent text-accent-fg')
      }
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {badgeText(count)}
    </span>
  )
}

/**
 * One row of the switcher menu. `entry.id === null` is the Local dashboard,
 * which has no tunnel state and no unread count of its own.
 */
export interface SwitcherEntry {
  id: string | null
  name: string
  /** Secondary line: the SSH host the crew is reached through. */
  detail: string
  /** Hover text naming the crew, its host, and its live tunnel state. */
  title: string
  state?: string
  connecting?: boolean
  unread: number
}

function SwitcherRow({ entry, onSelect }: { entry: SwitcherEntry; onSelect: () => void }) {
  const isLocal = entry.id === null
  return (
    <DropdownMenuRadioItem
      value={entry.id ?? LOCAL_VALUE}
      className="gap-2 text-[13px]"
      onSelect={onSelect}
      title={entry.title}
    >
      {isLocal ? (
        <Home className="lucide-inline shrink-0" />
      ) : entry.connecting ? (
        <Loader2 className="lucide-inline shrink-0 animate-spin" />
      ) : (
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${stateDotCls(entry.state)}`}
          aria-hidden
        />
      )}
      <span className="flex flex-col min-w-0 flex-1">
        <span className="truncate">{entry.name}</span>
        {/* A crew whose ssh alias IS its name would otherwise render the same
            word twice, which reads as a bug rather than as extra detail. */}
        {entry.detail && entry.detail !== entry.name ? (
          <span className="truncate text-[12px] text-muted">{entry.detail}</span>
        ) : null}
      </span>
      {entry.unread > 0 ? (
        <UnreadBadge
          count={entry.unread}
          label={i18nT('components.instanceTabBar.n_unread', { n: entry.unread })}
        />
      ) : null}
      {/* Visible, not sr-only. The dot is the only other carrier of state, and
          colour alone cannot distinguish a connected crew from a failed one for
          a colourblind user — who would otherwise have to hover every row to
          find the one that errored. One label serves both audiences, so the
          word a screen reader announces is the word on screen. */}
      {entry.state ? (
        <span className={`shrink-0 text-[11px] ${stateTextCls(entry.state)}`}>
          {stateLabel(entry.state)}
        </span>
      ) : null}
    </DropdownMenuRadioItem>
  )
}

// Outer container classes per variant. Inline is h-full so its 24px trigger sits
// vertically centered in the 42px header.
function barCls(variant: 'strip' | 'inline'): string {
  return variant === 'inline'
    ? 'instance-tab-bar-inline flex items-center h-full gap-1 min-w-0'
    : 'topbar-glass instance-tab-bar flex items-center gap-2 h-8 px-2 border-b border-border shrink-0 z-[46]'
}

/**
 * The switcher itself: a trigger naming the pane on screen, plus a menu of
 * every destination. Presentational — both the local and the embedded bar feed
 * it the same entry model, so the two paths cannot drift apart.
 */
function SwitcherMenu({
  entries,
  activeId,
  onSelect,
}: {
  entries: SwitcherEntry[]
  activeId: string | null
  onSelect: (id: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const active = entries.find(e => e.id === activeId) ?? entries[0]
  // Unread that the user cannot see right now. The active pane's own count is
  // already zeroed on selection, but excluding it explicitly keeps the badge
  // honest if a background message lands on the pane being viewed.
  const elsewhere = entries.reduce(
    (sum, e) => (e.id === activeId ? sum : sum + e.unread),
    0,
  )
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          // The same sentence the accessible name carries, so a sighted user is
          // not left reading a count of OTHER crews as this pane's own.
          title={
            elsewhere > 0
              ? i18nT('components.instanceTabBar.switch_crew_active_unread', {
                  name: active?.name ?? i18nT('components.instanceTabBar.local'),
                  n: elsewhere,
                })
              : undefined
          }
          aria-label={
            elsewhere > 0
              ? i18nT('components.instanceTabBar.switch_crew_active_unread', {
                  name: active?.name ?? i18nT('components.instanceTabBar.local'),
                  n: elsewhere,
                })
              : i18nT('components.instanceTabBar.switch_crew_active', {
                  name: active?.name ?? i18nT('components.instanceTabBar.local'),
                })
          }
          className="flex items-center gap-1.5 h-6 px-2.5 rounded-md text-[12px] whitespace-nowrap transition-colors border border-transparent shrink-0 max-w-[260px] bg-accent-subtle text-accent font-bold hover:bg-bg-hover focus-ring"
        >
          {active?.id === null ? (
            <Home className="lucide-inline shrink-0" />
          ) : active?.connecting ? (
            <Loader2 className="lucide-inline shrink-0 animate-spin" />
          ) : (
            <Server className="lucide-inline shrink-0" />
          )}
          <span className="truncate">{active?.name ?? i18nT('components.instanceTabBar.local')}</span>
          {elsewhere > 0 ? <UnreadBadge count={elsewhere} aggregate /> : null}
          <ChevronDown className="lucide-inline shrink-0 opacity-70" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        aria-label={i18nT('components.instanceTabBar.instances')}
        className="min-w-[240px] max-w-[340px] max-h-[60vh] overflow-y-auto"
      >
        <DropdownMenuRadioGroup value={activeId ?? LOCAL_VALUE}>
          {entries.map((entry, i) => (
            <Fragment key={entry.id ?? LOCAL_VALUE}>
              {/* Local is the user's own machine, not a crew: a rule separates it
                  from the remote list so the two never read as one flat set. */}
              {i === 1 ? <DropdownMenuSeparator /> : null}
              <SwitcherRow entry={entry} onSelect={() => onSelect(entry.id)} />
            </Fragment>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * One crew as an always-visible chip, used by the expanded switcher. Same
 * icon/state/unread vocabulary as a menu row, but a plain button: the expanded
 * row is not a menu, so it carries no `menuitemradio` semantics — `aria-current`
 * names the pane on screen instead. State reaches non-sighted and colourblind
 * users the same way the menu rows carry it: `aria-label` folds the tunnel
 * state into the accessible name, and a non-ok state shows its word (not colour
 * alone), so the errored crew is findable without hovering every chip.
 */
function SwitcherChip({
  entry,
  active,
  onSelect,
}: {
  entry: SwitcherEntry
  active: boolean
  onSelect: () => void
}) {
  const isLocal = entry.id === null
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
      aria-label={entry.title}
      title={entry.title}
      className={
        'flex items-center gap-1.5 h-6 px-2 rounded-md text-[12px] whitespace-nowrap transition-colors shrink-0 border focus-ring ' +
        (active
          ? 'bg-accent-subtle text-accent font-bold border-transparent'
          : 'border-border text-text hover:bg-bg-hover')
      }
    >
      {isLocal ? (
        <Home className="lucide-inline shrink-0" />
      ) : entry.connecting ? (
        <Loader2 className="lucide-inline shrink-0 animate-spin" />
      ) : (
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${stateDotCls(entry.state)}`}
          aria-hidden
        />
      )}
      <span className="truncate max-w-[140px]">{entry.name}</span>
      {entry.unread > 0 ? (
        <UnreadBadge
          count={entry.unread}
          label={i18nT('components.instanceTabBar.n_unread', { n: entry.unread })}
        />
      ) : null}
      {/* Only non-ok state gets a visible word, so a connected chip stays compact
          while an errored/connecting one is spottable without colour or hover. */}
      {entry.state && entry.state !== 'connected' ? (
        <span className={`shrink-0 text-[11px] ${stateTextCls(entry.state)}`}>
          {stateLabel(entry.state)}
        </span>
      ) : null}
    </button>
  )
}

/**
 * The pin that flips the switcher between the compact dropdown and the
 * always-expanded chip row. Pressed (accent, filled) means pinned open; the
 * choice is persisted so it survives reloads and pane switches.
 */
function ExpandToggle({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  const label = expanded
    ? i18nT('components.instanceTabBar.collapse_crews')
    : i18nT('components.instanceTabBar.show_all_crews')
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={expanded}
      title={label}
      aria-label={label}
      className={
        'flex items-center justify-center h-6 w-6 rounded-md shrink-0 transition-colors border border-transparent focus-ring ' +
        (expanded ? 'bg-accent-subtle text-accent' : 'text-muted hover:bg-bg-hover hover:text-text')
      }
    >
      <Pin className={`lucide-inline ${expanded ? 'fill-current' : ''}`} />
    </button>
  )
}

/**
 * The switcher surface both bars mount: the compact dropdown by default, or —
 * when the user pins it — every crew as an always-visible chip row. The pin
 * lives beside either form so the preference is reachable in both states.
 *
 * The expanded row always stays one line and scrolls horizontally: both the
 * fixed-height `inline` header and the fixed-height `strip` overlay bar would
 * spill a wrapped second row over the panel beneath them, so wrapping is not an
 * option in either. It needs no width cap of its own: in the header the row sits
 * inside the identity group's own grid track, which the centred search can never
 * be pushed out of.
 */
function Switcher({
  entries,
  activeId,
  onSelect,
  expanded: expandedProp,
  onSetExpanded,
}: {
  entries: SwitcherEntry[]
  activeId: string | null
  onSelect: (id: string | null) => void
  /** When provided (embedded pane), the pin state is driven by the parent's
   *  relayed model instead of this realm's localStorage — a remote pane lives
   *  in a separate cross-origin iframe whose store the parent can't reach, so
   *  without this override it would ignore the pin and always show collapsed. */
  expanded?: boolean
  /** Paired override for the toggle: the embedded pane relays the new value up
   *  to the parent (which owns the one shared preference) instead of writing
   *  its own store. Falls back to the module store when absent (local bar). */
  onSetExpanded?: (next: boolean) => void
}) {
  const [storeExpanded, setStoreExpanded] = useCrewSwitcherExpanded()
  const expanded = expandedProp ?? storeExpanded
  const setExpanded = onSetExpanded ?? setStoreExpanded
  if (!expanded) {
    return (
      <div className="flex items-center gap-1 min-w-0">
        <SwitcherMenu entries={entries} activeId={activeId} onSelect={onSelect} />
        <ExpandToggle expanded={false} onToggle={() => setExpanded(true)} />
      </div>
    )
  }
  return (
    <div className="flex items-center gap-1 min-w-0">
      {/* No role/aria here: the parent bar is already a role="group" labelled
          "Remote crews", so a second group with the same name would be
          announced twice around one control set. */}
      <div className="flex items-center gap-1 min-w-0 flex-nowrap overflow-x-auto">
        {entries.map(entry => (
          <SwitcherChip
            key={entry.id ?? LOCAL_VALUE}
            entry={entry}
            active={(entry.id ?? null) === activeId}
            onSelect={() => onSelect(entry.id)}
          />
        ))}
      </div>
      <ExpandToggle expanded onToggle={() => setExpanded(false)} />
    </div>
  )
}

/**
 * Embedded (remote pane) switcher: renders the SAME dropdown as the local tab,
 * driven by the model the parent relays into `instances.host`, and posts switch
 * requests back up so the parent flips `activeId`. This is what collapses the
 * remote pane's two stacked bars into one consolidated header.
 */
function EmbeddedInstanceTabBar({ variant }: { variant: 'strip' | 'inline' }) {
  const host = useAppSelector(s => s.instances.host)
  const onSelect = useCallback((id: string | null) => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage({ type: 'mc-switch-instance', v: 1, id }, '*')
  }, [])
  // The pin lives on the parent (one shared preference across every pane); this
  // pane can't write the parent's store from its own iframe realm, so it relays
  // the new value up and lets the parent re-broadcast the model back down.
  const onSetExpanded = useCallback((next: boolean) => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage({ type: 'mc-set-expanded', v: 1, expanded: next }, '*')
  }, [])
  const entries = useMemo<SwitcherEntry[]>(() => {
    if (!host) return []
    return [
      {
        id: null,
        name: i18nT('components.instanceTabBar.local'),
        detail: i18nT('components.instanceTabBar.local_dashboard'),
        title: i18nT('components.instanceTabBar.local_dashboard'),
        unread: 0,
      },
      ...host.tabs.map(t => ({
        id: t.id,
        name: t.name,
        detail: t.sshHost,
        title: `${t.name} (${t.sshHost}) — ${stateLabel(t.state)}`,
        state: t.state,
        connecting: t.state === 'connecting',
        unread: t.unread,
      })),
    ]
  }, [host])
  if (!host || host.tabs.length === 0) return null
  return (
    <div
      className={barCls(variant)}
      role="group"
      aria-label={i18nT('components.instanceTabBar.instances')}
    >
      <Switcher
        entries={entries}
        activeId={host.activeId}
        onSelect={onSelect}
        expanded={host.expanded}
        onSetExpanded={onSetExpanded}
      />
    </div>
  )
}

export default function InstanceTabBar({
  variant = 'strip',
  style,
}: { variant?: 'strip' | 'inline'; style?: CSSProperties } = {}) {
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
  // An entry exists for every crew the user *intends* to be connected — i.e.
  // `was_connected` (sticky intent, cleared only on an explicit disconnect) or
  // one that is currently warm/live. Live `status.state` only drives the
  // per-entry visual state, NOT whether the entry exists, so a crew survives a
  // gateway restart or a failed auto-reconnect (rendered with an error dot)
  // instead of vanishing and forcing the user back to Settings → Remote Crew.
  const tabInstances = useMemo(() => visibleInstanceTabs(instances, warm), [instances, warm])

  // Select-and-maybe-reconnect semantics live in the shared useSelectInstance
  // hook — the SAME unit the ⌘/Ctrl+digit chord uses (useInstanceShortcuts) —
  // so the click path and the keyboard path cannot drift apart.
  const { selectInstance, connectMutation } = useSelectInstance(instances)

  const onSelect = useCallback((id: string | null) => selectInstance(id), [selectInstance])

  const entries = useMemo<SwitcherEntry[]>(
    () => [
      {
        id: null,
        name: i18nT('components.instanceTabBar.local'),
        detail: i18nT('components.instanceTabBar.local_dashboard'),
        title: i18nT('components.instanceTabBar.local_dashboard'),
        unread: 0,
      },
      ...tabInstances.map(inst => {
        const st = inst.status?.state
        // An SSM crew has no ssh_host: it is reached through its managed-instance
        // target, so that is what names the machine on its row.
        const target = inst.connection_method === 'ssm' ? inst.ssm_target : inst.ssh_host
        return {
          id: inst.id,
          name: inst.name,
          detail: target,
          title: `${inst.name} (${target}) — ${stateLabel(st)}`,
          state: st,
          connecting:
            (connectMutation.isPending && connectMutation.variables === inst.id) ||
            st === 'connecting',
          unread: unread[inst.id] || 0,
        }
      }),
    ],
    // `tabInstances` is derived per render; depending on it directly is what
    // keeps the menu in step with a status poll.
    [tabInstances, unread, connectMutation.isPending, connectMutation.variables],
  )

  // Embedded panes render the parent-relayed switcher. Hooks above
  // still run unconditionally (rules-of-hooks); the instances poll is disabled
  // when embedded, so this is cheap.
  if (embedded) return <EmbeddedInstanceTabBar variant={variant} />

  // Single-crew experience is unchanged: no bar until a remote crew is
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
        tunnelLabel = untilRefresh > 0 ? i18nT('components.instanceTabBar.connected_refresh', { time: fmtDuration(untilRefresh) }) : i18nT('components.instanceTabBar.connected_refreshing')
        tunnelTitle = i18nT('components.instanceTabBar.tunnel_connected_token_valid_auto_refresh', {
          valid: fmtDuration(rem),
          refresh: untilRefresh > 0
            ? `in ${fmtDuration(untilRefresh)}`
            : i18nT('components.instanceTabBar.imminent'),
        })
      } else {
        tunnelLabel = i18nT('components.instanceTabBar.connected')
        tunnelTitle = i18nT('components.instanceTabBar.tunnel_connected')
      }
    } else if (st === 'connecting') {
      tunnelDotCls = 'bg-[var(--warn)]'
      tunnelLabel = i18nT('components.instanceTabBar.connecting')
      tunnelTitle = i18nT('components.instanceTabBar.tunnel_connecting')
    } else {
      tunnelDotCls = 'bg-[var(--danger)]'
      tunnelLabel = st === 'error' ? i18nT('components.instanceTabBar.tunnel_error') : (st || 'disconnected')
      tunnelTitle = activeInst.status?.error || i18nT('components.instanceTabBar.tunnel_state', { state: st || i18nT('components.instanceTabBar.disconnected') })
    }
  }

  return (
    <div
      className={barCls(variant)}
      style={style}
      role="group"
      aria-label={i18nT('components.instanceTabBar.instances')}
    >
      <div className={`flex items-center gap-1 min-w-0 ${variant === 'strip' ? 'flex-1' : ''}`}>
        <Switcher entries={entries} activeId={activeId} onSelect={onSelect} />
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
