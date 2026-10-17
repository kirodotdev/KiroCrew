/**
 * rum.ts is the public build's telemetry SHIM: every entry point must stay
 * callable and must do nothing observable. The contract worth pinning is
 * exactly that — no throw for any argument shape, and `getRum()` returning
 * null so a call site's `getRum()?.x` guard short-circuits instead of reaching
 * a live client.
 */
import { describe, it, expect } from 'vitest'
import { initRum, recordEvent, recordSessionStart, getRum } from '../rum'

describe('rum telemetry shim', () => {
  it('initRum accepts a version and returns undefined', () => {
    expect(initRum('9.9.9-zzq')).toBeUndefined()
    // Safe to call repeatedly — no init guard to trip.
    expect(initRum('9.9.9-zzq')).toBeUndefined()
  })

  it('recordEvent swallows any event type and payload', () => {
    expect(recordEvent('qqz_event', { zz: 1, nested: { q: [1, 2] } })).toBeUndefined()
    expect(recordEvent('', {})).toBeUndefined()
  })

  it('recordSessionStart accepts a full status object and a retry count', () => {
    expect(recordSessionStart({
      owner_id_hash: 'zzq-hash',
      version: '9.9.9-zzq',
      os_type: 'zzos',
      arch: 'zzarch',
      cpu_count: 4,
      mem_total_gb: 8,
      platform: 'zzplat',
    }, 2)).toBeUndefined()
  })

  it('recordSessionStart works with an empty status and the default retry count', () => {
    expect(recordSessionStart({})).toBeUndefined()
  })

  it('getRum returns null so optional-chained call sites short-circuit', () => {
    expect(getRum()).toBeNull()
  })
})
