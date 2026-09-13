/** `gen` counts edits to ONE record -- the writer reads that record's prior value and adds one -- so
 *  it says nothing about which of TWO records is newer. A record edited twice therefore outranked a
 *  record parked later, and a reload restored the older composer content.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { loadPaneRecovery, setPaneRecoveryFor } from './chatPaneRecovery'

const SLOT = 'slot-newest-parked-wins'

describe('the newest parked record wins, not the most-edited one', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => vi.restoreAllMocks())

  it('prefers the record parked LAST over one carrying a larger per-record gen', () => {
    const now = vi.spyOn(Date, 'now')

    now.mockReturnValue(1_000_000)
    setPaneRecoveryFor(SLOT, { text: 'edited twice, but parked first', files: [], sendId: 's-A', gen: 2 })

    now.mockReturnValue(2_000_000)
    setPaneRecoveryFor(SLOT, { text: 'the newer words', files: [], sendId: 's-B', gen: 1 })

    expect(loadPaneRecovery(SLOT)?.text,
      'a per-record edit count cannot order two DIFFERENT records').toBe('the newer words')
  })

  it('still orders records that carry no gen at all', () => {
    // Positive control: reading `gen` was the ONLY ordering before, so a fix must not depend on it.
    const now = vi.spyOn(Date, 'now')

    now.mockReturnValue(3_000_000)
    setPaneRecoveryFor(SLOT, { text: 'older, ungenerationed', files: [], sendId: 's-C' })

    now.mockReturnValue(4_000_000)
    setPaneRecoveryFor(SLOT, { text: 'newer, ungenerationed', files: [], sendId: 's-D' })

    expect(loadPaneRecovery(SLOT)?.text).toBe('newer, ungenerationed')
  })

  it('returns the only record when a slot has just one', () => {
    // Positive control: an ordering fix that dropped every candidate would satisfy neither of the
    // assertions above by returning undefined.
    setPaneRecoveryFor(SLOT, { text: 'the sole parked send', files: [], sendId: 's-E', gen: 7 })

    expect(loadPaneRecovery(SLOT)?.text).toBe('the sole parked send')
  })
})
