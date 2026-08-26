/**
 * Hero-art path resolution — repo-relative manifest art must be requested
 * through the blob proxy on every surface that renders it, while absolute paths
 * pass through byte-for-byte so built-ins keep working.
 *
 * The surface exercised here is `FeaturedSpotlight`, the EDITORIAL surface.
 * A list row and the Library card render no hero art: a list row shows the app's icon,
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

import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import { resolveArtPath, manifestArt, manifestArtList, classifyManifestArt } from '../components/appstore/useHeroArt'
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

describe('manifestArt', () => {
  it('proxies a repo-relative manifest path', () => {
    expect(manifestArt('assets/icon.webp', 'octocat/some-app'))
      .toBe('/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Ficon.webp')
  })

  it('leaves an absolute path or a full URL as written', () => {
    expect(manifestArt('/app-assets/dev-fleet/icon.svg', '')).toBe('/app-assets/dev-fleet/icon.svg')
  })

  it('REFUSES every spelling the URL parser resolves off-origin', () => {
    // Measured against a real parser, resolving each against a same-origin base.
    // Four families, three of which defeated a successive prefix test here:
    const offOrigin = [
      'https://example.com/icon.png', 'http://example.com/icon.png',
      'data:image/png;base64,AAAA',
      // protocol-relative, and the backslash forms the parser reads as slashes
      '//example.com/icon.png', '/\\example.com/icon.png',
      '\\\\example.com/icon.png', '\\/example.com/icon.png',
      // ASCII tab / newline split the two slashes; the parser strips them first
      '/\t/example.com/icon.png', '/\n/example.com/icon.png',
      '/\r/example.com/icon.png', '/\t\\example.com/icon.png',
      '/\t\t/example.com/icon.png',
      // leading C0 / space is trimmed, so position 0 is not where the value starts
      '\t//example.com/icon.png', ' //example.com/icon.png',
      ' /\\example.com/icon.png', '\u0000//example.com/icon.png',
      '\u000b//example.com/icon.png', ' https://example.com/icon.png',
    ]
    for (const bad of offOrigin) {
      expect(manifestArt(bad, 'octocat/some-app')).toBe('')
      expect(classifyManifestArt(bad)).toBe('refused')
    }
  })

  it('does not over-reject a value that stays on this origin', () => {
    // The mirror of the rule above: a single leading backslash, a mid-path
    // backslash, a mid-value space and a trailing tab all resolve same-origin, so
    // rejecting them would be a rule nobody could predict from the symptom.
    expect(classifyManifestArt('/app-assets/x\\y.svg')).toBe('same-origin')
    expect(classifyManifestArt('\\example.com/x.png')).toBe('relative')
    expect(classifyManifestArt('/app-assets/a b.svg')).toBe('same-origin')
    expect(manifestArt('/app-assets/x.svg\t', '')).toBe('/app-assets/x.svg')
  })

  it('emits the string it classified, not the raw one', () => {
    // Classifying a normalized value and handing <img> the raw one is the gap a
    // tab-splitting value walks through.
    expect(manifestArt('\tassets/a.png', 'octocat/some-app'))
      .toBe('/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fa.png')
  })

  it('classifies each path shape', () => {
    expect(classifyManifestArt('/app-assets/x.svg')).toBe('same-origin')
    expect(classifyManifestArt('assets/x.svg')).toBe('relative')
    expect(classifyManifestArt('https://example.com/x.svg')).toBe('refused')
    expect(classifyManifestArt('//example.com/x.svg')).toBe('refused')
    expect(classifyManifestArt('/\\example.com/x.svg')).toBe('refused')
    expect(classifyManifestArt('\\\\example.com/x.svg')).toBe('refused')
    expect(classifyManifestArt('\\/example.com/x.svg')).toBe('refused')
    expect(classifyManifestArt('')).toBe('refused')
    expect(classifyManifestArt(undefined)).toBe('refused')
  })

  it('answers empty for a repo-relative path with no repo, NOT the bare path', () => {
    // The distinction from `resolveArtPath` — a bare relative src resolves
    // against the current route and 404s the SPA shell instead of degrading.
    expect(manifestArt('assets/icon.webp', '')).toBe('')
    expect(manifestArt('assets/icon.webp', undefined)).toBe('')
  })

  it('answers empty for a missing path', () => {
    expect(manifestArt('', 'octocat/some-app')).toBe('')
    expect(manifestArt(undefined, 'octocat/some-app')).toBe('')
  })

  it('refuses a non-string value instead of throwing', () => {
    // A manifest is JSON from disk and the installed-app normalizer passes
    // unknown keys through verbatim, so `"iconPath": {}` reaches this as an
    // object. A bare `startsWith` would throw and blank the whole surface.
    for (const bad of [{}, [], 42, true, null] as unknown[]) {
      expect(manifestArt(bad, 'octocat/some-app')).toBe('')
      expect(classifyManifestArt(bad)).toBe('refused')
    }
  })
})

describe('manifestArtList', () => {
  it('resolves every entry and drops the refused ones', () => {
    expect(manifestArtList(['assets/a.png', 'https://evil.example/b.png', '/app-assets/c.svg'],
      'octocat/some-app')).toEqual([
      '/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fa.png',
      '/app-assets/c.svg',
    ])
  })

  it('answers an empty list for a non-array instead of throwing', () => {
    // `screenshotsDark` is NOT coerced by the installed-app normalizer (only
    // `screenshots` is), so `"screenshotsDark": {}` reaches the caller raw.
    for (const bad of [{}, 'assets/a.png', 42, null, undefined] as unknown[]) {
      expect(manifestArtList(bad, 'octocat/some-app')).toEqual([])
    }
  })

  it('drops a non-string entry inside an otherwise valid array', () => {
    expect(manifestArtList(['assets/a.png', {}, null], 'octocat/some-app'))
      .toEqual(['/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fa.png'])
  })
})

describe('FeaturedSpotlight hero art (editorial)', () => {
  const noop = () => {}

  const card = (over: Partial<RegistryApp>) => render(
    <FeaturedSpotlight
      type="app"
      apps={[registryApp(over)]}
      onOpenApp={noop} onGet={noop} onEnable={noop}
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
