import { createSelector } from '@reduxjs/toolkit'
import type { ChatSlot } from '../../types'
import { isChatPageSurface } from '../../utils/channelOrigin'
import type { DashboardState } from '../dashboardSlice'
import { heldOrphanTest } from './unread'

/** Read-side slot helpers: a slot's surface, its relay binding, the unread
 *  count per surface and the slot-list wait. `dashboardSlice` re-exports
 *  them under the names callers import from it. */

/**
 * Resolve a slot's surface key. Backend emits `surface` (mirrors `mode` today
 * but lets the two diverge later); fall back to `mode` for slots delivered
 * before the backend rollout. Empty string is the canonical "main chat" key.
 */
export function slotSurfaceKey(slot: { mode?: string; surface?: string }): string {
  return slot.surface ?? slot.mode ?? ''
}

/**
 * True for a relay archive: a chat an older build ran on a connected crew,
 * read-only now. Two client surfaces must agree with each other and with the
 * server on it: `selectContinuable` (which must not OFFER Continue on one) and
 * ChatPage's regenerate / edit-resend gates (which must not offer those
 * either). Mirrors `relay_archive_refusal` in
 * `src/kiro_crew/dashboard/relay_archive.py`, which REFUSES the same actions
 * with 409 `relay_archive_read_only`.
 *
 * Keyed on `executor` (the binding INTENT), never `instance_id`:
 * the server refuses a half-open binding (marker set, triple incomplete) too, so
 * an unbound `executor` must read as bound here exactly as it does there. An
 * absent slot is not bound — an older gateway ships `executor` on every slot, so
 * only a genuinely missing lookup lands here, and a missing slot has no local
 * action to gate.
 */
export function slotIsRemoteBound(slot: { executor?: string } | null | undefined): boolean {
  return slot?.executor === 'remote'
}

/**
 * Count unread slots whose surface matches `mode`. Slots present in
 * `unreadSlots` but missing from `slots` (e.g. deleted but not yet drained)
 * are treated as the default chat surface (`""`) so they keep contributing
 * to the Chat badge rather than vanishing silently.
 *
 * Note — intentional asymmetry with `filterUnreadKeysBySurface` in
 * `surfaces/registry.ts`: that helper drops orphan keys (the sidebar can't
 * display them regardless), whereas this one keeps them so the badge stays
 * stable across the brief race between `removeSlotOptimistic` and
 * `fetchSlots.fulfilled`.
 *
 * The exception is an orphan no reconcile will drain: any orphan while the
 * gateway labels its lists partial, or one the gateway holds (`slotsHeldKeys`, or a
 * channel key under `slotsHoldChannels`). The premise above does not hold for it. Its unread entry is kept so the dot returns with the tab, but
 * it is left out of the badge, which would otherwise count a session the
 * sidebar cannot show.
 */
function countUnreadByMode(
  slots: ChatSlot[],
  unread: string[],
  mode: string,
  heldKeys: readonly string[] = [],
  holdChannels = false,
  listPartial = false,
): number {
  if (unread.length === 0) return 0
  const surfaceByKey = new Map(slots.map(s => [s.key, slotSurfaceKey(s)]))
  const isHeldOrphan = heldOrphanTest(new Set(surfaceByKey.keys()), heldKeys, holdChannels, listPartial)
  // The chat surface ('') counts every slot the chat page renders, legacy
  // Autopilot slots included (see `isChatPageSurface`).
  const isChatSurface = mode === ''
  let count = 0
  for (const k of unread) {
    if (isHeldOrphan(k)) continue
    const sk = surfaceByKey.get(k) ?? ''
    if (isChatSurface ? isChatPageSurface(sk) : sk === mode) count++
  }
  return count
}

/**
 * Memoized factory for "unread count for slots whose surface === mode".
 * One memo cache per `mode` argument so registry surfaces don't trash each
 * other's memoization. Built-in nav badges should not call this directly —
 * they go through `selectSurfaceBadgeCount(navId)` from `surfaces/registry`,
 * which routes to this factory only when a surface declares `slotMode`.
 */
type UnreadByModeSelector = (state: { dashboard: DashboardState }) => number
const _unreadByModeCache = new Map<string, UnreadByModeSelector>()
export function selectUnreadByMode(mode: string): UnreadByModeSelector {
  let sel = _unreadByModeCache.get(mode)
  if (!sel) {
    sel = createSelector(
      (state: { dashboard: DashboardState }) => state.dashboard.slots,
      (state: { dashboard: DashboardState }) => state.dashboard.unreadSlots,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsHeldKeys,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsHoldChannels,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsListPartial,
      (slots, unread, heldKeys, holdChannels, listPartial) =>
        countUnreadByMode(slots, unread, mode, heldKeys ?? [], holdChannels === true, listPartial === true),
    )
    _unreadByModeCache.set(mode, sel)
  }
  return sel
}

/**
 * True while the page has never seen a slot list and one may still arrive, so a
 * pane with no open session cannot yet tell "no chats" from "chats loading".
 * `slotsGeneration`, not `slotsLoaded`: a socket reconnect resets the latter,
 * and a gateway whose list already landed is not waited on again. A failed
 * fetch with none left in flight ends the wait.
 */
export const selectSlotListPending = (state: { dashboard: DashboardState }): boolean => {
  const d = state.dashboard
  return !d.slotsLoaded && (d.slotsGeneration ?? 0) === 0 && !d.slotsFetchFailed
}
