/**
 * Tests for session-control discovery (app.json → contributes.sessionControls).
 *
 * Weighted toward the rules that decide whether a third-party chip reaches the
 * composer at all: this code runs on the path of every turn, so a malformed or
 * unknown declaration must be dropped rather than rendered.
 */
import { describe, it, expect } from 'vitest'
import { resolveSessionControls, MAX_INLINE_SESSION_CONTROLS, normalizeStatus, safeStatusPath } from '../hooks/useSessionControls'

const ctl = (over: Record<string, unknown> = {}) => ({
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Shield',
  ...over,
})

const app = (over: Record<string, unknown> = {}, controls = [ctl()]) => ({
  name: 'test-app',
  version: '0.1.0',
  displayName: 'Test App',
  enabled: true,
  manifest: {
    version: '0.1.0',
    displayName: 'Test App',
    contributes: { sessionControls: controls },
    permissions: { api: ['/api/apps/test-app'], events: ['notification'] },
  },
  ...over,
})

describe('resolveSessionControls', () => {
  it('resolves a declared control with a composite key', () => {
    const [c] = resolveSessionControls([app()])
    expect(c.key).toBe('test-app:scope')
    expect(c.entryPoint).toBe('dist/session-control.mjs')
    expect(c.label).toBe('Scope')
    expect(c.appDisplayName).toBe('Test App')
  })

  it('forwards the app permission lists for AppApiProvider', () => {
    const [c] = resolveSessionControls([app()])
    expect(c.allowedApi).toEqual(['/api/apps/test-app'])
    expect(c.allowedEvents).toEqual(['notification'])
  })

  it('falls back to the id when no label is declared', () => {
    const [c] = resolveSessionControls([app({}, [ctl({ label: undefined })])])
    expect(c.label).toBe('scope')
  })

  it('skips disabled apps', () => {
    expect(resolveSessionControls([app({ enabled: false })])).toEqual([])
  })

  it('treats an absent enabled flag as enabled, matching AppHost', () => {
    expect(resolveSessionControls([app({ enabled: undefined })])).toHaveLength(1)
  })

  it('ignores apps that declare no controls', () => {
    expect(resolveSessionControls([{ name: 'x', enabled: true, manifest: {} }])).toEqual([])
  })

  it('requires both id and entryPoint', () => {
    expect(resolveSessionControls([app({}, [ctl({ id: '' })])])).toEqual([])
    expect(resolveSessionControls([app({}, [ctl({ entryPoint: '' })])])).toEqual([])
  })

  it('marks a control process-backed when its app runs its own backend', () => {
    // The two backend kinds are served at different prefixes, so this flag is
    // what stops a process-backed app's chip being permanently stateless.
    const hooked = resolveSessionControls([app({}, [ctl({})])])
    expect(hooked[0].processBacked).toBe(false)

    const processed = resolveSessionControls([
      { ...app({}, [ctl({})]), manifest: { ...app({}, [ctl({})]).manifest, backend: { entryPoint: 'server.py' } } },
    ])
    expect(processed[0].processBacked).toBe(true)
  })

  it('treats an empty or absent backend entryPoint as hook-backed', () => {
    for (const backend of [undefined, {}, { entryPoint: '' }]) {
      const base = app({}, [ctl({})])
      const r = resolveSessionControls([
        { ...base, manifest: { ...base.manifest, backend } },
      ])
      expect(r[0].processBacked).toBe(false)
    }
  })

  it('survives junk entries without throwing', () => {
    const apps = [
      null,
      { name: '' },
      { name: 'a', enabled: true, manifest: { contributes: { sessionControls: 'nope' } } },
      { name: 'b', enabled: true, manifest: { contributes: { sessionControls: [null, 7, ctl()] } } },
    ]
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const out = resolveSessionControls(apps as any)
    expect(out.map(c => c.key)).toEqual(['b:scope'])
  })

  it('handles a non-array input', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(resolveSessionControls(undefined as any)).toEqual([])
  })

  it('caps the total rendered inline across all apps', () => {
    const many = Array.from({ length: MAX_INLINE_SESSION_CONTROLS + 3 }, (_, i) => ({
      ...app({ name: `app-${i}` }),
      name: `app-${i}`,
    }))
    expect(resolveSessionControls(many)).toHaveLength(MAX_INLINE_SESSION_CONTROLS)
  })

  it('orders stably so chips do not reshuffle between loads', () => {
    const a = app({ name: 'zeta' })
    const b = app({ name: 'alpha' })
    expect(resolveSessionControls([a, b]).map(c => c.appName)).toEqual(['alpha', 'zeta'])
    expect(resolveSessionControls([b, a]).map(c => c.appName)).toEqual(['alpha', 'zeta'])
  })

  it('drops a duplicate id within one app rather than emitting a colliding key', () => {
    // Regression for AutoSDE f-e69f4376. appName makes the key unique BETWEEN
    // apps; nothing made it unique WITHIN one. Two controls sharing an id
    // produced identical keys, which deduped their status probes into a single
    // query and collided their React list keys. The server rejects this at
    // install, but this function's contract is to survive a hand-edited manifest.
    const out = resolveSessionControls([
      app({}, [ctl({ label: 'First' }), ctl({ label: 'Second (duplicate id)' })]),
    ])
    expect(out).toHaveLength(1)
    expect(out[0].label).toBe('First')
    expect(new Set(out.map(c => c.key)).size).toBe(out.length)
  })

  it('keeps two controls from one app distinct', () => {
    const out = resolveSessionControls([
      app({}, [ctl(), ctl({ id: 'other-thing', label: 'Other' })]),
    ])
    expect(out.map(c => c.key)).toEqual([
      'test-app:other-thing',
      'test-app:scope',
    ])
  })
})

describe('normalizeStatus', () => {
  it('accepts the three known states', () => {
    expect(normalizeStatus({ state: 'ok' }).state).toBe('ok')
    expect(normalizeStatus({ state: 'warn' }).state).toBe('warn')
    expect(normalizeStatus({ state: 'none' }).state).toBe('none')
  })

  it('treats an unknown state as none rather than rendering it', () => {
    // An app is a third party: a newer manifest must not colour the chip with
    // a state this frontend has no rendering for.
    expect(normalizeStatus({ state: 'critical' }).state).toBe('none')
    expect(normalizeStatus({ state: 42 }).state).toBe('none')
  })

  it('falls back to none for malformed payloads', () => {
    expect(normalizeStatus(null).state).toBe('none')
    expect(normalizeStatus(undefined).state).toBe('none')
    expect(normalizeStatus('nope').state).toBe('none')
    expect(normalizeStatus({}).state).toBe('none')
  })

  it('carries a tooltip and bounds its length', () => {
    expect(normalizeStatus({ state: 'ok', tooltip: 'Scope: X' }).tooltip).toBe('Scope: X')
    expect(normalizeStatus({ state: 'ok', tooltip: 'y'.repeat(500) }).tooltip).toHaveLength(200)
  })

  it('ignores a non-string tooltip', () => {
    expect(normalizeStatus({ state: 'ok', tooltip: { a: 1 } }).tooltip).toBe('')
  })
})

describe('resolveSessionControls — statusPath', () => {
  const app = (ctl: Record<string, unknown>) => [
    { name: 'demo', manifest: { contributes: { sessionControls: [{ id: 'c', entryPoint: 'c.mjs', ...ctl }] } } },
  ]

  it('is empty when the app declares none', () => {
    expect(resolveSessionControls(app({}))[0].statusPath).toBe('')
  })

  it('is carried through when declared', () => {
    expect(resolveSessionControls(app({ statusPath: 'session-status' }))[0].statusPath).toBe(
      'session-status',
    )
  })

  it('strips a single leading slash so the fetch never doubles them', () => {
    expect(resolveSessionControls(app({ statusPath: '/session-status' }))[0].statusPath).toBe(
      'session-status',
    )
  })

  it('refuses a protocol-relative path rather than stripping it into a route', () => {
    // `//session-status` is syntactically a host, not a path. Stripping the
    // slashes would accept a cross-origin declaration by rewriting it, so both
    // this layer and the backend refuse it.
    expect(resolveSessionControls(app({ statusPath: '//session-status' }))[0].statusPath).toBe('')
  })

  it('ignores a non-string statusPath', () => {
    expect(resolveSessionControls(app({ statusPath: 7 }))[0].statusPath).toBe('')
  })
})

describe('safeStatusPath — the frontend half of the statusPath guard', () => {
  // The backend validates this at install time, so a conforming install cannot
  // arrive here with anything else. This exists because the hook interpolates
  // the value into a fetch URL and its docstring claims to survive a stale or
  // hand-edited manifest — these tests are the difference between that being
  // true and merely being claimed.

  it('accepts a plain relative route', () => {
    expect(safeStatusPath('session-status')).toBe('session-status')
    expect(safeStatusPath('a/b/c')).toBe('a/b/c')
    expect(safeStatusPath('/session-status')).toBe('session-status')
  })

  it('refuses traversal into another app', () => {
    // `.` is outside the character class, so `..` is unrepresentable rather
    // than merely rejected. Unfixed, this reached /api/apps/other-app/secret.
    expect(safeStatusPath('x/../../other-app/secret')).toBe('')
    expect(safeStatusPath('..')).toBe('')
    expect(safeStatusPath('a/../b')).toBe('')
  })

  it('refuses anything that would corrupt the appended query string', () => {
    expect(safeStatusPath('status?session=evil')).toBe('')
    expect(safeStatusPath('status#frag')).toBe('')
  })

  it('refuses another origin', () => {
    expect(safeStatusPath('https://evil.example/x')).toBe('')
    // Protocol-relative, including the dotless host that the charset alone
    // would have let through as a plausible relative route. Matches the
    // backend, which refuses it at install time.
    expect(safeStatusPath('//evilhost/x')).toBe('')
    expect(safeStatusPath('//evil.example/x')).toBe('')
  })

  it('mirrors the backend charset and length bound', () => {
    expect(safeStatusPath('Status')).toBe('')
    expect(safeStatusPath('-leading-dash')).toBe('')
    expect(safeStatusPath('x'.repeat(70))).toBe('')
    expect(safeStatusPath('x'.repeat(64))).toBe('x'.repeat(64))
  })

  it('refuses a non-string', () => {
    expect(safeStatusPath(7)).toBe('')
    expect(safeStatusPath(null)).toBe('')
    expect(safeStatusPath(undefined)).toBe('')
    expect(safeStatusPath({})).toBe('')
  })
})
