import { afterEach, describe, expect, it, vi } from 'vitest'
import { openInternalLink, originOf, resolveInternalNav, setInternalNavHandler } from './internalNav'

const OWN = window.location.origin
const KEY = 'chat-12-1793224442'
const TUNNEL = 'https://crew.tunnel.example.test'

describe('resolveInternalNav', () => {
  it.each([
    ['/schedule', '/schedule'],
    ['/schedule?job=nightly', '/schedule?job=nightly'],
    ['/settings/security/rules?highlight=security.denied-commands', '/settings/security/rules?highlight=security.denied-commands'],
    ['/settings/', '/settings'],
    ['/capabilities?tab=knowledge', '/capabilities?tab=knowledge'],
    ['/artifacts', '/artifacts'],
    ['/artifacts/q3-report', '/artifacts/q3-report'],
    ['/members?name=beehive', '/members?name=beehive'],
    ['/chat', '/chat'],
    [`/chat?sid=${KEY}`, `/chat?sid=${KEY}`],
    [`/chat/some-title?slot=${KEY}`, `/chat?sid=${KEY}`],
    [`/chat?sid=dashboard_${KEY}`, `/chat?sid=${KEY}`],
    [`${OWN}/schedule`, '/schedule'],
    ['/settings/display#theme', '/settings/display#theme'],
  ])('opens %s in place as %s', (href, want) => {
    expect(resolveInternalNav(href)).toBe(want)
  })

  it.each([
    ['an external site', 'https://example.test/schedule'],
    ['a protocol-relative host', '//example.test/schedule'],
    ['a backslash host', '/\\example.test/schedule'],
    ['another loopback port', 'http://127.0.0.1:1/schedule'],
    ['a non-web scheme', 'javascript:alert(1)'],
    ['credentials in the URL', `${OWN.replace('://', '://u:p@')}/schedule`],
    ['an unlisted page', '/projects'],
    ['an app page', '/apps/some-app'],
    ['a popout route', '/popout/chat/x'],
    ['an embed route', '/embed/settings'],
    ['a remote artifact', '/artifacts/remote/github/1'],
    ['an unlisted parameter', '/schedule?run=1'],
    ['a parameter another page allows', '/schedule?tab=x'],
    ['a repeated parameter', '/schedule?job=a&job=b'],
    ['a chat link carrying anything but the session', `/chat?sid=${KEY}&msg=3`],
    ['a short session name the roster would have to resolve', '/chat?sid=chat-12'],
    ['an encoded slash in a segment', '/settings/a%2Fb'],
    ['a control character in a value', '/schedule?job=a%0Ab'],
    ['an unusual fragment', '/settings#a b'],
    ['a fragment-only link', '#heading'],
    ['an empty href', ''],
  ])('refuses %s', (_label, href) => {
    expect(resolveInternalNav(href)).toBeNull()
  })

  it('accepts a configured origin only when it is listed, and only that exact origin', () => {
    expect(resolveInternalNav(`${TUNNEL}/schedule`)).toBeNull()
    expect(resolveInternalNav(`${TUNNEL}/schedule`, [TUNNEL])).toBe('/schedule')
    expect(resolveInternalNav(`${TUNNEL}:8443/schedule`, [TUNNEL])).toBeNull()
    expect(resolveInternalNav('http://crew.tunnel.example.test/schedule', [TUNNEL])).toBeNull()
  })
})

describe('originOf', () => {
  it('reduces a configured URL to its origin and drops anything that is not a web URL', () => {
    expect(originOf('https://crew.example.test/some/path?x=1')).toBe('https://crew.example.test')
    expect(originOf('  http://host.example.test:8080  ')).toBe('http://host.example.test:8080')
    expect(originOf('')).toBeNull()
    expect(originOf('not a url')).toBeNull()
    expect(originOf('file:///etc/hosts')).toBeNull()
    expect(originOf(undefined)).toBeNull()
  })
})

describe('openInternalLink', () => {
  let uninstall: (() => void) | null = null
  afterEach(() => { uninstall?.(); uninstall = null })

  it('hands an accepted link to the installed navigator and reports it', () => {
    const open = vi.fn()
    uninstall = setInternalNavHandler({ origins: [TUNNEL], open })
    expect(openInternalLink(`${TUNNEL}/settings/security`)).toBe(true)
    expect(open).toHaveBeenCalledWith('/settings/security')
  })

  it('reports false, and navigates nothing, for a refused link', () => {
    const open = vi.fn()
    uninstall = setInternalNavHandler({ origins: [], open })
    expect(openInternalLink('https://example.test/')).toBe(false)
    expect(open).not.toHaveBeenCalled()
  })

  it('reports false when no navigator is installed, so the caller keeps its new-tab open', () => {
    expect(openInternalLink('/schedule')).toBe(false)
  })

  it('leaves a newer registration in place when an older one uninstalls', () => {
    const first = vi.fn()
    const second = vi.fn()
    const dropFirst = setInternalNavHandler({ origins: [], open: first })
    uninstall = setInternalNavHandler({ origins: [], open: second })
    dropFirst()
    expect(openInternalLink('/schedule')).toBe(true)
    expect(second).toHaveBeenCalledWith('/schedule')
    expect(first).not.toHaveBeenCalled()
  })
})
