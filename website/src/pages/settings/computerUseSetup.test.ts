import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { setupDoc } from '../../test/computerUseSetupFixtures'
import {
  PANE_URL,
  SETUP_INSTALL_POLL_MAX_MS,
  SETUP_POLL_MAX_MS,
  SETUP_POLL_MS,
  SETUP_STATES,
  grantRecheckInterval,
  readinessFor,
  revokedPane,
  setupPollInterval,
  stepsFor,
  viewFor,
  waitExpired,
  waitingPane,
} from './computerUseSetup'

describe('computerUseSetup', () => {
  it('covers every contract state with a view', () => {
    expect(SETUP_STATES).toHaveLength(16)
    for (const state of SETUP_STATES) expect(viewFor(setupDoc(state))).toBeTruthy()
  })

  it('an installed driver that was never checked asks for the check, not another install', () => {
    const installed = setupDoc('not_configured', {
      platform: 'linux',
      steps: [
        { id: 'install', status: 'done' },
        { id: 'session', status: 'done' },
        { id: 'verify', status: 'current' },
        { id: 'ready', status: 'pending' },
      ],
    })
    expect(viewFor(installed)).toBe('verify')
    const fresh = setupDoc('not_configured', { platform: 'linux', steps: [{ id: 'install', status: 'current' }] })
    expect(viewFor(fresh)).toBe('install')
  })

  it('only the ready state maps to the ready view', () => {
    for (const state of SETUP_STATES) {
      expect(viewFor(setupDoc(state)) === 'ready').toBe(state === 'ready')
    }
  })

  it('pins the two pane URLs to the desktop allowlist', () => {
    const allowlist = readFileSync(resolve(__dirname, '../../../electron/external-scheme.js'), 'utf8')
    expect(allowlist).toContain(PANE_URL.accessibility)
    expect(allowlist).toContain(PANE_URL.screen_recording)
  })

  describe('setupPollInterval', () => {
    const t0 = 1_000_000
    it('polls moving states, inside the bound only', () => {
      for (const state of ['installing', 'validating'] as const) {
        expect(setupPollInterval(state, false, t0, t0)).toBe(SETUP_POLL_MS)
      }
      expect(setupPollInterval('validating', false, t0, t0 + SETUP_POLL_MAX_MS)).toBe(false)
      expect(setupPollInterval('installing', false, t0, t0 + SETUP_POLL_MAX_MS)).toBe(SETUP_POLL_MS)
      expect(setupPollInterval('installing', false, t0, t0 + SETUP_INSTALL_POLL_MAX_MS)).toBe(false)
    })
    it('answers a grant wait with a recheck, never a plain read', () => {
      // A read returns the recorded grants; only a recheck asks the driver.
      expect(setupPollInterval('waiting_for_system_settings', false, t0, t0)).toBe(false)
      expect(grantRecheckInterval('waiting_for_system_settings', false, t0, t0)).toBe(SETUP_POLL_MS)
      expect(setupPollInterval('requesting_accessibility', true, t0, t0)).toBe(false)
      expect(grantRecheckInterval('requesting_accessibility', false, t0, t0)).toBe(false)
      expect(grantRecheckInterval('requesting_accessibility', true, t0, t0)).toBe(SETUP_POLL_MS)
      expect(grantRecheckInterval('screen_recording_denied', true, t0, t0 + SETUP_POLL_MAX_MS)).toBe(false)
      expect(grantRecheckInterval('ready', true, t0, t0)).toBe(false)
    })
    it('a grant wait expires at the same bound its recheck stops', () => {
      const end = t0 + SETUP_POLL_MAX_MS
      expect(waitExpired('requesting_accessibility', t0, end - 1, true)).toBe(false)
      expect(waitExpired('requesting_accessibility', t0, end, true)).toBe(true)
      expect(grantRecheckInterval('requesting_accessibility', true, t0, end)).toBe(false)
      // Without the user in System Settings there is no wait to expire.
      expect(waitExpired('requesting_accessibility', t0, end, false)).toBe(false)
    })
    it('never polls a resting state', () => {
      for (const state of ['disabled', 'not_configured', 'ready', 'error', 'unsupported', 'restart_required', 'install_failed'] as const) {
        expect(setupPollInterval(state, true, t0, t0)).toBe(false)
      }
      expect(setupPollInterval(undefined, true, t0, t0)).toBe(false)
    })
    it('waitExpired agrees with the poll bound', () => {
      expect(waitExpired('validating', t0, t0 + SETUP_POLL_MAX_MS - 1)).toBe(false)
      expect(waitExpired('validating', t0, t0 + SETUP_POLL_MAX_MS)).toBe(true)
      expect(waitExpired('ready', t0, t0 + 10 * SETUP_POLL_MAX_MS)).toBe(false)
    })
  })

  it('falls back to the platform walk when the document carries no steps', () => {
    expect(stepsFor(setupDoc('error', { platform: 'linux' })).map(s => s.id)).toEqual(['install', 'session', 'verify', 'ready'])
    expect(stepsFor(setupDoc('error')).map(s => s.id)).toEqual(['install', 'session', 'verify', 'ready'])
    // macOS walks the built-in driver's check only: no install step.
    expect(stepsFor(setupDoc('error', { platform: 'macos', driver: { kind: 'native' } })).map(s => s.id)).toEqual(['verify', 'ready'])
  })

  it('points a revoked or waiting state at the missing grant', () => {
    expect(revokedPane(setupDoc('permission_revoked', { permissions: { screen_recording: 'missing' } }))).toBe('screen_recording')
    expect(revokedPane(setupDoc('permission_revoked', { permissions: { accessibility: 'missing', screen_recording: 'missing' } }))).toBe('accessibility')
    expect(
      waitingPane(setupDoc('waiting_for_system_settings', { steps: [{ id: 'accessibility', status: 'done' }, { id: 'screen_recording', status: 'current' }] })),
    ).toBe('screen_recording')
  })

  it.each([
    ['plain ready', setupDoc('ready'), 'ready'],
    ['ready with a problem', setupDoc('ready', { problem: { code: 'action_failed', message: 'x', recoverable: true } }), 'attention'],
    [
      'built-in macOS, a permission missing',
      setupDoc('ready', { platform: 'macos', driver: { kind: 'native' }, permissions: { accessibility: 'missing', screen_recording: 'granted' } }),
      'needs_permission',
    ],
    [
      'built-in macOS, screen recording missing',
      setupDoc('ready', { platform: 'macos', driver: { kind: 'native' }, permissions: { accessibility: 'granted', screen_recording: 'missing' } }),
      'needs_permission',
    ],
    [
      'built-in macOS, both unknown',
      setupDoc('ready', { platform: 'macos', driver: { kind: 'native' }, permissions: { accessibility: 'unknown', screen_recording: 'unknown' } }),
      'ready',
    ],
    ['open-source blocked, built-in in use', setupDoc('ready', { blocked: 'access denied', driver: { kind: 'native' } }), 'ready_builtin'],
    ['disabled', setupDoc('disabled'), 'off'],
    ['not configured', setupDoc('not_configured'), 'needs_setup'],
    ['error', setupDoc('error', { problem: { code: 'verify_failed', message: 'x', recoverable: true } }), 'attention'],
  ] as const)('derives the badge for %s', (_name, doc, want) => {
    expect(readinessFor(doc)).toBe(want)
  })

  it('summarizes readiness for the status card', () => {
    expect(readinessFor(setupDoc('disabled'))).toBe('off')
    expect(readinessFor(setupDoc('ready'))).toBe('ready')
    expect(readinessFor(setupDoc('unsupported'))).toBe('unavailable')
    // The open-source driver chosen on a Mac: the Mac works, the choice needs changing.
    expect(readinessFor(setupDoc('unsupported', { platform: 'macos', driver: { kind: 'upstream' } }))).toBe('attention')
    expect(readinessFor(setupDoc('unsupported', { platform: 'macos', driver: { kind: 'native' } }))).toBe('unavailable')
    expect(readinessFor(setupDoc('permission_revoked'))).toBe('attention')
    expect(readinessFor(setupDoc('requesting_accessibility'))).toBe('needs_setup')
  })
})
