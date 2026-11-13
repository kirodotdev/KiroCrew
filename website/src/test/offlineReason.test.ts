import { describe, it, expect } from 'vitest'
import { isRelayCrewWindow, offlineReason } from '../utils/offlineReason'
import en from '../i18n/locales/en.manual.json'

describe('offlineReason', () => {
  it('names the relay reason, and keeps the generic wording for a plain drop', () => {
    expect(offlineReason('session_ended', false)).toBe(en.app.relay_session_ended)
    expect(offlineReason('refused', false)).toBe(en.app.relay_access_refused)
    expect(offlineReason(null, false)).toBe(en.app.gateway_offline_reconnecting)
  })

  it('says only "Reconnecting" for a plain drop in a relay Crew window, and keeps the relay reasons', () => {
    expect(offlineReason(null, true)).toBe(en.app.relay_crew_reconnecting)
    expect(en.app.relay_crew_reconnecting).toBe('Reconnecting…')
    expect(offlineReason('session_ended', true)).toBe(en.app.relay_session_ended)
    expect(offlineReason('refused', true)).toBe(en.app.relay_access_refused)
  })

  it('reads the relay Crew window from its user-agent marker only', () => {
    expect(isRelayCrewWindow('Mozilla/5.0 (Macintosh) Electron/44 kiroCrewRelayWindow')).toBe(true)
    expect(isRelayCrewWindow('Mozilla/5.0 (Macintosh) Electron/44')).toBe(false)
    expect(isRelayCrewWindow('Mozilla/5.0 NotkiroCrewRelayWindowAtAll')).toBe(false)
    expect(isRelayCrewWindow('')).toBe(false)
    // The test runner is a plain dashboard, as a local or Tailscale one is.
    expect(isRelayCrewWindow()).toBe(false)
    expect(offlineReason(null)).toBe(en.app.gateway_offline_reconnecting)
  })
})
