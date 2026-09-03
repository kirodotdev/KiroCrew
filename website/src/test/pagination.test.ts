// Feature: chat-older-history
// The whole gate: provenance and scrollability checks were removed, not relocated.
import { describe, it, expect } from 'vitest'
import { shouldPaginateOlder, canForkAtWindow, searchScopeIsLimited, shouldContinueOlderWalk } from '../pages/chat/pagination'

describe('shouldPaginateOlder', () => {
  it('paginates when the server reported more history and nothing is in flight', () => {
    expect(shouldPaginateOlder({ loadingOlder: false, slotHasMore: true })).toBe(true)
  })

  it('does not paginate while a fetch is already in flight', () => {
    expect(shouldPaginateOlder({ loadingOlder: true, slotHasMore: true })).toBe(false)
  })

  it('does not paginate when the server reported no more history', () => {
    expect(shouldPaginateOlder({ loadingOlder: false, slotHasMore: false })).toBe(false)
  })

  it('an in-flight fetch outranks unloaded history', () => {
    expect(shouldPaginateOlder({ loadingOlder: true, slotHasMore: false })).toBe(false)
  })
})

describe('canForkAtWindow', () => {
  const base = { isStreaming: false, isInject: false, slotHasMore: false, cursorIsForActiveSlot: true }

  it('allows forking when the whole history is loaded', () => {
    expect(canForkAtWindow(base)).toBe(true)
  })

  it('refuses when the server reports older history', () => {
    expect(canForkAtWindow({ ...base, slotHasMore: true })).toBe(false)
  })

  // A switch installs a cached window and nulls the cursor key while leaving
  // `slotHasMore` describing the slot being left, so this pairing is reachable.
  it('refuses on a cached window whose cursor belongs to another slot', () => {
    expect(canForkAtWindow({ ...base, slotHasMore: false, cursorIsForActiveSlot: false })).toBe(false)
  })

  it('refuses while streaming or injecting regardless of the window', () => {
    expect(canForkAtWindow({ ...base, isStreaming: true })).toBe(false)
    expect(canForkAtWindow({ ...base, isInject: true })).toBe(false)
  })
})

describe('searchScopeIsLimited', () => {
  const base = { slotHasMore: false, cursorIsForActiveSlot: true }

  it('reads as complete only when the whole history is loaded for THIS slot', () => {
    expect(searchScopeIsLimited(base)).toBe(false)
  })

  it('qualifies the count when the server reports older history', () => {
    expect(searchScopeIsLimited({ ...base, slotHasMore: true })).toBe(true)
  })

  // The harmful pairing: a cached switch nulls the cursor key but leaves
  // `slotHasMore` describing the slot being left, so false here is not this slot's.
  it('qualifies the count on a cached window whose cursor belongs to another slot', () => {
    expect(searchScopeIsLimited({ slotHasMore: false, cursorIsForActiveSlot: false })).toBe(true)
  })

  it('an untrustworthy cursor cannot be overridden by a stale hasMore either way', () => {
    expect(searchScopeIsLimited({ slotHasMore: true, cursorIsForActiveSlot: false })).toBe(true)
  })
})

describe('shouldContinueOlderWalk', () => {
  const base = { sawRealInput: true, nearTop: false, walking: true, pagesSinceInput: 0 }

  it('walks while the reader is waiting on the walk they started', () => {
    expect(shouldContinueOlderWalk(base)).toBe(true)
    expect(shouldContinueOlderWalk({ ...base, walking: false, nearTop: true })).toBe(true)
  })

  it('never walks before the reader has expressed intent this session', () => {
    // A refresh parks a reader with zero wheel/touch input; boot-phase
    // transients must not be able to start a walk over them.
    expect(shouldContinueOlderWalk({ ...base, sawRealInput: false })).toBe(false)
  })

  it('stops once the page budget for one expression of intent is spent', () => {
    // The `walking` latch is still true here — each landing re-establishes it —
    // so ONLY the budget can end the walk. Without it, one wheel event walked
    // the whole archive and the spinner never cleared.
    expect(shouldContinueOlderWalk({ ...base, pagesSinceInput: 4 })).toBe(false)
    expect(shouldContinueOlderWalk({ ...base, pagesSinceInput: 3 })).toBe(true)
  })

  it('refills the budget on fresh input, so a climbing reader is continuous', () => {
    // The caller zeroes the counter on wheel/touch; proven here as the
    // budget-respecting boundary the caller relies on.
    expect(shouldContinueOlderWalk({ ...base, pagesSinceInput: 0, maxPages: 4 })).toBe(true)
  })

  it('does not walk when the reader is neither near the top nor waiting', () => {
    expect(shouldContinueOlderWalk({ ...base, walking: false, nearTop: false })).toBe(false)
  })
})
