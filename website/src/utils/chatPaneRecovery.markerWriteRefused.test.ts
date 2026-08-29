/** `setStagedSend` discarded `writeOne`'s landed/failed boolean, so a FULL localStorage refused the
 *  duplicate-send marker in silence — while the slot-scoped composer draft it warns about stayed
 *  persisted by its own debounced effect. A reload then restored an unmarked prompt, and resending
 *  could duplicate a turn the server had already accepted.
 *
 *  The two stores are refused independently because that is the whole point: the condition that
 *  exhausts the record store's quota does not exhaust this tab's own.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setStagedSend, loadStagedSend, clearStagedSend } from './chatPaneRecovery'

const FALLBACK = 'mc-chat-staged-send:'
const SLOT = 'slot-full-storage'
const SEND = 's-indeterminate'

/** Refuse every write to the named store(s), the way an exhausted quota does. */
const refuse = (...stores: Storage[]) => {
  const real = Storage.prototype.setItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, k: string, v: string) {
    if (stores.includes(this)) throw new DOMException('quota', 'QuotaExceededError')
    return real.call(this, k, v)
  })
}

describe('a refused marker write never leaves the draft unwarned', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => vi.restoreAllMocks())

  it('keeps the warning readable when the record store is FULL', () => {
    refuse(localStorage)

    expect(setStagedSend(SLOT, SEND), 'the marker still lands durably for this tab').toBe(true)
    expect(loadStagedSend(SLOT), 'a reload of this slot must still see the warning').toBe(SEND)
  })

  it('reports failure when EVERY store refuses', () => {
    // Mutation control: a hardcoded `true`, or no result at all, would satisfy the test above.
    refuse(localStorage, sessionStorage)

    expect(setStagedSend(SLOT, SEND)).toBe(false)
    expect(loadStagedSend(SLOT)).toBeUndefined()
  })

  it('retires the fallback too, so a dismissed warning stays dismissed', () => {
    refuse(localStorage)
    setStagedSend(SLOT, SEND)
    vi.restoreAllMocks()

    clearStagedSend(SLOT)

    expect(loadStagedSend(SLOT)).toBeUndefined()
    expect(sessionStorage.getItem(FALLBACK + SLOT)).toBeNull()
  })

  it('does not reach for the fallback while storage is healthy', () => {
    // Positive control: an unconditional fallback would satisfy the first test while hiding a
    // broken primary path from every cross-tab reader.
    expect(setStagedSend(SLOT, SEND)).toBe(true)

    expect(sessionStorage.getItem(FALLBACK + SLOT),
      'the record store took it, so nothing fell back').toBeNull()
    expect(loadStagedSend(SLOT)).toBe(SEND)
  })
})
