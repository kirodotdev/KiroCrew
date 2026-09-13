/** A crashed context must not strand the only copy of an unsent prompt.
 *
 *  The claim that arbitrates ownership is released as a document goes away, but a renderer crash runs
 *  no release. Treating a merely PRESENT claim as a live holder meant the reload reminted the tab id,
 *  `ownedHere` then refused the tab's own record, and the payload sat unreachable until its 30-day
 *  TTL. So a claim only speaks while its holder keeps confirming it, and a record whose owner is gone
 *  stays adoptable however that owner's id was lost.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setPaneRecoveryFor, loadPaneRecovery } from './chatPaneRecovery'

const SLOT = 'slot-crashed-owner'
const PROMPT = 'the only copy of the prompt'

/** A load reached by NAVIGATION, so nothing but the claim can decide ownership. */
const asNavigatedLoad = (origin: number) => {
  vi.spyOn(performance, 'timeOrigin', 'get').mockReturnValue(origin)
  vi.spyOn(performance, 'getEntriesByType').mockImplementation(((t: string) =>
    t === 'navigation' ? [{ type: 'navigate' }] : []) as typeof performance.getEntriesByType)
}

/** A load the browser reports as a RELOAD, which is how a user returns from a crash page. */
const asReload = (origin: number) => {
  vi.spyOn(performance, 'timeOrigin', 'get').mockReturnValue(origin)
  vi.spyOn(performance, 'getEntriesByType').mockImplementation(((t: string) =>
    t === 'navigation' ? [{ type: 'reload' }] : []) as typeof performance.getEntriesByType)
}

describe('a crash cannot orphan the record it parked', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('restores the prompt on the reload after a crash left the claim held', () => {
    asNavigatedLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-crash', gen: 1 })
    expect(loadPaneRecovery(SLOT)?.text, 'premise: the tab reads its own park').toBe(PROMPT)

    // The crash: no `pagehide`, so the claim is never released. The user reloads.
    asReload(2_000_000)

    expect(loadPaneRecovery(SLOT)?.text,
      'a reload is definitionally the same tab, so the id is still ours').toBe(PROMPT)
  })

  it('restores the prompt once an abandoned claim stops being confirmed', () => {
    asNavigatedLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-stale', gen: 1 })

    // Not a reload this time -- the user opens the app fresh. The abandoned claim has no live holder
    // refreshing it, so it must stop speaking for the id rather than withholding the record for good.
    vi.useFakeTimers()
    vi.setSystemTime(Date.now() + 10 * 60_000)
    asNavigatedLoad(3_000_000)

    expect(loadPaneRecovery(SLOT)?.text,
      'an unconfirmed claim must not withhold a record until the TTL').toBe(PROMPT)
  })

  it('still withholds the record while the holder KEEPS confirming', () => {
    // Positive control: a blanket "always adopt" would satisfy both cases above while handing a
    // duplicated tab its source's record, which is the harm the ownership guard exists for.
    asNavigatedLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-live', gen: 1 })

    asNavigatedLoad(3_000_000)

    expect(loadPaneRecovery(SLOT), 'a live holder still owns it').toBeUndefined()
  })
})
