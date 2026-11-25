// Under a sub-path build (KIROCREW_BASE_PATH) every page is served under the
// base, and the DOM shim writes links there. The resolver reads the route with
// the base removed and hands back the router's base-free path.
import { describe, expect, it, vi } from 'vitest'

const BASE = '/proxy/kirocrew'

vi.mock('../lib/basePath', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/basePath')>()
  return {
    ...real,
    BASE_PATH: BASE,
    appPathname: (pathname?: string) => real.appPathname(pathname, BASE),
    withBase: (p: string) => real.withBase(p, BASE),
  }
})

const { resolveInternalNav } = await import('./internalNav')

const OWN = window.location.origin
const KEY = 'chat-12-1793224442'

describe('resolveInternalNav under a sub-path base', () => {
  it.each([
    [`${BASE}/schedule`, '/schedule'],
    [`${BASE}/settings/security/rules?highlight=security.denied-commands`, '/settings/security/rules?highlight=security.denied-commands'],
    [`${OWN}${BASE}/sessions`, '/sessions'],
    [`${BASE}/chat?sid=${KEY}`, `/chat?sid=${KEY}`],
    [`${BASE}/chat`, '/chat'],
  ])('opens %s in place as %s', (href, want) => {
    expect(resolveInternalNav(href)).toBe(want)
  })

  it.each([
    ['the bare base', BASE],
    ['an unlisted page under the base', `${BASE}/projects`],
    ['another prefix that only starts like the base', '/proxy/kirocrew-other/schedule'],
  ])('refuses %s', (_label, href) => {
    expect(resolveInternalNav(href)).toBeNull()
  })
})
