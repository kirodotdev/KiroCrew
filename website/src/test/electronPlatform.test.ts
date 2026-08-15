/**
 * Framed-Linux derivation — the OTHER half of the Linux shell contract.
 *
 * App.linuxElectron.test.tsx locks the FRAMELESS window's layout (inset
 * class + caption-control corner). This test locks the framed case at the
 * derivation level: a Linux Electron shell whose preload does NOT report
 * `linuxFrameless` (native frame kept — SSD desktop, X11 session, or the
 * operator override) must never claim the frameless const, so no header
 * inset class and no reserved corner apply. Module-level on purpose: the
 * consts are computed once at module load from `window.kirocrew`, so the
 * derivation itself is what's under test, without duplicating the full App
 * mock harness.
 */
import { describe, it, expect, vi } from 'vitest'

// Must run before src/lib/electron.ts is imported (module-level consts).
vi.hoisted(() => {
  ;(window as unknown as { kirocrew: object }).kirocrew = {
    isElectron: true,
    platform: 'linux',
    linuxFrameless: false,
  }
})

import { isElectron, isLinuxFramelessElectron, isMacElectron, isWinElectron } from '../lib/electron'

describe('lib/electron — framed Linux window derivation', () => {
  it('a Linux shell without the linuxFrameless flag is Electron but NOT frameless-Linux', () => {
    expect(isElectron).toBe(true)
    expect(isLinuxFramelessElectron).toBe(false)
    expect(isMacElectron).toBe(false)
    expect(isWinElectron).toBe(false)
  })
})
