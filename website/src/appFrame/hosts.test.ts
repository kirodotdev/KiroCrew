import { describe, expect, it } from 'vitest'
import { claimableUrlHost, claimsHost, grantedSet, isClaimableHost, readHostClaims, withoutQuery } from './hosts'

describe('shared host claims', () => {
  it('refuses what host_refusal refuses', () => {
    for (const h of ['127.0.0.1', '.10.0.0.8', 'app.localhost', '.co.uk', 'github.io', 'nodot']) expect(isClaimableHost(h)).toBe(false)
    for (const h of ['example.com', '.example.dev', '123.example.com']) expect(isClaimableHost(h)).toBe(true)
  })

  it('reads claims normalized, capped and cut to the grant', () => {
    const claims = readHostClaims(['A.Example.com.', '.example.dev', '127.0.0.1', 'b.example.com', 7], 3, grantedSet(['a.example.com', '.example.dev']))
    expect([...claims.exact]).toEqual(['a.example.com'])
    expect([...claims.suffixes]).toEqual(['.example.dev'])
    expect(grantedSet('x')).toBeNull()
  })

  it('matches https links only, by host, as link previews always did', () => {
    const claims = readHostClaims(['a.example.com', '.example.dev'], 64)
    expect(claimableUrlHost('https://A.example.com./x')).toBe('a.example.com')
    // A port or userinfo does not change the host a link-preview claim matches.
    expect(claimableUrlHost('https://a.example.com:8443/T-1')).toBe('a.example.com')
    expect(claimableUrlHost('https://u:p@a.example.com/')).toBe('a.example.com')
    for (const href of ['http://a.example.com/', 'nope']) {
      expect(claimableUrlHost(href)).toBeNull()
    }
    expect(claimsHost(claims, 'a.example.com')).toBe(true)
    expect(claimsHost(claims, 'x.y.example.dev')).toBe(true)
    expect(claimsHost(claims, 'notexample.dev')).toBe(false)
  })

  it('reads every host when no cap is given, and matches in one lookup per label', () => {
    const raw = Array.from({ length: 5000 }, (_, i) => (i % 2 ? `.d${i}.example.com` : `h${i}.example.com`))
    const claims = readHostClaims(raw)
    expect(claims.exact.size + claims.suffixes.size).toBe(5000)
    expect(claimsHost(claims, 'd4999.example.com')).toBe(true)
    expect(claimsHost(claims, 'a.b.d1.example.com')).toBe(true)
    expect(claimsHost(claims, 'h4998.example.com')).toBe(true)
    expect(claimsHost(claims, 'x.h4998.example.com')).toBe(false)
    // The cost guard, counted instead of timed so it cannot flake: a hover looks
    // up each of the host's labels once, however many hosts the app claims.
    let lookups = 0
    const counted = new Set(claims.suffixes)
    const has = counted.has.bind(counted)
    counted.has = (s: string) => { lookups += 1; return has(s) }
    expect(claimsHost({ exact: claims.exact, suffixes: counted }, 'a.b.c.nomatch.example.org')).toBe(false)
    expect(lookups).toBe(6)
  })

  it('drops the query and fragment by default', () => {
    expect(withoutQuery('https://a.example.com/p?token=1#f')).toBe('https://a.example.com/p')
    expect(withoutQuery('not a url')).toBe('')
  })
})
