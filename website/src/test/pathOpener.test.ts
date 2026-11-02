/**
 * `pathOpenerTarget` (`apps/pathOpener.ts`): which registered page a surface
 * showing a file may hand the path to, read from synthetic registry rows so the
 * rule is pinned without any particular app.
 */
import { lazy } from 'react'
import { describe, expect, it } from 'vitest'

import type { BuiltinAppEntry } from '../apps/builtinRegistry'
import { pathOpenerLink, pathOpenerTarget } from '../apps/pathOpener'

const component = lazy(async () => ({ default: () => null }))
const entry = (appId: string, opensPathParam?: string): BuiltinAppEntry => ({ component, appId, opensPathParam })

const installed = (name: string, route: string, extra: Record<string, unknown> = {}) => ({
  name,
  displayName: name.toUpperCase(),
  enabled: true,
  origin: 'builtin',
  manifest: { ui: { pages: [{ route, label: `${name} page` }] } },
  ...extra,
})

describe('pathOpenerTarget', () => {
  it('is null when no registered page declares a path parameter', () => {
    const registry = { '/a': entry('app-a') }
    expect(pathOpenerTarget([installed('app-a', '/a')], registry)).toBeNull()
  })

  it('names the declaring page by its nav label, route and parameter', () => {
    const registry = { '/a': entry('app-a'), '/b': entry('app-b', 'path') }
    const apps = [installed('app-a', '/a'), installed('app-b', '/b')]
    expect(pathOpenerTarget(apps, registry)).toEqual({ label: 'app-b page', route: '/b', param: 'path' })
  })

  it('takes the first declaring page in registration order', () => {
    const registry = { '/b': entry('app-b', 'p'), '/c': entry('app-c', 'q') }
    const apps = [installed('app-c', '/c'), installed('app-b', '/b')]
    expect(pathOpenerTarget(apps, registry)?.route).toBe('/b')
  })

  it('skips a page whose app is missing, disabled, orphaned or AppHost-routed', () => {
    const registry = { '/b': entry('app-b', 'path'), '/c': entry('app-c', 'path') }
    for (const b of [
      [],
      [installed('app-b', '/b', { enabled: false })],
      [installed('app-b', '/b', { orphaned: true })],
      [installed('app-b', '/b', { manifest: { ui: { pages: [{ route: '/b', label: 'b' }], entry: 'ui/index.js' } } })],
    ]) {
      expect(pathOpenerTarget([...b, installed('app-c', '/c')], registry)?.route).toBe('/c')
    }
  })

  it('skips a parameter that would need encoding to sit in the link', () => {
    const registry = { '/b': entry('app-b', 'a&b'), '/c': entry('app-c', 'path') }
    const apps = [installed('app-b', '/b'), installed('app-c', '/c')]
    expect(pathOpenerTarget(apps, registry)?.route).toBe('/c')
  })
})

describe('pathOpenerLink', () => {
  it('URL-encodes the path under the declared parameter', () => {
    const url = new URL(pathOpenerLink({ route: '/b', param: 'path' }, '/home/u/notes & drafts/a#1.md'), 'http://x')
    expect(url.pathname).toBe('/b')
    expect(url.searchParams.get('path')).toBe('/home/u/notes & drafts/a#1.md')
  })
})
