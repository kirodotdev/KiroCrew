/**
 * Pure helper for the ChatSidebar unread-filter auto-drain effect.
 *
 * Decides whether the unread-only filter should be auto-disabled based on the
 * current and previous unread counts and load state. Extracted from the effect
 * so it can be unit-tested without the full component render.
 *
 * The null-sentinel on `prev` distinguishes "data not yet loaded" from "data
 * loaded and genuinely empty", so the persisted-true + data-loads-empty case
 * fires correctly on the first post-load tick.
 *
 * Returns 'disable' when the filter should flip off and the persistence key
 * should be cleared, 'noop' otherwise.
 *
 * Known accepted edge case: when the unread count transitions 0 → N → 0 in a
 * single React batch (SSE delivering markSlotUnread + markSlotRead together
 * on a backgrounded tab), this helper sees `prev === 0 && current === 0` and
 * returns 'noop'. The filter stays on with an empty list until the next
 * legitimate unread arrives and drains normally — considered acceptable
 * because the user doesn't perceive a backgrounded tab.
 */
export type DrainAction = 'disable' | 'noop'

export function decideUnreadDrain(params: {
  prev: number | null
  current: number
  slotsLoaded: boolean
  showUnreadOnly: boolean
}): DrainAction {
  const { prev, current, slotsLoaded, showUnreadOnly } = params
  if (!slotsLoaded) return 'noop'
  if (!showUnreadOnly) return 'noop'
  const drainedFromPositive = prev !== null && prev > 0 && current === 0
  const loadedEmpty = prev === null && current === 0
  return drainedFromPositive || loadedEmpty ? 'disable' : 'noop'
}
