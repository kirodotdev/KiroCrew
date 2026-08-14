/**
 * builtinRegistry — the builtin route → lazy page map and its extension seam.
 *
 * Two things are checked. First, every registered route's lazy factory actually
 * resolves to a module with a default export: a mistyped import path or a page
 * that lost its default export is invisible until a user navigates there and
 * gets a white screen, and nothing else in the suite pulls those factories.
 * Second, the seam's route shape guard — `BuiltinAppRoute` resolves ONE path
 * parameter from `location.pathname`, so a route that is not a single plain
 * segment would register and then never resolve, silently vanishing.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { lazy } from 'react'

// The editor pages (/papyrus, /md-notebook) eagerly pull Monaco, whose real
// bundle costs seconds to transform for no benefit here — this test only needs
// each page module to EVALUATE and expose a default export. Stubbed the same
// way DiffPanel's own suites stub it.
vi.mock('@monaco-editor/react', () => ({
  default: () => null,
  Editor: () => null,
  DiffEditor: () => null,
  useMonaco: () => null,
  loader: { config: () => {}, init: () => Promise.resolve({}) },
}))

// `/papyrus` reaches ChatPage through CoAuthorPanel, which drags the ENTIRE chat
// tree (chat rows, markdown, KaTeX, the tool panels) into this file's transform
// budget — on its own that one route cost ~4.8s locally, i.e. the only test here
// anywhere near a CI runner's per-test ceiling. ChatPage is not a lazy builtin:
// App.tsx imports it eagerly and its own suites assert its default export, so
// re-proving it here buys nothing. Stubbed so each route test stays ~sub-second.
vi.mock('../pages/ChatPage', () => ({ default: () => null }))

import {
  BUILTIN_COMPONENT_REGISTRY,
  getBuiltinComponent,
  hasBuiltinComponent,
  registerBuiltinComponents,
  type LazyComponent,
} from './builtinRegistry'

/**
 * The import factory React.lazy was handed. Reaching for `_payload` is the only
 * way to run the factory without mounting the page in a Suspense tree, which
 * would drag every page's provider requirements into this unit test.
 */
function importFactory(component: LazyComponent): () => Promise<{ default?: unknown }> {
  // React.lazy stores an uninitialized thenable as `_payload = { _status: -1,
  // _result: ctor }`; `_result` is the import factory until the first render.
  const payload = (component as unknown as {
    _payload: { _status: number; _result: () => Promise<{ default?: unknown }> }
  })._payload
  expect(payload._status).toBe(-1)
  return payload._result
}

const CORE_ROUTES = Object.keys(BUILTIN_COMPONENT_REGISTRY)

describe('BUILTIN_COMPONENT_REGISTRY', () => {
  it('registers every builtin under a single plain path segment', () => {
    expect(CORE_ROUTES.length).toBeGreaterThan(15)
    for (const route of CORE_ROUTES) {
      expect(route).toMatch(/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/)
    }
  })

  // One case per route, not one loop in one test: a cold CI runner pays Vite's
  // transform cost for whichever route's subtree lands first, and a single case
  // carrying all 21 modules puts that whole bill against ONE per-test timeout.
  // Timeout raised past the 15s default because the bill is runner-speed-bound,
  // not assertion-bound — locally every case is comfortably sub-2s.
  it.each(CORE_ROUTES)(
    '%s resolves to a module with a default export',
    async (route) => {
      const mod = await importFactory(BUILTIN_COMPONENT_REGISTRY[route])()
      expect(mod.default).toBeTruthy()
    },
    30_000,
  )
})

describe('registerBuiltinComponents', () => {
  const Dummy = () => null

  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('adds a downstream route and resolves it', () => {
    const Comp = lazy(async () => ({ default: Dummy }))
    registerBuiltinComponents({ '/zzq-edition-page': Comp })
    expect(hasBuiltinComponent('/zzq-edition-page')).toBe(true)
    expect(getBuiltinComponent('/zzq-edition-page')).toBe(Comp)
  })

  it('registers several entries in one call', () => {
    const a = lazy(async () => ({ default: Dummy }))
    const b = lazy(async () => ({ default: Dummy }))
    registerBuiltinComponents({ '/zzq-multi-a': a, '/zzq-multi-b': b })
    expect(getBuiltinComponent('/zzq-multi-a')).toBe(a)
    expect(getBuiltinComponent('/zzq-multi-b')).toBe(b)
  })

  it('lets the core win a duplicate route, loudly in dev/test', () => {
    const core = BUILTIN_COMPONENT_REGISTRY['/worlds']
    const shadow = lazy(async () => ({ default: Dummy }))
    expect(() => registerBuiltinComponents({ '/worlds': shadow })).toThrow(/already registered/)
    expect(getBuiltinComponent('/worlds')).toBe(core)
  })

  it.each([
    '/zzq-reports/daily', // extra segment — would match /zzq-reports
    '/zzq-reports?daily', // query is not in the pathname
    '/zzq-reports#top', // hash is not in the pathname
    '/zzq reports', // whitespace
    '/.', // dot segments cannot start with an alphanumeric
    '/..',
    'zzq-no-slash',
    '/',
  ])('refuses %s, which could never resolve', (route) => {
    expect(() => registerBuiltinComponents({ [route]: lazy(async () => ({ default: Dummy })) })).toThrow(
      /never resolve/,
    )
    expect(hasBuiltinComponent(route)).toBe(false)
  })

  it('rejects a bad route without dropping the good ones beside it', () => {
    const good = lazy(async () => ({ default: Dummy }))
    expect(() =>
      registerBuiltinComponents({
        '/zzq-good-first': good,
        '/zzq-bad/second': lazy(async () => ({ default: Dummy })),
      }),
    ).toThrow(/never resolve/)
    expect(getBuiltinComponent('/zzq-good-first')).toBe(good)
  })
})

describe('lookup helpers', () => {
  it('reports unknown routes as absent', () => {
    expect(hasBuiltinComponent('/zzq-unknown')).toBe(false)
    expect(getBuiltinComponent('/zzq-unknown')).toBeUndefined()
  })
})
