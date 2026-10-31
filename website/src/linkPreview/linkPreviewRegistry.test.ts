import { describe, expect, it } from 'vitest'
import { isPublicSuffix, normalizeHost, patternGrant, providerFor, resolveLinkPreviews } from './linkPreviewRegistry'

const app = (over: Record<string, unknown> = {}, preview: Record<string, unknown> = {}) => ({
  name: 'cards-app',
  enabled: true,
  manifest: {
    contributes: {
      linkPreviews: [{ id: 'cards', entry: 'dist/lp.mjs', hosts: ['phone.example.com', '.example.dev'], ...preview }],
    },
  },
  ...over,
})

describe('resolveLinkPreviews', () => {
  it('resolves an enabled provider and splits exact hosts from suffixes', () => {
    const [p] = resolveLinkPreviews([app()])
    expect(p.key).toBe('cards-app:cards')
    expect([...p.exact]).toEqual(['phone.example.com'])
    expect(p.suffixes).toEqual(['.example.dev'])
    expect(p.routeBase).toBe('/api/apps/cards-app')
  })

  it('skips disabled apps, bad ids, traversal entries and empty host lists', () => {
    expect(resolveLinkPreviews([app({ enabled: false })])).toEqual([])
    expect(resolveLinkPreviews([app({}, { id: 'Bad!' })])).toEqual([])
    expect(resolveLinkPreviews([app({}, { entry: '../x.mjs' })])).toEqual([])
    expect(resolveLinkPreviews([app({}, { hosts: ['.com', 'nodot'] })])).toEqual([])
  })

  it('uses the reverse-proxy base for a process-backed app', () => {
    const a = app()
    ;(a.manifest as Record<string, unknown>).backend = { entryPoint: 'server.py' }
    expect(resolveLinkPreviews([a])[0].routeBase).toBe('/apps/cards-app/api')
  })
})

describe('providerFor', () => {
  const ps = resolveLinkPreviews([app()])
  it('matches exact hosts and suffixes, https only', () => {
    expect(providerFor('https://phone.example.com/u/x', ps)?.key).toBe('cards-app:cards')
    expect(providerFor('https://example.dev/', ps)?.key).toBe('cards-app:cards')
    expect(providerFor('https://a.b.example.dev/x', ps)?.key).toBe('cards-app:cards')
    expect(providerFor('http://phone.example.com/', ps)).toBeNull()
    expect(providerFor('https://notexample.dev/', ps)).toBeNull()
    expect(providerFor('https://other.example.com/', ps)).toBeNull()
    expect(providerFor('not a url', ps)).toBeNull()
  })
})

describe('host normalization and public suffixes', () => {
  it('normalizes case, a trailing dot and IDNA, keeping a leading dot', () => {
    expect(normalizeHost('Tickets.Example.COM.')).toBe('tickets.example.com')
    expect(normalizeHost('.Example.dev')).toBe('.example.dev')
    expect(normalizeHost('bücher.example')).toBe('xn--bcher-kva.example')
    expect(normalizeHost('')).toBeNull()
  })

  it('refuses a public suffix or a bare TLD as a host claim', () => {
    for (const h of ['co.uk', 'com.au', 'github.io', 'com']) expect(isPublicSuffix(h)).toBe(true)
    for (const h of ['example.co.uk', 'example.com', 'me.github.io', 'example.dev']) expect(isPublicSuffix(h)).toBe(false)
    const [p] = resolveLinkPreviews([app({}, { hosts: ['.co.uk', '.github.io', 'Example.COM.'] })])
    expect([...p.exact]).toEqual(['example.com'])
    expect(p.suffixes).toEqual([])
  })

  it('matches a hovered URL whose host ends in a dot', () => {
    const ps = resolveLinkPreviews([app()])
    expect(providerFor('https://phone.example.com./x', ps)).not.toBeNull()
  })
})

describe('the owner\u2019s grant', () => {
  it('uses only the hosts and patterns the owner approved; an added one waits', () => {
    const tp = [{ id: 'a', pattern: 'A-\\d+' }, { id: 'b', pattern: 'B-\\d+', flags: 'gi' }]
    const row = {
      ...app({}, { textPatterns: tp }),
      linkPreviewGrant: { hosts: ['phone.example.com'], textPatterns: [patternGrant(tp[0], 0)] },
    }
    const [p] = resolveLinkPreviews([row])
    expect([...p.exact]).toEqual(['phone.example.com'])
    expect(p.suffixes).toEqual([]) // `.example.dev` is declared but not approved
    expect(p.textPatterns.map((t) => t.id)).toEqual(['a'])
  })

  it('an unnamed pattern keeps its declaration index as its id when an earlier one waits', () => {
    // Pattern 0 changed in an update and waits; pattern 1 stays approved. The off
    // switch the app reported for "1" must still name pattern 1.
    const tp = [{ pattern: 'A-\\d+' }, { pattern: 'B-\\d+' }]
    const [p] = resolveLinkPreviews([{ ...app({}, { textPatterns: tp }), linkPreviewGrant: { hosts: ['phone.example.com'], textPatterns: ['1::B-\\d+'] } }])
    expect(p.textPatterns).toEqual([{ pattern: 'B-\\d+', id: '1' }])
  })

  it('a provider with nothing approved is not resolved', () => {
    expect(resolveLinkPreviews([{ ...app(), linkPreviewGrant: { hosts: [], textPatterns: [] } }])).toEqual([])
  })

  it('the grant entry of a pattern is its id, its sorted flags without g, and its source', () => {
    expect(patternGrant({ id: 'x', pattern: 'P-\\d+', flags: 'uig' }, 3)).toBe('x:iu:P-\\d+')
    expect(patternGrant({ pattern: 'P' }, 3)).toBe('3::P')
  })
})
