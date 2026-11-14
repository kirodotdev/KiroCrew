import { describe, expect, it } from 'vitest'
import { appWebOrigin } from './webOrigin'

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

  it.each(['', 'Docs', 'a.b', '-x', 'x-', 'a'.repeat(64), 'a_b'])('refuses the app name %j', (name) => {
    expect(appWebOrigin(name, at('localhost'))).toBeNull()
  })
})
