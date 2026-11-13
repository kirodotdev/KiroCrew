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
 * Active status filters are OR'd, so while another one (Running, Pinned,
 * Recent, Crew) is on, an empty inbox does not empty the list: the drain keeps
 * Unread on then, and only fires once the last other filter is lifted over an
 * inbox that is still empty.
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
  /** Another show-only status filter is active now. */
  otherFiltersOn: boolean
  /** The same, as of the previous decision; null before the first one. */
  prevOtherFiltersOn: boolean | null
}): DrainAction {
  const { prev, current, slotsLoaded, showUnreadOnly, otherFiltersOn, prevOtherFiltersOn } = params
  if (!slotsLoaded) return 'noop'
  if (!showUnreadOnly) return 'noop'
  if (otherFiltersOn) return 'noop'
  if (current !== 0) return 'noop'
  const drainedFromPositive = prev !== null && prev > 0
  const loadedEmpty = prev === null
  const lastOtherFilterLifted = prevOtherFiltersOn === true
  return drainedFromPositive || loadedEmpty || lastOtherFilterLifted ? 'disable' : 'noop'
}
