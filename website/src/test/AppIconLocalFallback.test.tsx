/**
 * Local-art fallback for a failed registry icon (#6804).
 *
 * The detail page of an INSTALLED app renders the registry's content-addressed
 * icon as the primary `src` (immutable, cacheable forever) and must NOT flip
 * that precedence — but when the registry asset fails to LOAD (offline, captive
 * portal, blocked host) the app's own bytes are on local disk, so the icon must
 * swap to the local install route instead of degrading to the generic glyph.
 *
 * Per the issue's testing note these tests FIRE the element's `error` event and
 * assert the rendered `src` actually changed — asserting a handler is attached
 * proves nothing. The suite also locks the default-inert contract: consumers
 * that do not pass a fallback (every pre-existing call site) are unchanged.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AppIcon from '../components/AppIcon'
import AppListRow from '../components/appstore/AppListRow'
import type { RegistryApp } from '../components/appstore/types'

const theme = { current: 'light' as 'light' | 'dark' }
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: theme.current }) }))

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

const REGISTRY = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Ficon.png'
const LOCAL = '/apps/demo-app/art/assets/icon.png'
const LOCAL_DARK = '/apps/demo-app/art/assets/icon-dark.png'

function imgBySrc(src: string): HTMLImageElement | null {
  return document.querySelector(`img[src="${src}"]`)
}

describe('AppIcon — local-art fallback on load failure', () => {
  beforeEach(() => { theme.current = 'light' })

  it('swaps src to the fallback when the primary errors', () => {
    render(<AppIcon iconUrl={REGISTRY} iconUrlFallback={LOCAL} />)
    const img = imgBySrc(REGISTRY)
    expect(img).not.toBeNull()
    fireEvent.error(img!)
    expect(imgBySrc(REGISTRY)).toBeNull()
    expect(imgBySrc(LOCAL)).not.toBeNull()
  })

  it('degrades to the lucide glyph when the fallback also errors — no broken frame left', () => {
    const { container } = render(<AppIcon iconUrl={REGISTRY} iconUrlFallback={LOCAL} />)
    fireEvent.error(imgBySrc(REGISTRY)!)
    fireEvent.error(imgBySrc(LOCAL)!)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('a primary failure with no fallback goes straight to the glyph (not-installed row unchanged)', () => {
    const { container } = render(<AppIcon iconUrl={REGISTRY} />)
    fireEvent.error(imgBySrc(REGISTRY)!)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('skips a fallback identical to the failed primary rather than retrying it', () => {
    const { container } = render(<AppIcon iconUrl={LOCAL} iconUrlFallback={LOCAL} />)
    fireEvent.error(imgBySrc(LOCAL)!)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('resolves the dark fallback variant in dark mode', () => {
    theme.current = 'dark'
    render(<AppIcon iconUrl={REGISTRY} iconUrlFallback={LOCAL} iconUrlFallbackDark={LOCAL_DARK} />)
    fireEvent.error(imgBySrc(REGISTRY)!)
    expect(imgBySrc(LOCAL_DARK)).not.toBeNull()
  })

  it('falls back across themes within the fallback pair (dark mode, light-only fallback)', () => {
    theme.current = 'dark'
    render(<AppIcon iconUrl={REGISTRY} iconUrlFallback={LOCAL} />)
    fireEvent.error(imgBySrc(REGISTRY)!)
    expect(imgBySrc(LOCAL)).not.toBeNull()
  })

  it('a changed icon clears BOTH failure latches', async () => {
    const NEXT = '/api/apps/blob?repo=demo%2Fdemo-app&path=assets%2Ficon-v2.png'
    const { rerender } = render(<AppIcon iconUrl={REGISTRY} iconUrlFallback={LOCAL} />)
    fireEvent.error(imgBySrc(REGISTRY)!)
    fireEvent.error(imgBySrc(LOCAL)!)
    expect(document.querySelector('img')).toBeNull()
    // The app updated: the primary URL changed, and the local file was
    // rewritten in place under the SAME url. Both latches must clear, so the
    // new primary renders, and on failure the fallback gets retried too.
    rerender(<AppIcon iconUrl={NEXT} iconUrlFallback={LOCAL} />)
    await waitFor(() => expect(imgBySrc(NEXT)).not.toBeNull())
    fireEvent.error(imgBySrc(NEXT)!)
    expect(imgBySrc(LOCAL)).not.toBeNull()
  })
})

describe('AppDetailPage — installed app icon falls back to local art (#6804)', () => {
  beforeEach(() => {
    theme.current = 'light'
    getApp.mockReset()
    listRegistry.mockReset()
    system.mockReset()
    system.mockResolvedValue({ hostname: '' })
  })

  function renderDetail(name = 'demo-app') {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/apps/detail/${name}`]}>
          <Routes>
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps" element={<div>apps list</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  const INSTALLED = {
    name: 'demo-app',
    version: '1.0.0',
    displayName: 'Demo App',
    enabled: true,
    origin: 'registry',
    resources: 'app',
    lifecycle: 'app',
    installed: true,
    manifest: {
      displayName: 'Demo App',
      description: 'An installed registry app',
      iconPath: 'assets/icon.png',
    },
  }
  const REGISTRY_ROW = {
    name: 'demo-app',
    displayName: 'Demo App',
    version: '1.0.0',
    author: 'demo',
    iconUrl: REGISTRY,
  }

  it('registry icon error swaps the rendered src to the local install route, then degrades to the glyph', async () => {
    getApp.mockResolvedValue(INSTALLED)
    listRegistry.mockResolvedValue({ apps: [REGISTRY_ROW], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    renderDetail()

    // The registry asset stays the PRIMARY src — no precedence flip.
    const primary = await waitFor(() => {
      const el = imgBySrc(REGISTRY)
      expect(el).not.toBeNull()
      return el!
    })

    // Registry CDN unreachable: the src must actually CHANGE to the local route.
    fireEvent.error(primary)
    await waitFor(() => expect(imgBySrc(LOCAL)).not.toBeNull())
    expect(imgBySrc(REGISTRY)).toBeNull()

    // Local bytes gone too (uninstalled mid-render): terminal state is the
    // glyph, with no broken-image frame left in the icon box.
    fireEvent.error(imgBySrc(LOCAL)!)
    const box = document.querySelector('.w-24.h-24')!
    await waitFor(() => expect(box.querySelector('img')).toBeNull())
    expect(box.querySelector('svg')).not.toBeNull()
  })
})

describe('AppListRow — untouched consumer stays default-inert', () => {
  it('a failed row icon degrades straight to the glyph, attempting no fallback', () => {
    const app = {
      name: 'demo-app',
      displayName: 'Demo App',
      description: 'row',
      version: '1.0.0',
      author: 'demo',
      iconUrl: REGISTRY,
      tags: [],
      installed: false,
    } as unknown as RegistryApp
    const { container } = render(
      <MemoryRouter>
        <AppListRow app={app} onOpen={() => {}} onGet={() => {}} onUpdate={() => {}} onEnable={() => {}} />
      </MemoryRouter>,
    )
    const img = imgBySrc(REGISTRY)
    expect(img).not.toBeNull()
    fireEvent.error(img!)
    // No second <img> attempt anywhere in the row; AppIcon's glyph shows.
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('Demo App')).toBeInTheDocument()
  })
})
