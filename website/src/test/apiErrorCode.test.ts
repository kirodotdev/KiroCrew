/**
 * gatewayErrorCode — the structured `code` of a gateway refusal, read off the
 * raw body an ApiError keeps, so a caller can tell two same-status failures
 * apart (an unreachable registry vs. a malformed bundle, both 502) without
 * matching words in the message.
 */
import { describe, it, expect } from 'vitest'
import { ApiError, gatewayErrorCode, toApiError } from '../api/apiError'

const res = (status: number, body: string): Response =>
  ({
    ok: false,
    status,
    headers: { get: () => null },
    text: () => Promise.resolve(body),
  }) as unknown as Response

describe('gatewayErrorCode', () => {
  it('reads the code from a structured {error, code} body', async () => {
    const e = await toApiError(res(413, JSON.stringify({
      error: 'Skill bundle is 12.3 MiB, above the 10.0 MiB limit',
      code: 'too_large',
    })))
    expect(gatewayErrorCode(e)).toBe('too_large')
    // The message stays the human sentence; the code rides on the body.
    expect(e.message).toBe('Skill bundle is 12.3 MiB, above the 10.0 MiB limit')
  })

  it('distinguishes two 502s by code, which the status alone cannot', () => {
    const unreachable = new ApiError(502, 'Could not reach skills.sh',
      JSON.stringify({ error: 'Could not reach skills.sh', code: 'unreachable' }))
    const malformed = new ApiError(502, 'skills.sh returned an unexpected response format',
      JSON.stringify({ error: 'skills.sh returned an unexpected response format', code: 'bad_format' }))
    expect(gatewayErrorCode(unreachable)).toBe('unreachable')
    expect(gatewayErrorCode(malformed)).toBe('bad_format')
  })

  it("is '' for anything that is not the gateway's structured shape", () => {
    // A bare status, an edge throttle envelope, an HTML error page, a body
    // whose code is not a string, unparseable JSON, and non-error values.
    expect(gatewayErrorCode(new ApiError(500, 'HTTP 500'))).toBe('')
    expect(gatewayErrorCode(new ApiError(429, 'x', '{"message":"Rate exceeded"}'))).toBe('')
    expect(gatewayErrorCode(new ApiError(502, 'x', '<!DOCTYPE html><html></html>'))).toBe('')
    expect(gatewayErrorCode(new ApiError(502, 'x', '{"error":"x","code":7}'))).toBe('')
    expect(gatewayErrorCode(new ApiError(502, 'x', '{not json'))).toBe('')
    expect(gatewayErrorCode(new TypeError('Failed to fetch'))).toBe('')
    expect(gatewayErrorCode(null)).toBe('')
    expect(gatewayErrorCode(undefined)).toBe('')
    expect(gatewayErrorCode('too_large')).toBe('')
  })

  it('is duck-typed on body, so a mocked ApiError-shaped rejection counts', () => {
    const mocked = Object.assign(new Error('x'), {
      status: 404,
      body: JSON.stringify({ error: 'x', code: 'not_found' }),
    })
    expect(gatewayErrorCode(mocked)).toBe('not_found')
  })
})
