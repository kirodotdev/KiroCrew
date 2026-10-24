import { describe, it, expect } from 'vitest'

import { previewCookieExport } from '../lib/cookieExportPreview'

const NOW = 1_700_000_000

describe('previewCookieExport', () => {
  it('reads a Playwright storageState object', () => {
    const text = JSON.stringify({
      cookies: [
        { name: 'a', value: 'v', domain: '.example.com', expires: -1 },
        { name: 'b', value: 'v', domain: 'example.com', expires: NOW + 60 },
        { name: 'c', value: 'v', domain: 'sso.corp' },
      ],
    })
    expect(previewCookieExport(text, NOW)).toEqual({ cookieCount: 3, domains: ['example.com', 'sso.corp'] })
  })

  it('reads a bare extension array, using expirationDate and dropping expired cookies', () => {
    const text = JSON.stringify([
      { name: 'a', domain: 'live.example', expirationDate: NOW + 10 },
      { name: 'b', domain: 'gone.example', expirationDate: NOW - 10 },
    ])
    expect(previewCookieExport(text, NOW)).toEqual({ cookieCount: 1, domains: ['live.example'] })
  })

  it('reads a Netscape cookies.txt, including the #HttpOnly_ prefix and an empty value', () => {
    const text = [
      '# Netscape HTTP Cookie File',
      '.example.com\tTRUE\t/\tTRUE\t0\tsid\tabc',
      '#HttpOnly_api.example.com\tFALSE\t/\tTRUE\t0\ttok\t',
      'short\tline',
    ].join('\n')
    expect(previewCookieExport(text, NOW)).toEqual({ cookieCount: 2, domains: ['api.example.com', 'example.com'] })
  })

  it.each([
    ['blank', '   '],
    ['not JSON', '{nope'],
    ['a JSON scalar', '[1]'],
    ['an object without cookies', '{"origins":[]}'],
    ['a cookie without a domain', '[{"name":"a"}]'],
    ['a cookie without a name', '[{"domain":"a.example"}]'],
    ['nothing but expired cookies', JSON.stringify([{ name: 'a', domain: 'a.example', expires: NOW - 1 }])],
    ['text with no cookie rows', 'hello world'],
  ])('answers null for %s', (_label, text) => {
    expect(previewCookieExport(text, NOW)).toBeNull()
  })

  it('answers null past the import count limit', () => {
    const many = Array.from({ length: 5001 }, (_, i) => ({ name: `n${i}`, domain: 'a.example' }))
    expect(previewCookieExport(JSON.stringify(many), NOW)).toBeNull()
  })

  it('never returns a cookie value', () => {
    const text = JSON.stringify([{ name: 'a', value: 'top-secret', domain: 'a.example' }])
    expect(JSON.stringify(previewCookieExport(text, NOW))).not.toContain('top-secret')
  })
})
