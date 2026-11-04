import { describe, it, expect } from 'vitest'
import { decideUnreadDrain } from '../pages/unreadDrain'

type Params = Parameters<typeof decideUnreadDrain>[0]

/** Unread is the only active filter, both before and now, unless a test says otherwise. */
const decide = (p: Partial<Params> & Pick<Params, 'prev' | 'current'>) =>
  decideUnreadDrain({ slotsLoaded: true, showUnreadOnly: true, otherFiltersOn: false, prevOtherFiltersOn: false, ...p })

/**
 * Covers the state machine for the ChatSidebar unread-filter auto-drain. The
 * effect itself lives in chat-sidebar/filters.tsx, but the decision logic is
 * extracted into `decideUnreadDrain` specifically so it can be tested without
 * a full component render.
 */
describe('decideUnreadDrain', () => {
  describe('slotsLoaded guard', () => {
    it('returns noop when slotsLoaded is false regardless of counts', () => {
      expect(decide({ prev: null, current: 0, slotsLoaded: false })).toBe('noop')
      expect(decide({ prev: 5, current: 0, slotsLoaded: false })).toBe('noop')
      expect(decide({ prev: 0, current: 3, slotsLoaded: false })).toBe('noop')
    })
  })

  describe('showUnreadOnly guard', () => {
    it('returns noop when filter is off, even if conditions would otherwise disable', () => {
      expect(decide({ prev: 5, current: 0, showUnreadOnly: false })).toBe('noop')
      expect(decide({ prev: null, current: 0, showUnreadOnly: false })).toBe('noop')
      expect(decide({ prev: 0, current: 0, showUnreadOnly: false, prevOtherFiltersOn: true })).toBe('noop')
    })
  })

  describe('case 1: persisted=true + data loads with unreads > 0', () => {
    it('returns noop — filter stays on so the user sees their unreads', () => {
      expect(decide({ prev: null, current: 3, prevOtherFiltersOn: null })).toBe('noop')
    })
  })

  describe('case 2: persisted=true + data loads with unreads === 0 (loadedEmpty)', () => {
    it('returns disable — user would otherwise stare at an empty list', () => {
      expect(decide({ prev: null, current: 0, prevOtherFiltersOn: null })).toBe('disable')
    })
  })

  describe('case 3: live drain from >0 to 0 (drainedFromPositive)', () => {
    it('returns disable — the user just read the last unread', () => {
      expect(decide({ prev: 1, current: 0 })).toBe('disable')
      expect(decide({ prev: 7, current: 0 })).toBe('disable')
    })

    it('returns noop while draining but not yet empty', () => {
      expect(decide({ prev: 5, current: 3 })).toBe('noop')
      expect(decide({ prev: 1, current: 1 })).toBe('noop')
    })
  })

  describe('another status filter is on, so the list is not empty', () => {
    it('keeps Unread on when the inbox drains', () => {
      expect(decide({ prev: 1, current: 0, otherFiltersOn: true, prevOtherFiltersOn: true })).toBe('noop')
    })

    it('keeps Unread on when the first load finds the inbox empty', () => {
      expect(decide({ prev: null, current: 0, otherFiltersOn: true, prevOtherFiltersOn: null })).toBe('noop')
    })

    it('turns Unread off once the last other filter is lifted over an empty inbox', () => {
      expect(decide({ prev: 0, current: 0, otherFiltersOn: false, prevOtherFiltersOn: true })).toBe('disable')
    })

    it('keeps Unread on when the last other filter is lifted while something is unread', () => {
      expect(decide({ prev: 2, current: 2, otherFiltersOn: false, prevOtherFiltersOn: true })).toBe('noop')
    })
  })

  describe('known accepted edge case: 0 → 0 batched transition', () => {
    it('returns noop when prev === 0 && current === 0 (documented in helper)', () => {
      // SSE delivers markSlotUnread + markSlotRead in the same React batch while
      // tab is backgrounded; the intermediate >0 state is skipped. Filter stays
      // on with empty list until the next legitimate unread arrives.
      expect(decide({ prev: 0, current: 0 })).toBe('noop')
    })
  })

  describe('growth cases (no drain)', () => {
    it('returns noop when unreads grow from 0 to positive', () => {
      expect(decide({ prev: 0, current: 5 })).toBe('noop')
    })

    it('returns noop when unreads grow from positive to higher positive', () => {
      expect(decide({ prev: 2, current: 4 })).toBe('noop')
    })
  })
})
