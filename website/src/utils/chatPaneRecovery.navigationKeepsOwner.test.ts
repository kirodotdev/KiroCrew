/** An ORDINARY navigation must not abandon the tab's parked send.
 *
 *  A same-tab full-document navigation reports performance nav type `navigate` -- the same value a
 *  duplicated tab reports -- so treating that as evidence of a copy reminted the owner id on every
 *  navigation. `ownedHere` then rejected the tab's own record permanently, `loadPaneRecovery` skipped
 *  it, and the payload sat in localStorage until its TTL with no recovery and no error shown.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setPaneRecoveryFor, loadPaneRecovery } from './chatPaneRecovery'

const CLAIM_PREFIX = 'mc-chat-pane-claim:'
const SLOT = 'slot-navigated-away'
const PROMPT = 'the words a failed send parked'

/** A load that the browser says was reached by NAVIGATION, not a reload. */
const asNavigatedLoad = (origin: number) => {
  vi.spyOn(performance, 'timeOrigin', 'get').mockReturnValue(origin)
  vi.spyOn(performance, 'getEntriesByType').mockImplementation(((t: string) =>
    t === 'navigation' ? [{ type: 'navigate' }] : []) as typeof performance.getEntriesByType)
}

/** The outgoing document's `pagehide`: the live context releases its claim on the way out. */
const releaseClaims = () => {
  const doomed: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k?.startsWith(CLAIM_PREFIX)) doomed.push(k)
  }
  doomed.forEach(k => localStorage.removeItem(k))
}

describe('an ordinary navigation keeps the tab its parked send', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => vi.restoreAllMocks())

  it('still restores a record parked before the navigation', () => {
    asNavigatedLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-nav', gen: 1 })
    expect(loadPaneRecovery(SLOT)?.text, 'premise: the tab reads its own park').toBe(PROMPT)

    // Navigate within the SAME tab: the document is replaced, so its claim is released, and the new
    // document reports nav type `navigate` exactly as a duplicated tab would.
    releaseClaims()
    asNavigatedLoad(2_000_000)

    expect(loadPaneRecovery(SLOT)?.text,
      'a navigation is the same tab -- stranding its record leaves no recovery and no error').toBe(PROMPT)
  })

  it('still remints while another context HOLDS the claim', () => {
    // Positive control: a blanket "always keep" would hand a duplicated tab its source's record.
    // The claim is NOT released here, so the source is still alive.
    asNavigatedLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-dup', gen: 1 })

    asNavigatedLoad(3_000_000)

    expect(loadPaneRecovery(SLOT), 'a held claim is a live second context').toBeUndefined()
  })
})
