import { afterEach, describe, expect, it, vi } from 'vitest'
import { isNavSegment, openInternalLink, originOf, resolveInternalNav, setInternalNavHandler } from './internalNav'

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
    ['/members?member=beehive', '/members?member=beehive'],
    ['/chat', '/chat'],
    [`/chat?sid=${KEY}`, `/chat?sid=${KEY}`],
    [`/chat/some-title?slot=${KEY}`, `/chat?sid=${KEY}`],
    [`/chat?sid=dashboard_${KEY}`, `/chat?sid=${KEY}`],
    [`${OWN}/schedule`, '/schedule'],
    ['/settings/display#theme', '/settings/display#theme'],
    [`/artifacts/${'a'.repeat(128)}`, `/artifacts/${'a'.repeat(128)}`],
    ['/artifacts/a.b', '/artifacts/a.b'],
    ['/artifacts/a_b', '/artifacts/a_b'],
    ['/artifacts/a~b', '/artifacts/a~b'],
    ['/artifacts/a-b', '/artifacts/a-b'],
    ['/settings/display#a.b_c-d', '/settings/display#a.b_c-d'],
    [`/settings/display#${'h'.repeat(128)}`, `/settings/display#${'h'.repeat(128)}`],
    ['/schedule?job=a%C2%A0b', '/schedule?job=a%C2%A0b'],
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
    ['a repeated chat session', `/chat?sid=${KEY}&sid=chat-13-1793224443`],
    ['a chat link naming the session twice', `/chat?sid=${KEY}&slot=chat-13-1793224443`],
    ['an over-long chat session', `/chat?sid=${KEY}${'x'.repeat(600)}`],
    ['a control character in a chat session', `/chat?sid=${KEY}%0A`],
    ['a member page parameter MembersPage does not read', '/members?name=beehive'],
    ['a short session name the roster would have to resolve', '/chat?sid=chat-12'],
    ['an encoded slash in a segment', '/settings/a%2Fb'],
    ['a control character in a value', '/schedule?job=a%0Ab'],
    ['a segment over 128 characters', `/artifacts/${'a'.repeat(129)}`],
    ['a segment character outside the allowed set', '/artifacts/a!b'],
    ['an empty segment', '/settings//display'],
    ['a fragment carrying a tilde', '/settings/display#a~b'],
    ['a fragment over 128 characters', `/settings/display#${'h'.repeat(129)}`],
    ['a C0 control (U+001F) in a value', '/schedule?job=a%1Fb'],
    ['DEL (U+007F) in a value', '/schedule?job=a%7Fb'],
    ['a C1 control (U+009F) in a value', '/schedule?job=a%C2%9Fb'],
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

describe('isNavSegment', () => {
  // The URL parser drops `.` and `..` segments before `resolveInternalNav`
  // reads them, so the refusal is pinned on the check itself.
  it.each([['.'], ['..'], [''], ['a'.repeat(129)], ['a/b'], ['a b']])('refuses %j', (segment) => {
    expect(isNavSegment(segment)).toBe(false)
  })

  it.each([['a'], ['...'], ['.a'], ['a'.repeat(128)], ['A-z_0.9~']])('accepts %j', (segment) => {
    expect(isNavSegment(segment)).toBe(true)
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
