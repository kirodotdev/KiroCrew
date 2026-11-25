import { describe, expect, it } from 'vitest'
import { appWebOrigin, appWebOriginStatus } from './webOrigin'

const at = (hostname: string, port = '5476', protocol = 'http:') => ({ hostname, port, protocol })

describe('appWebOrigin', () => {
  it.each(['localhost', '127.0.0.1', 'kirocrew.localhost', 'LOCALHOST'])(
    'names the app site on the dashboard port from %s',
    (hostname) => {
      expect(appWebOrigin('docs', at(hostname))).toBe('http://docs.apps.localhost:5476')
    },
  )

  it('keeps the port the browser used, so a tunnel on another local port still matches', () => {
    expect(appWebOrigin('docs', at('localhost', '6000'))).toBe('http://docs.apps.localhost:6000')
    expect(appWebOrigin('docs', at('localhost', ''))).toBe('http://docs.apps.localhost:80')
  })

  it.each([
    ['an IPv6 dashboard', at('[::1]')],
    ['a Remote Crew pane name', at('box.localhost')],
    ['a published host', at('crew.example.com')],
    ['https', at('localhost', '5476', 'https:')],
  ])('has none on %s', (_label, location) => {
    expect(appWebOrigin('docs', location)).toBeNull()
  })

  it.each(['a', '0', 'a-b', 'a'.repeat(63)])('accepts the app name %j', (name) => {
    expect(appWebOrigin(name, at('localhost'))).toBe(`http://${name}.apps.localhost:5476`)
  })

  it.each(['', 'Docs', 'a.b', '-x', 'x-', 'a'.repeat(64), 'a_b', 'é', 'a\n'])('refuses the app name %j', (name) => {
    expect(appWebOrigin(name, at('localhost'))).toBeNull()
  })
})

describe('appWebOriginStatus', () => {
  it('answers the origin appWebOrigin answers, with the reason loopback', () => {
    expect(appWebOriginStatus('docs', at('localhost', '7778'))).toEqual({
      origin: 'http://docs.apps.localhost:7778',
      reason: 'loopback',
    })
  })

  it.each([
    ['https', at('localhost', '5476', 'https:')],
    ['a tailnet or tunnel host', at('node.tailnet.ts.net', '')],
    ['a relay viewer name', at('kcr-abc.localhost')],
    ['a published host', at('crew.example.com')],
  ])('says to open upstream on %s', (_label, location) => {
    expect(appWebOriginStatus('docs', location)).toEqual({ origin: null, reason: 'remote-front-door' })
  })

  it('names IPv6 loopback, which no frame ancestor can express', () => {
    expect(appWebOriginStatus('docs', at('[::1]'))).toEqual({ origin: null, reason: 'ipv6' })
  })

  it('names a bad app name before anything about the location', () => {
    expect(appWebOriginStatus('Docs', at('[::1]'))).toEqual({ origin: null, reason: 'invalid-name' })
  })
})
