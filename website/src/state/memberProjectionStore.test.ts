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
