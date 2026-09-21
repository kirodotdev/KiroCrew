import { describe, it, expect, beforeEach } from 'vitest'

import { MemberProjectionStore } from './memberProjectionStore'

describe('MemberProjectionStore', () => {
  let store: MemberProjectionStore

  beforeEach(() => {
    store = new MemberProjectionStore()
  })

  describe('apply: higher-seq-wins', () => {
    it('stores the first frame', () => {
      store.apply('a', 'roster', { name: 'A' }, 1)
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    })

    it('lets a higher seq overwrite', () => {
      store.apply('a', 'roster', { name: 'A' }, 1)
      store.apply('a', 'roster', { name: 'A2' }, 2)
      expect(store.get('a', 'roster')).toEqual({ name: 'A2' })
    })

    it('drops an equal seq (replay)', () => {
      store.apply('a', 'roster', { name: 'A' }, 5)
      store.apply('a', 'roster', { name: 'STALE' }, 5)
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    })

    it('drops a lower seq (out-of-order stale frame)', () => {
      store.apply('a', 'roster', { name: 'A' }, 5)
      store.apply('a', 'roster', { name: 'OLD' }, 3)
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    })

    it('notifies only when a frame actually lands', () => {
      let hits = 0
      store.faceOf('a', 'roster').subscribe(() => { hits += 1 })
      store.apply('a', 'roster', 1, 1) // lands
      store.apply('a', 'roster', 2, 1) // dropped (equal seq)
      expect(hits).toBe(1)
    })
  })

  describe('seed: never truncates', () => {
    it('applies each baseline value at asOfSeq', () => {
      store.seed('a', { roster: { name: 'A' }, wake: { patrol: 'none' } }, 4)
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
      expect(store.get('a', 'wake')).toEqual({ patrol: 'none' })
    })

    it('does not overwrite a live frame that raced ahead of the baseline', () => {
      store.apply('a', 'roster', { name: 'LIVE' }, 9)
      store.seed('a', { roster: { name: 'BASELINE' } }, 4)
      expect(store.get('a', 'roster')).toEqual({ name: 'LIVE' })
    })

    it('does not remove rows above asOfSeq (no truncation)', () => {
      store.apply('a', 'activity', { today: 3 }, 10)
      store.seed('a', { roster: { name: 'A' } }, 4)
      expect(store.get('a', 'activity')).toEqual({ today: 3 })
    })
  })

  describe('truncate: drops only seq > lastSeq and notifies', () => {
    it('drops rows above lastSeq, keeps those at or below', () => {
      store.apply('a', 'roster', { name: 'keep' }, 4)
      store.apply('a', 'activity', { today: 1 }, 5)
      store.apply('a', 'wake', { patrol: 'armed' }, 8)
      store.truncate('a', 5)
      expect(store.get('a', 'roster')).toEqual({ name: 'keep' })
      expect(store.get('a', 'activity')).toEqual({ today: 1 })
      expect(store.get('a', 'wake')).toBeUndefined()
    })

    it('notifies exactly the dropped rows', () => {
      store.apply('a', 'roster', 1, 4)
      store.apply('a', 'wake', 1, 8)
      let rosterHits = 0
      let wakeHits = 0
      store.faceOf('a', 'roster').subscribe(() => { rosterHits += 1 })
      store.faceOf('a', 'wake').subscribe(() => { wakeHits += 1 })
      store.truncate('a', 5)
      expect(rosterHits).toBe(0)
      expect(wakeHits).toBe(1)
    })

    it('is a no-op for an unknown slug', () => {
      expect(() => store.truncate('missing', 3)).not.toThrow()
    })

    it('never drops a contributed key: its seq is a different domain', () => {
      // A contributed row carries the contributor's own fold-position seq, not
      // the member-log asOfSeq that lastSeq bounds. Truncating it here would
      // drop a live contributed card on reconnect whenever its own seq exceeded
      // the member log's.
      store.apply('a', 'roster', { name: 'keep' }, 4)
      store.apply('a', 'demoapp/count', { n: 3 }, 99)
      store.truncate('a', 5)
      expect(store.get('a', 'roster')).toEqual({ name: 'keep' })
      // The contributed key with the high (foreign-domain) seq survives.
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 3 })
    })
  })

  describe('truncateAll', () => {
    it('truncates per slug and leaves absent slugs alone', () => {
      store.apply('a', 'wake', 1, 8)
      store.apply('b', 'wake', 1, 8)
      store.truncateAll({ a: 5 })
      expect(store.get('a', 'wake')).toBeUndefined()
      expect(store.get('b', 'wake')).toBe(1)
    })
  })

  describe('faceOf: referential stability', () => {
    it('returns the same reference until the row changes', () => {
      store.apply('a', 'roster', { name: 'A' }, 1)
      const face = store.faceOf('a', 'roster')
      const s1 = face.getSnapshot()
      const s2 = face.getSnapshot()
      expect(s1).toBe(s2)
      store.apply('a', 'roster', { name: 'A2' }, 2)
      const s3 = face.getSnapshot()
      expect(s3).not.toBe(s1)
    })

    it('returns undefined before any frame', () => {
      expect(store.faceOf('a', 'roster').getSnapshot()).toBeUndefined()
    })
  })

  describe('listener cleanup', () => {
    it('stops notifying after unsubscribe', () => {
      let hits = 0
      const unsub = store.faceOf('a', 'roster').subscribe(() => { hits += 1 })
      store.apply('a', 'roster', 1, 1)
      unsub()
      store.apply('a', 'roster', 2, 2)
      expect(hits).toBe(1)
    })
  })

  describe('has / clear', () => {
    it('reports whether a slug is held and clears everything', () => {
      store.apply('a', 'roster', 1, 1)
      expect(store.has('a')).toBe(true)
      store.clear()
      expect(store.has('a')).toBe(false)
      expect(store.get('a', 'roster')).toBeUndefined()
    })
  })
})

describe('a teardown is not undone by a baseline that predates it', () => {
  it('does not resurrect a key torn down before it was ever seeded', () => {
    const store = new MemberProjectionStore()

    // The roster response is already in flight; the teardown lands first, for a
    // key this client never held.
    store.apply('m', 'app/card', null, 7)

    // ...and the stale response arrives, carrying the key at its own older seq.
    store.seed('m', { 'app/card': { v: 1 } }, 5)

    expect(store.faceOf('m', 'app/card').getSnapshot()).toBeUndefined()
  })

  it('does not resurrect a key torn down after it was seeded', () => {
    const store = new MemberProjectionStore()
    store.seed('m', { 'app/card': { v: 1 } }, 3)
    store.apply('m', 'app/card', null, 7)

    store.seed('m', { 'app/card': { v: 1 } }, 5)

    expect(store.faceOf('m', 'app/card').getSnapshot()).toBeUndefined()
  })

  it('still lets a live re-enable win at its own lower seq', () => {
    const store = new MemberProjectionStore()
    store.apply('m', 'app/card', null, 7)

    // The contributor re-enables at ITS fold position, which is legitimately
    // below the teardown's seq. A tombstone must not block this -- that is why
    // the teardown removes the row instead of storing one.
    store.apply('m', 'app/card', { v: 2 }, 2)

    expect(store.faceOf('m', 'app/card').getSnapshot()).toEqual({ v: 2 })
  })

  it('accepts a baseline newer than the teardown', () => {
    const store = new MemberProjectionStore()
    store.apply('m', 'app/card', null, 7)

    store.seed('m', { 'app/card': { v: 3 } }, 9)

    expect(store.faceOf('m', 'app/card').getSnapshot()).toEqual({ v: 3 })
  })
})

describe('a schema arriving at the value\'s own seq is not lost', () => {
  it('merges a schema delivered on a separate equal-seq frame', () => {
    const store = new MemberProjectionStore()
    store.apply('m', 'app/card', { v: 1 }, 4)

    // The schema route re-pushes the same row at the same seq, carrying only
    // the schema. Higher-seq-wins alone drops it and the card renders fallback
    // content until a reload.
    store.apply('m', 'app/card', { v: 1 }, 4, { kind: 'table' } as never)

    expect(store.faceOf('m', 'app/card').getSnapshot()).toEqual({ v: 1 })
    expect(store.schemaOf?.('m', 'app/card') ?? store.rowSchema?.('m', 'app/card')).toBeDefined()
  })

  it('an equal-seq frame does not overwrite the value', () => {
    const store = new MemberProjectionStore()
    store.apply('m', 'app/card', { v: 1 }, 4)
    store.apply('m', 'app/card', { v: 999 }, 4, { kind: 'table' } as never)

    expect(store.faceOf('m', 'app/card').getSnapshot()).toEqual({ v: 1 })
  })
})

describe('tombstone overflow does not reopen the resurrection hole', () => {
  const overflowTheGuards = (store: MemberProjectionStore) => {
    // More concurrent teardowns than the map retains, so the earliest guards are
    // evicted. One app with 64 projections across 17 members is 1,088 frames.
    for (let i = 0; i < 1200; i++) store.apply('m', `app/card${i}`, null, 5000 + i)
  }

  it('refuses a baseline requested before a guard was evicted', () => {
    const store = new MemberProjectionStore()
    const generation = store.currentBaselineGeneration()

    overflowTheGuards(store)

    // Assembled before the eviction, so it cannot be shown not to carry a card
    // that has since been torn down.
    store.seed('m', { 'app/card0': { v: 1 } }, 10, undefined, undefined, generation)

    expect(store.faceOf('m', 'app/card0').getSnapshot()).toBeUndefined()
  })

  it('accepts a baseline requested after the evictions', () => {
    const store = new MemberProjectionStore()
    overflowTheGuards(store)
    const generation = store.currentBaselineGeneration()

    store.seed('m', { 'app/fresh': { v: 2 } }, 10, undefined, undefined, generation)

    expect(store.faceOf('m', 'app/fresh').getSnapshot()).toEqual({ v: 2 })
  })

  it('does not suppress a card whose seq merely sits below other teardowns', () => {
    const store = new MemberProjectionStore()
    overflowTheGuards(store)
    const generation = store.currentBaselineGeneration()

    // A low seq is not evidence about THIS key: a contributed key's seq is the
    // contributor's own fold position, so one key's number says nothing about
    // another's. A shared numeric threshold would refuse this card; being in the
    // current generation is what makes it trustworthy.
    store.seed('m', { 'other/untouched': { v: 3 } }, 1, undefined, undefined, generation)

    expect(store.faceOf('m', 'other/untouched').getSnapshot()).toEqual({ v: 3 })
  })
})

