/** Duplicating a tab COPIES sessionStorage, so an inherited owner id makes two live browsing
 *  contexts one owner -- which is exactly the state the ownership guard exists to prevent.
 *
 *  The discriminator is a CLAIM the live context holds and releases as its document goes away, not
 *  the navigation type: an ordinary in-app navigation and a duplicated tab both report `navigate`, so
 *  keying on that abandoned the tab's own parked send on every navigation. A claim still held is
 *  positive evidence that a second context is alive under the same id.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  setPaneRecoveryFor, loadPaneRecovery, loadRefusedRecovery, loadNewestRecoveryForSlot,
  PANE_RECOVERY_KEY,
} from './chatPaneRecovery'

const OWNER_KEY = `${PANE_RECOVERY_KEY}:tab`
const CLAIM_PREFIX = 'mc-chat-pane-claim:'
const SLOT = 'slot-copied-tab'
const PROMPT = 'the prompt tab A parked'

/** Model one page load by its own `timeOrigin`. */
const asLoad = (origin: number) => {
  vi.spyOn(performance, 'timeOrigin', 'get').mockReturnValue(origin)
}

/** Model the outgoing document's `pagehide`: the live context releases its claim. jsdom does not
 *  fire that event for us, so the release is explicit here -- the listener is the production half. */
// Enumerated by the index API, not Object.keys: a Storage is not a plain object, so spreading it
// silently yields nothing and a premise check would read as an absence.
const claims = (): string[] => {
  const hits: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k?.startsWith(CLAIM_PREFIX)) hits.push(k)
  }
  return hits
}

const releaseClaims = () => claims().forEach(k => localStorage.removeItem(k))

describe('an owner id inherited by a COPIED tab is reminted, not shared', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => vi.restoreAllMocks())

  it('does not hand a duplicated tab the record its source parked', () => {
    asLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-copy', gen: 1 })
    expect(loadPaneRecovery(SLOT)?.text, 'premise: the source tab reads its own park').toBe(PROMPT)
    expect(sessionStorage.getItem(OWNER_KEY), 'premise: an owner was recorded to inherit').toBeTruthy()
    expect(claims(), 'premise: the source holds its claim').toHaveLength(1)

    // The DUPLICATE: sessionStorage copied verbatim, same localStorage, and the source is STILL
    // ALIVE -- so its claim was never released.
    asLoad(2_000_000)

    expect(loadPaneRecovery(SLOT),
      'a copied context must not own -- and then retire -- its source\u2019s parked send').toBeUndefined()
  })

  it('does not hand a duplicated tab the REFUSED record its source parked', () => {
    asLoad(1_000_000)
    const realSet = localStorage.setItem.bind(localStorage)
    // Only the DURABLE recovery store is full, so the claim writes this fixture reads still land.
    vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k.startsWith(PANE_RECOVERY_KEY)) throw new Error('QuotaExceededError')
      realSet(k, v)
    })

    const durable = setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-refused', gen: 1 })
    expect(durable, 'premise: the durable write was refused, so the payload was parked').toBe(false)
    expect(loadRefusedRecovery(SLOT)?.text, 'premise: the source tab reads its own refused park')
      .toBe(PROMPT)
    expect(claims(), 'premise: the source holds its claim').toHaveLength(1)

    // The DUPLICATE: sessionStorage copied verbatim carries the refused record too, and the source
    // is still alive, so its claim was never released.
    asLoad(2_000_000)

    expect(loadRefusedRecovery(SLOT),
      'a copied context must not restore and RESEND a send it never owned').toBeUndefined()
    expect(loadRefusedRecovery(SLOT, 's-refused'),
      'nor by asking for the send id directly').toBeUndefined()
    expect(loadNewestRecoveryForSlot(SLOT),
      'nor through the reader that spans both stores').toBeUndefined()
  })

  it('still returns the tab its OWN park across a reload', () => {
    // Positive control: reminting on every fresh load would satisfy the assertion above while
    // destroying the reload recovery this whole store exists for.
    asLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-reload', gen: 1 })

    releaseClaims()
    asLoad(3_000_000)

    expect(loadPaneRecovery(SLOT)?.text, 'a reload is the SAME context and keeps its owner').toBe(PROMPT)
  })

  it('keeps the id when no claim can be read at all', () => {
    // Second positive control: an unreadable claim cannot PROVE a copy, and guessing copy there would
    // cost every such engine its reload recovery.
    asLoad(1_000_000)
    setPaneRecoveryFor(SLOT, { text: PROMPT, files: [], sendId: 's-unknown', gen: 1 })

    releaseClaims()
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    getItem.mockImplementation(function (this: Storage, k: string) {
      if (k.startsWith(CLAIM_PREFIX)) throw new Error('blocked')
      return Storage.prototype.getItem.wrappedMethod?.call(this, k) ?? null
    } as typeof Storage.prototype.getItem)
    asLoad(4_000_000)
    getItem.mockRestore()

    expect(loadPaneRecovery(SLOT)?.text).toBe(PROMPT)
  })
})
