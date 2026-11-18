import { describe, it, expect, vi } from 'vitest'
import type { NavigateFunction } from 'react-router-dom'

import { createPagesProvider } from '../../components/commandPalette/providers/pagesProvider'
import { getAdvertisedSurfaces } from '../../surfaces/registry'
// Registers the builtin surfaces, exactly as the app shell does at boot.
import '../../surfaces/builtins'

/**
 * ⌘K reachability for Home against the REAL surface registry (no mocks).
 *
 * `pagesProvider.test.ts` stubs the registry, so it can not notice if the
 * `home` surface were ever hidden from nav or gated behind a preview flag.
 * This test pins the contract the Home page relies on: it is advertised, so
 * Search Everywhere lists it, and Enter lands on `/home`.
 */
describe('Home in the command palette', () => {
  it('is an advertised surface', () => {
    const home = getAdvertisedSurfaces().find((s) => s.navId === 'home')
    expect(home?.route).toBe('/home')
  })

  it('is found by ⌘K "home" and Enter navigates to /home', async () => {
    const spy = vi.fn()
    const provider = createPagesProvider(spy as unknown as NavigateFunction)
    const results = await Promise.resolve(provider.search('home'))
    const hit = results.find((r) => r.subtitle === '/home')
    expect(hit, 'palette lists Home').toBeTruthy()
    hit!.onActivate()
    expect(spy).toHaveBeenCalledWith('/home')
  })
})
