/**
 * Regression test for the i18next recursion guard.
 *
 * Verifies that a cyclic `$t(...)` nesting reference in the catalog degrades
 * to a bounded fallback (returns the key) instead of overflowing the stack
 * with "too much recursion" — the crash observed on the dashboard `/chat`
 * route reported in #17357.
 *
 * i18next's own single-level guard catches direct (a→b→a) cycles because it
 * tracks the immediately prior key (`lastKey`). It does NOT catch indirect
 * cycles of three or more keys, because `lastKey` only holds the one-deep
 * ancestor — those are what the application-level depth guard intercepts.
 */

import { describe, expect, it, vi } from 'vitest'

import { i18next, initI18n } from './index'
import { installRecursionGuard } from './recursionGuard'

// `integration/setup.ts` already calls `initI18n('en')`, which installs the
// guard. We still call it here so the test is self-contained when run alone.
initI18n('en')

/**
 * Register a ring of `n` keys where each nests the next and the last nests back
 * to the first, under a prefix unique to this call so no two tests share keys.
 * Returns the first key of the ring.
 */
let ringSeq = 0
function registerCyclicRing(n: number): string {
  ringSeq += 1
  const prefix = `recursionGuardRing${ringSeq}`
  const keys = Array.from({ length: n }, (_, i) => `${prefix}.k${i}`)
  for (let i = 0; i < n; i++) {
    i18next.addResource('en', 'translation', keys[i], `$t(${keys[(i + 1) % n]})`)
  }
  return keys[0]
}

describe('recursion guard on translate', () => {
  it('stops a long cyclic ring without stack overflow', () => {
    // A ring far longer than any lastKey window: i18next cannot catch it, so
    // the depth guard must. The call must return (a string) rather than throw
    // "too much recursion" / RangeError.
    const first = registerCyclicRing(40)

    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const result = i18next.t(first)
    spy.mockRestore()

    expect(typeof result).toBe('string')
    expect(result.length).toBeGreaterThan(0)
  })

  it('logs a console.error when a cyclic ring hits the depth ceiling', () => {
    const first = registerCyclicRing(40)

    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    i18next.t(first)
    const calls = spy.mock.calls.map(call => String(call[0]))
    spy.mockRestore()

    expect(calls.some(m => m.includes('nesting exceeded'))).toBe(true)
  })

  it('returns a key string (not empty) for a value stopped by the guard', () => {
    // i18next's own null-nest fallback yields an empty string; the depth guard
    // instead returns the offending key. A non-empty result on a ring too long
    // for i18next's own guard shows the depth guard produced it.
    const first = registerCyclicRing(40)

    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const result = i18next.t(first)
    spy.mockRestore()

    expect(result).not.toBe('')
    expect(result.startsWith('recursionGuardRing')).toBe(true)
  })

  it('allows legitimate nested $t() references within the depth ceiling', () => {
    // A finite chain well within the ceiling must resolve fully to its leaf.
    ringSeq += 1
    const prefix = `recursionGuardChain${ringSeq}`
    const keys = Array.from({ length: 5 }, (_, i) => `${prefix}.k${i}`)
    for (let i = 0; i < keys.length - 1; i++) {
      i18next.addResource('en', 'translation', keys[i], `$t(${keys[i + 1]})`)
    }
    i18next.addResource('en', 'translation', keys[keys.length - 1], 'leaf value')

    expect(i18next.t(keys[0])).toBe('leaf value')
  })

  it('installRecursionGuard is idempotent', () => {
    const inst = i18next as unknown as { translator?: { translate: (...args: unknown[]) => unknown } }
    const before = inst.translator?.translate
    installRecursionGuard(inst)
    expect(inst.translator?.translate).toBe(before)
  })
})
