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

  describe('seed: applies at asOfSeq, and an empty block clears', () => {
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

    it('an empty block drops what is cached, so a collision cannot show another member', () => {
      // The roster returns an empty block for a slug it refuses to attribute,
      // which is what a shared-slug collision produces. Whatever is cached under
      // that slug belongs to whichever member won the cache first.
      store.apply('a', 'roster', { name: 'OTHER MEMBER' }, 2)
      store.seed('a', {}, 4)
      expect(store.get('a', 'roster')).toBeUndefined()
    })

    it('an empty block still keeps a row a live frame carried past asOfSeq', () => {
      // CONTROL. Without this, clearing the slug unconditionally would satisfy the
      // test above while throwing away a value newer than the baseline.
      store.apply('a', 'roster', { name: 'LIVE' }, 9)
      store.seed('a', {}, 4)
      expect(store.get('a', 'roster')).toEqual({ name: 'LIVE' })
    })

    it('an empty block notifies the rows it drops', () => {
      // A silent drop leaves the rendered card showing the value it just lost.
      store.apply('a', 'roster', { name: 'OTHER MEMBER' }, 2)
      let hits = 0
      const face = store.faceOf('a', 'roster')
      const stop = face.subscribe(() => {
        hits += 1
      })
      store.seed('a', {}, 4)
      stop()
      expect(hits).toBe(1)
    })
  })

  describe("seed: asOfSeq -1 is the server's refusal to attribute, not a sequence", () => {
    // The tests above seed at asOfSeq 4, a real position. The roster NEVER sends
    // that with an empty block: it sends -1, for a shared slug, a header naming
    // another member, or a failed read. Every real row's seq is above -1, so a
    // higher-seq-wins comparison against it keeps the entire cache -- the clear
    // fails on exactly the case it exists for.
    const UNATTRIBUTABLE = -1

    it("drops another member's cached projection", () => {
      store.apply('shared', 'roster', { name: 'MEMBER A' }, 7)
      store.apply('shared', 'wake', { patrol: 'running' }, 8)
      store.seed('shared', {}, UNATTRIBUTABLE)
      expect(store.get('shared', 'roster')).toBeUndefined()
      expect(store.get('shared', 'wake')).toBeUndefined()
      expect(store.has('shared')).toBe(false)
    })

    it('refuses the live frames that arrive afterwards', () => {
      // The roster is the only surface that checks attribution. The live publish
      // and the on-connect replay both read a snapshot straight out of the
      // service, so a frame for a refused slug still arrives -- and without this
      // it would re-populate the card the seed just cleared.
      store.seed('shared', {}, UNATTRIBUTABLE)
      store.apply('shared', 'roster', { name: 'MEMBER A' }, 9)
      expect(store.get('shared', 'roster')).toBeUndefined()
    })

    it('notifies each row it drops', () => {
      store.apply('shared', 'roster', { name: 'MEMBER A' }, 7)
      let hits = 0
      const stop = store.faceOf('shared', 'roster').subscribe(() => {
        hits += 1
      })
      store.seed('shared', {}, UNATTRIBUTABLE)
      stop()
      expect(hits).toBe(1)
    })

    it('lets an attributable baseline lift the refusal', () => {
      // CONTROL. Without this, marking a slug refused forever would satisfy every
      // test above while a renamed member's own projection never rendered again.
      store.seed('shared', {}, UNATTRIBUTABLE)
      store.seed('shared', { roster: { name: 'RENAMED' } }, 3)
      expect(store.get('shared', 'roster')).toEqual({ name: 'RENAMED' })
      store.apply('shared', 'wake', { patrol: 'none' }, 4)
      expect(store.get('shared', 'wake')).toEqual({ patrol: 'none' })
    })

    it('still keeps a raced row when the baseline IS attributable', () => {
      // CONTROL for the other direction: the unconditional clear must be reached
      // only by the sentinel, or an empty-but-real baseline would start throwing
      // away a live frame that legitimately raced past it.
      store.apply('a', 'roster', { name: 'LIVE' }, 9)
      store.seed('a', {}, 0)
      expect(store.get('a', 'roster')).toEqual({ name: 'LIVE' })
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

  describe('truncation reports what it dropped', () => {
    it('answers true when a row above the server seq is dropped', () => {
      // Dropping is correct -- that row records something that did not happen --
      // but it leaves the card blank, and only a refetch can supply the value the
      // server actually holds. The caller cannot know to refetch unless told.
      store.apply('a', 'roster', { name: 'ROLLED BACK' }, 10)
      expect(store.truncate('a', 9)).toBe(true)
      expect(store.get('a', 'roster')).toBeUndefined()
    })

    it('answers false when nothing was above it', () => {
      // CONTROL. Without this, answering true unconditionally would satisfy the
      // test above while refetching the whole roster on every connection.
      store.apply('a', 'roster', { name: 'FINE' }, 9)
      expect(store.truncate('a', 9)).toBe(false)
      expect(store.get('a', 'roster')).toEqual({ name: 'FINE' })
    })

    it('truncateAll answers true when an omitted slug is dropped', () => {
      store.apply('gone', 'roster', { name: 'GONE' }, 3)
      expect(store.truncateAll({ a: 9 })).toBe(true)
    })

    it('truncateAll answers false when the frame agrees with the cache', () => {
      // CONTROL for the whole-frame path, same reasoning as above.
      store.apply('a', 'roster', { name: 'FINE' }, 9)
      expect(store.truncateAll({ a: 9 })).toBe(false)
    })
  })

  describe('truncateAll', () => {
    it('truncates per slug against the baseline', () => {
      store.apply('a', 'wake', 1, 8)
      store.truncateAll({ a: 5 })
      expect(store.get('a', 'wake')).toBeUndefined()
    })

    it('drops a cached slug the baseline omits', () => {
      // This assertion replaces one that required an absent slug to be left
      // alone. A members_subscribed frame carries every slug the server holds a
      // readable log for, so an omitted slug is one it cannot serve -- a damaged
      // header, or a member that is gone. Keeping its rows lets them override the
      // empty roster baseline, and the page then shows pre-restart state with
      // nothing to distinguish it from a live read.
      store.apply('a', 'wake', 1, 8)
      store.apply('b', 'wake', 1, 8)
      store.truncateAll({ a: 9 })
      expect(store.get('a', 'wake')).toBe(1)
      expect(store.get('b', 'wake')).toBeUndefined()
    })

    it('notifies a listener when its slug is dropped', () => {
      store.apply('b', 'wake', 1, 8)
      let fired = 0
      const stop = store.faceOf('b', 'wake').subscribe(() => {
        fired += 1
      })
      store.truncateAll({ a: 1 })
      stop()
      expect(fired).toBeGreaterThan(0)
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
