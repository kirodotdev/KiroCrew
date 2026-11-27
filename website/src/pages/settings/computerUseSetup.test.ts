import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { grants, setupDoc } from '../../test/computerUseSetupFixtures'
import {
  PANE_URL,
  SETUP_INSTALL_POLL_MAX_MS,
  SETUP_POLL_MAX_MS,
  SETUP_POLL_MS,
  SETUP_STATES,
  readinessFor,
  setupPollInterval,
  stepsFor,
  viewFor,
  waitExpired,
} from './computerUseSetup'

describe('computerUseSetup', () => {
  it('covers every contract state with a view', () => {
    expect(new Set(SETUP_STATES).size).toBe(SETUP_STATES.length)
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

  it('a headless Linux gateway asks for a desktop session, not a driver restart', () => {
    // The backend reports no desktop session as temporarily_unavailable.
    const headless = setupDoc('temporarily_unavailable', {
      platform: 'linux',
      session: { kind: 'none' },
      problem: { code: 'no_desktop_session', message: 'no DISPLAY or WAYLAND_DISPLAY', recoverable: true },
    })
    expect(viewFor(headless)).toBe('no_desktop')
    expect(viewFor(setupDoc('temporarily_unavailable', { platform: 'windows', session: { kind: 'session0' } }))).toBe('session0')
    // A driver that stopped answering inside a desktop session, and a status read
    // that timed out before any platform was known, stay "not answering".
    expect(viewFor(setupDoc('temporarily_unavailable', { platform: 'linux', session: { kind: 'x11' } }))).toBe('unavailable')
    expect(viewFor(setupDoc('temporarily_unavailable', { platform: 'unknown', session: { kind: 'none' } }))).toBe('unavailable')
    // Unsupported never comes from a missing session: no native Linux driver is not a desktop problem.
    expect(viewFor(setupDoc('unsupported', { platform: 'linux', session: { kind: 'none' }, driver: { kind: 'native' } }))).toBe('unsupported')
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
        expect(setupPollInterval(state, t0, t0)).toBe(SETUP_POLL_MS)
      }
      expect(setupPollInterval('validating', t0, t0 + SETUP_POLL_MAX_MS)).toBe(false)
      expect(setupPollInterval('installing', t0, t0 + SETUP_POLL_MAX_MS)).toBe(SETUP_POLL_MS)
      expect(setupPollInterval('installing', t0, t0 + SETUP_INSTALL_POLL_MAX_MS)).toBe(false)
    })
    it('never polls a resting state', () => {
      for (const state of ['disabled', 'not_configured', 'ready', 'error', 'unsupported', 'temporarily_unavailable', 'install_failed'] as const) {
        expect(setupPollInterval(state, t0, t0)).toBe(false)
      }
      expect(setupPollInterval(undefined, t0, t0)).toBe(false)
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

  it.each([
    ['plain ready', setupDoc('ready'), grants(), 'ready'],
    ['ready with a problem', setupDoc('ready', { problem: { code: 'action_failed', message: 'x', recoverable: true } }), grants(), 'attention'],
    [
      'built-in macOS, a permission missing',
      setupDoc('ready', { platform: 'macos', driver: { kind: 'native' } }),
      grants({ accessibility: 'missing' }),
      'needs_permission',
    ],
    [
      'built-in macOS, screen recording missing',
      setupDoc('ready', { platform: 'macos', driver: { kind: 'native' } }),
      grants({ screen_recording: 'missing' }),
      'needs_permission',
    ],
    [
      'built-in macOS, both unknown',
      setupDoc('ready', { platform: 'macos', driver: { kind: 'native' } }),
      grants({ accessibility: 'unknown', screen_recording: 'unknown' }),
      'ready',
    ],
    // The badge has no separate "built-in" ready: the reason text says why.
    ['open-source blocked, built-in in use', setupDoc('ready', { blocked: 'access denied', driver: { kind: 'native' } }), grants(), 'ready'],
    // The grant rows belong to the built-in macOS driver only.
    ['Windows, a grant reads missing', setupDoc('ready', { driver: { kind: 'native' } }), grants({ accessibility: 'missing' }), 'ready'],
    ['disabled', setupDoc('disabled'), grants(), 'off'],
    ['not configured', setupDoc('not_configured'), grants(), 'needs_setup'],
    ['error', setupDoc('error', { problem: { code: 'verify_failed', message: 'x', recoverable: true } }), grants(), 'attention'],
  ] as const)('derives the badge for %s', (_name, doc, permissions, want) => {
    expect(readinessFor(doc, permissions)).toBe(want)
  })

  it('summarizes readiness for the status card', () => {
    expect(readinessFor(setupDoc('disabled'), grants())).toBe('off')
    expect(readinessFor(setupDoc('ready'), grants())).toBe('ready')
    expect(readinessFor(setupDoc('unsupported'), grants())).toBe('unavailable')
    // The open-source driver chosen on a Mac: the Mac works, the choice needs changing.
    expect(readinessFor(setupDoc('unsupported', { platform: 'macos', driver: { kind: 'upstream' } }), grants())).toBe('attention')
    expect(readinessFor(setupDoc('unsupported', { platform: 'macos', driver: { kind: 'native' } }), grants())).toBe('unavailable')
    expect(readinessFor(setupDoc('temporarily_unavailable'), grants())).toBe('attention')
    expect(readinessFor(setupDoc('install_failed'), grants())).toBe('attention')
    expect(readinessFor(setupDoc('installing'), grants())).toBe('needs_setup')
  })
})
