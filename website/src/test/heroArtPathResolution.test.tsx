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
import { classifyManifestArt, clientLocalArt, installedArt, resolveArtPath } from '../components/appstore/useHeroArt'
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

  it('refuses an absolute path whose dot segments would escape it (#6887 review)', () => {
    // A registry row is third-party content and its absolute paths pass
    // through to <img src> verbatim — a dot segment there normalizes into an
    // arbitrary same-origin request (`/app-assets/../api/tips/next` is fetched
    // as `/api/tips/next`), bypassing classifyManifestArt entirely. Refusing
    // answers '' so the surface degrades to the gradient, same as no art.
    expect(resolveArtPath('/app-assets/../api/tips/next', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets\\..\\api/tips/next', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets/%2e%2e/api/tips/next', 'octocat/some-app')).toBe('')
    // No repo to resolve against changes nothing: the value is still absolute.
    expect(resolveArtPath('/app-assets/../api/tips/next')).toBe('')
  })

  it('passes registry values through only on the art-route allowlist (#6887 review round 2)', () => {
    // Traversal refusal alone is not enough: a registry row can name an
    // authenticated same-origin API OUTRIGHT (`heroImage: "/api/tips/next"`),
    // no dot segments needed — an <img> GET with credentials the row's author
    // chose. Absolute values pass only on the art allowlist: shipped client
    // assets, the installed-art route, and the server-enriched blob proxy.
    expect(resolveArtPath('/api/tips/next', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/api/tips/next')).toBe('')
    expect(resolveArtPath('/settings', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/api/apps/registry', 'octocat/some-app')).toBe('')
    // The allowlist keeps every legitimate shape byte-identical.
    expect(resolveArtPath('/app-assets/dev-fleet/hero.svg', 'octocat/some-app'))
      .toBe('/app-assets/dev-fleet/hero.svg')
    expect(resolveArtPath('/apps/demo-app/art/assets/hero.png', 'octocat/some-app'))
      .toBe('/apps/demo-app/art/assets/hero.png')
    const enriched = '/api/apps/blob?repo=octocat%2Fsome-app&path=assets%2Fhero.png'
    expect(resolveArtPath(enriched, 'octocat/some-app')).toBe(enriched)
    // A RELATIVE value with no repo resolves against the current page — the
    // same reach (`api/tips/next` under a root-mounted page is `/api/...`), so
    // the repo-less pass-through is gone: nothing legitimate used it (built-ins
    // declare absolute /app-assets/ paths), and it rendered a dead relative
    // URL at best.
    expect(resolveArtPath('api/tips/next')).toBe('')
    expect(resolveArtPath('assets/hero.png')).toBe('')
    // Full URLs keep their documented pass-through (a cross-origin image
    // carries no same-origin credentials; registry rows may name CDNs).
    expect(resolveArtPath('https://example.com/hero.png', 'octocat/some-app'))
      .toBe('https://example.com/hero.png')
  })

  it('judges the value the PARSER will see, not the raw spelling (#6887 review)', () => {
    // The parser strips every ASCII tab and newline BEFORE it recognizes
    // separators and dot segments, so a tab inside `..` (or inside a percent
    // spelling of it) is invisible to a raw-string scan but normalizes all the
    // same. The guard runs on the parser's view of the value.
    expect(resolveArtPath('/app-assets/.\t./api/tips/next', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets/%2\tE%2E/api/tips/next', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets\\.\n.\\api/tips/next', 'octocat/some-app')).toBe('')
    expect(classifyManifestArt('a/.\t./b.png')).toBe('refused')
    // A relative value with NO repo passes through raw and resolves against
    // the current page, so it gets the same traversal guard...
    expect(resolveArtPath('assets\\..\\api/tips/next')).toBe('')
    // ...a full URL's own path is never this guard's business.
    expect(resolveArtPath('https://example.com/a/../b.png')).toBe('https://example.com/a/../b.png')
    // The parser also trims TRAILING C0 controls and spaces before it parses,
    // so a terminal dot segment cannot hide behind one.
    expect(resolveArtPath('/app-assets/..\u0000', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets/%2e%2e\u000c', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets/.. ', 'octocat/some-app')).toBe('')
    expect(classifyManifestArt('/app-assets/.. ')).toBe('refused')
    // Mid-value spaces are NOT stripped (the parser keeps them too).
    expect(classifyManifestArt('/app-assets/a b.svg')).toBe('same-origin')
    // `?` and `#` TERMINATE the path, so a dot segment right before one is
    // real ("/app-assets/..?x" is fetched from "/") even though the raw split
    // reads "..?x" as one non-dot segment.
    expect(resolveArtPath('/app-assets/..?x', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets/..#x', 'octocat/some-app')).toBe('')
    expect(resolveArtPath('/app-assets/%2e%2e?x', 'octocat/some-app')).toBe('')
    expect(classifyManifestArt('/app-assets/..?x')).toBe('refused')
    // ...while a RELATIVE value keeps the whole-value view: it is re-joined
    // under the art route after per-segment encoding, where `?` is literal
    // path data, so "a?/../y" would traverse the joined URL and stays refused.
    expect(classifyManifestArt('a?/../y')).toBe('refused')
    // An innocent query on a clean path stays accepted.
    expect(classifyManifestArt('/app-assets/x.svg?v=2')).toBe('same-origin')
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

  it('refuses a relative value with no repo to resolve against (#6887 review round 2)', () => {
    // Without a repo the value cannot be blob-proxied, and passing it raw
    // renders an <img> src relative to the CURRENT PAGE — on a root-mounted
    // SPA, 'api/tips/next' is '/api/tips/next', the same authenticated-GET
    // reach as the absolute spelling. Nothing legitimate used this branch:
    // built-ins declare absolute /app-assets/ paths.
    expect(resolveArtPath('assets/hero.png')).toBe('')
    expect(resolveArtPath('', 'octocat/some-app')).toBe('')
  })
})

describe('classifyManifestArt', () => {
  /*
   * These cases outlived `manifestArt`, which this change deleted as a
   * caller-less export. They were never really about that wrapper: what they pin
   * is the CLASSIFIER, which every art resolver still routes through, and three
   * of the refusal families below each defeated a successive prefix test during
   * review. Deleting them alongside the wrapper would have retired the evidence
   * and kept the rule.
   */

  it('refuses dot segments in every spelling the URL parser normalizes (#6887 review)', () => {
    // The fourth refusal family: a dot segment survives the origin probe (it
    // stays same-origin) but the browser NORMALIZES it after the art route is
    // joined on, so `/apps/<name>/art/../../../api/tips/next` is requested as
    // `/api/tips/next` — a manifest-controlled <img> invoking an authenticated
    // API. WHATWG treats `%2e` as `.` during path normalization, so the
    // percent forms are the same hole and are refused by the same rule.
    expect(classifyManifestArt('../../../api/tips/next')).toBe('refused')
    expect(classifyManifestArt('assets/../../../api/tips/next')).toBe('refused')
    expect(classifyManifestArt('/app-assets/../api/tips/next')).toBe('refused')
    expect(classifyManifestArt('/./api/tips/next')).toBe('refused')
    expect(classifyManifestArt('a/%2e%2e/b.png')).toBe('refused')
    expect(classifyManifestArt('a/%2E%2E/b.png')).toBe('refused')
    expect(classifyManifestArt('a/.%2e/b.png')).toBe('refused')
    expect(classifyManifestArt('a/%2e./b.png')).toBe('refused')
    expect(classifyManifestArt('a/%2e/b.png')).toBe('refused')
    expect(classifyManifestArt('./../api/tips/next')).toBe('refused')
    // WHATWG converts `\` to `/` in special-scheme URLs before normalizing, so
    // a backslash-separated dot segment is the same escape.
    expect(classifyManifestArt('/app-assets\\..\\api/tips/next')).toBe('refused')
    expect(classifyManifestArt('a\\..\\b.png')).toBe('refused')
    expect(installedArt('../../../api/tips/next', 'demo-app')).toBe('')
    expect(installedArt('/app-assets/../api/tips/next', 'demo-app')).toBe('')
  })

  it('refuses authority-form values, including ones naming the probe host itself (#6887 review)', () => {
    // "//host/..." is an AUTHORITY reference: the parser consumes the host
    // instead of path segments. A generic host already fails the origin
    // comparison, but a value naming the probe's own host would match it
    // exactly and slip through — while on the real dashboard origin it still
    // targets that external host. Refused by shape, before any origin math.
    expect(classifyManifestArt('//origin-probe.invalid/app-assets/x.svg')).toBe('refused')
    expect(classifyManifestArt('///origin-probe.invalid/x.png')).toBe('refused')
    expect(classifyManifestArt('/\\origin-probe.invalid/x.png')).toBe('refused')
    expect(classifyManifestArt('\\\\origin-probe.invalid\\x.png')).toBe('refused')
    // A SINGLE leading separator is not an authority form: the existing pins
    // ("/app-assets/x.svg" same-origin, "\\example.com/x.png" relative) hold.
    expect(classifyManifestArt('/app-assets/x.svg')).toBe('same-origin')
  })

  it('accepts a same-origin absolute path ONLY on the client-art allowlist (#6887 review)', () => {
    // The positive origin rule alone accepts ANY same-origin absolute path —
    // including "/api/tips/next", an authenticated API a malicious installed
    // manifest could name outright (no traversal needed) and have fetched by
    // <img> the moment the registry primary errors. A manifest may only point
    // at shipped client assets or the installed-art route.
    expect(classifyManifestArt('/api/tips/next')).toBe('refused')
    expect(classifyManifestArt('/api/apps/registry')).toBe('refused')
    expect(classifyManifestArt('/settings')).toBe('refused')
    expect(classifyManifestArt('/hero/browse-light.png')).toBe('refused')
    expect(installedArt('/api/tips/next', 'demo-app')).toBe('')
    expect(clientLocalArt('/api/tips/next')).toBe('')
    // The allowlist: shipped client assets, and the installed-art route the
    // resolvers themselves emit (the #6864 gates re-classify those outputs).
    expect(classifyManifestArt('/app-assets/worlds/hero-light.svg')).toBe('same-origin')
    expect(classifyManifestArt('/apps/demo-app/art/assets/hero.png')).toBe('same-origin')
    // A bare prefix with no child asset names nothing servable.
    expect(classifyManifestArt('/app-assets/')).toBe('refused')
    expect(classifyManifestArt('/apps/demo-app/art/')).toBe('refused')
  })

  it('keeps the sanctioned leading "./" spelling working', () => {
    // `./assets/x.png` means the same repo-relative path as `assets/x.png` and
    // is stripped downstream — only the LEADING dot segment is sanctioned.
    expect(classifyManifestArt('./assets/x.png')).toBe('relative')
    expect(installedArt('./assets/x.png', 'demo-app')).toBe('/apps/demo-app/art/assets/x.png')
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
      expect(classifyManifestArt(bad)).toBe('refused')
      // And the live resolvers must both act on that answer, not re-derive it.
      expect(installedArt(bad, 'some-app')).toBe('')
      expect(clientLocalArt(bad)).toBe('')
    }
  })

  it('does not over-reject a value that stays on this origin', () => {
    // The mirror of the rule above: a single leading backslash, a mid-path
    // backslash, a mid-value space and a trailing tab all resolve same-origin, so
    // rejecting them would be a rule nobody could predict from the symptom.
    expect(classifyManifestArt('/app-assets/x\\y.svg')).toBe('same-origin')
    expect(classifyManifestArt('\\example.com/x.png')).toBe('relative')
    expect(classifyManifestArt('/app-assets/a b.svg')).toBe('same-origin')
    expect(clientLocalArt('/app-assets/x.svg\t')).toBe('/app-assets/x.svg')
  })

  it('emits the string it classified, not the raw one', () => {
    // Classifying a normalized value and handing <img> the raw one is the gap a
    // tab-splitting value walks through.
    expect(installedArt('\tassets/a.png', 'some-app'))
      .toBe('/apps/some-app/art/assets/a.png')
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

  it('refuses a non-string value instead of throwing', () => {
    // A manifest is JSON from disk and the installed-app normalizer passes
    // unknown keys through verbatim, so `"iconPath": {}` reaches this as an
    // object. A bare `startsWith` would throw and blank the whole surface.
    for (const bad of [{}, [], 42, true, null] as unknown[]) {
      expect(classifyManifestArt(bad)).toBe('refused')
      expect(installedArt(bad, 'some-app')).toBe('')
      expect(clientLocalArt(bad)).toBe('')
    }
  })
})

describe('clientLocalArt', () => {
  /*
   * `iconUrl` means "a builtin's absolute client-local path". The backend's
   * declared-field set carries `iconPath`, NOT `iconUrl`, so building an art-route
   * URL out of a RELATIVE `iconUrl` produces a path the route refuses by
   * construction -- a guaranteed 404 dressed as a fallback. That is what this
   * refuses, and it is the whole reason the helper exists separately.
   */
  it('passes a builtin absolute client-local path through', () => {
    expect(clientLocalArt('/app-assets/dev-fleet/icon.svg'))
      .toBe('/app-assets/dev-fleet/icon.svg')
  })

  it('REFUSES a relative value rather than building a URL the route cannot serve', () => {
    expect(clientLocalArt('assets/icon.webp')).toBe('')
    expect(clientLocalArt('./assets/icon.webp')).toBe('')
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
