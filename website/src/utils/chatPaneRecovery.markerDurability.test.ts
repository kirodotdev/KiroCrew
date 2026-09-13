/** The page marker must be exactly as durable as the draft it captions: same store, same TTL.
 *  It now lives in the recovery store, so this pins that the merge did not weaken it. */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { PANE_RECOVERY_KEY, loadStagedSend, setStagedSend, clearStagedSend, setPaneRecoveryFor, clearPaneRecoveryFor, clearUnidentifiedPaneRecovery, adoptPaneRecovery, __resetPaneRecoveryForTests, loadPaneRecovery, loadPaneRecoveryById } from './chatPaneRecovery'
import { DRAFT_MAX_ENTRIES, DRAFT_MAX_STORE_BYTES, RECOVERY_MAX_STORE_BYTES } from './draftConstants'
import { DRAFTS_KEY } from './chatDrafts'

describe('the staged-send marker survives a reload in the recovery store', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); __resetPaneRecoveryForTests() })

  it('persists to localStorage and reads back', () => {
    setStagedSend('slot-a', 's-durable-1')
    // Enumerated by the index API, not Object.keys: a Storage is not a plain object, so spreading it
    // silently yields nothing and the assertion would read as an absence.
    const marker = (store: Storage) => {
      const hits: string[] = []
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i)
        if (k?.startsWith(`${PANE_RECOVERY_KEY}:page:slot-a`)) hits.push(k)
      }
      return hits
    }
    expect(marker(localStorage), 'the marker is as durable as the draft it captions').toHaveLength(1)
    expect(marker(sessionStorage), 'sessionStorage holds only the owner id, never the record').toHaveLength(0)
    expect(loadStagedSend('slot-a')).toBe('s-durable-1')
  })

  it('does not collide with a pane payload for the SAME slot', () => {
    // Measured: MembersPage renders <ChatPane slotKey={activeSlot}>, so both surfaces can hold
    // restored work for one slot. An unqualified key would have one clobber the other.
    setPaneRecoveryFor('slot-a', { text: 'the pane payload', files: [], sendId: 's-pane' })
    setStagedSend('slot-a', 's-page')
    expect(loadPaneRecovery('slot-a')?.text).toBe('the pane payload')
    expect(loadStagedSend('slot-a')).toBe('s-page')
  })

  it('clears only its own record', () => {
    setStagedSend('slot-a', 's-1')
    clearStagedSend('slot-a')
    expect(loadStagedSend('slot-a')).toBeUndefined()
  })

  it('uses a different key from the drafts store', () => {
    expect(DRAFTS_KEY).not.toBe(PANE_RECOVERY_KEY)
  })
})

describe('concurrent recoveries and the shared origin budget', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  it('keeps BOTH tabs recoveries for one slot, so the later write loses nothing', () => {
    setPaneRecoveryFor('slot-x', { text: 'tab one prompt', files: [], sendId: 's-one', gen: 1 })
    setPaneRecoveryFor('slot-x', { text: 'tab two prompt', files: [], sendId: 's-two', gen: 2 })

    const texts = [loadPaneRecoveryById('slot-x', 's-one')?.text, loadPaneRecoveryById('slot-x', 's-two')?.text].sort()
    expect(texts,
      'a payload has no in-system recovery once overwritten, so one shared slot key lost a prompt')
      .toEqual(['tab one prompt', 'tab two prompt'])
    expect(loadPaneRecovery('slot-x')?.text, 'the reader still resolves one record: the newest')
      .toBe('tab two prompt')
  })

  it('retires only the send it names, leaving the other tab parked', () => {
    setPaneRecoveryFor('slot-y', { text: 'keep me', files: [], sendId: 's-keep', gen: 1 })
    setPaneRecoveryFor('slot-y', { text: 'settle me', files: [], sendId: 's-done', gen: 2 })

    clearPaneRecoveryFor('slot-y', 's-done')

    expect(loadPaneRecovery('slot-y')?.text, 'a definitive receipt for one send is not a discard of the other')
      .toBe('keep me')
  })


  it('keeps every capped store inside the shared origin budget', () => {
    // TWO existing stores at 2 MiB plus the recovery store's own 512 KiB = 4.5 MiB against the
    // ~5 MB an origin gives. Recovery is budgeted ON TOP so its arrival cannot shrink the others.
    expect(DRAFT_MAX_STORE_BYTES * 2 + RECOVERY_MAX_STORE_BYTES,
      'the three capped stores together must still fit the shared origin quota')
      .toBeLessThan(5 * 1024 * 1024)
    expect(DRAFT_MAX_STORE_BYTES,
      'lowering this evicts drafts a user already typed, purely because the constant moved')
      .toBe(2 * 1024 * 1024)
  })
})

describe('a concurrent tab cannot lose a recovery', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  /* The window is between PROCESSES, so no in-realm interleave reproduces it; what closes it is
   * that a write no longer depends on a prior read of shared state. That is the invariant here. */
  it('writes its own key without first reading shared state', () => {
    const order: string[] = []
    const realGet = localStorage.getItem.bind(localStorage)
    const realSet = localStorage.setItem.bind(localStorage)
    const g = vi.spyOn(localStorage, 'getItem').mockImplementation((k: string) => {
      if (k.startsWith(PANE_RECOVERY_KEY)) order.push('read')
      return realGet(k)
    })
    const w = vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k.startsWith(PANE_RECOVERY_KEY)) order.push('write')
      realSet(k, v)
    })

    setPaneRecoveryFor('slot-z', { text: 'this tabs prompt', files: [], sendId: 's-mine', gen: 1 })

    g.mockRestore()
    w.mockRestore()
    expect(order.length, 'premise: the call really did touch the store').toBeGreaterThan(0)
    expect(order[0],
      'a read BEFORE the write is the cross-tab window: what it saw can be stale by the time we save')
      .toBe('write')
  })

  it('leaves a sibling tabs record untouched when it writes its own', () => {
    setPaneRecoveryFor('slot-z', { text: 'the other tabs prompt', files: [], sendId: 's-other', gen: 5 })
    setPaneRecoveryFor('slot-z', { text: 'this tabs prompt', files: [], sendId: 's-mine', gen: 1 })

    expect([loadPaneRecoveryById('slot-z', 's-other')?.text, loadPaneRecoveryById('slot-z', 's-mine')?.text].sort(),
      'each send owns its own storage key, so neither write can reconcile the other away')
      .toEqual(['the other tabs prompt', 'this tabs prompt'])
  })
})

describe('a full origin must not swallow the recovery', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  it('reclaims and retries when the first write hits quota', () => {
    // A disposable cache the reclaim tiers may sacrifice; without one there is nothing to free and
    // even a retrying writer stays stuck, which is the origin-full case the finding describes.
    localStorage.setItem('mc-paste-store-v1', JSON.stringify({ big: 'x'.repeat(2048) }))
    const realSet = localStorage.setItem.bind(localStorage)
    let thrown = false
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      // One genuine quota failure, exactly as a full origin gives: `safeSetItem` reclaims a tier
      // and retries, while a bare setItem in a swallowing try/catch loses the record for good.
      if (!thrown && k.startsWith(PANE_RECOVERY_KEY)) {
        thrown = true
        throw new DOMException('exceeded the quota', 'QuotaExceededError')
      }
      realSet(k, v)
    })

    setPaneRecoveryFor('slot-q', { text: 'the only copy of this prompt', files: [], sendId: 's-quota' })
    spy.mockRestore()

    expect(thrown, 'premise: the probe really did raise a quota error').toBe(true)
    expect(localStorage.getItem('mc-paste-store-v1'),
      'premise: the reclaim path ran and sacrificed the disposable cache').toBeNull()
    expect(loadPaneRecoveryById('slot-q', 's-quota')?.text,
      'the composer was already cleared, so a swallowed write loses the prompt with no other copy')
      .toBe('the only copy of this prompt')
  })
})

describe('a delete must never outrun the write', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })


  it('retires only the named send, never every record the slot owns', () => {
    setPaneRecoveryFor('slot-two', { text: 'this tabs prompt', files: [], sendId: 's-mine' })
    setPaneRecoveryFor('slot-two', { text: 'the other tabs prompt', files: [], sendId: 's-theirs' })

    clearPaneRecoveryFor('slot-two', 's-mine')

    expect(loadPaneRecoveryById('slot-two', 's-mine')).toBeUndefined()
    expect(loadPaneRecoveryById('slot-two', 's-theirs')?.text,
      'two tabs on one slot is a designed shape, and the sibling record has no other copy')
      .toBe('the other tabs prompt')
  })

  it('retires an unidentified record without reaching an identified sibling', () => {
    // The bare key is what a refusal restored without a send id writes.
    localStorage.setItem(`${PANE_RECOVERY_KEY}:pane:slot-mix`, JSON.stringify({
      v: { text: 'the unidentified one', files: [] }, ts: Date.now(),
    }))
    setPaneRecoveryFor('slot-mix', { text: 'a named send', files: [], sendId: 's-named' })

    clearUnidentifiedPaneRecovery('slot-mix')

    expect(localStorage.getItem(`${PANE_RECOVERY_KEY}:pane:slot-mix`)).toBeNull()
    expect(loadPaneRecoveryById('slot-mix', 's-named')?.text,
      'a single deterministic key cannot name a sibling tabs identified record')
      .toBe('a named send')
  })
})

describe('budgeting must not delete unresolved work', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  it('keeps every unresolved prompt past the entry cap', () => {
    // An intermittently-offline user accumulates recoveries. Each one is the ONLY durable copy of
    // a prompt whose composer was already cleared, so age is not a licence to delete it.
    const total = DRAFT_MAX_ENTRIES + 3
    for (let i = 0; i < total; i++) {
      setPaneRecoveryFor(`slot-${i}`, { text: `prompt number ${i}`, files: [], sendId: `s-${i}` })
    }
    const survivors = []
    for (let i = 0; i < total; i++) {
      const rec = loadPaneRecoveryById(`slot-${i}`, `s-${i}`)
      if (rec) survivors.push(rec.text)
    }
    expect(survivors.length,
      `all ${total} carry a prompt, so none may be evicted for budget`).toBe(total)
    expect(loadPaneRecoveryById('slot-0', 's-0')?.text,
      'the OLDEST is exactly the one an age-ordered eviction reached first').toBe('prompt number 0')
  })

  it('still evicts markers, which carry no prompt', () => {
    // The cap must remain load-bearing on bookkeeping, or the exemption becomes unbounded growth.
    for (let i = 0; i < DRAFT_MAX_ENTRIES + 3; i++) setStagedSend(`marker-${i}`, `s-${i}`)
    let live = 0
    for (let i = 0; i < DRAFT_MAX_ENTRIES + 3; i++) if (loadStagedSend(`marker-${i}`)) live++
    expect(live, 'markers name a send and hold no text, so the cap may still reclaim them')
      .toBeLessThanOrEqual(DRAFT_MAX_ENTRIES)
  })
})

describe('First Principles BLOCK at 3c73173da -- no migration for a format never shipped', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  it('does not read an unprefixed key as a slot record', () => {
    // No shipped build ever wrote this shape: a bare `<slot>` record key existed only inside this
    // change's own iterations. Matching it means any same-named key is adopted as pane recovery.
    localStorage.setItem(`${PANE_RECOVERY_KEY}:slot-bare`, JSON.stringify({
      v: { text: 'never written by any release', files: [], sendId: 's-bare' }, ts: Date.now(),
    }))
    expect(loadPaneRecovery('slot-bare'),
      'the unprefixed match arm is gone, so this key belongs to nothing').toBeUndefined()
  })

  it('still reads a properly keyed record', () => {
    // Positive control: the read path works, so the assertion above is about the KEY SHAPE and
    // not a broken accessor.
    setPaneRecoveryFor('slot-bare', { text: 'written by this build', files: [], sendId: 's-real' })
    expect(loadPaneRecovery('slot-bare')?.text).toBe('written by this build')
  })
})

describe('a refused write must not read as durable', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  it('reports false when the store refuses the record', () => {
    // Quota exhausted with nothing left to reclaim: `safeSetItem` answers false and NOTHING is
    // stored. Discarding that answer is what let a caller clear the only other copy.
    const realSet = localStorage.setItem.bind(localStorage)
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k.startsWith(`${PANE_RECOVERY_KEY}:`)) {
        throw new DOMException('exceeded the quota', 'QuotaExceededError')
      }
      realSet(k, v)
    })

    const landed = setPaneRecoveryFor('slot-refused', { text: 'the only copy', files: [], sendId: 's-refused' })
    spy.mockRestore()

    expect(landed, 'the write did not land, so it must not claim it did').toBe(false)
    expect(loadPaneRecoveryById('slot-refused', 's-refused'),
      'premise: nothing was stored, so the false is the truth').toBeUndefined()
  })

  it('reports true for a write that does land', () => {
    // Positive control: a zero-information `false` would satisfy the assertion above.
    expect(setPaneRecoveryFor('slot-ok', { text: 'stored fine', files: [], sendId: 's-ok' })).toBe(true)
    expect(loadPaneRecoveryById('slot-ok', 's-ok')?.text).toBe('stored fine')
  })
})

describe('a failed re-key must not delete the only durable prompt', () => {
  beforeEach(() => { __resetPaneRecoveryForTests() })

  // `loadPaneRecovery` is newest-for-slot, so after a landed adopt it reads the BOUND record and
  // says nothing about the bare key. These assertions are about the bare key itself.
  const bareRecord = (slot: string): { text?: string } | undefined => {
    const raw = localStorage.getItem(`${PANE_RECOVERY_KEY}:pane:${slot}`)
    return raw ? JSON.parse(raw).v : undefined
  }

  it('retains the bare record when the re-keyed write is refused', () => {
    // A refusal parks the prompt under the BARE slot key. This write must land: it is the premise.
    setPaneRecoveryFor('slot-adopt', { text: 'the only durable copy', files: [] })
    expect(bareRecord('slot-adopt')?.text,
      'premise: the bare record exists before the adopt').toBe('the only durable copy')

    // Now the origin is full and reclaim frees nothing, so the re-keyed write cannot land.
    const realSet = localStorage.setItem.bind(localStorage)
    const spy = vi.spyOn(localStorage, 'setItem').mockImplementation((k: string, v: string) => {
      if (k.startsWith(`${PANE_RECOVERY_KEY}:`)) {
        throw new DOMException('exceeded the quota', 'QuotaExceededError')
      }
      realSet(k, v)
    })
    const adopted = adoptPaneRecovery('slot-adopt', 's-adopt')
    spy.mockRestore()

    expect(bareRecord('slot-adopt')?.text,
      'the bare record is the ONLY copy of the prompt -- a failed re-key must not delete it')
      .toBe('the only durable copy')
    expect(adopted,
      'nothing was re-keyed, so the caller must not be told to look under the new id')
      .toBeUndefined()
  })

  it('re-keys and drops the bare record when the write does land', () => {
    // Positive control: an adopt hardwired to retain would satisfy the assertions above.
    setPaneRecoveryFor('slot-ok-adopt', { text: 'movable', files: [] })
    const adopted = adoptPaneRecovery('slot-ok-adopt', 's-ok-adopt')
    expect(adopted?.sendId).toBe('s-ok-adopt')
    expect(loadPaneRecoveryById('slot-ok-adopt', 's-ok-adopt')?.text).toBe('movable')
    expect(bareRecord('slot-ok-adopt'),
      'a landed re-key retires the bare record').toBeUndefined()
  })
})
