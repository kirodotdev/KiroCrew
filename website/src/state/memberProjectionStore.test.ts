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
      const issued = store.revision()
      store.apply('a', 'activity', { today: 3 }, 10)
      store.seed('a', { roster: { name: 'A' } }, 4, {}, {}, issued)
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
      // test above while throwing away a value newer than the baseline. Newer is
      // measured on the store's revision: the read is issued, then the frame
      // lands, so the frame is the later statement about the key.
      const issued = store.revision()
      store.apply('a', 'roster', { name: 'LIVE' }, 9)
      store.seed('a', {}, 4, {}, {}, issued)
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
      const issued = store.revision()
      store.apply('a', 'roster', { name: 'LIVE' }, 9)
      store.seed('a', {}, 0, {}, {}, issued)
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

  describe('deletion: ordered by generation, and it leaves nothing behind', () => {
    it('a deletion at a newer generation beats a row sitting at a huge seq', () => {
      const s = new MemberProjectionStore()
      // A contributor folding at a large position -- e.g. one using a nanosecond
      // clock as its seq. Under plain higher-seq-wins no deletion could ever
      // outrank this.
      s.apply('alice', 'demo/card', { n: 1 }, 1e15, 3)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 1 })

      // The deletion carries an ORDINARY seq and the next generation.
      s.apply('alice', 'demo/card', null, 7, 4)
      expect(s.get('alice', 'demo/card')).toBeUndefined()
    })

    it('a deletion retains NO generation, which is what lets a re-enable win', () => {
      const s = new MemberProjectionStore()
      s.apply('alice', 'demo/card', { n: 1 }, 5, 3)
      s.apply('alice', 'demo/card', null, 5, 4)

      // Nothing is held for the key, so nothing can outrank what comes next. A
      // tombstone at generation 4 would be the mirror defect: the app picks its
      // own stateVersion and cannot know the server advanced to 4, so its real
      // updates would be discarded until a reload.
      expect(s.has('alice')).toBe(false)

      // Deliberately NOT pinned here: refusing a publish from the retired
      // generation. Teardown revokes the grant BEFORE deleting rows
      // (delete_contribution_rows requires it) and the publish path commits
      // behind assert_grants_unchanged, so the server does not send that frame.
      // The fence is pinned in test/test_contrib_fence.py.
    })

    it('a re-enabled app publishing at ANY generation is not suppressed', () => {
      const s = new MemberProjectionStore()
      s.apply('alice', 'demo/card', { n: 1 }, 5, 3)
      s.apply('alice', 'demo/card', null, 5, 4)

      // The app cannot know the server advanced to 4 -- it supplies its own
      // version, and after a re-enable that may be anything, including 1. No
      // tombstone is held, so there is nothing for it to lose against.
      s.apply('alice', 'demo/card', { n: 2 }, 1, 1)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 2 })
    })

    it('within one generation the seq still decides, so replays still drop', () => {
      const s = new MemberProjectionStore()
      s.apply('alice', 'demo/card', { n: 1 }, 5, 3)
      s.apply('alice', 'demo/card', { n: 2 }, 5, 3)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 1 })
      s.apply('alice', 'demo/card', { n: 3 }, 6, 3)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 3 })
    })

    it('a deletion notifies the face, so the card actually clears', () => {
      const s = new MemberProjectionStore()
      s.apply('alice', 'demo/card', { n: 1 }, 5, 3)
      const face = s.faceOf('alice', 'demo/card')
      let notified = 0
      const stop = face.subscribe(() => {
        notified += 1
      })
      s.apply('alice', 'demo/card', null, 5, 4)
      stop()
      expect(notified).toBe(1)
      expect(face.getSnapshot()).toBeUndefined()
    })
  })

  describe("a contributed row seeds at its own seq, not the response's", () => {
    it('a live push BELOW asOfSeq still lands after a roster seed', () => {
      const s = new MemberProjectionStore()
      // The roster read is at asOfSeq 12 while this contributor has only folded to
      // 7. Seeding the row at 12 is what froze the card: the app's next push carries
      // ITS seq, which is below 12, so the gate dropped it.
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, { 'demo/card': 2 }, { 'demo/card': 7 })
      expect(s.get('alice', 'demo/card')).toEqual({ n: 1 })

      // The contributor's next fold: same generation, a seq above its own 7 but
      // still below the response's 12.
      s.apply('alice', 'demo/card', { n: 2 }, 8, 2)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 2 })
    })

    it('a built-in key with no entry still seeds at asOfSeq', () => {
      const s = new MemberProjectionStore()
      s.seed('alice', { roster: { name: 'A' } }, 12)
      // For a built-in key asOfSeq IS the row's seq, so the fallback must not weaken
      // the ordering that key already had.
      s.apply('alice', 'roster', { name: 'STALE' }, 11)
      expect(s.get('alice', 'roster')).toEqual({ name: 'A' })
    })

    it("a replay at or below the row's own seq is still dropped", () => {
      const s = new MemberProjectionStore()
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, { 'demo/card': 2 }, { 'demo/card': 7 })
      // Seeding lower must not turn into accepting anything: 7 is the bar now, and a
      // replay at 7 loses to it.
      s.apply('alice', 'demo/card', { n: 99 }, 7, 2)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 1 })
    })

    it('the generation still outranks the seq when both maps are present', () => {
      const s = new MemberProjectionStore()
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, { 'demo/card': 2 }, { 'demo/card': 7 })
      // A newer generation wins even at a seq below the seeded 7 -- the two maps must
      // not collapse into one comparison.
      s.apply('alice', 'demo/card', { n: 2 }, 1, 3)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 2 })
    })
  })

  describe('a non-empty baseline is authoritative about what it omits', () => {
    it('drops a contributed key the baseline no longer carries', () => {
      const s = new MemberProjectionStore()
      s.seed(
        'alice',
        { roster: { name: 'A' }, 'demo/card': { n: 1 } },
        12,
        { 'demo/card': 2 },
        { 'demo/card': 7 },
      )
      expect(s.get('alice', 'demo/card')).toEqual({ n: 1 })

      // The app was uninstalled and its row deleted, so the next roster read
      // carries the built-in keys and no contributed one. An empty block already
      // truncates at asOfSeq; a block that still holds built-ins must reach the
      // same verdict about the key it dropped, or the card outlives the app.
      s.seed('alice', { roster: { name: 'A' } }, 20)
      expect(s.get('alice', 'demo/card')).toBeUndefined()
    })

    it('notifies the dropped key so the card actually clears', () => {
      const s = new MemberProjectionStore()
      s.seed('alice', { roster: { name: 'A' }, 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      let hits = 0
      s.faceOf('alice', 'demo/card').subscribe(() => {
        hits += 1
      })

      s.seed('alice', { roster: { name: 'A' } }, 20)
      expect(hits).toBe(1)
      expect(s.faceOf('alice', 'demo/card').getSnapshot()).toBeUndefined()
    })

    it('keeps a row a live frame carried PAST the baseline', () => {
      const s = new MemberProjectionStore()
      s.seed('alice', { roster: { name: 'A' }, 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      // A push that landed after the roster read was taken. Removing it would
      // discard a value newer than the baseline that omits it. Newer is measured
      // on the store's own revision, which is the one axis a baseline and a frame
      // can both be placed on -- a contributed row's seq belongs to its app.
      const issued = s.revision()
      s.apply('alice', 'demo/card', { n: 2 }, 25)
      s.seed('alice', { roster: { name: 'A' } }, 20, {}, {}, issued)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 2 })
    })

    it('keeps a contributed row newer than the read even though its seq trails asOfSeq', () => {
      // The case the two counters differ on, and the reason the revision is the
      // only axis that answers: a contributed row's seq is its app's fold
      // position, which TRAILS the response's asOfSeq. Here 8 is below 20 and
      // still newer than this read, so a seq-versus-asOfSeq bound evicts exactly
      // the row it exists to protect.
      const s = new MemberProjectionStore()
      s.seed('alice', { roster: { name: 'A' }, 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      const issued = s.revision()
      s.apply('alice', 'demo/card', { n: 2 }, 8)
      s.seed('alice', { roster: { name: 'A' } }, 20, {}, {}, issued)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 2 })
    })
  })

  describe('a baseline older than a deletion cannot put the row back', () => {
    it('a read issued before the deletion leaves the key gone', () => {
      const s = new MemberProjectionStore()
      s.seed('alice', { roster: { name: 'A' }, 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      // The roster read is issued HERE and is still in flight.
      const issued = s.revision()
      // The app deletes the key while that read is on the wire. A deletion keeps
      // no row, so the answer below has nothing to lose a comparison against.
      s.apply('alice', 'demo/card', null, 8)
      expect(s.get('alice', 'demo/card')).toBeUndefined()

      // The read answers, still carrying the value it saw before the deletion.
      s.seed(
        'alice',
        { roster: { name: 'A' }, 'demo/card': { n: 1 } },
        12,
        {},
        { 'demo/card': 7 },
        issued,
      )
      expect(s.get('alice', 'demo/card')).toBeUndefined()
    })

    it('a read issued after the deletion does carry the key back', () => {
      // CONTROL for the other direction: a deletion must not become permanent, or
      // a re-enabled app's key would never render again from a roster read.
      const s = new MemberProjectionStore()
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      s.apply('alice', 'demo/card', null, 8)
      const issued = s.revision()
      s.seed('alice', { 'demo/card': { n: 5 } }, 12, {}, { 'demo/card': 9 }, issued)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 5 })
    })

    it('a truncation is protected the same way a deletion is', () => {
      // truncateAll drops a row that ran ahead of the server's own cursor. A
      // baseline older than that drop would restore exactly what was rolled back.
      const s = new MemberProjectionStore()
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      const issued = s.revision()
      expect(s.truncateAll({ alice: 3 })).toBe(true)
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 }, issued)
      expect(s.get('alice', 'demo/card')).toBeUndefined()
    })

    it("a drop never refuses the app's own later publish", () => {
      // CONTROL. The record of a drop is read by the baseline path alone. A live
      // publish is the app speaking now, so it always lands -- otherwise a
      // re-enabled app would be silenced by the teardown that removed its row.
      const s = new MemberProjectionStore()
      s.seed('alice', { 'demo/card': { n: 1 } }, 12, {}, { 'demo/card': 7 })
      s.apply('alice', 'demo/card', null, 8)
      s.apply('alice', 'demo/card', { n: 3 }, 9)
      expect(s.get('alice', 'demo/card')).toEqual({ n: 3 })
    })
  })
})
