/**
 * Tests for the surface registry (`src/surfaces/registry.ts`).
 *
 * The registry provides nav-item registration and per-id badge counts.
 * These tests pin down:
 * - registration & lookup semantics
 * - badge count derivation for slot-bearing surfaces (read from
 *   `slot.surface ?? slot.mode`) and non-slot surfaces (delegated to a
 *   surface-supplied selector)
 * - cross-surface attribution (orchestrator slots must not leak into the
 *   Chat badge)
 * - the orphan-key fallback to the chat bucket so `totalAttention` doesn't
 *   transiently drop while `fetchSlots` reconciliation is in flight
 */
import { describe, it, expect, beforeEach } from 'vitest'
import type { ReactElement } from 'react'
import {
  registerBuiltinSurface,
  getBuiltinSurfaces,
  getBuiltinSurface,
  findSurfaceBySlotMode,
  filterSlotsBySurface,
  filterUnreadKeysBySurface,
  selectSurfaceBadgeCount,
  selectSurfaceActivityCount,
  selectAllSurfacesAttention,
  _resetBuiltinsForTest,
} from '../surfaces/registry'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { ChatSlot } from '../types'

// Real ReactElement so we don't need `as any` on every Surface fixture.
const TEST_ICON: ReactElement = <span />

// Build a minimal RootState the badge selectors need. Slices we don't touch
// can come from their reducers' default state.
const buildState = (slots: ChatSlot[], unread: string[]) => {
  const initialDashboard = dashboardReducer(undefined, { type: '@@INIT' })
  return {
    dashboard: { ...initialDashboard, slots, unreadSlots: unread },
    notifications: notificationsReducer(undefined, { type: '@@INIT' }),
    // Other slices the registry's built-ins do not touch can be loose-typed.
  } as unknown as Parameters<ReturnType<typeof selectSurfaceBadgeCount>>[0]
}

const slot = (key: string, surface?: string, mode?: string): ChatSlot =>
  ({ key, title: key, messages: 0, running: false, mode, surface } as ChatSlot)

describe('surfaces registry', () => {
  beforeEach(() => {
    _resetBuiltinsForTest()
  })

  describe('registration', () => {
    it('records surfaces in insertion order', () => {
      registerBuiltinSurface({ navId: 'a', route: '/a', label: 'A', icon: TEST_ICON, group: 'Main' })
      registerBuiltinSurface({ navId: 'b', route: '/b', label: 'B', icon: TEST_ICON, group: 'Main' })
      expect(getBuiltinSurfaces().map(s => s.navId)).toEqual(['a', 'b'])
    })

    it('replaces in place when the same navId registers twice', () => {
      // Hot-module-reload safety: re-evaluating builtins.tsx during a Vite
      // refresh must not double-register.
      registerBuiltinSurface({ navId: 'a', route: '/a', label: 'First', icon: TEST_ICON, group: 'Main' })
      registerBuiltinSurface({ navId: 'a', route: '/a', label: 'Second', icon: TEST_ICON, group: 'Main' })
      expect(getBuiltinSurfaces()).toHaveLength(1)
      expect(getBuiltinSurface('a')?.label).toBe('Second')
    })

    it('throws when a different navId tries to claim a taken route', () => {
      // Two destinations sharing a route is always a programming error —
      // catch it at registration time, not at first navigation.
      registerBuiltinSurface({ navId: 'a', route: '/shared', label: 'A', icon: TEST_ICON, group: 'Main' })
      expect(() =>
        registerBuiltinSurface({ navId: 'b', route: '/shared', label: 'B', icon: TEST_ICON, group: 'Main' }),
      ).toThrow(/route conflict/)
    })

    it('allows a same-navId re-registration to keep its existing route', () => {
      // Route conflict check only fires across navIds. Same-navId HMR must
      // still work even though the new entry obviously matches its own route.
      registerBuiltinSurface({ navId: 'a', route: '/a', label: 'V1', icon: TEST_ICON, group: 'Main' })
      expect(() =>
        registerBuiltinSurface({ navId: 'a', route: '/a', label: 'V2', icon: TEST_ICON, group: 'Main' }),
      ).not.toThrow()
    })
  })

  describe('appOnly surfaces', () => {
    // appOnly is the seam used by Secretary (and any future built-in app
    // that publishes a manifest UI page) to wire a Redux-backed badge
    // selector without doubling up in NAV_ITEMS.

    it('excludes appOnly surfaces from getBuiltinSurfaces()', () => {
      registerBuiltinSurface({ navId: 'chat', route: '/chat', label: 'Chat', icon: TEST_ICON, group: 'Main', slotMode: '' })
      registerBuiltinSurface({
        navId: 'secretary',
        route: '/secretary',
        label: 'Secretary',
        icon: TEST_ICON,
        group: 'Apps',
        appOnly: true,
        unreadSelector: () => 4,
      })
      // Visible nav items: chat only. Secretary is rendered by appNavItems
      // elsewhere; including it here would duplicate the rail entry.
      expect(getBuiltinSurfaces().map(s => s.navId)).toEqual(['chat'])
    })

    it('still resolves a badge count for an appOnly surface', () => {
      registerBuiltinSurface({
        navId: 'secretary',
        route: '/secretary',
        label: 'Secretary',
        icon: TEST_ICON,
        group: 'Apps',
        appOnly: true,
        unreadSelector: () => 4,
      })
      const state = buildState([], [])
      // selectSurfaceBadgeCount iterates the full _builtins array so
      // appOnly surfaces still drive their badges via NavBadge.
      expect(selectSurfaceBadgeCount('secretary')(state)).toBe(4)
    })

    it('contributes to selectAllSurfacesAttention even when appOnly', () => {
      registerBuiltinSurface({ navId: 'chat', route: '/chat', label: 'Chat', icon: TEST_ICON, group: 'Main', slotMode: '' })
      registerBuiltinSurface({
        navId: 'secretary',
        route: '/secretary',
        label: 'Secretary',
        icon: TEST_ICON,
        group: 'Apps',
        appOnly: true,
        unreadSelector: () => 5,
      })
      const state = buildState([slot('chat-1', '')], ['chat-1'])
      // 1 chat + 5 secretary = 6 — appOnly surfaces must still be summed
      // into the browser tab attention count.
      expect(selectAllSurfacesAttention({ ...state, dashboard: { ...state.dashboard, enabledAppIds: ['secretary'] } })).toBe(6)
    })

    it('does not contribute to selectAllSurfacesAttention when disabled', () => {
      registerBuiltinSurface({ navId: 'chat', route: '/chat', label: 'Chat', icon: TEST_ICON, group: 'Main', slotMode: '' })
      registerBuiltinSurface({
        navId: 'notifications',
        route: '/notifications',
        label: 'Notifications',
        icon: TEST_ICON,
        group: 'Main',
        unreadSelector: () => 1,
      })
      registerBuiltinSurface({
        navId: 'secretary',
        route: '/secretary',
        label: 'Secretary',
        icon: TEST_ICON,
        group: 'Apps',
        appOnly: true,
        unreadSelector: () => 5,
      })
      const state = buildState([slot('chat-1', '')], ['chat-1'])
      // Secretary disabled (not in enabledAppIds) — only chat + notifications counted
      expect(selectAllSurfacesAttention({ ...state, dashboard: { ...state.dashboard, enabledAppIds: [] } })).toBe(2)
    })

    it('routes slots through an appOnly+slotMode surface', () => {
      // Hypothetical: a future built-in app whose nav item is rendered by
      // appNavItems but which also owns a chat-mode (e.g. an app that
      // launches its own conversations). The registry must still map slots
      // with that surface key into the badge count, otherwise appOnly would
      // silently disable slot routing.
      registerBuiltinSurface({
        navId: 'reviews',
        route: '/reviews',
        label: 'Reviews',
        icon: TEST_ICON,
        group: 'Apps',
        appOnly: true,
        slotMode: 'reviews',
        badgeLabel: 'unread reviews',
      })
      const state = buildState([slot('rev-1', 'reviews')], ['rev-1'])
      expect(selectSurfaceBadgeCount('reviews')(state)).toBe(1)
      // findSurfaceBySlotMode must also see appOnly surfaces — slot-mode
      // resolution is the routing contract, separate from "show in nav".
      expect(findSurfaceBySlotMode('reviews')?.navId).toBe('reviews')
      // But it must NOT show up in NAV_ITEMS.
      expect(getBuiltinSurfaces().map(s => s.navId)).not.toContain('reviews')
    })
  })

  describe('lookup', () => {
    beforeEach(() => {
      registerBuiltinSurface({ navId: 'chat', route: '/chat', label: 'Chat', icon: TEST_ICON, group: 'Main', slotMode: '' })
      registerBuiltinSurface({ navId: 'orchestrated', route: '/orchestrated', label: 'Autopilot', icon: TEST_ICON, group: 'Apps', slotMode: 'orchestrator' })
      registerBuiltinSurface({ navId: 'settings', route: '/settings', label: 'Settings', icon: TEST_ICON, group: 'Bottom' })
    })

    it('findSurfaceBySlotMode resolves "" to the chat surface', () => {
      expect(findSurfaceBySlotMode('')?.navId).toBe('chat')
      expect(findSurfaceBySlotMode(undefined)?.navId).toBe('chat')
    })

    it('findSurfaceBySlotMode resolves orchestrator to the autopilot surface', () => {
      expect(findSurfaceBySlotMode('orchestrator')?.navId).toBe('orchestrated')
    })

    it('findSurfaceBySlotMode returns undefined for an unmapped mode', () => {
      expect(findSurfaceBySlotMode('reviews')).toBeUndefined()
    })

    it('findSurfaceBySlotMode never returns a non-slot surface', () => {
      // Settings has no slotMode — it must not accidentally claim mode === undefined.
      expect(findSurfaceBySlotMode('settings')).toBeUndefined()
    })
  })

  describe('filterSlotsBySurface', () => {
    it('partitions slots by surface key (preferring slot.surface over slot.mode)', () => {
      const slots = [
        slot('chat-1', '', undefined),
        slot('orch-1', 'orchestrator', undefined),
        // Backend-version-skew case: backend sent only `mode` (older payload).
        slot('chat-2', undefined, ''),
        // Future-divergence case: backend explicitly disagrees with `mode`.
        slot('orch-2', 'orchestrator', 'something-else'),
      ]
      expect(filterSlotsBySurface(slots, '').map(s => s.key)).toEqual(['chat-1', 'chat-2'])
      expect(filterSlotsBySurface(slots, 'orchestrator').map(s => s.key)).toEqual(['orch-1', 'orch-2'])
    })
  })

  describe('filterUnreadKeysBySurface', () => {
    // Drives the sidebar's "show only unread" toggle: scope unreads to the
    // surface so the toggle's tooltip count and auto-drain effect agree with
    // the visible session list.
    const slots = [
      slot('chat-1', ''),
      slot('chat-2', ''),
      slot('orch-1', 'orchestrator'),
      slot('orch-2', 'orchestrator'),
    ]

    it('returns only unread keys whose slot is on the requested surface', () => {
      const unread = ['chat-1', 'orch-1', 'chat-2']
      expect(filterUnreadKeysBySurface(unread, slots, '')).toEqual(['chat-1', 'chat-2'])
      expect(filterUnreadKeysBySurface(unread, slots, 'orchestrator')).toEqual(['orch-1'])
    })

    it('regression: cross-mode unreads do NOT leak into the toggle count', () => {
      // An autopilot slot becoming unread while the user is on /chat must not
      // inflate the sidebar's "Show only unread sessions (N)" tooltip or
      // prevent the auto-drain effect from disabling the filter when the
      // same-surface inbox actually drains.
      const unread = ['orch-1']
      expect(filterUnreadKeysBySurface(unread, slots, '')).toEqual([])
      expect(filterUnreadKeysBySurface(unread, slots, 'orchestrator')).toEqual(['orch-1'])
    })

    it('drops orphan unread keys (slot deleted before reconciliation)', () => {
      // fetchSlots.fulfilled reconciles unreadSlots against the live slot
      // list. Until that runs, an unread key may reference a slot that has
      // been removed. The sidebar can't display it regardless, so dropping
      // it here keeps the toggle count consistent with the visible list.
      const unread = ['chat-1', 'orphan-key']
      expect(filterUnreadKeysBySurface(unread, slots, '')).toEqual(['chat-1'])
    })

    it('preserves the order of the input unreadKeys array', () => {
      // Sidebar uses this list directly for the badge ordering; preserving
      // input order means most-recent-unread-first behavior survives the
      // filter (the parent slice sorts unreadSlots by recency). `orch-3`
      // here is a cross-surface key (orchestrator slot) that must be
      // excluded — distinct from the orphan-key case (covered above).
      const unread = ['chat-2', 'chat-1', 'orch-3', 'chat-1']  // chat-1 twice
      const slots3 = [...slots, slot('orch-3', 'orchestrator')]
      expect(filterUnreadKeysBySurface(unread, slots3, '')).toEqual(['chat-2', 'chat-1', 'chat-1'])
    })

    it('returns [] for empty input without scanning slots', () => {
      expect(filterUnreadKeysBySurface([], slots, '')).toEqual([])
    })

    it('honors slot.surface over slot.mode when both are present', () => {
      // Forward-compat with the backend's `surface` field — same rule as
      // filterSlotsBySurface and slotSurfaceKey.
      const skewSlots = [slot('orch-divergent', 'orchestrator', '')]
      const unread = ['orch-divergent']
      expect(filterUnreadKeysBySurface(unread, skewSlots, '')).toEqual([])
      expect(filterUnreadKeysBySurface(unread, skewSlots, 'orchestrator')).toEqual(['orch-divergent'])
    })
  })

  describe('selectSurfaceBadgeCount — slot-bearing surfaces', () => {
    beforeEach(() => {
      registerBuiltinSurface({ navId: 'chat', route: '/chat', label: 'Chat', icon: TEST_ICON, group: 'Main', slotMode: '' })
      registerBuiltinSurface({ navId: 'orchestrated', route: '/orchestrated', label: 'Autopilot', icon: TEST_ICON, group: 'Apps', slotMode: 'orchestrator' })
    })

    it('counts all chat-like slots (including orchestrator) in the chat badge', () => {
      const state = buildState(
        [slot('chat-1', ''), slot('orch-1', 'orchestrator'), slot('chat-2', '')],
        ['chat-1', 'orch-1', 'chat-2'],
      )
      // Unified: orchestrator slots count toward the chat badge
      expect(selectSurfaceBadgeCount('chat')(state)).toBe(3)
    })

    it('orchestrator unread DOES inflate the chat badge (unified view)', () => {
      const state = buildState([slot('chat-1', ''), slot('orch-1', 'orchestrator')], ['orch-1'])
      // Unified: orchestrator unread contributes to chat badge
      expect(selectSurfaceBadgeCount('chat')(state)).toBe(1)
      expect(selectSurfaceBadgeCount('orchestrated')(state)).toBe(1)
    })

    it('orphan unread key (slot deleted before reconciliation) falls back to chat', () => {
      const state = buildState([], ['orphan-key'])
      expect(selectSurfaceBadgeCount('chat')(state)).toBe(1)
      expect(selectSurfaceBadgeCount('orchestrated')(state)).toBe(0)
    })

    it('returns 0 for an unknown navId', () => {
      const state = buildState([slot('chat-1', '')], ['chat-1'])
      expect(selectSurfaceBadgeCount('does-not-exist')(state)).toBe(0)
    })

    it('returns the same selector instance on repeated calls (memoization)', () => {
      // Stable reference matters because the badge is rendered inside
      // .map(NAV_ITEMS) — recreating the selector on every render would
      // defeat useAppSelector's referential-equality fast path.
      const a = selectSurfaceBadgeCount('chat')
      const b = selectSurfaceBadgeCount('chat')
      expect(a).toBe(b)
    })
  })

  describe('selectSurfaceBadgeCount — non-slot surfaces', () => {
    it('delegates to surface.unreadSelector', () => {
      registerBuiltinSurface({
        navId: 'notifications',
        route: '/notifications',
        label: 'Notifications',
        icon: TEST_ICON,
        group: 'Main',
        unreadSelector: () => 7,
      })
      const state = buildState([], [])
      expect(selectSurfaceBadgeCount('notifications')(state)).toBe(7)
    })

    it('returns 0 when neither slotMode nor unreadSelector is set', () => {
      registerBuiltinSurface({ navId: 'settings', route: '/settings', label: 'Settings', icon: TEST_ICON, group: 'Bottom' })
      const state = buildState([], [])
      expect(selectSurfaceBadgeCount('settings')(state)).toBe(0)
    })
  })

  describe('selectAllSurfacesAttention', () => {
    it('sums every registered surface so the tab title stays exact', () => {
      registerBuiltinSurface({ navId: 'chat', route: '/chat', label: 'Chat', icon: TEST_ICON, group: 'Main', slotMode: '' })
      registerBuiltinSurface({ navId: 'orchestrated', route: '/orchestrated', label: 'Autopilot', icon: TEST_ICON, group: 'Apps', slotMode: 'orchestrator' })
      registerBuiltinSurface({
        navId: 'notifications',
        route: '/notifications',
        label: 'Notifications',
        icon: TEST_ICON,
        group: 'Main',
        unreadSelector: () => 3,
      })
      const state = buildState(
        [slot('chat-1', ''), slot('orch-1', 'orchestrator')],
        ['chat-1', 'orch-1'],
      )
      // Unified chat badge counts both chat + orchestrator slots (2), plus
      // the orchestrated surface still counts its own (1), plus notifications (3) = 6.
      // In the real app only the chat surface exists, so no double-counting
      // occurs — this test registers both to verify the sum logic.
      expect(selectAllSurfacesAttention(state)).toBe(6)
    })
  })

  describe('activity counts', () => {
    it('resolves a surface-supplied activitySelector', () => {
      registerBuiltinSurface({
        navId: 'chat', route: '/chat', label: 'Sessions', icon: TEST_ICON, group: 'Main',
        slotMode: '', activitySelector: () => 4, activityLabel: 'subagents in flight',
      })
      expect(selectSurfaceActivityCount('chat')(buildState([], []))).toBe(4)
    })

    it('is zero for a surface with no activitySelector', () => {
      registerBuiltinSurface({ navId: 'a', route: '/a', label: 'A', icon: TEST_ICON, group: 'Main', slotMode: '' })
      expect(selectSurfaceActivityCount('a')(buildState([], []))).toBe(0)
    })

    it('never leaks into the badge count or the tab-title attention sum', () => {
      // Activity is a transient "in flight now" dot, NOT an unread count:
      // folding it into either number would misreport unread conversations.
      registerBuiltinSurface({
        navId: 'chat', route: '/chat', label: 'Sessions', icon: TEST_ICON, group: 'Main',
        slotMode: '', activitySelector: () => 7,
      })
      const state = buildState([slot('chat-1', '')], ['chat-1'])
      expect(selectSurfaceBadgeCount('chat')(state)).toBe(1)
      expect(selectAllSurfacesAttention(state)).toBe(1)
    })
  })
})
