/**
 * Hero-art path resolution — repo-relative manifest art must be requested
 * through the blob proxy on every surface that renders it, while absolute paths
 * pass through byte-for-byte so built-ins keep working.
 *
 * The surface exercised here is `FeatureCard`, one of the EDITORIAL surfaces.
 * It used to be `AppListRow`, and the Library's card had its own copy of these
 * assertions — neither renders hero art now: a list row shows the app's icon,
 * because a 96x54 crop of marketing art is too small to read as art and too
 * large to scan as an identity. Retargeting rather than deleting is the point:
 * the resolution RULES did not change, only which component still reaches them,
 * and a rule with no test is a rule that rots.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))
vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="app-icon" />,
}))

import FeatureCard from '../components/appstore/FeatureCard'
import { resolveArtPath } from '../components/appstore/useHeroArt'
import type { RegistryApp } from '../components/appstore/types'

function registryApp(over: Partial<RegistryApp> = {}): RegistryApp {
  return {
    name: 'some-app',
    displayName: 'Some App',
    description: 'A registry-installed app.',
    version: '1.0.0',
    author: 'octocat',
    installed: false,
    origin: 'registry',
    repo: 'octocat/some-app',
    ...over,
  }
}

describe('resolveArtPath', () => {
  it('routes a repo-relative path through the blob proxy', () => {
    expect(resolveArtPath('assets/hero.png', 'octocat/some-app'))
      .toBe('/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fhero.png')
  })

  it('normalizes a leading "./" before proxying', () => {
    expect(resolveArtPath('./assets/hero.png', 'octocat/some-app'))
      .toBe('/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fhero.png')
  })

  it('leaves absolute paths untouched', () => {
    expect(resolveArtPath('/app-assets/dev-fleet/hero.svg', 'octocat/some-app'))
      .toBe('/app-assets/dev-fleet/hero.svg')
  })

  it('does not double-wrap a server-enriched blob proxy URL', () => {
    const enriched = '/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fhero.png'
    expect(resolveArtPath(enriched, 'octocat/some-app')).toBe(enriched)
  })

  it('leaves full URLs and data URIs untouched', () => {
    expect(resolveArtPath('https://example.com/hero.png', 'octocat/some-app'))
      .toBe('https://example.com/hero.png')
    expect(resolveArtPath('data:image/png;base64,AAAA', 'octocat/some-app'))
      .toBe('data:image/png;base64,AAAA')
  })

  it('passes through when there is no repo to resolve against', () => {
    expect(resolveArtPath('assets/hero.png')).toBe('assets/hero.png')
    expect(resolveArtPath('', 'octocat/some-app')).toBe('')
  })
})

describe('FeatureCard hero art (editorial)', () => {
  const noop = () => {}

  const card = (over: Partial<RegistryApp>) => render(
    <FeatureCard
      app={registryApp(over)}
      onOpen={noop} onGet={noop} onEnable={noop}
    />,
  )

  it('requests a repo-relative hero through the blob proxy', () => {
    card({ heroImageDark: 'assets/hero-dark.png' })
    expect(document.querySelector('img')!.getAttribute('src'))
      .toBe('/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fhero-dark.png')
  })

  it('does not rewrite an absolute hero path', () => {
    card({ heroImageDark: '/app-assets/some-app/hero-dark.svg' })
    expect(document.querySelector('img')!.getAttribute('src'))
      .toBe('/app-assets/some-app/hero-dark.svg')
  })
})

describe('the list surfaces no longer reach art resolution at all', () => {
  it('AppListRow requests no blob-proxied art for a repo-relative hero', async () => {
    const { default: AppListRow } = await import('../components/appstore/AppListRow')
    const noop = () => {}
    render(
      <AppListRow
        app={registryApp({ heroImageDark: 'assets/hero-dark.png' })}
        onOpen={noop} onGet={noop} onUpdate={noop} onEnable={noop}
      />,
    )
    const srcs = [...document.querySelectorAll('img')].map(i => i.getAttribute('src') || '')
    expect(srcs.some(s => s.includes('/api/apps/blob'))).toBe(false)
  })
})
