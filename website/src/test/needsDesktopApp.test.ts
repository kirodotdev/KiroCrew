import { describe, it, expect } from 'vitest'
import { needsDesktopApp } from '../lib/electron'

// In the vitest (jsdom) environment there is no Electron preload, so `isElectron`
// is false — i.e. we are always "in a browser tab", which is exactly the surface
// whose enable action must surface the desktop requirement.
describe('needsDesktopApp', () => {
  it('reads the requirement from a top-level platform (catalog/registry shape)', () => {
    expect(needsDesktopApp({ platform: { requiresDesktopApp: true } })).toBe(true)
    expect(needsDesktopApp({ platform: { requiresDesktopApp: false } })).toBe(false)
  })

  it('also reads it from manifest.platform (installed-app shape)', () => {
    // The reported bug: an installed app carries the flag under manifest.platform,
    // so a top-level-only read left the requirement hidden and the enable action
    // handed over a window that cannot open.
    expect(needsDesktopApp({ manifest: { platform: { requiresDesktopApp: true } } })).toBe(true)
  })

  it('is false when neither location declares it', () => {
    expect(needsDesktopApp({})).toBe(false)
    expect(needsDesktopApp({ manifest: {} })).toBe(false)
    expect(needsDesktopApp({ platform: {}, manifest: { platform: {} } })).toBe(false)
  })
})
