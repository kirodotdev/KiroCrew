import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'

import { retryPolicy, isDeadlineError, isThrottleError } from '../api/queryClient'

/* A deadline we set ourselves must never be retried: the retry doubles the wait
 * the deadline exists to bound, so a 15s bound settles at ~31s with its backoff. */

const deadlineError = () => {
  const e = new Error('deadline exceeded')
  e.name = 'TimeoutError'
  return e
}
const throttleError = () => Object.assign(new Error('Rate exceeded'), { status: 429 })

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('retryPolicy — the deadline clause binds app-wide, not per query', () => {
  it('refuses to retry a deadline error', () => {
    expect(retryPolicy(0, deadlineError())).toBe(false)
  })

  it('keeps the single retry for an ordinary failure', () => {
    // The clause must not become a blanket `retry: false`.
    expect(retryPolicy(0, new Error('boom'))).toBe(true)
    expect(retryPolicy(1, new Error('boom'))).toBe(false)
  })

  it('keeps the longer throttle ladder for a 429', () => {
    expect(retryPolicy(0, throttleError())).toBe(true)
    expect(retryPolicy(3, throttleError())).toBe(true)
    expect(retryPolicy(4, throttleError())).toBe(false)
  })

  it('does not mistake a bare string, null, or another abort reason for a deadline', () => {
    expect(isDeadlineError(null)).toBe(false)
    expect(isDeadlineError('TimeoutError')).toBe(false)
    expect(isDeadlineError({ name: 'AbortError' })).toBe(false)
    // And the two clauses stay independent of each other.
    expect(isThrottleError(deadlineError())).toBe(false)
    expect(isDeadlineError(throttleError())).toBe(false)
  })
})

describe('every initiator inherits it, whichever one raised the deadline', () => {
  /** Run a query under the app's default policy and count queryFn invocations. */
  async function attempts(queryKey: unknown[]) {
    const fn = vi.fn(() => Promise.reject(deadlineError()))
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: retryPolicy, retryDelay: 0 } },
    })
    await qc.fetchQuery({ queryKey, queryFn: fn }).catch(() => {})
    return fn.mock.calls.length
  }

  // One case per react-query initiator of `api.skills`, keyed as each one keys it.
  // The three marked unfixed are the ones per-site threading missed.
  it.each([
    ['SkillPickerMenu', ['skills', 'dashboard:chat-1', null, null]],
    ['ChatInput prefetch', ['skills', 'dashboard:chat-1', '/work/p', null]],
    ['command palette', ['skills']],
    ['HookSkillsSelect (was unfixed)', ['skills']],
    ['SkillsTab (was unfixed)', ['skills']],
    ['AgentSkillsEditor (was unfixed)', ['skills-catalog']],
  ])('%s settles after ONE attempt', async (_label, key) => {
    expect(await attempts(key as unknown[])).toBe(1)
  })

  it('a non-deadline failure still gets its retry, so the control can fail', async () => {
    // Negative control on the assertion above: if the policy retried nothing at
    // all, every case would read 1 and prove nothing. This one must read 2.
    const fn = vi.fn(() => Promise.reject(new Error('boom')))
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: retryPolicy, retryDelay: 0 } },
    })
    await qc.fetchQuery({ queryKey: ['skills-control'], queryFn: fn }).catch(() => {})
    expect(fn.mock.calls.length).toBe(2)
  })
})
