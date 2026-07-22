import { describe, it, expect } from 'vitest'
import { decideUnreadDrain } from '../pages/unreadDrain'

/**
 * Covers the 3-case state machine for the ChatSidebar unread-filter auto-drain
 *. The effect itself lives in ChatSidebar.tsx, but the decision
 * logic is extracted into `decideUnreadDrain` specifically so it can be tested
 * without a full component render.
 */
describe('decideUnreadDrain', () => {
  describe('slotsLoaded guard', () => {
    it('returns noop when slotsLoaded is false regardless of counts', () => {
      expect(decideUnreadDrain({ prev: null, current: 0, slotsLoaded: false, showUnreadOnly: true })).toBe('noop')
      expect(decideUnreadDrain({ prev: 5, current: 0, slotsLoaded: false, showUnreadOnly: true })).toBe('noop')
      expect(decideUnreadDrain({ prev: 0, current: 3, slotsLoaded: false, showUnreadOnly: true })).toBe('noop')
    })
  })

  describe('showUnreadOnly guard', () => {
    it('returns noop when filter is off, even if conditions would otherwise disable', () => {
      // Filter already off — nothing to disable.
      expect(decideUnreadDrain({ prev: 5, current: 0, slotsLoaded: true, showUnreadOnly: false })).toBe('noop')
      expect(decideUnreadDrain({ prev: null, current: 0, slotsLoaded: true, showUnreadOnly: false })).toBe('noop')
    })
  })

  describe('case 1: persisted=true + data loads with unreads > 0', () => {
    it('returns noop — filter stays on so the user sees their unreads', () => {
      // First post-load tick: prev is still null (sentinel), current > 0.
      expect(decideUnreadDrain({ prev: null, current: 3, slotsLoaded: true, showUnreadOnly: true })).toBe('noop')
    })
  })

  describe('case 2: persisted=true + data loads with unreads === 0 (loadedEmpty)', () => {
    it('returns disable — user would otherwise stare at an empty list', () => {
      expect(decideUnreadDrain({ prev: null, current: 0, slotsLoaded: true, showUnreadOnly: true })).toBe('disable')
    })
  })

  describe('case 3: live drain from >0 to 0 (drainedFromPositive)', () => {
    it('returns disable — the user just read the last unread', () => {
      expect(decideUnreadDrain({ prev: 1, current: 0, slotsLoaded: true, showUnreadOnly: true })).toBe('disable')
      expect(decideUnreadDrain({ prev: 7, current: 0, slotsLoaded: true, showUnreadOnly: true })).toBe('disable')
    })

    it('returns noop while draining but not yet empty', () => {
      // 5 → 3 — still filtering something, don't disable.
      expect(decideUnreadDrain({ prev: 5, current: 3, slotsLoaded: true, showUnreadOnly: true })).toBe('noop')
      // 1 → 1 — stable, don't disable.
      expect(decideUnreadDrain({ prev: 1, current: 1, slotsLoaded: true, showUnreadOnly: true })).toBe('noop')
    })
  })

  describe('known accepted edge case: 0 → 0 batched transition', () => {
    it('returns noop when prev === 0 && current === 0 (documented in helper)', () => {
      // SSE delivers markSlotUnread + markSlotRead in the same React batch while
      // tab is backgrounded; the intermediate >0 state is skipped. Filter stays
      // on with empty list until the next legitimate unread arrives.
      expect(decideUnreadDrain({ prev: 0, current: 0, slotsLoaded: true, showUnreadOnly: true })).toBe('noop')
    })
  })

  describe('growth cases (no drain)', () => {
    it('returns noop when unreads grow from 0 to positive', () => {
      expect(decideUnreadDrain({ prev: 0, current: 5, slotsLoaded: true, showUnreadOnly: true })).toBe('noop')
    })

    it('returns noop when unreads grow from positive to higher positive', () => {
      expect(decideUnreadDrain({ prev: 2, current: 4, slotsLoaded: true, showUnreadOnly: true })).toBe('noop')
    })
  })
})
