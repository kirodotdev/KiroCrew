import { describe, expect, it } from 'vitest'
import { isRelayHostOffline } from './relayHostOffline'

describe('isRelayHostOffline', () => {
  it("is the relay's 503 crew_not_connected answer", () => {
    expect(isRelayHostOffline({ status: 503, body: '{"reason":"crew_not_connected","message":"The Crew is not connected to the relay."}' })).toBe(true)
  })
  it('is not one slow request the relay timed out (504 crew_not_answering)', () => {
    expect(isRelayHostOffline({ status: 504, body: '{"reason":"crew_not_answering","message":"The Crew did not answer in time."}' })).toBe(false)
  })
  it('is not another 503, another reason, or a gateway error', () => {
    expect(isRelayHostOffline({ status: 503, body: '{"reason":"draining"}' })).toBe(false)
    expect(isRelayHostOffline({ status: 503, body: 'Service Unavailable' })).toBe(false)
    expect(isRelayHostOffline({ status: 403, body: '{"reason":"crew_not_connected"}' })).toBe(false)
    expect(isRelayHostOffline(new Error('x'))).toBe(false)
    expect(isRelayHostOffline(null)).toBe(false)
  })
})
