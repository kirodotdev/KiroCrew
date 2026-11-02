import { describe, expect, it } from 'vitest'

import { normalizeLocalOrigin, previewAllowsUrl } from '../utils/localPreviewOrigins'

/**
 * The Preview tab's allowlist rule, shared by Settings and the panel. The IPv6
 * case is the one that went wrong: the URL API already brackets `[::1]`, and a
 * second pair (`[[::1]]:5173`) never matched a configured `[::1]:5173`.
 */
describe('previewAllowsUrl', () => {
  const origins = ['localhost:5173', '127.0.0.1:4200', '[::1]:5173']

  it('admits each loopback spelling the list names, on its port', () => {
    expect(previewAllowsUrl('http://localhost:5173/app', origins)).toBe(true)
    expect(previewAllowsUrl('http://127.0.0.1:4200/', origins)).toBe(true)
    expect(previewAllowsUrl('http://[::1]:5173/', origins)).toBe(true)
  })

  it('refuses another port, another host, a non-web scheme', () => {
    expect(previewAllowsUrl('http://localhost:3000/', origins)).toBe(false)
    expect(previewAllowsUrl('http://[::1]:4200/', origins)).toBe(false)
    expect(previewAllowsUrl('http://localhost.evil.com:5173/', origins)).toBe(false)
    expect(previewAllowsUrl('file:///etc/passwd', origins)).toBe(false)
    expect(previewAllowsUrl('not a url', origins)).toBe(false)
  })

  it('fills in the scheme default port', () => {
    expect(previewAllowsUrl('http://localhost/', ['localhost:80'])).toBe(true)
    expect(previewAllowsUrl('https://127.0.0.1/', ['127.0.0.1:443'])).toBe(true)
  })
})

describe('normalizeLocalOrigin', () => {
  it('keeps the three loopback spellings and a pasted dev-server URL', () => {
    expect(normalizeLocalOrigin(' LOCALHOST:5173 ')).toBe('localhost:5173')
    expect(normalizeLocalOrigin('http://127.0.0.1:4200/')).toBe('127.0.0.1:4200')
    expect(normalizeLocalOrigin('http://[::1]:5173')).toBe('[::1]:5173')
  })
  it('refuses what the gateway would drop', () => {
    expect(normalizeLocalOrigin('localhost:0')).toBeNull()
    expect(normalizeLocalOrigin('localhost:70000')).toBeNull()
    expect(normalizeLocalOrigin('http://localhost:5173/path')).toBeNull()
    expect(normalizeLocalOrigin('0.0.0.0:5173')).toBeNull()
  })
})
