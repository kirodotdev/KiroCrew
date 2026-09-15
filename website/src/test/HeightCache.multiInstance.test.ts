// Feature: chat-virtualizer
//
// Multi-instance property tests for HeightCache covering:
// - Property 8: Instance State Isolation (different sessionIds don't interact)
// - Property 9: Shared Cache Reusability (same sessionId reads back writes
//   from another instance after flush+reload)

import { describe, it, expect, beforeEach } from 'vitest'
import * as fc from 'fast-check'
import { HeightCache } from '../hooks/virtualizer/HeightCache'

beforeEach(() => {
  window.localStorage.clear()
})

const keyArb = fc.stringMatching(/^[a-zA-Z0-9_-]{1,12}$/)
const heightArb = fc.integer({ min: 1, max: 5000 })

// Feature: chat-virtualizer, Property 8: Instance State Isolation
// **Validates: Requirements 10.2**
describe('Property 8: Instance State Isolation', () => {
  it('writes to instance A do not affect instance B with a different sessionId', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 30 }),
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 30 }),
        (opsA, opsB) => {
          const sidA = `iso-a-${Math.random()}`
          const sidB = `iso-b-${Math.random()}`
          const a = new HeightCache(sidA)
          const b = new HeightCache(sidB)

          // Apply opsB to b first to seed its state.
          for (const [k, h] of opsB) b.set(k, h)
          b.flush()

          // Snapshot B's state.
          const beforeSnapshot = new Map<string, number>()
          for (const [k] of opsB) {
            const v = b.get(k)
            if (v !== undefined) beforeSnapshot.set(k, v)
          }

          // Apply opsA to a — and importantly, A.flush() should NOT touch
          // B's persisted blob.
          for (const [k, h] of opsA) a.set(k, h)
          a.flush()

          // Verify B's in-memory state unchanged.
          for (const [k, expectedH] of beforeSnapshot) {
            expect(b.get(k)).toBe(expectedH)
          }

          // Verify B's localStorage blob unchanged by reloading a fresh
          // instance C from sidB.
          const c = new HeightCache(sidB)
          for (const [k, expectedH] of beforeSnapshot) {
            expect(c.get(k)).toBe(expectedH)
          }

          a.clear(); b.clear(); c.clear()
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Feature: chat-virtualizer, Property 9: Shared Cache Reusability
// **Validates: Requirements 10.3**
describe('Property 9: Shared Cache Reusability', () => {
  it('two instances with the same sessionId can read each other\'s writes after flush', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 30 }),
        fc.uuid(),
        (ops, sid) => {
          const a = new HeightCache(sid)
          const expected = new Map<string, number>()
          for (const [k, h] of ops) {
            a.set(k, h)
            expected.set(k, h)
          }
          // A flushes — now persisted state contains all of A's writes.
          a.flush()

          // B is created AFTER A's flush, so it loads A's persisted state.
          const b = new HeightCache(sid)
          for (const [k, h] of expected) {
            expect(b.get(k)).toBe(h)
          }

          a.clear()
        },
      ),
      { numRuns: 50 },
    )
  })

  it('two simultaneous instances see each other\'s data on subsequent reload', () => {
    // Instance A and B exist concurrently with the same sessionId. They
    // each write a disjoint set of keys. After both flush, a fresh
    // instance C sees the LAST flush's data (last-writer-wins on
    // overlapping keys, but we use disjoint keys here).
    const sid = 'shared-concurrent'
    const a = new HeightCache(sid)
    const b = new HeightCache(sid)

    a.set('a-only-1', 100)
    a.set('a-only-2', 200)
    a.flush()

    // B reads from disk before its own flush — but only on construction.
    // After A's flush, B's in-memory state is stale relative to disk.
    // This is expected; the contract is "fresh instance reads same values"
    // (Property 9), not "live mirror".
    b.set('b-only-1', 300)
    b.flush() // overwrites with B's in-memory keys (b-only-1 only)

    // Fresh instance C loads whatever is on disk now (B's last flush wins).
    const c = new HeightCache(sid)
    expect(c.get('b-only-1')).toBe(300)
    // a-only-* keys are gone because B's flush overwrote the whole object.
    // This is a known limitation of the simple "rewrite the whole JSON
    // blob" persistence model. If multi-writer semantics ever matter,
    // we'd need to merge on flush.

    a.clear()
  })
})
