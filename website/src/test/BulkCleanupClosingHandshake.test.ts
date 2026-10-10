// GPT F1 at 817ee1aa9, upheld by Opus: the bulk cleanup's dirty filter is point-in-time,
// so a draft claimed inside the check -> archive interval was archived anyway.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetClosingIntentForTests,
  __setClosingAckWindowForTests,
  CLOSING_ACK_WINDOW_MS,
  INTENT_STALE_MS,
  PRESENCE_STALE_MS,
  anotherWindowHoldsComposer,
  awaitCleanupRefusals,
  publishComposerPresence,
  readClosingIntent,
  vetoClosingIntent,
} from '../utils/slotClosingIntent'

const NONE = () => false

describe('bulk cleanup asks every candidate before the server archives it', () => {
  beforeEach(() => {
    __resetClosingIntentForTests()
    __setClosingAckWindowForTests(5)
  })
  afterEach(() => {
    __setClosingAckWindowForTests(CLOSING_ACK_WINDOW_MS)
    __resetClosingIntentForTests()
  })

  it('lets a quiet batch through', async () => {
    expect((await awaitCleanupRefusals(['a', 'b'], new Set(), NONE)).refused).toEqual([])
  })

  it('names a slot that turned dirty AFTER the click-time filter', async () => {
    // The filter saw nothing; the claim lands while the batch waits. This is the archived-anyway case.
    const appeared = new Set<string>()
    const { refused } = await awaitCleanupRefusals(['a', 'b'], new Set(), k => {
      appeared.add(k)
      return k === 'b'
    })
    expect(refused).toEqual(['b'])
    expect(appeared.has('b')).toBe(true)
  })

  it('refuses the batch when a composer answers the published intent', async () => {
    const pending = awaitCleanupRefusals(['a', 'b'], new Set(), NONE)
    const intent = readClosingIntent('b')
    expect(intent).not.toBeNull()
    vetoClosingIntent(intent!.n, 'composer-other-window')
    expect((await pending).refused).toEqual(['b'])
  })

  it('still refuses a CONSENTED slot when a veto arrives during acknowledgement', async () => {
    // The consent covered the draft that existed at confirm time, not one typed since.
    const pending = awaitCleanupRefusals(['a', 'b'], new Set(['b']), NONE)
    const intent = readClosingIntent('b')
    expect(intent).not.toBeNull()
    vetoClosingIntent(intent!.n, 'composer-other-window')
    expect((await pending).refused).toEqual(['b'])
  })

  it('does NOT re-litigate a slot the user already accepted losing', async () => {
    // Otherwise the confirm is unanswerable: saying yes would still abort on the same draft.
    const { refused } = await awaitCleanupRefusals(['a', 'b'], new Set(['b']), k => k === 'b')
    expect(refused).toEqual([])
  })

  it('leaves no handshake key behind ONCE RELEASED, so no later close reads a phantom', async () => {
    const guard = await awaitCleanupRefusals(['a', 'b'], new Set(), NONE)
    guard.release()
    const left: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (key && key.startsWith('mc-slot-closing:')) left.push(key)
    }
    expect(left).toEqual([])
  })
})

describe('the ack window is only paid when another window could answer', () => {
  beforeEach(() => __resetClosingIntentForTests())
  afterEach(() => __resetClosingIntentForTests())

  it('reports nobody when this window is alone', () => {
    expect(anotherWindowHoldsComposer()).toBe(false)
  })

  it('does NOT count the composer this window itself holds', () => {
    const withdraw = publishComposerPresence('composer-mine')
    expect(anotherWindowHoldsComposer()).toBe(false)
    withdraw()
  })

  it('counts a presence key this window did not write', () => {
    localStorage.setItem('mc-slot-closing:present:composer-elsewhere', String(Date.now()))
    expect(anotherWindowHoldsComposer()).toBe(true)
  })

  it('withdraws presence on deregister, so a closed window stops charging the next close', () => {
    const withdraw = publishComposerPresence('composer-mine')
    withdraw()
    expect(localStorage.getItem('mc-slot-closing:present:composer-mine')).toBeNull()
  })

  it('fails CLOSED when storage cannot be enumerated', () => {
    const spy = vi.spyOn(Storage.prototype, 'key').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    localStorage.setItem('mc-slot-closing:present:x', '1')
    expect(anotherWindowHoldsComposer()).toBe(true)
    spy.mockRestore()
  })

  it('IGNORES a crashed window\u2019s stale stamp, so it stops charging every later close', () => {
    const old = Date.now() - (PRESENCE_STALE_MS + 1_000)
    localStorage.setItem('mc-slot-closing:present:composer-crashed', String(old))
    expect(anotherWindowHoldsComposer()).toBe(false)
  })

  it('still counts a FRESH stamp from another window', () => {
    localStorage.setItem('mc-slot-closing:present:composer-live', String(Date.now()))
    expect(anotherWindowHoldsComposer()).toBe(true)
  })

  it('treats an unparseable stamp as live, failing closed', () => {
    localStorage.setItem('mc-slot-closing:present:composer-weird', 'not-a-number')
    expect(anotherWindowHoldsComposer()).toBe(true)
  })

  it('stamps a RECENT time rather than a constant, which is what makes pruning possible', () => {
    const before = Date.now()
    const withdraw = publishComposerPresence('composer-stamped')
    const stamp = Number(localStorage.getItem('mc-slot-closing:present:composer-stamped'))
    // A constant would parse as a finite number yet read as ancient, disabling the handshake.
    expect(stamp).toBeGreaterThanOrEqual(before - 1_000)
    expect(Date.now() - stamp).toBeLessThan(PRESENCE_STALE_MS)
    withdraw()
  })

  it('KEEPS the intent published after it resolves, so the request itself is guarded', async () => {
    // The defect: the intent was torn down before the DELETE was even sent, so a draft typed
    // during the round-trip met no intent, was never claimed, and was archived silently.
    const guard = await awaitCleanupRefusals(['a', 'b'], new Set(), NONE)
    expect(guard.refused).toEqual([])
    expect(readClosingIntent('a')).not.toBeNull()
    expect(readClosingIntent('b')).not.toBeNull()
    guard.release()
    expect(readClosingIntent('a')).toBeNull()
  })

  it('releases idempotently, so a settle after an abort cannot throw', async () => {
    const guard = await awaitCleanupRefusals(['a'], new Set(), NONE)
    guard.release()
    expect(() => guard.release()).not.toThrow()
    expect(readClosingIntent('a')).toBeNull()
  })

  it('ignores an ABANDONED intent, so a caller killed mid-request cannot poison later closes',
    () => {
      const stale = Date.now() - (INTENT_STALE_MS + 1_000)
      localStorage.setItem('mc-slot-closing:intent:orphan', JSON.stringify({ n: 'x', t: stale }))
      expect(readClosingIntent('orphan')).toBeNull()
      localStorage.setItem('mc-slot-closing:intent:fresh',
        JSON.stringify({ n: 'y', t: Date.now() }))
      expect(readClosingIntent('fresh')).not.toBeNull()
    })

  it('RECHECKS the vetoes at commit time, after the ack window has closed', async () => {
    const guard = await awaitCleanupRefusals(['a', 'b'], new Set(), NONE)
    expect(guard.refused).toEqual([])
    // The composer wakes here -- after `refused` was computed, before the request goes.
    const intent = readClosingIntent('b')
    expect(intent).not.toBeNull()
    vetoClosingIntent(intent!.n, 'composer-late')
    expect(guard.recheck()).toEqual(['b'])
    guard.release()
  })

  it('recheck answers empty while nothing has refused, so a quiet batch still commits',
    async () => {
      const guard = await awaitCleanupRefusals(['a', 'b'], new Set(), NONE)
      expect(guard.recheck()).toEqual([])
      guard.release()
    })

  it('recheck survives a CONSENTED slot, because consent cannot cover later work',
    async () => {
      const guard = await awaitCleanupRefusals(['a', 'b'], new Set(['b']), NONE)
      const intent = readClosingIntent('b')
      vetoClosingIntent(intent!.n, 'composer-late')
      expect(guard.recheck()).toEqual(['b'])
      guard.release()
    })

  it('refuses ONLY the key whose intent write failed, and lets the rest archive', async () => {
    const real = Storage.prototype.setItem
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(
      function (this: Storage, key: string, value: string) {
        if (key.includes('intent:b')) throw new Error('storage disabled')
        return real.call(this, key, value)
      })
    const guard = await awaitCleanupRefusals(['a', 'b', 'c'], new Set(), NONE)
    setItem.mockRestore()
    // One unaskable key costs its own slot, not the batch.
    expect(guard.refused).toEqual(['b'])
    guard.release()
  })

  it('refuses EVERY key when storage rejects every intent write', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    const guard = await awaitCleanupRefusals(['a', 'b'], new Set(), NONE)
    setItem.mockRestore()
    // Nothing to WAIT for is not the same as nothing refused: none could be asked about.
    expect(guard.refused).toEqual(['a', 'b'])
    guard.release()
  })
})
