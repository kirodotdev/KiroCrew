import { describe, expect, it } from 'vitest'
import type { McpServer } from '../types'
import {
  connectionStateFor,
  disconnectFeedback,
  effectiveOAuth,
  isValidLoopbackReturnAddress,
  uninstallOnCancel,
  type OAuthState,
} from '../pages/connections/ConnectionsPage'

const server = (status: string): McpServer => ({
  name: 'notion',
  command: '',
  url: 'https://mcp.notion.com/mcp',
  status,
  source: 'mcp.json',
  enabled: true,
})

describe('Connections card states', () => {
  it('covers not connected, waiting, connected, and needs-attention states', () => {
    expect(connectionStateFor(undefined, undefined)).toBe('not-connected')
    expect(connectionStateFor(server('unknown'), undefined)).toBe('waiting-for-approval')
    expect(connectionStateFor(server('ok'), undefined)).toBe('connected')
    expect(connectionStateFor(server('error'), undefined)).toBe('needs-attention')
  })

  /**
   * #1853: a tokenless probe of a remote OAuth server returns `needs_auth`. That
   * must read as "we cannot see this server's authorization" — not as a broken
   * connection, and not as the perpetual "waiting for approval" spinner the
   * fallthrough produced before this state existed.
   */
  it('reports needs_auth as not-verified rather than an error card', () => {
    expect(connectionStateFor(server('needs_auth'), undefined)).toBe('not-verified')
  })

  it('lets live OAuth evidence outrank a tokenless needs_auth probe', () => {
    const oauth = (over: Partial<OAuthState>): OAuthState => ({
      completed: false, failed: false, oauthUrl: '', error: '', timestamp: 1, ...over,
    })
    // A grant in flight owns the card: the spinner belongs to that attempt.
    expect(connectionStateFor(server('needs_auth'), undefined, true)).toBe('waiting-for-approval')
    expect(connectionStateFor(server('needs_auth'), oauth({ oauthUrl: 'https://example.com/auth' })))
      .toBe('waiting-for-approval')
    // A completed grant is stronger evidence than a probe that cannot see tokens.
    expect(connectionStateFor(server('needs_auth'), oauth({ completed: true }))).toBe('connected')
    // A refused grant is a real failure and still reads as one.
    expect(connectionStateFor(server('needs_auth'), oauth({ failed: true, error: 'denied' })))
      .toBe('needs-attention')
  })

  it('keeps a newly added authorization error waiting until OAuth resolves', () => {
    expect(connectionStateFor(server('error'), undefined, true)).toBe('waiting-for-approval')
    expect(connectionStateFor(server('error'), {
      completed: false,
      failed: true,
      oauthUrl: '',
      error: 'denied',
      timestamp: 1,
    }, true)).toBe('needs-attention')
  })
})

describe('stale OAuth banner fencing', () => {
  const banner = (timestamp: number, completed = true): OAuthState => ({
    completed,
    failed: !completed,
    oauthUrl: '',
    error: completed ? '' : 'denied',
    timestamp,
  })

  it('ignores banners at or below the click-time snapshot', () => {
    // The banner observed at click time (ts 100) must not resurface…
    const same = effectiveOAuth(banner(100), { kind: 'reconnect', sinceTs: 100 })
    expect(same).toBeUndefined()
    expect(connectionStateFor(server('unknown'), same, true)).toBe('waiting-for-approval')
    // …nor anything even older.
    expect(effectiveOAuth(banner(50), { kind: 'reconnect', sinceTs: 100 })).toBeUndefined()
  })

  it('honors banners raised after the snapshot', () => {
    const fresh = banner(300)
    expect(effectiveOAuth(fresh, { kind: 'reconnect', sinceTs: 100 })).toBe(fresh)
    expect(connectionStateFor(server('unknown'), fresh, true)).toBe('connected')
  })

  it('accepts the first banner when none existed at click time', () => {
    const first = banner(1)
    expect(effectiveOAuth(first, { kind: 'new', sinceTs: 0 })).toBe(first)
  })

  it('leaves banners untouched when no attempt is pending', () => {
    const oauth = banner(100)
    expect(effectiveOAuth(oauth, undefined)).toBe(oauth)
  })
})

describe('cancel semantics', () => {
  it('uninstalls only a cancelled new connect', () => {
    expect(uninstallOnCancel({ kind: 'new', sinceTs: 0 })).toBe(true)
    expect(uninstallOnCancel({ kind: 'reconnect', sinceTs: 0 })).toBe(false)
    expect(uninstallOnCancel(undefined)).toBe(false)
  })
})

describe('disconnect feedback', () => {
  it('keeps the provider revoke destination after removing the local entry', () => {
    expect(disconnectFeedback({
      name: 'Notion',
      revoke_page_url: 'https://www.notion.so/my-integrations',
    }, 'Disconnected locally.')).toEqual({
      kind: 'success',
      text: 'Disconnected locally.',
      revoke: {
        href: 'https://www.notion.so/my-integrations',
        provider: 'Notion',
      },
    })
  })
})

describe('loopback OAuth return-address validation', () => {
  it('accepts only an IP-literal loopback callback with a port and code', () => {
    expect(isValidLoopbackReturnAddress('http://127.0.0.1:43123/?code=one-time&state=s')).toBe(true)
    expect(isValidLoopbackReturnAddress('http://[::1]:43123/callback?code=one-time')).toBe(true)
  })

  it.each([
    'https://127.0.0.1:43123/?code=x',
    'http://localhost:43123/?code=x',
    'http://10.0.0.5:43123/?code=x',
    'http://127.0.0.1/?code=x',
    'http://127.0.0.1:43123/',
    'http://user@127.0.0.1:43123/?code=x',
    'http://127.0.0.1:43123/?code=x&code=y',
    'http://127.0.0.1:43123/?code=x#fragment',
  ])('rejects unsafe or incomplete return address %s', value => {
    expect(isValidLoopbackReturnAddress(value)).toBe(false)
  })
})
