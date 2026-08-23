/** Unit tests for the slot-switch protocol behind the optimistic slot-field
 *  switches (#4523), exercised entirely through the module's public surface
 *  (`performSlotSwitch`, `pendingSlotSwitch`) — the protocol steps are
 *  deliberately module-private so no call site can mis-compose them. The
 *  integration halves live in test/ChatPage.switchLabelOptimistic.test.tsx
 *  and test/ChatPane.switchLabelOptimistic.test.tsx.
 *
 *  The model under test: the wire is strictly serialized per slot+field
 *  (send order = server processing order) and a failure changes nothing
 *  server-side, so the value in force is the newest request that SUCCEEDED —
 *  and the store must converge on it even when outcomes reach the registry
 *  late (a caller released by the confirm timeout).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  pendingSlotSwitch,
  performSlotSwitch,
  SWITCH_CONFIRM_TIMEOUT_MS,
} from './slotSwitch'

// Unique slot keys per test: the registry is deliberately module-global
// (every control sharing a slot+field must share one sequence), so tests
// isolate by key rather than by resetting shared state.
describe('performSlotSwitch (#4523)', () => {
  afterEach(() => { vi.useRealTimers() })

  it('writes the request value on success and resolves', async () => {
    const writes: string[] = []
    await performSlotSwitch('model', 'slot-ok', 'model-a',
      async () => 'model-a-stored', (v) => writes.push(v))
    expect(writes).toEqual(['model-a-stored'])
  })

  it('rejects on request failure and writes nothing (no earlier success to adopt)', async () => {
    const writes: string[] = []
    await expect(performSlotSwitch('model', 'slot-fail', 'model-a',
      () => Promise.reject(new Error('boom')), (v) => writes.push(v))).rejects.toThrow('boom')
    expect(writes).toEqual([])
  })

  it('serializes the wire: the second pick must not start before the first settles', async () => {
    const events: string[] = []
    let releaseFirst: (v: string) => void = () => {}
    const first = performSlotSwitch('model', 'slot-serial', 'model-a',
      () => { events.push('first-start'); return new Promise<string>(res => { releaseFirst = res }) },
      () => events.push('first-write'))
    const second = performSlotSwitch('model', 'slot-serial', 'model-b',
      async () => { events.push('second-start'); return 'model-b' },
      () => events.push('second-write'))
    // A macrotask tick drains all pending microtasks: the first request has
    // started, the second must still be waiting on it.
    await new Promise(res => setTimeout(res, 0))
    expect(events).toEqual(['first-start'])
    releaseFirst('model-a')
    await first
    await second
    // Wire order is the guarantee: the second request started only after the
    // first settled. And latest-request-wins means the FIRST pick's success
    // is held (superseded by the already-begun second), never written — the
    // one write is the newest pick's.
    expect(events.indexOf('second-start')).toBeGreaterThan(events.indexOf('first-start'))
    expect(events.filter(e => e.endsWith('-write'))).toEqual(['second-write'])
  })

  it('chains are independent per slot and per field', async () => {
    let releaseBlock: (v: string) => void = () => {}
    const blocked = performSlotSwitch('model', 'slot-ind-a', 'model-a',
      () => new Promise<string>(res => { releaseBlock = res }), () => {})
    // A different slot and a different field must not queue behind it.
    const otherSlot: string[] = []
    const otherField: string[] = []
    await performSlotSwitch('model', 'slot-ind-b', 'model-b',
      async () => 'model-b', (v) => otherSlot.push(v))
    await performSlotSwitch('project', 'slot-ind-a', '/dir',
      async () => '/dir', (v) => otherField.push(v))
    expect(otherSlot).toEqual(['model-b'])
    expect(otherField).toEqual(['/dir'])
    releaseBlock('model-a')
    await blocked
  })

  it('newest failure adopts the older pick that already landed', async () => {
    // Serialized: first request succeeds, second fails — the backend is left
    // on the first pick, so the second's failure path must write it.
    const writes: string[] = []
    const first = performSlotSwitch('model', 'slot-adopt', 'model-a',
      async () => 'model-a', (v) => writes.push('w1:' + v))
    const second = performSlotSwitch('model', 'slot-adopt', 'model-b',
      () => Promise.reject(new Error('boom')), (v) => writes.push('w2:' + v))
    await first
    await expect(second).rejects.toThrow('boom')
    // First's own success was superseded (held, not written); the second's
    // failure recovered it — exactly once, through the failure path.
    expect(writes).toEqual(['w2:model-a'])
  })

  it('pending carries the newest in-flight target and clears when it settles', async () => {
    expect(pendingSlotSwitch('model', 'slot-pend')).toBe('')
    let release: (v: string) => void = () => {}
    const p = performSlotSwitch('model', 'slot-pend', 'model-a',
      () => new Promise<string>(res => { release = res }), () => {})
    // Burst-stepping consumers (the cycle shortcuts) read this to advance
    // from the requested target rather than a store base that has not
    // settled yet. (The ticket is taken synchronously at the pick.)
    expect(pendingSlotSwitch('model', 'slot-pend')).toBe('model-a')
    // The request callback runs on the next tick — wait for it so `release`
    // is the real resolver, not the initial no-op.
    await new Promise(res => setTimeout(res, 0))
    release('model-a')
    await p
    expect(pendingSlotSwitch('model', 'slot-pend')).toBe('')
  })

  it('pending stays on the newest target while an older request is still in flight', async () => {
    let releaseFirst: (v: string) => void = () => {}
    const first = performSlotSwitch('model', 'slot-pend2', 'model-a',
      () => new Promise<string>(res => { releaseFirst = res }), () => {})
    const second = performSlotSwitch('model', 'slot-pend2', 'model-b',
      async () => 'model-b', () => {})
    expect(pendingSlotSwitch('model', 'slot-pend2')).toBe('model-b')
    await new Promise(res => setTimeout(res, 0))
    releaseFirst('model-a')
    await first
    await second
    expect(pendingSlotSwitch('model', 'slot-pend2')).toBe('')
  })

  it('a stalled pick reports unconfirmed to its caller but NEVER unblocks the chain', async () => {
    vi.useFakeTimers()
    const writes: string[] = []
    const wireStarts: string[] = []
    let releaseHung: (v: string) => void = () => {}
    const hung = performSlotSwitch('model', 'slot-hang', 'model-a',
      () => { wireStarts.push('a'); return new Promise<string>(res => { releaseHung = res }) },
      (v) => writes.push('hung:' + v))
    const queued = performSlotSwitch('model', 'slot-hang', 'model-b',
      async () => { wireStarts.push('b'); return 'model-b' },
      (v) => writes.push('queued:' + v))
    // Attached before the clock moves: the rejections land mid-advance, and
    // an expectation attached only afterwards reports an unhandled error.
    const hungRejects = expect(hung).rejects.toThrow()
    const queuedRejects = expect(queued).rejects.toThrow()

    // At the budget BOTH callers are released as unconfirmed — the stalled
    // pick because its wire call has not settled, the queued pick because it
    // is still waiting behind it. The picker never freezes silently…
    await vi.advanceTimersByTimeAsync(SWITCH_CONFIRM_TIMEOUT_MS + 1)
    await hungRejects
    await queuedRejects
    // …but the WIRE stays strictly ordered: the second call has not started,
    // so a stalled-but-alive older request can never land after a newer one.
    expect(wireStarts).toEqual(['a'])
    expect(writes).toEqual([])

    // The stalled request finally answers: its success adjudicates, then the
    // queued request fires and, being newest, its value ends up written.
    releaseHung('model-a')
    await vi.advanceTimersByTimeAsync(1)
    expect(wireStarts).toEqual(['a', 'b'])
    expect(writes).toEqual(['queued:model-b'])
  })

  it('a timed-out pick with no successor writes its own late success', async () => {
    vi.useFakeTimers()
    const writes: string[] = []
    let release: (v: string) => void = () => {}
    const p = performSlotSwitch('model', 'slot-late', 'model-a',
      () => new Promise<string>(res => { release = res }), (v) => writes.push(v))
    const pRejects = expect(p).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(SWITCH_CONFIRM_TIMEOUT_MS + 1)
    await pRejects
    expect(writes).toEqual([])
    // The wire call eventually lands: the backend applied it, nothing newer
    // exists, so the late settle writes and the chip becomes truthful.
    release('model-a-stored')
    await vi.advanceTimersByTimeAsync(1)
    expect(writes).toEqual(['model-a-stored'])
  })

  it('a late success older than an already-written newer success is history, not a write', async () => {
    // Serialized wire, out-of-order OUTCOME handling: first pick stalls past
    // the budget, later picks proceed only after it settles — here the stall
    // resolves AFTER a newer pick has already been written; the old success
    // must not overwrite it.
    vi.useFakeTimers()
    const writes: string[] = []
    let releaseFirst: (v: string) => void = () => {}
    const first = performSlotSwitch('model', 'slot-hist', 'model-a',
      () => new Promise<string>(res => { releaseFirst = res }), (v) => writes.push('w1:' + v))
    const firstRejects = expect(first).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(SWITCH_CONFIRM_TIMEOUT_MS + 1)
    await firstRejects

    const second = performSlotSwitch('model', 'slot-hist', 'model-b',
      async () => 'model-b', (v) => writes.push('w2:' + v))
    // The second pick still waits behind the stalled first (strict chain).
    releaseFirst('model-a')
    await vi.advanceTimersByTimeAsync(1)
    await second
    // First's late success wrote first (it was newest at settle time until
    // superseded)… the end state must be the newest pick's value.
    expect(writes[writes.length - 1]).toBe('w2:model-b')
  })
})
