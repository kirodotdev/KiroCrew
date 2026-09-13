import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RECOVERY_MAX_STORE_BYTES } from '../utils/draftConstants'
import { PANE_RECOVERY_KEY, __resetPaneRecoveryForTests, adoptPaneRecovery, clearPaneRecoveryFor, loadPaneRecoveryById, loadRefusedRecovery, setPaneRecoveryFor } from '../utils/chatPaneRecovery'

// Imported, not retyped: a hand-written prefix matched NOTHING, so every byte assertion in this file
// was reading an empty set and passing vacuously.
const RECOVERY_PREFIX = `${PANE_RECOVERY_KEY}:`

const storeBytes = (): number => {
  let n = 0
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith(RECOVERY_PREFIX)) n += k.length + (localStorage.getItem(k)?.length ?? 0)
  }
  return n
}

const recoveryKeys = (): string[] => {
  const out: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith(RECOVERY_PREFIX)) out.push(k)
  }
  return out
}

describe('the recovery store is bounded even when nothing is evictable', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPaneRecoveryForTests()
  })

  it('reclaims markers and retries when a SIBLING store exhausted the shared origin', () => {
    // A marker-only record exists and this store is well under its own budget, so `enforceBudget`
    // sees nothing to do -- the quota failure comes from elsewhere in the origin.
    setPaneRecoveryFor('slot-marker', { text: '', files: [], sendId: 'm-old' })
    expect(storeBytes(), 'premise: this store is under its own budget')
      .toBeLessThan(RECOVERY_MAX_STORE_BYTES)

    const store = globalThis.localStorage
    const realSet = store.setItem.bind(store)
    let originFull = true
    vi.spyOn(store, 'setItem').mockImplementation((k: string, v: string) => {
      // Refuses until this store gives bytes back, which is what the retry has to provoke.
      if (originFull && k.startsWith(RECOVERY_PREFIX) && recoveryKeys().some(x => x.includes('m-old'))) {
        throw new Error('QuotaExceededError')
      }
      originFull = false
      realSet(k, v)
    })

    const landed = setPaneRecoveryFor('slot-retry', { text: 'the recovered prompt', files: [], sendId: 's-retry' })
    vi.restoreAllMocks()

    expect(landed,
      'the prompt must reach the durable store after markers are reclaimed, not fall to session-only')
      .toBe(true)
    expect(loadPaneRecoveryById('slot-retry', 's-retry')?.text,
      'and it must read back after the reload it exists for')
      .toBe('the recovered prompt')
  })

  it('stays bounded when prompts accumulate, retaining the refused one outside localStorage', () => {
    // Every record here CARRIES a prompt. The cap binds them too: skipping it let unresolved sends
    // accumulate per-send until the shared origin quota went, losing the sibling drafts as well.
    const chunk = 'x'.repeat(24 * 1024)
    const landed: boolean[] = []
    for (let i = 0; i < 60; i++) {
      landed.push(setPaneRecoveryFor('slot-' + i, { text: chunk + i, files: [], sendId: 's-' + i }))
    }

    expect(storeBytes(),
      'a sustained outage must not be able to exhaust the shared origin quota')
      .toBeLessThanOrEqual(RECOVERY_MAX_STORE_BYTES)
    // Refused is NOT lost: the payload is retained outside localStorage for this session, and the
    // `false` tells the caller to keep its own copy instead of clearing on a durability it never got.
    const refused = landed.indexOf(false)
    expect(refused, 'premise: this fixture must genuinely exceed the budget').toBeGreaterThanOrEqual(0)
    expect(loadRefusedRecovery('slot-' + refused, 's-' + refused)?.text,
      'a prompt the cap refused must stay reachable rather than being dropped')
      .toContain('x')
  })

  it('reports the refusal instead of claiming a record it did not keep', () => {
    const chunk = 'y'.repeat(24 * 1024)
    for (let i = 0; i < 60; i++) {
      setPaneRecoveryFor('fill-' + i, { text: chunk + i, files: [], sendId: 'f-' + i })
    }
    const answer = setPaneRecoveryFor('late', { text: 'z'.repeat(24 * 1024), files: [], sendId: 's-late' })
    const kept = recoveryKeys().some(k => k.includes('s-late'))

    expect(answer,
      'a caller holding the only other copy must be told, or it clears the composer on a lie')
      .toBe(kept)
  })

  it('refuses an over-budget prompt without destroying the record already durable', () => {
    const first = setPaneRecoveryFor('slot-keep', { text: 'the durable prompt', files: [], sendId: 's-keep' })
    expect(first, 'premise: the small record lands').toBe(true)

    const huge = 'z'.repeat(RECOVERY_MAX_STORE_BYTES + 4096)
    const kept = setPaneRecoveryFor('slot-keep', { text: huge, files: [], sendId: 's-keep' })

    expect(kept, 'a record the cap refused must never be reported as durable').toBe(false)
    expect(loadPaneRecoveryById('slot-keep', 's-keep')?.text,
      'the rollback restores what was already durable rather than leaving the key empty')
      .toBe('the durable prompt')
    expect(loadRefusedRecovery('slot-keep', 's-keep')?.text,
      'and the refused payload is retained outside localStorage')
      .toBe(huge)
  })

  it('parks the payload when the ORIGIN is full even though this store is under budget', () => {
    // The two conditions co-occur in the ordinary case the store documents: a sibling store exhausts
    // the shared quota while this store holds almost nothing, so its own budget check passes.
    const store = globalThis.localStorage
    const realSet = store.setItem.bind(store)
    let quotaExhausted = false
    vi.spyOn(store, 'setItem').mockImplementation((k: string, v: string) => {
      if (quotaExhausted && k.startsWith(PANE_RECOVERY_KEY)) {
        const err = new Error('QuotaExceededError')
        err.name = 'QuotaExceededError'
        throw err
      }
      realSet(k, v)
    })

    quotaExhausted = true
    const landed = setPaneRecoveryFor('slot-full-origin', {
      text: 'words the composer already cleared', files: [], sendId: 's-quota',
    })
    // Restored immediately: a leaked spy makes every later write in this file fail, which reads as a
    // fixture problem in the NEXT test rather than as this one's doing.
    quotaExhausted = false
    vi.restoreAllMocks()

    expect(landed, 'a write that never reached the store must not report durability').toBe(false)
    expect(loadPaneRecoveryById('slot-full-origin', 's-quota'),
      'premise: the durable store must genuinely not hold it').toBeUndefined()
    // Nothing else holds these words: the caller's copy is in memory and a reload discards it.
    expect(loadRefusedRecovery('slot-full-origin', 's-quota')?.text,
      'a failed write with a passing budget check leaves no recovery path')
      .toContain('words the composer already cleared')
  })

  it('adopts a record the durable store REFUSED, so a retry can retire it', () => {
    const store = globalThis.localStorage
    const realSet = store.setItem.bind(store)
    let quotaExhausted = true
    vi.spyOn(store, 'setItem').mockImplementation((k: string, v: string) => {
      if (quotaExhausted && k.startsWith(RECOVERY_PREFIX)) throw new Error('QuotaExceededError')
      realSet(k, v)
    })

    // A refusal restores with NO send id, so the record is parked under the BARE slot key -- and when
    // the durable write was refused, the only copy of it lives in the session fallback.
    setPaneRecoveryFor('slot-adopt', { text: 'the delivered prompt', files: [] })
    quotaExhausted = false
    vi.restoreAllMocks()

    expect(loadPaneRecoveryById('slot-adopt', 's-retry'),
      'premise: nothing is bound to the retry yet').toBeUndefined()
    expect(loadRefusedRecovery('slot-adopt')?.text,
      'premise: the fallback is the only home of the unidentified record').toContain('the delivered prompt')

    // The retry binds its send id. Reading only the durable bare key returned undefined here, so the
    // arm stayed unbound and the settlement block -- guarded on a bound record -- retired nothing.
    const bound = adoptPaneRecovery('slot-adopt', 's-retry')
    expect(bound?.sendId, 'the retry must end up with a record it can settle').toBe('s-retry')

    expect(loadRefusedRecovery('slot-adopt'),
      'the unidentified fallback must not survive its own adoption').toBeUndefined()

    clearPaneRecoveryFor('slot-adopt', 's-retry')
    expect(loadPaneRecoveryById('slot-adopt', 's-retry'), 'settled durable copy').toBeUndefined()
    expect(loadRefusedRecovery('slot-adopt', 's-retry'),
      'a delivered prompt must not survive anywhere to re-arm on the next reload').toBeUndefined()
  })

  it('finds the refused payload from the SLOT alone, which is all a reload knows', () => {
    const store = globalThis.localStorage
    const realSet = store.setItem.bind(store)
    let quotaExhausted = false
    vi.spyOn(store, 'setItem').mockImplementation((k: string, v: string) => {
      if (quotaExhausted && k.startsWith(PANE_RECOVERY_KEY)) {
        const err = new Error('QuotaExceededError')
        err.name = 'QuotaExceededError'
        throw err
      }
      realSet(k, v)
    })

    quotaExhausted = true
    setPaneRecoveryFor('slot-reload', { text: 'the older send', files: [], sendId: 's-old' })
    setPaneRecoveryFor('slot-reload', { text: 'the newer send', files: [], sendId: 's-new' })
    quotaExhausted = false
    vi.restoreAllMocks()

    expect(loadPaneRecoveryById('slot-reload', 's-new'),
      'premise: the durable store must not hold it').toBeUndefined()
    // A reload has the slot and no send id, so rebuilding `pane:<slot>` cannot reach an id-keyed record.
    expect(loadRefusedRecovery('slot-reload')?.text,
      'the only surviving copy is unreachable from a slot-only load')
      .toContain('the newer send')
  })

  it('never evicts an unsettled prompt for budget, and keeps the refused one reachable', () => {
    // The lane's scenario: several durable unsettled prompts, then byte pressure past the cap. This
    // store is each prompt's only durable home, so reclaiming one loses the user's words outright.
    const chunk = 'q'.repeat(24 * 1024)
    const durable: string[] = []
    for (let i = 0; i < 20; i++) {
      const text = chunk + i
      if (setPaneRecoveryFor('slot-live-' + i, { text, files: [], sendId: 'p-' + i })) durable.push('p-' + i)
    }
    expect(durable.length, 'premise: at least two prompts must have landed durably')
      .toBeGreaterThanOrEqual(2)

    // Tips the budget: no live prompt may be taken to make room, and the incoming one is kept too.
    const newest = setPaneRecoveryFor('slot-newest', {
      text: 'the words the composer just cleared'.padEnd(64 * 1024, '!'), files: [], sendId: 's-newest',
    })

    // Read back through the store, which is the reload -- nothing in memory carries these.
    const survivors = durable.filter((id, i) => loadPaneRecoveryById('slot-live-' + i, id) !== undefined)
    expect(survivors.length,
      'a prompt evicted for budget is gone from its only durable home')
      .toBe(durable.length)

    expect(newest, 'a record the cap refused must not be reported as durable').toBe(false)
    // Refused, not lost: the newest words are retained outside localStorage, and `false` is what
    // stops the caller clearing the composer on a durability the store never granted.
    expect(loadRefusedRecovery('slot-newest', 's-newest')?.text,
      'so the newest words stay reachable for this session')
      .toContain('the words the composer just cleared')
  })

  it('refuses a single payload larger than the whole budget, retaining it outside localStorage', () => {
    // A payload past the cap alone can never be durable, so the honest answer is a refusal plus a
    // reachable copy -- not a `true` the caller would clear the composer on.
    const words = 'x'.repeat(RECOVERY_MAX_STORE_BYTES + 8192)
    const kept = setPaneRecoveryFor('slot-huge', { text: words, files: [], sendId: 's-huge' })

    expect(kept, 'a payload the cap cannot hold must not claim durability').toBe(false)
    expect(loadRefusedRecovery('slot-huge', 's-huge')?.text.length,
      'and it stays readable for the session that still holds it')
      .toBe(words.length)
  })
})
