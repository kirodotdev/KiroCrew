import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const embedInstanceToken = vi.fn()
vi.mock('../api/client', () => ({ api: { embedInstanceToken: (...a: unknown[]) => embedInstanceToken(...a) } }))

import { pagePort, paneCredentialFor, tokenParentPort } from './paneCredential'

/** A dashboard token's shape, `base64url(payload).signature`, naming `port` (or nothing). */
function tokenNaming(port: number | string | null, extra: Record<string, unknown> = {}): string {
  const payload = port === null ? { sub: 'owner', ...extra } : { sub: 'owner', embed_parent_port: String(port), ...extra }
  const b64 = btoa(JSON.stringify(payload)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')
  return `${b64}.c2lnbmF0dXJl`
}

/** Serve this test's page from `url` (the suite default is http://localhost:6776/). */
function servePageAt(url: string) {
  ;(window as unknown as { happyDOM: { setURL(u: string): void } }).happyDOM.setURL(url)
}

describe('tokenParentPort', () => {
  it('reads the port a token names', () => {
    expect(tokenParentPort(tokenNaming(5476))).toBe(5476)
  })

  it('reads a payload whose base64url spelling uses the url-safe characters', () => {
    // This pad puts both `+` and `/` in the standard spelling, so the token
    // carries both `-` and `_` and the substitution has to be right for each.
    const token = tokenNaming(5476, { pad: '?>?>?>~' })
    expect(token.split('.')[0]).toContain('-')
    expect(token.split('.')[0]).toContain('_')
    expect(tokenParentPort(token)).toBe(5476)
  })

  it('is null for a token naming no port, or one it cannot read', () => {
    expect(tokenParentPort(tokenNaming(null))).toBeNull()
    expect(tokenParentPort('tok')).toBeNull()
    expect(tokenParentPort('')).toBeNull()
    expect(tokenParentPort('%%%.sig')).toBeNull()
    expect(tokenParentPort(tokenNaming('05476'))).toBeNull()
    expect(tokenParentPort(tokenNaming(70000))).toBeNull()
  })
})

describe('pagePort', () => {
  it('is the explicit port, else the scheme default', () => {
    expect(pagePort('http:', '5477')).toBe(5477)
    expect(pagePort('http:', '')).toBe(80)
    expect(pagePort('https:', '')).toBe(443)
    expect(pagePort('file:', '')).toBeNull()
  })
})

describe('paneCredentialFor', () => {
  beforeEach(() => { embedInstanceToken.mockReset() })
  afterEach(() => {
    servePageAt('http://localhost:6776/')
    vi.restoreAllMocks()
  })

  it('keeps a token that already names this page, asking nothing (the default path)', async () => {
    servePageAt('http://127.0.0.1:5476/')
    const conn = { port: 7778, token: tokenNaming(5476) }
    await expect(paneCredentialFor('c', conn)).resolves.toBe(conn)
    expect(embedInstanceToken).not.toHaveBeenCalled()
  })

  it('keeps a token naming no port, asking nothing', async () => {
    servePageAt('http://127.0.0.1:5477/')
    const conn = { port: 7778, token: 'opaque' }
    await expect(paneCredentialFor('c', conn)).resolves.toBe(conn)
    expect(embedInstanceToken).not.toHaveBeenCalled()
  })

  it('asks for a token naming this page when the gateway token names another port', async () => {
    servePageAt('http://127.0.0.1:5477/')
    const lines: string[] = []
    vi.spyOn(console, 'info').mockImplementation((...a: unknown[]) => { lines.push(a.join(' ')) })
    embedInstanceToken.mockResolvedValue({ token: 'page-token', port: 7778, ttl: '30m' })
    const conn = await paneCredentialFor('c', { port: 7778, token: tokenNaming(5476) })
    expect(embedInstanceToken).toHaveBeenCalledWith('c', 5477)
    expect(conn).toEqual({ port: 7778, token: 'page-token' })
    expect(lines).toEqual(['[pane] pane-credential id=c port=7778 gatewayPort=5476 pagePort=5477'])
  })

  it('pairs the token with the forward the gateway names for it', async () => {
    // A rebuild between the connect and this mint moves the forward; the token
    // and the port the gateway answered with are one claim.
    servePageAt('http://localhost:5477/')
    embedInstanceToken.mockResolvedValue({ token: 'page-token', port: 7790, ttl: '30m' })
    await expect(paneCredentialFor('c', { port: 7778, token: tokenNaming(5476) })).resolves.toEqual({
      port: 7790,
      token: 'page-token',
    })
  })

  it('asks nothing on a page that cannot embed a pane', async () => {
    servePageAt('https://crew.example.com/')
    const conn = { port: 7778, token: tokenNaming(5476) }
    await expect(paneCredentialFor('c', conn)).resolves.toBe(conn)
    expect(embedInstanceToken).not.toHaveBeenCalled()
  })

  it('rejects rather than hand back a credential the crew would refuse', async () => {
    servePageAt('http://127.0.0.1:5477/')
    embedInstanceToken.mockRejectedValue(new Error('chain_too_deep'))
    await expect(paneCredentialFor('c', { port: 7778, token: tokenNaming(5476) })).rejects.toThrow('chain_too_deep')
    embedInstanceToken.mockResolvedValue({ token: '', port: 7778 })
    await expect(paneCredentialFor('c', { port: 7778, token: tokenNaming(5476) })).rejects.toThrow()
    embedInstanceToken.mockResolvedValue({ token: 'page-token', port: 0 })
    await expect(paneCredentialFor('c', { port: 7778, token: tokenNaming(5476) })).rejects.toThrow()
  })
})
