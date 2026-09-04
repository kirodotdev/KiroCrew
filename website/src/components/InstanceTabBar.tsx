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
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, Fragment, type CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Home, Loader2, ChevronDown, Pin, Check } from 'lucide-react'
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
  DropdownMenuItem,
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
// each pane via the `pinnedCrews` field of `mc-host-model`, and an embedded
// pin toggle posts `mc-set-crew-pin` back up; the pin set is thus one shared
// value across every pane (local header + all remote panes), not per-pane.
const PINNED_PREF_KEY = 'mc-crew-switcher-pinned'

/** The expand-everything switch this preference replaces. */
const LEGACY_EXPANDED_PREF_KEY = 'mc-crew-switcher-expanded'

/**
 * Resolve the pin set from raw stored values, migrating the legacy
 * expand-everything switch.
 *
 * A user who had pinned the switcher open wanted chips, so migrating them to an
 * EMPTY set would silently collapse the header back to a bare dropdown and read
 * as the feature having been removed. Local is the one destination guaranteed to
 * exist (no crew has to be configured for it), so it is the honest floor: they
 * keep a chip row, and pin the crews they want beside it.
 *
 * Pure, and exported, because the module store below reads storage exactly once
 * at import — a test cannot re-trigger that, so the decision has to be reachable
 * without it.
 */
export function resolvePinnedPref(stored: string | null, legacyExpanded: string | null): string[] {
  if (stored !== null) {
    try {
      const parsed: unknown = JSON.parse(stored)
      if (!Array.isArray(parsed)) return []
      return parsed.filter((id): id is string => typeof id === 'string')
    } catch {
      // A hand-corrupted value reads as "nothing pinned", never a crash.
      return []
    }
  }
  if (legacyExpanded !== null) return legacyExpanded === '1' ? [LOCAL_VALUE] : []
  return []
}

function readPinned(): Set<string> {
  try {
    const stored = localStorage.getItem(PINNED_PREF_KEY)
    const legacy = localStorage.getItem(LEGACY_EXPANDED_PREF_KEY)
    const ids = resolvePinnedPref(stored, legacy)
    // Land the migration so it runs once — but only DROP the legacy key after the
    // replacement is durable. Under a full quota the write fails, and removing
    // first would lose the preference outright with nothing to migrate from on
    // the next load; leaving the legacy key means the migration simply retries.
    if (stored === null && legacy !== null) {
      if (safeSetItem(PINNED_PREF_KEY, JSON.stringify(ids))) {
        localStorage.removeItem(LEGACY_EXPANDED_PREF_KEY)
      }
    }
    return new Set(ids)
  } catch {
    // Private mode or disabled storage: an unpinned switcher is the safe
    // fallback, never a throw during module init.
    return new Set()
  }
}

let pinnedState: Set<string> = readPinned()

const pinnedListeners = new Set<() => void>()

/**
 * Replace the pin set and broadcast to every bar in this realm. A fresh Set
 * identity per write is load-bearing: `useSyncExternalStore` compares snapshots
 * by reference, so mutating in place would not re-render.
 */
export function setCrewPins(ids: Iterable<string>) {
  pinnedState = new Set(ids)
  safeSetItem(PINNED_PREF_KEY, JSON.stringify([...pinnedState]))
  pinnedListeners.forEach(l => l())
}

/** Pin or unpin one crew (`LOCAL_VALUE` for the local dashboard). */
export function toggleCrewPin(id: string) {
  const next = new Set(pinnedState)
  if (!next.delete(id)) next.add(id)
  setCrewPins(next)
}

function subscribePinned(cb: () => void) {
  pinnedListeners.add(cb)
  return () => {
    pinnedListeners.delete(cb)
  }
}

/** Reactive read of the pin set + a toggler that broadcasts to every bar. */
export function useCrewPins(): [Set<string>, (id: string) => void] {
  const pinned = useSyncExternalStore(subscribePinned, () => pinnedState, () => pinnedState)
  return [pinned, toggleCrewPin]
}

// Persisted preference: keep the switcher's chip row in a FIXED order instead of
// pulling the crew on screen to the leading slot. Off by default — the active
// crew leads, which reads well for an occasional switcher but reshuffles the row
// on every switch, which a frequent switcher finds disorienting. When on, pinned
// crews hold their configured order and the active one is only highlighted in
// place, so the row never moves under the user.
//
// A module-level store (not usePersistedBool) for the SAME reason the pin set is
// one: several bars in this realm are mounted at once (the header's inline bar
// and the InstancesViewport loading/error overlay strips, hidden via
// display:none rather than unmounted) and must all flip the instant the
// preference toggles. Like the pin store it cannot cross into a remote pane's
// embedded bar — that runs in a separate cross-origin iframe realm with its own
// localStorage — so the embedded bar reads its own value there; the top-level
// dashboard header is the surface this preference governs.
const STABLE_ORDER_PREF_KEY = 'mc-crew-switcher-stable-order'

function readStableOrder(): boolean {
  try {
    return localStorage.getItem(STABLE_ORDER_PREF_KEY) === '1'
  } catch {
    // Private mode or disabled storage: default (active leads) is the safe
    // fallback, never a throw during module init.
    return false
  }
}

let stableOrderState: boolean = readStableOrder()

const stableOrderListeners = new Set<() => void>()

/** Set the stable-order preference and broadcast to every bar in this realm. */
export function setStableOrder(on: boolean) {
  stableOrderState = on
  safeSetItem(STABLE_ORDER_PREF_KEY, on ? '1' : '0')
  stableOrderListeners.forEach(l => l())
}

function subscribeStableOrder(cb: () => void) {
  stableOrderListeners.add(cb)
  return () => {
    stableOrderListeners.delete(cb)
  }
}

/** Reactive read of the stable-order preference + a toggler that broadcasts. */
export function useCrewSwitcherStableOrder(): [boolean, () => void] {
  const on = useSyncExternalStore(
    subscribeStableOrder,
    () => stableOrderState,
    () => stableOrderState,
  )
  return [on, () => setStableOrder(!stableOrderState)]
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

function SwitcherRow({
  entry,
  onSelect,
  pinned,
  noRoom,
  onTogglePin,
}: {
  entry: SwitcherEntry
  onSelect: () => void
  /** Pinned out of this menu into a header chip. */
  pinned: boolean
  /** Pinned, but the header cut its chip off — see `useClippedChipIds`. */
  noRoom: boolean
  onTogglePin: () => void
}) {
  const isLocal = entry.id === null
  const id = entry.id ?? LOCAL_VALUE
  // `noRoom` has to be sayable: a pinned crew with no visible chip otherwise
  // looks like the pin silently failed, and the glyph does not encode it.
  const pinTitle = pinned
    ? i18nT('components.instanceTabBar.unpin_crew', { name: entry.name })
    : i18nT('components.instanceTabBar.pin_crew', { name: entry.name })
  const pinLabel =
    pinned && noRoom
      ? `${pinTitle} — ${i18nT('components.instanceTabBar.pinned_no_room')}`
      : pinTitle
  return (
    // The destination and its pin are SIBLING menu items sharing one visual row,
    // never a control nested inside the row: a menuitemradio may not contain
    // another interactive element (invalid ARIA, and the menu's arrow-key focus
    // cannot reach it). Two stops per row is the cost, and it keeps each pin
    // beside the crew it pins instead of in a second list of the same crews.
    <div className="flex items-center">
      <DropdownMenuRadioItem
        value={id}
        className="gap-2 text-[13px] flex-1 min-w-0 pr-2"
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
      <DropdownMenuItem
        className="shrink-0 px-1.5 justify-center"
        role="menuitemcheckbox"
        aria-checked={pinned}
        data-testid={`crew-pin-${id}`}
        title={pinLabel}
        aria-label={pinLabel}
        // Toggle from `onSelect`, the one activation handler Radix fires exactly
        // once for BOTH pointer and keyboard (Enter/Space) — the menuitemcheckbox
        // is unreachable by keyboard otherwise. Hanging the toggle on the raw DOM
        // `onClick` also dropped pointer clicks: the whole entries list re-renders
        // on every pin change (the glyph flips), so the item pressed could be
        // replaced between pointerdown and click and never receive the event.
        // preventDefault keeps the menu open so a second crew can be pinned
        // without reopening it, and stops the click from switching crews.
        onSelect={(e: Event) => {
          e.preventDefault()
          onTogglePin()
        }}
      >
        {/* Two states, and FILL is the whole distinction: filled accent = pinned,
            unfilled muted outline = not pinned. The resting glyph carries no
            opacity, because it is the only affordance the feature has and
            `--muted` composited below full strength drops under the 3:1 contrast
            floor a UI control has to clear. A pinned crew whose header chip got
            clipped stays filled for the same reason inverted: fading accent to
            mark it would read as the unpinned outline and invite an accidental
            unpin, and `--accent` differs per theme so no single opacity is
            measurably safe. That state is said in the item's accessible name
            instead. */}
        <Pin
          className={`lucide-inline shrink-0 ${pinned ? 'text-accent fill-current' : 'text-muted'}`}
          aria-hidden
        />
      </DropdownMenuItem>
    </div>
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
  pinned,
  onTogglePin,
  clippedPinned,
  stableOrder,
  onToggleStableOrder,
  showStableOrderToggle,
}: {
  entries: SwitcherEntry[]
  activeId: string | null
  onSelect: (id: string | null) => void
  pinned: Set<string>
  onTogglePin: (id: string) => void
  clippedPinned: Set<string>
  stableOrder: boolean
  onToggleStableOrder: () => void
  showStableOrderToggle: boolean
}) {
  const [open, setOpen] = useState(false)
  // Unread the user cannot see: everything that is neither the active pane nor a
  // chip currently on screen. A pinned crew whose chip got cut off counts, since
  // its badge went with it.
  const elsewhere = entries.reduce((sum, e) => {
    const id = e.id ?? LOCAL_VALUE
    const onScreen = (e.id ?? null) === activeId || (pinned.has(id) && !clippedPinned.has(id))
    return onScreen ? sum : sum + e.unread
  }, 0)
  const label =
    elsewhere > 0
      ? i18nT('components.instanceTabBar.switch_crew_unread', { n: elsewhere })
      : i18nT('components.instanceTabBar.switch_crew')
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          title={label}
          aria-label={label}
          className="relative flex items-center justify-center h-6 w-6 shrink-0 rounded-md border border-transparent text-muted transition-colors hover:bg-bg-hover hover:text-text focus-ring"
        >
          <ChevronDown className="lucide-inline shrink-0" />
          {elsewhere > 0 ? (
            // Absolutely positioned so appearing cannot change the trigger's
            // width: the chip row is sized from the space this button leaves, so a
            // badge taking layout space would feed its own measurement.
            <span
              aria-hidden
              className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-[3px] rounded-full bg-accent text-accent-fg text-[10px] font-semibold leading-[14px] text-center pointer-events-none"
            >
              {badgeText(elsewhere)}
            </span>
          ) : null}
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
              <SwitcherRow
                entry={entry}
                onSelect={() => onSelect(entry.id)}
                pinned={pinned.has(entry.id ?? LOCAL_VALUE)}
                noRoom={clippedPinned.has(entry.id ?? LOCAL_VALUE)}
                onTogglePin={() => onTogglePin(entry.id ?? LOCAL_VALUE)}
              />
            </Fragment>
          ))}
        </DropdownMenuRadioGroup>
        {/* A row-order preference, not a destination: it sits below the crew list
            behind a separator so it never reads as one more crew to switch to.
            `onSelect`'s preventDefault keeps the menu open — the user sees the
            checkmark flip and can keep adjusting pins in the same session, the
            same discipline the per-crew pin toggle uses. In an embedded pane the
            toggle relays up to the parent (mc-set-stable-order), so it is shown
            there too. */}
        {showStableOrderToggle ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              role="menuitemcheckbox"
              aria-checked={stableOrder}
              data-testid="crew-stable-order-toggle"
              className="gap-2 text-[13px]"
              title={i18nT('components.instanceTabBar.keep_tab_order_fixed')}
              aria-label={i18nT('components.instanceTabBar.keep_tab_order_fixed')}
              onSelect={(e: Event) => {
                e.preventDefault()
                onToggleStableOrder()
              }}
            >
              <Check
                className={`lucide-inline shrink-0 ${stableOrder ? 'text-accent' : 'opacity-0'}`}
                aria-hidden
              />
              <span className="flex-1 min-w-0">
                {i18nT('components.instanceTabBar.keep_tab_order_fixed')}
              </span>
            </DropdownMenuItem>
          </>
        ) : null}
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
  className = '',
  shrinkable = false,
}: {
  entry: SwitcherEntry
  active: boolean
  onSelect: () => void
  /** Extra classes for the caller's own layout hooks (see `tb-crew-active-chip`). */
  className?: string
  /**
   * Let the chip give up NAME width when its row is short of room, instead of
   * holding its full width and letting the row cut whichever chip lands on the
   * boundary. Only the pinned row passes this: the crew ON SCREEN keeps its full
   * name, because it is the one label that says where you are.
   *
   * The shortage lands on the name because that is the only part of a chip with
   * slack — the state dot, the unread badge and the padding are all `shrink-0`,
   * and the name carries `truncate`, so it ellipsises rather than wrapping.
   *
   * The floor is on the CHIP, not on the name, and that placement is forced: a
   * flex item's `min-width:auto` resolves to its content-based minimum, which for
   * nowrap text is the FULL name — so leaving it `auto` means the chip never
   * shrinks, while `min-w-0` lets the row squeeze it past its own content and the
   * name paints outside its border. An explicit floor is the only third option.
   *
   * The floor's non-name half is read off this chip's own classes: `px-2` (16) +
   * `border` (2) + the `w-1.5` dot (6) + one `gap-1.5` (6) = 30px, plus, when an
   * unread badge is present, a second gap (6) + `ml-0.5` (2) + the badge's
   * `min-w-[16px]` (16) = 54px. Keep them in sync with the classes below;
   * `capture-crew-chip-shrink.mjs` asserts the declared floor against the measured
   * one, so a drift cannot pass silently. A chip carrying a state WORD is never
   * shrinkable (see `CrewChipRow`), because that word's width is not fixed and
   * would break the arithmetic.
   */
  shrinkable?: boolean
}) {
  const isLocal = entry.id === null
  const hasBadge = entry.unread > 0
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
      aria-label={entry.title}
      title={entry.title}
      className={
        className +
        ' ' +
        'flex items-center gap-1.5 h-6 px-2 rounded-md text-[12px] whitespace-nowrap transition-colors border focus-ring ' +
        // Both spellings are literal so Tailwind's content scan sees them.
        (shrinkable
          ? (hasBadge ? 'min-w-[calc(5ch+54px)] ' : 'min-w-[calc(5ch+30px)] ')
          : 'shrink-0 ') +
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
      {/* `tb-drop-crew-name` is the topbar identity group's collapse hook: inside
          `.tb-left` a container rung hides the name so the chip goes icon-only
          rather than pushing the trailing dropdown out of the clip box on a phone.
          The name stays in `aria-label`/`title`, so the chip keeps its accessible
          name either way.

          On a shrinkable chip the row squeezes the CHIP down to its declared floor
          and this span absorbs the difference: `truncate`'s `overflow:hidden` gives
          a flex item an automatic minimum size of zero, so the name is the part
          that gives way, ellipsised rather than clipped. The 5ch floor lives on the
          chip, not here, so there is one source of truth for it. */}
      <span className="tb-drop-crew-name truncate max-w-[140px]">{entry.name}</span>
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

/** Set equality by membership, so a re-measure that changed nothing is a no-op. */
function sameIds(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const id of a) if (!b.has(id)) return false
  return true
}

/**
 * Which chips the row cut off, given each chip's offset and the row's visible
 * width.
 *
 * Pure so it can be tested: jsdom performs no layout, so every offset there is 0
 * and a rendered-component test could never distinguish a fitted row from a
 * clipped one.
 *
 * A chip counts as cut off as soon as its trailing edge passes the visible width,
 * so the partially-visible one at the boundary is included — it is exactly the
 * chip a user cannot read, and the one the dropdown therefore has to account for.
 * The 1px tolerance absorbs sub-pixel layout.
 */
export function clippedChipIds(
  chips: readonly { id: string; left: number; width: number }[],
  visibleWidth: number,
): Set<string> {
  const clipped = new Set<string>()
  for (const chip of chips) {
    if (chip.left + chip.width > visibleWidth + 1) clipped.add(chip.id)
  }
  return clipped
}

/**
 * Which pinned chips the row had to cut off, by id.
 *
 * Measuring is what makes that state SAYABLE in the dropdown — without it, a
 * pinned crew with no visible chip reads as a pin that silently failed.
 *
 * `offsetLeft` is sound here only because the row carries `position: relative`,
 * which makes it the chips' offsetParent and puts both in the same coordinate
 * space as its `clientWidth`. Without that, offsetLeft is measured from some
 * arbitrary positioned ancestor and the comparison silently counts VISIBLE chips
 * as clipped.
 *
 * Reporting this upward cannot feed back into the layout, which is why it is
 * safe: the result is consumed only by the dropdown's rows, which are portalled
 * and contribute nothing to the header's width.
 */
function useClippedChipIds(
  rowRef: React.RefObject<HTMLDivElement | null>,
  ids: readonly string[],
): Set<string> {
  const [clipped, setClipped] = useState<Set<string>>(() => new Set())
  // Identity-stable dep: a fresh array on every render would re-arm the observer.
  const idsKey = ids.join('\u0000')
  useEffect(() => {
    const el = rowRef.current
    if (!el) return
    const idList = idsKey === '' ? [] : idsKey.split('\u0000')
    const measure = () => {
      const kids = Array.from(el.children) as HTMLElement[]
      const next = clippedChipIds(
        kids.flatMap((kid, i) =>
          idList[i] === undefined
            ? []
            : [{ id: idList[i], left: kid.offsetLeft, width: kid.offsetWidth }],
        ),
        el.clientWidth,
      )
      setClipped(prev => (sameIds(prev, next) ? prev : next))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [rowRef, idsKey])
  return clipped
}

/**
 * The pinned crews, as always-visible chips between the active crew and the
 * dropdown.
 *
 * One nowrap line that ADAPTS TO ITS OWN TRACK rather than spending the shortage
 * on one chip. The chips are `shrinkable`, so a row short of room takes the
 * shortage out of every chip's name (each carries `truncate`) instead of holding
 * them at full width and letting whichever chip lands on the boundary be sliced
 * through its unread badge. Nothing outside this group moves: the topbar's grid
 * hands the identity group a width derived from the WINDOW (both side tracks are
 * `minmax(0,1fr)` and the group's `container-type:inline-size` keeps its content
 * out of that calculation), so widening the group would have to be paid for by
 * the centred search — which stays put.
 *
 * Wrapping into a hidden second row is the other way to keep chips whole, and it
 * is rejected: the row would hold its full ALLOCATED width with the wrapped
 * chips' space empty, pushing the trailing dropdown away from the last visible
 * chip by a gap that changes with the viewport.
 *
 * A chip's floor is its icon-only form — the state dot, unread badge and padding
 * are all `shrink-0` — so a cut is still reachable once even those do not fit.
 * That cut is marked on the EDGE, never over the chips: an alpha fade across the
 * last pixels reads as a rendering fault rather than as an affordance, because
 * the unread badge is a chip's trailing element and is therefore the first thing
 * any cut reaches — a fade there dissolves the one glyph the chip exists to show.
 * `data-cut` drives a 1px rule at the boundary (index.css), and the count itself
 * survives twice over: the cut crew's unread is already rolled into the dropdown
 * trigger's aggregate badge, and its dropdown pin announces the cut in its own
 * accessible name (`pinned_no_room`).
 *
 * The row needs no width cap of its own: it sits in the topbar's left grid track
 * (`minmax(0,1fr)`) inside `.tb-left`, which carries `min-width:0` and
 * `overflow:hidden`, so the track already prevents it from reaching the centered
 * search column.
 */
function CrewChipRow({
  chips,
  activeId,
  onSelect,
  onClippedChange,
}: {
  chips: SwitcherEntry[]
  activeId: string | null
  onSelect: (id: string | null) => void
  onClippedChange: (clipped: Set<string>) => void
}) {
  const rowRef = useRef<HTMLDivElement>(null)
  const ids = useMemo(() => chips.map(c => c.id ?? LOCAL_VALUE), [chips])
  const clipped = useClippedChipIds(rowRef, ids)
  useEffect(() => {
    onClippedChange(clipped)
  }, [clipped, onClippedChange])
  return (
    <div
      ref={rowRef}
      data-testid="crew-chip-row"
      // Reflects the measurement, so the boundary rule paints only when a chip is
      // really cut. Safe to feed back: the rule is an absolutely-positioned
      // pseudo-element, so it takes no layout and cannot change what got clipped.
      data-cut={clipped.size > 0 ? 'true' : 'false'}
      // `relative` is load-bearing, not cosmetic: it makes this element the chips'
      // offsetParent so useClippedChipIds can compare their offsetLeft against
      // this row's own clientWidth.
      className="crew-chip-row relative flex flex-nowrap items-center gap-1 min-w-0 overflow-hidden"
    >
      {chips.map(entry => (
        <SwitcherChip
          key={entry.id ?? LOCAL_VALUE}
          entry={entry}
          active={(entry.id ?? null) === activeId}
          onSelect={() => onSelect(entry.id)}
          // A chip showing a state WORD ("error", "connecting") is not shrinkable:
          // that word is text of unknown width, so the chip's declared floor —
          // which is arithmetic over fixed parts — would not hold for it, and it is
          // the chip a user most needs to read whole anyway.
          shrinkable={!entry.state || entry.state === 'connected'}
        />
      ))}
    </div>
  )
}

/**
 * The switcher surface both bars mount: the crew on screen, then a chip for each
 * crew the user pinned, then the dropdown holding everything else.
 *
 * The dropdown TRAILS the chips so it stays adjacent to the last one and reads as
 * "and the rest" — see `CrewChipRow` for why that placement forces a clipped row
 * rather than a wrapped one.
 */
function Switcher({
  entries,
  activeId,
  onSelect,
  pinned: pinnedProp,
  onTogglePin: onTogglePinProp,
  stableOrder: stableOrderProp,
  onToggleStableOrder: onToggleStableOrderProp,
  embedded = false,
}: {
  entries: SwitcherEntry[]
  activeId: string | null
  onSelect: (id: string | null) => void
  /** When provided (embedded pane), the pin set is driven by the parent's relayed
   *  model instead of this realm's localStorage — a remote pane lives in a
   *  separate cross-origin iframe whose store the parent cannot reach, so without
   *  this override it would ignore the pins entirely. */
  pinned?: Set<string>
  /** Paired override: the embedded pane relays a toggle up to the parent (which
   *  owns the one shared preference) instead of writing its own store. */
  onTogglePin?: (id: string) => void
  /** Override for the stable-order preference. Defaults to this realm's own
   *  localStorage-backed store; callers that own the preference elsewhere pass
   *  it explicitly. An embedded pane passes the value the host relayed, or
   *  `null` when the host sent no opinion at all (an older parent predating the
   *  relay, which has no `mc-set-stable-order` handler). `null` orders by the
   *  pre-relay default and suppresses the toggle -- see `showStableOrderToggle`
   *  below -- rather than exposing a control that could never take effect. */
  stableOrder?: boolean | null
  /** Paired toggler for `stableOrder`. */
  onToggleStableOrder?: () => void
  /** True inside a remote pane's embedded switcher. The stable-order preference
   *  is parent-owned and relayed through `mc-host-model` (`stableOrder`), and an
   *  embedded toggle posts `mc-set-stable-order` back up, so the pane applies and
   *  offers the same preference as the local bar. The flag only feeds the older-
   *  host safety net in the resolution above; it no longer hides the toggle. */
  embedded?: boolean
}) {
  const [storePinned, storeTogglePin] = useCrewPins()
  const pinned = pinnedProp ?? storePinned
  const togglePin = onTogglePinProp ?? storeTogglePin
  const [storeStableOrder, storeToggleStableOrder] = useCrewSwitcherStableOrder()
  // The stable-order preference is parent-owned. An embedded pane receives it as
  // a prop relayed through `mc-host-model` (and toggles it back up via
  // `mc-set-stable-order`), so it no longer reads its own cross-origin store; a
  // top-level bar falls back to this realm's localStorage-backed store.
  //
  // `null` from an embedded pane means the host predates the relay, so it has no
  // handler for the toggle's message. Offering the control there would let the
  // user click a checkbox that can never change state, so the pane both orders
  // by the pre-relay default and hides the toggle in that one case.
  const relayUnsupported = embedded && (stableOrderProp ?? null) === null
  const stableOrder = (stableOrderProp ?? (embedded ? false : storeStableOrder)) === true
  const toggleStableOrder = onToggleStableOrderProp ?? storeToggleStableOrder
  const [clippedPinned, setClippedPinned] = useState<Set<string>>(() => new Set())
  const active = entries.find(e => (e.id ?? null) === activeId) ?? entries[0]
  // Two orderings for the always-visible chips:
  //  • Default: the crew on screen LEADS the row and is never also a pinned chip
  //    — two copies of one name would spend the track's width saying the same
  //    thing twice. Reads well for an occasional switcher, but reshuffles the row
  //    on every switch.
  //  • Stable order (opt-in): pinned crews hold their configured order and the
  //    active one is only highlighted in place, so a frequent switcher's row
  //    never moves under them. The active crew is still pulled out to lead when
  //    it is NOT itself a pinned chip, so it stays reachable without opening the
  //    dropdown — for a user who pins every crew that branch never fires and the
  //    row is fully fixed.
  const chips = useMemo(
    () =>
      stableOrder
        ? entries.filter(e => pinned.has(e.id ?? LOCAL_VALUE))
        : entries.filter(e => pinned.has(e.id ?? LOCAL_VALUE) && (e.id ?? null) !== activeId),
    [entries, pinned, activeId, stableOrder],
  )
  const activeIsChip = chips.some(e => (e.id ?? null) === activeId)
  const showLeadingActive = !stableOrder || !activeIsChip
  return (
    <div className="flex items-center gap-1 min-w-0">
      {showLeadingActive && active ? (
        <SwitcherChip
          entry={active}
          active
          onSelect={() => onSelect(active.id)}
          className="tb-crew-active-chip"
        />
      ) : null}
      {chips.length > 0 ? (
        <CrewChipRow
          chips={chips}
          activeId={activeId}
          onSelect={onSelect}
          onClippedChange={setClippedPinned}
        />
      ) : null}
      <SwitcherMenu
        entries={entries}
        activeId={activeId}
        onSelect={onSelect}
        pinned={pinned}
        onTogglePin={togglePin}
        clippedPinned={clippedPinned}
        stableOrder={stableOrder}
        onToggleStableOrder={toggleStableOrder}
        showStableOrderToggle={!relayUnsupported}
      />
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
  // The pins live on the parent (one shared set across every pane); this pane
  // cannot write the parent's store from its own iframe realm, so it relays the
  // toggle up and lets the parent re-broadcast the model back down.
  const onTogglePin = useCallback((id: string) => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage({ type: 'mc-set-crew-pin', v: 1, id }, '*')
  }, [])
  // The stable-order preference also lives on the parent (one shared value across
  // every pane). This pane cannot write the parent's store from its own iframe
  // realm, so it relays the flipped value up and lets the parent re-broadcast the
  // model back down. `null` = this host predates the relay, which the Switcher
  // reads as "order by the default and do not offer the toggle at all".
  const hostStableOrder = host?.stableOrder ?? null
  const onToggleStableOrder = useCallback(() => {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage({ type: 'mc-set-stable-order', v: 1, on: !hostStableOrder }, '*')
  }, [hostStableOrder])
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
  // The relayed model carries a plain array (postMessage cannot carry a Set).
  const pinnedFromHost = useMemo(() => new Set(host?.pinnedCrews ?? []), [host?.pinnedCrews])
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
        pinned={pinnedFromHost}
        onTogglePin={onTogglePin}
        stableOrder={hostStableOrder}
        onToggleStableOrder={onToggleStableOrder}
        embedded
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
  // instead of vanishing and forcing the user back to Settings → Remote Instances.
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
