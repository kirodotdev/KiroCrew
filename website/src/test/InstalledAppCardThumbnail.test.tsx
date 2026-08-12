/**
 * InstalledAppCard thumbnail — the Library tab must render the same thing
 * Discover's rows do, and that is the app's ICON, not its hero art.
 *
 * This file replaces a suite that asserted the opposite. Hero art is editorial:
 * it earns a wide panel on the spotlight and the feature cards, where there are
 * two or three of them. A list is scanned, and a 96x54 crop of someone's
 * marketing art is too small to read as art and too large to scan as an
 * identity. So the list surfaces take the icon and `useHeroArt` is reached only
 * from the editorial ones.
 *
 * The invariant the old file existed to protect still holds and is still the
 * point: ONE app must look like ONE app across both tabs. The Library shipped a
 * flat lucide icon for a whole release because the rule lived inside
 * `AppListRow` only, so the strongest assertion here is not "an icon renders"
 * but "manifest art is ignored EVEN WHEN PRESENT" — that is what a regression
 * would put back.
 *
 * Path-resolution rules for art (blob proxy, `./` normalization, absolute
 * pass-through) are covered directly against `resolveArtPath` in
 * `heroArtPathResolution.test.tsx`, so retiring the hero assertions here costs
 * no coverage of that logic.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

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
      description: 'A control panel for working on Kiro Crew itself.',
      author: 'kirocrew',
      ui: { pages: [{ route: '/dev-fleet', label: 'Dev Fleet', icon: 'Server' }] },
      ...manifest,
    },
  } as InstalledApp
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

describe('InstalledAppCard thumbnail', () => {
  it('renders the page icon', () => {
    renderCard(app())
    expect(screen.getByTestId('app-icon').getAttribute('data-icon')).toBe('Server')
  })

  it('ignores a hero image even when the manifest declares both variants', () => {
    renderCard(app({
      heroImage: '/app-assets/dev-fleet/hero-light.svg',
      heroImageDark: '/app-assets/dev-fleet/hero-dark.svg',
    }))
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('app-icon')).toBeTruthy()
  })

  it('ignores screenshots as a thumbnail source', () => {
    renderCard(app({ screenshots: ['/app-assets/dev-fleet/shot-1.png'] }))
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('app-icon')).toBeTruthy()
  })

  it('does not reach the blob proxy for repo-relative art', () => {
    // The old behaviour resolved this through /api/apps/blob. A request from a
    // list row is the specific thing that should no longer happen -- twelve rows
    // each fetching a publisher's art is the cost this change removes.
    renderCard(app({
      heroImageDark: 'assets/hero-dark.png',
      screenshots: ['shots/one.png'],
      repo: 'octocat/some-app',
    }))
    const srcs = [...document.querySelectorAll('img')].map(i => i.getAttribute('src') || '')
    expect(srcs.some(s => s.includes('/api/apps/blob'))).toBe(false)
  })
})
