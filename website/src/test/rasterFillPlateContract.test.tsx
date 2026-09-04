/**
 * The `rasterFill` PLATE CONTRACT, pinned at every call site that passes it.
 *
 * `rasterFill` renders the icon as `absolute inset-0`, which makes two classes on
 * the surrounding plate load-bearing rather than cosmetic:
 *
 *  - `relative` — an unpositioned plate hands the image to the nearest positioned
 *    ancestor, so the icon leaves the plate entirely. On the store's spotlight
 *    that ancestor is the `relative aspect-[16/9]` art panel, so the icon would
 *    render as hero art; on the detail page there is no positioned ancestor above
 *    the hero row at all, so it escapes to a page-level box.
 *  - `overflow-hidden` — the clip is the only thing that makes a bled image take
 *    the plate's own radius. The spotlight plate did not need it while the icon
 *    was inset, so it was genuinely absent, not merely unstated.
 *
 * Neither failure is visible in a diff — a reviewer sees `rasterFill` added and
 * the plate unchanged, and both look fine. They are only visible in the render,
 * which is why this asserts on the rendered DOM (the img's actual parent) rather
 * than on source text, and why it covers ALL THREE call sites in one place: the
 * next surface to pass the flag has one obvious file to add itself to.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppIconTile from '../components/appstore/AppIconTile'
import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import type { RegistryApp } from '../components/appstore/types'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
  },
}))

import AppDetailPage from '../pages/AppDetailPage'

/** An installed app's own icon file, served from its install directory. */
const RASTER = '/apps/demo-app/art/assets/icon.webp'

/** The plate is the img's parent; both classes are asserted on that one box. */
function assertPlateContract(img: HTMLImageElement) {
  const plate = img.parentElement
  expect(plate).not.toBeNull()
  expect(plate!.className).toContain('relative')
  expect(plate!.className).toContain('overflow-hidden')
  // And the image really is the bled form, so the contract guards something.
  expect(img.className).toContain('absolute')
  expect(img.className).toContain('inset-0')
  expect(img.className).toContain('object-cover')
}

describe('rasterFill plate contract', () => {
  beforeEach(() => {
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
  })

  it('AppIconTile positions and clips its plate', () => {
    render(<AppIconTile name="demo-app" iconUrl={RASTER} />)
    assertPlateContract(screen.getByRole('presentation', { hidden: true }) as HTMLImageElement)
  })

  it('FeaturedSpotlight positions and clips the 92px plate, so the icon cannot fill the art panel', () => {
    // No hero art on this fixture, which is the branch that renders the plate.
    const lead = {
      name: 'demo-app',
      displayName: 'Demo App',
      description: 'Ships an icon file and no hero art.',
      author: 'Kiro Crew', // brand-ok: fixture author
      version: '1.0.0',
      tags: [],
      installed: false,
      updateAvailable: false,
      iconUrl: RASTER,
    } as unknown as RegistryApp
    const noop = () => {}
    render(
      <FeaturedSpotlight type="app" apps={[lead]} onOpenApp={noop} onGet={noop} onEnable={noop} />,
    )
    const img = [...document.querySelectorAll('img')].find(i => i.getAttribute('src') === RASTER)
    expect(img).toBeTruthy()
    assertPlateContract(img as HTMLImageElement)
  })

  it('AppDetailPage positions and clips the 96px hero plate', async () => {
    getApp.mockResolvedValue({
      name: 'demo-app',
      version: '1.0.0',
      displayName: 'Demo App',
      enabled: true,
      origin: 'registry',
      resources: 'app',
      lifecycle: 'app',
      installed: true,
      manifest: { displayName: 'Demo App', description: 'An installed app', iconPath: 'assets/icon.webp' },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/detail/demo-app']}>
          <Routes>
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps" element={<div>apps list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const img = await waitFor(() => {
      const el = [...document.querySelectorAll('img')].find(i => i.getAttribute('src') === RASTER)
      expect(el).toBeTruthy()
      return el as HTMLImageElement
    })
    assertPlateContract(img)
  })
})
