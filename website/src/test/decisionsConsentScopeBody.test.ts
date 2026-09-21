/**
 * What actually leaves the machine when a scope switch is flipped.
 *
 * The card's own suite asserts the ARGUMENTS it passes; this asserts the JSON `client.ts`
 * puts on the wire, because those are different claims and it is this one the route's
 * safety rests on. A body carrying `enabled` can only carry what this client last READ,
 * so a view read before a revoke would turn egress back on -- and the fix is that the key
 * is not in the body at all, not that the value happens to be right.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'

const ENDPOINT = 'https://api.typesafe.ai/v1/systemone'

/** Capture one request's parsed JSON body. */
function captureBody(): { read: () => Record<string, unknown> } {
  let sent: Record<string, unknown> = {}
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: unknown, init?: { body?: string }) => {
      sent = JSON.parse(init?.body ?? '{}')
      return {
        ok: true,
        status: 200,
        json: async () => ({ enabled: true }),
        text: async () => '{"enabled":true}',
        headers: { get: () => 'application/json' },
      } as unknown as Response
    }),
  )
  return { read: () => sent }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a scope write carries no switch', () => {
  it('sends no `enabled` key for the tool-argument scope', async () => {
    const captured = captureBody()
    await api.saveDecisionsConsent(undefined, ENDPOINT, true)
    const body = captured.read()
    expect('enabled' in body).toBe(false)
    expect(body).toEqual({ endpoint: ENDPOINT, tool_args: true })
  })

  it('sends no `enabled` key for the whole-transcript scope', async () => {
    const captured = captureBody()
    await api.saveDecisionsConsent(undefined, ENDPOINT, undefined, true)
    const body = captured.read()
    expect('enabled' in body).toBe(false)
    // And no `tool_args` either: an omitted scope is preserved, so naming it would let
    // one switch move the other.
    expect(body).toEqual({ endpoint: ENDPOINT, compaction: true })
  })

  it('still sends the switch when the switch is what moved', async () => {
    // The omission is the SCOPE path, not a new default: an ordinary flip must still
    // carry `enabled`, or the route would refuse it as naming nothing.
    const captured = captureBody()
    await api.saveDecisionsConsent(true, ENDPOINT)
    expect(captured.read()).toEqual({ endpoint: ENDPOINT, enabled: true })
  })

  it('sends the switch alone when consent is revoked', async () => {
    // A revoke names no endpoint and no scope: the route clears all three, and sending
    // an address alongside would suggest the record keeps one.
    const captured = captureBody()
    await api.saveDecisionsConsent(false, ENDPOINT)
    expect(captured.read()).toEqual({ enabled: false })
  })
})
