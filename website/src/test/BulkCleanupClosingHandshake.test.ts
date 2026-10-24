// GPT F1 at 817ee1aa9, upheld by Opus: the bulk cleanup's dirty filter is point-in-time,
// so a draft claimed inside the check -> archive interval was archived anyway.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetClosingIntentForTests,
  __setClosingAckWindowForTests,
  CLOSING_ACK_WINDOW_MS,
  INTENT_STALE_MS,
  PRESENCE_STALE_MS,
  VETO_PROBE_TTL_MS,
  anotherWindowHoldsComposer,
  awaitCleanupRefusals,
  publishComposerPresence,
  readClosingIntent,
  vetoClosingIntent,
  vetoLiveClosingIntent,
} from '../utils/slotClosingIntent'

const NONE = () => false
const PINNED = new Date('2026-01-01T00:00:00.000Z')
const PINNED_MS = PINNED.getTime()

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
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PINNED)
    __resetClosingIntentForTests()
  })
  afterEach(() => {
    __resetClosingIntentForTests()
    vi.useRealTimers()
  })

  it('reports nobody when this window is alone', () => {
    expect(anotherWindowHoldsComposer()).toBe(false)
  })

  it('does NOT count the composer this window itself holds', () => {
    const withdraw = publishComposerPresence('composer-mine')
    expect(anotherWindowHoldsComposer()).toBe(false)
    withdraw()
  })

  it('counts a presence key this window did not write', () => {
    localStorage.setItem('mc-slot-closing:present:composer-elsewhere', String(PINNED_MS))
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
    const old = PINNED_MS - (PRESENCE_STALE_MS + 1_000)
    localStorage.setItem('mc-slot-closing:present:composer-crashed', String(old))
    expect(anotherWindowHoldsComposer()).toBe(false)
  })

  it('still counts a FRESH stamp from another window', () => {
    localStorage.setItem('mc-slot-closing:present:composer-live', String(PINNED_MS))
    expect(anotherWindowHoldsComposer()).toBe(true)
  })

  it('treats an unparseable stamp as live, failing closed', () => {
    localStorage.setItem('mc-slot-closing:present:composer-weird', 'not-a-number')
    expect(anotherWindowHoldsComposer()).toBe(true)
  })

  it('stamps a RECENT time rather than a constant, which is what makes pruning possible', () => {
    const withdraw = publishComposerPresence('composer-stamped')
    const stamp = Number(localStorage.getItem('mc-slot-closing:present:composer-stamped'))
    // A constant would parse as a finite number yet read as ancient, disabling the handshake.
    expect(stamp).toBe(PINNED_MS)
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
      const stale = PINNED_MS - (INTENT_STALE_MS + 1_000)
      localStorage.setItem('mc-slot-closing:intent:orphan:x', JSON.stringify({ n: 'x', t: stale }))
      expect(readClosingIntent('orphan')).toBeNull()
      localStorage.setItem('mc-slot-closing:intent:fresh:y',
        JSON.stringify({ n: 'y', t: PINNED_MS }))
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

describe('two windows closing the SAME slot do not answer for each other', () => {
  beforeEach(() => {
    __resetClosingIntentForTests()
    __setClosingAckWindowForTests(5)
  })
  afterEach(() => {
    __setClosingAckWindowForTests(CLOSING_ACK_WINDOW_MS)
    __resetClosingIntentForTests()
  })

  it('refuses BOTH closes when one composer turns dirty', async () => {
    // A slot-keyed intent held only the last nonce, so the earlier closer asked under a
    // nonce nothing could answer and deleted the draft while the later one was refused.
    const first = await awaitCleanupRefusals(['s'], new Set(), NONE)
    const second = await awaitCleanupRefusals(['s'], new Set(), NONE)
    vetoLiveClosingIntent('s', 'composer-other-window')
    expect(first.recheck()).toEqual(['s'])
    expect(second.recheck()).toEqual(['s'])
    first.release()
    second.release()
  })

  it('leaves the other closer answerable after one of them settles', async () => {
    const first = await awaitCleanupRefusals(['s'], new Set(), NONE)
    const second = await awaitCleanupRefusals(['s'], new Set(), NONE)
    // The first request settles and tears down. A slot-wide removal took the second
    // window's live intent with it, so the keystroke below met nothing to answer.
    first.release()
    expect(readClosingIntent('s')).not.toBeNull()
    vetoLiveClosingIntent('s', 'composer-late')
    expect(second.recheck()).toEqual(['s'])
    second.release()
  })

  it('keeps one slot\u2019s teardown out of another slot\u2019s intents', async () => {
    const guard = await awaitCleanupRefusals(['s', 's:nested'], new Set(), NONE)
    vetoLiveClosingIntent('s:nested', 'composer-other-window')
    // Encoding the slot is what keeps one scan prefix out of the other's keys.
    expect(guard.recheck()).toEqual(['s:nested'])
    guard.release()
  })
})

describe('a veto that cannot be written is a refusal, not silence', () => {
  beforeEach(() => {
    __resetClosingIntentForTests()
    __setClosingAckWindowForTests(5)
  })
  afterEach(() => {
    __setClosingAckWindowForTests(CLOSING_ACK_WINDOW_MS)
    __resetClosingIntentForTests()
    vi.useRealTimers()
  })

  const failVetoWrites = (match: (key: string) => boolean) => {
    const real = Storage.prototype.setItem
    return vi.spyOn(Storage.prototype, 'setItem').mockImplementation(
      function (this: Storage, key: string, value: string) {
        if (match(key)) throw new Error('quota exceeded')
        return real.call(this, key, value)
      })
  }

  it('refuses an ALREADY-CONSENTED slot whose veto write failed', async () => {
    // The destructive case: consent waives the dirty read, so the veto is the only thing
    // left protecting work typed since. Discarding its failure lost that work silently.
    const guard = await awaitCleanupRefusals(['s'], new Set(['s']), NONE)
    const intent = readClosingIntent('s')
    expect(intent).not.toBeNull()
    const spy = failVetoWrites(key => key.includes('veto'))
    expect(vetoClosingIntent(intent!.n, 'composer-other-window')).toBe(false)
    spy.mockRestore()
    expect(guard.recheck()).toEqual(['s'])
    guard.release()
  })

  it('records the refusal where the CLOSING window reads it', async () => {
    // The window that must not delete is a different one from the window that failed to
    // write, so the evidence has to live in the storage they share.
    const guard = await awaitCleanupRefusals(['s'], new Set(['s']), NONE)
    const intent = readClosingIntent('s')
    const spy = failVetoWrites(key => key.includes('veto:'))
    expect(vetoClosingIntent(intent!.n, 'composer-other-window')).toBe(false)
    spy.mockRestore()
    expect(localStorage.getItem(`mc-slot-closing:veto-fail:${intent!.n}`)).not.toBeNull()
    expect(guard.recheck()).toEqual(['s'])
    guard.release()
  })

  it('reports an unwritable store as refused, since no veto could have landed', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PINNED)
    const guard = await awaitCleanupRefusals(['s'], new Set(['s']), NONE)
    expect(guard.recheck()).toEqual([])
    // Past the probe memo, so the read asks storage again rather than reusing a verdict
    // taken while it was healthy. Well inside INTENT_STALE_MS, so the intent is still live.
    vi.setSystemTime(new Date(PINNED_MS + VETO_PROBE_TTL_MS + 1))
    const spy = failVetoWrites(() => true)
    // Nobody vetoed here; the point is that nobody COULD, which reads identically from
    // the closing window and is the reading that destroys a draft.
    expect(guard.recheck()).toEqual(['s'])
    spy.mockRestore()
    guard.release()
  })

  it('still lets a quiet batch through once storage recovers', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PINNED)
    const guard = await awaitCleanupRefusals(['s'], new Set(['s']), NONE)
    vi.setSystemTime(new Date(PINNED_MS + VETO_PROBE_TTL_MS + 1))
    const spy = failVetoWrites(() => true)
    expect(guard.recheck()).toEqual(['s'])
    spy.mockRestore()
    vi.setSystemTime(new Date(PINNED_MS + 2 * VETO_PROBE_TTL_MS + 2))
    expect(guard.recheck()).toEqual([])
    guard.release()
  })
})
