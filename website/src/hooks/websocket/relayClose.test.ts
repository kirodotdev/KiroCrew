import { describe, it, expect } from 'vitest'
import { QUIET_RECONNECT_GRACE_MS, classifyClose, isQuietClose, nextOfflineCause } from './relayClose'

describe('classifyClose', () => {
  it('names each relay case and leaves every other code offline', () => {
    expect(classifyClose(4401)).toBe('token_renewal')
    expect(classifyClose(1012)).toBe('relay_drain')
    expect(classifyClose(4400)).toBe('refused')
    expect(classifyClose(4429)).toBe('refused')
    // A host that is gone is an outage, still retried; the owner turning
    // Remote Control off (4410) is its own case, waited out calmly; a replaced
    // host connection (4409) stays a refusal.
    expect(classifyClose(4404)).toBe('offline')
    expect(classifyClose(4409)).toBe('refused')
    expect(classifyClose(4410)).toBe('remote_off')
    expect(classifyClose(4499)).toBe('refused')
    expect(classifyClose(1006)).toBe('offline')
    expect(classifyClose(1011)).toBe('offline')
    expect(classifyClose(4500)).toBe('offline')
    expect(classifyClose(undefined)).toBe('offline')
  })

  it('reconnects quietly only on a token renewal or a drain', () => {
    expect(isQuietClose('token_renewal')).toBe(true)
    expect(isQuietClose('relay_drain')).toBe(true)
    expect(isQuietClose('refused')).toBe(false)
    expect(isQuietClose('offline')).toBe(false)
  })

  it('does not reconnect quietly after Remote Control off', () => {
    expect(isQuietClose('remote_off')).toBe(false)
  })

  it('keeps a refusal or Remote Control off on screen until the relay says the host is gone', () => {
    // A refusal, then a plain drop: still the refusal.
    expect(nextOfflineCause('refused', 'offline', 1006, false)).toBe('refused')
    // A refusal, then the relay's crew_not_connected: an outage a retry can fix.
    expect(nextOfflineCause('refused', 'offline', 4404, false)).toBeUndefined()
    // Remote Control off, then 4404 or a drop: still why the Crew is unreachable.
    expect(nextOfflineCause(undefined, 'remote_off', 4410, false)).toBe('remote_off')
    expect(nextOfflineCause('remote_off', 'offline', 4404, false)).toBe('remote_off')
    expect(nextOfflineCause('remote_off', 'offline', 1006, false)).toBe('remote_off')
    // Each close sets its own kind, and a failed token renewal ends the session.
    expect(nextOfflineCause('remote_off', 'refused', 4409, false)).toBe('refused')
    expect(nextOfflineCause('refused', 'remote_off', 4410, false)).toBe('remote_off')
    expect(nextOfflineCause('remote_off', 'offline', 1006, true)).toBe('session_ended')
    expect(nextOfflineCause(undefined, 'offline', 1006, false)).toBeUndefined()
  })

  it('keeps the grace the reconnect test assumes', () => {
    expect(QUIET_RECONNECT_GRACE_MS).toBe(5000)
  })
})
