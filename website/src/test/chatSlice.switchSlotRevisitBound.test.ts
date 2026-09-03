/**
 * Switching BACK to a slot must not re-fetch the whole transcript.
 *
 * The switch used to go unbounded for any slot with rows already painted, and
 * the comment beside it carried the measurement: 6.2MB/~1s unbounded against
 * 0.7MB/57ms bounded. So a session was fast the first time it was opened and
 * slow every time after — the reported "switching chats got slow and janky",
 * worst on the largest sessions.
 *
 * What the unbounded shape actually protected against is a HOLE: a window
 * sitting entirely newer than the cache leaves a gap mid-transcript. That is a
 * coverage question, so these pins fix the two halves separately — the bound
 * asks for the cache plus a page, and the retry fires on GROWTH large enough to
 * clear that window, not on the response's size.
 */
import { describe, it, expect } from 'vitest'
import {
  slotSwitchFetchLimit,
  slotSwitchNeedsUnboundedRetry,
  OLDER_PAGE_LIMIT,
  SLOT_DETAIL_MAX_LIMIT,
} from '../store/chatSlice'

describe('slotSwitchFetchLimit', () => {
  it('bounds a fresh slot to one page', () => {
    expect(slotSwitchFetchLimit({ streaming: false, cached: 0 })).toBe(OLDER_PAGE_LIMIT)
  })

  it('keeps a streaming slot unbounded', () => {
    // A streaming slot's tail moves under the window, which is the one case a
    // bound genuinely cannot track.
    expect(slotSwitchFetchLimit({ streaming: true, cached: 0 })).toBeUndefined()
    expect(slotSwitchFetchLimit({ streaming: true, cached: 4000 })).toBeUndefined()
  })

  it('bounds a PAINTED idle slot to the cache plus a page, not the whole corpus', () => {
    expect(slotSwitchFetchLimit({ streaming: false, cached: 120 })).toBe(120 + OLDER_PAGE_LIMIT)
  })

  it('never asks past the handler ceiling, which would be clamped silently', () => {
    // chat_handlers clamps with `min(int(limit), 500)`, so asking for more would
    // make the requested limit a lie the coverage check then reasons from.
    expect(slotSwitchFetchLimit({ streaming: false, cached: 5000 })).toBe(SLOT_DETAIL_MAX_LIMIT)
  })
})

describe('slotSwitchNeedsUnboundedRetry', () => {
  const base = { requestedLimit: 200, cached: 100, serverTotal: 900, priorServerTotal: 900 }

  it('does not retry when the slot did not grow while this tab was away', () => {
    expect(slotSwitchNeedsUnboundedRetry(base)).toBe(false)
  })

  it('does not retry for growth smaller than the window just requested', () => {
    expect(slotSwitchNeedsUnboundedRetry({ ...base, serverTotal: 900 + 199 })).toBe(false)
  })

  it('retries once when growth could have cleared the window', () => {
    // 200 new rows against a 200-row window: the oldest row returned may be
    // newer than the newest cached row, which is the hole.
    expect(slotSwitchNeedsUnboundedRetry({ ...base, serverTotal: 900 + 200 })).toBe(true)
  })

  it('retries when overlap cannot be proven, rather than guessing', () => {
    expect(slotSwitchNeedsUnboundedRetry({ ...base, priorServerTotal: undefined })).toBe(true)
    expect(slotSwitchNeedsUnboundedRetry({ ...base, serverTotal: undefined })).toBe(true)
  })

  it('never retries an already-unbounded fetch, or a fresh slot', () => {
    expect(slotSwitchNeedsUnboundedRetry({ ...base, requestedLimit: undefined })).toBe(false)
    expect(slotSwitchNeedsUnboundedRetry({ ...base, cached: 0 })).toBe(false)
  })
})
