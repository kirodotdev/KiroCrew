// 429 edge-throttle UX: retry ladder + friendly error mapping.
//
// When the dashboard is served through Builder Tunnels (API Gateway), request
// bursts (e.g. opening Settings→Usage) can trip the edge throttle, which
// returns 429 {"message":"Rate exceeded","throttlingReasons":null} before the
// request reaches the gateway. Pre-fix the raw JSON body rendered in the
// Usage tab's error card after a single retry. These tests pin:
//   1. isThrottleError / retryPolicy / retryDelayPolicy (api/queryClient.ts)
//   2. the friendly 429 message thrown by the `j` helper (api/client.ts)
import { describe, it, expect, vi, afterEach } from 'vitest'
import { isThrottleError, retryPolicy, retryDelayPolicy } from '../api/queryClient'
import { api, ApiError } from '../api/client'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('isThrottleError', () => {
  it('is true for ApiError with status 429', () => {
    expect(isThrottleError(new ApiError(429, 'Rate exceeded'))).toBe(true)
  })
  it('is false for other statuses and non-API errors', () => {
    expect(isThrottleError(new ApiError(500, 'boom'))).toBe(false)
    expect(isThrottleError(new Error('boom'))).toBe(false)
    expect(isThrottleError(null)).toBe(false)
    expect(isThrottleError(undefined)).toBe(false)
    expect(isThrottleError('Rate exceeded')).toBe(false)
  })
})

describe('retryPolicy', () => {
  it('retries throttles up to 4 times', () => {
    const err = new ApiError(429, 'Rate exceeded')
    expect(retryPolicy(0, err)).toBe(true)
    expect(retryPolicy(3, err)).toBe(true)
    expect(retryPolicy(4, err)).toBe(false)
  })
  it('keeps the single retry for non-throttle errors', () => {
    const err = new ApiError(500, 'boom')
    expect(retryPolicy(0, err)).toBe(true)
    expect(retryPolicy(1, err)).toBe(false)
  })
})

describe('retryDelayPolicy', () => {
  it('uses jittered exponential backoff for throttles, capped at 15s + jitter', () => {
    const err = new ApiError(429, 'Rate exceeded')
    const d0 = retryDelayPolicy(0, err)
    expect(d0).toBeGreaterThanOrEqual(1_000)
    expect(d0).toBeLessThanOrEqual(1_500)
    const d4 = retryDelayPolicy(4, err)
    expect(d4).toBeGreaterThanOrEqual(15_000)
    expect(d4).toBeLessThanOrEqual(15_500)
  })
  it('uses the default curve for non-throttle errors (no jitter)', () => {
    const err = new ApiError(500, 'boom')
    expect(retryDelayPolicy(0, err)).toBe(1_000)
    expect(retryDelayPolicy(2, err)).toBe(4_000)
  })
})

describe('j helper 429 mapping', () => {
  it('replaces the raw API Gateway throttle body with a human-readable message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '{"message":"Rate exceeded","throttlingReasons":null}',
      { status: 429 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(429)
    expect((err as ApiError).message).toContain('Rate limited by the tunnel edge')
    expect((err as ApiError).message).not.toContain('throttlingReasons')
  })

  it('unwraps a non-429 JSON error body to the human message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '{"error": "boom"}',
      { status: 500 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
    expect((err as ApiError).message).toBe('boom')
  })

  it('preserves a non-JSON raw body for non-429 errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      'plain boom',
      { status: 500 },
    )))
    const err = await api.kiroUsage().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
    expect((err as ApiError).message).toBe('plain boom')
  })
})
