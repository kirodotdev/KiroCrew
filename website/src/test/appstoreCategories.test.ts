import { describe, it, expect } from 'vitest'
import { categoryFor, categoryCounts } from '../components/appstore/categories'
import { gradientFor } from '../components/appstore/gradient'
import {
  sourceLabel,
  isVerified,
  isRegistrySourced,
  normalizeRegistryApp,
  normalizeInstalledApp,
  normalizeInstalledApps,
  type InstalledApp,
  type RegistryApp,
} from '../components/appstore/types'
import { pickFeatured } from '../pages/apps/useAppsData'

const app = (over: Partial<RegistryApp>): RegistryApp => ({
  name: 'x', displayName: 'X', description: '', version: '1.0.0',
  author: 'someone', installed: false, ...over,
})

describe('categoryFor', () => {
  it('maps specific tags before generic ones (priority order)', () => {
    // research beats autonomy (Research & Writing checked before Agents & Automation)
    expect(categoryFor(['research', 'autonomy', 'autonudge'])).toBe('Research & Writing')
    // oncall beats tickets/pipelines being also ops — and beats productivity
    expect(categoryFor(['oncall', 'operations', 'tickets', 'pipelines'])).toBe('On-call & Ops')
    // code-quality (dev) beats automation/agents
    expect(categoryFor(['performance', 'code-quality', 'automation', 'agents'])).toBe('Developer Tools')
    // files app stays Productivity even with a generic 'code' tag present
    expect(categoryFor(['files', 'explorer', 'productivity', 'code'])).toBe('Productivity')
  })

  it('is case-insensitive and falls back to Other', () => {
    expect(categoryFor(['GitHub'])).toBe('Developer Tools')
    expect(categoryFor(['pixel-art'])).toBe('Other')
    expect(categoryFor([])).toBe('Other')
    expect(categoryFor(undefined)).toBe('Other')
  })
})

describe('categoryCounts', () => {
  it('counts in canonical order and omits empty categories', () => {
    const counts = categoryCounts([
      { tags: ['github'] },
      { tags: ['git'] },
      { tags: ['oncall'] },
      { tags: ['unknown-tag'] },
    ])
    expect(counts).toEqual([
      { category: 'Developer Tools', count: 2 },
      { category: 'On-call & Ops', count: 1 },
      { category: 'Other', count: 1 },
    ])
  })
})

describe('pickFeatured', () => {
  it('prefers curator flags, numbers ordered lowest-first', () => {
    const apps = [
      app({ name: 'a', displayName: 'A' }),
      app({ name: 'b', displayName: 'B', featured: 2 }),
      app({ name: 'c', displayName: 'C', featured: 1 }),
      app({ name: 'd', displayName: 'D', featured: true }),
    ]
    expect(pickFeatured(apps).map(a => a.name)).toEqual(['c', 'b', 'd'])
  })

  it('fills remaining slots from fallback when fewer than 3 flagged', () => {
    const apps = [
      app({ name: 'flagged', featured: true }),
      app({ name: 'hero', heroImage: '/x.png' }),
      app({ name: 'verified', author: 'kirocrew' }),
      app({ name: 'plain' }),
    ]
    // flagged first, then hero-art app, then verified
    expect(pickFeatured(apps).map(a => a.name)).toEqual(['flagged', 'hero', 'verified'])
  })

  it('ignores featured flags from EXTERNAL registries (no spotlight seizure)', () => {
    // The spotlight's Get runs third-party setup with gateway privileges, so an
    // added registry must not be able to flag itself into that slot.
    const apps = [
      app({ name: 'external-shouty', featured: 1, _registry: 'evil' }),
      app({ name: 'core-app', featured: 2 }),
    ]
    expect(pickFeatured(apps)[0].name).toBe('core-app')
    // It can still appear via the deterministic fallback, just not as curator-flagged.
    expect(pickFeatured(apps).map(a => a.name)).toContain('external-shouty')
  })

  it('ranks dark-only and screenshot-only art as art-bearing', () => {
    // Ranking must use the same candidate set useHeroArt renders from, so an
    // app shipping only dark art (or only screenshots) is not treated as
    // art-less and outranked by a plain entry.
    const apps = [
      app({ name: 'plain' }),
      app({ name: 'dark-only', heroImageDark: '/d.png' }),
      app({ name: 'shots-only', screenshots: ['/s.png'] }),
    ]
    const picked = pickFeatured(apps).map(a => a.name)
    expect(picked.indexOf('dark-only')).toBeLessThan(picked.indexOf('plain'))
    expect(picked.indexOf('shots-only')).toBeLessThan(picked.indexOf('plain'))
  })

  it('handles catalogs smaller than three', () => {
    expect(pickFeatured([app({ name: 'only' })]).map(a => a.name)).toEqual(['only'])
    expect(pickFeatured([])).toEqual([])
  })
})

describe('provenance helpers', () => {
  it('labels built-ins, tagged registries, and core entries', () => {
    expect(sourceLabel({ origin: 'builtin' })).toBe('Built-in')
    expect(sourceLabel({ _registry: 'kirodotdev-labs' })).toBe('kirodotdev-labs')
    expect(sourceLabel({})).toBe('Kiro Crew registry')
  })

  it('verifies built-ins and kirocrew-authored core apps only', () => {
    expect(isVerified({ origin: 'builtin', author: 'x' })).toBe(true)
    expect(isVerified({ author: 'KiroCrew' })).toBe(true)
    expect(isVerified({ author: 'random' })).toBe(false)
  })

  it('never lets an EXTERNAL registry self-award the verified mark', () => {
    // An external app.json claiming author "KiroCrew" must not be badged as
    // first-party — the badge sits next to an Install that runs setup code
    // with gateway privileges.
    expect(isVerified({ author: 'KiroCrew', _registry: 'evil-registry' })).toBe(false)
    expect(isVerified({ author: 'kirocrew', _registry: 'kirodotdev-labs' })).toBe(false)
  })

  it('rejects a forged origin: "builtin" from an external registry entry', () => {
    // registry.py copies index keys verbatim for not-yet-installed apps, so an
    // external index can declare origin: "builtin". _registry must be rejected
    // BEFORE the builtin short-circuit or the badge (and the "Built-in"
    // provenance label) is forgeable. Genuine built-ins are merged from the
    // installed list client-side and never carry _registry.
    expect(isVerified({ origin: 'builtin', author: 'whoever', _registry: 'evil' })).toBe(false)
    expect(sourceLabel({ origin: 'builtin', _registry: 'evil' })).toBe('evil')
    // A real built-in (no _registry) still verifies and labels correctly.
    expect(isVerified({ origin: 'builtin', author: 'whoever' })).toBe(true)
    expect(sourceLabel({ origin: 'builtin' })).toBe('Built-in')
  })
})

describe('isRegistrySourced', () => {
  it('reads the registry: prefix the gateway records on a cloned app', () => {
    expect(isRegistrySourced({ source: 'registry:secretary' })).toBe(true)
    expect(isRegistrySourced({ source: 'registry:secretary', origin: 'local' })).toBe(true)
  })

  it('treats a directory install as local however the path is spelled', () => {
    expect(isRegistrySourced({ source: '/home/u/apps/orchestrator-switch' })).toBe(false)
    // A path that merely CONTAINS the word must not read as a registry ref —
    // only the prefix the gateway writes counts.
    expect(isRegistrySourced({ source: '/home/u/registry:copy' })).toBe(false)
    expect(isRegistrySourced({ source: 'C:\\apps\\orchestrator-switch' })).toBe(false)
  })

  it('falls back to origin for a record written before source was stored', () => {
    expect(isRegistrySourced({ origin: 'registry' })).toBe(true)
    expect(isRegistrySourced({ origin: 'local' })).toBe(false)
    expect(isRegistrySourced({})).toBe(false)
    // A stored source always wins over origin — it is the value the backend's
    // own update branch reads.
    expect(isRegistrySourced({ source: '/srv/app', origin: 'registry' })).toBe(false)
  })

  it('survives a non-string source from an index-controlled catalog row', () => {
    // The detail page spreads a CATALOG row into its app object when the
    // installed-record fetch fails, and registry.py copies index keys verbatim
    // for a row it has not installed — so `source` can arrive as an object even
    // though the type says string. This runs inside the autoAction effect, where
    // an unguarded startsWith throws and Sync never dispatches.
    const objectSource = { source: { type: 'git' }, origin: 'registry' } as unknown as
      Parameters<typeof isRegistrySourced>[0]
    expect(() => isRegistrySourced(objectSource)).not.toThrow()
    expect(isRegistrySourced(objectSource)).toBe(true)

    // With no usable origin either, it must answer false (treat as path-installed
    // and let the update endpoint report the real problem) rather than throw.
    const noOrigin = { source: { type: 'git' } } as unknown as
      Parameters<typeof isRegistrySourced>[0]
    expect(() => isRegistrySourced(noOrigin)).not.toThrow()
    expect(isRegistrySourced(noOrigin)).toBe(false)

    for (const bad of [42, true, [], {}]) {
      const app = { source: bad } as unknown as Parameters<typeof isRegistrySourced>[0]
      expect(() => isRegistrySourced(app)).not.toThrow()
      expect(isRegistrySourced(app)).toBe(false)
    }
  })
})

describe('server-computed trust fields (issue #580)', () => {
  it('isVerified prefers the server verified field over client derivation', () => {
    // Server verified:false wins over a spoofed author/origin — the server
    // computed it where _registry is authoritative.
    expect(isVerified({ author: 'KiroCrew', verified: false })).toBe(false)  // brand-ok: author-spoof fixture
    expect(isVerified({ origin: 'builtin', verified: false })).toBe(false)
    // Server verified:true wins over an author that would fail the fallback.
    expect(isVerified({ author: 'random', verified: true })).toBe(true)
  })

  it('isVerified still rejects _registry rows even with a smuggled verified:true', () => {
    // The server never emits verified:true on a _registry-tagged row, so this
    // combination can only be index content passed through an OLDER gateway
    // that computes nothing — it must lose.
    expect(isVerified({ author: 'x', _registry: 'evil', verified: true })).toBe(false)
  })

  it('sourceLabel prefers the server provenance field', () => {
    expect(sourceLabel({ provenance: 'builtin' })).toBe('Built-in')
    expect(sourceLabel({ provenance: 'official', origin: 'builtin' })).toBe('Kiro Crew registry')
    expect(sourceLabel({ provenance: 'external', _registry: 'labs' })).toBe('labs')
  })

  it('sourceLabel still accepts the pre-migration "core" spelling', () => {
    // A newer client can meet an older gateway, which emits 'core' for the same
    // claim 'official' now carries. Dropping the alias would silently fall the
    // row through to the origin-based legacy arm.
    expect(sourceLabel({ provenance: 'core', origin: 'builtin' })).toBe('Kiro Crew registry')
    expect(sourceLabel({ provenance: 'core' })).toBe('Kiro Crew registry')
  })

  it('sourceLabel keeps the _registry name even with a smuggled provenance', () => {
    // Same older-gateway smuggling window: a tagged row is external by
    // construction, so provenance:"official" from index content cannot relabel it.
    expect(sourceLabel({ provenance: 'official', _registry: 'evil' })).toBe('evil')
    expect(sourceLabel({ provenance: 'core', _registry: 'evil' })).toBe('evil')
  })

  it('pickFeatured excludes provenance:"external" rows from curator flags', () => {
    const apps = [
      app({ name: 'external-shouty', featured: 1, provenance: 'external', _registry: 'evil' }),
      app({ name: 'official-app', featured: 2, provenance: 'official' }),
    ]
    expect(pickFeatured(apps)[0].name).toBe('official-app')
  })

  it('pickFeatured excludes external rows signalled by EITHER field', () => {
    // Belt-and-braces: provenance without _registry (new gateway contract)
    // and _registry without provenance (older gateway) are both external.
    const apps = [
      app({ name: 'prov-only', featured: 1, provenance: 'external' }),
      app({ name: 'tag-only', featured: 1, _registry: 'labs' }),
      app({ name: 'core-app', featured: 5 }),
    ]
    expect(pickFeatured(apps)[0].name).toBe('core-app')
  })

  it('legacy rows (older gateway, no server fields) derive exactly as before', () => {
    // Back-compat proof: without provenance/verified, results match the
    // pre-#580 client derivation (the untouched suites above double as this).
    expect(isVerified({ author: 'KiroCrew' })).toBe(true)  // brand-ok: author-spoof fixture
    expect(isVerified({ origin: 'builtin', author: 'x' })).toBe(true)
    expect(isVerified({ author: 'random' })).toBe(false)
    expect(sourceLabel({ origin: 'builtin' })).toBe('Built-in')
    expect(sourceLabel({})).toBe('Kiro Crew registry')
  })
})

describe('gradientFor', () => {
  it('is deterministic per name and returns a css gradient', () => {
    expect(gradientFor('code-review-sage')).toBe(gradientFor('code-review-sage'))
    expect(gradientFor('code-review-sage')).toMatch(/^linear-gradient\(135deg, #[0-9a-f]{6}, #[0-9a-f]{6}\)$/)
  })
})

describe('normalizeRegistryApp', () => {
  it('fills display fields for a minimal entry (failed app.json fetch)', () => {
    // registry.py yields name/repo only when the manifest fetch fails; the
    // store sorts and lowercases these, so undefined must never reach it.
    const out = normalizeRegistryApp({ name: 'orphan-app' } as RegistryApp)
    expect(out.displayName).toBe('orphan-app')
    expect(out.description).toBe('')
    expect(out.author).toBe('')
    expect(out.version).toBe('0.0.0')
    expect(out.tags).toEqual([])
    // The values downstream code calls string methods on are all strings.
    expect(() => out.displayName.toLowerCase().localeCompare(out.description)).not.toThrow()
  })

  it('coerces mistyped fields from user-supplied external JSON', () => {
    const out = normalizeRegistryApp({
      name: 'weird', displayName: 42, description: null, tags: 'not-an-array',
    } as unknown as RegistryApp)
    expect(out.displayName).toBe('weird')
    expect(out.description).toBe('')
    expect(out.tags).toEqual([])
  })

  it('drops non-string tag members but keeps valid ones', () => {
    const out = normalizeRegistryApp({ name: 'x', tags: ['github', 7, null, 'git'] } as unknown as RegistryApp)
    expect(out.tags).toEqual(['github', 'git'])
  })

  it('keeps a safe non-negative integer stargazersCount', () => {
    expect(normalizeRegistryApp({ name: 'x', stargazersCount: 1234 } as RegistryApp).stargazersCount).toBe(1234)
    expect(normalizeRegistryApp({ name: 'x', stargazersCount: 0 } as RegistryApp).stargazersCount).toBe(0)
    expect(normalizeRegistryApp({ name: 'x', stargazersCount: 9007199254740991 } as RegistryApp).stargazersCount).toBe(9007199254740991)
  })

  it('drops a malformed stargazersCount instead of coercing it', () => {
    // External indexes are user-supplied JSON and an older gateway does not
    // sanitize this field server-side, so the query boundary must. 1e308 is
    // finite but compact-formats into hundreds of digits — Number.isSafeInteger
    // is the gate, not Number.isFinite.
    for (const bad of [-1, '1234', NaN, Infinity, 1e308, 3.5, null, [], {}]) {
      const out = normalizeRegistryApp({ name: 'x', stargazersCount: bad } as unknown as RegistryApp)
      expect(out.stargazersCount, `stargazersCount=${String(bad)}`).toBeUndefined()
    }
  })
})

describe('normalizeInstalledApp', () => {
  it('supplies a manifest and its lists for a record that has none', () => {
    // /api/apps mirrors on-disk records, so a hand-written or older app can
    // arrive with no manifest at all. Every render site indexes these lists.
    const out = normalizeInstalledApp({ name: 'bare', version: '1.0.0' } as unknown as InstalledApp)
    expect(out.manifest).toBeTruthy()
    expect(out.manifest.agents).toEqual([])
    expect(out.manifest.skills).toEqual([])
    expect(out.manifest.sops).toEqual([])
    expect(out.manifest.crons).toEqual([])
    expect(out.manifest.tags).toEqual([])
    expect(out.manifest.jobFamilies).toEqual([])
    expect(out.manifest.screenshots).toEqual([])
    expect(out.manifest.highlights).toEqual([])
    // The reads the Apps page and the detail page make, without a gate.
    expect(() => out.manifest.agents.map(a => a.split('/').pop()).join(', ')).not.toThrow()
    expect(() => out.manifest.crons.map(c => c.name).join(', ')).not.toThrow()
  })

  it('coerces every art field, so a wrong TYPE cannot reach a render site', () => {
    // `app.json` is JSON from disk: a field's type is no more guaranteed than its
    // presence. Coercing here is what keeps `"iconPath": {}` from reaching a bare
    // `startsWith` and `"screenshotsDark": {}` a bare `.map` — each of which threw
    // on the surface that read it rather than degrading to no art.
    const out = normalizeInstalledApp({
      name: 'wrong-types',
      manifest: {
        iconUrl: {}, iconUrlDark: 1, iconPath: [], iconPathDark: true,
        heroImage: {}, heroImageDark: 0, heroImageDetail: [], heroImageDetailDark: null,
        repo: {}, screenshots: { 0: 'a.png' }, screenshotsDark: {},
      },
    } as unknown as InstalledApp)
    const m = out.manifest
    for (const v of [m.iconUrl, m.iconUrlDark, m.iconPath, m.iconPathDark,
      m.heroImage, m.heroImageDark, m.heroImageDetail, m.heroImageDetailDark, m.repo]) {
      expect(v).toBe('')
    }
    expect(m.screenshots).toEqual([])
    expect(m.screenshotsDark).toEqual([])
    // The reads the store surfaces make, without a gate.
    expect(() => (m.iconPath as string).startsWith('/')).not.toThrow()
    expect(() => (m.screenshotsDark as string[]).map(s => s)).not.toThrow()
  })

  it('keeps a published art field as written', () => {
    // The coercion must not erase a legitimate value — a built-in's absolute icon
    // and an external app's repo-relative paths both survive untouched.
    const out = normalizeInstalledApp({
      name: 'real-art',
      manifest: {
        iconUrl: '/app-assets/real/icon.svg', iconPath: 'assets/icon.webp',
        heroImageDetail: 'assets/hero-detail.webp', repo: 'octocat/real',
        screenshotsDark: ['assets/dark-1.webp'],
      },
    } as unknown as InstalledApp)
    expect(out.manifest.iconUrl).toBe('/app-assets/real/icon.svg')
    expect(out.manifest.iconPath).toBe('assets/icon.webp')
    expect(out.manifest.heroImageDetail).toBe('assets/hero-detail.webp')
    expect(out.manifest.repo).toBe('octocat/real')
    expect(out.manifest.screenshotsDark).toEqual(['assets/dark-1.webp'])
  })

  it('keeps published list contents and every non-list manifest field', () => {
    const out = normalizeInstalledApp({
      name: 'real',
      manifest: {
        displayName: 'Real', agents: ['agents/a.json'], crons: [{ name: 'nightly' }],
        ui: { pages: [{ route: '/apps/real', label: 'Real', icon: 'Box' }] },
      },
    } as unknown as InstalledApp)
    expect(out.manifest.agents).toEqual(['agents/a.json'])
    expect(out.manifest.crons).toEqual([{ name: 'nightly' }])
    expect(out.manifest.displayName).toBe('Real')
    expect(out.manifest.ui?.pages?.[0]?.route).toBe('/apps/real')
  })

  it('coerces mistyped lists and drops members it cannot render', () => {
    const out = normalizeInstalledApp({
      name: 'weird',
      manifest: { agents: 'agents/a.json', skills: ['s.md', 7, null], crons: [{ name: 'ok' }, {}, null] },
    } as unknown as InstalledApp)
    expect(out.manifest.agents).toEqual([])
    expect(out.manifest.skills).toEqual(['s.md'])
    // A cron is only ever rendered by name, so a nameless entry is dropped
    // rather than shown as a blank row.
    expect(out.manifest.crons).toEqual([{ name: 'ok' }])
  })

  it('preserves fields callers carry beyond InstalledApp', () => {
    const out = normalizeInstalledApp({ name: 'x', managed: true, _newVersion: '2.0.0' } as unknown as InstalledApp)
    expect((out as unknown as { managed: boolean }).managed).toBe(true)
    expect((out as unknown as { _newVersion: string })._newVersion).toBe('2.0.0')
  })

  it('passes a non-object payload through instead of inventing a record', () => {
    // getApp() rejects on 404, but a caller that swallows the failure must not
    // be handed a synthetic app that looks installed.
    expect(normalizeInstalledApp(null as unknown as InstalledApp)).toBeNull()
    expect(normalizeInstalledApps(null as unknown as InstalledApp[])).toBeNull()
  })

  it('normalizes every row of a list payload', () => {
    const out = normalizeInstalledApps([{ name: 'a' }, { name: 'b' }] as unknown as InstalledApp[])
    expect(out.map(a => a.manifest.agents)).toEqual([[], []])
  })
})

describe('categoryFor — malformed tags cannot crash the storefront', () => {
  it('tolerates non-array and non-string tags', () => {
    expect(categoryFor('github' as unknown)).toBe('Other')
    expect(categoryFor([7, null, undefined] as unknown)).toBe('Other')
    expect(categoryFor([null, 'github'] as unknown)).toBe('Developer Tools')
    expect(() => categoryCounts([{ tags: 'nope' }, { tags: [1, 2] }])).not.toThrow()
  })
})

describe('normalizeInstalledApp', () => {
  const installed = (over: Record<string, unknown> = {}, manifest: Record<string, unknown> = {}): InstalledApp =>
    ({
      name: 'zzq-app', version: '1.0.0', displayName: 'Zzq App', enabled: true,
      installedAt: '2026-08-01T00:00:00Z',
      manifest: {
        name: 'zzq-app', version: '1.0.0', displayName: 'Zzq App',
        description: 'd', author: 'a', ...manifest,
      },
      ...over,
    } as unknown as InstalledApp)

  it('defaults every optional manifest collection to an array', () => {
    const out = normalizeInstalledApp(installed())
    expect(out.manifest.agents).toEqual([])
    expect(out.manifest.skills).toEqual([])
    expect(out.manifest.sops).toEqual([])
    expect(out.manifest.crons).toEqual([])
    expect(out.manifest.tags).toEqual([])
    expect(out.manifest.jobFamilies).toEqual([])
    expect(out.manifest.ui?.pages).toEqual([])
  })

  it('survives a record with no manifest at all (the drifted-assertion crash class)', () => {
    const out = normalizeInstalledApp({ name: 'bare' } as unknown as InstalledApp)
    expect(out.manifest.name).toBe('bare')
    expect(out.manifest.displayName).toBe('bare')
    expect(out.manifest.description).toBe('')
    expect(out.manifest.author).toBe('')
    expect(out.manifest.agents).toEqual([])
    expect(out.manifest.ui?.pages).toEqual([])
    // Enumerating a normalized manifest collection cannot throw.
    expect(() => (out.manifest.agents ?? []).map(a => a.toLowerCase())).not.toThrow()
  })

  it('coerces mistyped collections and drops malformed members', () => {
    const out = normalizeInstalledApp(installed({}, {
      agents: 'not-an-array',
      skills: ['ok', 7, null],
      crons: [{ name: 'tick' }, { nope: true }, null, 'raw'],
      tags: { 0: 'x' },
    }))
    expect(out.manifest.agents).toEqual([])
    expect(out.manifest.skills).toEqual(['ok'])
    expect(out.manifest.crons).toEqual([{ name: 'tick' }])
    expect(out.manifest.tags).toEqual([])
  })

  it('keeps ui.entry and extra page keys while coercing pages', () => {
    const out = normalizeInstalledApp(installed({}, {
      ui: {
        entry: 'main.js',
        pages: [
          { route: '/apps/zzq', label: 'Zzq', icon: 'bot', iconUrl: 'icon.svg' },
          { label: 'no-route' },
          null,
        ],
      },
    }))
    expect(out.manifest.ui?.entry).toBe('main.js')
    expect(out.manifest.ui?.pages).toEqual([
      { route: '/apps/zzq', label: 'Zzq', icon: 'bot', iconUrl: 'icon.svg' },
    ])
  })

  it('does not invent a ui entry for an app without one', () => {
    const out = normalizeInstalledApp(installed())
    // hasUI/AppHost routing read entry truthiness; normalization must not
    // change navigation eligibility, only make the pages read safe.
    expect(out.manifest.ui?.entry).toBeUndefined()
  })

  it('fills required display strings from mistyped values', () => {
    const out = normalizeInstalledApp(installed(
      { displayName: 42, version: null },
      { displayName: undefined, version: undefined, description: null, author: 9 },
    ))
    expect(out.displayName).toBe('zzq-app')
    expect(out.version).toBe('0.0.0')
    expect(out.manifest.displayName).toBe('zzq-app')
    expect(out.manifest.version).toBe('0.0.0')
    expect(out.manifest.description).toBe('')
    expect(out.manifest.author).toBe('')
  })
})
