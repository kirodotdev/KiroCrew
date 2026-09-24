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

  describe('apply: stateVersion outranks seq', () => {
    it('applies a refold whose stateVersion rose even though its seq went back', () => {
      // The server accepts exactly this: a contributor that refolds from scratch
      // bumps stateVersion and starts its seq again
      // (ExternalProjectionStore.publish, by_state_version). Seq-wins alone
      // dropped the frame and left the obsolete card on screen.
      store.apply('a', 'demoapp/count', { n: 9 }, 40, undefined, 1)
      store.apply('a', 'demoapp/count', { n: 1 }, 2, undefined, 2)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
    })

    it('drops a frame whose stateVersion went backwards, whatever its seq', () => {
      store.apply('a', 'demoapp/count', { n: 1 }, 2, undefined, 2)
      store.apply('a', 'demoapp/count', { n: 9 }, 99, undefined, 1)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
    })

    it('still applies seq-wins at an equal stateVersion', () => {
      store.apply('a', 'demoapp/count', { n: 1 }, 5, undefined, 1)
      store.apply('a', 'demoapp/count', { n: 2 }, 4, undefined, 1)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
      store.apply('a', 'demoapp/count', { n: 3 }, 6, undefined, 1)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 3 })
    })

    it('removes a row on a deletion frame that carries no stateVersion', () => {
      // The server's deletion frame omits stateVersion (apps/teardown.py), so it
      // arrives here as 0, while any contributor that has ever refolded holds a
      // version above that. Asking the version before the teardown branch
      // therefore dropped every deletion and left the uninstalled app's card on
      // screen. The frame's max seq is what still guards it against a replay.
      const DELETION_SEQ = 2 ** 53 - 1
      store.apply('a', 'demoapp/count', { n: 1 }, 3, undefined, 4)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
      store.apply('a', 'demoapp/count', null, DELETION_SEQ)
      expect(store.get('a', 'demoapp/count')).toBeUndefined()
    })

    it('keeps a row when a deletion frame is older than the row it would remove', () => {
      store.apply('a', 'demoapp/count', { n: 1 }, 9, undefined, 0)
      store.apply('a', 'demoapp/count', null, 4)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
    })

    it('adopts a schema that arrives at the same position', () => {
      // A schema is not a value replay: it decides how the card renders, so
      // dropping it on seq-wins leaves the view drawn by a shape the contributor
      // has already replaced. The value and the position stay put.
      const first = { kind: 'badge' as const }
      const second = { kind: 'table' as const }
      store.apply('a', 'demoapp/count', { n: 1 }, 5, first, 1)
      expect(store.schemaOf('a', 'demoapp/count')).toEqual(first)
      store.apply('a', 'demoapp/count', { n: 9 }, 5, second, 1)
      expect(store.schemaOf('a', 'demoapp/count')).toEqual(second)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
    })
  })

  describe('reconcileContributed', () => {
    it('deletes a contributed row the authoritative baseline no longer carries', () => {
      // Teardown otherwise rests entirely on the one null-value frame §6 sends,
      // and a socket that drops at the wrong moment never delivers it -- the
      // card then outlived the app that published it.
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      store.apply('a', 'roster', { name: 'A' }, 1)
      const mark = store.mark()
      store.reconcileContributed('a', ['roster'], mark)
      expect(store.get('a', 'demoapp/count')).toBeUndefined()
      // A built-in key is not the contributed lifecycle's business.
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    })

    it('keeps a contributed row written after the mark, so a stale read cannot delete it', () => {
      // The baseline read is not instantaneous. A live frame that lands while it
      // is in flight is NEWER than the answer, so absence from that answer says
      // nothing about it.
      const mark = store.mark()
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      store.reconcileContributed('a', [], mark)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 1 })
    })

    it('notifies the key set when it drops a card', () => {
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      let hits = 0
      const unsub = store.contributedFace('a').subscribe(() => { hits += 1 })
      store.reconcileContributed('a', [], store.mark())
      unsub()
      expect(hits).toBe(1)
    })
  })

  describe('a withdrawal outranks a baseline that predates it', () => {
    // A roster read is not instantaneous. When a teardown frame deletes the card
    // while the answer is in flight, that answer still lists it -- and the row is
    // GONE, so higher-seq-wins has no held row to compare the stale value
    // against and seeded the withdrawn card straight back onto the screen.
    const DELETION_SEQ = 2 ** 53 - 1

    it('refuses a baseline key withdrawn after that baseline was requested', () => {
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      const mark = store.mark()
      store.apply('a', 'demoapp/count', null, DELETION_SEQ)
      expect(store.get('a', 'demoapp/count')).toBeUndefined()
      store.seed(
        'a',
        { roster: { name: 'A' }, 'demoapp/count': { n: 1 } },
        7,
        { 'demoapp/count': 1 },
        undefined,
        undefined,
        mark,
      )
      expect(store.get('a', 'demoapp/count')).toBeUndefined()
      // Only the withdrawn key is refused; the rest of the baseline still lands.
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    })

    it('keeps a marker for every key one teardown can withdraw', () => {
      // The record is sized against the SERVER's bound, not picked for comfort. An
      // app may hold MAX_PROJECTION_KEYS_PER_UNIT (200) keys on one unit and a
      // teardown withdraws all of them, so a cap below that evicts the OLDEST
      // markers during exactly the event the record exists to survive -- and a
      // roster read already in flight then restores those cards.
      const keys = Array.from({ length: 200 }, (_, i) => `demoapp/k${i}`)
      for (const key of keys) store.apply('a', key, { n: 1 }, 1, undefined, 1)
      const mark = store.mark()
      for (const key of keys) store.apply('a', key, null, DELETION_SEQ)
      for (const key of keys) expect(store.get('a', key)).toBeUndefined()

      // One stale baseline, requested before the teardown, listing every key.
      const values: { [k: string]: unknown } = {}
      const seqs: { [k: string]: number } = {}
      for (const key of keys) {
        values[key] = { n: 1 }
        seqs[key] = 1
      }
      store.seed('a', values, 7, seqs, undefined, undefined, mark)

      const resurrected = keys.filter((key) => store.get('a', key) !== undefined)
      expect(resurrected).toEqual([])
    })

    it('records a withdrawal for a key this browser never held', () => {
      // A cold cache holds no row for the key a teardown names: the tab opened
      // after that card was published, or the roster answer carrying it is still
      // in flight. That is the case the record is needed for MOST -- with no held
      // row there is nothing for higher-seq-wins to compare against, so the
      // in-flight answer is applied unguarded and the card returns.
      const mark = store.mark()
      expect(store.get('a', 'demoapp/count')).toBeUndefined()
      store.apply('a', 'demoapp/count', null, DELETION_SEQ)
      store.seed(
        'a',
        { roster: { name: 'A' }, 'demoapp/count': { n: 1 } },
        7,
        { 'demoapp/count': 1 },
        undefined,
        undefined,
        mark,
      )
      expect(store.get('a', 'demoapp/count')).toBeUndefined()
      // The rest of the baseline is untouched, so the refusal is scoped to the key.
      expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    })

    it('keeps a marker for every key a UNIT can hold, not one app share', () => {
      // The server charges MAX_PROJECTION_KEYS_PER_UNIT (200) PER APP, and one unit
      // legitimately carries rows from several contributors up to
      // MAX_PROJECTION_KEYS_PER_FILE (2000). A record sized to a single app's share
      // therefore evicts its oldest markers the moment a second contributor tears
      // down beside the first, which is an ordinary multi-app uninstall.
      const keys = [
        ...Array.from({ length: 200 }, (_, i) => `appone/k${i}`),
        ...Array.from({ length: 200 }, (_, i) => `apptwo/k${i}`),
      ]
      for (const key of keys) store.apply('a', key, { n: 1 }, 1, undefined, 1)
      const mark = store.mark()
      for (const key of keys) store.apply('a', key, null, DELETION_SEQ)

      const values: { [k: string]: unknown } = {}
      const seqs: { [k: string]: number } = {}
      for (const key of keys) {
        values[key] = { n: 1 }
        seqs[key] = 1
      }
      store.seed('a', values, 7, seqs, undefined, undefined, mark)

      const resurrected = keys.filter((key) => store.get('a', key) !== undefined)
      expect(resurrected).toEqual([])
    })

    it('believes a baseline requested after the withdrawal', () => {
      // CONTROL. The app is reinstalled and the next roster read legitimately
      // carries it. A record that outlived the reads it protects would hide the
      // new card for good.
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      store.apply('a', 'demoapp/count', null, DELETION_SEQ)
      const mark = store.mark()
      store.seed('a', { 'demoapp/count': { n: 2 } }, 7, { 'demoapp/count': 2 }, undefined, undefined, mark)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 2 })
    })

    it('lets a live frame republish a withdrawn key and retires the record', () => {
      // CONTROL. A live push lands after the withdrawal by construction, so it
      // is authoritative -- and retiring the record is what stops the NEXT
      // baseline refusing the card the contributor just republished.
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      const mark = store.mark()
      store.apply('a', 'demoapp/count', null, DELETION_SEQ)
      store.apply('a', 'demoapp/count', { n: 5 }, 2, undefined, 2)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 5 })
      store.seed('a', { 'demoapp/count': { n: 5 } }, 7, { 'demoapp/count': 2 }, undefined, undefined, mark)
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 5 })
    })

    it('seeds normally when no mark is offered', () => {
      // CONTROL. An omitted mark says "newer than anything held", which is what a
      // caller with no mark to offer is really claiming -- so the guard must not
      // silently start refusing keys for every such caller.
      store.apply('a', 'demoapp/count', { n: 1 }, 1, undefined, 1)
      store.apply('a', 'demoapp/count', null, DELETION_SEQ)
      store.seed('a', { 'demoapp/count': { n: 3 } }, 7, { 'demoapp/count': 3 })
      expect(store.get('a', 'demoapp/count')).toEqual({ n: 3 })
    })
  })

  describe('seed clears: a dropped contributed key bumps the keyset', () => {
    // A contributed card is rendered from a list recomputed on the keyset
    // version. The per-key face is not what the list reads, so a clear that
    // notifies only per-key leaves the card on screen with no value behind it.
    const version = (slug: string): unknown => store.contributedFace(slug).getSnapshot()

    it('bumps it when the refusal sentinel clears a contributed key', () => {
      store.apply('a', 'app/view', 'v', 5)
      const before = version('a')
      store.seed('a', {}, -1)
      expect(store.get('a', 'app/view')).toBeUndefined()
      expect(version('a')).not.toBe(before)
    })

    it('bumps it when an empty baseline clears a contributed key', () => {
      store.apply('a', 'app/view', 'v', 5)
      const before = version('a')
      store.seed('a', {}, 9)
      expect(store.get('a', 'app/view')).toBeUndefined()
      expect(version('a')).not.toBe(before)
    })

    // CONTROL: the bump is conditional on a CONTRIBUTED key going, not on the
    // clear happening. A member-log key carries no card, so bumping for it would
    // re-render every contributed list on every ordinary baseline.
    it('leaves it alone when only a member-log key is cleared', () => {
      store.apply('a', 'roster', 'v', 5)
      const before = version('a')
      store.seed('a', {}, 9)
      expect(store.get('a', 'roster')).toBeUndefined()
      expect(version('a')).toBe(before)
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

  describe('a same-seq schema change reaches the card', () => {
    it('notifies the contributed face, not just the key', () => {
      // A schema is what the card renders WITH, so a card already mounted has to be
      // told. Notifying only the key leaves it drawn by the shape the contributor has
      // just replaced -- the outcome adopting the schema at the same seq exists to
      // avoid.
      const store = new MemberProjectionStore()
      store.apply('a', 'demoapp/count', { n: 1 }, 5, { title: 'Old' })
      let keyset = 0
      const stop = store.contributedFace('a').subscribe(() => {
        keyset += 1
      })
      const before = keyset

      store.apply('a', 'demoapp/count', { n: 1 }, 5, { title: 'New' })

      expect(keyset).toBeGreaterThan(before)
      stop()
    })
  })
})
