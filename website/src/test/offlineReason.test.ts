import { describe, it, expect } from 'vitest'
import { isRelayCrewPane, isRelayCrewWindow, isRelayRetrying, offlineCover, offlineReason } from '../utils/offlineReason'
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

  it('reads the relay Crew pane from its own user-agent marker only', () => {
    expect(isRelayCrewPane('Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane')).toBe(true)
    // The separate relay Crew window keeps its own header.
    expect(isRelayCrewPane('Mozilla/5.0 Electron/44 kiroCrewRelayWindow')).toBe(false)
    expect(isRelayCrewPane('Mozilla/5.0 NotkiroCrewRelayPaneAtAll')).toBe(false)
    expect(isRelayCrewPane('')).toBe(false)
    expect(isRelayCrewPane()).toBe(false)
  })

  it('treats only a plain drop in a relay Crew window as retrying', () => {
    expect(isRelayRetrying(null, true)).toBe(true)
    expect(isRelayRetrying('refused', true)).toBe(false)
    expect(isRelayRetrying('session_ended', true)).toBe(false)
    expect(isRelayRetrying(null, false)).toBe(false)
    expect(isRelayRetrying(null)).toBe(false)
  })

  describe('offlineCover', () => {
    const down = { connected: false, connecting: false, authRequired: false, cause: null, relayCrewWindow: false, relayCrewPane: false, hostname: 'localhost' } as const

    it('gives a relay Crew window whose host is gone the calm view, and keeps a real refusal red', () => {
      // The relay closes every viewer socket 4404 once the host connection ends;
      // that close leaves no relay reason (relayClose.test.ts), so the window
      // keeps retrying calmly.
      expect(offlineCover({ ...down, relayCrewWindow: true })).toBe('relay')
      expect(offlineCover({ ...down, relayCrewWindow: true, cause: 'refused' })).toBeNull()
      expect(offlineCover({ ...down, relayCrewWindow: true, cause: 'session_ended' })).toBeNull()
    })

    it('covers the owner\'s own dashboard on this machine while its gateway is gone', () => {
      expect(offlineCover(down)).toBe('host')
      expect(offlineCover({ ...down, hostname: '127.0.0.1' })).toBe('host')
      expect(offlineCover({ ...down, hostname: 'crew.tailnet.example' })).toBeNull()
      expect(offlineCover({ ...down, relayCrewPane: true })).toBeNull()
      expect(offlineCover({ ...down, cause: 'refused' })).toBeNull()
    })

    it('covers nothing while connected, still connecting, or signed out', () => {
      for (const relayCrewWindow of [false, true]) {
        expect(offlineCover({ ...down, relayCrewWindow, connected: true })).toBeNull()
        expect(offlineCover({ ...down, relayCrewWindow, connecting: true })).toBeNull()
        expect(offlineCover({ ...down, relayCrewWindow, authRequired: true })).toBeNull()
      }
    })
  })
})
