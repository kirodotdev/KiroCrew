import { describe, expect, it } from 'vitest'
import { providerFor, resolveLinkPreviews } from './linkPreviewRegistry'

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
