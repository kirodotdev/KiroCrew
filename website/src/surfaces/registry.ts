/**
 * Surface registry — single source of truth for top-level navigation
 * destinations in the dashboard.
 *
 * The left rail contains a mix of static built-in nav items (Chat, Autopilot,
 * Notifications, Settings, ...) and dynamic app nav items (loaded from
 * installed apps). Both kinds register here through one shape: a `Surface`
 * describes a nav destination plus how its badge count is derived. `App.tsx`
 * iterates the registry exactly once to render nav items and their badges;
 * `ChatPage` resolves the slot filter via `surface.slotMode` instead of a
 * hardcoded comparison; new surfaces (built-in or third-party) register one
 * entry and everything else (route, badge, slot routing) just works.
 *
 * Slot-bearing vs non-slot surfaces:
 * - **Slot-bearing** surfaces (`slotMode` set) — Chat (`''`), Autopilot
 *   (`'orchestrator'`), and any future modes — pull their badge count from
 *   `selectUnreadByMode(slotMode)`. These are also the surfaces ChatPage
 *   filters slots into.
 * - **Non-slot** surfaces (no `slotMode`) — Notifications and third-party
 *   apps — supply their own `unreadSelector` that reads from wherever their
 *   truth lives (notifications slice, app's own state, etc.).
 *
 * Adding a new chat-mode surface (e.g. a future "Code Review" mode):
 *   registerBuiltinSurface({
 *     navId: 'reviews',
 *     route: '/reviews',
 *     label: 'Reviews',
 *     icon: <GitPullRequest size={16} />,
 *     group: 'Apps',
 *     slotMode: 'reviews',
 *     badgeLabel: 'unread reviews',
 *   })
 * — the badge, the route filter, and the unread routing fall out for free.
 */
import type { ReactElement } from 'react'
import { selectUnreadByMode, slotSurfaceKey } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

import { i18nT } from '../i18n/t'
export type SurfaceGroup = 'Main' | 'Apps' | 'Platform' | 'Bottom'

/**
 * Marks a registry literal as a machine value rather than rendered copy.
 *
 * Keep this wrapper narrow: `label` fallbacks in the built-in registry are
 * unreachable when `labelKey` is present (enforced by navLabels.test.tsx), and
 * `group` is a routing bucket. Other surface strings such as `badgeLabel` and
 * `activityLabel` are rendered and must not use this helper.
 */
export function surfaceMachineValue<T extends string>(value: T): T {
  return value
}

/** Anything that can appear as a top-level destination in the left rail. */
export interface Surface {
  /** Stable identifier matching the prior NAV_ITEMS `id` for back-compat. */
  navId: string
  /** Route path (must be unique across surfaces). */
  route: string
  /**
   * Display label used in the nav item — the English fallback.
   *
   * Surfaces are registered at MODULE LOAD, before any language is resolved, so
   * this string cannot be translated in place: whatever language was active at
   * import time would be frozen in forever. Set `labelKey` instead and let the
   * consumer resolve it per render.
   */
  label: string
  /**
   * i18n catalog key for `label`, resolved at RENDER time via `surfaceLabel()`.
   *
   * Optional so a downstream edition (or an app-contributed surface) can keep
   * registering a plain `label` and stay correct — it simply renders untranslated.
   */
  labelKey?: string
  /** Lucide icon (or per-app `<img>`) — built-ins import from lucide-react. */
  icon: ReactElement
  /** Sidebar group bucket. */
  group: SurfaceGroup
  /**
   * Slot-bearing surface: any chat slot whose effective surface key
   * (`slot.surface ?? slot.mode`) equals this string belongs here. Used by
   * `ChatPage` to filter slots and by the badge selector. Omit for non-slot
   * surfaces (Notifications, Settings, third-party apps that own their own
   * counts).
   */
  slotMode?: string
  /**
   * Custom selector for the unread/attention badge count. Used by surfaces
   * whose badge isn't derived from chat slots (notifications, apps).
   * Slot-bearing surfaces should omit this — the count is derived from
   * `slotMode` automatically.
   *
   * MUST be referentially stable and computationally cheap. Both
   * `selectSurfaceBadgeCount(navId)` and `selectAllSurfacesAttention` invoke
   * every registered surface's `unreadSelector` on every Redux dispatch
   * (they aren't wrapped in `createSelector` because the inputs span
   * multiple slices and arbitrary derivations). If a custom selector does
   * non-trivial work (filter/sort/etc.), wrap it in `createSelector` at the
   * source slice (see `selectUnreadByMode` for the memoized pattern). The
   * slot-bearing path is already memoized via `selectUnreadByMode`.
   */
  unreadSelector?: (state: RootState) => number
  /** Accessibility label for the badge ("X unread conversations"). */
  badgeLabel?: string
  /**
   * Optional live-activity count, rendered as a small pulsing dot on the rail
   * item rather than folded into the unread badge. Use for "something is
   * happening on this surface right now" signals whose semantics differ from
   * unread counts — e.g. subagents running in a background chat, which must be
   * visible from any other page but is not an unread conversation.
   *
   * Same performance contract as `unreadSelector`: invoked on every dispatch,
   * so memoize non-trivial derivations at the source slice with
   * `createSelector`.
   */
  activitySelector?: (state: RootState) => number
  /** Accessibility label for the activity dot ("X subagents running"). */
  activityLabel?: string
  /**
   * Mark a surface as registered for badge wiring only — its nav item is
   * rendered elsewhere (typically by `appNavItems` from `api.listApps()`).
   * `getBuiltinSurfaces()` excludes these so they don't show up twice in
   * NAV_ITEMS, but `selectSurfaceBadgeCount`/`selectAllSurfacesAttention`
   * still resolve their counts. Use this for built-in apps that publish a
   * manifest UI page but need a Redux-backed badge.
   */
  appOnly?: boolean
  /**
   * Hide this surface from the left nav rail while keeping its route, badge
   * selector, and attention-count contribution intact. Unlike `appOnly`
   * (which gates the attention sum on `enabledAppIds` membership), a
   * `hiddenFromNav` surface still contributes to `selectAllSurfacesAttention`
   * (browser-tab count) and `selectSurfaceBadgeCount` — only its rail item is
   * suppressed because it's surfaced elsewhere (the topbar Notifications
   * bell). The route stays registered in `App.tsx`'s `<Routes>`.
   */
  hiddenFromNav?: boolean
}

const _builtins: Surface[] = []

/**
 * Register a built-in surface. Call from `surfaces/builtins.tsx` at module
 * load time (side-effect import in `main.tsx`).
 *
 * Replacement semantics: if `s.navId` already exists, the entry is
 * overwritten in place. This makes Vite HMR re-evaluations idempotent (a
 * hot-reloaded `builtins.tsx` doesn't accumulate duplicates).
 *
 * Route conflicts: a *different* navId trying to claim a route already
 * taken by another surface throws synchronously. Two destinations sharing a
 * route is always a programming error (the router would resolve one
 * ambiguously). Same-navId replacement is allowed (HMR), so the check only
 * fires on a true cross-navId collision.
 */
export function registerBuiltinSurface(s: Surface): void {
  const routeOwner = _builtins.find(b => b.route === s.route && b.navId !== s.navId)
  if (routeOwner) {
    throw new Error(
      `Surface route conflict: '${s.route}' is already registered by navId='${routeOwner.navId}', cannot register it for navId='${s.navId}'`,
    )
  }
  const idx = _builtins.findIndex(b => b.navId === s.navId)
  if (idx >= 0) _builtins[idx] = s
  else _builtins.push(s)
}

/**
 * Built-in surfaces that should be rendered as primary nav items in
 * insertion order. `appOnly` surfaces are intentionally excluded — they
 * exist purely to wire a badge selector for an app rendered via
 * `appNavItems` (api.listApps()), and including them here would duplicate
 * the rail entry. Their badge counts are still resolvable via
 * `selectSurfaceBadgeCount(navId)` / `selectAllSurfacesAttention`, both of
 * which iterate `_builtins` directly.
 */
export function getBuiltinSurfaces(): readonly Surface[] {
  return _builtins.filter(s => !s.appOnly && !s.hiddenFromNav)
}

/** Look up a built-in surface by navId. Returns `undefined` for app-only ids. */
export function getBuiltinSurface(navId: string): Surface | undefined {
  return _builtins.find(s => s.navId === navId)
}

/**
 * Find the slot-bearing surface whose `slotMode` matches the given mode.
 * Useful for tools that want to resolve a `mode` string to its surface
 * descriptor (icon, label, route). Returns `undefined` if no surface
 * advertises that slotMode. Iterates the full registry — including
 * `appOnly` surfaces — because slot-bearing app-only surfaces are still
 * legitimate routing targets for slots.
 */
export function findSurfaceBySlotMode(mode: string | undefined): Surface | undefined {
  const m = mode || ''
  return _builtins.find(s => s.slotMode === m)
}

/**
 * Filter a list of slots down to those that belong to a given slot-bearing
 * surface (by slotMode). Falls back to `slot.surface ?? slot.mode ?? ''` so
 * older payloads (no `surface` field) still route correctly.
 */
export function filterSlotsBySurface(slots: readonly ChatSlot[], slotMode: string): ChatSlot[] {
  return slots.filter(s => slotSurfaceKey(s) === slotMode)
}

/**
 * Intersect a list of unread slot keys with the slots that belong to a given
 * surface — returns only the unread keys whose slot is on this surface.
 *
 * Used by `ChatPage` to scope the sidebar's "show only unread" toggle: its
 * tooltip count and auto-drain effect read this list, so without scoping a
 * cross-mode unread (e.g. an autopilot slot becoming unread while you're on
 * /chat) would inflate the toggle's count even though the sidebar's visible
 * session list — built from `filterSlotsBySurface` — wouldn't show it.
 *
 * Orphan unread keys (slot key in `unreadKeys` but not in `slots`, e.g.
 * deleted before reconciliation) are excluded; they're not visible in the
 * sidebar regardless, and `fetchSlots.fulfilled` drains them from
 * `unreadSlots` shortly after.
 *
 * Note — intentional asymmetry with the nav badge: `countUnreadByMode` in
 * `dashboardSlice.ts` treats orphans as the default chat surface (`''`) so
 * the Chat nav badge doesn't transiently drop while reconciliation is in
 * flight. This helper drops them instead because they can't be displayed
 * in the sidebar regardless, and showing a higher count than visible
 * sessions would be a worse UX than hiding the (about-to-be-drained) item.
 */
export function filterUnreadKeysBySurface(
  unreadKeys: readonly string[],
  slots: readonly ChatSlot[],
  slotMode: string,
): string[] {
  if (unreadKeys.length === 0) return []
  const surfaceKeys = new Set(
    slots.filter(s => slotSurfaceKey(s) === slotMode).map(s => s.key),
  )
  return unreadKeys.filter(k => surfaceKeys.has(k))
}

/**
 * Resolve the badge count for a registered surface. Slot-bearing surfaces
 * derive the count from `selectUnreadByMode`; non-slot surfaces use their
 * `unreadSelector`. Returns 0 for unknown navIds.
 *
 * Selectors are cached per `navId` so that consumers (typically inside a
 * `<NavBadge navId={...}>` rendered in a list of nav items) get stable
 * function references, which keeps `useAppSelector`'s referential-equality
 * fast-path effective. The cached function captures the navId by closure
 * and re-resolves the surface from the registry on each call so that HMR
 * replacements (same navId, new entry object) are picked up.
 */
const _badgeCountSelectorCache = new Map<string, (state: RootState) => number>()
const _activityCountSelectorCache = new Map<string, (state: RootState) => number>()
export function selectSurfaceBadgeCount(navId: string): (state: RootState) => number {
  let sel = _badgeCountSelectorCache.get(navId)
  if (!sel) {
    sel = (state: RootState): number => {
      const surface = _builtins.find(s => s.navId === navId)
      if (!surface) return 0
      if (surface.unreadSelector) return surface.unreadSelector(state)
      if (surface.slotMode !== undefined) return selectUnreadByMode(surface.slotMode)(state)
      return 0
    }
    _badgeCountSelectorCache.set(navId, sel)
  }
  return sel
}

/**
 * Live-activity count for a surface's rail item. Deliberately separate from
 * `selectSurfaceBadgeCount`: activity is a transient "in flight now" signal
 * rendered as a dot, and must not inflate the unread badge number (nor the
 * browser-tab attention sum, which `selectAllSurfacesAttention` owns).
 */
export function selectSurfaceActivityCount(navId: string): (state: RootState) => number {
  let sel = _activityCountSelectorCache.get(navId)
  if (!sel) {
    sel = (state: RootState): number => {
      const surface = _builtins.find(s => s.navId === navId)
      return surface?.activitySelector ? surface.activitySelector(state) : 0
    }
    _activityCountSelectorCache.set(navId, sel)
  }
  return sel
}

/**
 * Sum every built-in surface's badge count. Drives the browser tab title
 * attention number alongside dynamic app badges. Composes the per-surface
 * selectors directly (no `.find()` per call) so it's O(surfaces) on each
 * Redux state change rather than O(surfaces²); the per-surface inner
 * selectors are themselves memoized via `selectUnreadByMode`.
 */
export function selectAllSurfacesAttention(state: RootState): number {
  let total = 0
  for (const s of _builtins) {
    if (s.appOnly && !state.dashboard.enabledAppIds.includes(s.navId)) continue
    if (s.unreadSelector) total += s.unreadSelector(state)
    else if (s.slotMode !== undefined) total += selectUnreadByMode(s.slotMode)(state)
  }
  return total
}


/**
 * Test-only: clear the registry. Production code should never call this —
 * builtins are registered once at module load via `surfaces/builtins.tsx`.
 */
export function _resetBuiltinsForTest(): void {
  _builtins.length = 0
  _badgeCountSelectorCache.clear()
}

/**
 * Resolve a surface's display label for the CURRENT language.
 *
 * Surfaces register at module load — before a language is known — so their
 * `label` is a frozen English fallback. Call this at render time instead of
 * reading `.label` directly, or the nav rail stays English while the rest of the
 * dashboard translates (which is exactly the bug this exists to fix).
 *
 * A surface with no `labelKey` (an app-contributed or edition-registered one)
 * falls through to its literal label rather than rendering a raw key.
 */
export function surfaceLabel(s: { label: string; labelKey?: string }): string {
  return s.labelKey ? i18nT(s.labelKey) : s.label
}
