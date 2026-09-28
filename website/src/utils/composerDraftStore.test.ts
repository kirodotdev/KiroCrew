import { afterEach, describe, expect, it, vi } from 'vitest'
import { composerDraftStoreFor } from './composerDraftStore'

describe('composerDraftStoreFor', () => {
  it('a held draft is masked from every instance over the key until its last hold is released, while the slot stays writable', () => {
    // The host has this passage's post in flight; a second box over the same
    // passage (the side panel's full-screen layer) must not restore the pending
    // text and post it twice...
    const a = composerDraftStoreFor('mc-test-draft:hold')
    const b = composerDraftStoreFor('mc-test-draft:hold')
    a.write('pending', 'beta', 6)
    a.hold('beta', 6, 'pending')
    expect(b.read('beta', 6)).toBeNull()
    expect(b.read('gamma', 11)).toBeNull()
    // Two posts outstanding with the same text (one box closed mid-flight, a
    // new box posted): the first settling does not lift the hold the second needs.
    b.hold('beta', 6, 'pending')
    a.release('beta', 6, 'pending')
    expect(b.read('beta', 6)).toBeNull()
    b.release('beta', 6, 'pending')
    // The pending text survived the hold untouched; a refused flight leaves it.
    expect(b.read('beta', 6)).toBe('pending')
    a.hold('beta', 6, 'pending'); a.release('beta', 6, 'pending'); a.clear('beta', 6)
    expect(b.read('beta', 6)).toBeNull()
  })

  it('a newer draft written over a held passage is kept, and the settling post clears only the text it posted', () => {
    // ...but what the second box TYPES is the user's newer draft: the hold
    // masks the pending text, it does not refuse writes. A success then clears
    // the slot only if it still holds the posted text; a refusal leaves the
    // slot alone either way — so the newer draft survives both.
    const key = 'mc-test-draft:newer'
    const a = composerDraftStoreFor(key)
    const b = composerDraftStoreFor(key)
    a.write('first post', 'beta', 6)
    a.hold('beta', 6, 'first post')
    expect(b.read('beta', 6)).toBeNull()
    b.write('second draft', 'beta', 6)
    // The newer text is not the pending one, so it reads back at once.
    expect(b.read('beta', 6)).toBe('second draft')
    expect(a.read('beta', 6)).toBe('second draft')
    // The first post succeeds: it clears "first post" — no longer there — and
    // must not take the newer draft with it.
    a.release('beta', 6, 'first post')
    a.clear('beta', 6, 'first post')
    expect(b.read('beta', 6)).toBe('second draft')
    expect(JSON.parse(window.sessionStorage.getItem(key) ?? '{}')).toEqual({ '6|beta': 'second draft' })
    // A conditional clear over the matching text does clear (and tombstones).
    b.clear('beta', 6, 'second draft')
    expect(b.read('beta', 6)).toBeNull()
    window.sessionStorage.setItem(key, JSON.stringify({ '6|beta': 'second draft' }))
    expect(b.read('beta', 6)).toBeNull()
  })

  it('isEmpty sees through a hold: a slot holding a newer draft whose own post is in flight is NOT empty', () => {
    // Two posts outstanding over one passage: "first" (box 1, closed mid-flight)
    // and "second" (box 2, typed over it and posted). `read` masks the held
    // "second" as null -- that is what keeps a third box from re-posting it --
    // but a refused "first" deciding whether to put its text back must not
    // mistake that null for an empty slot and overwrite the newer draft.
    const key = 'mc-test-draft:isEmpty'
    const a = composerDraftStoreFor(key)
    const b = composerDraftStoreFor(key)
    expect(a.isEmpty('beta', 6)).toBe(true)
    a.write('first', 'beta', 6)
    a.hold('beta', 6, 'first')
    b.write('second', 'beta', 6)
    b.hold('beta', 6, 'second')
    expect(a.read('beta', 6)).toBeNull()
    expect(a.isEmpty('beta', 6)).toBe(false)
    // First refuses: released, slot not empty -> nothing written back.
    a.release('beta', 6, 'first')
    expect(a.isEmpty('beta', 6)).toBe(false)
    // Second refuses: its own text is still there for its box / the next open.
    b.release('beta', 6, 'second')
    expect(b.read('beta', 6)).toBe('second')
    // A discarded draft (unconditional clear) is the empty case -- and a
    // tombstoned storage record does not un-empty it.
    b.clear('beta', 6)
    expect(b.isEmpty('beta', 6)).toBe(true)
    window.sessionStorage.setItem(key, JSON.stringify({ '6|beta': 'second' }))
    expect(b.isEmpty('beta', 6)).toBe(true)
  })

  it('a hold over a multi-line passage never masks a sibling passage\u2019s draft that happens to spell the same bytes', () => {
    // The anchor is the selected passage itself and may hold newlines: an
    // extended-downward selection gives `A` and `A\nB` at the same start. A
    // saved draft "B\nC" over `A` and an in-flight "C" over `A\nB` must stay
    // two different things — a delimiter-joined key would make them one and
    // the next keystroke would overwrite the saved draft.
    const key = 'mc-test-draft:multiline'
    const a = composerDraftStoreFor(key)
    a.write('B\nC', 'A', 5)
    a.hold('A\nB', 5, 'C')
    expect(a.read('A', 5)).toBe('B\nC')
    // ...and the real pending text IS masked.
    a.write('C', 'A\nB', 5)
    expect(a.read('A\nB', 5)).toBeNull()
    a.release('A\nB', 5, 'C')
    expect(a.read('A\nB', 5)).toBe('C')
    expect(a.read('A', 5)).toBe('B\nC')
  })

  it('a slot seeded in storage by an earlier page lifetime is still read, and a cleared sibling stays cleared', () => {
    // Storage seeds what this tab has not written; only this tab's own
    // deletions (tombstones) remove from it.
    const key = 'mc-test-draft:seeded'
    const store = composerDraftStoreFor(key)
    store.write('mine', 'beta', 6)
    window.sessionStorage.setItem(key, JSON.stringify({ '6|beta': 'mine', '11|gamma': 'from before' }))
    expect(store.read('gamma', 11)).toBe('from before')
    store.clear('beta', 6)
    expect(store.read('beta', 6)).toBeNull()
    expect(store.read('gamma', 11)).toBe('from before')
    expect(JSON.parse(window.sessionStorage.getItem(key) ?? '{}')).toEqual({ '11|gamma': 'from before' })
  })

  it('a clear whose storage write is refused still wins on the next read (memory is authoritative)', () => {
    // Two live slots (so the emptied-key removal path is not what saves us),
    // an earlier successful storage write, then storage refusing the update
    // that records the deletion: the stale record must not resurrect the
    // cleared slot — that would put an already-posted comment back as a draft.
    const store = composerDraftStoreFor('mc-test-draft:authority')
    store.write('one', 'alpha', 0)
    store.write('two', 'beta', 6)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceeded') })
    try {
      store.clear('alpha', 0)
      expect(store.read('alpha', 0)).toBeNull()
      expect(store.read('beta', 6)).toBe('two')
      // The storage record still carries the stale slot; a fresh store over
      // the same key in THIS tab keeps trusting memory.
      expect(JSON.parse(window.sessionStorage.getItem('mc-test-draft:authority') ?? '{}')['0|alpha']).toBe('one')
      expect(composerDraftStoreFor('mc-test-draft:authority').read('alpha', 0)).toBeNull()
    } finally { setItem.mockRestore() }
  })

  afterEach(() => { window.sessionStorage.clear() })

  it('keeps one draft per passage and clears only the one asked for', () => {
    const store = composerDraftStoreFor('mc-artifact-composer-draft:doc')
    store.write('about A', 'alpha', 0)
    store.write('about B', 'beta', 6)
    expect(store.read('alpha', 0)).toBe('about A')
    expect(store.read('beta', 6)).toBe('about B')
    // Same text elsewhere in the document is another passage.
    expect(store.read('alpha', 40)).toBeNull()
    store.clear('alpha', 0)
    expect(store.read('alpha', 0)).toBeNull()
    expect(store.read('beta', 6)).toBe('about B')
  })

  it('a second store for the same key sees the draft — the panel that wrote it is gone after a slot switch', () => {
    composerDraftStoreFor('mc-artifact-composer-draft:doc').write('half a thought', 'gamma', 12)
    expect(composerDraftStoreFor('mc-artifact-composer-draft:doc').read('gamma', 12)).toBe('half a thought')
    expect(composerDraftStoreFor('mc-artifact-composer-draft:other').read('gamma', 12)).toBeNull()
  })

  it('survives a refusing sessionStorage through the in-memory twin, and never throws', () => {
    const original = window.sessionStorage.setItem
    Object.defineProperty(window.sessionStorage, 'setItem', { configurable: true, value: () => { throw new Error('QuotaExceeded') } })
    try {
      const store = composerDraftStoreFor('mc-artifact-composer-draft:full')
      expect(() => store.write('kept', 'delta', 3)).not.toThrow()
      expect(store.read('delta', 3)).toBe('kept')
    } finally {
      Object.defineProperty(window.sessionStorage, 'setItem', { configurable: true, value: original })
    }
  })

  it('ignores a corrupt record instead of throwing', () => {
    window.sessionStorage.setItem('mc-artifact-composer-draft:bad', '{not json')
    expect(composerDraftStoreFor('mc-artifact-composer-draft:bad').read('x', 0)).toBeNull()
  })
})
