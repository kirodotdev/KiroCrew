// GPT F1 at 817ee1aa9, upheld by Opus: the bulk cleanup's dirty filter is point-in-time,
// so a draft claimed inside the check -> archive interval was archived anyway.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

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

const SIDEBAR = readFileSync(join(__dirname, '..', 'pages', 'ChatSidebar.tsx'), 'utf-8')
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

  it('SENDS the candidate list, so one slot can be dropped without abandoning the batch', () => {
    // Was the opposite: the request carried no set, the server recomputed it, and a
    // single refusal had to abort everything.
    expect(SIDEBAR).toContain('api.cleanupSessions(cleanupDays, activeSlot')
    expect(SIDEBAR).toContain('cleanupMutation.mutate(commitKeys,')
    expect(SIDEBAR).toContain("const commitKeys = archivable.map(s => s.key).filter(k => !refusedNow.has(k))")
  })

  it('runs the handshake before the request, not in onSuccess', () => {
    const gate = SIDEBAR.indexOf('awaitCleanupRefusals(archivable.map(s => s.key)')
    const fire = SIDEBAR.indexOf('cleanupMutation.mutate(')
    expect(gate).toBeGreaterThan(-1)
    expect(gate).toBeLessThan(fire)
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

  it('is what gates the close, so a lone window never awaits', () => {
    const hook = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
    // Split across two statements now, because a FAILED publish must be told apart from
    // "nobody to ask": the first is a refusal, the second is not.
    expect(hook).toContain('const holderElsewhere = anotherWindowHoldsComposer()')
    expect(hook).toContain('holderElsewhere ? publishClosingIntent(slotKey) : null')
    // The unconditional form is what made every close pay the window.
    expect(hook).not.toContain('const intent = publishClosingIntent(slotKey)\n')
  })

  it('is published by the registry for every live composer', () => {
    const reg = readFileSync(join(__dirname, '..', 'utils', 'slotComposerRegistry.ts'), 'utf-8')
    expect(reg).toContain('publishComposerPresence(id)')
    expect(reg).toContain('withdrawPresence()')
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

  it('is re-stamped by the registration hook, so a live composer never goes stale', () => {
    const hook = readFileSync(join(__dirname, '..', 'hooks', 'useSlotComposerRegistration.ts'), 'utf-8')
    expect(hook).toContain('setInterval(() => publishComposerPresence(id), SLOT_DIRTY_REFRESH_MS)')
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

  it('releases the guard when the archive request settles, not when acks close', () => {
    expect(SIDEBAR).toContain('cleanupMutation.mutate(commitKeys, { onSettled: () => guard.release() })')
    // The abort path owns its own release, since no request will settle to do it.
    const abort = SIDEBAR.indexOf('guard.release()')
    const settle = SIDEBAR.indexOf('onSettled: () => guard.release()')
    expect(abort).toBeGreaterThan(-1)
    expect(abort).toBeLessThan(settle)
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

  it('reads the recheck BEFORE it fires the request, not after', () => {
    const rechecked = SIDEBAR.indexOf('...guard.recheck()')
    const fired = SIDEBAR.indexOf('cleanupMutation.mutate(commitKeys')
    expect(rechecked).toBeGreaterThan(-1)
    expect(fired).toBeGreaterThan(rechecked)
  })

  it('bounds the server to the sent set rather than letting it recompute', () => {
    const client = readFileSync(join(__dirname, '..', 'api', 'client.ts'), 'utf-8')
    expect(client).toContain("...(onlyKeys ? { only_keys: onlyKeys } : {})")
  })

  it('does NOT abort the batch when the FIRST tier refuses, only drops those slots', () => {
    // Was `if (guard.refused.length > 0) { ...return }`: one draft in another window
    // held back every other stale session until that window was found.
    const firstTier = SIDEBAR.indexOf("if (guard.refused.length > 0) {")
    expect(firstTier).toBeGreaterThan(-1)
    const block = SIDEBAR.slice(firstTier, firstTier + 320)
    expect(block).not.toContain('return')
    expect(SIDEBAR).toContain('new Set([...guard.refused, ...guard.recheck()])')
  })

  it('keeps a slot MOUNTED when its refusal lands during the round-trip', () => {
    // The server archived it; evicting it here is what would unmount the composer and
    // take the draft with it, so the eviction is the part that gets skipped.
    expect(SIDEBAR).toContain('const late = cleanupGuardRef.current?.recheck() ?? []')
    expect(SIDEBAR).toContain('for (const key of res.keys) if (!late.includes(key)) dispatch(deleteSlot(key))')
  })

  it('checks BOTH sides of the request, and not only when another window exists', () => {
    const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
    const lines = HOOK.split('\n')
    const del = lines.findIndex(l => l.includes('await dispatch(deleteSlot(slotKey)).unwrap()'))
    expect(del).toBeGreaterThan(-1)
    const above = lines.slice(Math.max(0, del - 16), del).join('\n')
    const below = lines.slice(del + 1, del + 14).join('\n')
    // `intent` is null when NO OTHER WINDOW holds a composer, so `intent && ...` skips the
    // single-window close entirely -- both sides must consult the storage-free registry.
    expect(above).toContain('closingIntentVetoed(intent)')
    expect(above).toContain('slotHasUnsentWorkHere(slotKey)')
    // The CONDITION, not just the capture: branching on `intent` alone still leaves the
    // single-window path unreported, and asserting the capture line passed that mutation.
    expect(below).toContain('if (appeared || (intent && closingIntentVetoed(intent))) {')
  })

  it('holds the slot QUIESCED across the request and releases it on settle', () => {
    const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
    expect(HOOK).toContain('const releaseQuiesce = beginSlotQuiesce(slotKey)')
    // In a `finally`, so a rejected DELETE cannot leave the slot quiesced forever.
    const lines = HOOK.split('\n')
    const del = lines.findIndex(l => l.includes('await dispatch(deleteSlot(slotKey)).unwrap()'))
    const after = lines.slice(del + 1, del + 5).join('\n')
    expect(after).toContain('finally')
    expect(after).toContain('releaseQuiesce()')
  })

  it('reports work that APPEARED in the window, not work already consented to', () => {
    const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
    expect(HOOK).toContain('const workBefore = slotHasUnsentWorkHere(slotKey)')
    expect(HOOK).toContain('const appeared = !workBefore && slotHasUnsentWorkHere(slotKey)')
  })

  it('claims work born mid-close UNRECOVERABLE, whatever the caller declared', () => {
    const REG = readFileSync(join(__dirname, '..', 'hooks', 'useSlotComposerRegistration.ts'), 'utf-8')
    expect(REG).toContain('slotIsQuiescing(slot)) publishSlotDirty(id, slot, true, false)')
  })

  it('puts no AWAIT between the final veto read and the request, and leaves it REACHABLE', () => {
    const HOOK = readFileSync(join(__dirname, '..', 'hooks', 'useSessionActions.ts'), 'utf-8')
    const lines = HOOK.split('\n')
    const del = lines.findIndex(l => l.includes('await dispatch(deleteSlot(slotKey)).unwrap()'))
    // Arming the quiesce DOES sit here, deliberately. Another `await` must not: that is
    // what reopens the window the guard just closed.
    let i = del - 1
    const between: string[] = []
    while (i >= 0 && lines[i].trim() !== '}') { between.push(lines[i]); i -= 1 }
    expect(between.join('\n')).not.toContain('await')
    expect(lines[i].trim()).toBe('}')
    // The CONDITION verbatim, so short-circuiting the guard dead (`false &&`, a flag, a
    // negation) fails here. Presence alone passed while the read was unreachable.
    const above = lines.slice(Math.max(0, del - 16), del).join('\n')
    expect(above).toContain('consentedAt === null && slotHasUnsentWorkHere(slotKey)')
    expect(above).toContain('return')
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
