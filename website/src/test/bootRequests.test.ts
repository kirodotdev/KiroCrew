import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { queryClient } from '../api/queryClient'
import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

/**
 * Guards for the round-trip reductions in the dashboard's boot path. Each of
 * these costs a full round-trip per occurrence when the dashboard is reached
 * over a tunnel, so a regression here is a user-visible slowdown, not a style
 * nit.
 */

describe('query defaults', () => {
  it('does not refetch every stale query on window focus', () => {
    // Live data arrives by WebSocket push; focus-refetch is redundant churn.
    expect(queryClient.getDefaultOptions().queries?.refetchOnWindowFocus).toBe(false)
  })

  it('keeps the retry policy and staleTime it had', () => {
    const q = queryClient.getDefaultOptions().queries
    expect(q?.staleTime).toBe(30_000)
    expect(q?.retry).toBeTypeOf('function')
  })
})

describe('agent-detail model resolution', () => {
  let detail: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    detail = vi.spyOn(api, 'agentDetail').mockResolvedValue({ model: 'claude-opus-5' })
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({} as never)
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('issues ONE request when several slots resolve the same template at once', async () => {
    const provider = new AcpAdapter()
    const [a, b, c] = await Promise.all([
      provider.resolveModel('custom-agent'),
      provider.resolveModel('custom-agent'),
      provider.resolveModel('custom-agent'),
    ])
    expect(detail).toHaveBeenCalledTimes(1)
    expect([a, b, c]).toEqual(['claude-opus-5', 'claude-opus-5', 'claude-opus-5'])
  })

  it('still hits the network on a later lookup, so an edited model is not cached stale', async () => {
    const provider = new AcpAdapter()
    await provider.resolveModel('custom-agent')
    detail.mockResolvedValue({ model: 'claude-haiku-4.5' })
    await expect(provider.resolveModel('custom-agent')).resolves.toBe('claude-haiku-4.5')
    expect(detail).toHaveBeenCalledTimes(2)
  })

  it('keys the dedupe by template name', async () => {
    const provider = new AcpAdapter()
    await Promise.all([
      provider.resolveModel('agent-one'),
      provider.resolveModel('agent-two'),
    ])
    expect(detail).toHaveBeenCalledTimes(2)
  })

  it('a failed lookup does not poison later lookups', async () => {
    const provider = new AcpAdapter()
    detail.mockRejectedValueOnce(new Error('500'))
    await expect(provider.resolveModel('custom-agent')).resolves.toBe('')
    detail.mockResolvedValue({ model: 'claude-opus-5' })
    await expect(provider.resolveModel('custom-agent')).resolves.toBe('claude-opus-5')
  })
})
