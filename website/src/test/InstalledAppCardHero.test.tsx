/**
 * InstalledAppCard hero art — the Library tab must render the same app art
 * Discover does, instead of the flat lucide icon it shipped with.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

vi.mock('../api/client', () => ({ api: { openApp: vi.fn() } }))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))
vi.mock('../components/AppIcon', () => ({
  default: ({ icon, iconUrl }: { icon?: string; iconUrl?: string }) => (
    <div data-testid="app-icon" data-icon={icon || ''} data-icon-url={iconUrl || ''} />
  ),
}))

import InstalledAppCard from '../components/appstore/InstalledAppCard'
import type { InstalledApp } from '../components/appstore/types'

function app(manifest: Partial<InstalledApp['manifest']> = {}): InstalledApp {
  return {
    name: 'dev-fleet',
    version: '1.0.0',
    displayName: 'Dev Fleet',
    enabled: true,
    installedAt: '2026-08-02T00:00:00Z',
    origin: 'builtin',
    manifest: {
      name: 'dev-fleet',
      version: '1.0.0',
      displayName: 'Dev Fleet',
      description: 'A control panel for working on KiroCrew itself.',
      author: 'kirocrew',
      ui: { pages: [{ route: '/dev-fleet', label: 'Dev Fleet', icon: 'Server' }] },
      ...manifest,
    },
  }
}

function renderCard(a: InstalledApp) {
  return render(
    <InstalledAppCard
      app={a}
      actionLoading={null}
      onAction={vi.fn()}
      onOpen={vi.fn()}
      onDetail={vi.fn()}
    />,
  )
}

describe('InstalledAppCard hero art', () => {
  it('renders the manifest hero image, preferring the current theme variant', () => {
    renderCard(app({
      heroImage: '/app-assets/dev-fleet/hero-light.svg',
      heroImageDark: '/app-assets/dev-fleet/hero-dark.svg',
    }))
    // useTheme is mocked to 'dark', so the dark variant wins.
    const img = document.querySelector('img')
    expect(img).toBeTruthy()
    expect(img!.getAttribute('src')).toBe('/app-assets/dev-fleet/hero-dark.svg')
    // Art replaces the icon slot rather than sitting next to it.
    expect(screen.queryByTestId('app-icon')).toBeNull()
  })

  it('falls back to a screenshot when no hero image is declared', () => {
    renderCard(app({ screenshots: ['/app-assets/dev-fleet/shot-1.png'] }))
    expect(document.querySelector('img')!.getAttribute('src'))
      .toBe('/app-assets/dev-fleet/shot-1.png')
  })

  it('falls back to the page icon when the app ships no art', () => {
    renderCard(app())
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('app-icon').getAttribute('data-icon')).toBe('Server')
  })

  it('degrades to the icon when the hero URL 404s', () => {
    renderCard(app({ heroImage: '/app-assets/dev-fleet/missing.svg' }))
    fireEvent.error(document.querySelector('img')!)
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('app-icon')).toBeTruthy()
  })
})
