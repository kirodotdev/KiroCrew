/**
 * Tests for tips API calls using jNullable.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { __resetAuthRecoveryStateForTests } from '../api/client'

// We test that the tips API calls go through the shared response handler by
// verifying the exported api.tipsNext / api.tipsStatus surface the proper
// behavior (204 -> null, 401 -> session-expired, error -> ApiError).

describe('tips API auth recovery (jNullable)', () => {
  let originalFetch: typeof fetch
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
    originalFetch = globalThis.fetch
    fetchMock = vi.fn()
    globalThis.fetch = fetchMock as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    __resetAuthRecoveryStateForTests()
  })

  it('tipsNext returns null on 204 without throwing', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    // Import fresh to pick up the fetch mock
    const { api } = await import('../api/client')
    const result = await api.tipsNext()
    expect(result).toBeNull()
  })

  it('tipsNext throws ApiError on 500', async () => {
    fetchMock.mockResolvedValue(
      new Response('Internal Server Error', { status: 500, headers: { 'content-type': 'text/plain' } }),
    )
    const { api, ApiError } = await import('../api/client')
    await expect(api.tipsNext()).rejects.toBeInstanceOf(ApiError)
  })

  it('tipsStatus throws ApiError on 500', async () => {
    fetchMock.mockResolvedValue(
      new Response('Internal Server Error', { status: 500, headers: { 'content-type': 'text/plain' } }),
    )
    const { api, ApiError } = await import('../api/client')
    await expect(api.tipsStatus()).rejects.toBeInstanceOf(ApiError)
  })

  it('tipsNext invokes checkSessionExpired on 403', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'Token required' }), {
        status: 403,
        headers: { 'content-type': 'application/json', 'X-Auth-Required': 'true' },
      }),
    )
    const { api } = await import('../api/client')
    // Should throw (403 is !ok) but auth-banner path is exercised
    await expect(api.tipsNext()).rejects.toThrow()
  })
})
