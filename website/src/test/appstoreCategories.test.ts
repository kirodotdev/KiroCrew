import { describe, it, expect } from 'vitest'
import { categoryFor, categoryCounts } from '../components/appstore/categories'
import { gradientFor } from '../components/appstore/gradient'
import { sourceLabel, isVerified, normalizeRegistryApp, type RegistryApp } from '../components/appstore/types'
import { pickFeatured } from '../pages/AppsPage'

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
})

describe('categoryFor — malformed tags cannot crash the storefront', () => {
  it('tolerates non-array and non-string tags', () => {
    expect(categoryFor('github' as unknown)).toBe('Other')
    expect(categoryFor([7, null, undefined] as unknown)).toBe('Other')
    expect(categoryFor([null, 'github'] as unknown)).toBe('Developer Tools')
    expect(() => categoryCounts([{ tags: 'nope' }, { tags: [1, 2] }])).not.toThrow()
  })
})
