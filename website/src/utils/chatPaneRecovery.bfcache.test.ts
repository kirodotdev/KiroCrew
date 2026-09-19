/** A BFCache navigation FREEZES the document rather than ending it.
 *
 *  `pagehide` fires either way, so releasing the tab's ownership claim there handed a still-live
 *  tab's records to any other context that asked -- which then cleared them. Only a real teardown
 *  (`persisted === false`) may release, and because timers are frozen while cached, the restore has
 *  to restate the claim or it ages out under a tab that never went away.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setPaneRecoveryFor, loadPaneRecovery } from './chatPaneRecovery'

const CLAIM_PREFIX = 'mc-chat-pane-claim:'
const SLOT = 'slot-bfcache'
const PROMPT = 'the prompt a back-navigation must not lose'

const asLoad = (origin: number) => {
  vi.spyOn(performance, 'timeOrigin', 'get').mockReturnValue(origin)
  vi.spyOn(performance, 'getEntriesByType').mockImplementation(((t: string) =>
    t === 'navigation' ? [{ type: 'navigate' }] : []) as typeof performance.getEntriesByType)
}

const claims = (): string[] => {
  const hits: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k?.startsWith(CLAIM_PREFIX)) hits.push(k)
  }
  return hits
}

/** Fire `pagehide` as the browser does, carrying whether the document was CACHED. */
const pagehide = (persisted: boolean) => {
  const e = new Event('pagehide') as Event & { persisted?: boolean }
  Object.defineProperty(e, 'persisted', { value: persisted })
  globalThis.dispatchEvent(e)
}

describe('a cached document keeps the claim it is still using', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

  it('keeps the claim across a BFCache pagehide', () => {
    asLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-bf', gen: 1 })
    expect(claims(), 'premise: this context holds a claim').toHaveLength(1)

    pagehide(true)

    expect(claims(), 'the document is frozen, not gone -- its records are still its own')
      .toHaveLength(1)
  })

  it('still releases on a real teardown', () => {
    // Positive control: never releasing would satisfy the case above while leaving every closed tab
    // holding its id, so a genuinely new tab could not take over a stranded record.
    asLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-gone', gen: 1 })
    expect(claims()).toHaveLength(1)

    pagehide(false)

    expect(claims(), 'a real unload hands the id back').toHaveLength(0)
  })

  it('restates the claim on restore, after frozen timers let it age', () => {
    asLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-aged', gen: 1 })
    pagehide(true)
    const key = claims()[0]
    // Timers do not run while cached, so the heartbeat could not have refreshed it.
    localStorage.setItem(key, String(Date.now() - 10 * 60_000))

    globalThis.dispatchEvent(new Event('pageshow'))

    expect(Date.now() - Number(localStorage.getItem(key)),
      'the restore refreshes it, so a live tab still confirms').toBeLessThan(60_000)
    expect(loadPaneRecovery(SLOT)?.text, 'and the record is still this tab\u2019s').toBe(PROMPT)
  })
})
