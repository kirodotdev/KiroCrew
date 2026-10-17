import { describe, expect, it } from 'vitest'

import { tokenTtlTotalSeconds, ttlToSeconds } from './tokenTtl'

const REFRESH_AT_ELAPSED_FRAC = 0.8

/** The comparison every caller makes, so a test can assert the real outcome. */
function refreshDue(remaining: number, total: number): boolean {
  return !(remaining > total * (1 - REFRESH_AT_ELAPSED_FRAC))
}

describe('ttlToSeconds', () => {
  it('parses the two units the registry stores', () => {
    expect(ttlToSeconds('20h')).toBe(72000)
    expect(ttlToSeconds('1h')).toBe(3600)
    expect(ttlToSeconds('30m')).toBe(1800)
  })

  it('reads anything else as zero, which callers treat as unknown', () => {
    expect(ttlToSeconds('')).toBe(0)
    expect(ttlToSeconds(undefined)).toBe(0)
    expect(ttlToSeconds('20')).toBe(0)
    expect(ttlToSeconds('20d')).toBe(0)
    expect(ttlToSeconds('-1h')).toBe(0)
  })
})

describe('tokenTtlTotalSeconds', () => {
  it("prefers the gateway's published total over the row's TTL", () => {
    expect(tokenTtlTotalSeconds({ token_ttl_total: 3600 }, '20h')).toBe(3600)
  })

  it('falls back to the row when nothing is published', () => {
    expect(tokenTtlTotalSeconds(undefined, '20h')).toBe(72000)
    expect(tokenTtlTotalSeconds({}, '20h')).toBe(72000)
    expect(tokenTtlTotalSeconds({ token_ttl_remaining: 10 }, '20h')).toBe(72000)
  })

  it('does not trust a non-positive or non-finite published total', () => {
    // A zero total would make every remaining read as past the threshold, which
    // is the very loop this function exists to prevent.
    expect(tokenTtlTotalSeconds({ token_ttl_total: 0 }, '20h')).toBe(72000)
    expect(tokenTtlTotalSeconds({ token_ttl_total: -5 }, '20h')).toBe(72000)
    expect(tokenTtlTotalSeconds({ token_ttl_total: NaN }, '20h')).toBe(72000)
    expect(tokenTtlTotalSeconds({ token_ttl_total: Infinity }, '20h')).toBe(72000)
  })

  it('stops a chained crew re-minting on every poll', () => {
    // A 1h token from the parent against a 20h row here. Measured against the
    // row, a token minted seconds ago is already past the refresh threshold, so
    // every poll re-mints and the hidden pane's iframe is remounted each time.
    const status = { token_ttl_remaining: 3500, token_ttl_total: 3600 }
    expect(refreshDue(3500, ttlToSeconds('20h'))).toBe(true)
    expect(refreshDue(3500, tokenTtlTotalSeconds(status, '20h'))).toBe(false)
  })

  it('still refreshes a chained token that really is near its end', () => {
    const status = { token_ttl_remaining: 500, token_ttl_total: 3600 }
    expect(refreshDue(500, tokenTtlTotalSeconds(status, '20h'))).toBe(true)
  })

  it('is unchanged for a crew whose token this gateway minted', () => {
    // Row and published total agree, so the decision is the same either way.
    const status = { token_ttl_remaining: 60000, token_ttl_total: 72000 }
    expect(tokenTtlTotalSeconds(status, '20h')).toBe(ttlToSeconds('20h'))
    expect(refreshDue(60000, tokenTtlTotalSeconds(status, '20h'))).toBe(false)
    expect(refreshDue(10000, tokenTtlTotalSeconds(status, '20h'))).toBe(true)
  })
})
