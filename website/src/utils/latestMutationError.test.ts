import { describe, expect, it } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useLatestMutationError } from './latestMutationError'

type M = { submittedAt: number; error: unknown }
const run = (initial: M[]) =>
  renderHook(({ ms }: { ms: M[] }) => useLatestMutationError(...ms), { initialProps: { ms: initial } })

describe('useLatestMutationError', () => {
  const failed = new Error('refused')

  it('is null once a later action succeeded', () => {
    expect(run([{ submittedAt: 1, error: failed }, { submittedAt: 2, error: null }]).result.current).toBeNull()
  })

  it('reports a newer failure over an older one', () => {
    const newer = new Error('newer')
    expect(run([{ submittedAt: 1, error: failed }, { submittedAt: 2, error: newer }]).result.current).toBe(newer)
  })

  it('lets the later-listed mutation win a same-millisecond tie', () => {
    const later = new Error('later')
    expect(run([{ submittedAt: 5, error: failed }, { submittedAt: 5, error: later }]).result.current).toBe(later)
  })

  it('ignores mutations that never ran', () => {
    expect(run([{ submittedAt: 0, error: null }, { submittedAt: 3, error: failed }]).result.current).toBe(failed)
    expect(run([{ submittedAt: 0, error: null }, { submittedAt: 0, error: null }]).result.current).toBeNull()
  })

  it('does not hand the notice back to an older failure when the latest action is reset', () => {
    // A save fails, a later create succeeds, then the create is reset (its
    // dialog reopened): the old save failure must not come back.
    const h = run([{ submittedAt: 1, error: failed }, { submittedAt: 2, error: null }])
    expect(h.result.current).toBeNull()
    h.rerender({ ms: [{ submittedAt: 1, error: failed }, { submittedAt: 0, error: null }] })
    expect(h.result.current).toBeNull()
  })
})
