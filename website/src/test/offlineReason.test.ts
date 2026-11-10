import { describe, it, expect } from 'vitest'
import { offlineReason } from '../shell/appShell'
import en from '../i18n/locales/en.manual.json'

describe('offlineReason', () => {
  it('names the relay reason, and keeps the generic wording for a plain drop', () => {
    expect(offlineReason('session_ended')).toBe(en.app.relay_session_ended)
    expect(offlineReason('refused')).toBe(en.app.relay_access_refused)
    expect(offlineReason(null)).toBe(en.app.gateway_offline_reconnecting)
  })
})
