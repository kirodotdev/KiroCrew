/**
 * System Monitor surface registration + route wiring.
 *
 * The Chat Resource Monitor is reached two ways, and each fails silently on its
 * own if the wiring is wrong:
 *
 *  - as a REGISTERED SURFACE, so the left rail and Search Everywhere both offer
 *    it (they read the same registry), its label translates, and its route is
 *    unique; and
 *  - as a ROUTED PAGE at `/monitor`, so following the surface's route actually
 *    lands on SystemMonitorPage.
 *
 * The surface half is asserted against the real registry (importing
 * `../surfaces/builtins` for its registration side effect); the route half
 * renders the page the App route mounts, with the snapshot API stubbed so the
 * assertion is network-free.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

// The page fetches its snapshot from `api.chatResources`; stub it so rendering
// the route is network-free and deterministic.
vi.mock('../api/client', () => ({
  api: {
    chatResources: vi.fn(),
    stopChatSlotIfPid: vi.fn(),
    spawnCancel: vi.fn(),
  },
}))

import { api } from '../api/client'
// Side-effect import registers every built-in surface, including `monitor`.
import '../surfaces/builtins'
import {
  getBuiltinSurface,
  getBuiltinSurfaces,
  getAdvertisedSurfaces,
  surfaceLabel,
} from '../surfaces/registry'
import { i18next } from '../i18n/all'
import SystemMonitorPage from '../pages/SystemMonitorPage'

const chatResources = api.chatResources as ReturnType<typeof vi.fn>

describe('System Monitor surface registration', () => {
  it('registers the monitor surface with the /monitor route in the Main group', () => {
    const s = getBuiltinSurface('monitor')
    expect(s, 'monitor surface is not registered').toBeDefined()
    expect(s!.route).toBe('/monitor')
    expect(s!.group).toBe('Main')
    // Reuses the page's own catalog key rather than minting a nav.* twin — the
    // rail row and the page it opens are one destination.
    expect(s!.labelKey).toBe('monitor.title')
  })

  it('advertises the surface (no preview gate) so the rail and palette both offer it', () => {
    // getBuiltinSurfaces() is the "what is registered" list; getAdvertisedSurfaces()
    // is the "what a user may see" list. An un-gated surface must appear in BOTH,
    // or it is registered but reachable from nowhere.
    expect(getBuiltinSurfaces().some(s => s.navId === 'monitor')).toBe(true)
    expect(getAdvertisedSurfaces().some(s => s.navId === 'monitor')).toBe(true)
  })

  it('resolves its label to real copy in English, not a raw key', () => {
    const s = getBuiltinSurface('monitor')!
    const label = surfaceLabel(s)
    expect(label).toBe(i18next.t('monitor.title'))
    expect(label).toBe('System Monitor')
    // A leaked raw key would still be truthy; assert it is not the key itself.
    expect(label).not.toBe('monitor.title')
  })

  it('keeps its route unique — no other surface claims /monitor', () => {
    const owners = getBuiltinSurfaces().filter(s => s.route === '/monitor')
    expect(owners.map(s => s.navId)).toEqual(['monitor'])
  })
})

describe('System Monitor route', () => {
  beforeEach(() => {
    chatResources.mockReset()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  })

  it('renders SystemMonitorPage at the surface route', async () => {
    // Mount the page the App `<Route path="/monitor">` mounts, at that path, and
    // prove it renders its header strip once the first snapshot lands.
    chatResources.mockResolvedValue({
      entries: [],
      posture: 'ample',
      available_gb: 12,
      host_total_gb: 32,
      cpu_count: 8,
      cgroup_used_gb: null,
      cgroup_limit_gb: null,
      sampling_supported: true,
      captured_at: 1_700_000_000,
      interval_s: 2,
    })
    renderWithProviders(<SystemMonitorPage />, { route: getBuiltinSurface('monitor')!.route })
    expect(await screen.findByTestId('monitor-header')).toBeInTheDocument()
    // The page title comes from the same catalog key the surface labels with.
    expect(screen.getByText('System Monitor')).toBeInTheDocument()
  })
})
