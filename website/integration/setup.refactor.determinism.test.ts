import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import actWarningBaseline from './act-warning-baseline.json'
import { STORAGE_CARRYOVER_FILES, isActWarning } from './setup'

// Self-tests for the determinism pieces of integration/setup.ts.

describe('the act() warning guard', () => {
  it('never throws on an argument String() cannot convert', () => {
    const revoked = Proxy.revocable({}, {})
    revoked.revoke()
    const hostile = {
      toString() {
        throw new Error('no string form')
      },
    }
    expect(() => isActWarning([Object.create(null), revoked.proxy, hostile])).not.toThrow()
    expect(() => console.error('x', Object.create(null))).not.toThrow()
  })

  it('recognises the warning by a string argument only', () => {
    const warning = 'Warning: An update to %s inside a test was not wrapped in act(...).'
    expect(isActWarning([warning, 'Probe'])).toBe(true)
    expect(isActWarning([new Error('not wrapped in act(')])).toBe(false)
  })
})

describe('the shrink-only lists', () => {
  it('keeps the act baseline sorted, unique, existing and no longer than its census', () => {
    expect([...actWarningBaseline].sort()).toEqual(actWarningBaseline)
    expect(new Set(actWarningBaseline).size).toBe(actWarningBaseline.length)
    expect(actWarningBaseline.length).toBeLessThanOrEqual(475)
    for (const file of actWarningBaseline) expect(existsSync(resolve(process.cwd(), file)), file).toBe(true)
  })

  it('keeps the storage carry-over list at its one file', () => {
    expect(STORAGE_CARRYOVER_FILES.size).toBeLessThanOrEqual(1)
  })
})

describe('each test undoes the fake timers and storage writes it leaves', () => {
  it('leaves fake timers installed and a storage write behind', () => {
    vi.useFakeTimers() // flake-ok: the leak this file proves setup.ts undoes
    localStorage.setItem('setup-determinism-probe', 'left behind')
    expect(vi.isFakeTimers()).toBe(true)
  })

  it('starts with real timers and without that write', () => {
    expect(vi.isFakeTimers()).toBe(false)
    expect(localStorage.getItem('setup-determinism-probe')).toBeNull()
  })
})
